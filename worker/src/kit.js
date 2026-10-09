// The listing kit: when something is 💸 Bought (a repair deal: once it's 🔧 Repaired), the seller role
// gets in private everything needed to list it: prices (worked out here from the market value and our
// min profit), where to list it, an Italian title and description (the AI, same writing rules), a
// photo checklist and the shipping size. Buttons copy the text, rewrite it, or mark it listed (asking
// the price). Listed for 7 days without selling: a ~10% price drop, never below the lowest price.

import { esc, g } from "./util.js";
import { cleanTitle, nicePrice, pricesFor } from "./group.js";

export const DROP_DAYS = 7;
const COPY_MAX = 256;   // Telegram's limit for a copy-text button

/** Rounded up to a price people ask (€45, €120). */
function niceUp(x) {
  return x >= 20 ? Math.ceil(x / 5) * 5 : Math.ceil(x);
}

/**
 * The kit's prices: list at the market value, a quick-sale price, and the lowest price to accept,
 * which still keeps our min profit after what we paid and the repair part.
 */
export function kitPrices(d, minProfit) {
  const [value, suggested, quick] = pricesFor(d, d.value_now);
  const paid = d.paid ?? d.cost ?? 0;
  const part = d.repair?.cost ?? d.ai?.parts ?? 0;
  const lowest = niceUp(paid + part + minProfit);
  if (lowest > suggested) {
    // the market won't pay our min profit: list at the lowest that does, say so
    return { value: Math.round(value), price: lowest, quick: lowest, lowest, short: true };
  }
  return { value: Math.round(value), price: suggested, quick: Math.max(quick, lowest), lowest, short: false };
}

/** The next price after a week without a sale: ~10% less, never below the lowest. */
export function dropPrice(current, lowest) {
  return Math.max(lowest, nicePrice(current * 0.9));
}

/** The AI's kit ("PLATFORM: ...", "TITLE: ...", "DESCRIPTION:" lines up to END, "PHOTOS: ...", "PACKAGE: ..."). */
export function parseKit(raw) {
  const field = (name) => raw.match(new RegExp(`^\\s*${name}:\\s*(.+?)\\s*$`, "im"))?.[1] || "";
  const [platform, why = ""] = field("PLATFORM").split("|").map((x) => x.trim());
  const desc = raw.match(/^\s*DESCRIPTION:\s*([\s\S]*?)^\s*END\s*$/im)?.[1] ||
    raw.match(/^\s*DESCRIPTION:\s*([\s\S]*?)(?=^\s*PHOTOS:)/im)?.[1] || "";
  const [box, option = ""] = field("PACKAGE").split("|").map((x) => x.trim());
  return {
    platform: ["Vinted", "Subito", "eBay"].find((p) => p.toLowerCase() === (platform || "").toLowerCase()) || "Vinted",
    why: why.replace(/\.$/, ""),
    title: cleanTitle(field("TITLE").replace(/^["«]|["»]$/g, ""), 60),
    // we're the seller now: no "as in the photo" / "according to the seller" left from the old listing
    description: desc.split("\n").map((l) => l.replace(/,?\s*(come (si vede )?(nella|nelle|in) foto|(nella|nelle|dalle|dalla) foto|secondo il venditore)\b/gi, "")
      .replace(/\s+([.,])/g, "$1").trim()).filter(Boolean).join("\n"),
    photos: field("PHOTOS").split(/\s*;\s*/).filter(Boolean).slice(0, 8),
    shipping: [box, option].filter(Boolean).join(", "),
  };
}

/** A kit without the AI (off, out of credit or failing): prices, a plain title and a template to fill in. */
export function plainKit(d, city) {
  const title = cleanTitle(d.title || "", 60);
  return {
    platform: "Vinted", why: "", title, plain: true,
    description: [`${title}.`, `Condizioni: ${d.condition || "[indica le condizioni]"}. [Aggiungi segni d'uso o difetti, e cosa è incluso.]`,
      `Spedizione con imballaggio accurato${city ? `, oppure ritiro a mano a ${city}` : ""}.`].join("\n"),
    photos: [], shipping: "",
  };
}

/** The kit as a private message (HTML). */
export function kitText(d, kit) {
  const p = kit.prices;
  const lines = [`📦 <b>Listing kit · #${d.n ?? "?"}</b> ${esc((d.title || "").slice(0, 50))}`,
    `💶 List at <b>€${g(p.price)}</b> · lowest to accept <b>€${g(p.lowest)}</b>` + (p.short ? "" : ` · quick sale €${g(p.quick)}`)];
  if (p.short) lines.push(`⚠️ The market value (about €${g(p.value)}) is under what keeps our min profit: €${g(p.lowest)} is the floor.`);
  lines.push(`🛒 Where: <b>${esc(kit.platform)}</b>${kit.why ? `, ${esc(kit.why)}` : ""}`,
    `🏷 Title: <code>${esc(kit.title)}</code>`, `📝 Description:\n<code>${esc(kit.description)}</code>`);
  if (kit.photos.length) lines.push(`📸 Photos: ${kit.photos.map((x) => esc(x)).join(" · ")}`);
  if (kit.shipping) lines.push(`📦 Shipping: ${esc(kit.shipping)}`);
  if (d.list_price) lines.push(`🏷 Listed now at €${g(d.list_price)}`);
  if (kit.plain) lines.push("<i>The AI is off, so this is a template: fill in the [brackets].</i>");
  return lines.join("\n");
}

/** 📋 Copy title, 📋 Copy description, 🔄 Rewrite, 🏷 Mark as listed. */
export function kitButtons(key, kit) {
  const copy = (text, label, fallback) => (text.length <= COPY_MAX
    ? { text: label, copy_text: { text } } : { text: label, callback_data: fallback });
  return { inline_keyboard: [
    [copy(kit.title, "📋 Copy title", `kt:${key}`), copy(kit.description, "📋 Copy description", `kd:${key}`)],
    [{ text: "🔄 Rewrite", callback_data: `kr:${key}` }, { text: "🏷 Mark as listed", callback_data: `kl:${key}` }],
  ] };
}

export class Kits {
  constructor(bot) {
    this.bot = bot;
  }

  /** Who gets the kit: the seller role; nobody has it yet: whoever bought it, else the owner. */
  recipients(d) {
    const sellers = this.bot.team.withRole("seller");
    if (sellers.length) return sellers.map((m) => ({ id: m.id, name: m.name }));
    return [{ id: d.who_id || this.bot.ownerId, name: d.who || "the owner" }];
  }

  /** Makes (or remakes) the kit and keeps it on the deal (`save: false`: only returns it). */
  async make(key, d, user = "auto", { save = true } = {}) {
    const ai = this.bot.ai;
    const prices = kitPrices(d, await ai.minProfit(d));
    const city = (await this.bot.store.get("area"))?.city || "";
    let kit = null;
    if (ai.config.enabled && !(await ai.blocked(user))) {
      await ai.count(user);
      const fixed = d.repaired_at ? `\nWe repaired it ourselves: ${d.repair?.part || d.ai?.part || "the faulty part"} replaced, it works now. Say so honestly.` : "";
      const task = `Task: listing kit (listing kit format). Pickup city: ${city || "none, shipping only"}.` +
        ` We'll list it at about €${prices.price}.${fixed}`;
      const raw = await ai.run([{ role: "user", content: await ai.dealContent(d, task) }], { maxLines: 20 });
      if (raw) kit = parseKit(raw);
      if (kit && (!kit.title || !kit.description)) kit = null;
    }
    kit ??= plainKit(d, city);
    kit.prices = prices;
    kit.at = this.bot.now;
    if (!save) return [d, kit];
    const latest = (await this.bot.store.deal(key)) || d;
    latest.kit = kit;
    await this.bot.store.saveDeal(key, latest);
    return [latest, kit];
  }

  /** Bought (or repaired): the kit to the seller(s) in private, a short note in the deal's thread. */
  async send(key, d) {
    const [latest, kit] = await this.make(key, d);
    const to = this.recipients(latest);
    const sent = [];
    for (const m of to) {
      const msg = await this.bot.tg.sendTo(String(m.id), kitText(latest, kit), { buttons: kitButtons(key, kit), preview: false });
      if (msg) sent.push(m.name);
    }
    await this.bot.postAbout(latest, sent.length ? `📦 Listing kit sent to ${sent.map((x) => esc(x)).join(", ")}`
      : `📦 Couldn't send the listing kit for #${latest.n ?? "?"} in private (has the seller started the bot?). /sell ${latest.n ?? ""} writes a listing.`);
    return sent;
  }

  /** A repair deal was bought: fix it first, the kit comes with 🔧 Repaired. */
  async repairFirst(d) {
    const to = this.recipients(d);
    const n = d.n ?? "?";
    for (const m of to) {
      await this.bot.team.dm(m.id, `🔧 #${n} ${esc((d.title || "").slice(0, 50))} is bought. It needs ` +
        `${esc(d.repair?.part || "a repair")} first: tap 🔧 Repaired on the deal when it's fixed and the listing kit follows.`);
    }
    await this.bot.postAbout(d, `🔧 #${n} is bought: repair it first, then tap 🔧 Repaired for the listing kit.`);
  }

  /** A week listed without a sale: a ~10% lower price to the seller(s), never below the lowest. */
  async drop(key, d) {
    const prices = d.kit?.prices || kitPrices(d, await this.bot.ai.minProfit(d));
    const current = d.list_price ?? prices.price;
    const next = dropPrice(current, prices.lowest);
    const n = d.n ?? "?";
    const days = Math.round((this.bot.now - (d.listed_at || this.bot.now)) / 86400);
    const text = next < current
      ? `🏷 #${n} ${esc((d.title || "").slice(0, 50))} has been listed for ${days} days at €${g(current)}. ` +
        `Try <b>€${g(next)}</b> (about 10% less). Lowest that keeps our profit: €${g(prices.lowest)}.`
      : `🏷 #${n} ${esc((d.title || "").slice(0, 50))} has been listed for ${days} days at €${g(current)}, already the lowest ` +
        "that keeps our min profit. Keep it there, or ask the owner before going lower.";
    const buttons = { inline_keyboard: [[{ text: "🏷 New price", callback_data: `kl:${key}` }]] };
    let ok = false;
    for (const m of this.recipients(d)) ok = (await this.bot.team.dm(m.id, text, buttons)) || ok;
    if (!ok) await this.bot.postAbout(d, text, buttons);
    return next;
  }
}
