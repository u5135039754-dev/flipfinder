// The weekly team meeting: by default Sunday 20:30 (right after the weekly report), in the group's
// Telegram video chat or at an external link (/meeting link <url>). Reminders in the group the day
// before and 15 minutes before, pinging every member, with ✅ I'm in / ❌ Can't make it; when 2 or more
// can't, a poll with 3 other times. The 15-minute reminder carries a short agenda from our numbers.
// /minutes saves the notes, posts them in Summary and the next weekly report shows them.

import { dailyStats } from "./stats.js";
import { addDays, esc, euro, rome, romeTs } from "./util.js";

const DAY = 86400;
export const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const DAY_NAMES = { monday: "Mon", tuesday: "Tue", wednesday: "Wed", thursday: "Thu", friday: "Fri", saturday: "Sat", sunday: "Sun",
  lunedi: "Mon", martedi: "Tue", mercoledi: "Wed", giovedi: "Thu", venerdi: "Fri", sabato: "Sat", domenica: "Sun",
  lun: "Mon", mar: "Tue", mer: "Wed", gio: "Thu", ven: "Fri", sab: "Sat", dom: "Sun" };
export const DEFAULT = { day: "Sun", time: "20:30", on: true, link: null };
const SOON = 15 * 60;          // the agenda reminder, this long before
const CANT_FOR_POLL = 2;       // this many "can't make it" bring a poll with other times

const mention = (m) => `<a href="tg://user?id=${m.id}">${esc(m.name)}</a>`;

/** "sun", "Sunday", "domenica" -> "Sun"; null if it isn't a day. */
export function parseDay(s) {
  const t = String(s || "").toLowerCase().normalize("NFKD").replace(/[^a-z]/g, "");
  if (DAY_NAMES[t]) return DAY_NAMES[t];
  return DAYS.find((d) => d.toLowerCase() === t.slice(0, 3)) || null;
}

const label = (ts) => {
  const r = rome(ts);
  return `${r.weekday} ${Number(r.day)} ${r.month}, ${String(r.hour).padStart(2, "0")}:${String(r.minute).padStart(2, "0")}`;
};

export class Meeting {
  constructor(bot) {
    this.bot = bot;
  }

  get config() {
    return { ...DEFAULT, ...(this.bot.settings.meeting || {}) };
  }

  set(changes) {
    this.bot.settings.meeting = { ...this.config, ...changes };
    this.bot.changed = true;
  }

  /** The next meeting (or one that started less than an hour ago) as {date, ts}. */
  next(now = this.bot.now) {
    const c = this.config;
    const [h, m] = c.time.split(":").map(Number);
    const today = rome(now).date;
    for (let i = -1; i <= 7; i++) {
      const date = addDays(today, i);
      const ts = romeTs(date, h, m);
      if (rome(ts).weekday === c.day && ts > now - 3600) return { date, ts };
    }
    return null;
  }

  members() {
    return this.bot.team.members().filter((m) => m.role);
  }

  place() {
    const c = this.config;
    return c.link ? `🔗 Join here: ${esc(c.link)}` : "📹 Video chat in the group, tap the call icon at the top";
  }

  buttons(date) {
    const rows = [[{ text: "✅ I'm in", callback_data: `mt:in:${date}` }, { text: "❌ Can't make it", callback_data: `mt:out:${date}` }]];
    if (this.config.link && /^https:\/\//.test(this.config.link)) rows.push([{ text: "Join", url: this.config.link }]);
    return { inline_keyboard: rows };
  }

  /** Who answered: "✅ Marco, Luca · ❌ Boss". */
  async answers(date) {
    const all = (await this.bot.store.get("meeting_rsvp", {}))[date] || {};
    const names = (v) => this.members().filter((m) => all[String(m.id)] === v).map((m) => esc(m.name));
    const ins = names("in");
    const outs = names("out");
    return { ins, outs, line: ins.length || outs.length ? `✅ ${ins.join(", ") || "nobody yet"} · ❌ ${outs.join(", ") || "nobody"}` : "" };
  }

  /** The agenda (at most ~10 lines) from this week's numbers. */
  async agenda() {
    const bot = this.bot;
    const now = bot.now;
    const s = await dailyStats(bot, { days: 7 });
    const t = s.totals;
    const lines = ["📋 <b>Agenda</b>",
      `• This week: ${t.sent} deals (✅ ${t.yes} · ❌ ${t.no}), bought ${t.bought}, sold ${t.sold}, profit ${euro(t.profit)}`];
    const stock = (await bot.allDeals()).map(([, d]) => d).filter((d) => ["bought", "listed"].includes(d.status))
      .map((d) => ({ n: d.n, title: (d.title || "").slice(0, 22), days: Math.round((now - (d.bought_at || now)) / DAY) }))
      .sort((a, b) => b.days - a.days);
    lines.push(stock.length ? `• Stock: ${stock.length} item${stock.length === 1 ? "" : "s"}: ` +
      stock.slice(0, 3).map((x) => `#${x.n} ${esc(x.title)} (${x.days} d)`).join(", ") + (stock.length > 3 ? ` +${stock.length - 3} more` : "")
      : "• Stock: nothing in stock");
    const rep = (await bot.store.get("reports", [])).filter((r) => r.kind === "week").at(-1);
    const open = (rep?.suggestions || []).filter((x) => x.action && !x.applied);
    lines.push(open.length ? `• Open suggestions: ${open.length} (${esc(open[0].text.slice(0, 60))}${open.length > 1 ? " …" : ""})`
      : "• Open suggestions: none");
    const wrong = (await bot.allDeals()).map(([, d]) => d).filter((d) => d.ai_overruled && now - d.ai_overruled.at < 7 * DAY);
    lines.push(`• AI got wrong (↩️ Not a NO): ${wrong.length ? wrong.slice(0, 4).map((d) => `#${d.n}`).join(", ") : "none"}`);
    const { per } = await bot.team.dutyHours(now);
    const hours = this.members().map((m) => `${esc(m.name)} ${Math.round(per[m.id] || 0)} h`);
    if (hours.length) lines.push(`• Duty hours: ${hours.join(", ")}`);
    return lines;
  }

  /** The reminder text: "tomorrow" or "in 15 minutes" (with the agenda). */
  async text(kind, m) {
    const pings = this.members().map(mention).join(" ");
    const when = kind === "soon" ? `in 15 minutes (${label(m.ts).split(", ")[1]})` : `tomorrow, ${label(m.ts)}`;
    const lines = [`👥 <b>Team meeting ${when}</b>`, this.place()];
    if (kind === "soon") lines.push(...(await this.agenda()));
    const { line } = await this.answers(m.date);
    if (line) lines.push(line);
    if (pings) lines.push(pings);
    return lines.join("\n");
  }

  /** The 5-minute cron: the day-before and the 15-minute reminders, once each. */
  async tick() {
    const bot = this.bot;
    if (!this.config.on || !bot.team.active) return;
    const m = this.next();
    const group = bot.tg.groups[0];
    if (!m || !group) return;
    const state = await bot.store.get("meeting_state", {});
    if (state.date !== m.date) Object.assign(state, { date: m.date, before: false, soon: false, poll: false, msgs: [] });
    const now = bot.now;
    let kind = null;
    if (!state.soon && now >= m.ts - SOON && now < m.ts + 30 * 60) kind = "soon";
    else if (!state.before && !state.soon && now >= m.ts - DAY && now < m.ts - SOON) kind = "before";
    if (!kind || bot.tg.callsLeft < 6) return;
    state[kind] = true;
    const sent = await bot.tg.sendTo(group, await this.text(kind, m), { buttons: this.buttons(m.date), preview: false });
    if (sent) state.msgs.push({ chat: group, id: sent.message_id, kind });
    await bot.store.put("meeting_state", state);
  }

  /** ✅ I'm in / ❌ Can't make it; with 2+ who can't, a poll with 3 other times that week (once). */
  async answer(cq, user, value, date) {
    const bot = this.bot;
    if (!this.members().some((m) => m.id === user)) return bot.answer(cq, "Only team members");
    const all = await bot.store.get("meeting_rsvp", {});
    all[date] = { ...(all[date] || {}), [String(user)]: value };
    for (const k of Object.keys(all).sort().slice(0, -8)) delete all[k];   // a couple of months
    await bot.store.put("meeting_rsvp", all);
    await bot.answer(cq, value === "in" ? "✅ See you there" : "❌ Noted");
    const state = await bot.store.get("meeting_state", {});
    const m = this.next();
    if (!m || m.date !== date || state.date !== date) return;
    for (const msg of state.msgs || []) {
      await bot.tg.call("editMessageText", { chat_id: msg.chat, message_id: msg.id, text: await this.text(msg.kind, m),
        parse_mode: "HTML", reply_markup: this.buttons(date), disable_web_page_preview: true });
    }
    const { outs } = await this.answers(date);
    if (outs.length >= CANT_FOR_POLL && !state.poll) {
      state.poll = true;
      await bot.store.put("meeting_state", state);
      const options = [1, 2, 3].map((i) => label(m.ts + i * DAY));
      await bot.tg.call("sendPoll", { chat_id: bot.tg.groups[0], question: `${outs.length} can't make it: another time for the meeting?`,
        options: [...options, "Keep the usual time"].map((text) => ({ text })), is_anonymous: false, allows_multiple_answers: true });
    }
  }

  /** /minutes <text>: kept (for the next weekly report) and posted in Summary. */
  async minutes(text, user) {
    const bot = this.bot;
    const list = await bot.store.get("meeting_minutes", []);
    const by = bot.team.member(user)?.name || "the owner";
    list.push({ at: bot.now, by, text: text.slice(0, 2000) });
    await bot.store.put("meeting_minutes", list.slice(-60));
    const r = rome(bot.now);
    const groups = bot.tg.groups;
    await bot.tg.sendText(`📝 <b>Meeting notes, ${r.weekday} ${Number(r.day)} ${r.month}</b> (${esc(by)})\n${esc(text, false)}`,
      { topic: "summary", chats: groups.length ? groups : undefined });
  }

  /** The notes saved between two dates (for the weekly report). */
  async notesBetween(from, to) {
    return (await this.bot.store.get("meeting_minutes", [])).filter((x) => rome(x.at).date >= from && rome(x.at).date <= to);
  }

  /** /meeting: when and where. */
  status() {
    const c = this.config;
    if (!c.on) return "👥 The weekly meeting is off. /meeting on brings it back";
    const m = this.next();
    return `👥 Weekly meeting: every ${c.day} at ${c.time}${m ? `, next ${label(m.ts)}` : ""}\n${this.place()}\n` +
      "Reminders the day before and 15 minutes before. /meeting time sun 20:30 · /meeting link <url> · /meeting link off · /meeting off";
  }
}
