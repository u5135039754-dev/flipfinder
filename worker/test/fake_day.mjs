// Dry run: a whole fake day for the team features (made-up people, fake Telegram). Not a test file:
//   node test/fake_day.mjs
import { setup, msg, tap, OWNER, MARCO, GROUP } from "./helpers.js";
import { romeTs, hhmm } from "../src/util.js";

const LUCA = 777;
const DAY = "2026-10-07";
const at = (h, m = 0) => romeTs(DAY, h, m);
const t = await setup({ settings: {
  allowed_users: [MARCO, LUCA], topics: { summary: 44, guitars: 11, electronics: 22 },
  roles: { [OWNER]: "manager", [MARCO]: "buyer", [LUCA]: "seller" },
  people: { [OWNER]: "Boss", [MARCO]: "Marco", [LUCA]: "Luca" } } });
await t.update(msg("/deposit Boss 100"), at(7));
await t.update(msg("/deposit Marco 100"), at(7));
await t.update(msg("/deposit Luca 100"), at(7));
t.tg.clear();

const who = { [String(OWNER)]: "Boss (private)", [String(MARCO)]: "Marco (private)", [String(LUCA)]: "Luca (private)", [GROUP]: "group" };
let seen = 0;
const plain = (x) => String(x || "").replace(/<a href="tg:\/\/user\?id=\d+">([^<]+)<\/a>/g, "@$1").replace(/<[^>]+>/g, "")
  .replace(/&amp;/g, "&").replace(/\n/g, "\n                 ");
function show(label, when) {
  console.log(`\n${hhmm(when)}  ${label}`);
  for (const c of t.tg.calls.slice(seen)) {
    const p = c.payload;
    if (c.method === "answerCallbackQuery") console.log(`        (pop-up) ${p.text}`);
    else if (c.method === "sendMessage" || c.method === "sendPhoto") {
      const btns = p.reply_markup?.inline_keyboard?.flat().map((b) => `[${b.text}]`).join(" ") || "";
      console.log(`        → ${who[String(p.chat_id)] || p.chat_id}${p.message_thread_id ? ` / topic ${p.message_thread_id}` : ""}: ${plain(p.text || p.caption)}${btns ? `\n                 ${btns}` : ""}`);
    } else if (c.method === "pinChatMessage") console.log("        (pinned the duty message)");
    else if (c.method === "editMessageText" && p.text?.startsWith("👮")) console.log(`        (pinned message now) ${plain(p.text)}`);
  }
  seen = t.tg.calls.length;
}
const step = async (label, when, fn) => { await fn(); show(label, when); };
const deal = (n, title, cost, source = "vinted", group = "Electronics") => ({ key: `${source}:${n}`, text: `🔥 <b>${title}</b>\n💶 €${cost}`,
  photo: "", group, record: { title, url: `https://example.test/${n}`, source, cost, value: cost * 2, profit: cost * 0.6, query: "q", score: [0, 0, 7, 20] } });

await t.store.put("schedule", { [DAY]: { 8: MARCO, 9: MARCO, 10: MARCO, 11: MARCO, 12: MARCO, 13: MARCO, 15: LUCA, 16: LUCA } });
await step("cron (before 07:30: quiet)", at(7, 20), () => t.cron(at(7, 20)));
await step("cron 07:30", at(7, 30), () => t.cron(at(7, 30)));
await step("Marco taps 🟢 Start duty", at(7, 55), () => t.update(tap("duty:on", { user: MARCO, chat: Number(GROUP) }), at(7, 55)));
await step("scanner finds an iPhone on Subito", at(9, 0), () => t.api("POST", "/api/deal", deal(1, "iPhone 13 128GB", 280, "subito"), { at: at(9) }));
await step("cron 09:11 (nobody claimed it yet)", at(9, 11), () => t.cron(at(9, 11)));
await step("Marco claims it", at(9, 12), () => t.update(tap("c:subito:1", { user: MARCO, first: "Marco", chat: Number(GROUP) }), at(9, 12)));
await step("Marco taps 🙋 Request buy", at(9, 30), () => t.update(tap("b:subito:1", { user: MARCO, chat: Number(GROUP) }), at(9, 30)));
await step("Marco replies with the agreed price", at(9, 31), () => t.update(msg("265", { user: MARCO, chat: Number(GROUP) }), at(9, 31)));
await step("Boss taps ✅ Approve", at(9, 40), () => t.update(tap("ap:subito:1", { chat: OWNER }), at(9, 40)));
await step("Boss adds a task", at(10, 0), () => t.update(msg("/task add Photos and listing for #1 @Luca by fri", { chat: Number(GROUP) }), at(10, 0)));
await step("Marco taps 🙋 Need a swap", at(12, 0), () => t.update(tap("duty:swap", { user: MARCO, chat: Number(GROUP) }), at(12, 0)));
await step("Marco picks 'Next 2 h'", at(12, 1), () => t.update(tap(`swr:2:${MARCO}`, { user: MARCO, chat: Number(GROUP) }), at(12, 1)));
await step("Luca taps ✅ I'll take it", at(12, 5), () => t.update(tap("swt:1", { user: LUCA, chat: Number(GROUP) }), at(12, 5)));
await step("Luca ends duty", at(14, 1), () => t.update(tap("duty:off", { user: LUCA, chat: Number(GROUP) }), at(14, 1)));
await step("cron 14:05 (nobody on duty)", at(14, 5), () => t.cron(at(14, 5)));
await step("cron 14:50 (Luca's 15:00 block)", at(14, 50), () => t.cron(at(14, 50)));
await step("cron 15:15 (Luca still not on)", at(15, 15), () => t.cron(at(15, 15)));
await step("Luca starts duty", at(15, 17), () => t.update(tap("duty:on", { user: LUCA, chat: Number(GROUP) }), at(15, 17)));
await step("cron 22:01", at(22, 1), () => t.cron(at(22, 1)));
await step("/tasks", at(22, 5), () => t.update(msg("/tasks", { chat: Number(GROUP) }), at(22, 5)));
await t.store.put("status", { last_run: romeTs("2026-10-11", 20) });
for (let i = 0; i < 5; i++) await t.api("POST", "/api/run", { status: { runs: 1 } }, { at: romeTs("2026-10-11", 19) });
const sun = romeTs("2026-10-11", 20, 5);
await step("Sunday 20:05: the weekly report", sun, () => t.cron(sun));
