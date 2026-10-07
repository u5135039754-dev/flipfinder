// The Crypto topic: read-only news and prices. Never touches the pot, deals or settings.
// Runs on its own hourly cron (CRYPTO_CRON): price/volume alerts every hour, a digest at 18:00
// Italy time. Free sources: CoinGecko's public API and the CoinDesk / Cointelegraph RSS feeds.

import { esc, num, rome } from "./util.js";

export const CRYPTO_CRON = "7 * * * *";
export const DISCLAIMER = "News only, not financial advice.";
export const DEFAULT_WATCH = [
  { id: "bitcoin", symbol: "BTC", name: "Bitcoin" },
  { id: "ethereum", symbol: "ETH", name: "Ethereum" },
  { id: "solana", symbol: "SOL", name: "Solana" },
];
export const MAX_WATCH = 10;
export const DIGEST_HOUR = 18;
export const MOVE_ALERT = 10;          // ±% in 24 h
export const VOLUME_ALERT = 3;         // × the 7-day average
export const ALERT_EVERY = 12 * 3600;  // per coin and kind
const GECKO = "https://api.coingecko.com/api/v3";
export const FEEDS = [
  ["CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"],
  ["Cointelegraph", "https://cointelegraph.com/rss"],
];

export function watchlist(settings) {
  return settings.crypto_watch?.length ? settings.crypto_watch : DEFAULT_WATCH;
}

async function getJson(fetchFn, url, apiKey) {
  try {
    const r = await fetchFn(url, { headers: { accept: "application/json", "user-agent": "flipFinder (personal)",
      ...(apiKey ? { "x-cg-demo-api-key": apiKey } : {}) } });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

/** Prices in EUR, 24 h change and 24 h volume for the watchlist: {id: {price, change, volume}}. */
export async function markets(fetchFn, coins, apiKey) {
  const ids = coins.map((c) => c.id).join(",");
  const data = await getJson(fetchFn, `${GECKO}/coins/markets?vs_currency=eur&ids=${encodeURIComponent(ids)}` +
    "&price_change_percentage=24h", apiKey);
  if (!Array.isArray(data)) return null;
  return Object.fromEntries(data.map((m) => [m.id, { price: m.current_price, change: m.price_change_percentage_24h,
    volume: m.total_volume }]));
}

/** "Normal" 24 h volume: the average of the rolling 24 h volume over the last 7 days. */
export async function weekVolume(fetchFn, id, apiKey) {
  const data = await getJson(fetchFn, `${GECKO}/coins/${encodeURIComponent(id)}/market_chart?vs_currency=eur&days=7`, apiKey);
  const vols = (data?.total_volumes || []).map((p) => p[1]).filter((v) => v > 0);
  return vols.length ? vols.reduce((s, v) => s + v, 0) / vols.length : null;
}

export async function trending(fetchFn, apiKey) {
  const data = await getJson(fetchFn, `${GECKO}/search/trending`, apiKey);
  return (data?.coins || []).slice(0, 5).map((c) => ({ name: c.item?.name || "", symbol: (c.item?.symbol || "").toUpperCase() }))
    .filter((c) => c.name);
}

/** A coin by symbol, id or name; the biggest one when several share a symbol. */
export async function findCoin(fetchFn, text, apiKey) {
  const q = text.trim().toLowerCase();
  const data = await getJson(fetchFn, `${GECKO}/search?query=${encodeURIComponent(q)}`, apiKey);
  const coins = data?.coins || [];
  const rank = (c) => c.market_cap_rank ?? 1e9;
  const exact = coins.filter((c) => c.symbol?.toLowerCase() === q || c.id === q || c.name?.toLowerCase() === q)
    .sort((a, b) => rank(a) - rank(b));
  const c = exact[0];
  return c ? { id: c.id, symbol: c.symbol.toUpperCase(), name: c.name } : null;
}

// --- news

const decode = (s) => s.replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1").replace(/<[^>]+>/g, "")
  .replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"')
  .replace(/&#0?39;|&apos;|&#x27;/g, "'").replace(/&#(\d+);/g, (_, n) => String.fromCharCode(Number(n))).trim();

export function parseRss(xml, source) {
  const items = [];
  for (const m of xml.matchAll(/<item\b[\s\S]*?<\/item>/g)) {
    const tag = (t) => decode((m[0].match(new RegExp(`<${t}\\b[^>]*>([\\s\\S]*?)</${t}>`)) || [])[1] || "");
    const title = tag("title");
    const link = tag("link") || tag("guid");
    const at = Date.parse(tag("pubDate")) / 1000 || 0;
    if (title && /^https?:\/\//.test(link)) items.push({ title, link, at, source });
  }
  return items;
}

export async function headlines(fetchFn, n = 5) {
  const all = [];
  for (const [source, url] of FEEDS) {
    try {
      const r = await fetchFn(url, { headers: { "user-agent": "flipFinder (personal RSS reader)" } });
      if (r.ok) all.push(...parseRss(await r.text(), source));
    } catch { /* one feed down: the other still counts */ }
  }
  const seen = new Set();
  return all.sort((a, b) => b.at - a.at).filter((h) => {
    const k = h.title.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim().slice(0, 60);
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  }).slice(0, n);
}

// --- messages

const pct = (x) => `${x >= 0 ? "▲" : "▼"}${Math.abs(x).toFixed(1)}%`;
const eur = (x) => (x >= 100 ? `€${num(x, 0)}` : x >= 1 ? `€${num(x, 2)}` : `€${Number(x.toPrecision(3))}`);

export function volumeText(volume, avg) {
  return avg ? `volume ${(volume / avg).toFixed(1)}× normal` : "";
}

export function digest({ coins, prices, avgs, news, trend, now }) {
  const t = rome(now);
  const lines = [`🪙 <b>Crypto · ${t.weekday} ${t.day} ${t.month}</b>`, "", "<b>Prices</b> (EUR, last 24 h)"];
  for (const c of coins) {
    const p = prices?.[c.id];
    if (!p || p.price == null) {
      lines.push(`${esc(c.symbol)}: no price right now`);
      continue;
    }
    const vol = volumeText(p.volume, avgs?.[c.id]?.avg);
    lines.push(`${esc(c.symbol)} ${eur(p.price)} ${p.change != null ? pct(p.change) : ""}${vol ? ` · ${vol}` : ""}`);
  }
  lines.push("", "<b>Headlines</b>");
  if (news.length) news.forEach((h, i) => lines.push(`${i + 1}. <a href="${esc(h.link)}">${esc(h.title)}</a> (${h.source})`));
  else lines.push("No headlines right now");
  if (trend.length) {
    lines.push("", "<b>Trending on CoinGecko</b>", trend.map((c) => `${esc(c.name)} (${esc(c.symbol)})`).join(" · "),
      "<i>Trending = lots of searches, not a reason to buy.</i>");
  }
  lines.push("", `<i>${DISCLAIMER}</i>`);
  return lines.join("\n");
}

/**
 * The hourly crypto job: alerts for big moves (±10 % price, 3× volume; each at most every 12 h
 * per coin) and, once a day from 18:00 Italy time, the digest. Posts only in the Crypto topic.
 */
export async function runCrypto({ store, tg, settings, now, fetchFn, apiKey }) {
  const groups = tg.groups;
  if (!settings.topics?.crypto || !groups.length) return { skipped: "no Crypto topic yet (send /topic crypto in it)" };
  const coins = watchlist(settings);
  const state = await store.get("crypto", { avgs: {}, alerts: {}, last_digest: "" });
  const prices = await markets(fetchFn, coins, apiKey);
  // the 7-day volume average changes slowly: refreshed once a day per coin
  for (const c of coins) {
    const a = state.avgs[c.id];
    if (!a || now - a.at > 20 * 3600) {
      const avg = await weekVolume(fetchFn, c.id, apiKey);
      if (avg) state.avgs[c.id] = { avg, at: now };
    }
  }
  const send = async (text) => {
    let ok = false;
    for (const g of groups) ok = (await tg.sendTo(g, text, { topic: "crypto" })) !== null || ok;
    return ok;
  };
  const sent = [];
  for (const c of coins) {
    const p = prices?.[c.id];
    if (!p) continue;
    const last = (state.alerts[c.id] ??= {});
    if (p.change != null && Math.abs(p.change) >= MOVE_ALERT && now - (last.move || 0) >= ALERT_EVERY) {
      if (await send(`🚨 <b>${esc(c.symbol)} ${pct(p.change)} in 24 h</b> · now ${eur(p.price)}\n<i>${DISCLAIMER}</i>`)) {
        last.move = now;
        sent.push(`${c.symbol} move`);
      }
    }
    const avg = state.avgs[c.id]?.avg;
    if (avg && p.volume / avg >= VOLUME_ALERT && now - (last.volume || 0) >= ALERT_EVERY) {
      if (await send(`📈 <b>${esc(c.symbol)} trading ${(p.volume / avg).toFixed(1)}× its normal volume</b> ` +
        `(24 h vs the 7-day average) · ${eur(p.price)} ${p.change != null ? pct(p.change) : ""}\n<i>${DISCLAIMER}</i>`)) {
        last.volume = now;
        sent.push(`${c.symbol} volume`);
      }
    }
  }
  const t = rome(now);
  if (t.hour >= DIGEST_HOUR && state.last_digest !== t.date) {
    const text = digest({ coins, prices, avgs: state.avgs, news: await headlines(fetchFn), trend: await trending(fetchFn, apiKey), now });
    if (await send(text)) {
      state.last_digest = t.date;
      sent.push("digest");
    }
  }
  await store.put("crypto", state);
  return { sent, prices: Boolean(prices) };
}
