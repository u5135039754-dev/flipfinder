// Designer clothes: the 👕 Clothes topic, its rules and brands from Telegram, the AI's fake check,
// the label question and the listing kit's measurements.

import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, tap, addDeal, MARCO } from "./helpers.js";
import { AI, LABEL_QUESTION, SYSTEM } from "../src/ai.js";
import { dealTopic } from "../src/group.js";
import { kitText, parseKit, plainKit } from "../src/kit.js";

const CLOTHES = { kind: "hoodie", size: "M", brand: "Stone Island", fake_risk: "medium", fake_why: ["only 8 reviews"] };
const text = (t) => ({ content: [{ type: "text", text: t }] });
const check = (fake) => ["RISK: Some pilling on the cuffs", "IF: none", "RED FLAG: none",
  "PARTS: 0 | DIFFICULTY: none | PART: none", fake].join("\n");

test("clothes deals go to the 👕 Clothes topic; /topic clothes sets it", async () => {
  assert.equal(dealTopic({ group: "Clothes" }), "clothes");
  const t = await setup({ settings: { allowed_users: [MARCO] } });
  const u = msg("/topic clothes", { chat: -100444 });
  Object.assign(u.message, { is_topic_message: true, message_thread_id: 77 });
  await t.update(u);
  assert.equal((await t.settings()).topics.clothes, 77);
  assert.ok(t.tg.texts().some((x) => x.startsWith("<b>👕 CLOTHES</b>\nDesigner pieces under €200, Italy + EU.") &&
    x.includes("- Pay only through Vinted checkout")));
});

test("/setrule clothes changes only the clothes minimums; /rules shows them", async () => {
  const t = await setup({ settings: { allowed_users: [MARCO] } });
  await t.updates(msg("/setrule clothes min_profit 35"), msg("/setrule clothes max_roi 90"), msg("/setrule clothes min_rating 11"),
    msg("/rules"));
  const r = t.tg.texts();
  assert.match(r[0], /Clothes min_profit is now 35/);
  assert.match(r[1], /Clothes rules: min_profit, min_roi, min_rating/);
  assert.match(r[2], /between 1 and 10/);
  assert.match(r[3], /👕 Clothes: min_profit €35, min_roi 40%, min_rating 5/);
  const s = await t.settings();
  assert.deepEqual(s.clothes_rules, { min_profit: 35 });
  assert.equal(s.rules?.min_profit, undefined);                      // the main rule is untouched
});

test("/add clothes <brand> and /remove clothes <brand>", async () => {
  const t = await setup({ settings: { allowed_users: [MARCO] } });
  await t.update(msg("/add clothes Moncler"));
  assert.match(t.tg.texts()[0], /Added 👕 <b>moncler<\/b> to Clothes/);
  assert.deepEqual((await t.settings()).added.at(-1), { query: "moncler", clothes: true });
  await t.update(msg("/categories"));
  assert.ok(t.tg.texts().some((x) => x.includes("Clothes") && x.includes("moncler")));
  await t.update(msg("/add clothes moncler"));
  assert.match(t.tg.texts().at(-1), /already a search called "moncler"/);
  await t.update(msg("/remove clothes moncler"));
  const ask = t.tg.sent().at(-1);
  assert.match(ask.text, /Remove <b>moncler<\/b>\?/);
});

test("the AI's fake check: no clear label photo caps it at 'if the seller sends label photos', high fake risk is a NO", async () => {
  const t = await setup();
  const key = await addDeal(t.store, { group: "Clothes", clothes: CLOTHES, value: 120, item: { price: 60 } });
  const d = await t.store.deal(key);
  const ai = new AI({ settings: {}, view: async () => ({ rules: { min_profit: 25 }, clothes_rules: { min_profit: 30 } }) }, "k");
  const r1 = await ai.finish(d, check("FAKE: low | LABEL: no | WHY: none"));
  assert.equal(r1.lines[0], "✅ YES, if the seller sends label photos. €60 profit.");
  assert.equal(r1.lines[2], "⚠️ Fake risk: low 🟢 · no clear label photo");
  const r2 = await ai.finish(d, check("FAKE: low | LABEL: yes | WHY: none"));
  assert.equal(r2.lines[0], "✅ YES. €60 profit.");
  assert.equal(r2.lines[2], "⚠️ Fake risk: low 🟢");
  const r3 = await ai.finish(d, check("FAKE: high | LABEL: yes | WHY: compass badge font looks off"));
  assert.equal(r3.lines[0], "❌ NO. High fake risk: compass badge font looks off.");
  assert.equal(r3.verdict, "no");
  // no FAKE line at all: medium, and no label photo assumed
  const r4 = await ai.finish(d, check(""));
  assert.equal(r4.lines[0], "✅ YES, if the seller sends label photos. €60 profit.");
  assert.match(r4.lines[2], /Fake risk: medium/);
  // clothes use their own min profit (30): €28 isn't enough
  const cheap = { ...d, value: 88 };
  assert.equal((await ai.finish(cheap, check("FAKE: low | LABEL: yes | WHY: none"))).lines[0], "❌ NO. Only €28 profit.");
  // the instructions say what to look at
  assert.match(SYSTEM, /inside neck label and the care\/wash tag/);
  assert.match(SYSTEM, /FAKE: <low, medium or high/);
});

test("❓ Seller questions for clothes always start with the label, care tag and codes question", async () => {
  const t = await setup({ settings: { allowed_users: [MARCO], ai: { enabled: true } } });
  t.env.ANTHROPIC_API_KEY = "k";
  const key = await addDeal(t.store, { group: "Clothes", clothes: CLOTHES, item: { price: 60, photos: ["https://img/1.jpg"] } });
  t.tg.answerClaude = () => text("IT: Mi mandi una foto dell'etichetta? | EN: Label photo?\nIT: Ci sono macchie? | EN: Any stains?");
  const u = tap(`aq:${key}`, { user: MARCO, chat: -100 });
  u.callback_query.message.message_id = 8;
  await t.update(u);
  assert.match(t.tg.claude[0].body.messages[0].content.at(-1).text, /label, care tag and code photos is already asked/);
  const post = t.tg.texts().at(-1);
  const codes = [...post.matchAll(/<code>(.+?)<\/code>/g)].map((m) => m[1]);
  assert.equal(codes.length, 2);
  assert.match(codes[0], /^Potresti mandarmi foto chiare dell'etichetta interna/);
  assert.equal(codes[1], "Ci sono macchie?");                       // the AI's own label question is dropped (asked already)
  assert.match(LABEL_QUESTION, /care tag/);
});

test("the listing kit for clothes: what to measure, Vinted or Vestiaire Collective", () => {
  const d = { n: 4, title: "Felpa Stone Island", clothes: CLOTHES, condition: "Ottime" };
  const kit = { ...parseKit("PLATFORM: Vestiaire Collective | authentication helps\nTITLE: Felpa con cappuccio Stone Island M\n" +
    "DESCRIPTION:\nFelpa Stone Island, taglia M.\nEND\nPHOTOS: front; inside label; care tag\nPACKAGE: 30x20x10 cm | Vinted piccolo"),
  prices: { price: 120, lowest: 90, quick: 100 } };
  assert.equal(kit.platform, "Vestiaire Collective");
  const out = kitText(d, kit);
  assert.match(out, /📏 Measure it laid flat: chest \(armpit to armpit\) · length \(back collar to hem, not the hood\)/);
  assert.ok(!out.includes("💡 Or Vestiaire"));
  const vinted = kitText(d, { ...kit, platform: "Vinted" });
  assert.match(vinted, /💡 Or Vestiaire Collective/);
  assert.match(plainKit(d, "").description, /Taglia M\. Misure: petto \[__\] cm, lunghezza \[__\] cm, maniche \[__\] cm\./);
});
