import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, tap, addDeal, OWNER, MARCO, DAYTIME } from "./helpers.js";
import { findDeal, fullText, keyboard, profit } from "../src/deals.js";

const withMarco = { allowed_users: [MARCO] };

test("deal lifecycle with prices and pool", async () => {
  const t = await setup({ settings: withMarco });
  const key = await addDeal(t.store);
  await t.store.put("pool", { start: 300, since: DAYTIME - 7200 });
  await t.updates(tap(`c:${key}`, { user: MARCO, first: "Marco" }), tap(`b:${key}`, { user: OWNER + 1 }));
  let d = await t.store.deal(key);
  assert.ok(d.status === "claimed" && d.who === "Marco");
  assert.equal(t.tg.sent("editMessageText").length + t.tg.sent("editMessageCaption").length, 2);
  assert.match(t.tg.sent("editMessageText")[0].text, /✋ Claimed by Marco/);
  assert.equal(t.tg.sent("editMessageCaption")[0].reply_markup.inline_keyboard[0][0].text, "💸 Bought (Marco)");
  // a stranger's tap was ignored entirely; now Marco buys it: asked for the price, answers 30
  t.tg.clear();
  await t.update(tap(`b:${key}`, { user: MARCO }));
  assert.ok(t.tg.texts()[0].includes("How much did you pay") && t.tg.texts()[0].includes("Known total"));
  assert.ok(t.tg.sent()[0].reply_markup.inline_keyboard[0][0].text.startsWith("✅ Use €"));
  t.tg.clear();
  await t.updates(msg("abc", { user: MARCO }), msg("30", { user: MARCO }));
  assert.match(t.tg.texts()[0], /need just the amount/);
  assert.match(t.tg.texts()[1], /💸 Bought for €30\.00 · pool now €270\.00/);
  d = await t.store.deal(key);
  assert.ok(d.status === "bought" && d.paid === 30);
  t.tg.clear();
  await t.updates(tap(`l:${key}`, { user: MARCO }), tap(`s:${key}`, { user: MARCO }), msg("75", { user: MARCO }));
  d = await t.store.deal(key);
  assert.ok(d.status === "sold" && d.sold_for === 75);
  assert.match(t.tg.texts().at(-1), /✅ Sold for €75\.00, profit €45\.00 · pool now €345\.00/);
  assert.match(fullText(d), /✅ Sold by Marco · paid €30\.00 · sold for €75\.00 · profit €45\.00/);
  assert.deepEqual(keyboard(key, d), { inline_keyboard: [] });
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
  assert.deepEqual(keyboard(key, d).inline_keyboard[1].map((b) => b.text), ["👍 1", "👎 1", "📩 Message seller"]);
  const fb = await t.store.feedbackSince(0);
  assert.ok(fb.length === 1 && fb[0].title === "Boss DS-1 distortion" && fb[0].url.endsWith("/items/1"));
  const seller = t.tg.texts()[0];
  assert.ok(["Ciao! L'articolo", "ancora disponibile", "video", "<code>"].every((s) => seller.includes(s)));
});

test("stock, profit and pool commands", async () => {
  const t = await setup({ settings: withMarco });
  const key = await addDeal(t.store);
  const d = await t.store.deal(key);
  Object.assign(d, { status: "bought", who: "Marco", who_id: MARCO, paid: 30, bought_at: DAYTIME + 60 });
  await t.store.saveDeal(key, d);
  await t.updates(msg("/pool"), msg("/pool abc"), msg("/pool 200"), msg("/stock"), msg("/profit"));
  const r = t.tg.texts();
  assert.ok(r[0].includes("No pool set") && r[1].includes("must be a number") && r[2].includes("Pool set to €200.00"));
  assert.ok(r[3].includes("Boss DS-1 distortion") && r[3].includes("Marco") && r[3].includes("€30.00"));
  assert.match(r[4], /Total: €0\.00 \(0 sold\)/);
  const state = await t.api("GET", "/api/state");
  assert.equal(state.body.pool, 170);   // bought after the pool started: 200 - 30
});

test("/pool shows both and the limit is the smaller", async () => {
  const t = await setup();
  await t.updates(msg("/pool"), msg("/pool 50"), msg("/pool"), msg("/budget 40"), msg("/pool"), msg("/pot"));
  const r = t.tg.texts();
  assert.ok(r[0].includes("No pool set") && r[0].includes("Budget-mode limit: €72.00"));
  assert.match(r[1], /Budget-mode limit: €50\.00/);
  assert.ok(r[2].includes("Pool: €50.00") && r[2].includes("/budget: €72.00") && r[2].includes("<b>€50.00</b>"));
  assert.match(r[3], /Budget is now €40/);
  assert.ok(r[4].includes("/budget: €40.00") && r[4].includes("<b>€40.00</b>"));
  assert.equal(r[5], r[4]);   // /pot answers like /pool until the shared pot exists
});

test("bought suggests the known cost; a typed amount overrides it", async () => {
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
  assert.ok(d.status === "bought" && d.paid === 26.95);
  assert.match(t.tg.texts().at(-1), /💸 Bought for €26\.95/);
  assert.equal(t.tg.sent("editMessageReplyMarkup").at(-1).reply_markup.inline_keyboard.length, 0);   // ✅ button gone
  const t2 = await setup({ settings: withMarco });
  await addDeal(t2.store);
  await t2.updates(tap(`c:${key}`, { user: MARCO }), tap(`b:${key}`, { user: MARCO }), msg("27,50", { user: MARCO }));
  d = await t2.store.deal(key);
  assert.equal(d.paid, 27.5);
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
