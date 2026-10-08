// The team handbook: ONE message pinned in the 📖 Rules topic, each section's rules in a collapsed
// quote. /handbook shows it anywhere, /handbook edit <section> (owner) changes a section and the
// pinned message is edited in place. The text lives only in the private D1 storage (kv "handbook"),
// never in the code: it names the team.

import { UserError, esc, rome } from "./util.js";

const LIMIT = 4000;   // Telegram's 4,096 characters per message, with some room

/** "🛒 Buying · Anna" -> "buying" */
export function sectionKey(title) {
  return (title.toLowerCase().match(/[a-z]+/) || ["section"])[0];
}

/** The whole handbook as one HTML message. */
export function render(book) {
  const parts = [`<b>${esc(book.title || "📖 HANDBOOK", false)}</b>`];
  if (book.intro) parts[0] += `\n${esc(book.intro, false)}`;
  for (const s of book.sections) {
    parts.push(`<b>${esc(s.title, false)}</b>\n<blockquote expandable>${esc(s.text.trim(), false)}</blockquote>`);
  }
  if (book.updated) {
    const t = rome(book.updated);
    parts.push(`<i>Last updated: ${Number(t.day)} ${t.month} ${t.date.slice(0, 4)}</i>`);
  }
  return parts.join("\n\n");
}

/** Characters Telegram counts (the text without the HTML tags). */
function visibleLength(html) {
  return html.replace(/<[^>]+>/g, "").replace(/&(amp|lt|gt|quot);/g, "x").length;
}

export class Handbook {
  constructor(bot) {
    this.bot = bot;
  }

  async load() {
    this._data ??= await this.bot.store.get("handbook", { sections: [] });
    return this._data;
  }

  async save() {
    if (this._data) await this.bot.store.put("handbook", this._data);
  }

  async find(name) {
    const { sections } = await this.load();
    const q = name.trim().toLowerCase();
    const s = sections.find((x) => x.key === q) || sections.find((x) => x.key.startsWith(q) && q.length >= 3);
    if (!s) {
      throw new UserError(sections.length ? `No section "${name}". Sections: ${sections.map((x) => x.key).join(", ")}`
        : "The handbook is empty");
    }
    return s;
  }

  /** A copy of the handbook (not pinned) wherever someone asked for it. */
  async send(chat, thread = null) {
    const book = await this.load();
    if (!book.sections.length) {
      return this.bot.reply(chat, "📖 No handbook yet. The owner writes it with /handbook edit &lt;section&gt;");
    }
    const payload = { chat_id: chat, text: render(book), parse_mode: "HTML", disable_web_page_preview: true };
    if (thread) payload.message_thread_id = thread;
    return this.bot.tg.call("sendMessage", payload);
  }

  /**
   * Makes sure the Rules topic has the handbook, pinned: the message we posted is edited in place;
   * a new one (pinned silently, the "pinned a message" notice removed) only when it's gone.
   */
  async ensure(chat, thread) {
    const book = await this.load();
    if (!book.sections.length || !thread) return false;
    const text = render(book);
    const tg = this.bot.tg;
    const old = book.message;
    if (old && String(old.chat) === String(chat) && old.thread === thread) {
      const r = await tg.request("editMessageText", { chat_id: chat, message_id: old.id, text, parse_mode: "HTML",
        disable_web_page_preview: true });
      if (r.ok || /not modified/.test(r.error)) return true;
      if (!/not found|can't be edited/.test(r.error)) return false;   // Telegram trouble: try again later
    }
    const sent = await tg.call("sendMessage", { chat_id: chat, message_thread_id: thread, text, parse_mode: "HTML",
      disable_web_page_preview: true });
    if (!sent?.message_id) return false;
    book.message = { chat: String(chat), thread, id: sent.message_id };
    await this.save();
    if (await tg.call("pinChatMessage", { chat_id: chat, message_id: sent.message_id, disable_notification: true })) {
      // the "pinned a message" notice comes right after it
      await tg.call("deleteMessage", { chat_id: chat, message_id: sent.message_id + 1 });
    }
    return true;
  }

  /** New rules for one section; the pinned message is updated in place. */
  async edit(name, text, now) {
    const s = await this.find(name);
    const body = text.trim();
    if (!body) throw new UserError("The new text is empty");
    const book = await this.load();
    const before = s.text;
    s.text = body;
    if (visibleLength(render({ ...book, updated: now })) > LIMIT) {
      s.text = before;
      throw new UserError("That makes the handbook too long for one Telegram message. Shorten it a little");
    }
    book.updated = now;
    await this.save();
    const where = book.message;
    const pinned = where ? await this.ensure(where.chat, where.thread) : false;
    return { section: s, pinned };
  }
}
