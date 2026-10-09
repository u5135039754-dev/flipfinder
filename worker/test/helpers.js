// Test helpers: D1 on Node's built-in SQLite, a fake Telegram that records every call.

import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { handleRequest, runCron } from "../src/app.js";
import { Store } from "../src/store.js";

export const OWNER = 1000001;
export const MARCO = 555;
export const GROUP = "-100444";
export const DAYTIME = Date.UTC(2026, 9, 7, 10, 0) / 1000;   // 12:00 in Italy
export const NIGHT = Date.UTC(2026, 9, 7, 1, 0) / 1000;      // 03:00 in Italy

const SCHEMA = readFileSync(new URL("../schema.sql", import.meta.url), "utf8");

/** The subset of the D1 API the Worker uses. */
export function fakeD1() {
  const db = new DatabaseSync(":memory:");
  db.exec(SCHEMA);
  const statement = (sql, args = []) => ({
    bind: (...a) => statement(sql, a),
    first: async () => {
      const row = db.prepare(sql).get(...args);
      return row === undefined ? null : { ...row };
    },
    all: async () => ({ results: db.prepare(sql).all(...args).map((r) => ({ ...r })) }),
    run: async () => {
      const r = db.prepare(sql).run(...args);
      return { meta: { changes: r.changes } };
    },
  });
  return { prepare: (sql) => statement(sql) };
}

/** Records Bot API calls; `fail(method, payload)` can return an error response. */
export function fakeTelegram() {
  const calls = [];
  let nextId = 900;
  const t = {
    calls,
    fail: () => null,
    sent: (method = "sendMessage") => calls.filter((c) => c.method === method).map((c) => c.payload),
    texts: () => t.sent().map((p) => p.text),
    clear: () => calls.splice(0),
    claude: [],          // requests the Worker sent to Anthropic's API (a stand-in answers them)
    answerClaude: null,  // (body, n) => a Messages API response, or {status, error}
    fetch: async (url, init = {}) => {
      url = String(url);
      if (url.startsWith("https://api.anthropic.com/")) {
        const body = JSON.parse(init.body);
        t.claude.push({ url, body, headers: Object.fromEntries(new Headers(init.headers || {})) });
        const r = t.answerClaude ? await t.answerClaude(body, t.claude.length) : { status: 500, error: "no stand-in" };
        if (r.status && r.status !== 200) {
          return new Response(JSON.stringify({ type: "error", error: { type: "api_error", message: r.error || "boom" } }),
            { status: r.status, headers: { "content-type": "application/json" } });
        }
        return new Response(JSON.stringify({ id: `msg_${t.claude.length}`, type: "message", role: "assistant",
          model: body.model, stop_reason: "end_turn", usage: { input_tokens: 1000, output_tokens: 100 }, ...r }),
        { headers: { "content-type": "application/json" } });
      }
      if (url.includes("/file/bot")) {   // a photo someone sent the bot
        calls.push({ method: "download", payload: { url } });
        return new Response(new Uint8Array([255, 216, 255, 224, 1, 2, 3]));
      }
      const method = url.split("/").pop();
      let payload;
      if (init.body instanceof FormData) {   // a file upload (sendDocument): the file as {name, type, text}
        payload = {};
        for (const [k, v] of init.body.entries()) payload[k] = typeof v === "string" ? v : { name: v.name, type: v.type, text: await v.text() };
      } else payload = JSON.parse(init.body);
      calls.push({ method, payload });
      const failure = t.fail(method, payload);
      const data = failure || { ok: true, result: method === "sendMessage" || method === "sendPhoto" || method === "sendDocument"
        ? { message_id: ++nextId, chat: { id: Number(payload.chat_id) }, ...(method === "sendPhoto" ? { photo: [{}] } : {}),
          ...(payload.message_thread_id ? { message_thread_id: payload.message_thread_id } : {}) }
        : true };
      return new Response(JSON.stringify(data), { headers: { "content-type": "application/json" } });
    },
  };
  return t;
}

export const CATALOG = {
  searches: [
    { query: "boss katana", price_from: 80, price_to: 300, budget: false, group: "Amps", price_to_is_budget: false },
    { query: "iphone 13", price_from: 140, price_to: 250, budget: false, group: "Electronics", price_to_is_budget: false },
    { query: "boss ds 1", price_from: 15, price_to: 72, budget: true, group: "Budget", price_to_is_budget: true },
  ],
  rules: { min_profit: 25, min_roi: 30, min_rating: 5, max_roi: 120 },
  budget_rules: { min_profit: 12, min_roi: 35, max_roi: 150 },
  budget: 72, ebay: true, subito: true,
};

export async function setup({ settings, catalog = CATALOG, chatIds = `${OWNER},${GROUP}`, now = DAYTIME } = {}) {
  const db = fakeD1();
  const store = new Store(db);
  const tg = fakeTelegram();
  const env = { DB: db, TELEGRAM_BOT_TOKEN: "TOKEN", TELEGRAM_CHAT_ID: chatIds, OWNER_ID: String(OWNER),
    API_KEY: "test-key", WEBHOOK_SECRET: "hook-secret" };
  if (catalog) await store.put("catalog", catalog);
  // roles are the only key: test members on the allow list get one unless the test says otherwise
  if (settings?.allowed_users && !settings.roles) {
    settings = { ...settings, roles: Object.fromEntries(settings.allowed_users.map((id) => [id, "seller"])) };
  }
  if (settings) await store.put("settings", settings);
  const ctx = { db, store, tg, env, now };
  ctx.update = async (u, at) => {
    const r = await handleRequest(new Request("https://w/telegram", { method: "POST",
      headers: { "x-telegram-bot-api-secret-token": "hook-secret" }, body: JSON.stringify(u) }), env,
    { fetchFn: tg.fetch, now: at ?? ctx.now });
    if (r.status !== 200) throw new Error(`webhook ${r.status}`);
  };
  ctx.updates = async (...us) => { for (const u of us) await ctx.update(u); };
  ctx.api = async (method, path, body, { at, key = "test-key", ...opts } = {}) => {
    const r = await handleRequest(new Request(`https://w${path}`, { method,
      headers: { authorization: `Bearer ${key}`, "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body) }), env, { fetchFn: tg.fetch, now: at ?? ctx.now, ...opts });
    return { status: r.status, body: await r.json() };
  };
  ctx.cron = (at) => runCron(env, { fetchFn: tg.fetch, now: at ?? ctx.now });
  ctx.settings = () => store.settings();
  return ctx;
}

let updateId = 0;
export function msg(text, { user = OWNER, chat = 111, first = null } = {}) {
  return { update_id: ++updateId, message: { text, from: { id: user, ...(first ? { first_name: first } : {}) },
    chat: { id: chat } } };
}

export function tap(data, { user = OWNER, chat = 111, first = null } = {}) {
  return { update_id: ++updateId, callback_query: { id: `cq${updateId}`, data,
    from: { id: user, ...(first ? { first_name: first } : {}) }, message: { chat: { id: chat }, message_id: 55 } } };
}

export function topicMsg(text, thread, createdName = null, user = OWNER) {
  const m = msg(text, { user, chat: Number(GROUP) });
  Object.assign(m.message, { message_thread_id: thread, is_topic_message: true });
  if (createdName) m.message.reply_to_message = { message_id: thread, forum_topic_created: { name: createdName } };
  return m;
}

/** A recorded deal like the scanner sends one, already posted in the private chat and the group. */
export async function addDeal(store, overrides = {}) {
  const d = await store.addDeal("vinted:1", {
    title: "Boss DS-1 distortion", url: "https://www.vinted.it/items/1", source: "vinted",
    cost: 26.95, value: 60, low: 60, profit: 25.1, sent: DAYTIME - 3600, query: "boss ds 1", group: "Budget",
    condition: "Buone", photo: "", text: "🔥 <b>Boss DS-1 distortion</b>", score: [1, 0.9, 7, 25.1], ...overrides,
  });
  d.messages = [{ chat: "111", id: 7, photo: false }, { chat: "-100", id: 8, photo: true }];
  await store.saveDeal("vinted:1", d);
  return "vinted:1";
}
