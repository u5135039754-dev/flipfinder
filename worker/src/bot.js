// Telegram commands and buttons, answered as soon as they arrive (webhook).
// Only the owner and users they /allow can use them; everyone else is ignored.

import { Telegram } from "./telegram.js";
import { RULES, applySettings, effectiveBudget, searchId } from "./searches.js";
import { SELLER_MESSAGE, fullText, keyboard, stock, profit, findDeal } from "./deals.js";
import { APPROVAL_OVER, allocate, approved, entryLine, memberKey, potText, reverse, shares, summarize } from "./pot.js";
import { INTROS, TOPIC_FOR_GROUP, TOPIC_NAMES, mention, pricesFor, sellListing } from "./group.js";
import { UserError, closeMatches, esc, euro, g, parseNumber, queryAndRange, splitArgs } from "./util.js";

export const COMMANDS_VERSION = 7;   // bump when the list below changes, so it's registered again
export const COMMANDS = [
  ["help", "List all commands"],
  ["app", "Open the flipFinder app: deals, stock, pot and settings"],
  ["status", "Last run, runs today, listings checked, deals sent, platforms"],
  ["categories", "Searches by category, with buttons to turn them on or off"],
  ["prices", "Show a search's price range: /prices boss katana"],
  ["setprice", 'Change a price range: /setprice "boss katana" 80 250'],
  ["budget", "Change the budget-mode budget: /budget 72"],
  ["rules", "Show the deal rules"],
  ["setrule", "Change a rule: /setrule min_roi 25"],
  ["add", 'Add a search: /add "zoom g1x four" 20 60'],
  ["remove", "Remove a search: /remove zoom g1x four"],
  ["stock", "Items we own now (bought or listed), who has them, what we paid"],
  ["sell", "Ready-to-copy listing: /sell 12 or /sell boss ds-1 (add en or uk for English/Ukrainian)"],
  ["topic", "Send in a group topic to make it the Guitars/Electronics/Budget/Summary topic"],
  ["profit", "Profit in total, this month and per person"],
  ["pot", "The shared pot: cash, stock, profit and what each member would get back"],
  ["ledger", "Every money action, newest last: /ledger or /ledger 30"],
  ["deposit", "Owner only: money put in: /deposit Marco 100"],
  ["withdraw", "Owner only: money taken out: /withdraw Marco 50"],
  ["fix", "Owner only: correct a price: /fix 12 paid 45 or /fix 12 sold 80"],
  ["undo", "Owner only: cancel a deposit, withdrawal or fix with a new entry: /undo 7"],
  ["split", "Owner only: how profit is shared: /split contribution or /split equal"],
  ["allow", "Owner only: let another user use commands: /allow 123456789"],
  ["intro", "Owner only: post or update the pinned intro in every topic"],
];
const OWNER_ONLY = new Set(["allow", "intro", "deposit", "withdraw", "fix", "undo", "split"]);

export function claimerName(user) {
  const name = [user.first_name, user.last_name].filter(Boolean).join(" ");
  return name || (user.username ? `@${user.username}` : `user ${user.id}`);
}

export class Bot {
  /** store: Store; tg: Telegram; settings from the store; now in seconds. */
  constructor({ store, tg, ownerId, settings, now }) {
    Object.assign(this, { store, tg, settings, now });
    this.ownerId = Number(ownerId);
    this.changed = false;          // settings changed: saved at the end
    this.capture = null;           // the Mini App: replies and pop-ups collected here instead of sent
    this._deals = null;
  }

  allowed(user) {
    return user === this.ownerId || this.settings.allowed_users.includes(user);
  }

  // --- data, loaded when a command needs it
  async allDeals() {
    this._deals ??= await this.store.deals();
    return this._deals;
  }

  async pot() {
    this._pot ??= summarize(await this.store.ledger(), await this.allDeals());
    return this._pot;
  }

  /** Cash in the pot (caps the budget-mode limit), or null before the first deposit. */
  async pool() {
    const p = await this.pot();
    return p.started ? p.cash : null;
  }

  splitMode() {
    return this.settings.split === "equal" ? "equal" : "contribution";
  }

  /** Records a money action and posts it in the group (Summary topic); the private chat gets a reply. */
  async money(entry, chat) {
    const saved = await this.store.addEntry({ at: this.now, ...entry });
    this._pot = null;
    this._deals = null;
    const p = await this.pot();
    const text = `💶 <b>Pot</b> · ${entryLine(saved)} · cash now <b>${euro(p.cash)}</b>`;
    const groups = this.tg.groups;
    for (const g of groups.length ? groups : [String(this.ownerId)]) await this.tg.sendTo(g, text, { topic: "summary" });
    if (chat !== undefined && !groups.includes(String(chat)) && (groups.length || String(chat) !== String(this.ownerId))) {
      await this.reply(chat, text);
    }
    return saved;
  }

  findMember(p, name) {
    return p.members.find((m) => memberKey(m.name) === memberKey(name));
  }

  async view() {
    const catalog = await this.store.get("catalog", { searches: [] });
    return applySettings(catalog, this.settings, await this.pool());
  }

  async find(view, name) {
    name = name.trim().replace(/^"|"$/g, "").toLowerCase();
    const s = view.searches.find((x) => x.query.toLowerCase() === name);
    if (s) return s;
    const close = closeMatches(name, view.searches.map((x) => x.query));
    const hint = close.length ? ` Did you mean: ${close.join(", ")}?` : " /categories lists them all.";
    throw new UserError(`No search called "${name}".${hint}`);
  }

  async reply(chat, text, buttons) {
    if (this.capture) {
      this.capture.push(text);
      return { message_id: 0 };
    }
    const payload = { chat_id: chat, text, parse_mode: "HTML", disable_web_page_preview: true };
    if (buttons) payload.reply_markup = { inline_keyboard: buttons };
    return this.tg.call("sendMessage", payload);
  }

  async answer(cq, text) {
    if (this.capture) return void this.capture.push(text);
    await this.tg.call("answerCallbackQuery", { callback_query_id: cq.id, text });
  }

  /** Saves settings changed by this update (and supergroup moves Telegram told us about). */
  async save() {
    const moved = Object.entries(this.tg.newMigrations);
    if (moved.length) {
      this.settings.chat_migrations = { ...(this.settings.chat_migrations || {}), ...this.tg.newMigrations };
      this.changed = true;
    }
    if (this.changed) await this.store.put("settings", this.settings);
    for (const [, newId] of moved) {
      await this.tg.sendText(`ℹ️ Your Telegram group was upgraded to a supergroup, so its chat id changed to ` +
        `<code>${newId}</code>. flipFinder switched to it by itself.`, { chats: [String(this.ownerId)] });
    }
    this.tg.newMigrations = {};
  }

  async handle(update) {
    try {
      if (update.callback_query) await this.onButton(update.callback_query);
      else if (update.message) await this.onMessage(update.message);
    } finally {
      await this.save();
    }
  }

  // --- messages
  async onMessage(msg) {
    const text = (msg.text || "").trim();
    const user = msg.from?.id;
    const chat = msg.chat.id;
    if (!this.allowed(user)) return;   // not someone we take commands from: ignore quietly
    this.learnTopic(msg);
    if (!text.startsWith("/")) return this.onPriceReply(chat, user, text);
    const space = text.indexOf(" ");
    const head = space < 0 ? text : text.slice(0, space);
    const rest = space < 0 ? "" : text.slice(space + 1);
    const cmd = head.slice(1).split("@")[0].toLowerCase();
    const handler = this[`cmd_${cmd}`];
    if (typeof handler !== "function") return this.reply(chat, `I don't know /${esc(cmd)}. Try /help`);
    try {
      await handler.call(this, chat, splitArgs(rest), user, msg);
    } catch (e) {
      if (!(e instanceof UserError)) throw e;
      await this.reply(chat, `⚠️ ${esc(e.message, false)}`);
    }
  }

  // --- commands
  async cmd_help(chat, args, user) {
    const lines = ["<b>flipFinder commands</b>", ""];
    for (const [c, d] of COMMANDS) if (!OWNER_ONLY.has(c) || user === this.ownerId) lines.push(`/${c} – ${esc(d)}`);
    lines.push("", "Search and price changes apply from the next run (every ~5 min).");
    await this.reply(chat, lines.join("\n"));
  }

  async cmd_app(chat) {
    const url = await this.store.get("app_url");
    if (!url) throw new UserError("The app isn't ready yet, try again in a few minutes");
    if (!Telegram.isGroup(chat)) {
      return this.reply(chat, "📱 Deals, stock, the pot and settings in one place:",
        [[{ text: "📱 Open app", web_app: { url } }]]);
    }
    // Telegram only opens Mini Apps from buttons in the private chat with the bot
    const me = await this.tg.call("getMe", {});
    await this.reply(chat, "📱 The app opens from the private chat with the bot (menu button, bottom left).",
      me?.username ? [[{ text: "Open the bot", url: `https://t.me/${me.username}` }]] : undefined);
  }

  async cmd_status(chat) {
    const st = await this.store.get("status", {});
    const area = await this.store.get("area");
    const view = await this.view();
    const last = st.last_run ? new Date(st.last_run * 1000).toUTCString().slice(5, 22).replace(/ (\d{4}) /, " ") + " UTC"
      : "unknown";
    const on = view.searches.filter((s) => s.enabled).length;
    const subito = view.subito && area ? `✅ (${g(area.radius_km)} km)` : "❌";
    await this.reply(chat, [
      "📊 <b>flipFinder status</b>",
      `Last run: ${last}`,
      `Since the last daily summary: ${(st.runs || 0).toLocaleString("en-US")} runs · ` +
        `${(st.checked || 0).toLocaleString("en-US")} listings checked · ${(st.deals_sent || 0).toLocaleString("en-US")} deals sent`,
      `Searches on: ${on} of ${view.searches.length} · budget €${g(view.budget)}`,
      `Platforms: Vinted ✅ · eBay ${view.ebay ? "✅" : "❌"} · Subito ${subito}`,
    ].join("\n"));
  }

  async categoryButtons(searches) {
    const rows = [];
    for (const s of searches) {
      rows.push([{ text: `${s.enabled ? "✅" : "❌"} ${s.query}`, callback_data: `t:${await searchId(s.query)}` }]);
    }
    return rows;
  }

  async cmd_categories(chat) {
    const groups = new Map();
    for (const s of (await this.view()).searches) {
      if (!groups.has(s.group)) groups.set(s.group, []);
      groups.get(s.group).push(s);
    }
    for (const [group, searches] of groups) {
      const on = searches.filter((s) => s.enabled).length;
      await this.reply(chat, `<b>${esc(group)}</b> (${on}/${searches.length} on) – tap to turn on/off`,
        await this.categoryButtons(searches));
    }
  }

  async cmd_prices(chat, args) {
    if (!args.length) throw new UserError("Which search? e.g. /prices boss katana");
    const s = await this.find(await this.view(), args.join(" "));
    let rng = s.price_from !== null && s.price_from !== undefined ? `€${g(s.price_from)}` : "any";
    rng += s.price_to !== null && s.price_to !== undefined ? ` – €${g(s.price_to)}` : " – no max";
    await this.reply(chat, `<b>${esc(s.query)}</b>: ${rng}${s.budget ? " (budget mode)" : ""}`);
  }

  async cmd_setprice(chat, args) {
    const [name, lo, hi] = queryAndRange(args);
    const view = await this.view();
    const s = await this.find(view, name);
    if (s.budget && hi > view.budget) {
      throw new UserError(`"${s.query}" is a budget search: the max can't be above the €${g(view.budget)} budget`);
    }
    this.settings.prices[s.query] = [lo, hi];
    this.changed = true;
    await this.reply(chat, `✅ <b>${esc(s.query)}</b>: €${g(lo)} – €${g(hi)}`);
  }

  async cmd_budget(chat, args) {
    if (args.length !== 1) throw new UserError("Give one amount, e.g. /budget 72");
    const amount = parseNumber(args[0]);
    if (amount === null) throw new UserError(`The budget must be a number, I got '${args[0]}'`);
    if (!(amount >= 1 && amount <= 100_000)) throw new UserError("The budget must be between €1 and €100,000");
    this.settings.budget = amount;
    this.changed = true;
    const pool = await this.pool();
    const note = pool !== null && pool < amount
      ? `\nℹ️ The pot has ${euro(pool)} cash, so the limit in effect is ${euro(effectiveBudget(amount, pool))} (the smaller of the two).`
      : "";
    await this.reply(chat, `✅ Budget is now €${g(amount)} (budget-mode searches only)${note}`);
  }

  async cmd_rules(chat) {
    const view = await this.view();
    const r = view.rules;
    const br = view.budget_rules;
    await this.reply(chat, [
      "<b>Deal rules</b>",
      `min_profit: €${g(r.min_profit)}`, `min_roi: ${g(r.min_roi)}%`, `min_rating: ${r.min_rating}`,
      `max_roi: ${g(r.max_roi)}%`,
      "", `<i>Budget mode (€${g(view.budget)}): min_profit €${g(br.min_profit)}, ` +
        `min_roi ${g(br.min_roi)}%, max_roi ${g(br.max_roi)}%</i>`,
    ].join("\n"));
  }

  async cmd_setrule(chat, args) {
    if (args.length !== 2) throw new UserError("Use /setrule <name> <value>, e.g. /setrule min_roi 25");
    const name = args[0].toLowerCase();
    if (!(name in RULES)) throw new UserError(`Unknown rule '${name}'. Rules: ${Object.keys(RULES).join(", ")}`);
    const [type, lo, hi] = RULES[name];
    const value = parseNumber(args[1].replace(/%/g, ""));
    if (value === null || (type === "int" && !Number.isInteger(value))) {
      throw new UserError(`${name} must be ${type === "int" ? "a whole number" : "a number"}, I got '${args[1]}'`);
    }
    if (!(value >= lo && value <= hi)) throw new UserError(`${name} must be between ${lo} and ${hi}`);
    const r = (await this.view()).rules;
    if ((name === "max_roi" && value <= r.min_roi) || (name === "min_roi" && value >= r.max_roi)) {
      throw new UserError("min_roi must stay below max_roi");
    }
    this.settings.rules[name] = value;
    this.changed = true;
    await this.reply(chat, `✅ ${name} is now ${g(value)}`);
  }

  async cmd_add(chat, args) {
    const [name, lo, hi] = queryAndRange(args);
    if (!name) throw new UserError("The search can't be empty");
    if ((await this.view()).searches.some((s) => s.query.toLowerCase() === name)) {
      throw new UserError(`There's already a search called "${name}". Change it with /setprice`);
    }
    this.settings.added = [...this.settings.added.filter((a) => a.query !== name),
      { query: name, price_from: lo, price_to: hi }];
    this.settings.removed = this.settings.removed.filter((q) => q !== name);
    this.changed = true;
    await this.reply(chat, `✅ Added <b>${esc(name)}</b> (€${g(lo)} – €${g(hi)}). Its current listings are ` +
      "checked on the next run, then new ones alert as usual.");
  }

  async cmd_remove(chat, args) {
    if (!args.length) throw new UserError("Which search? e.g. /remove boss katana");
    const s = await this.find(await this.view(), args.join(" "));
    const sid = await searchId(s.query);
    await this.reply(chat, `Remove <b>${esc(s.query)}</b>? You can /add it again later.`,
      [[{ text: "Yes, remove it", callback_data: `rm:${sid}:y` }, { text: "No", callback_data: `rm:${sid}:n` }]]);
  }

  async cmd_allow(chat, args, user) {
    if (user !== this.ownerId) throw new UserError("Only the owner can use /allow");
    if (args.length !== 1 || !/^\d+$/.test(args[0])) throw new UserError("Give a numeric Telegram user id, e.g. /allow 123456789");
    const uid = Number(args[0]);
    if (!this.settings.allowed_users.includes(uid) && uid !== this.ownerId) {
      this.settings.allowed_users.push(uid);
      this.changed = true;
    }
    await this.reply(chat, `✅ User ${uid} can now use commands`);
  }

  async cmd_stock(chat) {
    const items = stock(await this.allDeals());
    if (!items.length) return this.reply(chat, "📦 Nothing in stock right now");
    const lines = ["📦 <b>In stock</b>"];
    for (const d of items) {
      lines.push(`${d.status === "bought" ? "💸" : "🏷"} #${d.n ?? "?"} <a href="${esc(d.url)}">` +
        `${esc(d.title.slice(0, 50))}</a> · ${esc(d.who)} · paid ${euro(d.paid || 0)}`);
    }
    lines.push(`\nTied up: ${euro(items.reduce((s, d) => s + (d.paid || 0), 0))}`);
    await this.reply(chat, lines.join("\n"));
  }

  async cmd_profit(chat) {
    const p = profit(await this.allDeals(), this.now);
    const lines = ["💰 <b>Profit</b>", `Total: ${euro(p.total)} (${p.sold} sold)`, `This month: ${euro(p.month)}`];
    for (const [who, v] of Object.entries(p.people).sort((a, b) => b[1] - a[1])) lines.push(`· ${esc(who)}: ${euro(v)}`);
    lines.push("\n/pot shows each member's share");
    await this.reply(chat, lines.join("\n"));
  }

  async cmd_pot(chat) {
    const setting = this.settings.budget ?? (await this.store.get("catalog", {})).budget ?? 72;
    const p = await this.pot();
    await this.reply(chat, potText(p, this.splitMode(), effectiveBudget(setting, p.started ? p.cash : null)));
  }

  async cmd_pool(chat, args, user) {
    return this.cmd_pot(chat, args, user);   // the old name
  }

  amountArg(raw, what = "The amount") {
    const amount = parseNumber(raw ?? "");
    if (amount === null) throw new UserError(`${what} must be a number, I got '${raw ?? ""}'`);
    if (!(amount > 0 && amount <= 100_000)) throw new UserError(`${what} must be between €0.01 and €100,000`);
    return Math.round(amount * 100) / 100;
  }

  ownerOnly(user, cmd) {
    if (user !== this.ownerId) throw new UserError(`Only the owner (treasurer) can use /${cmd}`);
  }

  async cmd_deposit(chat, args, user) {
    this.ownerOnly(user, "deposit");
    if (args.length < 2) throw new UserError("Who and how much? e.g. /deposit Marco 100");
    const amount = this.amountArg(args.at(-1));
    const typed = args.slice(0, -1).join(" ");
    const name = this.findMember(await this.pot(), typed)?.name || typed;
    await this.money({ kind: "deposit", amount, member: name, deposited: { [name]: amount } }, chat);
  }

  async cmd_withdraw(chat, args, user) {
    this.ownerOnly(user, "withdraw");
    if (args.length < 2) throw new UserError("Who and how much? e.g. /withdraw Marco 50");
    const amount = this.amountArg(args.at(-1));
    const p = await this.pot();
    const m = this.findMember(p, args.slice(0, -1).join(" "));
    if (!m) throw new UserError(`No member called "${args.slice(0, -1).join(" ")}". /pot lists them`);
    if (amount > m.account) throw new UserError(`${m.name} has ${euro(m.account)} in the pot, can't take out ${euro(amount)}`);
    if (amount > p.cash) {
      throw new UserError(`The pot only has ${euro(p.cash)} in cash (the rest is in stock), can't pay out ${euro(amount)}`);
    }
    await this.money({ kind: "withdraw", amount: -amount, member: m.name, withdrawn: { [m.name]: amount } }, chat);
  }

  async cmd_ledger(chat, args) {
    const all = await this.store.ledger();
    if (!all.length) return this.reply(chat, "📒 Nothing in the ledger yet");
    const n = Math.min(Math.max(Number(args[0]) || 15, 1), 50);
    const when = (e) => new Date(e.at * 1000).toLocaleDateString("en-GB", { timeZone: "Europe/Rome", day: "2-digit", month: "short" });
    const lines = [`📒 <b>Ledger</b> (last ${Math.min(n, all.length)} of ${all.length})`];
    for (const e of all.slice(-n)) lines.push(`${when(e)} · ${entryLine(e)}`);
    lines.push("", "Entries are never changed: /undo and /fix add new ones.");
    await this.reply(chat, lines.join("\n"));
  }

  async cmd_undo(chat, args, user) {
    this.ownerOnly(user, "undo");
    const id = Number(args[0]);
    const all = await this.store.ledger();
    const e = all.find((x) => x.id === id);
    if (!e) throw new UserError("Which entry? /ledger shows the numbers, e.g. /undo 7");
    if (!["deposit", "withdraw", "fix"].includes(e.kind)) {
      throw new UserError("Buys and sales follow the deal: correct them with /fix <deal number> paid|sold <amount>");
    }
    if (all.some((x) => x.kind === "undo" && x.ref === id)) throw new UserError(`Entry ${id} was already undone`);
    await this.money({ kind: "undo", ref: id, member: e.member, n: e.n, ...reverse(e) }, chat);
  }

  async cmd_fix(chat, args, user) {
    this.ownerOnly(user, "fix");
    const [ref, what, raw] = args;
    if (!ref || !["paid", "sold"].includes(what)) throw new UserError("Use /fix <deal number> paid <amount> or /fix <deal number> sold <amount>");
    const amount = this.amountArg(raw);
    const found = findDeal(await this.allDeals(), ref);
    if (!found || !/^#?\d+$/.test(ref)) throw new UserError(`No deal #${ref.replace(/^#/, "")}`);
    const [key, d] = found;
    const field = what === "paid" ? "paid" : "sold_for";
    if (d[field] === undefined || d[field] === null) throw new UserError(`#${d.n} has no ${what} price yet`);
    const diff = Math.round((amount - d[field]) * 100) / 100;
    if (!diff) throw new UserError(`#${d.n} already has ${what} ${euro(amount)}`);
    // paying more means less cash and (once sold) less profit; selling for more, the opposite
    const cash = what === "paid" ? -diff : diff;
    const entry = { kind: "fix", amount: cash, n: d.n, deal: key, note: `${what} ${euro(d[field])} → ${euro(amount)}` };
    if (d.status === "sold") {
      const sale = (await this.store.ledger()).find((x) => x.kind === "sale" && x.deal === key);
      const weights = sale?.shares || shares(await this.pot(), this.splitMode());
      entry.profit = allocate(cash, weights);   // shared like the sale was
    }
    d[field] = amount;
    await this.store.saveDeal(key, d);
    await this.refreshDeal(key, d);
    await this.money(entry, chat);
  }

  async cmd_split(chat, args, user) {
    this.ownerOnly(user, "split");
    const mode = (args[0] || "").toLowerCase();
    if (!["equal", "contribution"].includes(mode)) throw new UserError("Use /split contribution or /split equal");
    this.settings.split = mode;
    this.changed = true;
    const text = `⚖️ From now on profit is split ${mode === "equal" ? "equally" : "by how much each member put in"}. ` +
      "Profit from earlier sales stays as it was shared.";
    await this.reply(chat, text);
    for (const g of this.tg.groups) if (String(g) !== String(chat)) await this.tg.sendTo(g, text, { topic: "summary" });
  }

  async cmd_sell(chat, args) {
    let lang = "it";
    const last = args.at(-1)?.toLowerCase();
    if (["en", "uk", "ua", "it"].includes(last)) {
      lang = last === "ua" ? "uk" : last;
      args = args.slice(0, -1);
    }
    if (!args.length) throw new UserError("Which item? /sell 12 or /sell boss ds-1 (add en or uk for English/Ukrainian)");
    const found = findDeal(await this.allDeals(), args.join(" "));
    if (!found) throw new UserError(`No deal matching "${args.join(" ")}". /stock lists what we have`);
    const area = await this.store.get("area");
    await this.reply(chat, sellListing(found[1], found[1].value_now, lang, area?.city || ""));
  }

  // --- group topics and their pinned intros
  /** A message in a topic tells us its id, and (via the topic's first message) its name. */
  learnTopic(msg, name = null) {
    const thread = msg.message_thread_id;
    if (!msg.is_topic_message || !thread) return null;
    const created = msg.reply_to_message?.forum_topic_created?.name || "";
    const wanted = (name || created).toLowerCase();
    const key = wanted ? Object.keys(TOPIC_NAMES).find((k) => wanted.includes(k) || wanted.startsWith(k.slice(0, 4))) : null;
    if (key && this.settings.topics?.[key] !== thread) {
      this.settings.topics = { ...(this.settings.topics || {}), [key]: thread };
      this.tg.topics = { ...this.settings.topics };
      this.changed = true;
    }
    return key || null;
  }

  async cmd_topic(chat, args, user, msg) {
    if (!msg?.is_topic_message) throw new UserError("Send /topic inside a group topic (Guitars, Electronics, Budget or Summary)");
    const key = this.learnTopic(msg, args.length ? args.join(" ") : null);
    if (!key) {
      throw new UserError("I couldn't tell which topic this is. Use /topic guitars, /topic electronics, " +
        "/topic budget or /topic summary");
    }
    const known = Object.keys(TOPIC_NAMES).filter((k) => k in (this.settings.topics || {})).map((k) => TOPIC_NAMES[k]);
    await this.tg.call("sendMessage", { chat_id: chat, message_thread_id: msg.message_thread_id, parse_mode: "HTML",
      text: `✅ This topic is now <b>${TOPIC_NAMES[key]}</b>. Set so far: ${known.join(", ")}` });
    await this.postIntro(chat, key);
    if (!this.settings.intros?.[String(chat)]?.general) await this.postIntro(chat, "general");
  }

  /**
   * Pins INTROS[key] in its topic ("general" = the main chat). An intro we posted before is edited
   * in place (and pinned again in case someone unpinned it); a new one only when it's gone.
   */
  async postIntro(chat, key) {
    const text = INTROS[key];
    const thread = key === "general" ? null : this.settings.topics?.[key];
    if (key !== "general" && !thread) return false;
    this.settings.intros ??= {};
    const posted = (this.settings.intros[String(chat)] ??= {});
    const old = posted[key];
    let msgId = null;
    if (old && (old.thread ?? null) === (thread ?? null)) {
      if (old.text === text) msgId = old.id;   // unchanged: Telegram refuses an edit that changes nothing
      else if (await this.tg.call("editMessageText", { chat_id: chat, message_id: old.id, text, parse_mode: "HTML",
        disable_web_page_preview: true })) msgId = old.id;
    }
    if (msgId === null) {   // never posted, deleted, or the topic changed
      const payload = { chat_id: chat, text, parse_mode: "HTML", disable_web_page_preview: true };
      if (thread) payload.message_thread_id = thread;
      const res = await this.tg.call("sendMessage", payload);
      if (!res?.message_id) return false;
      msgId = res.message_id;
    }
    const pinned = (await this.tg.call("pinChatMessage", { chat_id: chat, message_id: msgId, disable_notification: true })) !== null;
    posted[key] = { id: msgId, thread: thread ?? null, text };
    this.changed = true;
    return pinned;
  }

  async cmd_intro(chat, args, user) {
    if (user !== this.ownerId) throw new UserError("Only the owner can use /intro");
    const groups = Telegram.isGroup(chat) ? [String(chat)] : this.tg.groups;
    if (!groups.length) throw new UserError("No group to post in");
    const topics = this.settings.topics || {};
    const keys = [...Object.keys(TOPIC_NAMES).filter((k) => k in topics), "general"];
    let results = {};
    for (const grp of groups) {
      results = {};
      for (const k of keys) results[k] = await this.postIntro(grp, k);
    }
    const name = (k) => TOPIC_NAMES[k] || "General";
    const lines = [`📌 Intros pinned: ${keys.filter((k) => results[k]).map(name).join(", ") || "none"}`];
    const unpinned = keys.filter((k) => !results[k]).map(name);
    if (unpinned.length) {
      lines.push(`⚠️ Posted but not pinned: ${unpinned.join(", ")}. Make the bot an admin with "Pin messages" and send /intro again.`);
    }
    const missing = Object.keys(TOPIC_NAMES).filter((k) => !(k in topics)).map((k) => TOPIC_NAMES[k]);
    if (missing.length) lines.push(`Not set up yet: ${missing.join(", ")} (send /topic in each)`);
    await this.reply(chat, lines.join("\n"));
  }

  // --- deal lifecycle: ✋ Claim -> 💸 Bought -> 🏷 Listed -> ✅ Sold
  /** Edit every copy of the deal (private chat and group) to the current status. */
  async refreshDeal(key, d) {
    const text = fullText(d);
    const kb = keyboard(key, d);
    for (const m of d.messages || []) {
      if (m.photo) {
        await this.tg.call("editMessageCaption", { chat_id: m.chat, message_id: m.id, caption: text.slice(0, 1024),
          parse_mode: "HTML", reply_markup: kb });
      } else {
        await this.tg.call("editMessageText", { chat_id: m.chat, message_id: m.id, text, parse_mode: "HTML",
          reply_markup: kb });
      }
    }
  }

  async onDealButton(cq, chat, user, kind, key) {
    const d = await this.store.deal(key);
    if (!d) return this.answer(cq, "I don't have this deal any more");
    const who = claimerName(cq.from || {});
    if (kind === "up" || kind === "dn") {
      const vote = kind === "up" ? "up" : "down";
      const wasApproved = approved(d, [d.who_id]);
      d.votes[String(user)] = vote;
      if (vote === "down") {
        await this.store.addFeedback({ key, title: d.title, url: d.url, by: who, at: this.now, value: d.value ?? null,
          cost: d.cost ?? null, query: d.query || "" });
      }
      await this.store.saveDeal(key, d);
      await this.refreshDeal(key, d);
      if (d.status === "claimed" && (d.cost || 0) > APPROVAL_OVER && !wasApproved && approved(d, [d.who_id])) {
        await this.postAbout(d, `✅ #${d.n ?? "?"} approved by ${esc(who)}: ${mention(d.who || "", d.who_id)} can tap 💸 Bought.`);
      }
      return this.answer(cq, vote === "up" ? "👍 Noted" : "👎 Noted, it's logged to tune the filters");
    }
    if (kind === "m") {
      await this.reply(chat, "📩 Copy and send this to the seller:\n\n<code>" + esc(SELLER_MESSAGE(d.title), false) +
        `</code>\n\n<a href="${esc(d.url)}">Open the listing</a>`);
      return this.answer(cq, "Message ready below");
    }
    if (kind === "c") {
      if (d.status !== "new") return this.answer(cq, `${d.who || "Someone"} already has this one`);
      Object.assign(d, { status: "claimed", who, who_id: user, claimed_at: this.now });
      await this.store.saveDeal(key, d);
      await this.refreshDeal(key, d);
      return this.answer(cq, "It's yours, good luck!");
    }
    // the next steps belong to whoever claimed it (or the owner)
    if (user !== d.who_id && user !== this.ownerId) return this.answer(cq, `${d.who || "Someone else"} has this one`);
    const expected = { b: "claimed", l: "bought", s: "listed" }[kind];
    if (d.status !== expected) return this.answer(cq, "That step is already done");
    if (kind === "l") {
      Object.assign(d, { status: "listed", listed_at: this.now });
      await this.store.saveDeal(key, d);
      await this.refreshDeal(key, d);
      return this.answer(cq, "🏷 Listed");
    }
    if (kind === "b" && (d.cost || 0) > APPROVAL_OVER && !approved(d, [d.who_id, user])) {
      await this.postAbout(d, `✋ #${d.n ?? "?"} costs ${euro(d.cost)}: buys over €${APPROVAL_OVER} need a 👍 on the deal ` +
        "from another member first.");
      return this.answer(cq, `Over €${APPROVAL_OVER}: needs a 👍 from another member first`);
    }
    const ask = kind === "b" ? "paid" : "sold_for";
    const title = esc(d.title.slice(0, 60));
    const cost = ask === "paid" ? d.cost : null;
    let q;
    let markup;
    if (cost) {
      // suggest what we know it costs; a different amount can still be typed as a reply
      q = `💸 How much did you pay for <b>${title}</b>?\nKnown total (price + buyer fee + shipping/pickup): ` +
        `<b>${euro(cost)}</b>.\nTap ✅ to use it, or reply to this message with a different amount.`;
      markup = { inline_keyboard: [[{ text: `✅ Use ${euro(cost)}`, callback_data: `pay:${key}` }]] };
    } else if (ask === "paid") {
      q = `💸 How much did you pay for <b>${title}</b>? Reply with the amount, e.g. 45`;
      markup = { force_reply: true, input_field_placeholder: "amount in €" };
    } else {
      q = `✅ How much did <b>${title}</b> sell for? Reply with the amount, e.g. 80`;
      markup = { force_reply: true, input_field_placeholder: "amount in €" };
    }
    const sent = await this.tg.call("sendMessage", { chat_id: chat, text: q, parse_mode: "HTML", reply_markup: markup });
    const pending = await this.store.get("pending", {});
    pending[String(user)] = { key, ask, chat, ask_msg: sent?.message_id ?? null };
    await this.store.put("pending", pending);
    return this.answer(cq, cost ? "Tap ✅ or reply with the price" : "Reply with the price");
  }

  /** ✅ Use €X under the "how much did you pay?" question. */
  async onUseCost(cq, chat, user, key) {
    const pending = (await this.store.get("pending", {}))[String(user)];
    const d = await this.store.deal(key);
    if (!pending || pending.key !== key || pending.ask !== "paid" || !d) {
      return this.answer(cq, `That's for ${d?.who || "someone else"}`);
    }
    await this.answer(cq, "Done");
    await this.applyPrice(chat, user, Number(d.cost));
  }

  async onPriceReply(chat, user, text) {
    const pending = (await this.store.get("pending", {}))[String(user)];
    if (!pending || String(pending.chat) !== String(chat)) return;   // just chatting
    const amount = parseNumber(text);
    if (amount === null) return this.reply(chat, `⚠️ I need just the amount, e.g. 45 (got ${esc(text.slice(0, 30))})`);
    if (!(amount >= 0 && amount <= 100_000)) return this.reply(chat, "⚠️ That amount doesn't look right");
    await this.applyPrice(chat, user, amount);
  }

  async applyPrice(chat, user, amount) {
    const all = await this.store.get("pending", {});
    const pending = all[String(user)];
    delete all[String(user)];
    await this.store.put("pending", all);
    if (pending.ask_msg) {
      // the question is answered: drop its "✅ Use €X" button
      await this.tg.call("editMessageReplyMarkup", { chat_id: pending.chat, message_id: pending.ask_msg,
        reply_markup: { inline_keyboard: [] } });
    }
    const d = await this.store.deal(pending.key);
    if (!d) return;
    if (pending.ask === "paid" && amount > APPROVAL_OVER && !approved(d, [d.who_id, user])) {
      return this.reply(chat, `⚠️ ${euro(amount)} is over €${APPROVAL_OVER}: it needs a 👍 on the deal from another ` +
        "member first. Then tap 💸 Bought again.");
    }
    let done;
    let entry;
    if (pending.ask === "paid") {
      Object.assign(d, { status: "bought", paid: amount, bought_at: this.now });
      done = `💸 Bought for ${euro(amount)}`;
      entry = { kind: "buy", amount: -amount, n: d.n, deal: pending.key };
    } else {
      Object.assign(d, { status: "sold", sold_for: amount, sold_at: this.now });
      const gain = Math.round((amount - (d.paid || 0)) * 100) / 100;
      done = `✅ Sold for ${euro(amount)}, profit ${euro(gain)}`;
      const weights = shares(await this.pot(), this.splitMode());
      entry = { kind: "sale", amount, n: d.n, deal: pending.key, profit: allocate(gain, weights), shares: weights };
    }
    await this.store.saveDeal(pending.key, d);
    await this.refreshDeal(pending.key, d);
    await this.reply(chat, done);
    await this.money(entry, chat);
  }

  // --- reminders' buttons
  async onKeepRelease(cq, user, kind, key) {
    const d = await this.store.deal(key);
    if (!d || d.status !== "claimed") return this.answer(cq, "That's already sorted");
    if (user !== d.who_id && user !== this.ownerId) return this.answer(cq, `That's ${d.who || "someone else"}'s`);
    if (kind === "keep") {
      d.kept_at = this.now;
      await this.store.saveDeal(key, d);
      return this.answer(cq, "✋ Kept, you have another 24 h");
    }
    await this.release(key, d, "released it");
    return this.answer(cq, "❌ Released for someone else");
  }

  async release(key, d, why) {
    const who = d.who || "someone";
    for (const f of ["who", "who_id", "claimed_at", "kept_at", "pinged_at"]) delete d[f];
    d.status = "new";
    await this.store.saveDeal(key, d);
    await this.refreshDeal(key, d);
    await this.postAbout(d, `⌛ #${d.n ?? "?"} ${esc(d.title.slice(0, 50))} is free again (${esc(who)} ${why}). ` +
      "Tap ✋ to claim it.");
  }

  /** A note about a deal: in the group, in its topic, as a reply to the deal; else the private chat. */
  async postAbout(d, text, buttons) {
    const msgs = d.messages || [];
    const target = msgs.find((m) => Telegram.isGroup(m.chat)) || msgs[0] || null;
    const chat = target ? target.chat : this.tg.chatIds[0];
    if (!chat) return;
    await this.tg.sendTo(chat, text, { buttons, topic: TOPIC_FOR_GROUP[d.group || ""], replyTo: target?.id });
  }

  /** Reminders due now (called by the 5-minute cron); stops when the request budget runs low. */
  async reminders(due) {
    for (const [kind, key] of due) {
      if (this.tg.callsLeft < 6) break;
      const d = await this.store.deal(key);
      if (!d) continue;
      const n = d.n ?? "?";
      const title = esc(d.title.slice(0, 50));
      if (kind === "ping") {
        d.pinged_at = this.now;
        await this.postAbout(d, `${mention(d.who || "", d.who_id)}, still on it? (#${n} ${title}) ` +
          "Tap ✋ to keep it or ❌ to release it.", { inline_keyboard: [[
          { text: "✋ Keep it", callback_data: `keep:${key}` }, { text: "❌ Release", callback_data: `rel:${key}` }]] });
      } else if (kind === "release") {
        await this.release(key, d, "didn't update it for 48 h");
        continue;
      } else if (kind === "list") {
        d.nudged_at = this.now;
        await this.postAbout(d, `📦 ${mention(d.who || "", d.who_id)}, #${n} ${title} was bought ` +
          `${Math.round((this.now - d.bought_at) / 86400)} days ago. Time to list it? /sell ${n} writes the listing.`);
      } else if (kind === "cut") {
        d.cut_at = this.now;
        const [value, price, quick] = pricesFor(d, d.value_now);
        await this.postAbout(d, `🏷 #${n} ${title} has been listed for ${Math.round((this.now - d.listed_at) / 86400)} ` +
          `days. Market value now about €${Math.round(value).toLocaleString("en-US")}: try <b>€${g(price)}</b>, ` +
          `or <b>€${g(quick)}</b> to sell it quickly.`);
      }
      await this.store.saveDeal(key, d);
    }
  }

  // --- older alerts' single "I'm on it ✋" button
  async onClaim(cq, msg, chat, data) {
    if (data === "claimed") {
      const label = msg.reply_markup?.inline_keyboard?.[0]?.[0]?.text || "";
      return this.answer(cq, label || "Already claimed");
    }
    const who = claimerName(cq.from || {});
    const when = new Date(this.now * 1000).toISOString().slice(11, 16);
    await this.tg.call("editMessageReplyMarkup", { chat_id: chat, message_id: msg.message_id,
      reply_markup: { inline_keyboard: [[{ text: `✋ ${who} is on it (${when} UTC)`, callback_data: "claimed" }]] } });
    return this.answer(cq, "It's yours, good luck!");
  }

  // --- buttons
  async onButton(cq) {
    const user = cq.from?.id;
    const data = cq.data || "";
    const msg = cq.message || {};
    const chat = msg.chat?.id;
    if (!this.allowed(user)) return;   // only allowed users can press buttons
    if (data === "claim" || data === "claimed") return this.onClaim(cq, msg, chat, data);
    const colon = data.indexOf(":");
    const kind = colon < 0 ? data : data.slice(0, colon);
    const rest = colon < 0 ? "" : data.slice(colon + 1);
    if (kind === "pay") return this.onUseCost(cq, chat, user, rest);
    if (kind === "keep" || kind === "rel") return this.onKeepRelease(cq, user, kind, rest);
    if (["c", "b", "l", "s", "up", "dn", "m"].includes(kind)) return this.onDealButton(cq, chat, user, kind, rest);
    const view = await this.view();
    const byId = {};
    for (const s of view.searches) byId[await searchId(s.query)] = s;
    let note;
    if (kind === "t" && byId[rest]) {
      const s = byId[rest];
      s.enabled = !s.enabled;
      const dis = new Set(this.settings.disabled);
      if (s.enabled) dis.delete(s.query);
      else dis.add(s.query);
      this.settings.disabled = [...dis].sort();
      this.changed = true;
      note = `${s.query}: ${s.enabled ? "on" : "off"}`;
      // refresh this category's buttons
      await this.tg.call("editMessageReplyMarkup", { chat_id: chat, message_id: msg.message_id,
        reply_markup: { inline_keyboard: await this.categoryButtons(view.searches.filter((x) => x.group === s.group)) } });
    } else if (kind === "rm") {
      const [sid, answer] = rest.split(":");
      const s = byId[sid];
      if (!s) note = "That search is already gone";
      else if (answer === "y") {
        this.settings.removed = [...new Set([...this.settings.removed, s.query])].sort();
        this.settings.added = this.settings.added.filter((a) => a.query !== s.query);
        this.changed = true;
        note = `Removed ${s.query}`;
      } else note = "Kept it";
      await this.tg.call("editMessageText", { chat_id: chat, message_id: msg.message_id,
        text: (answer === "y" && s ? "🗑 " : "") + note });
    } else {
      note = "That button is out of date, send the command again";
    }
    await this.answer(cq, note);
  }
}
