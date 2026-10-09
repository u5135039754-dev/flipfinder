// Deal tracking: ✋ Claim -> 💸 Bought (price asked) -> 🏷 Listed -> ✅ Sold (price asked),
// the shared money pool, 👍/👎 and a ready-to-copy message for the seller.

import { esc, euro, num, rome } from "./util.js";

export const STATUS = { new: "", claimed: "✋ Claimed", bought: "💸 Bought", listed: "🏷 Listed", sold: "✅ Sold" };
export const NEXT = { claimed: ["b", "🙋 Request buy"], bought: ["l", "🏷 Listed"], listed: ["s", "✅ Sold"] };
export const PLATFORMS = { vinted: "Vinted", ebay: "eBay", subito: "Subito" };
export const SELLER_MESSAGE = (title) =>
  `Ciao! L'articolo "${title}" è ancora disponibile? Se sì, potresti mandarmi un breve video ` +
  "in cui si vede che funziona? Grazie mille!";

/**
 * A deal's buttons: ✋ Claim (then the next step) and Open; with the AI on, ❓ Seller questions and
 * 🧠 Deep analysis; 👍/👎.
 */
export function keyboard(key, d, ai = false) {
  const votes = Object.values(d.votes || {});
  const up = votes.filter((v) => v === "up").length;
  const down = votes.filter((v) => v === "down").length;
  const rows = [];
  const first = [];
  if (d.status === "new") first.push({ text: "✋ Claim", callback_data: `c:${key}` });
  else if (d.status === "claimed" && d.request) {
    first.push({ text: `⏳ Waiting for OK: ${euro(d.request.amount)} (${d.request.by})`, callback_data: `wait:${key}` });
  } else if (d.status === "bought" && d.repair && !d.repaired_at) {
    first.push({ text: `🔧 Repaired (${d.who})`, callback_data: `rp:${key}` });   // a repair deal: fixed before it's listed
  } else if (NEXT[d.status]) {
    const [code, label] = NEXT[d.status];
    first.push({ text: `${label} (${d.who})`, callback_data: `${code}:${key}` });
  }
  if (["new", "claimed"].includes(d.status)) first.push({ text: "🚨 Buy now", callback_data: `bn:${key}` });
  if (first.length) rows.push(first);
  if (/^https:\/\//.test(d.url || "")) rows.push([{ text: `Open on ${PLATFORMS[d.source] || "the site"}`, url: d.url }]);
  if (d.status !== "sold") {
    if (ai) {
      rows.push([{ text: "❓ Seller questions", callback_data: `aq:${key}` }, { text: "🧠 Deep analysis", callback_data: `aid:${key}` }]);
    }
    rows.push([
      { text: up ? `👍 ${up}` : "👍", callback_data: `up:${key}` },
      { text: down ? `👎 ${down}` : "👎", callback_data: `dn:${key}` },
      ...(d.rejected ? [{ text: "↩️ Not a NO", callback_data: `unrej:${key}` }] : []),
    ]);
  }
  return { inline_keyboard: rows };
}

// --- reposts: the same seller putting the same thing up again (or twice) within a week
export const REPOST_DAYS = 7;

function titleWords(t) {
  return new Set(String(t || "").toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, " ").split(" ").filter(Boolean));
}

/** How alike two titles are: shared words over all words (1 = the same words). */
export function titleSimilarity(a, b) {
  const x = titleWords(a);
  const y = titleWords(b);
  if (!x.size || !y.size) return 0;
  let both = 0;
  for (const w of x) if (y.has(w)) both++;
  return both / (x.size + y.size - both);
}

/** Same seller: the same account (Vinted), else the same trust numbers for an established seller. */
export function sameSeller(a, b) {
  if (!a || !b || a.source !== b.source) return false;
  if (a.id || b.id) return Boolean(a.id && b.id && String(a.id) === String(b.id));
  return (a.reviews || 0) > 0 && a.reviews === b.reviews && a.stars === b.stars && (a.sold ?? null) === (b.sold ?? null);
}

/** The earlier deal this one repeats (same seller, very similar title, price within ~10%), or null. */
export function findRepost(record, recent, now) {
  const price = Number(record.item?.price ?? record.cost) || 0;
  for (const [key, d] of recent) {
    if (now - (d.sent || 0) > REPOST_DAYS * 86400) continue;
    if (!sameSeller(record.item?.seller, d.item?.seller)) continue;
    if (titleSimilarity(record.title, d.title) < 0.8) continue;
    const other = Number(d.item?.price ?? d.cost) || 0;
    if (Math.abs(price - other) > Math.max(5, 0.1 * Math.max(price, other))) continue;
    return [key, d];
  }
  return null;
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

/**
 * The deal message: title and price, the AI's ✅ YES / ❌ NO and main risk (once it answered), profit and
 * how fast it sells. The full breakdown (d.text, from the scanner) is behind 📊 Numbers. Deals from before
 * this layout keep their full text.
 */
/** The price the team sees in the deal message (buyer fee included); the AI gets the same. */
export function shownPrice(d) {
  const price = d.item?.total_price ?? d.item?.price ?? d.cost;
  if (price == null) return null;
  return d.short ? Math.round(price) : Number(price).toFixed(2);   // the full message shows cents
}

export function shortText(d) {
  const price = d.item?.total_price ?? d.item?.price ?? d.cost;
  const no = d.ai?.verdict === "no";
  const lines = [`${no ? "🔕" : d.repair ? "🔧" : "🔥"} <b>${esc(String(d.title || "").slice(0, 120), false)}</b> · ${eur(price)}`];
  if (d.ai?.lines) lines.push(...d.ai.lines.map((l) => esc(l, false)));
  const profit = d.ai?.profit ?? d.profit;
  const after = d.ai?.profit != null ? " after the part" : "";
  let money = profit != null && profit < 0 ? `💸 ${eur(-profit)} loss${after}` : `💰 ${eur(profit)} profit${after}`;
  const days = d.sell_days != null ? Math.max(1, Math.round(d.sell_days)) : null;
  if (days) money += ` · sells in ~${days} day${days === 1 ? "" : "s"}`;
  lines.push(money + (d.n ? ` · #${d.n}` : ""));
  return lines.join("\n");
}

/** Whole euros for the short lines: €177. */
function eur(x) {
  return x == null ? "€?" : `€${num(x, 0)}`;
}

/**
 * The deal message: the scanner's full text (every number), then the AI's two lines once it answered
 * ("🤖 ✅ YES ..." and "⚠️ ..."), then the status. A few deals from the short layout keep it.
 */
export function fullText(d) {
  const line = statusLine(d);
  let text = d.short ? shortText(d) : d.text;
  if (!d.short && d.ai?.lines) text += `\n\n🤖 ${esc(d.ai.lines[0], false)}\n${esc(d.ai.lines[1] || "", false)}`;
  return text + (line ? `\n\n<b>${line}</b>` : "");
}

export const CAPTION_LIMIT = 1024;
// lines a photo caption can do without, least useful first
const SPARE = ["↔️", "📊 By platform", "🎯", "🏪", "💬 Negotiable", "<i>", "🔍 Before buying", "📊 Demand", "🔋"];

/** A photo caption within Telegram's 1024 characters: spare lines go first, so the AI's lines at the end stay. */
export function fitCaption(text) {
  let lines = text.split("\n");
  for (const prefix of SPARE) {
    if (lines.join("\n").length <= CAPTION_LIMIT) break;
    lines = lines.filter((l) => !l.startsWith(prefix));
  }
  const out = lines.join("\n");
  return out.length <= CAPTION_LIMIT ? out : out.slice(0, CAPTION_LIMIT);
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
