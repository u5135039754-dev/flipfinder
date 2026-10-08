import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, topicMsg, MARCO, GROUP, DAYTIME } from "./helpers.js";
import { forApp, pinnedMessage } from "../src/handbook.js";

const BOOK = () => ({ title: "📖 TEAM HANDBOOK", intro: "Read it once, follow it always", updated: DAYTIME, sections: [
  { key: "buying", title: "🛒 Buying · Anna", text: "1. Claim first\n2. No video, no buy" },
  { key: "money", title: "💰 Money · Bob", text: "1. Every buy needs approval\n2. Profit < costs? Tell Bob" },
  { key: "general", title: "🤝 General", text: "1. Be polite" },
] });
const RULES = 66;
const LINK = "https://t.me/flip_bot?startapp=handbook";

/** Telegram knows the bot as @flip_bot. */
function botName(t) {
  t.tg.fail = (m) => (m === "getMe" ? { ok: true, result: { id: 1, username: "flip_bot" } } : null);
}

async function withRules(extra = {}) {
  const t = await setup({ settings: { allowed_users: [MARCO], topics: { rules: RULES } } });
  await t.store.put("handbook", { ...BOOK(), ...extra });
  botName(t);
  return t;
}

const inRules = (text, user) => topicMsg(text, RULES, null, user);
const handbookPosts = (t) => t.tg.sent().filter((p) => p.text?.startsWith("<b>📖"));

test("the pinned message is short, with a button that opens the handbook page", () => {
  assert.deepEqual(pinnedMessage(BOOK(), "flip_bot"), {
    text: "<b>📖 TEAM HANDBOOK</b>\nRead it once, follow it always",
    markup: { inline_keyboard: [[{ text: "📖 Open handbook", url: LINK }]] },
  });
});

test("the app gets each section's icon, name, person in charge and plain rules", () => {
  const app = forApp(BOOK());
  assert.deepEqual(app.sections[0], { key: "buying", icon: "🛒", name: "Buying", person: "Anna",
    rules: ["Claim first", "No video, no buy"] });
  assert.deepEqual(app.sections[2], { key: "general", icon: "🤝", name: "General", person: "", rules: ["Be polite"] });
  assert.equal(app.updated, DAYTIME);
});

test("/topic rules posts it once, pins it silently, removes the pin notice, and cleans up after itself", async () => {
  const t = await setup({ settings: { allowed_users: [MARCO] } });
  await t.store.put("handbook", BOOK());
  botName(t);
  const cmd = topicMsg("/topic rules", RULES);
  cmd.message.message_id = 70;
  await t.update(cmd);
  const posts = handbookPosts(t);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].message_thread_id, RULES);
  assert.equal(posts[0].reply_markup.inline_keyboard[0][0].url, LINK);
  const id = (await t.store.get("handbook")).message.id;
  assert.deepEqual(t.tg.sent("pinChatMessage").find((p) => p.message_id === id), { chat_id: Number(GROUP), message_id: id,
    disable_notification: true });
  assert.ok(t.tg.sent("deleteMessage").some((p) => p.message_id === id + 1));            // "pinned a message"
  const reply = t.tg.sent().find((p) => p.text.startsWith("✅ This topic is now"));
  assert.equal(reply.message_thread_id, RULES);
  // 10 s later (the cron too, in case that was missed) the command and the reply are gone
  assert.equal((await t.store.get("cleanup")).length, 2);
  t.tg.clear();
  await t.cron(DAYTIME + 15);
  assert.equal(t.tg.sent("deleteMessages")[0].message_ids[0], 70);
  assert.equal(t.tg.sent("deleteMessages")[0].message_ids.length, 2);
  assert.deepEqual(await t.store.get("cleanup"), []);
  assert.equal(handbookPosts(t).length, 0);                                               // already there
});

test("/handbook edit changes the app's text and date, not the pinned message; owner only; answers vanish", async () => {
  const t = await withRules({ message: { chat: GROUP, thread: RULES, id: 500 } });
  await t.updates(inRules("/handbook edit buying\n1. Claim first\n2. Video, always", MARCO));
  await t.update(inRules("/handbook edit buying\n1. Claim first\n2. Video, always"), DAYTIME + 60);
  await t.update(inRules("/handbook edit shipping\nx"));
  const r = t.tg.texts();
  assert.match(r[0], /Only the owner can edit the handbook/);
  assert.match(r[1], /✅ Updated 🛒 Buying · Anna\. The app shows it now/);
  assert.match(r[2], /No section "shipping"\. Sections: buying, money, general/);
  assert.ok(t.tg.sent().every((p) => p.message_thread_id === RULES));
  assert.equal(handbookPosts(t).length, 0);
  assert.equal(t.tg.sent("editMessageText").length, 0);                                   // the pinned one stays
  const book = await t.store.get("handbook");
  assert.equal(book.sections[0].text, "1. Claim first\n2. Video, always");
  assert.equal(book.updated, DAYTIME + 60);
  assert.equal((await t.store.get("cleanup")).length, 6);                                 // 3 commands + 3 answers
});

test("/handbook edit <section> alone asks for the rules; the question and answer are cleaned up too", async () => {
  const t = await withRules({ message: { chat: GROUP, thread: RULES, id: 500 } });
  await t.update(inRules("/handbook edit mon"));                                          // a prefix is enough
  const ask = t.tg.sent()[0];
  assert.match(ask.text, /new rules for <b>💰 Money · Bob<\/b>/);
  assert.ok(ask.reply_markup.force_reply);
  assert.equal(ask.message_thread_id, RULES);
  const answer = inRules("1. Bob approves every buy\n2. Settle monthly");
  answer.message.message_id = 77;
  await t.update(answer, DAYTIME + 60);
  assert.match(t.tg.texts().at(-1), /✅ Updated 💰 Money · Bob/);
  assert.equal((await t.store.get("handbook")).sections[1].text, "1. Bob approves every buy\n2. Settle monthly");
  const ids = (await t.store.get("cleanup")).map((x) => x.id);
  assert.ok(ids.includes(77) && ids.includes(901));                                       // the answer, the question
});

test("a section that's far too long is refused", async () => {
  const t = await withRules();
  await t.update(msg(`/handbook edit buying\n${"x".repeat(2100)}`));
  assert.match(t.tg.texts()[0], /too long for one section/);
  assert.equal((await t.store.get("handbook")).sections[0].text, "1. Claim first\n2. No video, no buy");
});

test("/handbook elsewhere sends the short message with its button (not pinned); /rules is unchanged", async () => {
  const t = await withRules({ message: { chat: GROUP, thread: RULES, id: 500 } });
  await t.updates(msg("/handbook", { user: MARCO }), msg("/rules", { user: MARCO }));
  const sent = t.tg.sent();
  assert.equal(sent[0].text, "<b>📖 TEAM HANDBOOK</b>\nRead it once, follow it always");
  assert.equal(sent[0].reply_markup.inline_keyboard[0][0].url, LINK);
  assert.match(sent[1].text, /<b>Deal rules<\/b>/);
  assert.equal(t.tg.sent("pinChatMessage").length, 0);
  assert.equal((await t.store.get("cleanup", [])).length, 0);
});

test("/handbook in the Rules topic doesn't repost; a deleted handbook message is posted again", async () => {
  const t = await withRules({ message: { chat: GROUP, thread: RULES, id: 500 } });
  t.tg.fail = (m) => (m === "getMe" ? { ok: true, result: { username: "flip_bot" } }
    : m === "editMessageText" ? { ok: false, description: "Bad Request: message is not modified" } : null);
  await t.update(inRules("/handbook"));
  assert.equal(handbookPosts(t).length, 0);
  assert.match(t.tg.texts()[0], /pinned at the top/);
  t.tg.fail = (m) => (m === "getMe" ? { ok: true, result: { username: "flip_bot" } }
    : m === "editMessageText" ? { ok: false, description: "Bad Request: message to edit not found" } : null);
  await t.update(inRules("/handbook"));
  assert.equal(handbookPosts(t).length, 1);
  assert.notEqual((await t.store.get("handbook")).message.id, 500);
});

test("pin notices in the Rules topic are removed; the cron posts the handbook message if it isn't there yet", async () => {
  const t = await withRules();
  const pin = inRules("");
  Object.assign(pin.message, { message_id: 88, pinned_message: { message_id: 87 } });
  delete pin.message.text;
  await t.update(pin);
  assert.deepEqual(t.tg.sent("deleteMessage"), [{ chat_id: Number(GROUP), message_id: 88 }]);
  await t.cron();
  assert.equal(handbookPosts(t).length, 1);
  await t.cron();
  assert.equal(handbookPosts(t).length, 1);                                               // once
});

test("the app's state carries the handbook (same access check as everything else)", async () => {
  const t = await withRules();
  const { snapshot } = await import("../src/webapp.js");
  const { open } = await import("../src/app.js");
  const { bot } = await open(t.env, { fetchFn: t.tg.fetch, now: t.now });
  const state = await snapshot(bot, { id: MARCO, first_name: "Marco" });
  assert.equal(state.handbook.sections.length, 3);
  assert.deepEqual(state.handbook.sections[1].rules, ["Every buy needs approval", "Profit < costs? Tell Bob"]);
});
