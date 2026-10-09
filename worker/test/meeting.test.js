// The weekly team meeting: reminders, the agenda, ✅/❌ answers, the poll, /meeting settings, /minutes.

import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, tap, addDeal, OWNER, MARCO, GROUP } from "./helpers.js";
import { parseDay } from "../src/meeting.js";

const LUCA = 777;
const team = { allowed_users: [MARCO, LUCA], roles: { [OWNER]: "manager", [MARCO]: "buyer", [LUCA]: "seller" },
  people: { [OWNER]: "Boss", [MARCO]: "Marco", [LUCA]: "Luca" }, topics: { summary: 44 } };
const SAT = Date.UTC(2026, 9, 10, 18, 31) / 1000;      // Saturday 10 Oct, 20:31 in Italy
const SUN_SOON = Date.UTC(2026, 9, 11, 18, 16) / 1000; // Sunday 11 Oct, 20:16
const inGroup = (t) => t.tg.sent().filter((p) => String(p.chat_id) === GROUP && p.text?.startsWith("👥"));

test("days in English or Italian", () => {
  assert.deepEqual(["sun", "Sunday", "domenica", "LUN", "mercoledì", "xyz"].map(parseDay), ["Sun", "Sun", "Sun", "Mon", "Wed", null]);
});

test("Sunday 20:30 by default: a reminder the day before and one 15 minutes before with the agenda, each once", async () => {
  const t = await setup({ settings: team });
  await addDeal(t.store, { status: "new" });
  await t.cron(SAT - 3600);
  assert.equal(inGroup(t).length, 0);
  await t.cron(SAT);
  await t.cron(SAT + 300);
  const [before] = inGroup(t);
  assert.equal(inGroup(t).length, 1);
  assert.equal(before.message_thread_id, undefined);                                  // the group's main chat
  assert.match(before.text, /^👥 <b>Team meeting tomorrow, Sun 11 Oct, 20:30<\/b>\n📹 Video chat in the group, tap the call icon at the top\n/);
  assert.ok(before.text.includes('tg://user?id=555">Marco</a>') && before.text.includes('tg://user?id=777">Luca</a>'));
  assert.deepEqual(before.reply_markup.inline_keyboard[0].map((b) => b.callback_data), ["mt:in:2026-10-11", "mt:out:2026-10-11"]);
  t.tg.clear();
  await t.cron(SUN_SOON);
  await t.cron(SUN_SOON + 300);
  const [soon] = inGroup(t);
  assert.equal(inGroup(t).length, 1);
  assert.match(soon.text, /^👥 <b>Team meeting in 15 minutes \(20:30\)<\/b>\n📹 .*\n📋 <b>Agenda<\/b>\n• This week: \d+ deals \(✅ \d+ · ❌ \d+\), bought 0, sold 0, profit €0\.00\n• Stock: nothing in stock\n• Open suggestions: none\n• AI got wrong \(↩️ Not a NO\): none\n• Duty hours: Boss 0 h, Marco 0 h, Luca 0 h\n/);
  assert.ok(soon.text.split("\n").length <= 11);
});

test("✅/❌ answers show on the reminder; 2 who can't bring one poll with 3 other times", async () => {
  const t = await setup({ settings: team });
  await t.cron(SAT);
  await t.updates(tap("mt:in:2026-10-11", { user: MARCO, chat: -100 }), tap("mt:out:2026-10-11", { user: LUCA, chat: -100 }));
  assert.match(t.tg.sent("editMessageText").at(-1).text, /\n✅ Marco · ❌ Luca\n/);
  assert.equal(t.tg.sent("sendPoll").length, 0);
  await t.updates(tap("mt:out:2026-10-11", { chat: -100 }), tap("mt:out:2026-10-11", { chat: -100 }));
  const polls = t.tg.sent("sendPoll");
  assert.equal(polls.length, 1);
  assert.equal(polls[0].question, "2 can't make it: another time for the meeting?");
  assert.deepEqual(polls[0].options.map((o) => o.text), ["Mon 12 Oct, 20:30", "Tue 13 Oct, 20:30", "Wed 14 Oct, 20:30", "Keep the usual time"]);
  const answers = t.tg.sent("answerCallbackQuery").length;
  await t.update(tap("mt:in:2026-10-11", { user: 4242, chat: -100 }));               // a stranger: ignored
  assert.equal(t.tg.sent("answerCallbackQuery").length, answers);
});

test("/meeting: time, an external link (Join button), back to Telegram, off; only the owner or a manager", async () => {
  const t = await setup({ settings: team });
  await t.updates(msg("/meeting"), msg("/meeting time mon 19:00", { user: MARCO }), msg("/meeting time lunedi 19:00"),
    msg("/meeting link https://meet.google.com/abc-defg-hij"));
  const r = t.tg.texts();
  assert.match(r[0], /every Sun at 20:30, next Sun 11 Oct, 20:30\n📹 Video chat/);
  assert.match(r[1], /Only the owner or a manager/);
  assert.match(r[2], /every Mon at 19:00, next Mon 12 Oct, 19:00/);
  assert.match(r[3], /Join button/);
  await t.cron(Date.UTC(2026, 9, 11, 17, 5) / 1000);   // Sunday 19:05: the day before Monday 19:00
  const [rem] = inGroup(t);
  assert.match(rem.text, /🔗 Join here: https:\/\/meet\.google\.com\/abc-defg-hij/);
  assert.deepEqual(rem.reply_markup.inline_keyboard[1][0], { text: "Join", url: "https://meet.google.com/abc-defg-hij" });
  await t.updates(msg("/meeting link off"), msg("/meeting off"));
  assert.match(t.tg.texts().at(-2), /Back to the Telegram video chat/);
  t.tg.clear();
  await t.cron(Date.UTC(2026, 9, 12, 16, 46) / 1000);  // Monday 18:46: would be the 15-minute one
  assert.equal(inGroup(t).length, 0);
});

test("no team (no roles yet): no meeting reminders", async () => {
  const t = await setup();
  await t.cron(SAT);
  assert.equal(inGroup(t).length, 0);
});

test("/minutes: posted in Summary and in the next weekly report", async () => {
  const t = await setup({ settings: team });
  await t.update(msg("/minutes Marco lists the two amps by Wednesday", { user: MARCO }), SUN_SOON + 3600);
  const posts = t.tg.sent().filter((p) => p.text?.startsWith("📝 <b>Meeting notes"));
  assert.deepEqual(posts.map((p) => [String(p.chat_id), p.message_thread_id]), [[GROUP, 44]]);   // only in Summary
  const [post] = posts;
  assert.match(post.text, /📝 <b>Meeting notes, Sun 11 Oct<\/b> \(Marco\)\nMarco lists the two amps by Wednesday/);
  const { open } = await import("../src/app.js");
  const { bot } = await open(t.env, { fetchFn: t.tg.fetch, now: SUN_SOON + 7200 });
  const r = await bot.reports.data("week", "2026-10-05", "2026-10-11");
  assert.equal(r.notes[0].text, "Marco lists the two amps by Wednesday");
  assert.ok(JSON.stringify(bot.reports.sheets(r, { note: "", items: [] })[0].rows).includes("Meeting notes"));
  await t.update(msg("/minutes"));
  assert.match(t.tg.texts().at(-1), /Write the notes after the command/);
});
