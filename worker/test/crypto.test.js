import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, MARCO, GROUP } from "./helpers.js";
import { runCryptoJob } from "../src/app.js";
import { DISCLAIMER, digest, headlines, parseRss } from "../src/crypto.js";

const at = (h, m = 7) => Date.UTC(2026, 9, 7, h - 2, m) / 1000;   // Italy time in October (UTC+2)

const rss = (items) => `<?xml version="1.0"?><rss><channel><title>x</title>${items.map(([title, link, date]) =>
  `<item><title><![CDATA[${title}]]></title><link>${link}</link><pubDate>${date}</pubDate></item>`).join("")}</channel></rss>`;

/** Routes CoinGecko and the RSS feeds to fixtures, everything else to the fake Telegram. */
function world(t, { prices = {}, week = {}, feedsDown = false } = {}) {
  const calls = [];
  const json = (data, ok = true) => new Response(JSON.stringify(data), { status: ok ? 200 : 429 });
  t.fetch = async (url, init) => {
    url = String(url);
    if (url.includes("coingecko.com")) {
      calls.push(url.replace(/^https:\/\/api\.coingecko\.com\/api\/v3/, ""));
      if (url.includes("/coins/markets")) {
        const ids = decodeURIComponent(url.match(/ids=([^&]+)/)[1]).split(",");
        return json(ids.filter((id) => prices[id]).map((id) => ({ id, current_price: prices[id][0],
          price_change_percentage_24h: prices[id][1], total_volume: prices[id][2] })));
      }
      const chart = url.match(/\/coins\/([^/]+)\/market_chart/);
      if (chart) return json({ total_volumes: [[1, week[chart[1]] ?? 100], [2, week[chart[1]] ?? 100]] });
      if (url.includes("/search/trending")) {
        return json({ coins: ["Pepe:pepe", "Sui:sui", "Bonk:bonk", "Kaspa:kas", "Toncoin:ton", "Extra:x"]
          .map((s) => ({ item: { name: s.split(":")[0], symbol: s.split(":")[1] } })) });
      }
      if (url.includes("/search?query=")) {
        const q = decodeURIComponent(url.split("query=")[1]);
        return json({ coins: q === "link" ? [{ id: "link-fake", symbol: "link", name: "Fake Link", market_cap_rank: 900 },
          { id: "chainlink", symbol: "link", name: "Chainlink", market_cap_rank: 15 }] : [] });
      }
      return json({}, false);
    }
    if (url.includes("coindesk.com") || url.includes("cointelegraph.com")) {
      calls.push(url);
      if (feedsDown && url.includes("coindesk")) return new Response("down", { status: 503 });
      return new Response(url.includes("coindesk") ? rss([
        ["Bitcoin climbs &amp; holds", "https://www.coindesk.com/a", "Wed, 07 Oct 2026 14:00:00 GMT"],
        ["ETH upgrade date set", "https://www.coindesk.com/b", "Wed, 07 Oct 2026 12:00:00 GMT"],
        ["Old news", "https://www.coindesk.com/c", "Mon, 05 Oct 2026 12:00:00 GMT"],
      ]) : rss([
        ["Bitcoin climbs & holds!", "https://cointelegraph.com/dup", "Wed, 07 Oct 2026 13:00:00 GMT"],
        ["Solana fees drop", "https://cointelegraph.com/s", "Wed, 07 Oct 2026 15:00:00 GMT"],
        ["Regulators meet", "https://cointelegraph.com/r", "Wed, 07 Oct 2026 11:00:00 GMT"],
        ["Miners sell", "https://cointelegraph.com/m", "Wed, 07 Oct 2026 10:00:00 GMT"],
      ]));
    }
    return t.tg.fetch(url, init);
  };
  t.gecko = calls;
  return t;
}

const run = (t, now) => runCryptoJob(t.env, { fetchFn: t.fetch, now });
const inCrypto = (t) => t.tg.sent().filter((p) => String(p.chat_id) === GROUP && p.message_thread_id === 55);
const topics = { topics: { crypto: 55, summary: 44 }, allowed_users: [MARCO] };

test("RSS: titles, links and dates, CDATA and entities", () => {
  const items = parseRss(rss([["A &amp; B", "https://x.test/1", "Wed, 07 Oct 2026 14:00:00 GMT"], ["no link", "", ""]]), "Feed");
  assert.deepEqual(items, [{ title: "A & B", link: "https://x.test/1", at: Date.UTC(2026, 9, 7, 14) / 1000, source: "Feed" }]);
});

test("headlines: newest first from both feeds, duplicates once, five at most, a feed down is fine", async () => {
  const t = world(await setup());
  const h = await headlines(t.fetch);
  assert.deepEqual(h.map((x) => x.title), ["Solana fees drop", "Bitcoin climbs & holds", "ETH upgrade date set",
    "Regulators meet", "Miners sell"]);
  const t2 = world(await setup(), { feedsDown: true });
  assert.equal((await headlines(t2.fetch)).length, 4);
});

test("nothing is posted until the Crypto topic exists", async () => {
  const t = world(await setup({ settings: { topics: { summary: 44 } } }), { prices: { bitcoin: [50000, 15, 900] } });
  const r = await run(t, at(18));
  assert.match(r.skipped, /no Crypto topic/);
  assert.equal(t.tg.calls.length, 0);
  assert.equal(t.gecko.length, 0);
});

test("the 18:00 digest: prices, volume vs normal, headlines, trending, the disclaimer; once a day", async () => {
  const t = world(await setup({ settings: topics }), {
    prices: { bitcoin: [58123.4, 2.14, 230], ethereum: [2401.5, -1.3, 100], solana: [131.2, 0.4, 90] },
    week: { bitcoin: 100, ethereum: 100, solana: 100 } });
  await run(t, at(17));
  assert.equal(inCrypto(t).length, 0);                                     // not before 18:00
  await run(t, at(18));
  const [d] = inCrypto(t);
  assert.ok(d.text.startsWith("🪙 <b>Crypto · Wed 07 Oct</b>"));
  assert.match(d.text, /BTC €58,123 ▲2\.1% · volume 2\.3× normal/);
  assert.match(d.text, /ETH €2,402 ▼1\.3% · volume 1\.0× normal/);       // whole euros from €100
  assert.match(d.text, /1\. <a href="https:\/\/cointelegraph.com\/s">Solana fees drop<\/a> \(Cointelegraph\)/);
  assert.match(d.text, /Pepe \(PEPE\) · Sui \(SUI\) · Bonk \(BONK\) · Kaspa \(KAS\) · Toncoin \(TON\)\n/);
  assert.ok(!d.text.includes("Extra"));
  assert.match(d.text, /Trending = lots of searches, not a reason to buy\./);
  assert.ok(d.text.endsWith(`<i>${DISCLAIMER}</i>`));
  assert.equal(t.tg.sent().filter((p) => String(p.chat_id) !== GROUP).length, 0);   // group topic only
  await run(t, at(19));
  assert.equal(inCrypto(t).length, 1);                                     // once a day
  assert.equal(t.gecko.filter((u) => u.includes("market_chart")).length, 3);   // 7-day volume: once a day per coin
});

test("alerts: ±10% moves and 3× volume, each at most every 12 hours per coin", async () => {
  const t = world(await setup({ settings: topics }), {
    prices: { bitcoin: [50000, -12.4, 100], ethereum: [2000, 3, 350], solana: [120, 9.9, 290] },
    week: { bitcoin: 100, ethereum: 100, solana: 100 } });
  await run(t, at(9));
  const texts = inCrypto(t).map((p) => p.text);
  assert.equal(texts.length, 2);
  assert.match(texts[0], /🚨 <b>BTC ▼12\.4% in 24 h<\/b> · now €50,000/);
  assert.match(texts[1], /📈 <b>ETH trading 3\.5× its normal volume<\/b>/);
  assert.ok(texts.every((x) => x.includes(DISCLAIMER)));
  await run(t, at(15));
  assert.equal(inCrypto(t).length, 2);                                     // 6 h later: quiet
  await run(t, at(22));
  assert.equal(inCrypto(t).filter((p) => p.text.startsWith("🚨") || p.text.startsWith("📈")).length, 4);   // 13 h: again
});

test("CoinGecko down: no alerts, the digest still has the news", async () => {
  const t = world(await setup({ settings: topics }));
  t.fetch = ((orig) => async (url, init) => (String(url).includes("coingecko") ? new Response("busy", { status: 429 }) : orig(url, init)))(t.fetch);
  await run(t, at(18));
  const [d] = inCrypto(t);
  assert.match(d.text, /BTC: no price right now/);
  assert.match(d.text, /Solana fees drop/);
  assert.ok(!d.text.includes("Trending on CoinGecko"));
});

test("/watch and /unwatch: any allowed member, checked against CoinGecko", async () => {
  const t = world(await setup({ settings: topics }));
  const send = async (text, user) => {
    const { handleRequest } = await import("../src/app.js");
    const u = msg(text, { user });
    await handleRequest(new Request("https://w/telegram", { method: "POST",
      headers: { "x-telegram-bot-api-secret-token": "hook-secret" }, body: JSON.stringify(u) }), t.env, { fetchFn: t.fetch, now: at(10) });
  };
  await send("/watch", MARCO);
  await send("/watch link", MARCO);
  await send("/watch link", MARCO);
  await send("/watch nosuchcoin", MARCO);
  await send("/unwatch eth", MARCO);
  await send("/unwatch doge", MARCO);
  await send("/watch xrp", 4242);                                          // not allowed: ignored
  const r = t.tg.texts();
  assert.match(r[0], /watchlist: BTC, ETH, SOL/);
  assert.match(r[1], /Watching Chainlink \(LINK\)\. Watchlist: BTC, ETH, SOL, LINK/);   // the big one, not the copycat
  assert.match(r[2], /already on the watchlist/);
  assert.match(r[3], /CoinGecko doesn't know "nosuchcoin"/);
  assert.match(r[4], /Stopped watching ETH\. Watchlist: BTC, SOL, LINK/);
  assert.match(r[5], /isn't on the watchlist/);
  assert.equal(r.length, 6);
  const s = await t.settings();
  assert.deepEqual(s.crypto_watch.map((c) => c.id), ["bitcoin", "solana", "chainlink"]);
  assert.equal(await t.store.get("pot"), null);                            // never touches money
});

test("the digest itself always ends with the disclaimer", () => {
  const text = digest({ coins: [], prices: null, avgs: {}, news: [], trend: [], now: at(18) });
  assert.ok(text.includes("No headlines right now") && text.endsWith(`<i>${DISCLAIMER}</i>`));
});
