import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, addDeal, OWNER, MARCO, GROUP, DAYTIME } from "./helpers.js";
import { handleRequest } from "../src/app.js";
import { verifyInitData } from "../src/webapp.js";

const LUCA = 777;
const enc = new TextEncoder();

async function hmac(key, data) {
  const k = await crypto.subtle.importKey("raw", typeof key === "string" ? enc.encode(key) : key,
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return crypto.subtle.sign("HMAC", k, enc.encode(data));
}

/** initData exactly as Telegram signs it. */
async function initData(user, { token = "TOKEN", authDate = DAYTIME - 60 } = {}) {
  const fields = { auth_date: String(authDate), query_id: "AAE123", user: JSON.stringify(user) };
  const check = Object.keys(fields).sort().map((k) => `${k}=${fields[k]}`).join("\n");
  const hash = [...new Uint8Array(await hmac(await hmac("WebAppData", token), check))]
    .map((b) => b.toString(16).padStart(2, "0")).join("");
  return new URLSearchParams({ ...fields, hash }).toString();
}

const people = { marco: { id: MARCO, first_name: "Marco" }, luca: { id: LUCA, first_name: "Luca" },
  owner: { id: OWNER, first_name: "Owner" }, stranger: { id: 4242, first_name: "Eve" } };

async function app(t, who, path, body, raw) {
  const auth = raw ?? `tma ${await initData(people[who])}`;
  const r = await handleRequest(new Request(`https://w/app/api/${path}`, { method: body ? "POST" : "GET",
    headers: { authorization: auth, "content-type": "application/json" }, body: body ? JSON.stringify(body) : undefined }),
  t.env, { fetchFn: t.tg.fetch, now: t.now });
  return { status: r.status, body: await r.json() };
}

const members = { allowed_users: [MARCO, LUCA], topics: { summary: 44 } };

test("Telegram's signature is checked: tampered, expired or missing data is refused", async () => {
  const good = await initData(people.marco);
  assert.equal((await verifyInitData(good, "TOKEN", DAYTIME)).id, MARCO);
  assert.equal(await verifyInitData(good, "OTHER-TOKEN", DAYTIME), null);
  assert.equal(await verifyInitData(good.replace("Marco", "Mallory"), "TOKEN", DAYTIME), null);
  assert.equal(await verifyInitData(await initData(people.marco, { authDate: DAYTIME - 2 * 86400 }), "TOKEN", DAYTIME), null);
  assert.equal(await verifyInitData("", "TOKEN", DAYTIME), null);
});

test("only allowed users get the app's data", async () => {
  const t = await setup({ settings: members });
  await addDeal(t.store);
  assert.equal((await app(t, "marco", "state", null, "tma nonsense")).status, 401);
  assert.equal((await app(t, "marco", "state", null, "")).status, 401);
  assert.equal((await app(t, "stranger", "state")).status, 403);
  const r = await app(t, "marco", "state");
  assert.equal(r.status, 200);
  assert.deepEqual(r.body.me, { id: MARCO, name: "Marco", owner: false, manager: false, team: false });
  assert.equal(r.body.deals[0].title, "Boss DS-1 distortion");
  assert.equal(r.body.deals[0].source, "vinted");
  assert.equal(r.body.deals[0].sell_days, null);
  assert.equal(r.body.deals[0].demand, "");
  assert.ok(Array.isArray(r.body.settings.searches) && r.body.settings.searches.length === 3);
  assert.ok(!("text" in r.body.deals[0]) && !("item" in r.body.deals[0]));   // only what the screen needs
  assert.equal((await app(t, "owner", "state")).body.me.owner, true);
});

test("old untouched deals are hidden; claimed ones stay; stock is separate", async () => {
  const t = await setup({ settings: members });
  await addDeal(t.store);
  await t.store.addDeal("vinted:2", { title: "old", text: "x", sent: DAYTIME - 8 * 86400 });
  await t.store.addDeal("vinted:3", { title: "old but claimed", text: "x", sent: DAYTIME - 8 * 86400 });
  const d3 = await t.store.deal("vinted:3");
  Object.assign(d3, { status: "claimed", who: "Luca", who_id: LUCA });
  await t.store.saveDeal("vinted:3", d3);
  await t.store.addDeal("vinted:4", { title: "in stock", text: "x", sent: DAYTIME });
  const d4 = await t.store.deal("vinted:4");
  Object.assign(d4, { status: "bought", who: "Marco", who_id: MARCO, paid: 20 });
  await t.store.saveDeal("vinted:4", d4);
  const s = (await app(t, "marco", "state")).body;
  assert.deepEqual(s.deals.map((x) => x.title).sort(), ["Boss DS-1 distortion", "old but claimed"]);
  assert.deepEqual(s.stock.map((x) => [x.title, x.mine]), [["in stock", true]]);
});

test("claiming in the app updates the deal in Telegram too", async () => {
  const t = await setup({ settings: members });
  const key = await addDeal(t.store);
  const r = await app(t, "marco", "action", { action: "claim", key });
  assert.equal(r.status, 200);
  assert.equal(r.body.notice, "It's yours, good luck!");
  assert.equal(r.body.state.deals[0].status, "claimed");
  assert.equal(r.body.state.deals[0].mine, true);
  assert.match(t.tg.sent("editMessageText")[0].text, /✋ Claimed by Marco/);   // the chat message changed
  assert.equal(t.tg.sent("answerCallbackQuery").length, 0);                    // no Telegram pop-up to answer
  const again = await app(t, "luca", "action", { action: "claim", key });
  assert.equal(again.body.notice, "Marco already has this one");
});

test("buying in the app: a request, the manager approves in the app, the pot and the Summary post", async () => {
  const t = await setup({ settings: members });
  await t.update(msg("/deposit Owner 300"));
  const key = await addDeal(t.store, { cost: 60 });
  await app(t, "marco", "action", { action: "claim", key });
  let r = await app(t, "luca", "action", { action: "bought", key, amount: "60" });
  assert.equal(r.status, 400);
  assert.match(r.body.error, /Marco has this one/);
  t.tg.clear();
  r = await app(t, "marco", "action", { action: "bought", key, amount: "61,50" });
  assert.equal(r.status, 200);
  assert.match(r.body.notice, /Sent for approval/);
  assert.deepEqual(r.body.state.deals[0].request, { amount: 61.5, by: "Marco" });
  assert.equal((await app(t, "marco", "action", { action: "bought", key, amount: "61" })).status, 400);   // already waiting
  assert.equal((await app(t, "luca", "action", { action: "approve", key })).status, 400);              // not a manager
  r = await app(t, "owner", "action", { action: "approve", key });
  assert.equal(r.status, 200);
  assert.match(r.body.notice, /Approved #1 at €61\.50/);
  assert.equal(r.body.state.stock[0].paid, 61.5);
  assert.equal(r.body.state.pot.cash, 238.5);
  const posts = t.tg.sent().filter((p) => String(p.chat_id) === GROUP && p.message_thread_id === 44);
  assert.ok(posts.some((p) => p.text.includes("−€61.50 bought #1")));
  r = await app(t, "marco", "action", { action: "listed", key });
  r = await app(t, "marco", "action", { action: "sold", key, amount: "abc" });
  assert.equal(r.status, 400);
  r = await app(t, "marco", "action", { action: "sold", key, amount: "90" });
  assert.equal(r.body.state.pot.cash, 328.5);
  assert.equal(r.body.state.pot.profit, 28.5);
  assert.equal(r.body.state.pot.ledger[0].kind, "sale");                      // newest first
});

test("settings from the app: same checks as the commands", async () => {
  const t = await setup({ settings: members });
  let r = await app(t, "marco", "action", { action: "price", query: "boss katana", from: "300", to: "100" });
  assert.equal(r.status, 400);
  assert.match(r.body.error, /must be lower than the maximum/);
  r = await app(t, "marco", "action", { action: "price", query: "boss katana", from: "90", to: "260" });
  assert.equal(r.body.state.settings.searches.find((s) => s.query === "boss katana").price_to, 260);
  r = await app(t, "marco", "action", { action: "toggle", query: "iphone 13" });
  assert.equal(r.body.notice, "iphone 13: off");
  assert.equal(r.body.state.settings.searches.find((s) => s.query === "iphone 13").enabled, false);
  r = await app(t, "marco", "action", { action: "budget", value: "60" });
  assert.equal(r.body.state.settings.budget_setting, 60);
  r = await app(t, "marco", "action", { action: "rule", name: "min_roi", value: "25" });
  assert.equal(r.body.state.settings.rules.min_roi, 25);
  r = await app(t, "marco", "action", { action: "split", value: "equal" });
  assert.equal(r.status, 400);                                                   // owner only
  r = await app(t, "owner", "action", { action: "split", value: "equal" });
  assert.equal(r.body.state.pot.split, "equal");
  const s = await t.settings();
  assert.deepEqual([s.prices["boss katana"], s.disabled, s.budget, s.rules.min_roi], [[90, 260], ["iphone 13"], 60, 25]);
  assert.equal(t.tg.sent().filter((p) => String(p.chat_id) === String(MARCO)).length, 0);   // no chat noise
});

test("seller message and listing text come back to the app", async () => {
  const t = await setup({ settings: members });
  const key = await addDeal(t.store);
  let r = await app(t, "marco", "action", { action: "message", key });
  assert.match(r.body.notice, /Ciao! L'articolo "Boss DS-1 distortion"/);
  const d = await t.store.deal(key);
  Object.assign(d, { status: "bought", who: "Marco", who_id: MARCO, paid: 30 });
  await t.store.saveDeal(key, d);
  r = await app(t, "marco", "action", { action: "sell", key });
  assert.match(r.body.notice, /Listing for #1/);
  assert.ok(!r.body.notice.includes("<b>"));
});

test("the menu button points at the app once the Worker knows its address; /app opens it", async () => {
  const t = await setup({ settings: members });
  await t.cron();
  assert.equal(t.tg.sent("setChatMenuButton").length, 0);                       // address not known yet
  await t.update(msg("/app"));                                                   // the webhook tells it
  assert.equal(t.tg.sent()[0].reply_markup.inline_keyboard[0][0].web_app.url, "https://w/app/");
  t.tg.clear();
  await t.cron();
  const menu = t.tg.sent("setChatMenuButton")[0].menu_button;
  assert.deepEqual(menu, { type: "web_app", text: "📱 Open app", web_app: { url: "https://w/app/" } });
  t.tg.clear();
  await t.cron();
  assert.equal(t.tg.sent("setChatMenuButton").length, 0);                       // set once
  await t.update(msg("/app"));
  assert.deepEqual(t.tg.sent()[0].reply_markup.inline_keyboard[0][0], { text: "📱 Open app", web_app: { url: "https://w/app/" } });
  t.tg.clear();
  await t.update(msg("/app", { chat: Number(GROUP) }));
  const inGroup = t.tg.sent()[0];
  assert.match(inGroup.text, /opens from the private chat/);
  assert.ok(!inGroup.reply_markup?.inline_keyboard?.[0]?.[0]?.web_app);          // groups can't open Mini Apps
});
