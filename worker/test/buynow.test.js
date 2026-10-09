// 🚨 Buy now (a private alert with sound, ✋ On it, everyone else after 5 minutes) and buys paid with a
// member's own money (👛, /paidby, /refund, "Owed to members" in /pot).

import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, tap, addDeal, OWNER, MARCO, DAYTIME } from "./helpers.js";
import { summarize } from "../src/pot.js";

const LUCA = 777;
const team = { allowed_users: [MARCO, LUCA], roles: { [OWNER]: "manager", [MARCO]: "buyer", [LUCA]: "seller" },
  people: { [OWNER]: "Boss", [MARCO]: "Marco", [LUCA]: "Luca" } };
const dm = (t, id) => t.tg.sent().filter((p) => String(p.chat_id) === String(id));

test("🚨 Buy now: the buyer gets a private alert with sound, price and max offer; once while it's waiting", async () => {
  const t = await setup({ settings: team });
  const key = await addDeal(t.store, { item: { price: 22, total_price: 22.5 } });
  await t.update(tap(`bn:${key}`, { user: LUCA, chat: -100 }));
  const [alert] = dm(t, MARCO);
  assert.match(alert.text, /^🚨 <b>Luca wants this, buy now<\/b>\n#1 Boss DS-1 distortion\n💶 Price: <b>€22\.50<\/b> · 💬 Max offer: <b>€\d+<\/b>$/);
  assert.ok(!("disable_notification" in alert));                                     // with sound
  assert.deepEqual(alert.reply_markup.inline_keyboard[0][0], { text: "✋ On it", callback_data: `bon:${key}` });
  assert.equal(alert.reply_markup.inline_keyboard[1][0].url, "https://www.vinted.it/items/1");
  assert.equal(t.tg.sent("answerCallbackQuery").at(-1).text, "🚨 Sent to Marco");
  assert.equal(dm(t, LUCA).length, 0);                                              // not to whoever tapped
  await t.update(tap(`bn:${key}`, { user: OWNER, chat: -100 }));
  assert.match(t.tg.sent("answerCallbackQuery").at(-1).text, /Already sent \(Luca\)/);
});

test("nobody taps ✋ On it within 5 minutes: everyone else gets it too, once", async () => {
  const t = await setup({ settings: team });
  const key = await addDeal(t.store, { sent: DAYTIME });
  await t.update(tap(`bn:${key}`, { user: LUCA, chat: -100 }), DAYTIME);
  t.tg.clear();
  await t.cron(DAYTIME + 4 * 60);
  assert.equal(dm(t, OWNER).filter((p) => p.text?.includes("wants this")).length, 0);
  await t.cron(DAYTIME + 6 * 60);
  await t.cron(DAYTIME + 11 * 60);
  const late = dm(t, OWNER).filter((p) => p.text?.includes("wants this"));
  assert.equal(late.length, 1);
  assert.match(late[0].text, /^⏰ Nobody answered in 5 minutes\n🚨 <b>Luca wants this/);
  assert.equal(dm(t, MARCO).filter((p) => p.text?.includes("wants this")).length, 0);   // already had it
});

test("✋ On it: claimed for them, the other alerts say who, whoever asked is told, no escalation", async () => {
  const t = await setup({ settings: team });
  const key = await addDeal(t.store, { sent: DAYTIME });
  await t.update(tap(`bn:${key}`, { user: LUCA, chat: -100 }), DAYTIME);
  t.tg.clear();
  await t.update(tap(`bon:${key}`, { user: MARCO, chat: MARCO }), DAYTIME + 60);
  const d = await t.store.deal(key);
  assert.ok(d.status === "claimed" && d.who === "Marco" && d.buy_now.on_it.name === "Marco");
  assert.deepEqual(t.tg.sent("editMessageReplyMarkup")[0].reply_markup.inline_keyboard[0][0].text, "✋ Marco is on it");
  assert.match(dm(t, LUCA).at(-1).text, /^✋ Marco is on #1/);
  await t.update(tap(`bon:${key}`, { user: OWNER, chat: OWNER }), DAYTIME + 90);
  assert.match(t.tg.sent("answerCallbackQuery").at(-1).text, /Marco is already on it/);
  t.tg.clear();
  await t.cron(DAYTIME + 10 * 60);
  assert.ok(!t.tg.texts().some((x) => x.includes("Nobody answered")));
});

test("👛 paid with own money: the pot owes them (cash back up), /refund pays it back, never more than owed", async () => {
  const t = await setup({ settings: team });
  const key = await addDeal(t.store);
  await t.update(msg("/deposit Boss 300"));
  await t.updates(tap(`c:${key}`, { first: "Boss" }), tap(`b:${key}`), msg("30"));
  const confirm = t.tg.sent().find((p) => p.text === "💸 Bought for €30.00");
  assert.deepEqual(confirm.reply_markup.inline_keyboard[0][0], { text: "👛 I paid with my own money", callback_data: `own:${key}` });
  await t.update(tap(`own:${key}`, { user: LUCA }));
  assert.match(t.tg.sent("answerCallbackQuery").at(-1).text, /That's for Boss/);
  await t.update(tap(`own:${key}`));
  let p = summarize(await t.store.ledger(), await t.store.deals());
  assert.deepEqual([p.cash, p.owed], [300, 30]);                                     // the pot didn't pay
  assert.equal(p.members.find((m) => m.name === "Boss").account, 330);               // its 300 + what it's owed
  t.tg.clear();
  await t.update(msg("/pot"));
  assert.match(t.tg.texts().at(-1), /👛 <b>Owed to members: €30\.00<\/b> \(bought with their own money, not paid back yet: Boss €30\.00\)/);
  await t.updates(tap(`own:${key}`), msg("/paidby Boss 1"));
  assert.match(t.tg.texts().at(-1), /already down as paid with someone's own money/);
  await t.updates(msg("/refund Boss 40 1"), msg("/refund Marco 10 1"), msg("/refund Boss 30 1", { user: MARCO }));
  const r = t.tg.texts();
  assert.match(r.at(-3), /owes Boss €30\.00 for #1, not more/);
  assert.match(r.at(-2), /doesn't owe Marco anything for #1/);
  assert.match(r.at(-1), /Only the owner/);
  await t.update(msg("/refund Boss 30 1"));
  assert.ok(t.tg.texts().some((x) => x.includes("−€30.00 refund to Boss for #1")));
  p = summarize(await t.store.ledger(), await t.store.deals());
  assert.deepEqual([p.cash, p.owed], [270, 0]);
  await t.update(msg("/refund Boss 1 1"));
  assert.match(t.tg.texts().at(-1), /doesn't owe Boss anything/);
});

test("/paidby after the fact (owner), and /undo of it", async () => {
  const t = await setup({ settings: team });
  const key = await addDeal(t.store);
  await t.updates(msg("/deposit Boss 300"), tap(`c:${key}`, { first: "Boss" }), tap(`b:${key}`), msg("30"));
  await t.update(msg("/paidby Marco 1 25"));
  let p = summarize(await t.store.ledger(), await t.store.deals());
  assert.equal(p.members.find((m) => m.name === "Marco").owed, 25);
  const id = (await t.store.ledger()).at(-1).id;
  await t.update(msg(`/undo ${id}`));
  p = summarize(await t.store.ledger(), await t.store.deals());
  assert.deepEqual([p.cash, p.owed], [270, 0]);
});

test("reports show refunds paid, what's still owed, and who paid with their own money", async () => {
  const t = await setup({ settings: team });
  const key = await addDeal(t.store);
  await t.updates(msg("/deposit Boss 300"), tap(`c:${key}`, { first: "Boss" }), tap(`b:${key}`), msg("30"), msg("/paidby Marco 1 30"),
    msg("/refund Marco 10 1"));
  const { open } = await import("../src/app.js");
  const { bot } = await open(t.env, { fetchFn: t.tg.fetch, now: t.now });
  const r = await bot.reports.data("week", "2026-10-01", "2026-10-07");
  assert.deepEqual([r.refunds, r.owed, r.buys[0].own], [10, 20, "Marco"]);
  const summary = JSON.stringify(bot.reports.sheets(r, { note: "", items: [] })[0].rows);
  assert.ok(summary.includes("Refunds paid") && summary.includes("Still owed to members"));
});
