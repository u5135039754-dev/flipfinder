// Group features: topics and their pinned intros, reminders, /sell listings, the weekly report.

import { DAY, esc, euro, num, g, rome } from "./util.js";

// which topic a search category posts in (topics learned with /topic)
export const TOPIC_FOR_GROUP = {
  Guitars: "guitars", Amps: "guitars", Pedals: "guitars", "Added from Telegram": "guitars",
  Audio: "electronics", Electronics: "electronics", Budget: "budget",
};
export const TOPIC_NAMES = { guitars: "Guitars", electronics: "Electronics", budget: "Budget", summary: "Summary",
  crypto: "Crypto", rules: "Rules", repairs: "Repairs" };

/** Which topic a deal goes to: repair deals to 🔧 Repairs once that topic is set up, else by category. */
export function dealTopic(d, topics = {}) {
  return d.repair && topics.repairs ? "repairs" : TOPIC_FOR_GROUP[d.group || ""];
}

// Pinned intro per topic ("general" = the main chat), posted when a topic is set with /topic
// and again with /intro; an intro that's already there is edited, not posted twice
export const INTROS = {
  guitars: [
    "🎸 <b>Guitars</b>",
    "Underpriced guitars, amps, pedals and soundbars land here.",
    "Tap ✋ Claim if you're on it → 💸 Bought → 🏷 Listed → ✅ Sold. First claim wins 🏃",
    "👍/👎 tells the bot if a deal was any good, 📩 gives you a ready Italian message for the seller.",
  ].join("\n"),
  electronics: [
    "📱 <b>Electronics</b>",
    "Phones, AirPods, iPads, consoles and GPUs land here.",
    "Before you pay: 🔋 battery health, 🔓 iCloud/account unlocked, 🔍 no cracks, 🎥 ask for a video of it working.",
    "No video, no deal 😉",
  ].join("\n"),
  budget: [
    "💰 <b>Budget</b>",
    "Cheap flips that fit our budget: pedals, calculators, cameras, DS, Kindles, controllers.",
    "/budget shows or changes the limit, /pot shows how much money we have.",
    "Small buys, quick flips 🔁",
  ].join("\n"),
  summary: [
    "📊 <b>Summary</b>",
    "Daily summary, weekly report (Sunday 20:00), pot updates and failure alerts land here.",
    "/status is the bot running · /stock what we own · /profit how we're doing · /pot our money",
  ].join("\n"),
  repairs: [
    "🔧 <b>Repairs</b>",
    "Damaged things we can fix ourselves that are still a deal after the part: price + part (+€5 tools) vs what it sells for working.",
    "🟢 easy · 🟡 medium · 🔴 hard (only when the profit is big). iPhones with a new screen/battery/camera sell ~15% lower (\"unknown part\").",
    "/repairs off stops them, /repairs on brings them back.",
  ].join("\n"),
  crypto: [
    "🪙 <b>Crypto</b>",
    "Read-only crypto news: a digest at 18:00 (headlines, watchlist prices and volume, trending coins) and an alert when a watched coin moves ±10% or trades 3× its usual volume.",
    "/watch shows or adds coins, /unwatch removes them. This never touches the pot.",
    "<i>News only, not financial advice.</i>",
  ].join("\n"),
  general: [
    "💸 <b>Welcome to FLIP MAFIA</b>",
    "This chat is just for talking. Deals go to their topics: Guitars, Electronics, Budget and Summary.",
    "/help lists everything the bot can do.",
  ].join("\n"),
};

/** Clickable mention that works without a @username. */
export function mention(name, userId) {
  return userId ? `<a href="tg://user?id=${userId}">${esc(name)}</a>` : esc(name);
}

// --- reminders

/** [kind, key] for: ping after 24 h claimed, release after 48 h, list nudge, price cut. */
export function dueReminders(deals, now) {
  const out = [];
  for (const [key, d] of deals) {
    if (d.status === "claimed") {
      const since = Math.max(d.claimed_at || 0, d.kept_at || 0);
      if (now - since >= 2 * DAY) out.push(["release", key]);
      else if (now - since >= DAY && (d.pinged_at || 0) < since) out.push(["ping", key]);
    } else if (d.status === "bought" && now - (d.bought_at ?? now) >= 3 * DAY && now - (d.nudged_at || 0) >= 3 * DAY) {
      out.push(["list", key]);
    } else if (d.status === "listed" && now - (d.listed_at ?? now) >= 7 * DAY && now - (d.cut_at || 0) >= 7 * DAY) {
      out.push(["cut", key]);
    }
  }
  return out;
}

/** Prices people actually ask: €75, €120, €19. */
export function nicePrice(x) {
  if (x >= 20) return Math.max(5, Math.round(x / 5) * 5);
  return Math.max(1, Math.round(x));
}

/** [market value used, suggested price, quick-sale price]. */
export function pricesFor(d, valueNow) {
  const value = valueNow || d.value || 0;
  const suggested = nicePrice(value);
  let quick = nicePrice(d.low ? Math.min(value * 0.85, d.low) : value * 0.85);
  if (quick >= suggested) quick = nicePrice(suggested * 0.85);
  return [value, suggested, quick];
}

// --- /sell

const EMOJI = /[^\p{L}\p{N}\p{M}_\s\-+.,'/&()|:%€]/gu;

export function cleanTitle(title, limit = 60) {
  const t = title.replace(EMOJI, " ").replace(/\s+/g, " ").replace(/^[ \-–|]+|[ \-–|]+$/g, "");
  if (t.length <= limit) return t;
  const cut = t.slice(0, limit + 1).replace(/ [^ ]*$/, "");
  return cut.slice(0, limit).replace(/[ \-–,|]+$/, "");
}

const PICKUP = {
  it: (city) => `Spedizione rapida con imballaggio accurato${city ? `, oppure ritiro a mano a ${city}` : ""}.`,
  en: (city) => `Fast shipping, carefully packed${city ? `, or pickup in ${city}` : ""}.`,
  uk: (city) => `Швидка доставка з надійним пакуванням${city ? ` або самовивіз (${city})` : ""}.`,
};
const TEMPLATES = {
  it: (t, c, p, q, pickup) => `<b>Titolo</b>\n<code>${t}</code>\n\n<b>Descrizione</b>\n<code>${t}.\n` +
    `Condizioni: ${c}. Testato e funzionante [conferma prima di pubblicare].\n` +
    "[Aggiungi eventuali segni d'uso o difetti, e cosa è incluso.]\n" +
    `${pickup}</code>\n\n💶 Prezzo consigliato: <b>€${g(p)}</b> · vendita veloce: <b>€${g(q)}</b>`,
  en: (t, c, p, q, pickup) => `<b>Title</b>\n<code>${t}</code>\n\n<b>Description</b>\n<code>${t}.\n` +
    `Condition: ${c}. Tested and working [confirm before posting].\n` +
    "[Add any signs of wear or faults, and what's included.]\n" +
    `${pickup}</code>\n\n💶 Suggested price: <b>€${g(p)}</b> · quick sale: <b>€${g(q)}</b>`,
  uk: (t, c, p, q, pickup) => `<b>Назва</b>\n<code>${t}</code>\n\n<b>Опис</b>\n<code>${t}.\n` +
    `Стан: ${c}. Перевірено, працює [підтвердіть перед публікацією].\n` +
    "[Додайте сліди використання чи дефекти, і що входить у комплект.]\n" +
    `${pickup}</code>\n\n💶 Рекомендована ціна: <b>€${g(p)}</b> · швидкий продаж: <b>€${g(q)}</b>`,
};
const CONDITION = {
  en: { "Nuovo con cartellino": "new with tags", "Nuovo senza cartellino": "new without tags",
    Ottime: "very good", Buone: "good", Discrete: "fair" },
  uk: { "Nuovo con cartellino": "новий з біркою", "Nuovo senza cartellino": "новий без бірки",
    Ottime: "дуже добрий", Buone: "добрий", Discrete: "задовільний" },
};

/** Ready-to-copy listing; `city` (from the private area settings) is where pickup happens. */
export function sellListing(d, valueNow, lang = "it", city = "") {
  const [value, price, quick] = pricesFor(d, valueNow);
  let cond = d.condition || "[indica le condizioni]";
  if (lang !== "it") cond = CONDITION[lang][cond] || cond;
  const title = cleanTitle(d.title || "");
  const body = TEMPLATES[lang](esc(title), esc(cond), price, quick, esc(PICKUP[lang](city)));
  const basis = `\n\n<i>Market value €${num(value, 0)} (${valueNow ? "current comparables" : "when the deal was found"})` +
    (d.paid !== undefined && d.paid !== null ? `, paid ${euro(d.paid)}` : "") + "</i>";
  return `🏷 <b>Listing for #${d.n ?? "?"}</b>\n\n` + body + basis;
}

// --- weekly report

export function weeklyDue(lastWeekly, now) {
  const t = rome(now);
  return t.weekday === "Sun" && t.hour >= 20 && lastWeekly !== t.date;
}

export function weeklyReport(deals, feedback, now) {
  const t = rome(now);
  const since = now - 7 * DAY;
  const all = deals.map(([, d]) => d);
  const found = all.filter((d) => (d.sent || 0) >= since).length;
  const claimed = all.filter((d) => (d.claimed_at || 0) >= since).length;
  const bought = all.filter((d) => (d.bought_at || 0) >= since).length;
  const sold = all.filter((d) => d.status === "sold" && (d.sold_at || 0) >= since);
  const people = {};
  for (const d of sold) people[d.who] = (people[d.who] || 0) + d.sold_for - (d.paid || 0);
  const lines = [`🗓 <b>flipFinder · week to ${t.weekday} ${t.day} ${t.month}</b>`,
    `Deals found: ${found} · claimed: ${claimed} · bought: ${bought} · sold: ${sold.length}`];
  const ranked = Object.entries(people).sort((a, b) => b[1] - a[1]);
  if (ranked.length) {
    lines.push("Profit: " + ranked.map(([w, p]) => `${esc(w)} ${euro(p)}`).join(" · ") +
      ` · total ${euro(ranked.reduce((s, [, p]) => s + p, 0))}`);
  } else {
    lines.push("Profit: nothing sold this week");
  }
  if (sold.length) {
    const gain = (d) => d.sold_for - (d.paid || 0);
    const best = sold.reduce((a, b) => (gain(b) > gain(a) ? b : a));
    lines.push(`🏆 Best flip: <a href="${esc(best.url)}">${esc(best.title.slice(0, 50))}</a> by ${esc(best.who)}: ` +
      `${euro(best.paid || 0)} → ${euro(best.sold_for)} (+${euro(gain(best))})`);
  }
  const byKey = Object.fromEntries(deals);
  const downs = {};
  for (const f of feedback) {
    if ((f.at || 0) < since) continue;
    const q = f.query || byKey[f.key]?.query || "(unknown search)";
    downs[q] = (downs[q] || 0) + 1;
  }
  const worst = Object.entries(downs).sort((a, b) => b[1] - a[1])[0];
  lines.push(worst ? `👎 Most down-voted search: <b>${esc(worst[0])}</b> (${worst[1]}×) – worth tuning`
    : "👎 No down-votes this week");
  return lines.join("\n");
}
