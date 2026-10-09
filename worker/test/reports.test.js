// Report files: weekly and monthly Excel reports, predicted vs real profit, and suggestions with ✅ Apply.

import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, tap, OWNER, MARCO, GROUP, DAYTIME } from "./helpers.js";
import { period, parseAction, MIN_SALES } from "../src/reports.js";

const DAY = 86400;
const SUNDAY = Date.UTC(2026, 9, 11, 18, 5) / 1000;   // Sunday 11 Oct, 20:05 in Italy
const text = (t) => ({ content: [{ type: "text", text: t }] });
const SUGGEST = [
  "SUGGESTION: iPhones sold 20% under the predicted price, so lower their values | ACTION: value electronics -15",
  "SUGGESTION: Pedals sold in 3 days on average | ACTION: music min_profit 15",
  "SUGGESTION: Raise the bar for the main deals | ACTION: set min_profit 9999999",   // out of range: shown, no button
].join("\n");

/** A team with sales: `sales` deals bought on 6 Oct and sold on 9 Oct, one in stock. */
async function withSales(sales = 3) {
  const t = await setup({ settings: { allowed_users: [MARCO], topics: { summary: 44 }, ai: { enabled: true },
    rules: { min_profit: 50, min_roi: 40 } } });
  t.env.ANTHROPIC_API_KEY = "test-key";
  t.tg.answerClaude = () => text(SUGGEST);
  for (let n = 1; n <= sales + 1; n++) {
    const key = `vinted:${n}`;
    const d = await t.store.addDeal(key, { title: `iPhone 13 #${n}`, text: `iPhone ${n}`, group: "Electronics", cost: 160, value: 220,
      profit: 50, sent: DAYTIME - DAY });
    const sold = n <= sales;
    Object.assign(d, { status: sold ? "sold" : "bought", paid: 150, bought_at: DAYTIME - DAY + 600, messages: [{ chat: GROUP, id: n }],
      ...(sold ? { listed_at: DAYTIME, sold_at: DAYTIME + 2 * DAY, sold_for: 190 } : {}) });
    await t.store.saveDeal(key, d);
    await t.store.addEntry({ at: DAYTIME - DAY + 600, kind: "buy", amount: -150, n, deal: key });
    if (sold) await t.store.addEntry({ at: DAYTIME + 2 * DAY, kind: "sale", amount: 190, n, deal: key, profit: { Owner: 40 } });
  }
  const no = await t.store.addDeal("vinted:90", { title: "Fake PS5", text: "x", group: "Electronics", sent: DAYTIME });
  await t.store.saveDeal("vinted:90", { ...no, messages: [{ chat: GROUP, id: 90 }], ai: { verdict: "no", lines: ["❌ NO. Fake.", "⚠️ x"] },
    ai_overruled: { by: MARCO, at: DAYTIME } });
  return t;
}

const open = async (t, now) => (await import("../src/app.js")).open(t.env, { fetchFn: t.tg.fetch, now });

test("periods: the 7 days up to Sunday; on the 1st, the whole month before (January: December)", () => {
  assert.deepEqual(period("week", SUNDAY), { from: "2026-10-05", to: "2026-10-11" });
  assert.deepEqual(period("month", Date.UTC(2026, 10, 1, 9) / 1000), { from: "2026-10-01", to: "2026-10-31" });
  assert.deepEqual(period("month", Date.UTC(2027, 0, 1, 9) / 1000), { from: "2026-12-01", to: "2026-12-31" });
});

test("suggestions only become buttons for actions we allow, within limits", () => {
  const view = { rules: { min_profit: 50 }, music_rules: { min_profit: 20 }, searches: [{ query: "iphone 13" }] };
  assert.deepEqual(parseAction("set min_profit 60", view), { kind: "set", name: "min_profit", value: 60, was: 50 });
  assert.deepEqual(parseAction("music min_roi 30", view), { kind: "music", name: "min_roi", value: 30, was: null });
  assert.deepEqual(parseAction("value iphone 13 -15", view), { kind: "value", target: "iphone 13", change: -15 });
  assert.deepEqual(parseAction("value Electronics -15%", view), { kind: "value", target: "electronics", change: -15 });
  for (const bad of ["value electronics -60", "value guitars of mars -10", "set min_profit 99999", "delete everything", "none"]) {
    assert.equal(parseAction(bad, view), null, bad);
  }
});

test("the report: deals, buys, sales with days to sell, predicted vs real profit per category, stock, the AI's NOs", async () => {
  const t = await withSales(3);
  const { bot } = await open(t, SUNDAY);
  const r = await bot.reports.data("week", "2026-10-05", "2026-10-11");
  assert.equal(r.buys.length, 4);
  assert.deepEqual(r.sales.map((s) => [s.days, s.paid, s.sold, s.profit]), [[2, 150, 190, 40], [2, 150, 190, 40], [2, 150, 190, 40]]);
  assert.deepEqual(r.prediction, [{ group: "Electronics", sales: 3, predicted: 50, real: 40, price_off: 190 / 220 - 1, days: 2 }]);
  assert.equal(r.stock.length, 1);
  assert.equal(r.in_stock, 150);
  assert.deepEqual([r.ai.no, r.ai.overruled], [1, 1]);
  const sheets = bot.reports.sheets(r, { note: "", items: [] });
  assert.deepEqual(sheets.map((s) => s.name), ["Summary", "Deals per day", "Buys", "Sales", "Stock", "AI"]);
  const summary = JSON.stringify(sheets[0].rows);
  assert.ok(summary.includes("Predicted vs real profit, per category") && summary.includes("\"Electronics\",3"));
  assert.ok(JSON.stringify(sheets[5].rows).includes("Fake PS5"));                     // a NO sent back
});

test("fewer than 3 sales: no suggestions, and no AI call", async () => {
  const t = await withSales(2);
  const { bot } = await open(t, SUNDAY);
  const r = await bot.reports.make("week", { save: false });
  assert.equal(r.suggestions.note, `Not enough data yet: 2 sales in this period, I need at least ${MIN_SALES} before suggesting changes.`);
  assert.equal(t.tg.claude.length, 0);
  assert.ok(r.bytes[0] === 0x50 && r.bytes[1] === 0x4b);                              // a zip: the .xlsx
});

test("Sunday 20:00: the file to the owner with the suggestions and ✅ Apply, and pinned in Summary", async () => {
  const t = await withSales(3);
  await t.cron(SUNDAY);
  const docs = t.tg.sent("sendDocument");
  assert.deepEqual(docs.map((d) => String(d.chat_id)), [String(OWNER), GROUP]);
  assert.equal(docs[1].message_thread_id, "44");
  assert.equal(docs[0].document.name, "flipfinder-week-2026-10-11.xlsx");
  assert.match(docs[0].caption, /📊 <b>Weekly report<\/b> · 5 Oct 2026 to 11 Oct 2026\n.*sold 3 · profit €120\.00 \(ROI 27%\)/);
  assert.equal(t.tg.sent("pinChatMessage").at(-1).chat_id, GROUP);
  const sug = t.tg.sent().find((p) => p.text?.startsWith("💡 <b>Suggestions</b>"));
  assert.equal(String(sug.chat_id), String(OWNER));
  assert.deepEqual(sug.reply_markup.inline_keyboard.map((r) => r[0].callback_data), ["sug:week-2026-10-11:0", "sug:week-2026-10-11:1"]);
  assert.match(sug.text, /3\. Raise the bar for the main deals\.\n   \(nothing to apply automatically\)/);
  // the AI saw numbers only: no names, no chat ids
  const asked = JSON.stringify(t.tg.claude[0].body.messages);
  assert.ok(asked.includes("per_category") && !asked.includes("Marco") && !asked.includes(GROUP));
  // kept privately; /reports sends it again
  assert.ok(await t.store.get("report_file:week-2026-10-11"));
  t.tg.clear();
  await t.update(msg("/reports", { user: MARCO }), SUNDAY);
  assert.match(t.tg.texts().at(-1), /Only the owner/);
  await t.update(msg("/reports"), SUNDAY);
  assert.deepEqual(t.tg.sent().at(-1).reply_markup.inline_keyboard, [[{ text: "Week 10-05 to 10-11", callback_data: "rep:week-2026-10-11" }]]);
  await t.update(tap("rep:week-2026-10-11"), SUNDAY);
  assert.equal(t.tg.sent("sendDocument")[0].document.name, "flipfinder-week-2026-10-11.xlsx");
});

test("✅ Apply: owner only, once; the scanner gets the change from the next run", async () => {
  const t = await withSales(3);
  await t.cron(SUNDAY);
  t.tg.clear();
  await t.update(tap("sug:week-2026-10-11:0", { user: MARCO }), SUNDAY);
  assert.match(t.tg.sent("answerCallbackQuery").at(-1).text, /Only the owner/);
  await t.updates(tap("sug:week-2026-10-11:0"), tap("sug:week-2026-10-11:1"), tap("sug:week-2026-10-11:0"));
  const s = await t.settings();
  assert.deepEqual(s.value_adjust, { electronics: 0.85 });
  assert.equal(s.music_rules.min_profit, 15);
  assert.match(t.tg.sent("answerCallbackQuery").at(-1).text, /Already applied/);
  assert.ok(t.tg.texts().includes("✅ Applied: electronics values -15% (from the next scan)"));
  const state = await t.api("GET", "/api/state");
  assert.deepEqual(state.body.settings.value_adjust, { electronics: 0.85 });
});

test("the monthly report comes once, on the 1st from 09:00, for the month before", async () => {
  const t = await withSales(3);
  const first = Date.UTC(2026, 10, 1, 8, 30) / 1000;   // 1 Nov, 09:30 in Italy
  await t.cron(first - 3600);
  assert.equal(t.tg.sent("sendDocument").length, 0);
  await t.cron(first);
  await t.cron(first + 600);
  const docs = t.tg.sent("sendDocument");
  assert.equal(docs.length, 2);
  assert.equal(docs[0].document.name, "flipfinder-month-2026-10.xlsx");
  assert.match(docs[0].caption, /Monthly report<\/b> · 1 Oct 2026 to 31 Oct 2026/);
});
