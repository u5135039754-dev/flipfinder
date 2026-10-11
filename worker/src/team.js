// Team management: roles, duty (08:00-22:00 Italy time), swaps, the weekly schedule, tasks,
// and the team part of the Sunday report. All of it lives in the private D1 storage:
//   settings.people {id: name} (learned from Telegram), settings.roles {id: role},
//   kv "duty" (who's on, the log, the pinned message, swaps), kv "schedule" {date: {hour: id}},
//   kv "tasks", kv "health" (per-day scan counts for the report).

import { Telegram } from "./telegram.js";
import { TOPIC_FOR_GROUP } from "./group.js";
import { DAY, UserError, addDays, esc, hhmm, rome, romeTs } from "./util.js";

export const ROLES = {
  manager: "manager + bot + money (approves and pays all buys)",
  buyer: "buying + main deal watcher",
  seller: "selling + profiles (photos, listings, buyer messages, shipping)",
};
export const DUTY_FROM = 8;
export const DUTY_TO = 22;
export const PING_FROM_MIN = 7 * 60 + 30;   // "nobody on duty" pings from 07:30
export const NOBODY_EVERY = 30 * 60;
export const UNCLAIMED_AFTER = 10 * 60;
// "not claimed yet" digest: one message, at most every 30 min, 08:30-22:00 Italy time (the night queue
// goes out at 08:00, so its first digest is at 08:30), only deals rated 7+, each deal once, only fresh ones
export const DIGEST_EVERY = 30 * 60;
export const DIGEST_FROM = 8 * 60 + 30;
export const DIGEST_TO = 22 * 60;
export const DIGEST_MIN_RATING = 7;
export const DIGEST_MAX_AGE = 3 * 3600;
const DIGEST_LIST = 12;
export const BUYER_TARGET = 8;              // hours a day for the main watcher
export const OTHERS_CAP = Math.round((DUTY_TO - DUTY_FROM) * 0.2 * 10) / 10;   // 2.8 h: 20% of the 14 h
export const TASK_LATE_DAYS = 3;
// a colour slot per person: the app maps it to the validated categorical palette (light/dark)
export const SLOTS = 6;

const mention = (m) => `<a href="tg://user?id=${m.id}">${esc(m.name)}</a>`;
const hours = (x) => (Math.round(x * 10) / 10).toString();
const WEEKDAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];

export class Team {
  constructor(bot) {
    this.bot = bot;
  }

  get now() {
    return this.bot.now;
  }

  // --- people and roles
  /** Everyone who can use the bot: the owner and /allow-ed users, with names and roles. */
  members() {
    const s = this.bot.settings;
    const ids = [this.bot.ownerId, ...(s.allowed_users || [])];
    return [...new Set(ids.map(Number))].map((id, i) => ({
      id, name: this.nameOf(id),
      role: s.roles?.[id] || (id === this.bot.ownerId ? "manager" : ""), slot: i % SLOTS,
    }));
  }

  member(id) {
    return this.members().find((m) => m.id === Number(id)) || null;
  }

  find(text) {
    const q = text.trim().replace(/^@/, "").toLowerCase();
    if (/^\d+$/.test(q)) return this.member(q);
    const all = this.members();
    return all.find((m) => m.name.toLowerCase() === q) ||
      all.find((m) => m.name.toLowerCase().split(/\s+/)[0] === q) ||
      all.find((m) => m.name.toLowerCase().startsWith(q)) || null;
  }

  withRole(role) {
    return this.members().filter((m) => m.role === role);
  }

  /** Duty, pings and the pinned message start once the owner has set a role with /setrole. */
  get active() {
    return Object.keys(this.bot.settings.roles || {}).length > 0;
  }

  /** Roles are the only key: the owner always has one, everyone else needs /setrole. */
  hasRole(id) {
    if (Number(id) === this.bot.ownerId) return true;
    const role = this.bot.settings.roles?.[Number(id)];
    return Boolean(role && ROLES[role]);
  }

  isManager(id) {
    return Number(id) === this.bot.ownerId || this.member(id)?.role === "manager";
  }

  /** The name shown everywhere: the one the owner set (/setname), else Telegram's first name, else @username. */
  nameOf(id) {
    const s = this.bot.settings;
    return s.names?.[id] || s.people?.[id] || (s.usernames?.[id] ? `@${s.usernames[id]}` : "") ||
      (Number(id) === this.bot.ownerId ? "Owner" : `user ${id}`);
  }

  /** Telegram tells us people's first name and username with everything they send; keep them. */
  learn(user) {
    if (!user?.id || user.is_bot) return;
    const s = this.bot.settings;
    const first = String(user.first_name || user.last_name || "").trim();
    if (first && s.people?.[user.id] !== first) {
      s.people = { ...(s.people || {}), [user.id]: first };
      this.bot.changed = true;
    }
    if (user.username && s.usernames?.[user.id] !== user.username) {
      s.usernames = { ...(s.usernames || {}), [user.id]: user.username };
      this.bot.changed = true;
    }
  }

  /**
   * Once a day (and within the hour when a member's name is missing): ask Telegram for each
   * member's first name and username in our group.
   */
  async refreshNames() {
    const s = this.bot.settings;
    const group = this.bot.tg.groups[0];
    if (!group) return 0;
    if (!this.inDutyHours()) return 0;   // daytime only: the bot stays quiet at night
    const today = rome(this.now).date;
    const ids = this.members().map((m) => m.id).filter((id) => id === this.bot.ownerId || s.roles?.[id] || (s.allowed_users || []).includes(id));
    const missing = ids.some((id) => !s.names?.[id] && !s.people?.[id] && !s.usernames?.[id]);
    const last = await this.bot.store.get("names_refresh", { date: "", at: 0 });
    if (last.date === today && !(missing && this.now - last.at >= 3600)) return 0;
    await this.bot.store.put("names_refresh", { date: today, at: this.now });
    let n = 0;
    for (const id of ids) {
      if (this.bot.tg.callsLeft < 8) break;
      const cm = await this.bot.tg.call("getChatMember", { chat_id: group, user_id: id });
      if (cm?.user) {
        this.learn(cm.user);
        n++;
      }
    }
    return n;
  }

  /** /setname: the owner's name for someone wins over what Telegram says. */
  setName(who, name) {
    const m = this.find(who);
    if (!m) throw new UserError(`I don't know "${who}": use their Telegram id (/roles lists who's in)`);
    const clean = name.replace(/[<>]/g, "").trim().slice(0, 40);
    if (!clean) throw new UserError("Which name? e.g. /setname 123456789 Anna");
    this.bot.settings.names = { ...(this.bot.settings.names || {}), [m.id]: clean };
    this.bot.changed = true;
    return `✅ ${esc(String(m.id))} is now called ${esc(clean)}`;
  }

  /** Hours a day: the main watcher's target, everyone else's 20% cap. */
  limit(m) {
    return m.role === "buyer" ? { target: BUYER_TARGET } : { cap: OTHERS_CAP };
  }

  rolesText() {
    const lines = ["👥 <b>Who does what</b>"];
    for (const m of this.members()) {
      const lim = this.limit(m);
      lines.push(`· <b>${esc(m.name)}</b>: ${m.role ? ROLES[m.role] : "no role yet (🔒 blocked)"}` +
        (m.role ? ` · duty ${lim.target ? `target ${lim.target} h/day` : `up to ${lim.cap} h/day`}` : ""));
    }
    lines.push("", "The owner changes roles with /setrole &lt;name&gt; manager|buyer|seller|none");
    return lines.join("\n");
  }

  setRole(name, role) {
    const m = this.find(name);
    if (!m) throw new UserError(`I don't know "${name}" yet: they need to send the bot a message first (and be /allow-ed)`);
    if (!ROLES[role]) throw new UserError(`Roles: ${Object.keys(ROLES).join(", ")} (or none to remove it)`);
    this.bot.settings.roles = { ...(this.bot.settings.roles || {}), [m.id]: role };
    this.bot.changed = true;
    return `✅ ${esc(m.name)} is now ${role}: ${ROLES[role]}`;
  }

  /** Takes someone's role away: from that moment the bot, the app and the group are closed to them. */
  removeRole(name) {
    const m = this.find(name);
    if (!m) throw new UserError(`I don't know "${name}"`);
    if (m.id === this.bot.ownerId) throw new UserError("You're the owner: you always keep a role, so you can't lock yourself out");
    if (!this.bot.settings.roles?.[m.id]) throw new UserError(`${m.name} has no role`);
    const roles = { ...this.bot.settings.roles };
    delete roles[m.id];
    this.bot.settings.roles = roles;
    this.bot.changed = true;
    return m;
  }

  // --- duty state
  async duty() {
    this._duty ??= await this.bot.store.get("duty", { on: null, log: [], pin: null, nobody_ping: 0, swaps: {},
      next_swap: 1, reminded: {}, late: {} });
    return this._duty;
  }

  async saveDuty() {
    if (this._duty) await this.bot.store.put("duty", this._duty);
  }

  async schedule() {
    this._schedule ??= await this.bot.store.get("schedule", {});
    return this._schedule;
  }

  async saveSchedule() {
    if (this._schedule) await this.bot.store.put("schedule", this._schedule);
  }

  today() {
    return rome(this.now).date;
  }

  inDutyHours(ts = this.now) {
    const r = rome(ts);
    return r.hour >= DUTY_FROM && r.hour < DUTY_TO;
  }

  /** Hours on duty on `date` (only 08:00-22:00 counts) per member id. */
  async hoursOn(date) {
    const d = await this.duty();
    const from = romeTs(date, DUTY_FROM);
    const to = romeTs(date, DUTY_TO);
    const out = {};
    const add = (id, a, b) => {
      const x = Math.max(0, Math.min(b, to) - Math.max(a, from));
      if (x) out[id] = (out[id] || 0) + x / 3600;
    };
    for (const e of d.log) add(e.id, e.start, e.end);
    if (d.on) add(d.on.id, d.on.since, this.now);
    return out;
  }

  /** Where someone's scheduled blocks from now run until (or 22:00). */
  async until(id) {
    const s = (await this.schedule())[this.today()] || {};
    let h = rome(this.now).hour;
    if (s[h] !== id) return romeTs(this.today(), DUTY_TO);
    while (h < DUTY_TO && s[h] === id) h++;
    return romeTs(this.today(), h);
  }

  async groupPost(text, buttons) {
    let res = null;
    for (const g of this.bot.tg.groups) res = (await this.bot.tg.sendTo(g, text, { buttons })) || res;
    return res;
  }

  /** A private message; people who never started the bot can't get one (false). */
  async dm(id, text, buttons) {
    return (await this.bot.tg.sendTo(String(id), text, { buttons })) !== null;
  }

  async start(id) {
    const d = await this.duty();
    const m = this.member(id);
    if (!m) throw new UserError("Only team members go on duty");
    if (d.on?.id === m.id) return `You're already on duty (until ${hhmm(d.on.until)})`;
    const prev = d.on ? this.member(d.on.id) : null;
    if (d.on) d.log.push({ id: d.on.id, start: d.on.since, end: this.now });
    d.on = { id: m.id, since: this.now, until: await this.until(m.id) };
    d.nobody_ping = 0;
    await this.groupPost(`👮 ${mention(m)} is on duty (until ${hhmm(d.on.until)})` +
      (prev ? `, taking over from ${esc(prev.name)}` : ""));
    await this.saveDuty();
    await this.updatePin();
    return `🟢 You're on duty until ${hhmm(d.on.until)}`;
  }

  async end(id, why = "") {
    const d = await this.duty();
    if (!d.on) return "Nobody is on duty";
    if (d.on.id !== Number(id) && !this.isManager(id)) return `${this.member(d.on.id)?.name || "Someone else"} is on duty, not you`;
    const m = this.member(d.on.id);
    const end = why === "22:00" ? Math.min(this.now, romeTs(rome(d.on.since).date, DUTY_TO)) : this.now;
    d.log.push({ id: d.on.id, start: d.on.since, end });
    d.on = null;
    d.nobody_ping = 0;
    await this.groupPost(why === "22:00" ? `🌙 ${esc(m?.name || "")}'s shift ended at 22:00` : `${esc(m?.name || "")} ended duty`);
    await this.saveDuty();
    await this.updatePin();
    return "🔴 Duty ended";
  }

  // --- the pinned "On duty now" message
  async pinText() {
    const d = await this.duty();
    const today = this.today();
    const done = await this.hoursOn(today);
    const lines = [];
    if (d.on) {
      const m = this.member(d.on.id);
      lines.push(`👮 <b>On duty now:</b> ${esc(m?.name || "?")} (since ${hhmm(d.on.since)}, until ${hhmm(d.on.until)})`);
    } else {
      lines.push(this.inDutyHours() ? "👮 <b>Nobody is on duty</b>: tap 🟢 Start duty" : "🌙 Off hours: duty runs 08:00–22:00");
    }
    const today_ = this.members().filter((m) => m.role).map((m) => {
      const lim = this.limit(m);
      return `${esc(m.name)} ${hours(done[m.id] || 0)}/${lim.target ?? lim.cap} h${lim.cap ? " max" : ""}`;
    });
    if (today_.length) lines.push(`Today: ${today_.join(" · ")}`);
    const s = (await this.schedule())[today] || {};
    const h0 = rome(this.now).hour + 1;
    for (let h = Math.max(h0, DUTY_FROM); h < DUTY_TO; h++) {
      if (s[h] && s[h] !== d.on?.id) {
        let e = h;
        while (e < DUTY_TO && s[e] === s[h]) e++;
        lines.push(`Next: ${esc(this.member(s[h])?.name || "?")} ${String(h).padStart(2, "0")}:00–${e}:00`);
        break;
      }
    }
    return lines.join("\n");
  }

  pinButtons() {
    return { inline_keyboard: [[{ text: "🟢 Start duty", callback_data: "duty:on" }, { text: "🔴 End duty", callback_data: "duty:off" }],
      [{ text: "🙋 Need a swap", callback_data: "duty:swap" }, { text: "🙋 Ask someone", callback_data: "duty:ask" }]] };
  }

  /** Keeps the pinned message current (posting and pinning it the first time). */
  async updatePin() {
    const d = await this.duty();
    const groups = this.bot.tg.groups;
    if (!groups.length) return;
    const text = await this.pinText();
    if (d.pin && d.pin.text === text) return;
    if (d.pin && await this.bot.tg.call("editMessageText", { chat_id: d.pin.chat, message_id: d.pin.id, text,
      parse_mode: "HTML", reply_markup: this.pinButtons() })) {
      d.pin.text = text;
    } else {
      const res = await this.bot.tg.sendTo(groups[0], text, { buttons: this.pinButtons() });
      if (!res) return;
      await this.bot.tg.call("pinChatMessage", { chat_id: groups[0], message_id: res.message_id, disable_notification: true });
      d.pin = { chat: String(res.chat.id), id: res.message_id, text };
    }
    await this.saveDuty();
  }

  // --- swaps
  swapChoices(id) {
    const left = Math.max(0, (romeTs(this.today(), DUTY_TO) - this.now) / 3600);
    const row = [1, 2, 3].filter((h) => h < left).map((h) => ({ text: `Next ${h} h`, callback_data: `swr:${h}:${id}` }));
    row.push({ text: "Rest of today", callback_data: `swr:rest:${id}` });
    return { inline_keyboard: [row] };
  }

  /** A swap request: someone else covers [start, end). Posted in the group, first ✅ gets it. */
  async requestSwap(id, start, end) {
    const m = this.member(id);
    if (!m) throw new UserError("Only team members swap");
    if (end <= start) throw new UserError("That time is already over");
    const d = await this.duty();
    const n = d.next_swap++;
    d.swaps[n] = { n, from: m.id, start, end, status: "open", at: this.now };
    const day = rome(start).date === this.today() ? "today" : `${rome(start).weekday} ${rome(start).day} ${rome(start).month}`;
    const res = await this.groupPost(`🙋 ${mention(m)} needs a swap: ${hhmm(start)}–${hhmm(end)} ${day}. Who can cover?`,
      { inline_keyboard: [[{ text: "✅ I'll take it", callback_data: `swt:${n}` }]] });
    if (res) d.swaps[n].msg = { chat: String(res.chat.id), id: res.message_id };
    await this.saveDuty();
    return `🙋 Swap request posted: ${hhmm(start)}–${hhmm(end)} ${day}`;
  }

  async takeSwap(n, id) {
    const d = await this.duty();
    const sw = d.swaps[n];
    if (!sw || sw.status !== "open") return "Someone already took it";
    if (sw.from === Number(id)) return "That's your own swap request";
    const m = this.member(id);
    if (!m) return "Only team members can take a swap";
    const from = this.member(sw.from);
    Object.assign(sw, { status: "taken", taker: m.id, taken_at: this.now });
    // their scheduled blocks in that time become the taker's
    const sched = await this.schedule();
    // every block the swap touches, from the hour it starts in
    for (let t = romeTs(rome(sw.start).date, rome(sw.start).hour); t < sw.end; t += 3600) {
      const r = rome(t);
      if (sched[r.date]?.[r.hour] === sw.from) sched[r.date][r.hour] = m.id;
    }
    await this.saveSchedule();
    // on duty now? hand over right away
    if (d.on?.id === sw.from && sw.start <= this.now + 300) {
      d.log.push({ id: d.on.id, start: d.on.since, end: this.now });
      d.on = { id: m.id, since: this.now, until: sw.end };
    }
    const lim = this.limit(m);
    const day = rome(sw.start).date;
    const after = ((await this.hoursOn(day))[m.id] || 0) + (sw.end - Math.max(sw.start, this.now)) / 3600;
    const warn = lim.cap && after > lim.cap ? `\n⚠️ That puts ${esc(m.name)} at ${hours(after)} h on that day (max ${lim.cap} h)` : "";
    if (sw.msg) {
      await this.bot.tg.call("editMessageReplyMarkup", { chat_id: sw.msg.chat, message_id: sw.msg.id,
        reply_markup: { inline_keyboard: [] } });
    }
    await this.groupPost(`✅ ${mention(m)} covers ${esc(from?.name || "")}'s ${hhmm(sw.start)}–${hhmm(sw.end)}${warn}`);
    await this.saveDuty();
    await this.updatePin();
    return warn ? "It's yours (over your 20%, see the group)" : "It's yours, thanks!";
  }

  // --- asking one person to take duty (/askduty, 🙋 Ask someone): a private message with ✅ Accept / ❌ Can't
  /** The default time to ask for: my shift now (or my next one today), else from now to 22:00. */
  async askWindow(id) {
    const d = await this.duty();
    const today = this.today();
    const end = romeTs(today, DUTY_TO);
    if (d.on?.id === Number(id)) return [this.now, Math.min(d.on.until || end, end)];
    const s = (await this.schedule())[today] || {};
    for (let h = Math.max(rome(this.now).hour, DUTY_FROM); h < DUTY_TO; h++) {
      if (s[h] !== Number(id)) continue;
      let e = h;
      while (e < DUTY_TO && s[e] === Number(id)) e++;
      return [Math.max(this.now, romeTs(today, h)), romeTs(today, e)];
    }
    return [Math.max(this.now, romeTs(today, DUTY_FROM)), end];
  }

  /** "14:00-18:00", "14-18", "tomorrow 9:30–12" -> [start, end] (Italy time), or null. */
  parseWindow(text) {
    const t = text.trim().toLowerCase();
    const m = t.match(/^(?:(today|tomorrow)\s*)?(\d{1,2})(?:[:.](\d{2}))?\s*[-–—]\s*(\d{1,2})(?:[:.](\d{2}))?$/);
    if (!m) return null;
    const date = m[1] === "tomorrow" ? addDays(this.today(), 1) : this.today();
    const [h1, m1, h2, m2] = [m[2], m[3] || 0, m[4], m[5] || 0].map(Number);
    if (h1 > 24 || h2 > 24 || m1 > 59 || m2 > 59) return null;
    return [romeTs(date, h1, m1), romeTs(date, h2, m2)];
  }

  askDay(ts) {
    const r = rome(ts);
    if (r.date === this.today()) return "";
    return r.date === addDays(this.today(), 1) ? " tomorrow" : ` ${r.weekday} ${r.day} ${r.month}`;
  }

  askButtons(n) {
    return { inline_keyboard: [[{ text: "✅ Accept", callback_data: `dya:${n}` }, { text: "❌ Can't", callback_data: `dyn:${n}` }]] };
  }

  /** The other members to ask instead (after a ❌ or no answer). */
  otherButtons(n, ask) {
    const tried = new Set([ask.from, ask.to, ...(ask.tried || [])]);
    const row = this.members().filter((m) => m.role && !tried.has(m.id))
      .map((m) => ({ text: `🙋 Ask ${m.name}`, callback_data: `dyo:${n}:${m.id}` }));
    return row.length ? { inline_keyboard: row.map((b) => [b]) } : undefined;
  }

  async askDuty(fromId, toText, start, end, { tried = [], note = "" } = {}) {
    const from = this.member(fromId);
    if (!from) throw new UserError("Only team members ask for duty");
    const to = typeof toText === "number" ? this.member(toText) : this.find(String(toText || ""));
    if (!to || !to.role) throw new UserError(`I don't know "${toText}" in the team. /roles lists everyone`);
    if (to.id === from.id) throw new UserError("That's you: tap 🟢 Start duty instead");
    // keep it inside the duty day (08:00-22:00) and from now on
    const day = rome(start).date;
    start = Math.max(start, romeTs(day, DUTY_FROM), this.now);
    end = Math.min(end, romeTs(day, DUTY_TO));
    if (end <= start) throw new UserError("That time is over or outside duty hours (08:00–22:00)");
    if (end - start < 15 * 60) throw new UserError("Ask for at least 15 minutes");
    const d = await this.duty();
    d.asks ??= {};
    d.next_ask ??= 1;
    if (Object.values(d.asks).some((a) => a.status === "open" && a.from === from.id && a.to === to.id)) {
      throw new UserError(`You already asked ${to.name}: waiting for an answer`);
    }
    const n = d.next_ask++;
    note = String(note || "").replace(/\s+/g, " ").trim().slice(0, 200);
    const ask = { n, from: from.id, to: to.id, start, end, status: "open", at: this.now, reminded: 0, tried, note, msgs: [] };
    d.asks[n] = ask;
    const when = `${hhmm(start)}–${hhmm(end)}${this.askDay(start)}`;
    const text = `🙋 <b>${esc(from.name)} asks you to take duty ${when}</b>` + (note ? `
💬 ${esc(note)}` : "");
    let res = await this.bot.tg.sendTo(String(to.id), text, { buttons: this.askButtons(n) });
    if (res) ask.msgs.push({ chat: String(to.id), id: res.message_id });
    else {   // never opened the bot in private: ask in the group instead
      const bot = await this.bot.botUsername().catch(() => "");
      const open = bot ? `
📩 Open a private chat with @${esc(bot)} (tap Start) to get these privately next time` : "";
      res = await this.groupPost(`🙋 ${mention(to)}, ${esc(from.name)} asks you to take duty ${when}` + (note ? `
💬 ${esc(note)}` : "") + open, this.askButtons(n));
      if (res) ask.msgs.push({ chat: String(res.chat.id), id: res.message_id });
      if (res) ask.in_group = true;
    }
    await this.saveDuty();
    const where = !res ? " (I couldn't reach them)" : ask.in_group ? ` in the group (${to.name} hasn't opened a private chat with the bot yet)` : " in private";
    return `🙋 Asked ${to.name} for ${when}${where}`;
  }

  async closeAsk(ask, label) {
    for (const m of ask.msgs || []) {
      await this.bot.tg.call("editMessageReplyMarkup", { chat_id: m.chat, message_id: m.id,
        reply_markup: { inline_keyboard: [[{ text: label, callback_data: "noop" }]] } });
    }
  }

  /** ✅ Accept: on duty for that time (the asker's blocks become theirs), the group and the asker are told. */
  async acceptAsk(n, id) {
    const d = await this.duty();
    const ask = d.asks?.[n];
    if (!ask) return "That request is gone";
    if (ask.to !== Number(id)) return "That request is for someone else";
    if (ask.status !== "open") return ask.status === "accepted" ? "You already accepted it" : "That request is closed";
    if (ask.end <= this.now) {
      ask.status = "expired";
      await this.saveDuty();
      return "That time is already over";
    }
    const m = this.member(id);
    const from = this.member(ask.from);
    Object.assign(ask, { status: "accepted", answered_at: this.now });
    const sched = await this.schedule();
    const kept = new Set();
    for (let t = romeTs(rome(ask.start).date, rome(ask.start).hour); t < ask.end; t += 3600) {
      const r = rome(t);
      const day = (sched[r.date] ??= {});
      if (!day[r.hour] || day[r.hour] === ask.from) day[r.hour] = m.id;
      else if (day[r.hour] !== m.id) kept.add(day[r.hour]);
    }
    await this.saveSchedule();
    // the time has started: they're on duty now (whoever was on hands over)
    if (ask.start <= this.now + 300 && d.on?.id !== m.id) {
      if (d.on) d.log.push({ id: d.on.id, start: d.on.since, end: this.now });
      d.on = { id: m.id, since: this.now, until: ask.end };
      d.nobody_ping = 0;
    } else if (d.on?.id === m.id) d.on.until = Math.max(d.on.until, ask.end);
    const lim = this.limit(m);
    const date = rome(ask.start).date;
    const after = ((await this.hoursOn(date))[m.id] || 0) + (ask.end - Math.max(ask.start, this.now)) / 3600;
    const warn = lim.cap && after > lim.cap ? `\n⚠️ That puts ${esc(m.name)} at ${hours(after)} h on that day (max ${lim.cap} h)` : "";
    const note = kept.size ? `\n(${[...kept].map((x) => esc(this.member(x)?.name || "?")).join(", ")} keeps their own hours in that time)` : "";
    const when = `${hhmm(ask.start)}–${hhmm(ask.end)}${this.askDay(ask.start)}`;
    await this.closeAsk(ask, "✅ Accepted");
    await this.groupPost(`✅ ${mention(m)} takes duty ${when} (asked by ${esc(from?.name || "")})${warn}${note}`);
    if (from) await this.dm(from.id, `✅ ${esc(m.name)} accepted: on duty ${when}`);
    await this.saveDuty();
    await this.updatePin();
    return `✅ You're on duty ${when}`;
  }

  /** ❌ Can't: the asker is told, with buttons to ask someone else. */
  async declineAsk(n, id) {
    const d = await this.duty();
    const ask = d.asks?.[n];
    if (!ask) return "That request is gone";
    if (ask.to !== Number(id)) return "That request is for someone else";
    if (ask.status !== "open") return "That request is closed";
    Object.assign(ask, { status: "declined", answered_at: this.now });
    await this.closeAsk(ask, "❌ Can't");
    const m = this.member(id);
    const when = `${hhmm(ask.start)}–${hhmm(ask.end)}${this.askDay(ask.start)}`;
    const others = this.otherButtons(n, ask);
    await this.dm(ask.from, `❌ ${esc(m?.name || "")} can't take duty ${when}.` + (others ? " Ask someone else?" : ""), others);
    await this.saveDuty();
    return `OK, I told ${this.member(ask.from)?.name || "them"}`;
  }

  /** 🙋 Ask <other> after a ❌ or no answer: the same time, the next person. */
  async askOther(n, id, toId) {
    const d = await this.duty();
    const ask = d.asks?.[n];
    if (!ask) return "That request is gone";
    if (ask.from !== Number(id)) return "That's someone else's request";
    if (ask.status === "accepted") return "Someone already took it";
    if (ask.status === "open") return "Still waiting for an answer";
    if (ask.passed) return "You already asked someone else";
    const tried = [...new Set([...(ask.tried || []), ask.to])];
    const res = await this.askDuty(id, Number(toId), ask.start, ask.end, { tried, note: ask.note });
    (await this.duty()).asks[n].passed = true;
    await this.saveDuty();
    return res;
  }

  /** The 5-minute job: no answer in 15 minutes -> one reminder; 15 more -> the asker is told. */
  async askTick() {
    const d = await this.duty();
    for (const [n, ask] of Object.entries(d.asks || {})) {
      if (ask.status === "open" && ask.end <= this.now) {
        ask.status = "expired";
        await this.closeAsk(ask, "⌛ Time's over");
      } else if (ask.status === "open" && !ask.reminded && this.now - ask.at >= 15 * 60) {
        ask.reminded = this.now;
        const from = this.member(ask.from);
        const text = `⏰ Reminder: ${esc(from?.name || "someone")} asks you to take duty ` +
          `${hhmm(ask.start)}–${hhmm(ask.end)}${this.askDay(ask.start)}` + (ask.note ? `
💬 ${esc(ask.note)}` : "");
        const res = await this.bot.tg.sendTo(String(ask.to), text, { buttons: this.askButtons(n) });
        if (res) ask.msgs.push({ chat: String(ask.to), id: res.message_id });
      } else if (ask.status === "open" && ask.reminded && this.now - ask.reminded >= 15 * 60) {
        ask.status = "no_answer";
        await this.closeAsk(ask, "⌛ No answer");
        const m = this.member(ask.to);
        const others = this.otherButtons(n, ask);
        await this.dm(ask.from, `⌛ ${esc(m?.name || "")} didn't answer about duty ${hhmm(ask.start)}–${hhmm(ask.end)}` +
          `${this.askDay(ask.start)}.` + (others ? " Ask someone else?" : ""), others);
      }
      if (this.now - ask.end > 2 * DAY) delete d.asks[n];
    }
  }

  /** Open requests I sent or got, for the app. */
  async asksFor(id) {
    const d = await this.duty();
    return Object.values(d.asks || {}).filter((a) => a.status === "open" && (a.from === Number(id) || a.to === Number(id)))
      .map((a) => ({ n: a.n, from: a.from, to: a.to, start: a.start, end: a.end,
        note: a.note || "", from_name: this.member(a.from)?.name || "", to_name: this.member(a.to)?.name || "" }));
  }

  /** The asker takes a request back (the other person's message says so). */
  async cancelAsk(n, id) {
    const d = await this.duty();
    const ask = d.asks?.[n];
    if (!ask || ask.from !== Number(id)) throw new UserError("That request isn't yours");
    if (ask.status !== "open") throw new UserError(ask.status === "accepted" ? "Already accepted" : "That request is closed");
    Object.assign(ask, { status: "cancelled", answered_at: this.now });
    await this.closeAsk(ask, "↩️ Cancelled");
    await this.saveDuty();
    return `↩️ Cancelled the request to ${this.member(ask.to)?.name || "them"}`;
  }

  /** The pinned message's 🙋 Ask someone: pick who, then how long. */
  askPickButtons(id) {
    const row = this.members().filter((m) => m.role && m.id !== Number(id))
      .map((m) => ({ text: m.name, callback_data: `dyp:${m.id}:${id}` }));
    const rows = [];
    for (let i = 0; i < row.length; i += 3) rows.push(row.slice(i, i + 3));
    return { inline_keyboard: rows };
  }

  async askTimeButtons(id, toId) {
    const left = Math.max(0, (romeTs(this.today(), DUTY_TO) - this.now) / 3600);
    const row = [1, 2, 3].filter((h) => h < left).map((h) => ({ text: `Next ${h} h`, callback_data: `dyt:${toId}:${id}:${h}` }));
    row.push({ text: "Rest of today", callback_data: `dyt:${toId}:${id}:rest` });
    const d = await this.duty();
    const s = (await this.schedule())[this.today()] || {};
    const hasShift = d.on?.id === Number(id) || Object.entries(s).some(([h, x]) => x === Number(id) && Number(h) >= rome(this.now).hour);
    const rows = [row];
    if (hasShift) {
      const [a, b] = await this.askWindow(id);
      rows.unshift([{ text: `My shift ${hhmm(a)}–${hhmm(b)}`, callback_data: `dyt:${toId}:${id}:shift` }]);
    }
    return { inline_keyboard: rows };
  }

  // --- the weekly schedule (Mini App)
  async toggleBlock(id, date, hour) {
    if (!(hour >= DUTY_FROM && hour < DUTY_TO)) throw new UserError("Duty runs 08:00–22:00");
    if (romeTs(date, hour + 1) <= this.now) throw new UserError("That hour is over");
    const sched = await this.schedule();
    const day = (sched[date] ??= {});
    const owner = day[hour];
    if (owner && owner !== Number(id)) throw new UserError(`That's ${this.member(owner)?.name || "someone"}'s block: ask for a swap`);
    if (owner) delete day[hour];
    else day[hour] = Number(id);
    // keep two weeks back at most
    for (const k of Object.keys(sched)) if (k < addDays(this.today(), -14)) delete sched[k];
    await this.saveSchedule();
    await this.updatePin();
    return owner ? `Freed ${date} ${hour}:00` : `Claimed ${date} ${hour}:00`;
  }

  /** The grid for the app: this week and next, Monday to Sunday. */
  async grid() {
    const sched = await this.schedule();
    const today = this.today();
    const dow = (new Date(`${today}T12:00:00Z`).getUTCDay() + 6) % 7;
    const monday = addDays(today, -dow);
    const weeks = [0, 7].map((off) => Array.from({ length: 7 }, (_, i) => {
      const date = addDays(monday, off + i);
      const r = rome(romeTs(date, 12));
      return { date, label: `${r.weekday} ${r.day}`, blocks: sched[date] || {} };
    }));
    const done = await this.hoursOn(today);
    const d = await this.duty();
    return {
      from: DUTY_FROM, to: DUTY_TO, today, weeks,
      members: this.members().map((m) => ({ ...m, ...this.limit(m), today: Math.round((done[m.id] || 0) * 10) / 10 })),
      on: d.on ? { ...d.on, name: this.member(d.on.id)?.name || "" } : null,
    };
  }

  /** The grid plus the open duty requests I sent or got. */
  async gridFor(id) {
    return { ...(await this.grid()), asks: await this.asksFor(id) };
  }

  // --- tasks
  async tasks() {
    this._tasks ??= await this.bot.store.get("tasks", { next: 1, items: [] });
    return this._tasks;
  }

  /** "by fri", "by 12.10", "by tomorrow", "in 3d" -> end of that day (Italy), or null. */
  parseDue(text) {
    const t = text.trim().toLowerCase();
    const today = this.today();
    let date = null;
    let m;
    if (t === "today") date = today;
    else if (t === "tomorrow") date = addDays(today, 1);
    else if ((m = t.match(/^in (\d+) ?d(ays?)?$/))) date = addDays(today, Number(m[1]));
    else if (WEEKDAYS.some((w) => t.startsWith(w))) {
      const want = WEEKDAYS.findIndex((w) => t.startsWith(w));
      const now = new Date(`${today}T12:00:00Z`).getUTCDay();
      date = addDays(today, ((want - now + 7) % 7) || 7);
    } else if ((m = t.match(/^(\d{1,2})[./](\d{1,2})$/))) {
      const y = Number(today.slice(0, 4));
      const pad = (x) => String(x).padStart(2, "0");
      date = `${y}-${pad(m[2])}-${pad(m[1])}`;
      if (date < today) date = `${y + 1}-${pad(m[2])}-${pad(m[1])}`;
      if (Number.isNaN(Date.parse(date))) date = null;
    }
    return date ? romeTs(date, 23, 59) : null;
  }

  async addTask(byId, raw, entities = []) {
    let text = raw.trim();
    let due = null;
    const dm = text.match(/\s+(?:by|due)\s+([\w./ ]+?)$/i) || text.match(/\s+(in \d+ ?d(?:ays?)?)$/i);
    if (dm) {
      due = this.parseDue(dm[1]);
      if (!due) throw new UserError(`I don't understand the date "${dm[1]}". Try: by fri, by 12.10, by tomorrow, in 3d`);
      text = text.slice(0, dm.index).trim();
    }
    let who = null;
    const at = text.match(/(^|\s)@([\p{L}\p{N}_]+)/u);
    const ent = entities.find((e) => e.type === "text_mention" && e.user);
    if (ent) who = this.member(ent.user.id);
    else if (at) who = this.find(at[2]);
    if (at) text = (text.slice(0, at.index) + text.slice(at.index + at[0].length)).replace(/\s+/g, " ").trim();
    if (!who) throw new UserError("Who is it for? e.g. /task add Photos for #12 @Anna by fri");
    if (!text) throw new UserError("What's the task? e.g. /task add Photos for #12 @Anna by fri");
    const tasks = await this.tasks();
    const task = { n: tasks.next++, text, who: who.id, by: Number(byId), created: this.now, due, done_at: null, reminded: 0 };
    tasks.items.push(task);
    await this.bot.store.put("tasks", tasks);
    return task;
  }

  async doneTask(n, byId) {
    const tasks = await this.tasks();
    const t = tasks.items.find((x) => x.n === Number(n));
    if (!t) throw new UserError(`No task ${n}. /tasks lists them`);
    if (t.done_at) return `Task ${t.n} was already done`;
    t.done_at = this.now;
    t.done_by = Number(byId);
    await this.bot.store.put("tasks", tasks);
    return `✅ Task ${t.n} done: ${esc(t.text)}`;
  }

  taskLine(t) {
    const m = this.member(t.who);
    let due = "";
    if (t.due) {
      const r = rome(t.due);
      const late = Math.floor((this.now - t.due) / DAY);
      due = ` · due ${r.weekday} ${r.day} ${r.month}${late > 0 ? ` (${late} day${late > 1 ? "s" : ""} late)` : ""}`;
    }
    return `${t.n}. ${esc(t.text)} · ${esc(m?.name || "?")}${due}`;
  }

  async tasksText() {
    const open = (await this.tasks()).items.filter((t) => !t.done_at);
    if (!open.length) return "📝 No open tasks";
    return ["📝 <b>Open tasks</b>", ...open.map((t) => this.taskLine(t)), "", "/task done &lt;n&gt; when it's done"].join("\n");
  }

  // --- the 5-minute job
  async tick() {
    if (!this.active) return;
    const d = await this.duty();
    const r = rome(this.now);
    const mins = r.hour * 60 + r.minute;
    const team = this.members().filter((m) => m.role);
    // 22:00: shifts end by themselves
    if (d.on && (r.hour >= DUTY_TO || rome(d.on.since).date !== r.date)) await this.end(d.on.id, "22:00");
    // nobody on duty: everyone, now and every 30 minutes (from 07:30)
    if (!d.on && mins >= PING_FROM_MIN && r.hour < DUTY_TO && team.length && this.now - (d.nobody_ping || 0) >= NOBODY_EVERY) {
      d.nobody_ping = this.now;
      const before = r.hour < DUTY_FROM ? "Duty starts at 08:00 and nobody's on yet" : "Nobody is on duty";
      await this.groupPost(`👮 ${before}: ${team.map(mention).join(" ")}. Tap 🟢 Start duty in the pinned message.`,
        this.pinButtons());
      await this.saveDuty();
    }
    // scheduled blocks: a reminder 10 minutes before, the group if 15 minutes late
    const day = (await this.schedule())[r.date] || {};
    for (let h = DUTY_FROM; h < DUTY_TO; h++) {
      const id = day[h];
      if (!id || day[h - 1] === id) continue;   // only where someone's run of blocks starts
      const start = romeTs(r.date, h);
      const key = `${r.date}:${h}:${id}`;
      const m = this.member(id);
      if (!m) continue;
      if (this.now >= start - 600 && this.now < start && !d.reminded[key] && d.on?.id !== id) {
        d.reminded[key] = 1;
        const text = `⏰ ${mention(m)}, your duty starts at ${String(h).padStart(2, "0")}:00. Tap 🟢 Start duty when you're on.`;
        if (!(await this.dm(id, text, this.pinButtons()))) await this.groupPost(text);
        await this.saveDuty();
      }
      if (this.now >= start + 900 && this.now < start + 3600 && !d.late[key] && d.on?.id !== id) {
        d.late[key] = 1;
        await this.groupPost(`⏰ ${mention(m)} was due on duty at ${String(h).padStart(2, "0")}:00 and hasn't started. ` +
          "Can someone cover? Tap 🟢 Start duty.", this.pinButtons());
        await this.saveDuty();
      }
    }
    for (const k of Object.keys(d.reminded)) if (!k.startsWith(r.date)) delete d.reminded[k];
    for (const k of Object.keys(d.late)) if (!k.startsWith(r.date)) delete d.late[k];
    d.log = d.log.filter((e) => this.now - e.end < 60 * DAY);
    for (const [n, sw] of Object.entries(d.swaps)) if (this.now - sw.end > 14 * DAY) delete d.swaps[n];
    if (this.bot.tg.callsLeft > 6) await this.askTick();
    // tasks 3 days late (or 3 days old without a due date)
    const tasks = await this.tasks();
    let changed = false;
    for (const t of tasks.items) {
      if (t.done_at) continue;
      const base = (t.due ?? t.created) + TASK_LATE_DAYS * DAY;
      if (this.now >= base && this.now - (t.reminded || 0) >= TASK_LATE_DAYS * DAY && this.bot.tg.callsLeft > 6) {
        const m = this.member(t.who);
        t.reminded = this.now;
        changed = true;
        const late = t.due ? `${Math.floor((this.now - t.due) / DAY)} days late` : `open for ${Math.floor((this.now - t.created) / DAY)} days`;
        await this.groupPost(`📝 ${m ? mention(m) : "Someone"}, task ${t.n} is ${late}: ${esc(t.text)}\n/task done ${t.n} when it's done`);
      }
    }
    if (changed) await this.bot.store.put("tasks", tasks);
    await this.saveDuty();
    await this.updatePin();
  }

  /** Who to @mention under a new deal: whoever is on duty, or everyone when nobody is. */
  async dealMention(ts = this.now) {
    if (!this.active) return "";
    const r = rome(ts);
    if (r.hour * 60 + r.minute < PING_FROM_MIN || r.hour >= DUTY_TO) return "";
    const d = await this.duty();
    if (d.on) {
      const m = this.member(d.on.id);
      return m ? `👮 ${mention(m)}` : "";
    }
    const team = this.members().filter((m) => m.role);
    return team.length ? `👮 Nobody on duty: ${team.map(mention).join(" ")}` : "";
  }

  /** The person a topic always tags (/tagalways clothes <name>), or null. */
  tagAlways(topic) {
    const id = this.bot.settings.tag_always?.[topic];
    const m = id ? this.member(id) : null;
    return m && this.hasRole(m.id) ? m : null;
  }

  /**
   * The @mentions under a deal: whoever is on duty (dealMention), plus its topic's always-tag person, never
   * the same person twice. A ❌ NO (or a deal going to Rejected) tags nobody.
   */
  async alertMention(d, ts = this.now) {
    if (d.rejected || d.ai?.verdict === "no") return "";
    const base = await this.dealMention(ts);
    const always = this.tagAlways(TOPIC_FOR_GROUP[d.group || ""] || "");
    if (!always || base.includes(`tg://user?id=${always.id}`)) return base;
    return [base, `📌 ${mention(always)}`].filter(Boolean).join(" · ");
  }

  /** New deals nobody claimed within 10 minutes: ping the people who aren't on duty. */
  /**
   * Good deals nobody claimed: one "⏰ not claimed yet" digest in the group's main chat, pinging only
   * whoever is on duty (nobody on duty: no ping). Lower-rated deals expire quietly.
   */
  async unclaimed(deals) {
    if (!this.active || !this.bot.settings.remind) return null;   // off unless the owner turns it on (/remind on)
    const t = rome(this.now);
    const minute = t.hour * 60 + t.minute;
    if (minute < DIGEST_FROM || minute >= DIGEST_TO) return null;
    const d = await this.duty();
    if (this.now - (d.last_digest || 0) < DIGEST_EVERY) return null;
    const due = deals.filter(([, x]) => x.status === "new" && x.alerted_at && !x.escalated && !x.rejected && (x.ai?.verdict !== "no" || x.ai_overruled) &&
      (x.rating ?? 0) >= DIGEST_MIN_RATING && this.now - x.alerted_at >= UNCLAIMED_AFTER && this.now - x.alerted_at <= DIGEST_MAX_AGE)
      .sort((a, b) => (a[1].n ?? 0) - (b[1].n ?? 0));
    const group = this.bot.tg.groups[0];
    if (!due.length || !group || this.bot.tg.callsLeft < 4) return null;
    for (const [key, x] of due) {
      x.escalated = this.now;   // one reminder per deal, ever
      await this.bot.store.saveDeal(key, x);
    }
    d.last_digest = this.now;
    await this.saveDuty();
    const link = (x) => {
      const m = (x.messages || []).find((y) => Telegram.isGroup(y.chat));
      const label = `#${x.n ?? "?"}`;
      return m && String(m.chat).startsWith("-100") ? `<a href="https://t.me/c/${String(m.chat).slice(4)}/${m.id}">${label}</a>` : label;
    };
    const shown = due.slice(0, DIGEST_LIST).map(([, x]) => link(x)).join(", ");
    const more = due.length > DIGEST_LIST ? ` and ${due.length - DIGEST_LIST} more` : "";
    const on = d.on ? this.member(d.on.id) : null;
    const text = `⏰ ${due.length} deal${due.length === 1 ? "" : "s"} not claimed yet: ${shown}${more}` +
      (on ? `\n${mention(on)}, you're on duty` : "");
    await this.bot.tg.sendTo(group, text);
    return text;
  }

  // --- the Sunday report
  /** Hours each member was on duty (08:00-22:00) in the 7 days up to `now`, and the shifts. */
  async dutyHours(now) {
    const d = await this.duty();
    const per = {};
    const shifts = [...d.log, ...(d.on ? [{ id: d.on.id, start: d.on.since, end: now }] : [])];
    for (let i = 0; i < 7; i++) {
      const date = rome(now - i * DAY).date;
      const from = romeTs(date, DUTY_FROM);
      const to = romeTs(date, DUTY_TO);
      for (const e of shifts) {
        const x = Math.max(0, Math.min(e.end, to) - Math.max(e.start, from));
        per[e.id] = (per[e.id] || 0) + x / 3600;
      }
    }
    return { per, shifts };
  }

  async weekly(deals, now) {
    const since = now - 7 * DAY;
    const lines = ["", "👥 <b>Team</b>"];
    const { per, shifts } = await this.dutyHours(now);
    for (const m of this.members().filter((x) => x.role)) {
      const lim = this.limit(m);
      const caught = deals.filter(([, x]) => x.claimed_at >= since && shifts.some((e) => e.id === m.id &&
        x.claimed_at >= e.start && x.claimed_at <= e.end)).length;
      lines.push(`· ${esc(m.name)}: ${hours(per[m.id] || 0)} h on duty (${lim.target ? `target ${lim.target * 7}` : `max ${hours(lim.cap * 7)}`}) · ` +
        `${caught} deal${caught === 1 ? "" : "s"} caught on shift`);
    }
    const reactions = deals.map(([, x]) => x).filter((x) => x.claimed_at >= since && x.alerted_at && x.claimed_at >= x.alerted_at)
      .map((x) => (x.claimed_at - x.alerted_at) / 60);
    lines.push(reactions.length ? `⏱ Average reaction: ${Math.round(reactions.reduce((s, x) => s + x, 0) / reactions.length)} min ` +
      `(alert to claim, ${reactions.length} deals)` : "⏱ Average reaction: no claims this week");
    const open = (await this.tasks()).items.filter((t) => !t.done_at);
    const late = open.filter((t) => t.due && t.due < now).length;
    lines.push(`📝 Open tasks: ${open.length}${late ? ` (${late} late)` : ""}`);
    const bought = deals.filter(([, x]) => x.status === "bought").length;
    const listed = deals.filter(([, x]) => x.status === "listed").length;
    lines.push(`📦 Bought, not listed: ${bought} · 🏷 Listed, not sold: ${listed}`);
    lines.push(await this.healthLine(now));
    return lines.join("\n");
  }

  async healthLine(now) {
    const h = await this.bot.store.get("health", {});
    let runs = 0;
    let failed = 0;
    let blocked = 0;
    for (let i = 0; i < 7; i++) {
      const x = h[rome(now - i * DAY).date] || {};
      runs += x.runs || 0;
      failed += x.failed || 0;
      blocked += x.blocked || 0;
    }
    const st = await this.bot.store.get("status", {});
    const stale = st.last_run ? Math.round((now - st.last_run) / 60) : null;
    const problems = [];
    if (failed) problems.push(`${failed} failure alert${failed > 1 ? "s" : ""}`);
    if (blocked) problems.push(`Vinted blocks on ${blocked} run${blocked > 1 ? "s" : ""}`);
    if (stale === null || stale > 30) problems.push(stale === null ? "no scan reported yet" : `no scan for ${stale} min`);
    return problems.length ? `🩺 Bot health: ⚠️ ${problems.join(", ")} (${runs.toLocaleString("en-US")} scans)`
      : `🩺 Bot health: OK (${runs.toLocaleString("en-US")} scans)`;
  }
}

/** Scan counts per day, from the scanner's run reports (for the Sunday report). */
export async function recordRun(store, body, now) {
  const h = await store.get("health", {});
  const day = rome(now).date;
  const x = (h[day] ??= { runs: 0, failed: 0, blocked: 0 });
  if (body.status) x.runs++;
  for (const n of body.notify || []) {
    if (/runs failed/.test(n.text)) x.failed++;
    if (/Vinted refused/.test(n.text)) x.blocked++;
  }
  for (const k of Object.keys(h)) if (k < addDays(day, -14)) delete h[k];
  await store.put("health", h);
}

export function isGroup(chat) {
  return Telegram.isGroup(chat);
}
