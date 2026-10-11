// The Mini App's Searches section: searches per topic with their rules, ➕ add / edit / delete (owner and
// managers), 💡 suggestions from everyone else (to the owner with ✅ Add / ❌ Ignore), previews the scanner
// runs (it can load Vinted pages, the Worker can't), and a line in Summary for every change. /add and
// /remove write the same settings (added, removed), so commands and the app stay in sync.

import { CLOTHES_DEFAULTS, RULES } from "./searches.js";
import { TOPIC_FOR_GROUP, TOPIC_NAMES } from "./group.js";
import { DAY, UserError, esc, g, parseNumber } from "./util.js";

export const TOPIC_ICONS = { guitars: "🎸", electronics: "📱", budget: "💸", clothes: "👕", repairs: "🔧" };
const TOPIC_LABELS = { guitars: "Music" };   // "Borys added 'boss katana' to Music"
// the group a search added in a topic's card gets: its rules come with it
export const TOPIC_GROUP = { guitars: "Guitars", electronics: "Electronics", budget: "Budget", clothes: "Clothes" };
export const TOPIC_RULES = ["min_profit", "min_roi", "max_price"];
export const EXTRA_PLATFORMS = ["ebay", "subito", "mercatino"];   // Vinted is always on: market values come from it
const MUSIC_ONLY = ["mercatino"];   // Mercatino Musicale: music searches only
const PREVIEW_MAX_AGE = 3600;     // the scanner picks a preview up within a run (~5 min); after an hour it's dropped
const PREVIEWS_PER_PERSON = 3;

const words = (x) => (Array.isArray(x) ? x : String(x || "").split(","))
  .map((w) => String(w).trim().toLowerCase()).filter(Boolean);

export class Searches {
  constructor(bot) {
    this.bot = bot;
  }

  /** Deal topics in order: every topic a search category posts in (new ones appear by themselves), then 🔧 Repairs. */
  topicKeys() {
    return [...new Set([...Object.values(TOPIC_FOR_GROUP), "repairs"])];
  }

  label(key) {
    return TOPIC_LABELS[key] || TOPIC_NAMES[key] || key;
  }

  topicOf(s) {
    return TOPIC_FOR_GROUP[s.group || ""] || "guitars";
  }

  /** A topic's editable rules (null for 🔧 Repairs): each maps onto the rules the scanner already uses. */
  rules(view, key) {
    const main = view.rules;
    if (key === "guitars") return { min_profit: view.music_rules.min_profit, min_roi: view.music_rules.min_roi,
      max_price: view.music_rules.max_price ?? main.max_price, note: "Music gear's own rules" };
    if (key === "electronics") return { min_profit: main.min_profit, min_roi: main.min_roi, max_price: main.max_price,
      note: "The main rules: also anything without its own" };
    if (key === "budget") return { min_profit: view.budget_rules.min_profit, min_roi: view.budget_rules.min_roi,
      max_price: view.budget_setting, note: `Max price is the budget (in effect €${g(view.budget)}: never more than the pot)` };
    if (key === "clothes") return { min_profit: view.clothes_rules.min_profit, min_roi: view.clothes_rules.min_roi,
      max_price: view.clothes_rules.max_price ?? 200, note: "Plus the fake checks" };
    return null;
  }

  /** The app's Searches section: per topic its rules and searches (with deals found in the last 7 days). */
  async forApp(userId) {
    const view = await this.bot.view();
    const deals = await this.bot.allDeals();
    const week = {};
    for (const [, d] of deals) {
      if ((d.sent || 0) >= this.bot.now - 7 * DAY && d.query) week[d.query.toLowerCase()] = (week[d.query.toLowerCase()] || 0) + 1;
    }
    const topics = this.topicKeys().map((key) => ({
      key, name: this.label(key), icon: TOPIC_ICONS[key] || "📂", rules: this.rules(view, key), can_add: key in TOPIC_GROUP,
      ...(key === "repairs" ? { repairs: this.bot.settings.repairs !== false } : {}),
      searches: view.searches.filter((s) => key !== "repairs" && this.topicOf(s) === key).map((s) => ({
        query: s.query, price_from: s.price_from ?? null, price_to: s.price_to ?? null, max_price: s.max_price ?? null,
        enabled: s.enabled !== false,
        budget: Boolean(s.budget), clothes: Boolean(s.clothes), added: Boolean(s.added), week: week[s.query.toLowerCase()] || 0,
        platforms: ["vinted", ...(s.platforms ? s.platforms.filter((p) => EXTRA_PLATFORMS.includes(p))
          : s.clothes ? [] : [view.ebay && "ebay", view.subito && "subito", key === "guitars" && view.mercatino && "mercatino"].filter(Boolean))],
        must: s.must || [], exclude: s.exclude || [], size: s.size || "",
      })),
    }));
    return { can_edit: this.bot.team.isManager(userId), owner: userId === this.bot.ownerId, topics,
      platforms: { ebay: view.ebay, subito: view.subito, mercatino: view.mercatino } };
  }

  // --- checks
  manager(userId) {
    if (!this.bot.team.isManager(userId)) throw new UserError("Only the owner and managers change searches: 💡 Suggest it instead");
  }

  /** The form, checked: query, topic, prices, platforms, words, size. */
  clean(spec) {
    const query = String(spec.query || "").trim().replace(/\s+/g, " ").toLowerCase();
    if (query.length < 2 || query.length > 60) throw new UserError("What to search: 2 to 60 characters, e.g. boss katana");
    const topic = String(spec.topic || "");
    if (!(topic in TOPIC_GROUP)) throw new UserError("Pick a topic for the search");
    const price = (x, name) => {
      if (x === null || x === undefined || String(x).trim() === "") return null;
      const v = parseNumber(String(x));
      if (v === null || !(v >= 0 && v <= 10_000)) throw new UserError(`${name} must be a number of euros`);
      return v;
    };
    const from = price(spec.price_from, "Min price");
    const to = price(spec.price_to, "Max price");
    if (from !== null && to !== null && from >= to) throw new UserError("Min price must be under max price");
    const must = words(spec.must).slice(0, 10);
    const exclude = words(spec.exclude).slice(0, 20);
    const platforms = ["vinted", ...EXTRA_PLATFORMS.filter((p) => (spec.platforms || []).includes(p)
      && (topic === "guitars" || !MUSIC_ONLY.includes(p)))];
    const size = topic === "clothes" ? String(spec.size || "").trim().slice(0, 10) : "";
    return { query, topic, price_from: from, price_to: to, platforms, must, exclude, size };
  }

  extras(c) {
    return { platforms: c.platforms.filter((p) => p !== "vinted"), must: c.must, exclude: c.exclude, size: c.size };
  }

  // --- changes (owner and managers)
  /** Adds a search to a topic, or changes one (`old`: its current name). A line goes to Summary. */
  async save(userId, spec, old = "", { via = "", by = null } = {}) {
    this.manager(userId);
    const c = this.clean(spec);
    const view = await this.bot.view();
    const before = old ? view.searches.find((s) => s.query.toLowerCase() === String(old).toLowerCase()) : null;
    if (old && !before) throw new UserError(`No search called "${old}"`);
    if ((!before || before.query.toLowerCase() !== c.query) && view.searches.some((s) => s.query.toLowerCase() === c.query)) {
      throw new UserError(`There's already a search called "${c.query}"`);
    }
    const s = this.bot.settings;
    s.added ??= [];
    s.removed ??= [];
    const keepConfig = before && !before.added && before.query.toLowerCase() === c.query && this.topicOf(before) === c.topic;
    if (keepConfig) {
      // a config.yaml search: prices and extras go over it, it stays where it is
      s.prices = { ...(s.prices || {}), [c.query]: [c.price_from, c.price_to] };
      s.overrides = { ...(s.overrides || {}), [c.query]: this.extras(c) };
    } else {
      if (before) this.drop(before);
      s.added = [...s.added.filter((a) => a.query.toLowerCase() !== c.query),
        { query: c.query, price_from: c.price_from, price_to: c.price_to, group: TOPIC_GROUP[c.topic],
          ...(c.topic === "clothes" ? { clothes: true } : {}), ...this.extras(c) }];
      s.removed = s.removed.filter((q) => q.toLowerCase() !== c.query);
    }
    this.bot.changed = true;
    const who = esc(this.bot.team.nameOf(userId));
    const range = c.price_from !== null || c.price_to !== null ? ` (€${g(c.price_from ?? 0)}–${c.price_to !== null ? `€${g(c.price_to)}` : "no max"})` : "";
    const text = before
      ? `✏️ ${who} changed '${esc(c.query)}'${before.query.toLowerCase() !== c.query ? ` (was '${esc(before.query)}')` : ""} in ${this.label(c.topic)}${range}`
      : `➕ ${who} added '${esc(c.query)}' to ${this.label(c.topic)}${range}${by ? ` (suggested by ${esc(by)})` : ""}${via ? ` ${via}` : ""}`;
    await this.log(text);
    return before ? `Saved '${c.query}'` : `Added '${c.query}' to ${this.label(c.topic)}: checked from the next scan`;
  }

  /** A search out of the settings (an /add-ed one is dropped, a config.yaml one goes on the removed list). */
  drop(search) {
    const s = this.bot.settings;
    const q = search.query.toLowerCase();
    if (search.added) s.added = (s.added || []).filter((a) => a.query.toLowerCase() !== q);
    else s.removed = [...new Set([...(s.removed || []), q])].sort();
    for (const k of ["prices", "overrides"]) if (s[k]?.[q]) delete s[k][q];
    s.disabled = (s.disabled || []).filter((x) => x.toLowerCase() !== q);
  }

  async remove(userId, query) {
    this.manager(userId);
    const view = await this.bot.view();
    const search = view.searches.find((s) => s.query.toLowerCase() === String(query || "").toLowerCase());
    if (!search) throw new UserError(`No search called "${query}"`);
    this.drop(search);
    this.bot.changed = true;
    await this.log(`🗑 ${esc(this.bot.team.nameOf(userId))} removed '${esc(search.query)}' from ${this.label(this.topicOf(search))}`);
    return `Removed '${search.query}'`;
  }

  async toggle(userId, query) {
    this.manager(userId);
    const view = await this.bot.view();
    const search = view.searches.find((s) => s.query.toLowerCase() === String(query || "").toLowerCase());
    if (!search) throw new UserError("That search is gone");
    const dis = new Set(this.bot.settings.disabled || []);
    if (search.enabled) dis.add(search.query);
    else dis.delete(search.query);
    this.bot.settings.disabled = [...dis].sort();
    this.bot.changed = true;
    await this.log(`${search.enabled ? "⏸" : "▶️"} ${esc(this.bot.team.nameOf(userId))} turned '${esc(search.query)}' ` +
      `${search.enabled ? "off" : "on"} in ${this.label(this.topicOf(search))}`);
    return `${search.query}: ${search.enabled ? "off" : "on"}`;
  }

  /** A topic's min profit, min ROI or max price. */
  async setRule(userId, key, name, raw) {
    this.manager(userId);
    if (!(key in TOPIC_GROUP)) throw new UserError("That topic has no rules of its own");
    if (!TOPIC_RULES.includes(name)) throw new UserError(`Rules: ${TOPIC_RULES.join(", ")}`);
    const [, lo, hi] = RULES[name];
    const value = parseNumber(String(raw ?? "").replace(/[%€]/g, ""));
    if (value === null || !(value >= lo && value <= hi)) throw new UserError(`${name.replace("_", " ")} must be between ${lo} and ${hi}`);
    const s = this.bot.settings;
    if (key === "guitars") s.music_rules = { ...(s.music_rules || {}), [name]: value };
    else if (key === "electronics") {
      if (name === "min_roi" && value >= (await this.bot.view()).rules.max_roi) throw new UserError("min ROI must stay below max ROI");
      s.rules = { ...(s.rules || {}), [name]: value };
    } else if (key === "budget") {
      if (name === "max_price") s.budget = value;
      else s.budget_rules = { ...(s.budget_rules || {}), [name]: value };
    } else if (key === "clothes") s.clothes_rules = { ...(s.clothes_rules || {}), [name]: value };
    this.bot.changed = true;
    const shown = name === "min_roi" ? `${g(value)}%` : `€${g(value)}`;
    await this.log(`⚙️ ${esc(this.bot.team.nameOf(userId))} set ${this.label(key)} ${name.replace("_", " ").replace("roi", "ROI")} to ${shown}`);
    return `${this.label(key)}: ${name.replace("_", " ")} ${shown}`;
  }

  /** One line in the Summary topic for every change. */
  async log(text) {
    for (const gid of this.bot.tg.groups) await this.bot.tg.sendTo(gid, text, { topic: "summary" });
  }

  // --- 💡 suggestions (members without the right to change searches)
  async suggest(userId, spec) {
    const c = this.clean(spec);
    const view = await this.bot.view();
    if (view.searches.some((s) => s.query.toLowerCase() === c.query)) throw new UserError(`There's already a search called "${c.query}"`);
    const box = await this.bot.store.get("search_suggestions", { next: 1, items: {} });
    const id = box.next++;
    box.items[id] = { spec: c, by: userId, at: this.bot.now, status: "open" };
    for (const [k, x] of Object.entries(box.items)) if (this.bot.now - x.at > 30 * DAY) delete box.items[k];
    await this.bot.store.put("search_suggestions", box);
    const who = esc(this.bot.team.nameOf(userId));
    const extras = [c.price_from !== null || c.price_to !== null ? `€${g(c.price_from ?? 0)}–${c.price_to !== null ? `€${g(c.price_to)}` : "no max"}` : "",
      c.platforms.length > 1 ? c.platforms.join(" + ") : "", c.must.length ? `must: ${c.must.join(", ")}` : "",
      c.exclude.length ? `not: ${c.exclude.join(", ")}` : "", c.size ? `size ${c.size}` : ""].filter(Boolean);
    await this.bot.tg.sendTo(String(this.bot.ownerId), `💡 ${who} suggests a search for ${TOPIC_ICONS[c.topic]} ${this.label(c.topic)}: ` +
      `<b>${esc(c.query)}</b>${extras.length ? `\n${esc(extras.join(" · "))}` : ""}`,
      { buttons: { inline_keyboard: [[{ text: "✅ Add", callback_data: `sgy:${id}` }, { text: "❌ Ignore", callback_data: `sgn:${id}` }]] } });
    return `💡 Sent to the owner: '${c.query}'`;
  }

  /** The owner's ✅ Add / ❌ Ignore on a suggestion. */
  async answer(userId, id, add) {
    if (userId !== this.bot.ownerId) return "Only the owner";
    const box = await this.bot.store.get("search_suggestions", { next: 1, items: {} });
    const x = box.items[id];
    if (!x || x.status !== "open") return "Already answered";
    let reply;
    if (add) {
      try {
        await this.save(userId, x.spec, "", { by: this.bot.team.nameOf(x.by) });
      } catch (e) {
        if (e instanceof UserError) return e.message.slice(0, 190);
        throw e;
      }
      reply = `✅ Added '${x.spec.query}'`;
    } else reply = "❌ Ignored";
    x.status = add ? "added" : "ignored";
    await this.bot.store.put("search_suggestions", box);
    await this.bot.team.dm(x.by, add ? `✅ Your search <b>${esc(x.spec.query)}</b> was added to ${this.label(x.spec.topic)}`
      : `Your search <b>${esc(x.spec.query)}</b> wasn't added this time`);
    return reply;
  }

  // --- previews: the scanner loads Vinted, the app waits for the result
  async previewCreate(userId, spec) {
    const c = this.clean(spec);
    const box = await this.bot.store.get("previews", {});
    for (const [k, p] of Object.entries(box)) if (this.bot.now - p.at > DAY) delete box[k];
    if (Object.values(box).filter((p) => p.by === userId && p.status === "waiting").length >= PREVIEWS_PER_PERSON) {
      throw new UserError("You have 3 previews waiting already: the next scan checks them");
    }
    const id = `${this.bot.now.toString(36)}${Math.random().toString(36).slice(2, 6)}`;
    box[id] = { spec: { query: c.query, price_from: c.price_from, price_to: c.price_to, group: TOPIC_GROUP[c.topic],
      ...this.extras(c) }, by: userId, at: this.bot.now, status: "waiting" };
    await this.bot.store.put("previews", box);
    return id;
  }

  async previewGet(userId, id) {
    const p = (await this.bot.store.get("previews", {}))[id];
    if (!p || p.by !== userId) throw new UserError("That preview is gone");
    if (p.status === "waiting" && this.bot.now - p.at > PREVIEW_MAX_AGE) return { status: "expired" };
    return { status: p.status, result: p.result || null, at: p.at };
  }

  /** For the scanner (GET /api/state): the oldest waiting previews. */
  async pending() {
    const box = await this.bot.store.get("previews", {});
    return Object.entries(box).filter(([, p]) => p.status === "waiting" && this.bot.now - p.at <= PREVIEW_MAX_AGE)
      .sort((a, b) => a[1].at - b[1].at).slice(0, 3).map(([id, p]) => ({ id, spec: p.spec }));
  }

  /** The scanner's answer (POST /api/preview). */
  async previewDone(id, result) {
    const box = await this.bot.store.get("previews", {});
    if (!box[id]) return false;
    box[id] = { ...box[id], status: "done", result, done_at: this.bot.now };
    await this.bot.store.put("previews", box);
    return true;
  }
}
