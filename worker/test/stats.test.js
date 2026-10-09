// /stats: one row per day from the deals, the pot's ledger and the AI's spend.

import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, MARCO, DAYTIME } from "./helpers.js";
import { dailyStats, dayList, weekOf, statsCsv } from "../src/stats.js";

const DAY = 86400;
const D6 = DAYTIME - DAY;            // 6 Oct, 12:00 in Italy
const D7 = DAYTIME;                  // 7 Oct
const D8 = DAYTIME + DAY;            // 8 Oct (today in these tests)

async function withData() {
  const t = await setup({ settings: { allowed_users: [MARCO] } });
  t.now = D8;
  const deal = async (n, sent, extra = {}) => {
    const d = await t.store.addDeal(`vinted:${n}`, { title: `Deal ${n}`, text: `Deal ${n}`, cost: 50, value: 120, profit: 60,
      sent, ...extra });
    d.messages = extra.queued ? [] : [{ chat: "-100", id: n }];
    await t.store.saveDeal(`vinted:${n}`, d);
  };
  await deal(1, D6, { ai: { verdict: "yes" } });
  await deal(2, D6, { ai_verdict: "no" });                              // checked after the fact
  await deal(3, D7, { ai: { verdict: "yes" }, status: "claimed", claimed_at: D7 + 60 });
  await deal(4, D7, { status: "sold", claimed_at: D7 + 120, paid: 40, sold_for: 90 });
  await deal(5, D8);                                                      // no AI answer
  await deal(6, D8, { queued: true });                                    // not sent (overnight queue)
  // the ledger: 7 Oct bought #4 for 40, 8 Oct sold it for 90 (profit 50), then a correction: paid 45
  await t.store.addEntry({ at: D6, kind: "deposit", amount: 300, member: "Owner", deposited: { Owner: 300 } });
  await t.store.addEntry({ at: D7, kind: "buy", amount: -40, n: 4, deal: "vinted:4" });
  await t.store.addEntry({ at: D8, kind: "sale", amount: 90, n: 4, deal: "vinted:4", profit: { Owner: 50 } });
  await t.store.addEntry({ at: D8 + 60, kind: "fix", amount: -5, n: 4, deal: "vinted:4", note: "paid €40.00 → €45.00", profit: { Owner: -5 } });
  // something still in stock (not profit)
  const amp = await t.store.addDeal("vinted:7", { title: "Amp", text: "Amp", cost: 80, sent: D6 - 10 * DAY });
  await t.store.saveDeal("vinted:7", { ...amp, status: "bought", paid: 75 });
  await t.store.put("ai_daily", { "2026-10-07": 0.02, "2026-10-08": 0.01 });
  return t;
}

const bot = async (t) => (await import("../src/app.js")).open(t.env, { fetchFn: t.tg.fetch, now: t.now });

test("one row per Italian day: sent, the AI's YES/NO, claims, buys, sales, profit, AI cost; stock apart", async () => {
  const t = await withData();
  const { bot: b } = await bot(t);
  const s = await dailyStats(b, { days: 7 });
  assert.deepEqual(s.rows.map((r) => r.date), ["2026-10-06", "2026-10-07", "2026-10-08"]);   // nothing before 6 Oct
  const [d6, d7, d8] = s.rows;
  assert.deepEqual([d6.sent, d6.yes, d6.no, d6.claimed], [2, 1, 1, 0]);
  assert.deepEqual([d7.sent, d7.yes, d7.claimed, d7.bought, d7.spent], [2, 1, 2, 1, 40]);
  assert.deepEqual([d8.sent, d8.sold, d8.made, d8.spent, d8.profit], [1, 1, 90, 5, 45]);    // the correction: €5 more spent, €5 less profit
  assert.equal(d7.ai, 0.0184);                                                             // $0.02 at 0.92
  assert.deepEqual([s.totals.sent, s.totals.spent, s.totals.made, s.totals.profit], [5, 45, 90, 45]);
  assert.equal(s.totals.roi, 113);                                                         // 45 profit on 40 paid
  assert.equal(s.in_stock, 75);
  assert.deepEqual(s.weekly, [{ week: "2026-10-05", profit: 45 }]);
});

test("/stats: a short table and the totals; /stats 30; bad input", async () => {
  const t = await withData();
  await t.update(msg("/stats", { user: MARCO }), D8);
  const text = t.tg.texts().at(-1);
  assert.match(text, /📊 <b>Stats, last 7 days<\/b>\n<pre>Day   Sent   Y   N Cl Buy     Sold    Prof  AI\n10-06    2   1   1  0/);
  assert.match(text, /\n10-08    1   0   0  0 0·5     1·90      45 \.01<\/pre>/);
  assert.match(text, /Totals<\/b>: 5 deals \(✅ 2 · ❌ 1\) · ✋ 2 claimed/);
  assert.match(text, /💸 Bought 1 for €45\.00 · ✅ Sold 1 for €90\.00/);
  assert.match(text, /💰 Profit \(sold items\): <b>€45\.00<\/b> · ROI 113%/);
  assert.match(text, /📦 In stock: €75\.00 · 🧠 AI: €0\.03/);
  // the table stays narrow enough for a phone, even with 3-digit counts and 4-digit amounts
  assert.ok(text.split("<pre>")[1].split("</pre>")[0].split("\n").every((l) => l.length <= 46));
  const { statsText } = await import("../src/stats.js");
  const big = { date: "2026-10-07", sent: 121, yes: 100, no: 21, claimed: 3, bought: 12, spent: 1450, sold: 9, made: 1630, profit: 480, ai: 0.18 };
  const wide = statsText({ rows: [big], totals: { ...big, roi: 33 }, in_stock: 0 }, 7).split("\n");
  assert.equal(wide[1].replace("<pre>", "").indexOf("Prof") + 4, wide[2].indexOf("480") + 3);   // columns line up
  assert.ok(wide[2].replace("</pre>", "").length <= 46);
  await t.update(msg("/stats 30", { user: MARCO }), D8);
  assert.match(t.tg.texts().at(-1), /last 30 days/);
  await t.update(msg("/stats lots", { user: MARCO }), D8);
  assert.match(t.tg.texts().at(-1), /Use \/stats/);
});

test("/stats csv: the owner gets a file for Excel or Google Sheets; others don't", async () => {
  const t = await withData();
  await t.update(msg("/stats csv", { user: MARCO }), D8);
  assert.equal(t.tg.sent("sendDocument").length, 0);
  assert.match(t.tg.texts().at(-1), /owner/i);
  await t.update(msg("/stats csv"), D8);
  const [doc] = t.tg.sent("sendDocument");
  assert.equal(doc.document.name, "flipfinder-stats-2026-10-08.csv");
  const lines = doc.document.text.trim().split("\n");
  assert.equal(lines[0], "Date,Deals sent,YES,NO,Claimed,Bought,Spent EUR,Sold,Made EUR,Profit EUR,AI cost EUR");
  assert.equal(lines[1], "2026-10-06,2,1,1,0,0,0.00,0,0.00,0.00,0.0000");
  assert.equal(lines.at(-2), "Total,5,2,1,2,1,45.00,1,90.00,45.00,0.0276");
  assert.equal(lines.at(-1), "In stock EUR,75.00");
});

test("old days keep their counts after untouched deals are dropped (frozen by the daily cron)", async () => {
  const t = await withData();
  const later = D8 + 40 * DAY;
  await t.cron(D8 + 4 * DAY);                            // the daily 10:00 check freezes days that ended 3+ days ago
  await t.cron(later);                                   // deals nobody touched for 30 days are dropped
  assert.equal((await t.store.get("stats_log"))["2026-10-06"].sent, 2);
  assert.equal(await t.store.deal("vinted:1"), null);    // dropped
  const { bot: b } = await (await import("../src/app.js")).open(t.env, { fetchFn: t.tg.fetch, now: later });
  const s = await dailyStats(b, { days: 45 });
  assert.deepEqual(s.rows.find((r) => r.date === "2026-10-06").sent, 2);
});

test("the Sunday report has the week's totals; the Mini App gets the last 30 days", async () => {
  const t = await withData();
  const sunday = Date.UTC(2026, 9, 11, 18, 5) / 1000;   // Sunday 20:05 in Italy
  await t.cron(sunday);
  const report = t.tg.texts().find((x) => x.includes("📊 <b>This week</b>"));
  assert.match(report, /This week<\/b>: 5 deals \(✅ 2 · ❌ 1\), ✋ 2 claimed, 💸 1 bought for €45\.00, ✅ 1 sold for €90\.00, profit <b>€45\.00<\/b>/);
  const { snapshot } = await import("../src/webapp.js");
  const { bot: b } = await bot(t);
  const state = await snapshot(b, { id: MARCO });
  assert.equal(state.stats.rows.length, 3);
  assert.equal(state.stats.totals.profit, 45);
});

test("helpers: days in Italy time, weeks start on Monday, the AI's spend is logged per day", async () => {
  assert.deepEqual(dayList(D8, 3), ["2026-10-06", "2026-10-07", "2026-10-08"]);
  assert.equal(weekOf("2026-10-11"), "2026-10-05");
  assert.equal(weekOf("2026-10-12"), "2026-10-12");
  assert.match(statsCsv({ rows: [], totals: { sent: 0, yes: 0, no: 0, claimed: 0, bought: 0, spent: 0, sold: 0, made: 0, profit: 0, ai: 0 },
    in_stock: 0 }), /^Date,/);
  const { AI } = await import("../src/ai.js");
  const t = await setup();
  const { bot: b } = await bot(t);
  await b.ai.track("claude-haiku-5-5", { input_tokens: 1000, output_tokens: 100 });
  assert.equal((await t.store.get("ai_daily"))["2026-10-07"], AI.cost("claude-haiku-5-5", { input_tokens: 1000, output_tokens: 100 }));
});
