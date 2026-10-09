import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, tap, addDeal, OWNER, MARCO, DAYTIME } from "./helpers.js";
import { findDeal, fullText, keyboard, profit } from "../src/deals.js";

const withMarco = { allowed_users: [MARCO] };

test("deal lifecycle: buys come out of the pot, sales go back in, profit is shared", async () => {
  const t = await setup({ settings: withMarco });
  const key = await addDeal(t.store);
  await t.updates(msg("/deposit Owner 100"), msg("/deposit Marco 100"), msg("/deposit Luca 100"));
  t.tg.clear();
  await t.updates(tap(`c:${key}`, { user: MARCO, first: "Marco" }), tap(`b:${key}`, { user: OWNER + 1 }));
  let d = await t.store.deal(key);
  assert.ok(d.status === "claimed" && d.who === "Marco");
  assert.equal(t.tg.sent("editMessageText").length + t.tg.sent("editMessageCaption").length, 2);
  assert.match(t.tg.sent("editMessageText")[0].text, /✋ Claimed by Marco/);
  assert.equal(t.tg.sent("editMessageCaption")[0].reply_markup.inline_keyboard[0][0].text, "🙋 Request buy (Marco)");
  // a stranger's tap was ignored entirely; now Marco asks to buy it at the price he agreed: 30
  t.tg.clear();
  await t.update(tap(`b:${key}`, { user: MARCO }));
  assert.ok(t.tg.texts()[0].includes("What price did you agree") && t.tg.texts()[0].includes("Known total"));
  t.tg.clear();
  await t.updates(msg("abc", { user: MARCO }), msg("30", { user: MARCO }));
  let texts = t.tg.texts();
  assert.match(texts[0], /need just the amount/);
  assert.ok(texts.some((x) => x.includes("Marco asks to buy #1") && x.includes("€30.00") && x.includes("Vinted: pay €30.00 online")));
  assert.ok(texts.some((x) => x.startsWith("🙋 Sent for approval")));
  d = await t.store.deal(key);
  assert.ok(d.status === "claimed" && d.request.amount === 30);
  assert.match(keyboard(key, d).inline_keyboard[0][0].text, /⏳ Waiting for OK: €30\.00 \(Marco\)/);
  // the owner (a manager) approves: bought, paid from the pot
  t.tg.clear();
  await t.update(tap(`ap:${key}`));
  texts = t.tg.texts();
  assert.ok(texts.some((x) => x.includes("−€30.00 bought #1") && x.includes("cash now <b>€270.00</b>")));
  assert.ok(texts.some((x) => x.includes("approved #1 at €30.00")));
  d = await t.store.deal(key);
  assert.ok(d.status === "bought" && d.paid === 30 && !d.request);
  t.tg.clear();
  await t.updates(tap(`l:${key}`, { user: MARCO }), tap(`s:${key}`, { user: MARCO }), msg("75", { user: MARCO }));
  d = await t.store.deal(key);
  assert.ok(d.status === "sold" && d.sold_for === 75);
  assert.ok(t.tg.texts().includes("✅ Sold for €75.00, profit €45.00"));
  assert.ok(t.tg.texts().some((x) => x.includes("+€75.00 sold #1") && x.includes("cash now <b>€345.00</b>")));
  assert.match(fullText(d), /✅ Sold by Marco · paid €30\.00 · sold for €75\.00 · profit €45\.00/);
  assert.deepEqual(keyboard(key, d), { inline_keyboard: [[{ text: "Open on Vinted", url: "https://www.vinted.it/items/1" }]] });
  const sale = (await t.store.ledger()).at(-1);
  assert.deepEqual(sale.profit, { Owner: 15, Marco: 15, Luca: 15 });          // €100 each: equal thirds
  const p = profit(await t.store.deals(), DAYTIME);
  assert.deepEqual(p, { total: 45, month: 45, people: { Marco: 45 }, sold: 1 });
});

test("only the claimer or the owner advances a deal", async () => {
  const t = await setup({ settings: withMarco });
  const key = await addDeal(t.store);
  await t.updates(tap(`c:${key}`, { user: MARCO }), tap(`b:${key}`, { user: 777 }), tap(`c:${key}`));
  assert.deepEqual(t.tg.sent("answerCallbackQuery").map((p) => p.text),
    ["It's yours, good luck!", "user 555 already has this one"]);   // 777 ignored
  t.tg.clear();
  await t.update(tap(`b:${key}`));                                   // the owner may step in
  assert.match(t.tg.texts()[0], /How much did you pay/);
});

test("votes, feedback and the seller message", async () => {
  const t = await setup({ settings: withMarco });
  const key = await addDeal(t.store);
  await t.updates(tap(`up:${key}`, { user: MARCO }), tap(`dn:${key}`), tap(`m:${key}`, { user: MARCO }));
  const d = await t.store.deal(key);
  assert.deepEqual(keyboard(key, d).inline_keyboard[1].map((b) => b.text), ["👍 1", "👎 1"]);
  const fb = await t.store.feedbackSince(0);
  assert.ok(fb.length === 1 && fb[0].title === "Boss DS-1 distortion" && fb[0].url.endsWith("/items/1"));
  const seller = t.tg.texts()[0];
  assert.ok(["Ciao! L'articolo", "ancora disponibile", "video", "<code>"].every((s) => seller.includes(s)));
});

test("stock and profit commands", async () => {
  const t = await setup({ settings: withMarco });
  const key = await addDeal(t.store);
  const d = await t.store.deal(key);
  Object.assign(d, { status: "bought", who: "Marco", who_id: MARCO, paid: 30, bought_at: DAYTIME + 60 });
  await t.store.saveDeal(key, d);
  await t.updates(msg("/stock"), msg("/profit"));
  const r = t.tg.texts();
  assert.ok(r[0].includes("Boss DS-1 distortion") && r[0].includes("Marco") && r[0].includes("€30.00"));
  assert.match(r[1], /Total: €0\.00 \(0 sold\)/);
});

test("the budget-mode limit is the smaller of /budget and the pot's cash", async () => {
  const t = await setup();
  await t.updates(msg("/pot"), msg("/deposit Owner 50"), msg("/pot"), msg("/budget 40"), msg("/pot"), msg("/pool"));
  const r = t.tg.texts().filter((x) => x.startsWith("💰"));
  assert.ok(r[0].includes("No money in yet") && r[0].includes("€72.00"));
  assert.ok(r[1].includes("Cash: <b>€50.00</b>") && r[1].includes("Budget-mode limit: €50.00"));
  assert.ok(r[2].includes("Budget-mode limit: €40.00"));
  assert.equal(r[3], r[2]);                                    // /pool is the old name
  assert.equal((await t.api("GET", "/api/state")).body.pool, 50);   // the scanner caps budget searches at it
  const fresh = await setup();
  assert.equal((await fresh.api("GET", "/api/state")).body.pool, null);    // no deposits yet: no cap
});

test("the known cost is suggested; a typed price overrides it; the owner buys without approval", async () => {
  const t = await setup({ settings: withMarco });
  const key = await addDeal(t.store);
  await t.updates(tap(`c:${key}`, { user: MARCO }), tap(`b:${key}`, { user: MARCO }));
  const q = t.tg.sent()[0];
  assert.ok(q.text.includes("€26.95") && q.reply_markup.inline_keyboard[0][0].callback_data === `pay:${key}`);
  t.tg.clear();
  await t.update(tap(`pay:${key}`));                    // not the owner's question
  assert.match(t.tg.sent("answerCallbackQuery")[0].text, /^That's for/);
  await t.update(tap(`pay:${key}`, { user: MARCO }));
  let d = await t.store.deal(key);
  assert.ok(d.status === "claimed" && d.request.amount === 26.95);   // asked, not bought
  const t2 = await setup({ settings: withMarco });
  await addDeal(t2.store);
  await t2.updates(tap(`c:${key}`, { user: MARCO }), tap(`b:${key}`, { user: MARCO }), msg("27,50", { user: MARCO }));
  assert.equal((await t2.store.deal(key)).request.amount, 27.5);
  const t3 = await setup({ settings: withMarco });
  await addDeal(t3.store);
  await t3.updates(tap(`c:${key}`), tap(`b:${key}`), msg("31"));     // the owner claims and buys: no approval needed
  d = await t3.store.deal(key);
  assert.ok(d.status === "bought" && d.paid === 31);
  assert.ok(t3.tg.texts().some((x) => x.startsWith("💸 How much did you pay")));
});

test("deals are numbered and found by number or name", async () => {
  const t = await setup();
  await addDeal(t.store);
  await t.store.addDeal("vinted:2", { title: "Big Muff Pi", url: "u2", text: "x", sent: 5 });
  const deals = await t.store.deals();
  assert.deepEqual(deals.map(([, d]) => d.n), [1, 2]);
  assert.match(deals[0][1].text, /🔢 #1$/);
  assert.equal(findDeal(deals, "#2")[0], "vinted:2");
  assert.equal(findDeal(deals, "big muff")[0], "vinted:2");
  assert.equal(findDeal(deals, "zoom"), null);
  assert.equal(await t.store.addDeal("vinted:2", { title: "again", text: "x" }), null);   // already have it
});

test("untouched deals are dropped after 30 days, claimed or voted ones are kept", async () => {
  const t = await setup();
  const old = DAYTIME - 31 * 86400;
  await t.store.addDeal("vinted:1", { title: "a", text: "x", sent: old });
  await t.store.addDeal("vinted:2", { title: "b", text: "x", sent: old, votes: { 5: "up" } });
  await t.store.addDeal("vinted:3", { title: "c", text: "x", sent: DAYTIME });
  await t.cron();
  assert.deepEqual((await t.store.deals()).map(([k]) => k), ["vinted:2", "vinted:3"]);
});
