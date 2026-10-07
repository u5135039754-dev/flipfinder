// Formatting helpers shared by the bot (same output as the Python version had).

export const DAY = 86400;

export function nowSeconds() {
  return Date.now() / 1000;
}

/** HTML-escape for Telegram messages (quotes too, unless quote = false). */
export function esc(s, quote = true) {
  let out = String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  if (quote) out = out.replace(/"/g, "&quot;").replace(/'/g, "&#x27;");
  return out;
}

/** 1234.5 -> "1,234.50" (digits = 2) or "1,235" (digits = 0). */
export function num(x, digits = 2) {
  return Number(x).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function euro(x) {
  return `€${num(x)}`;
}

/** Python's "{x:g}": 90 -> "90", 27.5 -> "27.5". */
export function g(x) {
  return String(Number(Number(x).toPrecision(6)));
}

/** "boss katana" 80 250 -> [query, lo, hi]; same rules and messages as before. */
export function queryAndRange(args) {
  if (args.length < 3) throw new UserError('I need a search and two prices, e.g. /setprice "boss katana" 80 250');
  const lo = parseNumber(args.at(-2));
  const hi = parseNumber(args.at(-1));
  if (lo === null || hi === null) {
    throw new UserError(`The prices must be numbers, I got '${args.at(-2)}' and '${args.at(-1)}'`);
  }
  if (lo < 0 || hi < 0) throw new UserError("Prices can't be negative");
  if (lo >= hi) throw new UserError(`The minimum (${g(lo)}) must be lower than the maximum (${g(hi)})`);
  return [args.slice(0, -2).join(" ").trim().toLowerCase(), lo, hi];
}

/** "€27,50" -> 27.5; null when it isn't a number. */
export function parseNumber(raw) {
  const s = String(raw).replace(/€/g, "").replace(/,/g, ".").trim();
  if (!/^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?$/i.test(s)) return null;
  return Number(s);
}

/** Shell-like split: '"boss katana" 80 250' -> ["boss katana", "80", "250"]. */
export function splitArgs(text) {
  text = text.replace(/[“”]/g, '"').replace(/[‘’]/g, "'");
  const out = [];
  let cur = null;
  let quote = null;
  for (const ch of text) {
    if (quote) {
      if (ch === quote) quote = null;
      else cur += ch;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
      cur = cur ?? "";
    } else if (/\s/.test(ch)) {
      if (cur !== null) out.push(cur);
      cur = null;
    } else {
      cur = (cur ?? "") + ch;
    }
  }
  if (quote) return text.split(/\s+/).filter(Boolean);   // unbalanced quote (e.g. d'angelico): plain words
  if (cur !== null) out.push(cur);
  return out;
}

/** An error the user should see ("⚠️ ..."). */
export class UserError extends Error {}

/** difflib.get_close_matches: up to n candidates whose similarity ratio is >= cutoff, best first. */
export function closeMatches(word, candidates, n = 3, cutoff = 0.5) {
  return candidates
    .map((c) => [ratio(word, c), c])
    .filter(([r]) => r >= cutoff)
    .sort((a, b) => b[0] - a[0])
    .slice(0, n)
    .map(([, c]) => c);
}

function ratio(a, b) {
  const matches = (a1, a2, b1, b2) => {
    let best = [a1, b1, 0];
    for (let i = a1; i < a2; i++) {
      for (let j = b1; j < b2; j++) {
        let k = 0;
        while (i + k < a2 && j + k < b2 && a[i + k] === b[j + k]) k++;
        if (k > best[2]) best = [i, j, k];
      }
    }
    const [i, j, k] = best;
    if (!k) return 0;
    return k + matches(a1, i, b1, j) + matches(i + k, a2, j + k, b2);
  };
  const total = a.length + b.length;
  return total ? (2 * matches(0, a.length, 0, b.length)) / total : 1;
}

// --- Italy time

const ROME = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/Rome", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", weekday: "short", hourCycle: "h23",
});

/** {date: "2026-10-11", hour, minute, weekday: "Sun", day: "11", month: "Oct"} in Italy. */
export function rome(ts) {
  const p = Object.fromEntries(ROME.formatToParts(new Date(ts * 1000)).map((x) => [x.type, x.value]));
  const monthName = new Date(ts * 1000).toLocaleString("en-GB", { timeZone: "Europe/Rome", month: "short" });
  return {
    date: `${p.year}-${p.month}-${p.day}`, hour: Number(p.hour), minute: Number(p.minute),
    weekday: p.weekday, day: p.day, month: monthName,
  };
}

/** No deal alerts 00:00-07:30 Italy time. */
export function inQuietHours(ts) {
  const t = rome(ts);
  return t.hour * 60 + t.minute < 7 * 60 + 30;
}
