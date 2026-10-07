// Private storage in D1: a small key/value table plus one row per deal.

const DAY = 86400;

export const SETTINGS_DEFAULTS = {
  disabled: [], prices: {}, added: [], removed: [], rules: {}, allowed_users: [], commands_version: 0,
};

export class Store {
  constructor(db) {
    this.db = db;
  }

  async get(k, fallback = null) {
    const row = await this.db.prepare("SELECT v FROM kv WHERE k = ?").bind(k).first();
    return row ? JSON.parse(row.v) : fallback;
  }

  async put(k, value) {
    await this.db.prepare("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT (k) DO UPDATE SET v = excluded.v")
      .bind(k, JSON.stringify(value)).run();
  }

  async settings() {
    return { ...structuredClone(SETTINGS_DEFAULTS), ...(await this.get("settings", {})) };
  }

  // --- deals
  async deal(key) {
    const row = await this.db.prepare("SELECT data FROM deals WHERE key = ?").bind(key).first();
    return row ? JSON.parse(row.data) : null;
  }

  async deals(where = "", ...args) {
    const { results } = await this.db.prepare(`SELECT key, data FROM deals ${where} ORDER BY n`).bind(...args).all();
    return results.map((r) => [r.key, JSON.parse(r.data)]);
  }

  async saveDeal(key, d) {
    await this.db.prepare("UPDATE deals SET status = ?, queued = ?, data = ? WHERE key = ?")
      .bind(d.status, d.queued ? 1 : 0, JSON.stringify(d), key).run();
  }

  /** A new deal gets the next number ("#12") and "🔢 #12" under its text. Null if we already have it. */
  async addDeal(key, d) {
    if (await this.db.prepare("SELECT 1 AS x FROM deals WHERE key = ?").bind(key).first()) return null;
    const n = await this.nextNumber();
    const deal = { votes: {}, messages: [], ...d, n, status: "new", text: `${d.text}\n🔢 #${n}` };
    await this.db.prepare("INSERT INTO deals (key, n, status, sent, queued, data) VALUES (?, ?, ?, ?, ?, ?)")
      .bind(key, n, "new", deal.sent || 0, deal.queued ? 1 : 0, JSON.stringify(deal)).run();
    return deal;
  }

  async nextNumber() {
    await this.db.prepare(
      "INSERT INTO kv (k, v) VALUES ('next_n', '1') ON CONFLICT (k) DO UPDATE SET v = CAST(v AS INTEGER) + 1").run();
    return Number(await this.get("next_n"));
  }

  /** Deals nobody touched (no claim, no votes) are dropped after 30 days. */
  async prune(now) {
    await this.db.prepare("DELETE FROM deals WHERE status = 'new' AND queued = 0 AND sent < ? " +
      "AND json_extract(data, '$.votes') = '{}'").bind(now - 30 * DAY).run();
  }

  // --- 👎 feedback
  async addFeedback(f) {
    await this.db.prepare("INSERT INTO feedback (at, data) VALUES (?, ?)").bind(f.at, JSON.stringify(f)).run();
  }

  async feedbackSince(ts) {
    const { results } = await this.db.prepare("SELECT data FROM feedback WHERE at >= ?").bind(ts).all();
    return results.map((r) => JSON.parse(r.data));
  }
}
