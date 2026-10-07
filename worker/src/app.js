// flipFinder Worker: answers Telegram right away (webhook), keeps the private data (D1),
// and gives the GitHub scanner an API to read settings and hand over deals.
//
//   POST /telegram    Telegram webhook (X-Telegram-Bot-Api-Secret-Token = WEBHOOK_SECRET)
//   /api/*            the scanner, with "Authorization: Bearer API_KEY"
//   cron every 5 min  queued overnight deals, reminders, weekly report, cleanup

import { Store } from "./store.js";
import { Telegram } from "./telegram.js";
import { Bot, COMMANDS, COMMANDS_VERSION } from "./bot.js";
import { compare, keyboard } from "./deals.js";
import { TOPIC_FOR_GROUP, dueReminders, weeklyDue, weeklyReport } from "./group.js";
import { DAY, UserError, inQuietHours, nowSeconds } from "./util.js";
import { action, snapshot, verifyInitData } from "./webapp.js";

export const MENU_VERSION = 1;   // bump to set the "📱 Open app" menu button again

export const MAX_QUEUE_FLUSH = 15;

/** Everything a request needs: storage, settings, a Telegram client set up with topics and moved chats. */
export async function open(env, { fetchFn, now } = {}) {
  const store = new Store(env.DB);
  const settings = await store.settings();
  const tg = new Telegram({
    token: env.TELEGRAM_BOT_TOKEN,
    chatIds: String(env.TELEGRAM_CHAT_ID || "").split(",").map((c) => c.trim()).filter(Boolean),
    topics: settings.topics || {},
    migrations: settings.chat_migrations || {},
    api: env.TELEGRAM_API || "https://api.telegram.org",
    fetch: fetchFn,
  });
  const t = now ?? nowSeconds();
  return { store, settings, tg, now: t, bot: new Bot({ store, tg, ownerId: env.OWNER_ID, settings, now: t }) };
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
}

function sameSecret(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export async function handleRequest(request, env, opts = {}) {
  const url = new URL(request.url);
  if (url.pathname === "/telegram" && request.method === "POST") {
    if (!sameSecret(request.headers.get("x-telegram-bot-api-secret-token") || "", env.WEBHOOK_SECRET || "")) {
      return new Response("forbidden", { status: 403 });
    }
    const update = await request.json();
    const ctx = await open(env, opts);
    const { bot, store } = ctx;
    const appUrl = `${url.origin}/app/`;
    if ((await store.get("app_url")) !== appUrl) await store.put("app_url", appUrl);   // for the menu button
    try {
      await bot.handle(update);
    } catch (e) {
      console.error("update failed", e?.stack || e);   // 200 anyway: Telegram would resend it forever
    }
    return new Response("ok");
  }
  if (url.pathname.startsWith("/app/api/")) return handleApp(request, env, opts, url);
  if (url.pathname.startsWith("/api/")) {
    const auth = request.headers.get("authorization") || "";
    if (!sameSecret(auth, `Bearer ${env.API_KEY || ""}`) || !env.API_KEY) return json({ error: "unauthorized" }, 401);
    const route = `${request.method} ${url.pathname}`;
    const handler = API[route];
    if (!handler) return json({ error: "not found" }, 404);
    const body = request.method === "GET" ? null : await request.json();
    return json(await handler(await open(env, opts), body));
  }
  return new Response("flipFinder", { status: 404 });
}

// --- the Mini App's API

async function handleApp(request, env, opts, url) {
  const auth = request.headers.get("authorization") || "";
  const ctx = await open(env, opts);
  const user = await verifyInitData(auth.startsWith("tma ") ? auth.slice(4) : "", env.TELEGRAM_BOT_TOKEN, ctx.now);
  if (!user) return json({ error: "Open the app from Telegram" }, 401);
  const { bot } = ctx;
  if (!bot.allowed(user.id)) return json({ error: "This app is only for the flipFinder group" }, 403);
  bot.capture = [];
  try {
    if (request.method === "GET" && url.pathname === "/app/api/state") return json(await snapshot(bot, user));
    if (request.method === "POST" && url.pathname === "/app/api/action") {
      const notice = await action(bot, user, await request.json());
      await bot.save();
      bot._deals = null;
      bot._pot = null;
      return json({ ok: true, notice, state: await snapshot(bot, user) });
    }
    return json({ error: "not found" }, 404);
  } catch (e) {
    if (e instanceof UserError) return json({ error: e.message }, 400);
    console.error("app request failed", e?.stack || e);
    return json({ error: "Something went wrong" }, 500);
  }
}

// --- the scanner's API

const API = {
  /** Settings (Telegram changes), the home area, the pool and deals we own (for current values). */
  async "GET /api/state"(ctx) {
    const { store, bot } = ctx;
    const open = await store.deals("WHERE status IN ('bought', 'listed')");
    return {
      settings: ctx.settings,
      area: await store.get("area"),
      pool: await bot.pool(),
      open: open.filter(([, d]) => d.item).map(([key, d]) => ({ key, query: d.query || "", item: d.item })),
    };
  },

  /** The searches and rules from config.yaml, so commands can list and change them. */
  async "PUT /api/catalog"({ store }, catalog) {
    const old = await store.get("catalog");
    if (JSON.stringify(old) !== JSON.stringify(catalog)) await store.put("catalog", catalog);
    return { ok: true };
  },

  /** A deal found by the scanner: numbered, sent to every chat (or kept for 07:30 in quiet hours). */
  async "POST /api/deal"(ctx, body) {
    const { store, tg, now, bot } = ctx;
    const quiet = inQuietHours(now);
    const d = await store.addDeal(body.key, { ...body.record, text: body.text, photo: body.photo || "",
      group: body.group || "", sent: now, queued: quiet });
    if (!d) return { status: "exists" };
    if (quiet) return { status: "queued", n: d.n };
    d.messages = await tg.sendAlert(d.text, d.photo, keyboard(body.key, d), TOPIC_FOR_GROUP[d.group]);
    await store.saveDeal(body.key, d);
    await bot.save();
    return { status: d.messages.length ? "sent" : "failed", n: d.n };
  },

  /**
   * After each run: its status (for /status), current market values of what we own (for /sell
   * and price cuts) and messages to post in Summary (daily summary, failure alerts).
   */
  async "POST /api/run"(ctx, body) {
    const { store, tg, bot } = ctx;
    if (body.status) await store.put("status", body.status);
    for (const [key, value] of Object.entries(body.values || {})) {
      const d = await store.deal(key);
      if (d && value !== null && d.value_now !== value) {
        d.value_now = value;
        await store.saveDeal(key, d);
      }
    }
    const notified = [];
    for (const n of body.notify || []) notified.push(await tg.sendText(n.text, { topic: n.topic || "summary" }));
    await bot.save();
    // deals the cron sent from the overnight queue since the last run, for the daily summary
    const flushed = await store.get("flushed", 0);
    if (flushed) await store.put("flushed", 0);
    return { notified, flushed };
  },

  /** One-time move of deals.json / settings.json (and the home area) into private storage. */
  async "POST /api/import"({ store }, body) {
    const have = await store.db.prepare("SELECT COUNT(*) AS c FROM deals").first();
    if (have.c && !body.force && body.deals && Object.keys(body.deals).length) {
      return { error: "deals already imported (send force: true to add/overwrite)" };
    }
    if (body.settings) await store.put("settings", body.settings);
    if (body.area) await store.put("area", body.area);
    for (const k of ["pool", "pending", "last_weekly"]) if (body[k] !== undefined) await store.put(k, body[k]);
    const queue = new Set(body.queue || []);
    const rows = Object.entries(body.deals || {}).map(([key, d]) => ({
      key, n: d.n ?? null, status: d.status, sent: d.sent || 0, queued: queue.has(key) ? 1 : 0,
      data: JSON.stringify({ ...d, queued: queue.has(key) }),
    }));
    if (rows.length) {
      // one statement for the whole batch (D1 limits queries per request)
      await store.db.prepare(
        "INSERT INTO deals (key, n, status, sent, queued, data) SELECT json_extract(value, '$.key'), " +
        "json_extract(value, '$.n'), json_extract(value, '$.status'), json_extract(value, '$.sent'), " +
        "json_extract(value, '$.queued'), json_extract(value, '$.data') FROM json_each(?) WHERE true " +
        "ON CONFLICT (key) DO UPDATE SET n = excluded.n, status = excluded.status, sent = excluded.sent, " +
        "queued = excluded.queued, data = excluded.data").bind(JSON.stringify(rows)).run();
    }
    if (body.feedback?.length) {
      await store.db.prepare("INSERT INTO feedback (at, data) SELECT json_extract(value, '$.at'), value " +
        "FROM json_each(?)").bind(JSON.stringify(body.feedback.map((f) => ({ ...f, at: f.at || 0 })))).run();
    }
    const top = await store.db.prepare("SELECT MAX(n) AS n FROM deals").first();
    const next = Math.max(Number(await store.get("next_n", 0)), Number(body.next_n || 0), Number(top.n || 0));
    await store.put("next_n", next);
    const count = await store.db.prepare("SELECT COUNT(*) AS c FROM deals").first();
    return { ok: true, deals: count.c, next_n: next };
  },
};

// --- every 5 minutes

export async function runCron(env, opts = {}) {
  const { store, tg, bot, now, settings } = await open(env, opts);
  await store.prune(now);
  if (settings.commands_version !== COMMANDS_VERSION) {
    const commands = COMMANDS.map(([command, description]) => ({ command, description }));
    let ok = true;
    for (const type of ["default", "all_group_chats"]) {
      ok = (await tg.call("setMyCommands", { commands, scope: { type } })) !== null && ok;
    }
    if (ok) {
      settings.commands_version = COMMANDS_VERSION;
      bot.changed = true;
    }
  }
  // the "📱 Open app" button next to the message box in private chats
  const appUrl = await store.get("app_url");
  if (appUrl && settings.menu_version !== `${MENU_VERSION}:${appUrl}`) {
    const ok = await tg.call("setChatMenuButton", { menu_button: { type: "web_app", text: "📱 Open app",
      web_app: { url: appUrl } } });
    if (ok !== null) {
      settings.menu_version = `${MENU_VERSION}:${appUrl}`;
      bot.changed = true;
    }
  }
  // weekly report, Sunday 20:00 Italy time
  if (weeklyDue(await store.get("last_weekly"), now)) {
    const text = weeklyReport(await store.deals(), await store.feedbackSince(now - 7 * DAY), now);
    if (await tg.sendText(text, { topic: "summary" })) await store.put("last_weekly", new Date(now * 1000)
      .toLocaleDateString("en-CA", { timeZone: "Europe/Rome" }));
  }
  // deals found overnight, best first, once quiet hours are over
  if (!inQuietHours(now)) {
    const queued = (await store.deals("WHERE queued = 1")).filter(([, d]) => d.status === "new");
    queued.sort((a, b) => compare(b[1].score || [0, 0, 0, 0], a[1].score || [0, 0, 0, 0]));
    let flushed = 0;
    for (const [key, d] of queued.slice(0, MAX_QUEUE_FLUSH)) {
      if (tg.callsLeft < 8) break;   // the rest go out in 5 minutes
      d.messages = [...(d.messages || []), ...(await tg.sendAlert(d.text, d.photo, keyboard(key, d), TOPIC_FOR_GROUP[d.group || ""]))];
      d.queued = false;
      await store.saveDeal(key, d);
      flushed += d.messages.length ? 1 : 0;
    }
    if (flushed) await store.put("flushed", Number(await store.get("flushed", 0)) + flushed);
  }
  // claims with no update, things bought but not listed, things listed but not sold
  await bot.reminders(dueReminders(await store.deals("WHERE status IN ('claimed', 'bought', 'listed')"), now));
  await bot.save();
}
