// The Mini App's Searches section: per topic, add / edit / delete (owner and managers), topic rules,
// suggestions to the owner, previews run by the scanner, a Summary line for every change.

import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, tap, OWNER, MARCO, GROUP, DAYTIME, addDeal } from "./helpers.js";
import { handleRequest } from "../src/app.js";
import { applySettings } from "../src/searches.js";
import { CATALOG } from "./helpers.js";

const LUCA = 777;
const team = { allowed_users: [MARCO, LUCA], roles: { [OWNER]: "manager", [MARCO]: "manager", [LUCA]: "seller" },
  people: { [OWNER]: "Boss", [MARCO]: "Marco", [LUCA]: "Luca" }, topics: { summary: 44 } };
const enc = new TextEncoder();
async function hmac(key, data) {
  const k = await crypto.subtle.importKey("raw", typeof key === "string" ? enc.encode(key) : key,
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return crypto.subtle.sign("HMAC", k, enc.encode(data));
}
const people = { owner: { id: OWNER, first_name: "Boss" }, marco: { id: MARCO, first_name: "Marco" }, luca: { id: LUCA, first_name: "Luca" } };
async function call(t, who, path, body, method = body ? "POST" : "GET") {
  const fields = { auth_date: String(t.now - 60), query_id: "AAE1", user: JSON.stringify(people[who]) };
  const check = Object.keys(fields).sort().map((k) => `${k}=${fields[k]}`).join("\n");
  const hash = [...new Uint8Array(await hmac(await hmac("WebAppData", "TOKEN"), check))].map((b) => b.toString(16).padStart(2, "0")).join("");
  const r = await handleRequest(new Request(`https://w/app/api/${path}`, { method,
    headers: { authorization: `tma ${new URLSearchParams({ ...fields, hash })}`, "content-type": "application/json" },
    body: body ? JSON.stringify(body) : undefined }), t.env, { fetchFn: t.tg.fetch, now: t.now });
  return { status: r.status, body: await r.json() };
}
const act = (t, who, body) => call(t, who, "action", body);
const summary = (t) => t.tg.sent().filter((p) => String(p.chat_id) === GROUP && p.message_thread_id === 44).map((p) => p.text);
const spec = (extra = {}) => ({ query: "Boss Katana Mini", topic: "guitars", price_from: "30", price_to: "", platforms: ["ebay"],
  must: "mini, katana", exclude: "cover", ...extra });

test("the Searches section: topics in order with their rules, searches and deals found this week", async () => {
  const t = await setup({ settings: team });
  await addDeal(t.store, { query: "boss katana", sent: DAYTIME - 3600 });
  await t.store.addDeal("vinted:2", { title: "old", text: "x", query: "boss katana", sent: DAYTIME - 9 * 86400 });   // older than a week
  const s = (await call(t, "luca", "state")).body.searches;
  assert.equal(s.can_edit, false);
  assert.deepEqual(s.topics.map((x) => [x.key, x.name, x.icon]), [["guitars", "Music", "🎸"], ["electronics", "Electronics", "📱"],
    ["budget", "Budget", "💸"], ["clothes", "Clothes", "👕"], ["repairs", "Repairs", "🔧"]]);
  const music = s.topics[0];
  assert.deepEqual(music.rules, { min_profit: 20, min_roi: 35, max_price: 200, note: "Music gear's own rules" });
  assert.deepEqual(music.searches.find((x) => x.query === "boss katana"), { query: "boss katana", price_from: 80, price_to: 300,
    enabled: true, budget: false, clothes: false, added: false, week: 1, platforms: ["vinted", "ebay", "subito"], must: [], exclude: [], size: "" });
  assert.deepEqual(s.topics[4].searches, []);
  assert.equal(s.topics[4].repairs, true);
  assert.equal((await call(t, "marco", "state")).body.searches.can_edit, true);
});

test("➕ add (owner/manager): into the topic's group, with its extras; a Summary line; /remove sees it too", async () => {
  const t = await setup({ settings: team });
  assert.equal((await act(t, "luca", { action: "search_save", spec: spec() })).status, 400);   // not a manager
  const r = await act(t, "marco", { action: "search_save", spec: spec() });
  assert.equal(r.body.notice, "Added 'boss katana mini' to Music: checked from the next scan");
  const s = await t.settings();
  assert.deepEqual(s.added.at(-1), { query: "boss katana mini", price_from: 30, price_to: null, group: "Guitars",
    platforms: ["ebay"], must: ["mini", "katana"], exclude: ["cover"], size: "" });
  assert.deepEqual(summary(t), ["➕ Marco added 'boss katana mini' to Music (€30–no max)"]);
  const view = applySettings(CATALOG, s);
  assert.equal(view.searches.find((x) => x.query === "boss katana mini").group, "Guitars");
  assert.equal((await act(t, "marco", { action: "search_save", spec: spec() })).status, 400);   // already there
  // a clothes search: the clothes flag and its size; the price range is optional
  await act(t, "owner", { action: "search_save", spec: { query: "moncler", topic: "clothes", size: "M" } });
  assert.deepEqual((await t.settings()).added.at(-1), { query: "moncler", price_from: null, price_to: null, group: "Clothes",
    clothes: true, platforms: [], must: [], exclude: [], size: "M" });
  // /remove works on it like on any other
  await t.updates(msg("/remove boss katana mini"));
  assert.match(t.tg.texts().at(-1), /Remove <b>boss katana mini<\/b>\?/);
});

test("✏️ edit: a config.yaml search keeps its place (prices + extras over it); renaming an added one moves it", async () => {
  const t = await setup({ settings: team });
  await act(t, "owner", { action: "search_save", old: "boss katana", spec: spec({ query: "boss katana", price_from: 90, price_to: 250 }) });
  let s = await t.settings();
  assert.deepEqual(s.prices["boss katana"], [90, 250]);
  assert.deepEqual(s.overrides["boss katana"], { platforms: ["ebay"], must: ["mini", "katana"], exclude: ["cover"], size: "" });
  assert.match(summary(t).at(-1), /✏️ Boss changed 'boss katana' in Music \(€90–€250\)/);
  await act(t, "owner", { action: "search_save", spec: spec() });
  await act(t, "owner", { action: "search_save", old: "boss katana mini", spec: spec({ query: "katana mini", topic: "electronics" }) });
  s = await t.settings();
  assert.ok(!s.added.some((a) => a.query === "boss katana mini"));
  assert.equal(s.added.find((a) => a.query === "katana mini").group, "Electronics");
  assert.match(summary(t).at(-1), /changed 'katana mini' \(was 'boss katana mini'\) in Electronics/);
});

test("🗑 delete and on/off: owner/manager only, each with a Summary line", async () => {
  const t = await setup({ settings: team });
  assert.equal((await act(t, "luca", { action: "search_delete", query: "iphone 13" })).status, 400);
  assert.equal((await act(t, "marco", { action: "toggle", query: "iphone 13" })).body.notice, "iphone 13: off");
  assert.equal((await act(t, "marco", { action: "search_delete", query: "iphone 13" })).body.notice, "Removed 'iphone 13'");
  const s = await t.settings();
  assert.ok(s.removed.includes("iphone 13") && !s.disabled.includes("iphone 13"));
  assert.deepEqual(summary(t), ["⏸ Marco turned 'iphone 13' off in Electronics", "🗑 Marco removed 'iphone 13' from Electronics"]);
});

test("topic rules: each card's min profit, min ROI and max price map onto the rules the scanner uses", async () => {
  const t = await setup({ settings: team });
  for (const [topic, name, value] of [["guitars", "max_price", 250], ["guitars", "min_profit", 22], ["electronics", "min_roi", 28],
    ["budget", "max_price", 60], ["budget", "min_roi", 30], ["clothes", "max_price", 180]]) {
    const r = await act(t, "owner", { action: "topic_rule", topic, name, value: String(value) });
    assert.equal(r.status, 200, JSON.stringify(r.body));
  }
  const s = await t.settings();
  assert.deepEqual([s.music_rules, s.rules.min_roi, s.budget, s.budget_rules, s.clothes_rules],
    [{ max_price: 250, min_profit: 22 }, 28, 60, { min_roi: 30 }, { max_price: 180 }]);
  assert.equal((await act(t, "owner", { action: "topic_rule", topic: "repairs", name: "min_roi", value: "5" })).status, 400);
  assert.equal((await act(t, "luca", { action: "topic_rule", topic: "clothes", name: "min_roi", value: "50" })).status, 400);
  assert.match(summary(t).at(-1), /⚙️ Boss set Clothes max price to €180/);
});

test("💡 suggest (everyone else): to the owner with ✅ Add / ❌ Ignore; the answer goes back to who suggested it", async () => {
  const t = await setup({ settings: team });
  const r = await act(t, "luca", { action: "search_suggest", spec: spec({ topic: "electronics", query: "ipad air" }) });
  assert.equal(r.body.notice, "💡 Sent to the owner: 'ipad air'");
  const dm = t.tg.sent().find((p) => String(p.chat_id) === String(OWNER) && p.text.startsWith("💡"));
  assert.match(dm.text, /Luca suggests a search for 📱 Electronics: <b>ipad air<\/b>/);
  assert.deepEqual(dm.reply_markup.inline_keyboard[0].map((b) => b.callback_data), ["sgy:1", "sgn:1"]);
  await t.update(tap("sgy:1", { user: MARCO, chat: MARCO }));
  assert.match(t.tg.sent("answerCallbackQuery").at(-1).text, /Only the owner/);
  await t.update(tap("sgy:1", { user: OWNER, chat: OWNER }));
  assert.ok((await t.settings()).added.some((a) => a.query === "ipad air" && a.group === "Electronics"));
  assert.match(summary(t).at(-1), /➕ Boss added 'ipad air' to Electronics .*\(suggested by Luca\)/);
  assert.ok(t.tg.sent().some((p) => String(p.chat_id) === String(LUCA) && /Your search <b>ipad air<\/b> was added/.test(p.text)));
  await t.update(tap("sgy:1", { user: OWNER, chat: OWNER }));
  assert.match(t.tg.sent("answerCallbackQuery").at(-1).text, /Already answered/);
});

test("preview: the app queues it, the scanner's run picks it up and answers, the app reads it", async () => {
  const t = await setup({ settings: team });
  const { id } = (await call(t, "luca", "preview", spec())).body;
  assert.ok(id);
  assert.deepEqual((await call(t, "luca", `preview?id=${id}`)).body.status, "waiting");
  const state = await t.api("GET", "/api/state");
  assert.deepEqual(state.body.previews, [{ id, spec: { query: "boss katana mini", price_from: 30, price_to: null, group: "Guitars",
    platforms: ["ebay"], must: ["mini", "katana"], exclude: ["cover"], size: "" } }]);
  await t.api("POST", "/api/preview", { id, result: { listings: 40, valued: 22, pass: 3, best: [] } });
  const done = (await call(t, "luca", `preview?id=${id}`)).body;
  assert.deepEqual([done.status, done.result.listings, done.result.pass], ["done", 40, 3]);
  assert.equal((await t.api("GET", "/api/state")).body.previews.length, 0);
  assert.equal((await call(t, "marco", `preview?id=${id}`)).status, 400);   // someone else's
  // three waiting at most per person
  for (let i = 0; i < 3; i++) await call(t, "luca", "preview", spec({ query: `q${i} test` }));
  assert.equal((await call(t, "luca", "preview", spec({ query: "one more" }))).status, 400);
});
