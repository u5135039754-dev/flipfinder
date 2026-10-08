// The team handbook: read in the Mini App (📖 Handbook page). The Rules topic has one short pinned
// message with a button that opens that page. /handbook edit <section> (owner) changes a section;
// the app shows it at once, the pinned message stays the same. The text lives only in the private
// D1 storage (kv "handbook"), never in the code: it names the team.

import { UserError, esc } from "./util.js";

const MAX_SECTION = 2000;   // characters per section, plenty for a list of short rules

/** "🛒 Buying · Anna" -> "buying" */
export function sectionKey(title) {
  return (title.toLowerCase().match(/[a-z]+/) || ["section"])[0];
}

/** The pinned message: title, one line, and the button that opens the handbook page. */
export function pinnedMessage(book, botUsername) {
  const text = `<b>${esc(book.title || "📖 HANDBOOK", false)}</b>` + (book.intro ? `\n${esc(book.intro, false)}` : "");
  const markup = botUsername
    ? { inline_keyboard: [[{ text: "📖 Open handbook", url: `https://t.me/${botUsername}?startapp=handbook` }]] }
    : undefined;
  return { text, markup };
}

/** For the app: "🛒 Buying · Anna" -> icon, name, person; "1. Claim first" -> "Claim first". */
export function forApp(book) {
  return {
    title: book.title || "📖 HANDBOOK", intro: book.intro || "", updated: book.updated || null,
    sections: (book.sections || []).map((s) => {
      const [head, person = ""] = s.title.split(" · ");
      const m = head.match(/^(\S+)\s+(.*)$/);
      const icon = m && !/[a-z0-9]/i.test(m[1]) ? m[1] : "📄";
      const name = m && icon === m[1] ? m[2] : head;
      const rules = s.text.split("\n").map((l) => l.replace(/^\s*\d+[.)]\s*/, "").trim()).filter(Boolean);
      return { key: s.key, icon, name, person: person.trim(), rules };
    }),
  };
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

  async username() {
    this._me ??= await this.bot.tg.call("getMe", {});
    return this._me?.username || null;
  }

  /** The short message with the button, wherever someone asked for it (not pinned). */
  async send(chat, thread = null) {
    const book = await this.load();
    if (!book.sections.length) {
      return this.bot.reply(chat, "📖 No handbook yet. The owner writes it with /handbook edit &lt;section&gt;");
    }
    const { text, markup } = pinnedMessage(book, await this.username());
    const payload = { chat_id: chat, text, parse_mode: "HTML", disable_web_page_preview: true };
    if (markup) payload.reply_markup = markup;
    if (thread) payload.message_thread_id = thread;
    return this.bot.tg.call("sendMessage", payload);
  }

  /**
   * Makes sure the Rules topic has the short handbook message, pinned: the one we posted is edited in
   * place if needed; a new one (pinned silently, the "pinned a message" notice removed) only when it's gone.
   */
  async ensure(chat, thread) {
    const book = await this.load();
    if (!book.sections.length || !thread) return false;
    const { text, markup } = pinnedMessage(book, await this.username());
    if (!markup) return false;   // without the button it's no use: try again later
    const tg = this.bot.tg;
    const old = book.message;
    if (old && String(old.chat) === String(chat) && old.thread === thread) {
      const r = await tg.request("editMessageText", { chat_id: chat, message_id: old.id, text, parse_mode: "HTML",
        disable_web_page_preview: true, reply_markup: markup });
      if (r.ok || /not modified/.test(r.error)) return true;
      if (!/not found|can't be edited/.test(r.error)) return false;   // Telegram trouble: try again later
    }
    const sent = await tg.call("sendMessage", { chat_id: chat, message_thread_id: thread, text, parse_mode: "HTML",
      disable_web_page_preview: true, reply_markup: markup });
    if (!sent?.message_id) return false;
    book.message = { chat: String(chat), thread, id: sent.message_id };
    await this.save();
    if (await tg.call("pinChatMessage", { chat_id: chat, message_id: sent.message_id, disable_notification: true })) {
      // the "pinned a message" notice comes right after it
      await tg.call("deleteMessage", { chat_id: chat, message_id: sent.message_id + 1 });
    }
    return true;
  }

  /** New rules for one section: the app shows them right away, with the new date. */
  async edit(name, text, now) {
    const s = await this.find(name);
    const body = text.trim();
    if (!body) throw new UserError("The new text is empty");
    if (body.length > MAX_SECTION) throw new UserError("That's too long for one section. Shorten it a little");
    s.text = body;
    const book = await this.load();
    book.updated = now;
    await this.save();
    return { section: s };
  }
}
