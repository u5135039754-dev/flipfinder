// Deal tracking: ✋ Claim -> 💸 Bought (price asked) -> 🏷 Listed -> ✅ Sold (price asked),
// the shared money pool, 👍/👎 and a ready-to-copy message for the seller.

import { esc, euro, rome } from "./util.js";

export const STATUS = { new: "", claimed: "✋ Claimed", bought: "💸 Bought", listed: "🏷 Listed", sold: "✅ Sold" };
export const NEXT = { claimed: ["b", "💸 Bought"], bought: ["l", "🏷 Listed"], listed: ["s", "✅ Sold"] };
export const SELLER_MESSAGE = (title) =>
  `Ciao! L'articolo "${title}" è ancora disponibile? Se sì, potresti mandarmi un breve video ` +
  "in cui si vede che funziona? Grazie mille!";

export function keyboard(key, d) {
  const votes = Object.values(d.votes || {});
  const up = votes.filter((v) => v === "up").length;
  const down = votes.filter((v) => v === "down").length;
  const rows = [];
  if (d.status === "new") rows.push([{ text: "✋ Claim", callback_data: `c:${key}` }]);
  else if (NEXT[d.status]) {
    const [code, label] = NEXT[d.status];
    rows.push([{ text: `${label} (${d.who})`, callback_data: `${code}:${key}` }]);
  }
  if (d.status !== "sold") {
    rows.push([
      { text: up ? `👍 ${up}` : "👍", callback_data: `up:${key}` },
      { text: down ? `👎 ${down}` : "👎", callback_data: `dn:${key}` },
      { text: "📩 Message seller", callback_data: `m:${key}` },
    ]);
  }
  return { inline_keyboard: rows };
}

export function statusLine(d) {
  if (d.status === "new") return "";
  let line = (d.n ? `#${d.n} · ` : "") + `${STATUS[d.status]} by ${esc(d.who)}`;
  if (d.paid !== undefined && d.paid !== null) line += ` · paid ${euro(d.paid)}`;
  if (d.status === "sold" && d.sold_for !== undefined && d.sold_for !== null) {
    line += ` · sold for ${euro(d.sold_for)} · profit ${euro(d.sold_for - (d.paid || 0))}`;
  }
  return line;
}

export function fullText(d) {
  const line = statusLine(d);
  return d.text + (line ? `\n\n<b>${line}</b>` : "");
}

/** Shared money: starting amount - what was paid + what things sold for. */
export function poolValue(pool, deals) {
  if (!pool) return null;
  let total = pool.start;
  for (const [, d] of deals) {
    if (["bought", "listed", "sold"].includes(d.status) && (d.bought_at || 0) >= pool.since) total -= d.paid || 0;
    if (d.status === "sold" && (d.sold_at || 0) >= pool.since) total += d.sold_for || 0;
  }
  return Math.round(total * 100) / 100;
}

export function stock(deals) {
  return deals.filter(([, d]) => ["bought", "listed"].includes(d.status)).map(([key, d]) => ({ ...d, key }));
}

export function profit(deals, now) {
  const out = { total: 0, month: 0, people: {}, sold: 0 };
  const thisMonth = rome(now).date.slice(0, 7);
  for (const [, d] of deals) {
    if (d.status !== "sold" || d.sold_for === undefined || d.sold_for === null) continue;
    const p = d.sold_for - (d.paid || 0);
    out.total += p;
    out.sold += 1;
    out.people[d.who] = (out.people[d.who] || 0) + p;
    if (rome(d.sold_at || 0).date.slice(0, 7) === thisMonth) out.month += p;
  }
  return out;
}

/** A deal by number ("12", "#12") or name (best title match, things in stock first). */
export function findDeal(deals, ref) {
  ref = ref.trim().replace(/^#+/, "");
  if (/^\d+$/.test(ref)) return deals.find(([, d]) => d.n === Number(ref)) || null;
  const words = ref.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return null;
  const score = ([, d]) => {
    const title = d.title.toLowerCase();
    return [words.every((w) => title.includes(w)), ["bought", "listed"].includes(d.status),
      words.filter((w) => title.includes(w)).length, d.sent || 0];
  };
  let best = null;
  let bestScore = null;
  for (const kd of deals) {
    const s = score(kd);
    if (!best || compare(s, bestScore) > 0) [best, bestScore] = [kd, s];
  }
  return best && words.some((w) => best[1].title.toLowerCase().includes(w)) ? best : null;
}

/** Python-style tuple comparison (booleans and numbers). */
export function compare(a, b) {
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const x = Number(a[i] ?? 0);
    const y = Number(b[i] ?? 0);
    if (x !== y) return x > y ? 1 : -1;
  }
  return 0;
}
