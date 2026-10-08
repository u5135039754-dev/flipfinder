// Telegram Bot API: several chats, group topics, and groups that became supergroups.

export class Telegram {
  /**
   * chatIds: the owner's private chat first, then the group(s).
   * migrations: old group id -> supergroup id (learned from Telegram, saved in settings).
   * maxCalls: Cloudflare's free plan allows 50 outgoing requests per invocation.
   */
  constructor({ token, chatIds = [], topics = {}, migrations = {}, api = "https://api.telegram.org",
    fetch: fetchFn = (...a) => fetch(...a), maxCalls = 45 }) {
    this.base = `${api}/bot${token}`;
    this.migrations = { ...migrations };
    this.chatIds = chatIds.map((c) => this.migrations[String(c)] || String(c));
    this.topics = { ...topics };
    this.fetch = fetchFn;
    this.callsLeft = maxCalls;
    this.newMigrations = {};
  }

  static isGroup(chat) {
    return String(chat).startsWith("-");
  }

  get groups() {
    return this.chatIds.filter((c) => Telegram.isGroup(c));
  }

  /** POST to the Bot API; follows a group's upgrade to a supergroup. Returns {ok, result, error}. */
  async request(method, payload) {
    for (let attempt = 0; attempt < 2; attempt++) {
      if (this.callsLeft <= 0) return { ok: false, error: "request budget used up" };
      this.callsLeft--;
      let data = {};
      try {
        const r = await this.fetch(`${this.base}/${method}`, {
          method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload),
        });
        data = await r.json().catch(() => ({}));
      } catch (e) {
        return { ok: false, error: String(e) };
      }
      if (data.ok) return { ok: true, result: data.result ?? true };
      const newId = data.parameters?.migrate_to_chat_id;
      const oldId = String(payload.chat_id ?? "");
      if (newId && oldId) {
        // the group was upgraded to a supergroup and has a new id: use it from now on
        this.migrations[oldId] = this.newMigrations[oldId] = String(newId);
        this.chatIds = this.chatIds.map((c) => (c === oldId ? String(newId) : c));
        payload = { ...payload, chat_id: String(newId) };
        continue;
      }
      return { ok: false, error: data.description || "unknown error" };
    }
    return { ok: false, error: "moved twice" };
  }

  /** Any Bot API method; its result, or null on failure. */
  async call(method, payload) {
    const r = await this.request(method, payload);
    return r.ok ? r.result : null;
  }

  /** One message to one chat; in a group it goes to `topic` (General if that topic is unknown). */
  async sendTo(chat, text, { buttons, topic, replyTo, photo } = {}) {
    chat = this.migrations[String(chat)] || String(chat);
    const payload = { chat_id: chat, parse_mode: "HTML" };
    if (photo) Object.assign(payload, { photo, caption: text });
    else Object.assign(payload, { text, disable_web_page_preview: false });
    if (buttons) payload.reply_markup = buttons;
    const thread = topic && Telegram.isGroup(chat) ? this.topics[topic] : null;
    if (thread) payload.message_thread_id = thread;
    if (replyTo) payload.reply_parameters = { message_id: replyTo, allow_sending_without_reply: true };
    const method = photo ? "sendPhoto" : "sendMessage";
    let r = await this.request(method, payload);
    if (!r.ok && thread) {
      // the topic was deleted or the id is wrong: General is better than nothing
      delete payload.message_thread_id;
      r = await this.request(method, payload);
    }
    return r.ok ? r.result : null;
  }

  /** To every chat (or the given ones); true if at least one got it. */
  async sendText(text, { chats, buttons, topic } = {}) {
    let sent = false;
    for (const chat of chats || [...this.chatIds]) {
      sent = (await this.sendTo(chat, text, { buttons, topic })) !== null || sent;
    }
    return sent;
  }

  /** A deal alert to every chat, with its photo when Telegram can load it; where it landed. */
  async sendAlert(text, photo, buttons, topic, groupLine = "") {
    const out = [];
    for (const chat of [...this.chatIds]) {
      // in the group, a line @mentioning whoever is on duty
      const body = groupLine && Telegram.isGroup(chat) ? `${text}\n${groupLine}` : text;
      let res = null;
      if (photo && body.length <= 1024) res = await this.sendTo(chat, body, { buttons, topic, photo });
      if (res === null) res = await this.sendTo(chat, body, { buttons, topic });
      if (res !== null) {
        out.push({ chat: String(res.chat.id), id: res.message_id, photo: "photo" in res,
          thread: res.message_thread_id ?? null });
      }
    }
    return out;
  }
}
