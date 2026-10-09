// Daily stats: one row per day (Italy time), worked out from what we already keep: the deals (sent,
// the AI's ✅/❌, claims), the pot's ledger (buys, sales, corrections, profit) and the AI's daily spend.
// Profit counts sold items only; money in unsold stock is shown on its own ("In stock").
// Deals nobody touched are dropped after 30 days, so each day's deal counts are frozen in kv
// "stats_log" a few days after it ends (the cron), and the frozen numbers are used from then on.

import { euro, rome } from "./util.js";

export const STATS_FROM = "2026-10-06";   // the first day with data
const FREEZE_AFTER = 3;                    // days before a day's deal counts are frozen
const DAY = 86400;

const r2 = (x) => Math.round(x * 100) / 100;
const sum = (o) => Object.values(o || {}).reduce((s, v) => s + v, 0);

/** "2026-10-09" for `days` days ending on today (oldest first), never before STATS_FROM. */
export function dayList(now, days) {
  const out = [];
  for (let i = days - 1; i >= 0; i--) {
    const date = rome(now - i * DAY).date;
    if (date >= STATS_FROM && !out.includes(date)) out.push(date);
  }
  return out;
}

/** The Monday of a date's week ("2026-10-06" -> "2026-10-05"). */
export function weekOf(date) {
  const d = new Date(`${date}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7));
  return d.toISOString().slice(0, 10);
}

/** Every day's raw numbers, by date: deals from the deals table, money from the ledger, AI spend. */
async function collect(bot) {
  const deals = await bot.allDeals();
  const ledger = await bot.store.ledger();
  const days = {};
  const row = (date) => (days[date] ??= { date, sent: 0, yes: 0, no: 0, claimed: 0, bought: 0, spent: 0, sold: 0, made: 0,
    profit: 0, sold_cost: 0, ai_usd: 0 });
  const byKey = Object.fromEntries(deals);
  for (const [, d] of deals) {
    if (d.messages?.length && d.sent) {
      const r = row(rome(d.sent).date);
      if (!d.rejected) r.sent++;   // ❌ Rejected deals only count in the NO column
      const v = d.ai?.verdict ?? d.ai_verdict;
      if (v === "yes") r.yes++;
      if (v === "no") r.no++;
    }
    if (d.claimed_at) row(rome(d.claimed_at).date).claimed++;
  }
  const byId = Object.fromEntries(ledger.map((e) => [e.id, e]));
  for (const e of ledger) {
    const r = row(rome(e.at).date);
    r.profit += sum(e.profit);
    if (e.kind === "buy") {
      r.bought++;
      r.spent -= e.amount;
    } else if (e.kind === "sale") {
      r.sold++;
      r.made += e.amount;
      r.sold_cost += e.amount - sum(e.profit);   // what we'd paid for it
    } else if (e.kind === "fix" || (e.kind === "undo" && byId[e.ref]?.kind === "fix")) {
      // a corrected price: "paid" moves what we spent, "sold" what we made
      const note = (e.kind === "fix" ? e : byId[e.ref]).note || "";
      if (note.startsWith("paid")) r.spent -= e.amount;
      else if (note.startsWith("sold")) r.made += e.amount;
    }
  }
  // AI spend per day; before the daily log existed, from the last calls' log
  const daily = await bot.store.get("ai_daily", {});
  const calls = {};
  for (const c of await bot.store.get("ai_calls", [])) calls[rome(c.at).date] = (calls[rome(c.at).date] || 0) + (c.usd || 0);
  for (const [date, usd] of Object.entries({ ...calls, ...daily })) row(date).ai_usd = usd;
  // frozen deal counts for older days (their deals may have been dropped since)
  const log = await bot.store.get("stats_log", {});
  for (const [date, frozen] of Object.entries(log)) Object.assign(row(date), frozen);
  const inStock = deals.filter(([, d]) => ["bought", "listed"].includes(d.status)).reduce((s, [, d]) => s + (d.paid || 0), 0);
  return { days, inStock: r2(inStock), byKey };
}

const EMPTY = { sent: 0, yes: 0, no: 0, claimed: 0, bought: 0, spent: 0, sold: 0, made: 0, profit: 0, sold_cost: 0, ai_usd: 0 };

/** Every date from `from` to `to` ("YYYY-MM-DD", both included), never before STATS_FROM. */
export function datesBetween(from, to) {
  const out = [];
  for (let t = Date.parse(`${from}T12:00:00Z`); t <= Date.parse(`${to}T12:00:00Z`); t += DAY * 1000) {
    const date = new Date(t).toISOString().slice(0, 10);
    if (date >= STATS_FROM) out.push(date);
  }
  return out;
}

/**
 * Rows for the last `days` days (oldest first), or `from`-`to`, their totals, money in stock and
 * profit per week.
 */
export async function dailyStats(bot, { days = 7, from = null, to = null } = {}) {
  const { days: all, inStock } = await collect(bot);
  const eur = bot.ai.config.usd_to_eur;
  const shape = (r) => ({ date: r.date, sent: r.sent, yes: r.yes, no: r.no, claimed: r.claimed, bought: r.bought,
    spent: r2(r.spent), sold: r.sold, made: r2(r.made), profit: r2(r.profit), sold_cost: r2(r.sold_cost),
    ai: Math.round(r.ai_usd * eur * 10000) / 10000 });
  const dates = from && to ? datesBetween(from, to) : dayList(bot.now, days);
  const rows = dates.map((date) => shape({ ...EMPTY, ...(all[date] || {}), date }));
  const totals = rows.reduce((t, r) => {
    for (const k of Object.keys(EMPTY).filter((k) => k !== "ai_usd")) t[k] += r[k];
    t.ai += r.ai;
    return t;
  }, { ...EMPTY, ai: 0 });
  for (const k of ["spent", "made", "profit", "sold_cost"]) totals[k] = r2(totals[k]);
  totals.ai = Math.round(totals.ai * 10000) / 10000;
  totals.roi = totals.sold_cost > 0 ? Math.round((totals.profit / totals.sold_cost) * 100) : null;
  const weeks = {};
  for (const r of Object.values(all)) if (r.date >= STATS_FROM) weeks[weekOf(r.date)] = r2((weeks[weekOf(r.date)] || 0) + r.profit);
  const weekly = Object.entries(weeks).sort().map(([week, profit]) => ({ week, profit }));
  return { rows, totals, in_stock: inStock, weekly };
}

/** Freezes the deal counts of days that ended FREEZE_AFTER days ago or more (once a day, by the cron). */
export async function freezeDays(bot) {
  const log = await bot.store.get("stats_log", {});
  const { days } = await collect(bot);
  const last = rome(bot.now - FREEZE_AFTER * DAY).date;
  let changed = false;
  for (const [date, r] of Object.entries(days)) {
    if (date < STATS_FROM || date > last || log[date]) continue;
    log[date] = { sent: r.sent, yes: r.yes, no: r.no };
    changed = true;
  }
  if (changed) await bot.store.put("stats_log", log);
  return changed;
}

const money0 = (x) => (x < 0 ? "-" : "") + Math.round(Math.abs(x));
const pad = (s, n) => String(s).padStart(n);

/** /stats: a short table (fits a phone) and the totals. */
export function statsText(s, days) {
  const lines = [`📊 <b>Stats, last ${days} days</b>`, "<pre>Day   Sent   Y   N Cl Buy     Sold    Prof  AI"];
  for (const r of s.rows) {
    lines.push(`${r.date.slice(5)} ${pad(r.sent, 4)} ${pad(r.yes, 3)} ${pad(r.no, 3)} ${pad(r.claimed, 2)} ` +
      `${`${r.bought}·${money0(r.spent)}`.padEnd(7)} ${`${r.sold}·${money0(r.made)}`.padEnd(7)} ${pad(money0(r.profit), 4)} ${r.ai.toFixed(2).replace(/^0/, "")}`);
  }
  lines[lines.length - 1] += "</pre>";
  const t = s.totals;
  lines.push("Sent = in the deal topics · Y/N = the AI's ✅/❌ · Cl = claimed · Buy, Sold = how many · €",
    "",
    `<b>Totals</b>: ${t.sent} deals (✅ ${t.yes} · ❌ ${t.no}) · ✋ ${t.claimed} claimed`,
    `💸 Bought ${t.bought} for ${euro(t.spent)} · ✅ Sold ${t.sold} for ${euro(t.made)}`,
    `💰 Profit (sold items): <b>${euro(t.profit)}</b>${t.roi !== null ? ` · ROI ${t.roi}%` : ""}`,
    `📦 In stock: ${euro(s.in_stock)} · 🧠 AI: €${t.ai.toFixed(2)}`);
  return lines.join("\n");
}

/** The Sunday report's line: the last 7 days' totals. */
export function weekLine(s) {
  const t = s.totals;
  return `\n\n📊 <b>This week</b>: ${t.sent} deals (✅ ${t.yes} · ❌ ${t.no}), ✋ ${t.claimed} claimed, ` +
    `💸 ${t.bought} bought for ${euro(t.spent)}, ✅ ${t.sold} sold for ${euro(t.made)}, profit <b>${euro(t.profit)}</b>` +
    `${t.roi !== null ? ` (ROI ${t.roi}%)` : ""} · in stock ${euro(s.in_stock)} · AI €${t.ai.toFixed(2)}`;
}

/** /stats csv: every day since the start, for Excel or Google Sheets. */
export function statsCsv(s) {
  const head = "Date,Deals sent,YES,NO,Claimed,Bought,Spent EUR,Sold,Made EUR,Profit EUR,AI cost EUR";
  const rows = s.rows.map((r) => [r.date, r.sent, r.yes, r.no, r.claimed, r.bought, r.spent.toFixed(2), r.sold, r.made.toFixed(2),
    r.profit.toFixed(2), r.ai.toFixed(4)].join(","));
  const t = s.totals;
  rows.push(["Total", t.sent, t.yes, t.no, t.claimed, t.bought, t.spent.toFixed(2), t.sold, t.made.toFixed(2), t.profit.toFixed(2),
    t.ai.toFixed(4)].join(","), `In stock EUR,${s.in_stock.toFixed(2)}`);
  return `${head}\n${rows.join("\n")}\n`;
}

/** How many days since STATS_FROM, today included (for the CSV). */
export function daysSinceStart(now) {
  return Math.max(1, Math.round((Date.parse(`${rome(now).date}T12:00:00Z`) - Date.parse(`${STATS_FROM}T12:00:00Z`)) / 1000 / DAY) + 1);
}
