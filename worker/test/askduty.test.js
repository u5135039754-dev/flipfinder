import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, tap, OWNER, MARCO, GROUP } from "./helpers.js";
import { handleRequest } from "../src/app.js";
import { romeTs } from "../src/util.js";

const LUCA = 777;
const DAY = "2026-10-07";                  // a Wednesday
const at = (h, m = 0) => romeTs(DAY, h, m);
const team = {
  allowed_users: [MARCO, LUCA],
  roles: { [OWNER]: "manager", [MARCO]: "buyer", [LUCA]: "seller" },
  people: { [OWNER]: "Boss", [MARCO]: "Marco", [LUCA]: "Luca" },
  topics: { summary: 44 },
};
const inGroup = (t) => t.tg.sent().filter((p) => String(p.chat_id) === GROUP).map((p) => p.text);
const dmTo = (t, id) => t.tg.sent().filter((p) => String(p.chat_id) === String(id)).map((p) => p);
const said = (t) => t.tg.sent("answerCallbackQuery").at(-1).text;
const send = (t, text, user, when) => t.update(msg(text, { user, chat: user }), when);
const tapAt = (t, data, user, when, chat = user) => t.update(tap(data, { user, chat }), when);

test("/askduty: a private message, ✅ Accept moves the schedule and the shift, the group and the asker are told", async () => {
  const t = await setup({ settings: team });
  await t.store.put("schedule", { [DAY]: { 14: MARCO, 15: MARCO, 16: MARCO, 17: MARCO } });
  await tapAt(t, "duty:on", MARCO, at(14), Number(GROUP));
  t.tg.clear();
  await send(t, "/askduty Luca 15:00-17:00 dentist at 15", MARCO, at(14, 30));
  const ask = dmTo(t, LUCA)[0];
  assert.equal(ask.text, "🙋 <b>Marco asks you to take duty 15:00–17:00</b>\n💬 dentist at 15");
  assert.ok(!ask.disable_notification);                                   // with sound
  assert.deepEqual(ask.reply_markup.inline_keyboard[0].map((b) => b.callback_data), ["dya:1", "dyn:1"]);
  assert.match(dmTo(t, MARCO).at(-1).text, /Asked Luca for 15:00–17:00/);
  await send(t, "/askduty Luca 16-18", MARCO, at(14, 31));                // one open request per person
  assert.match(dmTo(t, MARCO).at(-1).text, /already asked Luca/);
  await send(t, "/askduty Marco 16-18", MARCO, at(14, 31));
  assert.match(dmTo(t, MARCO).at(-1).text, /That's you/);
  await tapAt(t, "dya:1", OWNER, at(14, 40));
  assert.match(said(t), /for someone else/);
  t.tg.clear();
  await tapAt(t, "dya:1", LUCA, at(15));
  assert.match(said(t), /You're on duty 15:00–17:00/);
  assert.ok(inGroup(t).some((x) => x.includes("Luca</a> takes duty 15:00–17:00 (asked by Marco)")));
  assert.match(dmTo(t, MARCO).at(-1).text, /Luca accepted: on duty 15:00–17:00/);
  assert.equal(t.tg.sent("editMessageReplyMarkup")[0].reply_markup.inline_keyboard[0][0].text, "✅ Accepted");
  const sched = (await t.store.get("schedule"))[DAY];
  assert.deepEqual([sched[14], sched[15], sched[16], sched[17]], [MARCO, LUCA, LUCA, MARCO]);
  const duty = await t.store.get("duty");
  assert.equal(duty.on.id, LUCA);                                         // it has started: the shift moves now
  assert.deepEqual(duty.log.map((e) => e.id), [MARCO]);
  await tapAt(t, "dyn:1", LUCA, at(15, 1));
  assert.match(said(t), /closed/);
});

test("❌ Can't: the asker is told and can ask the other member", async () => {
  const t = await setup({ settings: team });
  await send(t, "/askduty luca 18-20", MARCO, at(14));
  t.tg.clear();
  await tapAt(t, "dyn:1", LUCA, at(14, 5));
  const told = dmTo(t, MARCO).at(-1);
  assert.match(told.text, /Luca can't take duty 18:00–20:00\. Ask someone else\?/);
  assert.deepEqual(told.reply_markup.inline_keyboard.flat().map((b) => b.callback_data), [`dyo:1:${OWNER}`]);
  t.tg.clear();
  await tapAt(t, `dyo:1:${OWNER}`, MARCO, at(14, 6));
  assert.match(dmTo(t, OWNER)[0].text, /Marco asks you to take duty 18:00–20:00/);
  await tapAt(t, `dyo:1:${OWNER}`, MARCO, at(14, 7));
  assert.match(said(t), /already asked someone else/);
});

test("no answer: one reminder after 15 minutes, then the asker is told", async () => {
  const t = await setup({ settings: team });
  await send(t, "/askduty Luca 18-20", MARCO, at(14));
  t.tg.clear();
  await t.cron(at(14, 10));
  assert.equal(dmTo(t, LUCA).length, 0);
  await t.cron(at(14, 15));
  assert.match(dmTo(t, LUCA)[0].text, /^⏰ Reminder: Marco asks you to take duty 18:00–20:00/);
  await t.cron(at(14, 20));
  assert.equal(dmTo(t, LUCA).length, 1);                                  // once
  await t.cron(at(14, 30));
  assert.match(dmTo(t, MARCO).at(-1).text, /Luca didn't answer about duty 18:00–20:00/);
  await tapAt(t, "dya:1", LUCA, at(14, 31));
  assert.match(said(t), /closed/);
});

test("the pinned message's 🙋 Ask someone: pick who, then how long", async () => {
  const t = await setup({ settings: team });
  await tapAt(t, "duty:ask", MARCO, at(10), Number(GROUP));
  const pick = t.tg.sent().find((p) => p.text?.includes("who should take duty"));
  assert.deepEqual(pick.reply_markup.inline_keyboard.flat().map((b) => b.callback_data), [`dyp:${OWNER}:${MARCO}`, `dyp:${LUCA}:${MARCO}`]);
  await tapAt(t, `dyp:${LUCA}:${MARCO}`, LUCA, at(10, 1), Number(GROUP));
  assert.match(said(t), /someone else's question/);
  await tapAt(t, `dyp:${LUCA}:${MARCO}`, MARCO, at(10, 1), Number(GROUP));
  const times = t.tg.sent("editMessageText").at(-1);
  assert.ok(times.reply_markup.inline_keyboard.flat().some((b) => b.callback_data === `dyt:${LUCA}:${MARCO}:2`));
  t.tg.clear();
  await tapAt(t, `dyt:${LUCA}:${MARCO}:2`, MARCO, at(10, 2), Number(GROUP));
  assert.match(dmTo(t, LUCA)[0].text, /Marco asks you to take duty 10:02–12:02/);
});

test("a member with no role can't be asked; the time must be inside 08:00-22:00", async () => {
  const t = await setup({ settings: { ...team, roles: { [OWNER]: "manager", [MARCO]: "buyer" } } });
  await send(t, "/askduty Luca 15-17", MARCO, at(14));
  assert.match(dmTo(t, MARCO).at(-1).text, /I don't know "Luca" in the team/);
  await send(t, "/askduty Boss 22-23", MARCO, at(14));
  assert.match(dmTo(t, MARCO).at(-1).text, /outside duty hours/);
});

// --- the Mini App
const enc = new TextEncoder();
async function hmac(key, data) {
  const k = await crypto.subtle.importKey("raw", typeof key === "string" ? enc.encode(key) : key,
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return crypto.subtle.sign("HMAC", k, enc.encode(data));
}
async function app(t, user, body) {
  const fields = { auth_date: String(t.now - 60), query_id: "AAE1", user: JSON.stringify(user) };
  const check = Object.keys(fields).sort().map((k) => `${k}=${fields[k]}`).join("\n");
  const hash = [...new Uint8Array(await hmac(await hmac("WebAppData", "TOKEN"), check))].map((b) => b.toString(16).padStart(2, "0")).join("");
  const r = await handleRequest(new Request(`https://w/app/api/${body ? "action" : "state"}`, { method: body ? "POST" : "GET",
    headers: { authorization: `tma ${new URLSearchParams({ ...fields, hash })}`, "content-type": "application/json" },
    body: body ? JSON.stringify(body) : undefined }), t.env, { fetchFn: t.tg.fetch, now: t.now });
  return { status: r.status, body: await r.json() };
}
const marco = { id: MARCO, first_name: "Marco" };
const luca = { id: LUCA, first_name: "Luca" };

test("Mini App: ask with a note, incoming and outgoing lists, accept from the app, cancel", async () => {
  const t = await setup({ settings: team, now: at(14) });
  let r = await app(t, marco, { action: "ask_duty", to_id: LUCA, from: "16:00", to: "18:00", note: "gym" });
  assert.equal(r.status, 200);
  assert.match(dmTo(t, LUCA)[0].text, /Marco asks you to take duty 16:00–18:00<\/b>\n💬 gym/);
  assert.deepEqual(r.body.state.schedule.asks.map((a) => [a.from, a.to, a.note]), [[MARCO, LUCA, "gym"]]);
  assert.equal((await app(t, marco, { action: "ask_duty", to_id: LUCA })).status, 400);       // already open
  assert.equal((await app(t, marco, { action: "ask_duty", to_id: MARCO })).status, 400);      // yourself
  assert.equal((await app(t, luca, { action: "ask_cancel", n: 1 })).status, 400);             // not Luca's to cancel
  r = await app(t, luca);
  assert.equal(r.body.schedule.asks[0].from_name, "Marco");                                   // Luca's incoming card
  t.tg.clear();
  r = await app(t, luca, { action: "ask_accept", n: 1 });
  assert.equal(r.status, 200);
  assert.deepEqual(r.body.state.schedule.asks, []);
  assert.ok(inGroup(t).some((x) => x.includes("takes duty 16:00–18:00")));
  assert.equal(t.tg.sent("editMessageReplyMarkup")[0].chat_id, String(LUCA));               // the bot message updates too
  assert.equal((await t.store.get("schedule"))[DAY][16], LUCA);
  // cancel: the other person's message says so
  await app(t, marco, { action: "ask_duty", to_id: OWNER });
  t.tg.clear();
  r = await app(t, marco, { action: "ask_cancel", n: 2 });
  assert.equal(r.status, 200);
  assert.equal(t.tg.sent("editMessageReplyMarkup")[0].reply_markup.inline_keyboard[0][0].text, "↩️ Cancelled");
  assert.equal((await app(t, { id: OWNER, first_name: "Boss" }, { action: "ask_accept", n: 2 })).status, 400);
});

test("someone who never opened the bot in private: asked in the group, the asker is told why", async () => {
  const t = await setup({ settings: { ...team, bot_username: "flip_bot" } });
  t.tg.fail = (method, p) => (method === "sendMessage" && String(p.chat_id) === String(LUCA)
    ? { ok: false, error_code: 403, description: "Forbidden: bot can't initiate conversation with a user" } : null);
  await send(t, "/askduty Luca 18-20", MARCO, at(14));
  const post = t.tg.sent().find((p) => String(p.chat_id) === GROUP && p.text?.includes("asks you to take duty"));
  assert.match(post.text, /Luca<\/a>, Marco asks you to take duty 18:00–20:00\n📩 Open a private chat with @flip_bot/);
  assert.deepEqual(post.reply_markup.inline_keyboard[0].map((b) => b.callback_data), ["dya:1", "dyn:1"]);
  assert.match(dmTo(t, MARCO).at(-1).text, /Asked Luca for 18:00–20:00 in the group \(Luca hasn't opened a private chat/);
  await tapAt(t, "dya:1", LUCA, at(14, 5), Number(GROUP));               // accepting in the group works the same
  assert.match(said(t), /You're on duty 18:00–20:00/);
});
