import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, tap, addDeal, OWNER, MARCO, GROUP } from "./helpers.js";
import { allocate, summarize } from "../src/pot.js";

const LUCA = 777;
const members = { allowed_users: [MARCO, LUCA], topics: { summary: 44 } };

async function sold(t, key, paid, soldFor) {
  const d = await t.store.deal(key);
  Object.assign(d, { status: "claimed", who: "Marco", who_id: MARCO });
  await t.store.saveDeal(key, d);
  await t.updates(tap(`b:${key}`, { user: MARCO }), msg(String(paid), { user: MARCO }), tap(`ap:${key}`),
    tap(`l:${key}`, { user: MARCO }), tap(`s:${key}`, { user: MARCO }), msg(String(soldFor), { user: MARCO }));
}

test("deposits: owner only, posted in the group's Summary topic, /pot shows everyone's share", async () => {
  const t = await setup({ settings: members });
  await t.updates(msg("/deposit Owner 100"), msg("/deposit Marco 100"), msg("/deposit luca 100"),
    msg("/deposit Luca 50", { user: MARCO }), msg("/deposit Luca"), msg("/deposit Luca -5"));
  const sent = t.tg.sent();
  const posts = sent.filter((p) => String(p.chat_id) === GROUP);
  assert.equal(posts.length, 3);
  assert.ok(posts.every((p) => p.message_thread_id === 44));
  assert.match(posts[0].text, /\+€100\.00 deposit from Owner · cash now <b>€100\.00<\/b>/);
  assert.match(posts[2].text, /deposit from luca · cash now <b>€300\.00<\/b>/);
  const replies = sent.filter((p) => String(p.chat_id) === "111").map((p) => p.text);
  assert.equal(replies.filter((x) => x.startsWith("💶")).length, 3);       // the private chat gets a copy
  assert.ok(replies.some((x) => x.includes("Only the owner can use /deposit")));
  assert.ok(replies.some((x) => x.includes("Who and how much?")) && replies.some((x) => x.includes("between €0.01")));
  t.tg.clear();
  await t.update(msg("/pot", { user: MARCO }));
  const pot = t.tg.texts()[0];
  assert.match(pot, /🏦 Owner holds the pot's money \(treasurer\); the bot only keeps the numbers\nCash: <b>€300\.00<\/b> · in stock \(at cost\): €0\.00/);
  assert.ok(pot.includes("· Owner: €100.00 · €0.00 (33%) · <b>€100.00</b>"));
  assert.equal((await t.store.ledger()).length, 3);
});

test("only a manager approves; a rejection keeps the claim; the seller is asked to list it", async () => {
  const t = await setup({ settings: { ...members, roles: { [OWNER]: "manager", [MARCO]: "buyer", [LUCA]: "seller" },
    people: { [OWNER]: "Boss", [MARCO]: "Marco", [LUCA]: "Luca" } } });
  await t.update(msg("/deposit Owner 300"));
  const key = await addDeal(t.store, { cost: 60, source: "subito" });
  const deal = await t.store.deal(key);
  deal.messages = [{ chat: GROUP, id: 8, photo: false }];
  await t.store.saveDeal(key, deal);
  await t.updates(tap(`c:${key}`, { user: MARCO, first: "Marco" }), tap(`b:${key}`, { user: MARCO }), msg("62", { user: MARCO }));
  const ask = t.tg.sent().filter((p) => p.text?.includes("asks to buy"));
  assert.deepEqual(ask.map((p) => String(p.chat_id)), [String(OWNER), GROUP]);          // the manager privately + the group
  assert.ok(ask[0].text.includes("Subito pickup: send Marco €62.00 for it"));
  assert.deepEqual(ask[0].reply_markup.inline_keyboard[0].map((b) => b.callback_data), [`ap:${key}`, `rj:${key}`]);
  t.tg.clear();
  await t.update(tap(`ap:${key}`, { user: LUCA }));                                      // not a manager
  assert.match(t.tg.sent("answerCallbackQuery")[0].text, /Only a manager/);
  await t.update(tap(`rj:${key}`));
  let d = await t.store.deal(key);
  assert.ok(d.status === "claimed" && !d.request && d.who === "Marco");
  assert.ok(t.tg.texts().some((x) => x.includes("didn't approve buying #1")));
  assert.equal(t.tg.sent("editMessageReplyMarkup").length, 2);                            // both Approve buttons gone
  await t.updates(tap(`b:${key}`, { user: MARCO }), msg("58", { user: MARCO }));
  t.tg.clear();
  await t.updates(tap(`ap:${key}`), tap(`ap:${key}`));                                   // a double tap pays once
  d = await t.store.deal(key);
  assert.ok(d.status === "bought" && d.paid === 58);
  assert.equal(summarize(await t.store.ledger(), await t.store.deals()).cash, 242);
  const texts = t.tg.texts();
  assert.ok(texts.includes("📦 Listing kit sent to Luca"));                                // a note in the deal's thread
  assert.ok(t.tg.sent().some((p) => String(p.chat_id) === String(LUCA) && p.text.startsWith("📦 <b>Listing kit · #1</b>")));
  assert.ok(t.tg.sent().some((p) => String(p.chat_id) === String(MARCO) && p.text.includes("Approved: #1 at €58.00")));
  assert.match(t.tg.sent("answerCallbackQuery").at(-1).text, /already decided/);
});

test("withdrawals are limited to the member's money and the cash; /undo adds a reversing entry", async () => {
  const t = await setup({ settings: members });
  await t.updates(msg("/deposit Owner 100"), msg("/deposit Marco 100"));
  const key = await addDeal(t.store);
  const d = await t.store.deal(key);
  Object.assign(d, { status: "claimed", who: "Marco", who_id: MARCO });
  await t.store.saveDeal(key, d);
  await t.updates(tap(`b:${key}`, { user: MARCO }), msg("40", { user: MARCO }), tap(`ap:${key}`));   // €160 cash, €40 in stock
  t.tg.clear();
  await t.updates(msg("/withdraw Marco 150"), msg("/withdraw Nobody 5"), msg("/withdraw Marco 60", { user: MARCO }));
  const r = t.tg.texts();
  assert.match(r[0], /Marco has €100\.00 in the pot, can't take out €150\.00/);
  assert.match(r[1], /No member called "Nobody"/);
  assert.match(r[2], /Only the owner/);
  await t.updates(msg("/deposit Luca 10"), msg("/withdraw Owner 100"), msg("/withdraw Marco 80"));
  assert.ok(t.tg.texts().some((x) => x.includes("only has €70.00 in cash (the rest is in stock)")));
  const before = JSON.stringify(await t.store.ledger());
  const withdrawal = (await t.store.ledger()).find((e) => e.kind === "withdraw");
  await t.updates(msg(`/undo ${withdrawal.id}`), msg(`/undo ${withdrawal.id}`), msg("/undo 3"), msg("/undo 99"));
  const after = await t.store.ledger();
  assert.equal(JSON.stringify(after.slice(0, -1)), before);                    // nothing changed, one entry added
  assert.deepEqual([after.at(-1).kind, after.at(-1).amount, after.at(-1).ref], ["undo", 100, withdrawal.id]);
  const texts = t.tg.texts();
  assert.ok(texts.some((x) => x.includes(`was already undone`)));
  assert.ok(texts.some((x) => x.includes("Buys and sales follow the deal")));   // entry 3 is the buy
  assert.ok(texts.some((x) => x.includes("Which entry?")));
  const p = summarize(after, await t.store.deals());
  assert.deepEqual([p.cash, p.stock], [170, 40]);
  assert.equal(p.members.find((m) => m.name === "Owner").account, 100);
  t.tg.clear();
  await t.update(msg("/ledger"));
  const ledger = t.tg.texts()[0];
  assert.ok(ledger.includes("2. +€100.00 deposit from Marco") && ledger.includes("−€40.00 bought #1") &&
    ledger.includes(`+€100.00 undo of entry ${withdrawal.id}`));
});

test("profit is split by contribution at the time of the sale; /split equal applies to later sales", async () => {
  const t = await setup({ settings: members });
  await t.updates(msg("/deposit Owner 100"), msg("/deposit Marco 200"));
  const k1 = await addDeal(t.store);
  await sold(t, k1, 20, 50);
  assert.deepEqual((await t.store.ledger()).at(-1).profit, { Owner: 10, Marco: 20 });
  await t.update(msg("/split equal"));
  assert.ok(t.tg.texts().some((x) => x.includes("profit is split equally")));
  const k2 = (await t.store.addDeal("vinted:2", { title: "Big Muff", url: "u", text: "x", cost: 10 })).n && "vinted:2";
  await sold(t, k2, 10, 40);
  assert.deepEqual((await t.store.ledger()).at(-1).profit, { Owner: 15, Marco: 15 });
  const p = summarize(await t.store.ledger(), await t.store.deals());
  assert.equal(p.profit, 60);
  assert.deepEqual(p.members.map((m) => [m.name, m.profit, m.account]), [["Owner", 25, 125], ["Marco", 35, 235]]);
  assert.equal(p.cash, 360);                                                  // 300 + 30 + 30
  assert.equal(p.members.reduce((s, m) => s + m.account, 0), p.cash + p.stock);
});

test("/fix corrects a price with a new entry, shared like the original sale", async () => {
  const t = await setup({ settings: members });
  await t.updates(msg("/deposit Owner 100"), msg("/deposit Marco 200"));
  const key = await addDeal(t.store);
  await sold(t, key, 20, 50);
  const before = JSON.stringify(await t.store.ledger());
  await t.update(msg("/split equal"));                                       // changing the split later...
  await t.updates(msg("/fix 1 sold 56"), msg("/fix 1 paid 20"), msg("/fix 9 sold 5"), msg("/fix 1 sold 56", { user: MARCO }));
  const all = await t.store.ledger();
  assert.equal(JSON.stringify(all.slice(0, -1)), before);
  const fix = all.at(-1);
  assert.deepEqual([fix.kind, fix.amount, fix.note], ["fix", 6, "sold €50.00 → €56.00"]);
  assert.deepEqual(fix.profit, { Owner: 2, Marco: 4 });                       // ...doesn't change this sale's split
  assert.equal((await t.store.deal(key)).sold_for, 56);
  const texts = t.tg.texts();
  assert.ok(texts.some((x) => x.includes("already has paid €20.00")) && texts.some((x) => x.includes("No deal #9")));
  assert.equal(summarize(all, await t.store.deals()).profit, 36);
});

test("cents are split without losing any", () => {
  assert.deepEqual(allocate(10, { A: 1 / 3, B: 1 / 3, C: 1 / 3 }), { A: 3.34, B: 3.33, C: 3.33 });
  assert.deepEqual(allocate(-10, { A: 1 / 3, B: 1 / 3, C: 1 / 3 }), { A: -3.34, B: -3.33, C: -3.33 });
  assert.deepEqual(allocate(5, {}), {});
  assert.equal(Object.values(allocate(99.99, { A: 0.5, B: 0.25, C: 0.25 })).reduce((s, x) => s + x, 0).toFixed(2), "99.99");
});

test("/settreasurer: a member (shown as a name of our choice) holds the money; /pot says so; owner only", async () => {
  const t = await setup({ settings: { ...members, roles: { [OWNER]: "manager", [MARCO]: "buyer", [LUCA]: "seller" },
    people: { [OWNER]: "Boss", [MARCO]: "Marco✨", [LUCA]: "Luca" } } });
  await t.updates(msg("/deposit Boss 100"), msg("/settreasurer Marco", { user: MARCO }), msg("/settreasurer Nobody"),
    msg("/settreasurer marco as Marcello"), msg("/pot", { user: LUCA }), msg("/settreasurer"));
  const r = t.tg.texts();
  assert.match(r.at(-5), /Only the owner/);
  assert.match(r.at(-4), /No team member called "Nobody"/);
  assert.match(r.at(-3), /🏦 Marcello is now the treasurer/);
  assert.match(r.at(-2), /🏦 Marcello holds the pot's money \(treasurer\)/);
  assert.match(r.at(-1), /Treasurer: Marcello/);
  assert.deepEqual((await t.settings()).treasurer, { id: MARCO, name: "Marcello" });
});

test("Sunday 19:30: the treasurer is asked for a screenshot of the balance, once; in Summary if a DM fails", async () => {
  const t = await setup({ settings: { ...members, roles: { [OWNER]: "manager", [MARCO]: "buyer" }, people: { [MARCO]: "Marco" },
    treasurer: { id: MARCO, name: "Marcello" } } });
  await t.update(msg("/deposit Boss 100"));
  const sun = (h, m) => Date.UTC(2026, 9, 11, h - 2, m) / 1000;   // Sunday 11 Oct, Italy time
  t.tg.clear();
  await t.cron(sun(19, 20));
  assert.ok(!t.tg.texts().some((x) => x.includes("Weekly pot check")));
  await t.cron(sun(19, 31));
  await t.cron(sun(19, 36));
  const asks = t.tg.sent().filter((p) => p.text?.includes("Weekly pot check"));
  assert.equal(asks.length, 1);
  assert.equal(String(asks[0].chat_id), String(MARCO));
  assert.match(asks[0].text, /post a screenshot of the pot's bank balance in the Summary topic\.\nThe bot's numbers: cash <b>€100\.00<\/b>, in stock €0\.00/);
  // next week, private chat closed: in Summary with a mention instead
  t.tg.fail = (method, p) => (method === "sendMessage" && String(p.chat_id) === String(MARCO) ? { ok: false, description: "bot was blocked" } : null);
  t.tg.clear();
  await t.cron(sun(19, 31) + 7 * 86400);
  const post = t.tg.sent().find((p) => p.text?.includes("Weekly pot check") && String(p.chat_id) === GROUP);
  assert.equal(post.message_thread_id, 44);
  assert.ok(post.text.startsWith('<a href="tg://user?id=555">Marcello</a>, 📸'));
});
