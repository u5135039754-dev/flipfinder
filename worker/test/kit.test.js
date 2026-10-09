// The listing kit: made when something is bought (a repair deal: once repaired), sent to the seller role.

import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, tap, addDeal, OWNER, MARCO } from "./helpers.js";
import { dropPrice, kitPrices, parseKit } from "../src/kit.js";

const LUCA = 777;
const KIT = [
  "PLATFORM: Vinted | pedals sell fast there and buyers pay the shipping",
  "TITLE: Boss DS-1 Distortion pedale chitarra",
  "DESCRIPTION:",
  "Boss DS-1 Distortion, funziona bene.",
  "Qualche graffio sul fondo, niente di serio.",
  "Senza alimentatore.",
  "Spedizione o ritiro a mano a Testville.",
  "END",
  "PHOTOS: top; bottom with the label; jacks; knobs close up",
  "PACKAGE: 20x15x10 cm, about 0.5 kg | Vinted piccolo",
].join("\n");
const text = (t) => ({ content: [{ type: "text", text: t }] });

async function team({ ai = true, repair = null } = {}) {
  const t = await setup({ settings: { allowed_users: [MARCO, LUCA], ai: { enabled: ai },
    roles: { [OWNER]: "manager", [MARCO]: "buyer", [LUCA]: "seller" }, people: { [OWNER]: "Boss", [MARCO]: "Marco", [LUCA]: "Luca" } } });
  t.env.ANTHROPIC_API_KEY = "test-key";
  await t.store.put("area", { city: "Testville" });
  const key = await addDeal(t.store, { cost: 26.95, value: 60, low: 50, item: { price: 22, photos: ["https://img/1.jpg"] },
    ...(repair ? { repair } : {}) });
  t.tg.answerClaude = () => text(KIT);
  return { t, key };
}

/** The owner (a manager) claims and buys it for `paid`. */
async function buy(t, key, paid = 30) {
  await t.updates(tap(`c:${key}`, { first: "Boss" }), tap(`b:${key}`), msg(String(paid)));
}

const toLuca = (t) => t.tg.sent().filter((p) => String(p.chat_id) === String(LUCA));

test("prices: list at the market value, quick sale, and a floor that keeps our min profit", () => {
  // value 60, paid 30, min profit 12: floor €42 -> €45
  assert.deepEqual(kitPrices({ value: 60, low: 50, paid: 30 }, 12), { value: 60, price: 60, quick: 50, lowest: 45, short: false });
  // a repair part counts too; a market under the floor lists at the floor and says so
  assert.deepEqual(kitPrices({ value: 60, paid: 30, repair: { cost: 25 } }, 12), { value: 60, price: 70, quick: 70, lowest: 70, short: true });
  assert.equal(dropPrice(60, 45), 55);
  assert.equal(dropPrice(48, 45), 45);                      // never below the floor
});

test("the AI's kit is read line by line", () => {
  const k = parseKit(KIT);
  assert.equal(k.platform, "Vinted");
  assert.equal(k.why, "pedals sell fast there and buyers pay the shipping");
  assert.equal(k.title, "Boss DS-1 Distortion pedale chitarra");
  assert.equal(k.description.split("\n").length, 4);
  assert.deepEqual(k.photos, ["top", "bottom with the label", "jacks", "knobs close up"]);
  assert.equal(k.shipping, "20x15x10 cm, about 0.5 kg, Vinted piccolo");
  // nothing left from the old listing
  assert.equal(parseKit("DESCRIPTION:\nIn ottime condizioni, nessun danno evidente nelle foto.\nControlli come nella foto.\nEND").description,
    "In ottime condizioni, nessun danno evidente.\nControlli.");
});

test("bought: the kit goes to the seller in private, a note in the deal's thread", async () => {
  const { t, key } = await team();
  await buy(t, key, 30);
  const [kit] = toLuca(t).filter((p) => p.text?.startsWith("📦 <b>Listing kit"));
  assert.equal(kit.text, [
    "📦 <b>Listing kit · #1</b> Boss DS-1 distortion",
    "💶 List at <b>€60</b> · lowest to accept <b>€45</b> · quick sale €50",
    "🛒 Where: <b>Vinted</b>, pedals sell fast there and buyers pay the shipping",
    "🏷 Title: <code>Boss DS-1 Distortion pedale chitarra</code>",
    "📝 Description:\n<code>Boss DS-1 Distortion, funziona bene.\nQualche graffio sul fondo, niente di serio.\nSenza alimentatore.\n" +
      "Spedizione o ritiro a mano a Testville.</code>",
    "📸 Photos: top · bottom with the label · jacks · knobs close up",
    "📦 Shipping: 20x15x10 cm, about 0.5 kg, Vinted piccolo",
  ].join("\n"));
  assert.deepEqual(kit.reply_markup.inline_keyboard, [
    [{ text: "📋 Copy title", copy_text: { text: "Boss DS-1 Distortion pedale chitarra" } },
      { text: "📋 Copy description", copy_text: { text: (await t.store.deal(key)).kit.description } }],
    [{ text: "🔄 Rewrite", callback_data: `kr:${key}` }, { text: "🏷 Mark as listed", callback_data: `kl:${key}` }]]);
  assert.ok(t.tg.texts().includes("📦 Listing kit sent to Luca"));
  // the AI was told the city and the price, and wrote in its listing-kit format
  const task = t.tg.claude.at(-1).body.messages[0].content.at(-1).text;
  assert.match(task, /Task: listing kit \(listing kit format\)\. Pickup city: Testville\. We'll list it at about €60\./);
});

test("🏷 Mark as listed asks the price; under the floor it warns; a new price later restarts the week", async () => {
  const { t, key } = await team();
  await buy(t, key, 30);
  await t.updates(tap(`kl:${key}`, { user: MARCO }));
  assert.match(t.tg.sent("answerCallbackQuery").at(-1).text, /for the seller/);       // a buyer can't
  await t.updates(tap(`kl:${key}`, { user: LUCA, chat: LUCA }), msg("60", { user: LUCA, chat: LUCA }));
  let d = await t.store.deal(key);
  assert.ok(d.status === "listed" && d.list_price === 60 && d.listed_at);
  assert.equal(t.tg.texts().at(-1), "🏷 #1 listed at €60.00");
  await t.updates(tap(`kl:${key}`, { user: LUCA, chat: LUCA }), msg("40", { user: LUCA, chat: LUCA }));
  d = await t.store.deal(key);
  assert.equal(d.list_price, 40);
  assert.match(t.tg.texts().at(-1), /now at €40\.00\. ⚠️ That's under €45/);
});

test("🔄 Rewrite makes a new kit in place; long texts are copied from a message", async () => {
  const { t, key } = await team();
  await buy(t, key, 30);
  t.tg.answerClaude = () => text(KIT.replace("Boss DS-1 Distortion pedale chitarra", "Pedale Boss DS-1 distorsione"));
  await t.update(tap(`kr:${key}`, { user: LUCA, chat: LUCA }));
  const edit = t.tg.sent("editMessageText").at(-1);
  assert.match(edit.text, /<code>Pedale Boss DS-1 distorsione<\/code>/);
  await t.update(tap(`kd:${key}`, { user: LUCA, chat: LUCA }));
  assert.match(t.tg.texts().at(-1), /^<code>Boss DS-1 Distortion, funziona bene\./);
});

test("a repair deal: fix it first; 🔧 Repaired brings the kit, which says it was repaired", async () => {
  const { t, key } = await team({ repair: { part: "footswitch", cost: 10 } });
  await buy(t, key, 30);
  assert.ok(toLuca(t).some((p) => p.text.includes("needs footswitch first: tap 🔧 Repaired")));
  assert.ok(!toLuca(t).some((p) => p.text.startsWith("📦 <b>Listing kit")));
  const deal = await t.store.deal(key);
  const kb = t.tg.sent("editMessageText").concat(t.tg.sent("editMessageCaption")).at(-1).reply_markup;
  assert.deepEqual(kb.inline_keyboard[0][0], { text: "🔧 Repaired (Boss)", callback_data: `rp:${key}` });
  assert.equal(deal.status, "bought");
  await t.update(tap(`rp:${key}`));
  assert.ok((await t.store.deal(key)).repaired_at);
  const kit = toLuca(t).find((p) => p.text.startsWith("📦 <b>Listing kit"));
  assert.match(kit.text, /lowest to accept <b>€55<\/b>/);                            // 30 + part 10 + 12
  assert.match(t.tg.claude.at(-1).body.messages[0].content.at(-1).text, /We repaired it ourselves: footswitch replaced/);
  const after = t.tg.sent("editMessageText").concat(t.tg.sent("editMessageCaption")).at(-1).reply_markup;
  assert.equal(after.inline_keyboard[0][0].text, "🏷 Listed (Boss)");
});

test("with the AI off the kit is a template with the prices; the Mini App shows the kit in Stock", async () => {
  const { t, key } = await team({ ai: false });
  await buy(t, key, 30);
  const kit = toLuca(t).find((p) => p.text.startsWith("📦 <b>Listing kit"));
  assert.match(kit.text, /List at <b>€60<\/b>/);
  assert.match(kit.text, /fill in the \[brackets\]/);
  assert.match(kit.text, /ritiro a mano a Testville/);
  assert.equal(t.tg.claude.length, 0);
  const { open } = await import("../src/app.js");
  const { snapshot } = await import("../src/webapp.js");
  const { bot } = await open(t.env, { fetchFn: t.tg.fetch, now: t.now });
  const state = await snapshot(bot, { id: LUCA });
  assert.equal(state.stock[0].kit.prices.lowest, 45);
  assert.equal(state.stock[0].kit.title, "Boss DS-1 distortion");
});

test("no seller role yet: the kit goes to whoever bought it", async () => {
  const t = await setup();
  const key = await addDeal(t.store, { cost: 26.95, value: 60 });
  await buy(t, key, 30);
  assert.ok(t.tg.sent().some((p) => String(p.chat_id) === String(OWNER) && p.text?.startsWith("📦 <b>Listing kit")));
});
