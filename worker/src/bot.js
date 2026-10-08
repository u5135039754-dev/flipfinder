// Telegram commands and buttons, answered as soon as they arrive (webhook).
// Only the owner and users they /allow can use them; everyone else is ignored.

import { Telegram } from "./telegram.js";
import { RULES, applySettings, effectiveBudget, searchId } from "./searches.js";
import { SELLER_MESSAGE, fullText, keyboard, stock, profit, findDeal } from "./deals.js";
import { allocate, entryLine, memberKey, potText, reverse, shares, summarize } from "./pot.js";
import { ROLES, Team } from "./team.js";
import { Handbook } from "./handbook.js";
import { AI, PRICES } from "./ai.js";
import { INTROS, TOPIC_FOR_GROUP, TOPIC_NAMES, mention, pricesFor, sellListing } from "./group.js";
import { DEFAULT_WATCH, MAX_WATCH, findCoin, watchlist } from "./crypto.js";
import { hhmm, rome, romeTs } from "./util.js";
import { UserError, closeMatches, esc, euro, g, parseNumber, queryAndRange, splitArgs } from "./util.js";

export const COMMANDS_VERSION = 16;   // bump when the list below changes, so it's registered again
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
  ["demand", "How fast a model sells: /demand iphone 13 128gb or /demand boss ds 1"],
  ["watch", "Crypto watchlist: /watch shows it, /watch link adds a coin"],
  ["unwatch", "Remove a coin from the crypto watchlist: /unwatch sol"],
  ["pot", "The shared pot: cash, stock, profit and what each member would get back"],
  ["roles", "Who does what in the team"],
  ["duty", "Who's on duty, with Start / End / Swap buttons"],
  ["task", "Tasks: /task add Photos for #12 @Anna by fri · /task done 3"],
  ["tasks", "Open tasks"],
  ["handbook", "The team handbook (owner: /handbook edit buying, then the new rules)"],
  ["ask", "Ask the AI: /ask is a Boss DS-1 for €30 a good buy?"],
  ["note", "Teach the AI a lesson: /note amps need a tube check"],
  ["notes", "The team's notes for the AI"],
  ["delnote", "Owner only: remove a note: /delnote 3"],
  ["ai", "AI status; owner: /ai on|off, /ai auto on|off"],
  ["aiusage", "AI spend and calls today and this month"],
  ["fast", "Fast lane (newest listings every few minutes); owner: /fast 2, /fast off"],
  ["setrole", "Owner only: /setrole Anna seller (manager, buyer, seller, or none to remove it)"],
  ["removerole", "Owner only: /removerole Anna (blocks them right away)"],
  ["remind", "Owner only: the \"not claimed yet\" digest: /remind on or /remind off"],
  ["setname", "Owner only: the name the bot uses for someone: /setname 123456789 Anna"],
  ["ledger", "Every money action, newest last: /ledger or /ledger 30"],
  ["deposit", "Owner only: money put in: /deposit Marco 100"],
  ["withdraw", "Owner only: money taken out: /withdraw Marco 50"],
  ["fix", "Owner only: correct a price: /fix 12 paid 45 or /fix 12 sold 80"],
  ["undo", "Owner only: cancel a deposit, withdrawal or fix with a new entry: /undo 7"],
  ["split", "Owner only: how profit is shared: /split contribution or /split equal"],
  ["allow", "Owner only: first step for a new member: /allow 123456789, then /setrole"],
  ["intro", "Owner only: post or update the pinned intro in every topic"],
];
const OWNER_ONLY = new Set(["allow", "intro", "deposit", "withdraw", "fix", "undo", "split", "setrole", "removerole", "delnote", "setname", "remind"]);
export const LOCKED = "🔒 You're not a member of FLIP MAFIA";
const IN_GROUP = new Set(["member", "administrator", "creator", "restricted"]);
// the fast lane's settings (the scanner has the same defaults in flipfinder/fastlane.py)
export const FAST_DEFAULTS = { enabled: false, interval: 2, groups: ["Guitars", "Amps", "Pedals", "Electronics", "Audio"],
  per_run: 8, backoff_until: 0 };
const FAST_BACKOFF = 3600;

export function claimerName(user) {
  const name = [user.first_name, user.last_name].filter(Boolean).join(" ");
  return name || (user.username ? `@${user.username}` : `user ${user.id}`);
}

export class Bot {
  /** store: Store; tg: Telegram; settings from the store; now in seconds. */
  constructor({ store, tg, ownerId, settings, now, fetchFn, geckoKey, aiKey }) {
    Object.assign(this, { store, tg, settings, now });
    this.loaded = structuredClone(settings || {});   // as read: save() writes back only what this run changed
    this.fetchFn = fetchFn || ((...a) => fetch(...a));   // CoinGecko lookups for /watch
    this.geckoKey = geckoKey || "";
    this.ownerId = Number(ownerId);
    this.changed = false;          // settings changed: saved at the end
    this.capture = null;           // the Mini App: replies and pop-ups collected here instead of sent
    this.team = new Team(this);
    this.handbook = new Handbook(this);
    this.ai = new AI(this, aiKey);
    this.cleanup = [];   // {chat, id}: commands and replies in the Rules topic, deleted 10 s later
    this.rules = null;   // {chat, thread} while answering a message in the Rules topic
    this._deals = null;
  }

  /** Only people with a role (the owner always has one) can use the bot, the app and the group. */
  allowed(user) {
    return this.team.hasRole(user);
  }

  /** On the /allow list (the first step), with or without a role yet. */
  listed(user) {
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
    const rules = this.rules && String(this.rules.chat) === String(chat) ? this.rules : null;
    if (rules) payload.message_thread_id = rules.thread;   // answers stay in the Rules topic, briefly
    const sent = await this.tg.call("sendMessage", payload);
    if (rules && sent?.message_id) this.cleanup.push({ chat: String(chat), id: sent.message_id });
    return sent;
  }

  /** Answering a message in the Rules topic: the command and our answer go away after 10 s. */
  enterRules(msg, isCommand) {
    const thread = this.settings.topics?.rules;
    if (!thread || !msg.is_topic_message || msg.message_thread_id !== thread) return;
    this.rules = { chat: msg.chat.id, thread };
    if (isCommand) this.cleanup.push({ chat: String(msg.chat.id), id: msg.message_id });
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
    if (this.changed) {
      // Another request (the 5-min job, a command) may have saved settings since we read them:
      // start from what's stored now and apply only the parts this run changed, so nothing is lost
      const fresh = await this.store.settings();
      for (const k of new Set([...Object.keys(this.settings), ...Object.keys(this.loaded)])) {
        if (JSON.stringify(this.settings[k]) === JSON.stringify(this.loaded[k])) continue;
        if (this.settings[k] === undefined) delete fresh[k];
        else fresh[k] = this.settings[k];
      }
      await this.store.put("settings", fresh);
      this.loaded = structuredClone(fresh);
      Object.assign(this.settings, fresh);
      this.changed = false;
    }
    for (const [, newId] of moved) {
      await this.tg.sendText(`ℹ️ Your Telegram group was upgraded to a supergroup, so its chat id changed to ` +
        `<code>${newId}</code>. flipFinder switched to it by itself.`, { chats: [String(this.ownerId)] });
    }
    this.tg.newMigrations = {};
  }

  async handle(update) {
    try {
      const m = update.message;
      if (m) await this.seeInGroup(m);
      if (update.chat_join_request) await this.onJoinRequest(update.chat_join_request);
      else if (update.chat_member) await this.onMemberChange(update.chat_member);
      else if (update.callback_query) await this.onButton(update.callback_query);
      else if (m?.pinned_message && m.is_topic_message && m.message_thread_id === this.settings.topics?.rules) {
        // only the handbook is pinned there: no "pinned a message" notices
        await this.tg.call("deleteMessage", { chat_id: m.chat.id, message_id: m.message_id });
      } else if (m) await this.onMessage(m);
    } finally {
      await this.save();
    }
  }

  // --- messages
  async onMessage(msg) {
    const text = (msg.text || "").trim();
    const user = msg.from?.id;
    const chat = msg.chat.id;
    if (!this.allowed(user)) {
      if (this.listed(user)) this.team.learn(msg.from);   // their name, for /setrole
      // in the group: ignore quietly; in a private chat: one short answer
      if (msg.chat.type === "private" || (!Telegram.isGroup(chat) && chat === user)) await this.reply(chat, LOCKED);
      return;
    }
    this.team.learn(msg.from);
    this.learnTopic(msg);
    this.enterRules(msg, text.startsWith("/"));
    if (!text.startsWith("/")) {
      if (await this.aiMessage(msg, text)) return;
      return this.onPriceReply(chat, user, text, msg);
    }
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

  async cmd_demand(chat, args) {
    if (!args.length) throw new UserError("Which model? e.g. /demand iphone 13 128gb or /demand boss ds 1");
    const table = await this.store.get("demand");
    if (!table?.searches) throw new UserError("No demand numbers yet: they come with the next scans (every 30 min)");
    const flat = (x) => x.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");
    const words = args.join(" ").toLowerCase().split(/\s+/).map(flat).filter(Boolean);
    const has = (w) => words.some((x) => x === w || (w.length > 2 && x.includes(w)) || (x.length > 2 && w.includes(x)));
    // the search whose words best match what was asked
    const scored = Object.keys(table.searches).map((q) => {
      const qw = q.split(/\s+/).map(flat).filter(Boolean);
      return [q, qw.filter(has).length / qw.length, qw.length];
    }).sort((a, b) => b[1] - a[1] || b[2] - a[2]);
    let query = scored[0]?.[1] >= 0.5 ? scored[0][0] : closeMatches(args.join(" ").toLowerCase(), Object.keys(table.searches))[0];
    if (!query) throw new UserError(`No search like "${args.join(" ")}". /categories lists them`);
    const s = table.searches[query];
    // model rows matching the extra words first (e.g. "128gb")
    const extra = words.filter((w) => !query.split(/\s+/).map(flat).includes(w));
    const rows = [...s.models].map((r) => [r, extra.filter((w) => r.model.split(/\s+/).map(flat).includes(w)).length])
      .sort((a, b) => b[1] - a[1] || b[0].listed - a[0].listed).map(([r]) => r);
    const updated = table.updated ? Math.round((this.now - table.updated) / 60) : null;
    const lines = [`📊 <b>Demand: ${esc(query)}</b>`, `All models: ${esc(s.all)}`];
    for (const r of rows.slice(0, 5)) lines.push(`· <b>${esc(r.model)}</b>: ${esc(r.text)}`);
    lines.push("", `<i>Tracked ${Math.round(s.tracked_days)} days${updated !== null ? ` · updated ${updated} min ago` : ""}. ` +
      "Sold per week is an estimate: we check a sample of the listings that leave Vinted's search.</i>");
    await this.reply(chat, lines.join("\n"));
  }

  // --- the Crypto topic's watchlist (read-only news; any allowed member)
  async cmd_watch(chat, args) {
    const list = watchlist(this.settings);
    const show = (l) => l.map((c) => c.symbol).join(", ") || "empty";
    if (!args.length) return this.reply(chat, `🪙 Crypto watchlist: ${esc(show(list))}
Add one with /watch link, remove with /unwatch sol`);
    if (list.length >= MAX_WATCH) throw new UserError(`The watchlist is full (${MAX_WATCH} coins). /unwatch one first`);
    const coin = await findCoin(this.fetchFn, args.join(" "), this.geckoKey);
    if (!coin) throw new UserError(`CoinGecko doesn't know "${args.join(" ")}". Try its symbol (e.g. LINK) or full name`);
    if (list.some((c) => c.id === coin.id)) return this.reply(chat, `${esc(coin.symbol)} is already on the watchlist`);
    this.settings.crypto_watch = [...list, coin];
    this.changed = true;
    await this.reply(chat, `✅ Watching ${esc(coin.name)} (${esc(coin.symbol)}). Watchlist: ${esc(show(this.settings.crypto_watch))}`);
  }

  async cmd_unwatch(chat, args) {
    if (!args.length) throw new UserError("Which coin? e.g. /unwatch sol");
    const q = args.join(" ").toLowerCase();
    const list = watchlist(this.settings);
    const coin = list.find((c) => [c.symbol.toLowerCase(), c.id, c.name.toLowerCase()].includes(q));
    if (!coin) throw new UserError(`"${args.join(" ")}" isn't on the watchlist (${list.map((c) => c.symbol).join(", ")})`);
    this.settings.crypto_watch = list.filter((c) => c.id !== coin.id);
    this.changed = true;
    const rest = this.settings.crypto_watch.map((c) => c.symbol).join(", ") || "empty";
    await this.reply(chat, `✅ Stopped watching ${esc(coin.symbol)}. Watchlist: ${esc(rest)}`);
  }

  // --- the team: roles, duty, tasks
  async cmd_roles(chat) {
    await this.reply(chat, this.team.rolesText());
  }

  async cmd_setrole(chat, args, user) {
    if (user !== this.ownerId) throw new UserError("Only the owner can use /setrole");
    if (args.length < 2) throw new UserError(`Use /setrole <name> <role>. Roles: ${Object.keys(ROLES).join(", ")}, or none`);
    const name = args.slice(0, -1).join(" ");
    const role = args.at(-1).toLowerCase();
    if (role === "none") return this.takeRole(chat, name);
    await this.reply(chat, this.team.setRole(name, role));
  }

  async cmd_remind(chat, args, user) {
    if (user !== this.ownerId) throw new UserError("Only the owner can use /remind");
    const a = (args[0] || "").toLowerCase();
    if (a !== "on" && a !== "off") {
      return this.reply(chat, `⏰ The "not claimed yet" digest is ${this.settings.remind ? "on" : "off"}. /remind on or /remind off`);
    }
    this.settings.remind = a === "on";
    this.changed = true;
    await this.reply(chat, a === "on" ? "⏰ Digest on: deals rated 7+ nobody claimed, at most every 30 min, 08:30-22:00"
      : "⏰ Digest off: no more \"not claimed yet\" messages");
  }

  async cmd_setname(chat, args, user) {
    if (user !== this.ownerId) throw new UserError("Only the owner can use /setname");
    if (args.length < 2) throw new UserError("Use /setname <id> <name>, e.g. /setname 123456789 Anna");
    await this.reply(chat, this.team.setName(args[0], args.slice(1).join(" ")));
  }

  async cmd_removerole(chat, args, user) {
    if (user !== this.ownerId) throw new UserError("Only the owner can use /removerole");
    if (!args.length) throw new UserError("Use /removerole <name>");
    return this.takeRole(chat, args.join(" "));
  }

  /** Role gone: blocked at once; if they're in the group, ask the owner whether to remove them too. */
  async takeRole(chat, name) {
    const m = this.team.removeRole(name);
    await this.reply(chat, `🔒 ${esc(m.name)} no longer has a role: the bot and the app are closed to them from now on.`);
    for (const group of this.tg.groups) {
      const cm = await this.tg.call("getChatMember", { chat_id: group, user_id: m.id });
      if (!IN_GROUP.has(cm?.status)) continue;
      await this.tg.call("sendMessage", { chat_id: this.ownerId, parse_mode: "HTML",
        text: `Remove <b>${esc(m.name)}</b> from the group too?`,
        reply_markup: { inline_keyboard: [[{ text: "Yes, remove", callback_data: `kick:${m.id}:y` },
          { text: "No, keep", callback_data: `kick:${m.id}:n` }]] } });
      break;
    }
  }

  /** "Remove from the group too?": out (ban + unban at once, so they can ask to join again later). */
  async onKick(cq, chat, msg, user, rest) {
    if (user !== this.ownerId) return this.answer(cq, "Only the owner decides that");
    const [id, yes] = rest.split(":");
    const uid = Number(id);
    const name = this.team.member(uid)?.name || `user ${uid}`;
    const done = (text) => this.tg.call("editMessageText", { chat_id: chat, message_id: msg.message_id, text, parse_mode: "HTML" });
    if (yes !== "y") {
      await done(`👍 ${esc(name)} stays in the group (without a role they can't use the bot or the app)`);
      return this.answer(cq, "Kept");
    }
    if (this.team.hasRole(uid)) {
      await done(`ℹ️ ${esc(name)} has a role again, so I didn't remove them`);
      return this.answer(cq, "Not removed");
    }
    let ok = true;
    for (const group of this.tg.groups) {
      const r = await this.tg.request("banChatMember", { chat_id: group, user_id: uid, revoke_messages: false });
      if (!r.ok) {
        ok = false;
        await done(`⚠️ Couldn't remove ${esc(name)}: ${esc(r.error)}. The bot needs the "Ban users" admin right.`);
        continue;
      }
      await this.tg.call("unbanChatMember", { chat_id: group, user_id: uid, only_if_banned: true });
    }
    if (ok) await done(`🚪 ${esc(name)} is out of the group. They can ask to join again; I'll only let them in with a role.`);
    return this.answer(cq, ok ? "Removed" : "Couldn't remove");
  }

  // --- the AI (Claude): read-only checks and answers; members with a role only (checked before this)
  async onAiButton(cq, chat, msg, user, kind, key) {
    if (!this.ai.config.enabled) return this.answer(cq, "🧠 The AI is switched off");
    const d = await this.store.deal(key);
    if (!d) return this.answer(cq, "That deal is gone");
    const deep = kind === "aid";
    if (deep ? d.ai_deep : d.ai) return this.answer(cq, deep ? "Deep analysis done: see the reply" : "Already checked: see the 🧠 reply under the deal");
    const busy = deep ? "ai_deep_busy" : "ai_busy";
    if (d[busy] && this.now - d[busy] < 90) return this.answer(cq, "Already on it, one moment");
    d[busy] = this.now;
    await this.store.saveDeal(key, d);
    await this.answer(cq, deep ? "🧠 Deep analysis… up to a minute" : "🔍 Checking… a few seconds");
    const r = deep ? await this.ai.deep(key, d, user) : await this.ai.check(key, d, { user });
    if (r.error) {
      d[busy] = 0;
      await this.store.saveDeal(key, d);
      await this.tg.sendTo(chat, r.error, { replyTo: msg.message_id });
    }
  }

  /** The deal a message belongs to (the alert or its AI check), if any. */
  async dealByMessage(chat, id) {
    const same = (m) => String(m.chat) === String(chat) && m.id === id;
    return (await this.allDeals()).find(([, d]) => (d.messages || []).some(same) || (d.ai?.msgs || []).some(same)) || null;
  }

  async botUsername() {
    if (!this.settings.bot_username) {
      const me = await this.tg.call("getMe", {});
      if (!me?.username) return null;
      this.settings.bot_username = me.username;
      this.changed = true;
    }
    return this.settings.bot_username;
  }

  /** Text that isn't a command: a reply to a deal, an @mention, or a photo in a private chat goes to the AI. */
  async aiMessage(msg, text) {
    if (!this.ai.config.enabled) return false;
    const chat = msg.chat.id;
    const user = msg.from?.id;
    const pending = (await this.store.get("pending", {}))[String(user)];
    if (pending && String(pending.chat) === String(chat)) return false;   // they're answering the bot's question
    const isPrivate = msg.chat.type === "private" || (!Telegram.isGroup(chat) && chat === user);
    if (msg.photo && isPrivate) {
      const image = await this.ai.photoBlock(msg.photo);
      if (!image) {
        await this.reply(chat, "⚠️ I couldn't open that photo, try again");
        return true;
      }
      await this.ai.ask(chat, user, (msg.caption || "").trim(), { image, replyTo: msg.message_id });
      return true;
    }
    if (!text) return false;
    if (msg.reply_to_message) {
      const found = await this.dealByMessage(chat, msg.reply_to_message.message_id);
      if (found) {
        await this.ai.answerAboutDeal(chat, user, found[0], found[1], text, msg.message_id);
        return true;
      }
    }
    if (text.includes("@")) {
      const name = await this.botUsername();
      const tag = name ? new RegExp(`@${name.replace(/[^\w]/g, "")}\\b`, "ig") : null;
      if (tag && tag.test(text)) {
        await this.ai.ask(chat, user, text.replace(tag, "").trim() || "Hi", { replyTo: msg.message_id });
        return true;
      }
    }
    return false;
  }

  async cmd_ask(chat, args, user, msg) {
    if (!this.ai.config.enabled) throw new UserError("🧠 The AI is switched off");
    const q = (msg?.text || args.join(" ")).replace(/^\/ask(@\S+)?\s*/i, "").trim();
    if (!q) throw new UserError("Ask something, e.g. /ask is a Boss DS-1 for €30 a good buy?");
    await this.ai.ask(chat, user, q, { replyTo: msg?.message_id });
  }

  async cmd_ai(chat, args, user) {
    const [a = "", b = ""] = args.map((x) => x.toLowerCase());
    if (!a) return this.reply(chat, await this.ai.usageText());
    if (user !== this.ownerId) throw new UserError("Only the owner can change the AI settings");
    const number = (lo, hi) => {
      const x = parseNumber(b);
      if (x === null || x < lo || x > hi) throw new UserError(`Give a number from ${lo} to ${hi}`);
      return x;
    };
    if (a === "on" || a === "off") {
      this.ai.set({ enabled: a === "on" });
      return this.reply(chat, `🧠 AI is now ${a}` + (a === "on" && !this.ai.apiKey ? " (but no API key is set up yet)" : "") +
        (a === "on" ? ". New deals get a 🔍 Check with AI button." : ". Deals go out exactly as before."));
    }
    if (a === "auto" && (b === "on" || b === "off")) {
      this.ai.set({ auto: b === "on" });
      return this.reply(chat, b === "on" ? `🧠 New deals rated ${this.ai.config.min_rating}+ are checked automatically`
        : "🧠 Checks run only when someone taps 🔍 Check with AI");
    }
    if (a === "model" || a === "deepmodel") {
      if (!PRICES[b]) throw new UserError(`Models I know the prices of: ${Object.keys(PRICES).join(", ")}`);
      this.ai.set({ [a === "model" ? "model" : "deep_model"]: b });
      return this.reply(chat, `🧠 ${a === "model" ? "Checks and questions" : "Deep analysis"} now use ${b}`);
    }
    if (a === "cap") {
      this.ai.set({ daily_cap_eur: number(0, 20) });
      return this.reply(chat, `🧠 Daily AI spend cap: ${euro(this.ai.config.daily_cap_eur)}`);
    }
    if (a === "limit") {
      this.ai.set({ user_daily: Math.round(number(1, 500)) });
      return this.reply(chat, `🧠 Up to ${this.ai.config.user_daily} AI questions per person a day`);
    }
    if (a === "minrating") {
      this.ai.set({ min_rating: Math.round(number(1, 10)) });
      return this.reply(chat, `🧠 Automatic checks for deals rated ${this.ai.config.min_rating}+`);
    }
    throw new UserError("Use /ai on|off, /ai auto on|off, /ai cap 0.5, /ai limit 20, /ai minrating 6, /ai model <id>, /ai deepmodel <id>");
  }

  async cmd_aiusage(chat) {
    await this.reply(chat, await this.ai.usageText());
  }

  async cmd_note(chat, args, user, msg) {
    const text = (msg?.text || args.join(" ")).replace(/^\/note(@\S+)?\s*/i, "").trim();
    if (!text) throw new UserError("Write the lesson, e.g. /note Strats from seller X were fake");
    const id = await this.ai.addNote(text, this.team.member(user)?.name || "");
    await this.reply(chat, `📝 Note ${id} saved. The AI takes it into account from now on.`);
  }

  async cmd_notes(chat) {
    const notes = await this.ai.notes();
    if (!notes.length) return this.reply(chat, "📝 No notes yet. Add one with /note &lt;lesson&gt;");
    await this.reply(chat, ["📝 <b>Notes for the AI</b>", ...notes.map((x) => `${x.id}. ${esc(x.text)}`)].join("\n"));
  }

  async cmd_delnote(chat, args, user) {
    if (user !== this.ownerId) throw new UserError("Only the owner can remove notes");
    const id = Number(args[0]);
    if (!Number.isInteger(id)) throw new UserError("Which note? e.g. /delnote 3 (/notes lists them)");
    await this.ai.delNote(id);
    await this.reply(chat, `🗑 Note ${id} removed`);
  }

  // --- the fast lane: settings for the scanner's quick passes between full scans
  fastSettings() {
    return { ...FAST_DEFAULTS, ...(this.settings.fast || {}) };
  }

  async cmd_fast(chat, args, user) {
    const [a = "", ...rest] = args.map((x) => x.toLowerCase());
    if (a && user !== this.ownerId) throw new UserError("Only the owner can change the fast lane");
    const set = (changes) => {
      this.settings.fast = { ...(this.settings.fast || {}), ...changes };
      this.changed = true;
    };
    if (a === "off") {
      set({ enabled: false });
      return this.reply(chat, "⚡ Fast lane off. The full scan still runs every 5 min.");
    }
    if (/^\d+$/.test(a)) {
      const n = Number(a);
      if (n < 1 || n > 10) throw new UserError("Every 1 to 10 minutes, e.g. /fast 2");
      set({ enabled: true, interval: n });
      return this.reply(chat, `⚡ Fast lane on: the newest listings every ${n} min (${this.fastSettings().per_run} searches a pass)`);
    }
    if (a === "per") {
      const n = Number(rest[0]);
      if (!Number.isInteger(n) || n < 1 || n > 20) throw new UserError("1 to 20 searches a pass, e.g. /fast per 8");
      set({ per_run: n });
      return this.reply(chat, `⚡ ${n} searches a pass (about ${n * 4} s)`);
    }
    if (a === "groups") {
      const known = [...new Set((await this.view()).searches.map((x) => x.group))];
      const want = rest.join(" ").split(/[,\s]+/).filter(Boolean);
      const groups = want.map((w) => known.find((g) => g.toLowerCase() === w)).filter(Boolean);
      if (!groups.length || groups.length !== want.length) {
        throw new UserError(`Groups: ${known.join(", ")}. E.g. /fast groups guitars, amps, electronics`);
      }
      set({ groups });
      return this.reply(chat, `⚡ Fast lane searches: ${groups.join(", ")} (in that order)`);
    }
    if (a) throw new UserError("Use /fast 2, /fast off, /fast per 8, /fast groups guitars, electronics");
    await this.reply(chat, await this.fastText());
  }

  async fastText() {
    const fs = this.fastSettings();
    const runs = (await this.store.get("fast_stats", [])).filter((r) => this.now - r.at < 3600);
    const avg = (k) => (runs.length ? runs.reduce((s, r) => s + (r[k] || 0), 0) / runs.length : 0);
    const lines = [`⚡ <b>Fast lane</b>: ${fs.enabled ? `on, every ${fs.interval} min` : "off"}`,
      `${fs.per_run} searches a pass from: ${esc(fs.groups.join(", "))}`];
    if (fs.backoff_until > this.now) lines.push(`⚠️ Backing off after a Vinted block: every 5 min until ${hhmm(fs.backoff_until)}`);
    lines.push(runs.length
      ? `Last hour: ${runs.length} passes, ${avg("secs").toFixed(1)} s each on average, ` +
        `${Math.round(runs.reduce((s, r) => s + r.requests, 0))} Vinted page loads, ` +
        `${runs.reduce((s, r) => s + r.blocked, 0)} blocked, ${runs.reduce((s, r) => s + r.sent, 0)} deal(s) sent`
      : "No passes in the last hour");
    lines.push("", "The full scan (every 5 min), eBay (every 20 min) and Subito don't change.");
    return lines.join("\n");
  }

  /** A fast pass finished: keep its numbers; a Vinted block slows it to every 5 min for an hour (told once). */
  async fastReport(r) {
    const stats = await this.store.get("fast_stats", []);
    stats.push({ at: this.now, secs: Number(r.secs) || 0, requests: Number(r.requests) || 0, blocked: Number(r.blocked) || 0,
      searches: Number(r.searches) || 0, checked: Number(r.checked) || 0, deals: Number(r.deals) || 0, sent: Number(r.sent) || 0 });
    await this.store.put("fast_stats", stats.slice(-300));
    // when the last pass started and how many ran: the scanner's "too soon?" check and rotation
    const state = await this.store.get("fast_state", { last_started: 0, passes: 0 });
    await this.store.put("fast_state", { last_started: Number(r.started) || this.now, passes: (state.passes || 0) + 1 });
    const fs = this.fastSettings();
    if (!(Number(r.blocked) > 0) || fs.backoff_until > this.now) return { backoff: fs.backoff_until > this.now };
    this.settings.fast = { ...(this.settings.fast || {}), backoff_until: this.now + FAST_BACKOFF };
    this.changed = true;
    await this.tg.call("sendMessage", { chat_id: this.ownerId, parse_mode: "HTML",
      text: `⚠️ <b>Fast lane</b>: Vinted refused ${Number(r.blocked)} request(s) (403/429). It slows to every 5 min ` +
        `for an hour, then goes back to every ${fs.interval} min by itself. The full scan carries on as usual.` });
    return { backoff: true };
  }

  // --- the group: join requests, who's in it
  /** Join requests: in with a role, otherwise declined and the owner told. */
  async onJoinRequest(r) {
    const group = String(r.chat?.id);
    if (!this.tg.groups.includes(group)) return;
    const u = r.from || {};
    if (this.listed(u.id)) this.team.learn(u);
    const name = [u.first_name, u.last_name].filter(Boolean).join(" ") || (u.username ? `@${u.username}` : "someone");
    if (this.allowed(u.id)) {
      await this.tg.call("approveChatJoinRequest", { chat_id: group, user_id: u.id });
      await this.rememberMember(u, true);
      return;
    }
    await this.tg.call("declineChatJoinRequest", { chat_id: group, user_id: u.id });
    await this.tg.call("sendMessage", { chat_id: this.ownerId, parse_mode: "HTML",
      text: `❗ ${esc(name)} (<code>${u.id}</code>) asked to join, no role\n` +
        `To let them in: /allow ${u.id}, then /setrole ${u.id} buyer|seller|manager, and ask them to request again.` });
  }

  /** Someone joined or left (Telegram's chat_member updates). */
  async onMemberChange(cm) {
    if (!this.tg.groups.includes(String(cm.chat?.id))) return;
    const u = cm.new_chat_member?.user;
    if (u && !u.is_bot) await this.rememberMember(u, IN_GROUP.has(cm.new_chat_member.status));
  }

  /** Everyone who writes in the group, joins or leaves: so the daily check knows who's there. */
  async seeInGroup(m) {
    if (!this.tg.groups.includes(String(m.chat?.id))) return;
    for (const u of m.new_chat_members || []) if (!u.is_bot) await this.rememberMember(u, true);
    if (m.left_chat_member && !m.left_chat_member.is_bot) await this.rememberMember(m.left_chat_member, false);
    if (m.from && !m.from.is_bot) await this.rememberMember(m.from, true);
  }

  async rememberMember(u, inside) {
    const seen = await this.groupSeen();
    const name = [u.first_name, u.last_name].filter(Boolean).join(" ") || (u.username ? `@${u.username}` : "");
    if (inside ? seen[u.id] === name : !(u.id in seen)) return;
    if (inside) seen[u.id] = name;
    else delete seen[u.id];
    await this.store.put("group_seen", seen);
  }

  async groupSeen() {
    this._seen ??= await this.store.get("group_seen", {});
    return this._seen;
  }

  /**
   * Once a day: who's in the group without a role? The owner gets a list; nobody is removed.
   * Telegram doesn't list a group's members, so this checks everyone the bot has ever seen there
   * and says how many it can't identify.
   */
  async memberCheck(maxLookups = 30) {
    const lines = [];
    for (const group of this.tg.groups) {
      const count = await this.tg.call("getChatMemberCount", { chat_id: group });
      const list = await this.tg.call("getChatAdministrators", { chat_id: group });
      const admins = Array.isArray(list) ? list : [];
      const inside = new Map(admins.map((a) => [a.user.id, a.user]));
      const seen = await this.groupSeen();
      const s = this.settings;
      const candidates = [...new Set([...Object.keys(seen), ...(s.allowed_users || []), ...Object.keys(s.roles || {}),
        ...Object.keys(s.people || {})].map(Number))].filter((id) => !inside.has(id)).slice(0, maxLookups);
      let changed = false;
      for (const id of candidates) {
        const cm = await this.tg.call("getChatMember", { chat_id: group, user_id: id });
        if (!cm?.status) continue;
        if (IN_GROUP.has(cm.status)) inside.set(id, cm.user);
        else if (id in seen) {
          delete seen[id];
          changed = true;
        }
      }
      if (changed) await this.store.put("group_seen", seen);
      const noRole = [...inside.values()].filter((u) => !u.is_bot && !this.allowed(u.id));
      const unknown = typeof count === "number" ? Math.max(0, count - inside.size) : 0;
      for (const u of noRole) {
        const name = [u.first_name, u.last_name].filter(Boolean).join(" ") || (u.username ? `@${u.username}` : "?");
        lines.push(`· ${esc(name)} (<code>${u.id}</code>)`);
      }
      if (unknown) lines.push(`· ${unknown} more I can't identify (they haven't written since I joined): check the member list`);
    }
    if (!lines.length) return null;
    const text = ["👥 <b>Daily member check</b>: in the group without a role", ...lines, "",
      "Nobody was removed. Give them a role with /setrole, or remove them from the group."].join("\n");
    await this.tg.call("sendMessage", { chat_id: this.ownerId, parse_mode: "HTML", text });
    return text;
  }

  async cmd_duty(chat) {
    await this.reply(chat, await this.team.pinText(), this.team.pinButtons().inline_keyboard);
  }

  async cmd_task(chat, args, user, msg) {
    const sub = (args[0] || "").toLowerCase();
    if (sub === "done") {
      if (!/^\d+$/.test(args[1] || "")) throw new UserError("Which task? e.g. /task done 3");
      const text = await this.team.doneTask(args[1], user);
      await this.reply(chat, text);
      return;
    }
    if (sub !== "add") throw new UserError("Use /task add <text> @name [by fri] or /task done <n>. /tasks lists them");
    const raw = (msg?.text || "").replace(/^\/task(@\w+)?\s+add\s*/i, "");
    // entities point into the whole message: shift them to the part after "/task add "
    const shift = (msg?.text || "").length - raw.length;
    const entities = (msg?.entities || []).map((e) => ({ ...e, offset: e.offset - shift }));
    const t = await this.team.addTask(user, raw, entities);
    const who = this.team.member(t.who);
    await this.reply(chat, `📝 Task ${t.n} for ${who ? `<a href="tg://user?id=${who.id}">${esc(who.name)}</a>` : "?"}: ` +
      `${esc(t.text)}${t.due ? ` · due ${this.team.taskLine(t).split(" · due ")[1]}` : ""}`);
  }

  async cmd_handbook(chat, args, user, msg) {
    if ((args[0] || "").toLowerCase() !== "edit") {
      if (!this.rules) return void (await this.handbook.send(chat, msg?.is_topic_message ? msg.message_thread_id : null));
      // in the Rules topic it's already pinned: just make sure it still is
      const ok = await this.handbook.ensure(chat, this.rules.thread);
      return this.reply(chat, ok ? "📖 The handbook is pinned at the top: tap 📖 Open handbook" : "📖 No handbook yet");
    }
    if (user !== this.ownerId) throw new UserError("Only the owner can edit the handbook");
    if (!args[1]) throw new UserError("Which section? e.g. /handbook edit buying");
    // the new rules can come in the same message, on the lines after "/handbook edit buying"
    const full = msg?.text || "";
    const nl = full.indexOf("\n");
    if (nl > 0 && full.slice(nl + 1).trim()) return this.editHandbook(chat, args[1], full.slice(nl + 1));
    const s = await this.handbook.find(args[1]);
    const payload = { chat_id: chat, parse_mode: "HTML",
      text: `✏️ Reply to this message with the new rules for <b>${esc(s.title, false)}</b>, one per line ` +
        `(they replace the whole section).\n\nNow:\n${esc(s.text, false)}`,
      reply_markup: { force_reply: true, input_field_placeholder: "1. ...  2. ..." } };
    if (this.rules) payload.message_thread_id = this.rules.thread;
    const sent = await this.tg.call("sendMessage", payload);   // stays until the answer comes
    const pending = await this.store.get("pending", {});
    pending[String(user)] = { handbook: s.key, chat, ask_msg: sent?.message_id ?? null };
    await this.store.put("pending", pending);
  }

  async editHandbook(chat, name, text) {
    const { section } = await this.handbook.edit(name, text, this.now);
    return this.reply(chat, `✅ Updated ${esc(section.title, false)}. The app shows it now`);
  }

  async cmd_tasks(chat) {
    await this.reply(chat, await this.team.tasksText());
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
    await this.reply(chat, `✅ User ${uid} is on the list. They can use the bot once they have a role: ` +
      `/setrole ${uid} buyer|seller|manager`);
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
    if (key === "rules" && !this.rules) this.enterRules(msg, true);   // just became the Rules topic
    const sent = await this.tg.call("sendMessage", { chat_id: chat, message_thread_id: msg.message_thread_id, parse_mode: "HTML",
      text: `✅ This topic is now <b>${TOPIC_NAMES[key]}</b>. Set so far: ${known.join(", ")}` });
    if (key === "rules" && sent?.message_id) this.cleanup.push({ chat: String(chat), id: sent.message_id });
    await this.postIntro(chat, key);
    if (!this.settings.intros?.[String(chat)]?.general) await this.postIntro(chat, "general");
  }

  /**
   * Pins INTROS[key] in its topic ("general" = the main chat). An intro we posted before is edited
   * in place (and pinned again in case someone unpinned it); a new one only when it's gone.
   */
  async postIntro(chat, key) {
    if (key === "rules") return this.handbook.ensure(chat, this.settings.topics?.rules);   // the handbook, one message
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
    const kb = keyboard(key, d, this.ai.config.enabled);
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
      d.votes[String(user)] = vote;
      if (vote === "down") {
        await this.store.addFeedback({ key, title: d.title, url: d.url, by: who, at: this.now, value: d.value ?? null,
          cost: d.cost ?? null, query: d.query || "" });
      }
      await this.store.saveDeal(key, d);
      await this.refreshDeal(key, d);
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
    if (kind === "b" && d.request) return this.answer(cq, `Waiting for the OK on ${euro(d.request.amount)}`);
    const ask = kind === "b" ? "paid" : "sold_for";
    const title = esc(d.title.slice(0, 60));
    const cost = ask === "paid" ? d.cost : null;
    const manager = this.team.isManager(user);
    let q;
    let markup;
    if (cost) {
      // suggest what we know it costs; a different amount can still be typed as a reply
      q = (manager ? `💸 How much did you pay for <b>${title}</b>?` : `🙋 What price did you agree for <b>${title}</b>?`) +
        `\nKnown total (price + buyer fee + shipping/pickup): <b>${euro(cost)}</b>.\n` +
        "Tap ✅ to use it, or reply to this message with a different amount." +
        (manager ? "" : "\nIt goes to the manager for approval.");
      markup = { inline_keyboard: [[{ text: `✅ Use ${euro(cost)}`, callback_data: `pay:${key}` }]] };
    } else if (ask === "paid") {
      q = (manager ? `💸 How much did you pay for <b>${title}</b>?` : `🙋 What price did you agree for <b>${title}</b>?`) +
        " Reply with the amount, e.g. 45";
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

  async onPriceReply(chat, user, text, msg = null) {
    const pending = (await this.store.get("pending", {}))[String(user)];
    if (!pending || String(pending.chat) !== String(chat)) return;   // just chatting
    if (pending.handbook) {
      const all = await this.store.get("pending", {});
      delete all[String(user)];
      await this.store.put("pending", all);
      if (this.rules) {   // the question and the answer go too
        if (msg?.message_id) this.cleanup.push({ chat: String(chat), id: msg.message_id });
        if (pending.ask_msg) this.cleanup.push({ chat: String(chat), id: pending.ask_msg });
      }
      try {
        return await this.editHandbook(chat, pending.handbook, msg?.text || text);
      } catch (e) {
        if (!(e instanceof UserError)) throw e;
        return this.reply(chat, `⚠️ ${esc(e.message, false)}`);
      }
    }
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
    if (pending.ask === "paid" && !this.team.isManager(user)) return this.requestBuy(chat, user, pending.key, d, amount);
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
    if (entry.kind === "buy") await this.pingSellers(d);
  }

  // --- 🙋 Request buy -> ✅ Approve / ❌ Reject (a manager pays every buy)
  payNote(d, amount, who) {
    return d.source === "subito" ? `Subito pickup: send ${esc(who)} ${euro(amount)} for it`
      : `${d.source === "ebay" ? "eBay" : "Vinted"}: pay ${euro(amount)} online`;
  }

  async requestBuy(chat, user, key, d, amount) {
    const who = this.team.member(user)?.name || d.who || "Someone";
    d.request = { amount, by_id: user, by: who, at: this.now, msgs: [] };
    const buttons = { inline_keyboard: [[{ text: "✅ Approve", callback_data: `ap:${key}` },
      { text: "❌ Reject", callback_data: `rj:${key}` }]] };
    const managers = this.team.withRole("manager");
    const text = `🙋 ${esc(who)} asks to buy #${d.n ?? "?"} ${esc(d.title.slice(0, 50))} for <b>${euro(amount)}</b>` +
      ` (known total ${euro(d.cost || amount)})\n${this.payNote(d, amount, who)}`;
    for (const m of managers) {
      const res = await this.tg.sendTo(String(m.id), text, { buttons });
      if (res) d.request.msgs.push({ chat: String(res.chat.id), id: res.message_id });
    }
    const msgs = d.messages || [];
    const target = msgs.find((x) => Telegram.isGroup(x.chat));
    if (target) {
      const tag = managers.map((m) => `<a href="tg://user?id=${m.id}">${esc(m.name)}</a>`).join(" ");
      const res = await this.tg.sendTo(target.chat, `${text}\n${tag}`, { buttons, topic: TOPIC_FOR_GROUP[d.group || ""],
        replyTo: target.id });
      if (res) d.request.msgs.push({ chat: String(res.chat.id), id: res.message_id });
    }
    await this.store.saveDeal(key, d);
    await this.refreshDeal(key, d);
    await this.reply(chat, `🙋 Sent for approval: #${d.n ?? "?"} at ${euro(amount)}. You'll get a message when it's decided.`);
  }

  async decideBuy(cq, user, key, approve) {
    if (!this.team.isManager(user)) return this.answer(cq, "Only a manager approves buys");
    const d = await this.store.deal(key);
    if (!d?.request) return this.answer(cq, "That's already decided");
    const req = d.request;
    delete d.request;
    for (const m of req.msgs || []) {
      await this.tg.call("editMessageReplyMarkup", { chat_id: m.chat, message_id: m.id, reply_markup: { inline_keyboard: [] } });
    }
    const boss = this.team.member(user)?.name || "The manager";
    const n = d.n ?? "?";
    if (!approve) {
      await this.store.saveDeal(key, d);
      await this.refreshDeal(key, d);
      await this.postAbout(d, `❌ ${esc(boss)} didn't approve buying #${n} at ${euro(req.amount)}`);
      await this.team.dm(req.by_id, `❌ ${esc(boss)} didn't approve buying #${n} at ${euro(req.amount)}. The claim is still yours.`);
      return this.answer(cq, "Rejected");
    }
    Object.assign(d, { status: "bought", paid: req.amount, bought_at: this.now, approved_by: user });
    await this.store.saveDeal(key, d);
    await this.refreshDeal(key, d);
    await this.postAbout(d, `✅ ${esc(boss)} approved #${n} at ${euro(req.amount)}. ${this.payNote(d, req.amount, req.by)}`);
    await this.team.dm(req.by_id, `✅ Approved: #${n} at ${euro(req.amount)}. ${this.payNote(d, req.amount, req.by)}`);
    this.capture?.push(`✅ Approved #${n} at ${euro(req.amount)}`);
    await this.money({ kind: "buy", amount: -req.amount, n: d.n, deal: key }, undefined);
    await this.pingSellers(d);
    return this.answer(cq, "Approved");
  }

  /** Something was bought: the sellers list it. */
  async pingSellers(d) {
    const sellers = this.team.withRole("seller");
    if (!sellers.length) return;
    const n = d.n ?? "?";
    const tag = sellers.map((m) => `<a href="tg://user?id=${m.id}">${esc(m.name)}</a>`).join(" ");
    await this.postAbout(d, `📦 ${tag}, #${n} ${esc(d.title.slice(0, 50))} is bought: please list it. /sell ${n} writes the listing.`);
    for (const m of sellers) await this.team.dm(m.id, `📦 #${n} ${esc(d.title.slice(0, 50))} is bought: please list it. /sell ${n} writes the listing.`);
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
    if (!this.allowed(user)) {   // no role: a short note in a private chat, nothing at all in the group
      if (msg.chat?.type === "private") await this.answer(cq, LOCKED);
      return;
    }
    this.team.learn(cq.from);
    if (data === "claim" || data === "claimed") return this.onClaim(cq, msg, chat, data);
    const colon = data.indexOf(":");
    const kind = colon < 0 ? data : data.slice(0, colon);
    const rest = colon < 0 ? "" : data.slice(colon + 1);
    if (kind === "pay") return this.onUseCost(cq, chat, user, rest);
    if (kind === "kick") return this.onKick(cq, chat, msg, user, rest);
    if (kind === "ai" || kind === "aid") return this.onAiButton(cq, chat, msg, user, kind, rest);
    if (kind === "ap" || kind === "rj") return this.decideBuy(cq, user, rest, kind === "ap");
    if (kind === "wait") return this.answer(cq, "Waiting for the manager's OK");
    if (kind === "duty") {
      if (rest === "on") return this.answer(cq, await this.team.start(user));
      if (rest === "off") return this.answer(cq, await this.team.end(user));
      await this.tg.sendTo(chat, `🙋 ${esc(this.team.member(user)?.name || "")}, how long do you need covered?`,
        { buttons: this.team.swapChoices(user), replyTo: msg.message_id });
      return this.answer(cq, "Pick how long");
    }
    if (kind === "swr") {
      const [len, owner] = rest.split(":");
      if (Number(owner) !== user) return this.answer(cq, "That's someone else's question");
      const end = len === "rest" ? romeTs(rome(this.now).date, 22) : Math.min(this.now + Number(len) * 3600, romeTs(rome(this.now).date, 22));
      await this.tg.call("editMessageReplyMarkup", { chat_id: chat, message_id: msg.message_id, reply_markup: { inline_keyboard: [] } });
      return this.answer(cq, await this.team.requestSwap(user, this.now, end));
    }
    if (kind === "swt") return this.answer(cq, await this.team.takeSwap(Number(rest), user));
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
