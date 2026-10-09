// Report files: every Sunday at 20:00 (the week) and on the 1st (the month before), an Excel file sent
// to the owner in private and pinned in the Summary topic, kept in private storage (/reports lists
// them). Tabs: Summary, Deals per day, Buys, Sales, Stock, AI. The key check is predicted vs real
// profit on each sale, per category. At the end, the AI's suggestions from the numbers: each one is
// a known action with an ✅ Apply button for the owner; nothing changes without that tap.

import { dailyStats } from "./stats.js";
import { RULES } from "./searches.js";
import { workbook, toBase64, fromBase64, XLSX_TYPE } from "./xlsx.js";
import { esc, euro, rome } from "./util.js";

const DAY = 86400;
export const MIN_SALES = 3;          // sales needed before the AI suggests anything
const KEEP = 60;                     // reports kept
const CATEGORIES = ["Electronics", "Budget", "Guitars", "Amps", "Pedals", "Audio"];
const MUSIC_RULES = { min_profit: [0, 1000], min_roi: [0, 1000] };

const r2 = (x) => Math.round(x * 100) / 100;
const sum = (o) => Object.values(o || {}).reduce((s, v) => s + v, 0);
const day = (ts) => rome(ts).date;
const eur = (v) => (v === null || v === undefined ? null : { v: r2(v), s: "euro" });
const pct = (v) => (v === null || v === undefined || !Number.isFinite(v) ? null : { v: Math.round(v * 1000) / 1000, s: "pct" });
const date = (ts) => (ts ? { v: day(ts), s: "date" } : null);
const nice = (d) => new Date(`${d}T12:00:00Z`).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });

/** The report's dates: the 7 days up to today, or last month. */
export function period(kind, now) {
  const today = rome(now).date;
  if (kind === "week") {
    return { from: rome(now - 6 * DAY).date, to: today };
  }
  const [y, m] = today.split("-").map(Number);
  const first = new Date(Date.UTC(m === 1 ? y - 1 : y, m === 1 ? 11 : m - 2, 1));
  const last = new Date(Date.UTC(y, m - 1, 0));
  return { from: first.toISOString().slice(0, 10), to: last.toISOString().slice(0, 10) };
}

/** A suggestion's action ("set min_profit 60", "music min_roi 30", "value Electronics -15") checked against what we allow. */
export function parseAction(text, view) {
  const t = String(text || "").trim();
  let m = t.match(/^set (min_profit|min_roi|max_price|min_rating|max_roi) (-?\d+(?:\.\d+)?)$/i);
  if (m) {
    const [, lo, hi] = RULES[m[1].toLowerCase()];
    const value = Number(m[2]);
    if (value < lo || value > hi) return null;
    return { kind: "set", name: m[1].toLowerCase(), value, was: view.rules?.[m[1].toLowerCase()] ?? null };
  }
  m = t.match(/^music (min_profit|min_roi) (\d+(?:\.\d+)?)$/i);
  if (m) {
    const [lo, hi] = MUSIC_RULES[m[1].toLowerCase()];
    const value = Number(m[2]);
    if (value < lo || value > hi) return null;
    return { kind: "music", name: m[1].toLowerCase(), value, was: view.music_rules?.[m[1].toLowerCase()] ?? null };
  }
  m = t.match(/^value (.+?) ([+-]?\d+(?:\.\d+)?)%?$/i);
  if (m) {
    const target = m[1].trim().toLowerCase();
    const known = CATEGORIES.map((c) => c.toLowerCase()).includes(target) || view.searches?.some((s) => s.query.toLowerCase() === target);
    const change = Number(m[2]);
    if (!known || change < -40 || change > 40 || change === 0) return null;
    return { kind: "value", target, change };
  }
  return null;
}

/** What an action does, in words. */
export function actionText(a) {
  if (a.kind === "set") return `${a.name} ${a.was ?? "?"} → ${a.value}`;
  if (a.kind === "music") return `music ${a.name} ${a.was ?? "?"} → ${a.value}`;
  return `${a.target} values ${a.change > 0 ? "+" : ""}${a.change}%`;
}

export class Reports {
  constructor(bot) {
    this.bot = bot;
  }

  /** Everything in the report, for the dates given. */
  async data(kind, from, to) {
    const bot = this.bot;
    const deals = await bot.allDeals();
    const byKey = Object.fromEntries(deals);
    const inPeriod = (ts) => ts && day(ts) >= from && day(ts) <= to;
    const ledger = await bot.store.ledger();
    const stats = await dailyStats(bot, { from, to });
    const buys = ledger.filter((e) => e.kind === "buy" && inPeriod(e.at)).map((e) => {
      const d = byKey[e.deal] || {};
      return { n: e.n, title: d.title || "", group: d.group || "", at: e.at, paid: -e.amount, value: d.value ?? null,
        predicted: d.profit ?? null };
    });
    const sales = ledger.filter((e) => e.kind === "sale" && inPeriod(e.at)).map((e) => {
      const d = byKey[e.deal] || {};
      const profit = sum(e.profit);
      const paid = e.amount - profit;
      const since = d.listed_at || d.bought_at;
      return { n: e.n, title: d.title || "", group: d.group || "", bought: d.bought_at, listed: d.listed_at, at: e.at,
        days: since ? Math.max(0, Math.round((e.at - since) / DAY)) : null, paid, sold: e.amount, profit,
        roi: paid > 0 ? profit / paid : null, predicted: d.profit ?? null, value: d.value ?? null,
        price_off: d.value ? e.amount / d.value - 1 : null };
    });
    const stock = deals.filter(([, d]) => ["bought", "listed"].includes(d.status)).map(([, d]) => ({
      n: d.n, title: d.title, group: d.group || "", status: d.status, bought: d.bought_at, paid: d.paid ?? 0,
      days: d.bought_at ? Math.round((bot.now - d.bought_at) / DAY) : null, list_price: d.list_price ?? null,
      value: d.value_now ?? d.value ?? null }));
    // predicted vs real, per category
    const cats = {};
    for (const s of sales) {
      const c = (cats[s.group || "Other"] ??= { group: s.group || "Other", sales: 0, predicted: 0, real: 0, off: [], days: [] });
      c.sales++;
      c.predicted += s.predicted ?? 0;
      c.real += s.profit;
      if (s.price_off !== null) c.off.push(s.price_off);
      if (s.days !== null) c.days.push(s.days);
    }
    const prediction = Object.values(cats).map((c) => ({ group: c.group, sales: c.sales, predicted: r2(c.predicted / c.sales),
      real: r2(c.real / c.sales), price_off: c.off.length ? c.off.reduce((a, b) => a + b, 0) / c.off.length : null,
      days: c.days.length ? Math.round(c.days.reduce((a, b) => a + b, 0) / c.days.length) : null }));
    // the AI: checked, its NOs, and the NOs a person sent back (↩️ Not a NO)
    const checked = deals.filter(([, d]) => inPeriod(d.sent) && (d.ai?.verdict || d.ai_verdict)).map(([, d]) => d);
    const nos = checked.filter((d) => (d.ai?.verdict || d.ai_verdict) === "no");
    const overruled = nos.filter((d) => d.ai_overruled);
    const ai = { checked: checked.length, yes: checked.length - nos.length, no: nos.length, rejected: nos.filter((d) => d.rejected).length,
      overruled: overruled.length, overruled_list: overruled.map((d) => ({ n: d.n, title: d.title, why: d.ai?.lines?.[0] || "" })),
      cost: stats.totals.ai };
    return { kind, from, to, stats, buys, sales, stock, prediction, ai, in_stock: stats.in_stock };
  }

  /** The AI's suggestions from the numbers (only with MIN_SALES+ sales); each with a checked action. */
  async suggest(r) {
    if (r.sales.length < MIN_SALES) {
      return { note: `Not enough data yet: ${r.sales.length} sale${r.sales.length === 1 ? "" : "s"} in this period, ` +
        `I need at least ${MIN_SALES} before suggesting changes.`, items: [] };
    }
    const ai = this.bot.ai;
    if (!ai.config.enabled || (await ai.blocked("auto"))) return { note: "The AI is off or out of today's budget: no suggestions this time.", items: [] };
    const view = await this.bot.view();
    const numbers = {
      period: `${r.from} to ${r.to}`, deals_sent: r.stats.totals.sent, ai_yes: r.stats.totals.yes, ai_no: r.stats.totals.no,
      claimed: r.stats.totals.claimed, bought: r.buys.length, sold: r.sales.length, profit: r.stats.totals.profit,
      roi_pct: r.stats.totals.roi, in_stock: r.in_stock,
      per_category: r.prediction.map((p) => ({ category: p.group, sales: p.sales, predicted_profit_avg: p.predicted,
        real_profit_avg: p.real, sold_vs_predicted_price_pct: p.price_off === null ? null : Math.round(p.price_off * 100), days_to_sell_avg: p.days })),
      ai_no_overruled: `${r.ai.overruled} of ${r.ai.no}`,
      rules: view.rules, music_rules: view.music_rules, budget_rules: view.budget_rules,
    };
    const task = "Task: report suggestions. From these numbers of our reselling, give at most 3 suggestions that would " +
      "make more profit or fewer bad buys. Only suggest what the numbers clearly show; with nothing clear, write NONE.\n" +
      "Format, one per line: SUGGESTION: <one short sentence with the number that shows it> | ACTION: <one of: " +
      "set min_profit <euros> / set min_roi <percent> / set max_price <euros> / set min_rating <1-10> / " +
      "music min_profit <euros> / music min_roi <percent> / value <category> <percent change, -40 to 40> / none>\n" +
      `<numbers>\n${JSON.stringify(numbers)}\n</numbers>`;
    const raw = await ai.run([{ role: "user", content: [{ type: "text", text: task }] }], { maxLines: 6 });
    if (!raw) return { note: "The AI couldn't write suggestions this time.", items: [] };
    const items = [];
    for (const line of raw.split("\n")) {
      const m = line.match(/SUGGESTION:\s*(.+?)\s*\|\s*ACTION:\s*(.+)$/i);
      if (!m) continue;
      items.push({ text: m[1].replace(/\.$/, "") + ".", action: parseAction(m[2], view), applied: false });
    }
    return { note: items.length ? "" : "Nothing clear to change from this period's numbers.", items: items.slice(0, 3) };
  }

  /** The tabs. */
  sheets(r, sug) {
    const t = r.stats.totals;
    const title = `flipFinder ${r.kind === "week" ? "weekly" : "monthly"} report: ${nice(r.from)} to ${nice(r.to)}`;
    const summary = [
      [{ v: title, s: "bold" }], [],
      ["What", "Value"],
      ["Deals sent", t.sent], ["✅ AI said YES", t.yes], ["❌ AI said NO", t.no], ["✋ Claimed", t.claimed],
      ["💸 Bought", t.bought], ["Money spent", eur(t.spent)], ["✅ Sold", t.sold], ["Money made", eur(t.made)],
      ["💰 Profit (sold items only)", eur(t.profit)], ["ROI on sold items", pct(t.roi === null ? null : t.roi / 100)],
      ["📦 In stock (at cost)", eur(r.in_stock)], ["🧠 AI cost", eur(t.ai)],
      ["❌ NOs sent back (↩️ Not a NO)", `${r.ai.overruled} of ${r.ai.no}`], [],
      [{ v: "Predicted vs real profit, per category", s: "bold" }],
      ["Category", "Sales", "Predicted profit (avg)", "Real profit (avg)", "Sold vs predicted price", "Days to sell (avg)"],
      ...(r.prediction.length ? r.prediction.map((p) => [p.group, p.sales, eur(p.predicted), eur(p.real), pct(p.price_off), p.days])
        : [["No sales in this period"]]),
      [], [{ v: "Suggestions", s: "bold" }],
      ...(sug.note ? [[{ v: sug.note, s: "wrap" }]] : []),
      ...sug.items.map((x, i) => [{ v: `${i + 1}. ${x.text}${x.action ? ` (✅ Apply: ${actionText(x.action)})` : ""}`, s: "wrap" }]),
    ];
    return [
      { name: "Summary", head: 2, widths: [44, 14, 22, 18, 22, 18], rows: summary },
      { name: "Deals per day", head: 0, widths: [13, 8, 8, 8, 9, 9, 12, 8, 12, 12, 10],
        rows: [["Date", "Sent", "✅", "❌", "Claimed", "Bought", "Spent", "Sold", "Made", "Profit", "AI cost"],
          ...r.stats.rows.map((x) => [{ v: x.date, s: "date" }, x.sent, x.yes, x.no, x.claimed, x.bought, eur(x.spent), x.sold,
            eur(x.made), eur(x.profit), eur(x.ai)]),
          ["Total", t.sent, t.yes, t.no, t.claimed, t.bought, eur(t.spent), t.sold, eur(t.made), eur(t.profit), eur(t.ai)]] },
      { name: "Buys", head: 0, widths: [7, 40, 13, 13, 11, 14, 17],
        rows: [["#", "Item", "Category", "Date", "Paid", "Market value", "Predicted profit"],
          ...r.buys.map((b) => [b.n, b.title, b.group, date(b.at), eur(b.paid), eur(b.value), eur(b.predicted)])] },
      { name: "Sales", head: 0, widths: [7, 36, 13, 13, 13, 9, 10, 10, 10, 8, 16, 16],
        rows: [["#", "Item", "Category", "Bought", "Sold", "Days", "Paid", "Sold for", "Profit", "ROI", "Predicted profit", "Off vs predicted price"],
          ...r.sales.map((s) => [s.n, s.title, s.group, date(s.bought), date(s.at), s.days, eur(s.paid), eur(s.sold), eur(s.profit),
            pct(s.roi), eur(s.predicted), pct(s.price_off)]),
          ...(r.sales.length ? [["Total", "", "", "", "", "", eur(r.sales.reduce((a, s) => a + s.paid, 0)),
            eur(r.sales.reduce((a, s) => a + s.sold, 0)), eur(r.sales.reduce((a, s) => a + s.profit, 0))]] : [])] },
      { name: "Stock", head: 0, widths: [7, 40, 13, 9, 13, 10, 10, 12, 14],
        rows: [["#", "Item", "Category", "Status", "Bought", "Days held", "Paid", "Listed at", "Market value"],
          ...r.stock.map((s) => [s.n, s.title, s.group, s.status, date(s.bought), s.days, eur(s.paid), eur(s.list_price), eur(s.value)]),
          ...(r.stock.length ? [["Total", "", "", "", "", "", eur(r.in_stock)]] : [])] },
      { name: "AI", head: 2, widths: [36, 14, 50],
        rows: [[{ v: "The AI's YES/NO on new deals", s: "bold" }], [],
          ["What", "Count"], ["Deals checked", r.ai.checked], ["✅ YES", r.ai.yes], ["❌ NO", r.ai.no],
          ["❌ NO, moved to Rejected", r.ai.rejected], ["❌ NO sent back (↩️ Not a NO)", r.ai.overruled],
          ["Sent back, as a share of NOs", pct(r.ai.no ? r.ai.overruled / r.ai.no : null)], ["AI cost", eur(r.ai.cost)], [],
          [{ v: "NOs sent back", s: "bold" }], ["#", "Item", "The AI's reason"],
          ...r.ai.overruled_list.map((x) => [x.n, x.title, { v: x.why, s: "wrap" }])] },
    ];
  }

  /** Builds the report; with `save`, keeps it (private storage) and sends it. */
  async make(kind, { save = true } = {}) {
    const bot = this.bot;
    const { from, to } = period(kind, bot.now);
    const r = await this.data(kind, from, to);
    const sug = await this.suggest(r);
    const bytes = workbook(this.sheets(r, sug));
    const t = r.stats.totals;
    const name = `flipfinder-${kind === "week" ? "week" : "month"}-${kind === "week" ? to : from.slice(0, 7)}.xlsx`;
    const caption = `📊 <b>${kind === "week" ? "Weekly" : "Monthly"} report</b> · ${nice(from)} to ${nice(to)}\n` +
      `${t.sent} deals (✅ ${t.yes} · ❌ ${t.no}) · bought ${t.bought} · sold ${t.sold} · profit ${euro(t.profit)}` +
      (t.roi !== null ? ` (ROI ${t.roi}%)` : "") + ` · in stock ${euro(r.in_stock)} · AI €${t.ai.toFixed(2)}`;
    const out = { name, caption, bytes, suggestions: sug, data: r };
    if (!save) return out;
    const id = `${kind}-${kind === "week" ? to : from.slice(0, 7)}`;
    await bot.store.put(`report_file:${id}`, toBase64(bytes));
    const list = (await bot.store.get("reports", [])).filter((x) => x.id !== id);
    list.push({ id, kind, from, to, name, at: bot.now, suggestions: sug.items, note: sug.note });
    await bot.store.put("reports", list.slice(-KEEP));
    await this.send(id, out);
    return { ...out, id };
  }

  /** To the owner in private (with the suggestions and their ✅ Apply buttons), and pinned in Summary. */
  async send(id, out) {
    const bot = this.bot;
    const tg = bot.tg;
    await tg.sendDocument(String(bot.ownerId), out.name, out.bytes, { caption: out.caption, type: XLSX_TYPE, html: true });
    await bot.reply(String(bot.ownerId), this.suggestionText(out.suggestions), this.buttons(id, out.suggestions.items)?.inline_keyboard);
    const group = tg.groups[0];
    if (!group) return;
    const sent = await tg.sendDocument(group, out.name, out.bytes, { caption: out.caption, type: XLSX_TYPE, html: true, topic: "summary" });
    if (!sent) return;
    const old = await bot.store.get("report_pin");
    if (old) await tg.call("unpinChatMessage", { chat_id: old.chat, message_id: old.id });
    if (await tg.call("pinChatMessage", { chat_id: group, message_id: sent.message_id, disable_notification: true })) {
      await bot.store.put("report_pin", { chat: group, id: sent.message_id });
    }
  }

  suggestionText(sug) {
    const lines = ["💡 <b>Suggestions</b>"];
    if (sug.note) lines.push(esc(sug.note, false));
    sug.items.forEach((x, i) => lines.push(`${i + 1}. ${esc(x.text, false)}` +
      (x.action ? `\n   ✅ Apply: ${esc(actionText(x.action), false)}` : "\n   (nothing to apply automatically)")));
    if (sug.items.some((x) => x.action)) lines.push("<i>Nothing changes until you tap ✅ Apply.</i>");
    return lines.join("\n");
  }

  buttons(id, items) {
    const rows = items.map((x, i) => (x.action && !x.applied ? [{ text: `✅ Apply ${i + 1}: ${actionText(x.action)}`.slice(0, 60),
      callback_data: `sug:${id}:${i}` }] : null)).filter(Boolean);
    return rows.length ? { inline_keyboard: rows } : undefined;
  }

  /** ✅ Apply (owner only): the suggestion's action, once. */
  async apply(id, i, user) {
    const bot = this.bot;
    if (user !== bot.ownerId) return "Only the owner can apply suggestions";
    const list = await bot.store.get("reports", []);
    const rep = list.find((x) => x.id === id);
    const item = rep?.suggestions?.[i];
    if (!item?.action) return "That suggestion is gone";
    if (item.applied) return "Already applied";
    const a = item.action;
    const s = bot.settings;
    if (a.kind === "set") s.rules = { ...(s.rules || {}), [a.name]: a.value };
    else if (a.kind === "music") s.music_rules = { ...(s.music_rules || {}), [a.name]: a.value };
    else {
      const was = s.value_adjust?.[a.target] ?? 1;
      s.value_adjust = { ...(s.value_adjust || {}), [a.target]: Math.round(was * (1 + a.change / 100) * 1000) / 1000 };
    }
    bot.changed = true;
    item.applied = { by: user, at: bot.now };
    await bot.store.put("reports", list);
    return `✅ Applied: ${actionText(a)} (from the next scan)`;
  }

  /** /reports: the latest ones, each a button that sends the file again. */
  async list() {
    return (await this.bot.store.get("reports", [])).slice().reverse().slice(0, 12);
  }

  async resend(id, chat) {
    const rep = (await this.bot.store.get("reports", [])).find((x) => x.id === id);
    const b64 = rep && (await this.bot.store.get(`report_file:${id}`));
    if (!b64) return false;
    return Boolean(await this.bot.tg.sendDocument(String(chat), rep.name, fromBase64(b64), { type: XLSX_TYPE }));
  }
}
