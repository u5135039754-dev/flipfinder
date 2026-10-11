// /tagalways: someone @mentioned on every deal in a topic (plus whoever is on duty), never on a ❌ NO,
// and overnight deals tagged once at 08:00 in one message.

import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, tap, OWNER, MARCO, GROUP } from "./helpers.js";
import { romeTs } from "../src/util.js";

const LUCA = 777;
const DAY = "2026-10-07";
const at = (h, m = 0) => romeTs(DAY, h, m);
const team = { allowed_users: [MARCO, LUCA], roles: { [OWNER]: "manager", [MARCO]: "buyer", [LUCA]: "seller" },
  people: { [OWNER]: "Boss", [MARCO]: "Marco", [LUCA]: "Luca" }, topics: { summary: 44, clothes: 55, guitars: 11 } };
const deal = (n, group = "Clothes") => ({ key: `vinted:${n}`, text: `🔥 <b>Item ${n}</b>`, photo: "", group,
  record: { title: `Item ${n}`, url: `https://www.vinted.it/items/${n}`, source: "vinted", cost: 50, value: 120, profit: 60,
    rating: 8, query: "stone island", item: { id: n, price: 50, total_price: 50 } } });
const inGroup = (t) => t.tg.sent().filter((p) => String(p.chat_id) === GROUP);
const tagged = (p, id) => (p.text || p.caption || "").includes(`tg://user?id=${id}`);

test("/tagalways: owner only, per topic, list and off", async () => {
  const t = await setup({ settings: team, now: at(10) });
  await t.update(msg("/tagalways clothes Luca", { user: MARCO, chat: MARCO }));
  assert.match(t.tg.texts().at(-1), /Only the owner/);
  await t.updates(msg("/tagalways clothes Luca"), msg("/tagalways shoes Luca"), msg("/tagalways clothes Nobody"), msg("/tagalways"));
  const r = t.tg.texts().slice(-4);
  assert.match(r[0], /Clothes: Luca is tagged on every deal/);
  assert.match(r[1], /Which topic\? guitars, electronics, budget, clothes/);
  assert.match(r[2], /I don't know "Nobody"/);
  assert.match(r[3], /Clothes: Luca/);
  assert.deepEqual((await t.settings()).tag_always, { clothes: LUCA });
  await t.update(msg("/tagalways clothes off"));
  assert.deepEqual((await t.settings()).tag_always, {});
});

test("a clothes deal tags the always person and whoever is on duty, once each; other topics don't", async () => {
  const t = await setup({ settings: { ...team, tag_always: { clothes: LUCA } }, now: at(10) });
  await t.update(tap("duty:on", { user: MARCO, chat: Number(GROUP) }), at(9, 55));
  t.tg.clear();
  await t.api("POST", "/api/deal", deal(1), { at: at(10) });
  const post = inGroup(t).find((p) => (p.text || "").includes("Item 1"));
  assert.ok(tagged(post, MARCO) && tagged(post, LUCA));
  assert.match(post.text, /👮 <a href="tg:\/\/user\?id=555">Marco<\/a> · 📌 <a href="tg:\/\/user\?id=777">Luca<\/a>/);
  assert.equal(post.message_thread_id, 55);
  // Luca on duty himself: tagged once
  await t.update(tap("duty:on", { user: LUCA, chat: Number(GROUP) }), at(10, 5));
  t.tg.clear();
  await t.api("POST", "/api/deal", deal(2), { at: at(10, 10) });
  const second = inGroup(t).find((p) => (p.text || "").includes("Item 2"));
  assert.equal((second.text.match(/id=777/g) || []).length, 1);
  // a guitar deal: only the duty mention
  t.tg.clear();
  await t.api("POST", "/api/deal", deal(3, "Guitars"), { at: at(10, 15) });
  const guitar = inGroup(t).find((p) => (p.text || "").includes("Item 3"));
  assert.ok(tagged(guitar, LUCA) && !guitar.text.includes("📌"));   // Luca is on duty, not "always"
});

test("a ❌ NO tags nobody", async () => {
  const t = await setup({ settings: { ...team, tag_always: { clothes: LUCA } }, now: at(10) });
  const { Bot } = await import("../src/bot.js");
  const bot = new Bot({ store: t.store, tg: { groups: [GROUP] }, ownerId: OWNER, settings: await t.settings(), now: at(10) });
  assert.equal(await bot.team.alertMention({ group: "Clothes", ai: { verdict: "no" } }), "");
  assert.equal(await bot.team.alertMention({ group: "Clothes", rejected: true }), "");
  // nobody on duty: everyone is tagged already, Luca included, so no second tag
  const all = await bot.team.alertMention({ group: "Clothes", ai: { verdict: "yes" } });
  assert.equal((all.match(/id=777/g) || []).length, 1);
  assert.ok(!all.includes("📌"));
});

test("quiet hours: nothing at night; at 08:00 the clothes deals go out untagged and one message tags once", async () => {
  const t = await setup({ settings: { ...team, tag_always: { clothes: LUCA } }, now: at(3) });
  for (const n of [1, 2, 3]) assert.equal((await t.api("POST", "/api/deal", deal(n), { at: at(3, n) })).body.status, "queued");
  await t.api("POST", "/api/deal", deal(4, "Guitars"), { at: at(3, 5) });
  assert.equal(inGroup(t).filter((p) => /Item/.test(p.text || "")).length, 0);   // nothing at night
  await t.update(tap("duty:on", { user: MARCO, chat: Number(GROUP) }), at(8, 0));
  t.tg.clear();
  await t.cron(at(8, 1));
  const clothes = inGroup(t).filter((p) => /Item [123]/.test(p.text || ""));
  assert.equal(clothes.length, 3);
  assert.ok(clothes.every((p) => !p.text.includes("tg://user")));                // not per deal
  const once = inGroup(t).filter((p) => (p.text || "").startsWith("☀️"));
  assert.equal(once.length, 1);
  assert.match(once[0].text, /^☀️ 3 deals from overnight: #1, #2, #3\n👮 <a href="tg:\/\/user\?id=555">Marco<\/a> · 📌 <a href="tg:\/\/user\?id=777">Luca<\/a>$/);
  assert.equal(once[0].message_thread_id, 55);
});
