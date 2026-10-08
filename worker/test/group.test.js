import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, tap, addDeal, MARCO, DAYTIME } from "./helpers.js";
import { dueReminders, weeklyDue, weeklyReport } from "../src/group.js";
import { inQuietHours } from "../src/util.js";

const withMarco = { allowed_users: [MARCO] };
const at = (y, mo, d, h, mi) => Date.UTC(y, mo - 1, d, h, mi) / 1000;

test("quiet hours are Italy time", () => {
  // October: Italy is UTC+2
  assert.ok(!inQuietHours(at(2026, 10, 7, 21, 59)) && inQuietHours(at(2026, 10, 7, 22, 0)));   // 23:59 / 00:00
  assert.ok(inQuietHours(at(2026, 10, 7, 5, 59)) && !inQuietHours(at(2026, 10, 7, 6, 0)));     // 07:59 / 08:00
  assert.ok(inQuietHours(at(2026, 1, 15, 6, 0)));       // 07:00 in Rome (winter)
  assert.ok(!inQuietHours(at(2026, 7, 15, 6, 0)));      // 08:00 in Rome (summer)
});

test("reminder schedule", () => {
  const t0 = 1_000_000;
  const d = { status: "claimed", who: "Marco", who_id: MARCO, claimed_at: t0 };
  const due = (now) => dueReminders([["k", d]], now);
  assert.deepEqual(due(t0 + 23 * 3600), []);
  assert.deepEqual(due(t0 + 25 * 3600), [["ping", "k"]]);
  d.pinged_at = t0 + 25 * 3600;
  assert.deepEqual(due(t0 + 30 * 3600), []);                 // pinged once
  assert.deepEqual(due(t0 + 49 * 3600), [["release", "k"]]);
  d.kept_at = t0 + 30 * 3600;                                 // "keep it" restarts the clock
  assert.deepEqual(due(t0 + 49 * 3600), []);
  Object.assign(d, { status: "bought", bought_at: t0 });
  assert.deepEqual(due(t0 + 2 * 86400), []);
  assert.deepEqual(due(t0 + 3 * 86400), [["list", "k"]]);
  Object.assign(d, { status: "listed", listed_at: t0 });
  assert.deepEqual(due(t0 + 13 * 86400), []);
  assert.deepEqual(due(t0 + 14 * 86400), [["cut", "k"]]);
  d.cut_at = t0 + 14 * 86400;
  assert.deepEqual(due(t0 + 18 * 86400), []);
  assert.deepEqual(due(t0 + 21 * 86400), [["cut", "k"]]);
});

test("reminders: ping, keep, release, list nudge and price cut", async () => {
  const t = await setup({ settings: withMarco });
  const key = await addDeal(t.store);
  const now = DAYTIME;
  const set = async (fields) => {
    const d = await t.store.deal(key);
    for (const [k, v] of Object.entries(fields)) if (v === undefined) delete d[k]; else d[k] = v;
    await t.store.saveDeal(key, d);
  };
  await set({ status: "claimed", who: "Marco", who_id: MARCO, claimed_at: now - 25 * 3600 });
  await t.cron(now);
  const ping = t.tg.sent()[0];
  assert.ok(ping.text.includes('href="tg://user?id=555">Marco</a>, still on it?'));
  assert.deepEqual(ping.reply_markup.inline_keyboard[0].map((b) => b.callback_data), [`keep:${key}`, `rel:${key}`]);
  assert.equal(ping.reply_parameters.message_id, 8);           // a reply to the deal in the group
  await t.updates(tap(`keep:${key}`, { user: 777 }), tap(`keep:${key}`, { user: MARCO }));
  let d = await t.store.deal(key);
  assert.ok(d.kept_at && d.status === "claimed");
  await set({ claimed_at: now - 50 * 3600, kept_at: now - 49 * 3600 });   // 49 h since the last "keep it"
  t.tg.clear();
  await t.cron(now);
  d = await t.store.deal(key);
  assert.ok(d.status === "new" && !("who" in d));
  assert.match(t.tg.texts()[0], /is free again/);
  assert.ok(t.tg.sent("editMessageText").length);               // deal message updated
  await set({ status: "bought", who: "Marco", who_id: MARCO, paid: 30, bought_at: now - 3 * 86400 });
  t.tg.clear();
  await t.cron(now);
  assert.ok(t.tg.texts()[0].includes("Time to list it?") && t.tg.texts()[0].includes("/sell 1"));
  await set({ status: "listed", listed_at: now - 15 * 86400 });
  await t.api("POST", "/api/run", { values: { [key]: 52.0 } });  // the scanner's current market value
  t.tg.clear();
  await t.cron(now);
  const cut = t.tg.texts()[0];
  assert.ok(cut.includes("listed for 15 days") && cut.includes("€52") && cut.includes("<b>€50</b>"));
});

test("/sell by number or name in three languages, pickup town from the private area", async () => {
  const t = await setup();
  const key = await addDeal(t.store, { title: "🔥🔥 BOSS DS-1 Distortion pedale chitarra originale made in Taiwan anni 90 perfetto",
    condition: "Ottime" });
  const d = await t.store.deal(key);
  Object.assign(d, { status: "bought", who: "Marco", who_id: MARCO, paid: 30 });
  await t.store.saveDeal(key, d);
  await t.store.put("area", { city: "Paesino" });
  await t.updates(msg("/sell 1"), msg("/sell boss ds-1 en"), msg("/sell ds-1 uk"), msg("/sell zoom g1x"));
  const [it, en, uk, missing] = t.tg.texts();
  const title = it.split("<code>")[1].split("</code>")[0];
  assert.ok(title.length <= 60 && !title.includes("🔥") && title.startsWith("BOSS DS-1 Distortion"));
  assert.ok(it.includes("Condizioni: Ottime") && it.includes("Prezzo consigliato: <b>€60</b>") && it.includes("vendita veloce"));
  assert.ok(it.includes("ritiro a mano a Paesino"));
  assert.ok(en.includes("Condition: very good") && en.includes("Suggested price") && en.includes("pickup in Paesino"));
  assert.match(uk, /Стан: дуже добрий/);
  assert.match(missing, /No deal matching/);
});

test("weekly report once on Sunday evening, from the cron", async () => {
  const sun = at(2026, 10, 11, 18, 5);   // 20:05 in Italy
  const t = await setup({ now: sun });
  const key = await addDeal(t.store);
  const d = await t.store.deal(key);
  Object.assign(d, { status: "sold", who: "Marco", who_id: MARCO, paid: 30, sold_for: 75, sent: sun - 86400,
    claimed_at: sun - 80000, bought_at: sun - 70000, sold_at: sun - 3600 });
  await t.store.saveDeal(key, d);
  await t.store.addDeal("vinted:2", { title: "Big Muff", url: "u2", text: "", sent: sun - 5000, query: "big muff" });
  for (const [k, a] of [["vinted:2", 4000], ["vinted:2", 3000], [key, 2000]]) await t.store.addFeedback({ key: k, at: sun - a });
  assert.ok(!weeklyDue(null, at(2026, 10, 11, 17, 59)));
  assert.ok(weeklyDue(null, sun) && !weeklyDue(null, at(2026, 10, 12, 18, 5)));
  const text = weeklyReport(await t.store.deals(), await t.store.feedbackSince(sun - 7 * 86400), sun);
  assert.match(text, /week to Sun 11 Oct/);
  assert.match(text, /Deals found: 2 · claimed: 1 · bought: 1 · sold: 1/);
  assert.ok(text.includes("Marco €45.00") && text.includes("Best flip") && text.includes("€30.00 → €75.00 (+€45.00)"));
  assert.match(text, /Most down-voted search: <b>big muff<\/b> \(2×\)/);
  await t.cron(sun);
  await t.cron(sun + 300);
  assert.equal(t.tg.texts().filter((x) => x.includes("week to")).length, 2);   // once, to both chats
  assert.equal(await t.store.get("last_weekly"), "2026-10-11");
});
