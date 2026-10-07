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
  await t.updates(tap(`b:${key}`, { user: MARCO }), msg(String(paid), { user: MARCO }), tap(`l:${key}`, { user: MARCO }),
    tap(`s:${key}`, { user: MARCO }), msg(String(soldFor), { user: MARCO }));
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
  assert.ok(replies.some((x) => x.includes("Only the owner (treasurer) can use /deposit")));
  assert.ok(replies.some((x) => x.includes("Who and how much?")) && replies.some((x) => x.includes("between €0.01")));
  t.tg.clear();
  await t.update(msg("/pot", { user: MARCO }));
  const pot = t.tg.texts()[0];
  assert.match(pot, /Cash: <b>€300\.00<\/b> · in stock \(at cost\): €0\.00/);
  assert.ok(pot.includes("· Owner: €100.00 · €0.00 (33%) · <b>€100.00</b>"));
  assert.equal((await t.store.ledger()).length, 3);
});

test("buys over €50 need a 👍 from another member; the buyer's own 👍 doesn't count", async () => {
  const t = await setup({ settings: members });
  await t.update(msg("/deposit Owner 300"));
  const key = await addDeal(t.store, { cost: 60 });
  await t.update(tap(`c:${key}`, { user: MARCO, first: "Marco" }));
  t.tg.clear();
  await t.update(tap(`b:${key}`, { user: MARCO }));
  assert.match(t.tg.sent("answerCallbackQuery")[0].text, /needs a 👍 from another member/);
  assert.match(t.tg.texts()[0], /costs €60\.00: buys over €50 need a 👍/);
  assert.equal((await t.store.deal(key)).status, "claimed");
  await t.update(tap(`up:${key}`, { user: MARCO }));                          // own vote
  await t.update(tap(`b:${key}`, { user: MARCO }));
  assert.equal(t.tg.sent("answerCallbackQuery").at(-1).text.includes("needs a 👍"), true);
  t.tg.clear();
  await t.update(tap(`up:${key}`, { user: LUCA, first: "Luca" }));
  assert.ok(t.tg.texts().some((x) => x.includes("#1 approved by Luca") && x.includes("can tap 💸 Bought")));
  await t.updates(tap(`b:${key}`, { user: MARCO }), msg("62", { user: MARCO }));
  assert.equal((await t.store.deal(key)).status, "bought");
  assert.equal(summarize(await t.store.ledger(), await t.store.deals()).cash, 238);
});

test("a typed price over €50 also needs the 👍, even when the known cost was lower", async () => {
  const t = await setup({ settings: members });
  const key = await addDeal(t.store);                                        // known cost €26.95
  await t.updates(tap(`c:${key}`, { user: MARCO }), tap(`b:${key}`, { user: MARCO }), msg("70", { user: MARCO }));
  assert.ok(t.tg.texts().some((x) => x.includes("€70.00 is over €50")));
  assert.equal((await t.store.deal(key)).status, "claimed");
  assert.equal((await t.store.ledger()).length, 0);
});

test("withdrawals are limited to the member's money and the cash; /undo adds a reversing entry", async () => {
  const t = await setup({ settings: members });
  await t.updates(msg("/deposit Owner 100"), msg("/deposit Marco 100"));
  const key = await addDeal(t.store);
  const d = await t.store.deal(key);
  Object.assign(d, { status: "claimed", who: "Marco", who_id: MARCO });
  await t.store.saveDeal(key, d);
  await t.updates(tap(`b:${key}`, { user: MARCO }), msg("40", { user: MARCO }));   // €160 cash, €40 in stock
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
