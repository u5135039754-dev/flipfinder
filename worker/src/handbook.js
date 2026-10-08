// The team handbook: sections pinned in the 📖 Rules topic, /handbook to repost it anywhere,
// /handbook edit <section> (owner) to change one. The text lives only in the private D1 storage
// (kv "handbook"), never in the code: it names the team.

import { UserError, esc } from "./util.js";

/** "🛒 BUYING (Anna)" -> "buying" */
export function sectionKey(title) {
  return (title.toLowerCase().match(/[a-z]+/) || ["section"])[0];
}

export function sectionText(s) {
  return `<b>${esc(s.title, false)}</b>\n${esc(s.text, false)}`;   // quotes need no escaping in Telegram HTML
}

export class Handbook {
  constructor(bot) {
    this.bot = bot;
  }

  async load() {
    this._data ??= await this.bot.store.get("handbook", { sections: [], posted: {} });
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

  /** Every section as its own message in `chat` (and `thread`); pinned when it's the Rules topic. */
  async post(chat, thread = null, pin = false) {
    const { sections, posted } = await this.load();
    if (!sections.length) {
      await this.bot.reply(chat, "📖 No handbook yet. The owner writes it with /handbook edit &lt;section&gt;");
      return 0;
    }
    const where = (posted[`${chat}:${thread ?? ""}`] ??= {});
    let n = 0;
    for (const s of sections) {
      const text = sectionText(s);
      const old = pin ? where[s.key] : null;
      if (old && old.text === text) {
        n++;
        continue;   // already there and unchanged
      }
      if (old && await this.bot.tg.call("editMessageText", { chat_id: chat, message_id: old.id, text, parse_mode: "HTML",
        disable_web_page_preview: true })) {
        old.text = text;
        n++;
        continue;
      }
      const payload = { chat_id: chat, text, parse_mode: "HTML", disable_web_page_preview: true };
      if (thread) payload.message_thread_id = thread;
      const res = await this.bot.tg.call("sendMessage", payload);
      if (!res?.message_id) continue;
      n++;
      if (pin) {
        await this.bot.tg.call("pinChatMessage", { chat_id: chat, message_id: res.message_id, disable_notification: true });
        where[s.key] = { id: res.message_id, text };
      }
    }
    await this.save();
    return n;
  }

  /** New text for a section; the pinned copies in the Rules topic are edited in place. */
  async edit(name, text) {
    const s = await this.find(name);
    const body = text.trim();
    if (!body) throw new UserError("The new text is empty");
    if (body.length > 3500) throw new UserError("That's too long for one Telegram message (3,500 characters max)");
    s.text = body;
    const { posted } = await this.load();
    let updated = 0;
    for (const [where, keys] of Object.entries(posted)) {
      const old = keys[s.key];
      if (!old) continue;
      const chat = where.split(":")[0];
      const newText = sectionText(s);
      if (await this.bot.tg.call("editMessageText", { chat_id: chat, message_id: old.id, text: newText, parse_mode: "HTML",
        disable_web_page_preview: true })) {
        old.text = newText;
        updated++;
      }
    }
    await this.save();
    return { section: s, updated };
  }
}
