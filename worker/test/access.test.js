// Roles are the only key: the bot, the app and the group are for people with a role.

import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, tap, addDeal, OWNER, MARCO, GROUP, DAYTIME } from "./helpers.js";
import { handleRequest } from "../src/app.js";
import { LOCKED } from "../src/bot.js";

const LUCA = 777;
const enc = new TextEncoder();
const team = { allowed_users: [MARCO, LUCA], roles: { [MARCO]: "buyer" }, people: { [MARCO]: "Marco", [LUCA]: "Luca" } };

async function hmac(key, data) {
  const k = await crypto.subtle.importKey("raw", typeof key === "string" ? enc.encode(key) : key,
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return crypto.subtle.sign("HMAC", k, enc.encode(data));
}

async function app(t, user, path = "state", body) {
  const fields = { auth_date: String(DAYTIME - 60), user: JSON.stringify(user) };
  const check = Object.keys(fields).sort().map((k) => `${k}=${fields[k]}`).join("\n");
  const hash = [...new Uint8Array(await hmac(await hmac("WebAppData", "TOKEN"), check))]
    .map((b) => b.toString(16).padStart(2, "0")).join("");
  const r = await handleRequest(new Request(`https://w/app/api/${path}`, { method: body ? "POST" : "GET",
    headers: { authorization: `tma ${new URLSearchParams({ ...fields, hash })}`, "content-type": "application/json" },
    body: body ? JSON.stringify(body) : undefined }), t.env, { fetchFn: t.tg.fetch, now: t.now });
  return { status: r.status, body: await r.json() };
}

/** A message in someone's private chat with the bot. */
function dm(text, user) {
  const m = msg(text, { user, chat: user });
  m.message.chat.type = "private";
  return m;
}

/** Telegram says whether someone is in the group (getChatMember). */
function membership(t, statuses, extra = {}) {
  t.tg.fail = (method, p) => {
    if (method === "getChatMember") return { ok: true, result: { status: statuses[p.user_id] || "left", user: { id: p.user_id, first_name: `U${p.user_id}` } } };
    return extra[method]?.(p) ?? null;
  };
}

test("no role: one short answer in a private chat, silence in the group, buttons do nothing", async () => {
  const t = await setup({ settings: team });
  const key = await addDeal(t.store);
  const inGroup = msg("/stock", { user: LUCA, chat: Number(GROUP) });
  inGroup.message.chat.type = "supergroup";
  await t.updates(dm("/stock", LUCA), inGroup, dm("hello", 4242), tap(`c:${key}`, { user: LUCA, chat: Number(GROUP) }));
  assert.deepEqual(t.tg.texts(), [LOCKED, LOCKED]);                       // Luca's DM and a stranger's DM
  assert.equal(t.tg.sent("answerCallbackQuery").length, 0);                // group button: nothing at all
  assert.equal((await t.store.deal(key)).status, "new");                   // and the claim didn't happen
  const privateTap = tap(`c:${key}`, { user: LUCA, chat: LUCA });
  privateTap.callback_query.message.chat.type = "private";
  await t.update(privateTap);
  assert.deepEqual(t.tg.sent("answerCallbackQuery").map((p) => p.text), [LOCKED]);
  t.tg.clear();
  await t.update(dm("/stock", MARCO));                                     // a role: works as before
  assert.match(t.tg.texts()[0], /Nothing in stock/);
});

test("the owner always has a role and can never lock themselves out", async () => {
  const t = await setup({ settings: { allowed_users: [], roles: {} } });
  await t.updates(dm("/stock", OWNER), msg("/setrole Owner none"), msg(`/removerole ${OWNER}`));
  const r = t.tg.texts();
  assert.match(r[0], /Nothing in stock/);
  assert.match(r[1], /you always keep a role, so you can't lock yourself out/);
  assert.match(r[2], /you always keep a role/);
  assert.equal((await app(t, { id: OWNER, first_name: "Owner" })).status, 200);
});

test("the app: no role gets a locked answer with no data, on every call; a role taken away closes it at once", async () => {
  const t = await setup({ settings: team });
  await addDeal(t.store);
  const luca = await app(t, { id: LUCA, first_name: "Luca" });
  assert.equal(luca.status, 403);
  assert.deepEqual(luca.body, { error: "🔒 Members only", locked: true });  // no deals, pot, schedule or handbook
  assert.equal((await app(t, { id: LUCA, first_name: "Luca" }, "action", { action: "claim", key: "vinted:1" })).status, 403);
  const marco = await app(t, { id: MARCO, first_name: "Marco" });
  assert.equal(marco.status, 200);
  assert.ok(marco.body.deals.length && marco.body.handbook);
  await t.update(msg("/removerole Marco"));
  const after = await app(t, { id: MARCO, first_name: "Marco" });
  assert.equal(after.status, 403);
  assert.equal(after.body.deals, undefined);
  t.tg.clear();
  await t.update(dm("/pot", MARCO));                                       // the bot too, right away
  assert.deepEqual(t.tg.texts(), [LOCKED]);
});

test("/setrole <name> none and /removerole: blocked now; the owner is asked about the group, ban + unban on yes", async () => {
  const t = await setup({ settings: team });
  membership(t, { [MARCO]: "member" });
  await t.update(msg("/setrole Marco none"));
  assert.match(t.tg.texts()[0], /🔒 Marco no longer has a role/);
  const ask = t.tg.sent().find((p) => p.text.startsWith("Remove <b>Marco</b> from the group too?"));
  assert.equal(ask.chat_id, OWNER);                                        // asked in private, not in the group
  assert.deepEqual(ask.reply_markup.inline_keyboard[0].map((b) => b.callback_data), [`kick:${MARCO}:y`, `kick:${MARCO}:n`]);
  t.tg.clear();
  await t.update(tap(`kick:${MARCO}:y`, { user: MARCO }));                 // not the owner (and no role now)
  assert.equal(t.tg.sent("banChatMember").length, 0);
  await t.update(tap(`kick:${MARCO}:y`));
  assert.deepEqual(t.tg.sent("banChatMember"), [{ chat_id: GROUP, user_id: MARCO, revoke_messages: false }]);
  assert.deepEqual(t.tg.sent("unbanChatMember"), [{ chat_id: GROUP, user_id: MARCO, only_if_banned: true }]);
  assert.match(t.tg.sent("editMessageText")[0].text, /Marco is out of the group\. They can ask to join again/);
  // not in the group: no question; no role to remove: an error
  t.tg.clear();
  membership(t, {});
  await t.update(msg("/setrole Marco buyer"));
  await t.update(msg("/removerole Marco"));
  assert.equal(t.tg.texts().filter((x) => x.startsWith("Remove")).length, 0);
  await t.update(msg("/removerole Marco"));
  assert.match(t.tg.texts().at(-1), /Marco has no role/);
});

test("'No, keep' keeps them; a role given back before tapping Yes isn't removed", async () => {
  const t = await setup({ settings: team });
  membership(t, { [MARCO]: "member" });
  await t.update(msg("/removerole Marco"));
  await t.update(tap(`kick:${MARCO}:n`));
  assert.match(t.tg.sent("editMessageText")[0].text, /Marco stays in the group/);
  await t.update(msg("/setrole Marco seller"));
  await t.update(tap(`kick:${MARCO}:y`));
  assert.match(t.tg.sent("editMessageText")[1].text, /has a role again, so I didn't remove them/);
  assert.equal(t.tg.sent("banChatMember").length, 0);
});

test("join requests: approved with a role, declined without (and the owner is told who asked)", async () => {
  const t = await setup({ settings: team });
  const request = (id, first, chat = Number(GROUP)) => ({ update_id: id, chat_join_request: { chat: { id: chat, type: "supergroup" },
    from: { id, first_name: first }, user_chat_id: id, date: DAYTIME } });
  await t.updates(request(MARCO, "Marco"), request(LUCA, "Luca"), request(4242, "Eve <script>"), request(5, "X", -999));
  assert.deepEqual(t.tg.sent("approveChatJoinRequest"), [{ chat_id: GROUP, user_id: MARCO }]);
  assert.deepEqual(t.tg.sent("declineChatJoinRequest").map((p) => p.user_id), [LUCA, 4242]);   // other chats: ignored
  const told = t.tg.sent().filter((p) => p.chat_id === OWNER);
  assert.match(told[0].text, /^❗ Luca \(<code>777<\/code>\) asked to join, no role/);
  assert.match(told[1].text, /^❗ Eve &lt;script&gt; \(<code>4242<\/code>\) asked to join, no role/);
  assert.match(told[1].text, /\/allow 4242, then \/setrole 4242/);
});

test("the daily member check lists people in the group without a role, once a day, and removes nobody", async () => {
  const t = await setup({ settings: team });
  // the bot saw Luca and a stranger write in the group
  for (const [id, name] of [[LUCA, "Luca"], [4242, "Eve"]]) {
    const m = msg("hi", { user: id, chat: Number(GROUP), first: name });
    m.message.chat.type = "supergroup";
    await t.update(m);
  }
  assert.deepEqual(Object.keys(await t.store.get("group_seen")).map(Number).sort((a, b) => a - b), [LUCA, 4242]);
  membership(t, { [MARCO]: "member", [LUCA]: "member", [4242]: "member" }, {
    getChatMemberCount: () => ({ ok: true, result: 6 }),
    getChatAdministrators: () => ({ ok: true, result: [{ status: "creator", user: { id: OWNER, first_name: "Owner" } },
      { status: "administrator", user: { id: 99, first_name: "flipFinder", is_bot: true } }] }),
  });
  const nine = Date.UTC(2026, 9, 7, 7, 0) / 1000;                          // 09:00 in Italy: not yet
  await t.cron(nine);
  assert.equal(t.tg.sent().filter((p) => p.text?.startsWith("👥")).length, 0);
  await t.cron(DAYTIME);
  const report = t.tg.sent().filter((p) => p.text?.startsWith("👥"));
  assert.equal(report.length, 1);
  assert.equal(report[0].chat_id, OWNER);
  assert.ok(report[0].text.includes("U777 (<code>777</code>)") && report[0].text.includes("U4242 (<code>4242</code>)"));
  assert.ok(!report[0].text.includes("<code>555</code>"));                 // Marco has a role
  assert.match(report[0].text, /1 more I can't identify/);                 // 6 members, 5 known
  assert.equal(t.tg.sent("banChatMember").length, 0);
  await t.cron(DAYTIME + 3600);
  assert.equal(t.tg.sent().filter((p) => p.text?.startsWith("👥")).length, 1);   // once a day
});

test("joins and leaves are tracked; the webhook is told to send join requests and member changes, once", async () => {
  const t = await setup({ settings: team });
  await t.update({ update_id: 1, chat_member: { chat: { id: Number(GROUP) }, from: { id: OWNER },
    old_chat_member: { status: "left", user: { id: 31, first_name: "Ann" } }, new_chat_member: { status: "member", user: { id: 31, first_name: "Ann" } } } });
  assert.deepEqual(await t.store.get("group_seen"), { 31: "Ann" });
  await t.update({ update_id: 2, chat_member: { chat: { id: Number(GROUP) }, from: { id: OWNER },
    old_chat_member: { status: "member", user: { id: 31 } }, new_chat_member: { status: "left", user: { id: 31, first_name: "Ann" } } } });
  assert.deepEqual(await t.store.get("group_seen"), {});
  await t.update(msg("/app"));                                             // the Worker learns its address
  await t.cron();
  await t.cron();
  const hooks = t.tg.sent("setWebhook");
  assert.equal(hooks.length, 1);
  assert.deepEqual(hooks[0], { url: "https://w/telegram", secret_token: "hook-secret",
    allowed_updates: ["message", "callback_query", "chat_join_request", "chat_member"] });
});

test("two requests at once don't undo each other's settings (a /setrole during the 5-min job is kept)", async () => {
  const t = await setup({ settings: { allowed_users: [MARCO, LUCA], roles: {} } });
  const { open } = await import("../src/app.js");
  // the 5-min job reads settings, then a /setrole comes in and is saved, then the job saves its own change
  const job = await open(t.env, { fetchFn: t.tg.fetch, now: t.now });
  await t.update(msg(`/setrole ${LUCA} seller`));
  job.bot.settings.commands_version = 99;
  job.bot.changed = true;
  await job.bot.save();
  const s = await t.settings();
  assert.equal(s.roles[LUCA], "seller");                   // the role survived
  assert.equal(s.commands_version, 99);                    // and so did the job's change
});
