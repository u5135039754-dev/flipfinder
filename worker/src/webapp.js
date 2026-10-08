// The Telegram Mini App's API (/app/api/*). Every request carries Telegram's signed initData
// ("Authorization: tma <initData>"), checked against the bot token: only allowed users get in.
// Actions run through the same Bot code as the chat buttons, so deal messages, money posts
// and the €50 approval rule behave exactly the same.

import { forApp } from "./handbook.js";
import { UserError, nowSeconds, parseNumber } from "./util.js";
import { shares } from "./pot.js";
import { romeTs } from "./util.js";
import { TOPIC_FOR_GROUP } from "./group.js";

const enc = new TextEncoder();
const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");

async function hmac(key, data) {
  const k = await crypto.subtle.importKey("raw", typeof key === "string" ? enc.encode(key) : key,
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return crypto.subtle.sign("HMAC", k, enc.encode(data));
}

/** Telegram's check: hash = HMAC(HMAC("WebAppData", token), sorted "key=value" lines). The user, or null. */
export async function verifyInitData(initData, token, now = nowSeconds(), maxAge = 86400) {
  if (!initData || !token) return null;
  const params = new URLSearchParams(initData);
  const hash = params.get("hash") || "";
  params.delete("hash");
  const check = [...params.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([k, v]) => `${k}=${v}`).join("\n");
  const expected = hex(await hmac(await hmac("WebAppData", token), check));
  let diff = expected.length ^ hash.length;
  for (let i = 0; i < expected.length; i++) diff |= expected.charCodeAt(i) ^ (hash.charCodeAt(i) || 0);
  const authDate = Number(params.get("auth_date"));
  if (diff || !authDate || now - authDate > maxAge) return null;
  try {
    const user = JSON.parse(params.get("user") || "null");
    return user?.id ? user : null;
  } catch {
    return null;
  }
}

/** What the app shows: deals to grab, our stock, the pot, settings. */
export async function snapshot(bot, user) {
  const deals = await bot.allDeals();
  const now = bot.now;
  const card = ([key, d]) => ({
    key, n: d.n, title: d.title, url: d.url, photo: d.photo || "", source: d.source || "vinted",
    group: d.group || "", topic: TOPIC_FOR_GROUP[d.group || ""] || "", cost: d.cost ?? null, value: d.value ?? null,
    profit: d.profit ?? null, roi: d.cost ? Math.round((d.profit || 0) / d.cost * 100) : null, rating: d.rating ?? null,
    status: d.status, who: d.who || "", mine: d.who_id === user.id, sent: d.sent || 0,
    paid: d.paid ?? null, value_now: d.value_now ?? null, sell_days: d.sell_days ?? null, demand: d.demand || "", seller: d.seller || "",
    up: Object.values(d.votes || {}).filter((v) => v === "up").length,
    down: Object.values(d.votes || {}).filter((v) => v === "down").length,
    voted: (d.votes || {})[String(user.id)] || "",
    request: d.request ? { amount: d.request.amount, by: d.request.by } : null,
  });
  const open = deals.filter(([, d]) => (d.status === "new" && (d.sent || 0) >= now - 7 * 86400) || d.status === "claimed");
  const stockList = deals.filter(([, d]) => ["bought", "listed"].includes(d.status));
  const pot = await bot.pot();
  const ledger = await bot.store.ledger();
  // profit over time for the Pot chart: the running total after each entry that moved it
  let running = 0;
  const series = [];
  for (const e of ledger) {
    const x = Object.values(e.profit || {}).reduce((s, v) => s + v, 0);
    if (!x) continue;
    running = Math.round((running + x) * 100) / 100;
    series.push({ at: e.at, profit: running });
  }
  const view = await bot.view();
  return {
    me: { id: user.id, name: [user.first_name, user.last_name].filter(Boolean).join(" "), owner: user.id === bot.ownerId,
      manager: bot.team.isManager(user.id), team: bot.team.active },
    schedule: await bot.team.grid(),
    handbook: forApp(await bot.handbook.load()),
    deals: open.map(card).sort((a, b) => b.sent - a.sent),
    stock: stockList.map(card),
    pot: { ...pot, shares: shares(pot, bot.splitMode()), split: bot.splitMode(), series,
      ledger: ledger.slice(-60).reverse().map((e) => ({ id: e.id, at: e.at, kind: e.kind, amount: e.amount,
        member: e.member || "", n: e.n || null, note: e.note || "", ref: e.ref || null })) },
    settings: {
      searches: view.searches.map((s) => ({ query: s.query, group: s.group, enabled: s.enabled, budget: s.budget,
        price_from: s.price_from ?? null, price_to: s.price_to ?? null })),
      rules: view.rules, budget_rules: view.budget_rules, budget_setting: view.budget_setting, budget: view.budget,
    },
  };
}

const strip = (html) => String(html || "").replace(/<[^>]+>/g, "").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
  .replace(/&quot;/g, '"').replace(/&#x27;/g, "'").replace(/&amp;/g, "&");

/** One tap or change from the app. Returns a short notice for the user. */
export async function action(bot, user, body) {
  const uid = user.id;
  const chat = uid;                       // anything the bot would reply goes back to the app instead
  const cq = { id: null, from: user, app: true };
  const deal = async () => {
    const d = await bot.store.deal(body.key || "");
    if (!d) throw new UserError("I don't have this deal any more");
    return d;
  };
  const amount = () => {
    const a = parseNumber(body.amount ?? "");
    if (a === null || !(a >= 0 && a <= 100_000)) throw new UserError("Give the amount in euros, e.g. 45");
    return Math.round(a * 100) / 100;
  };
  switch (body.action) {
    case "claim": case "up": case "down": case "listed": case "message": {
      await deal();
      const kind = { claim: "c", up: "up", down: "dn", listed: "l", message: "m" }[body.action];
      await bot.onDealButton(cq, chat, uid, kind, body.key);
      break;
    }
    case "bought": case "sold": {
      const d = await deal();
      const want = body.action === "bought" ? "claimed" : "listed";
      if (uid !== d.who_id && uid !== bot.ownerId) throw new UserError(`${d.who || "Someone else"} has this one`);
      if (d.status !== want) throw new UserError(body.action === "bought" ? "Claim it first" : "List it first");
      const a = amount();
      if (body.action === "bought" && d.request) throw new UserError("Already waiting for the manager's OK");
      const pending = await bot.store.get("pending", {});
      pending[String(uid)] = { key: body.key, ask: body.action === "bought" ? "paid" : "sold_for", chat, ask_msg: null };
      await bot.store.put("pending", pending);
      await bot.applyPrice(chat, uid, a);
      break;
    }
    case "approve": case "reject":
      await deal();
      if (!bot.team.isManager(uid)) throw new UserError("Only a manager approves buys");
      await bot.decideBuy(cq, uid, body.key, body.action === "approve");
      break;
    case "duty_start":
      bot.capture.push(await bot.team.start(uid));
      break;
    case "duty_end":
      bot.capture.push(await bot.team.end(uid));
      break;
    case "block":
      bot.capture.push(await bot.team.toggleBlock(uid, String(body.date || ""), Number(body.hour)));
      break;
    case "swap_block": {
      const start = romeTs(String(body.date || ""), Number(body.hour));
      const owner = (await bot.team.schedule())[body.date]?.[Number(body.hour)];
      if (owner !== uid) throw new UserError("You can only ask for a swap on your own block");
      bot.capture.push(await bot.team.requestSwap(uid, Math.max(start, bot.now), start + 3600));
      break;
    }
    case "sell": {
      const d = await deal();
      await bot.cmd_sell(chat, [String(d.n), body.lang || "it"], uid);
      break;
    }
    case "toggle": {
      const s = (await bot.view()).searches.find((x) => x.query === body.query);
      if (!s) throw new UserError("That search is gone");
      const dis = new Set(bot.settings.disabled);
      if (s.enabled) dis.add(s.query);
      else dis.delete(s.query);
      bot.settings.disabled = [...dis].sort();
      bot.changed = true;
      bot.capture.push(`${s.query}: ${s.enabled ? "off" : "on"}`);
      break;
    }
    case "price":
      await bot.cmd_setprice(chat, [String(body.query || ""), String(body.from ?? ""), String(body.to ?? "")], uid);
      break;
    case "budget":
      await bot.cmd_budget(chat, [String(body.value ?? "")], uid);
      break;
    case "rule":
      await bot.cmd_setrule(chat, [String(body.name || ""), String(body.value ?? "")], uid);
      break;
    case "split":
      await bot.cmd_split(chat, [String(body.value || "")], uid);
      break;
    default:
      throw new UserError("Unknown action");
  }
  const said = bot.capture.map(strip).filter(Boolean);
  const problem = said.find((x) => x.startsWith("⚠️"));
  if (problem) throw new UserError(problem.replace(/^⚠️\s*/, ""));
  return said.join("\n");
}
