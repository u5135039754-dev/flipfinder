import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, topicMsg, MARCO, GROUP, DAYTIME } from "./helpers.js";
import { render } from "../src/handbook.js";

const BOOK = () => ({ title: "📖 TEAM HANDBOOK", intro: "How we work. Tap a section to open it.", updated: DAYTIME, sections: [
  { key: "buying", title: "🛒 Buying · Anna", text: "1. Claim first\n2. No video, no buy" },
  { key: "money", title: "💰 Money · Bob", text: "1. Every buy needs approval\n2. Profit < costs? Tell Bob" },
] });
const RULES = 66;

async function withRules(extra = {}) {
  const t = await setup({ settings: { allowed_users: [MARCO], topics: { rules: RULES } } });
  await t.store.put("handbook", { ...BOOK(), ...extra });
  return t;
}

const inRules = (text, user) => topicMsg(text, RULES, null, user);
const handbookPosts = (t) => t.tg.sent().filter((p) => p.text?.startsWith("<b>📖"));

test("the handbook is one message: sections in collapsed quotes, escaped, with the date", () => {
  assert.equal(render(BOOK()), [
    "<b>📖 TEAM HANDBOOK</b>\nHow we work. Tap a section to open it.",
    "<b>🛒 Buying · Anna</b>\n<blockquote expandable>1. Claim first\n2. No video, no buy</blockquote>",
    "<b>💰 Money · Bob</b>\n<blockquote expandable>1. Every buy needs approval\n2. Profit &lt; costs? Tell Bob</blockquote>",
    "<i>Last updated: 7 Oct 2026</i>",
  ].join("\n\n"));
});

test("/topic rules posts it once, pins it silently, removes the pin notice, and cleans up after itself", async () => {
  const t = await setup({ settings: { allowed_users: [MARCO] } });
  await t.store.put("handbook", BOOK());
  const cmd = topicMsg("/topic rules", RULES);
  cmd.message.message_id = 70;
  await t.update(cmd);
  const posts = handbookPosts(t);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].message_thread_id, RULES);
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

test("/handbook edit changes that one message in place; owner only; answers vanish from the Rules topic", async () => {
  const t = await withRules({ message: { chat: GROUP, thread: RULES, id: 500 } });
  await t.updates(inRules("/handbook edit buying\n1. Claim first\n2. Video, always", MARCO),
    inRules("/handbook edit buying\n1. Claim first\n2. Video, always"), inRules("/handbook edit shipping\nx"));
  const r = t.tg.texts();
  assert.match(r[0], /Only the owner can edit the handbook/);
  assert.match(r[1], /✅ Updated 🛒 Buying · Anna in the pinned handbook/);
  assert.match(r[2], /No section "shipping"\. Sections: buying, money/);
  assert.ok(t.tg.sent().every((p) => p.message_thread_id === RULES));
  assert.equal(handbookPosts(t).length, 0);                                               // no new post
  const edit = t.tg.sent("editMessageText").at(-1);
  assert.equal(edit.message_id, 500);
  assert.ok(edit.text.includes("<blockquote expandable>1. Claim first\n2. Video, always</blockquote>"));
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
  assert.match(t.tg.texts().at(-1), /✅ Updated 💰 Money · Bob in the pinned handbook/);
  const book = await t.store.get("handbook");
  assert.equal(book.sections[1].text, "1. Bob approves every buy\n2. Settle monthly");
  assert.equal(book.updated, DAYTIME + 60);
  const ids = (await t.store.get("cleanup")).map((x) => x.id);
  assert.ok(ids.includes(77) && ids.includes(901));                                       // the answer, the question
});

test("a handbook that would be too long for one message is refused", async () => {
  const t = await withRules();
  await t.update(msg(`/handbook edit buying\n${"x".repeat(4100)}`));
  assert.match(t.tg.texts()[0], /too long for one Telegram message/);
  assert.equal((await t.store.get("handbook")).sections[0].text, "1. Claim first\n2. No video, no buy");
});

test("/handbook shows a copy anywhere else (not pinned, nothing deleted); /rules is unchanged", async () => {
  const t = await withRules({ message: { chat: GROUP, thread: RULES, id: 500 } });
  await t.updates(msg("/handbook", { user: MARCO }), msg("/rules", { user: MARCO }));
  const texts = t.tg.texts();
  assert.equal(texts[0], render(BOOK()));
  assert.match(texts[1], /<b>Deal rules<\/b>/);
  assert.equal(t.tg.sent("pinChatMessage").length, 0);
  assert.equal((await t.store.get("cleanup", [])).length, 0);
});

test("/handbook in the Rules topic doesn't repost; a deleted handbook is posted again", async () => {
  const t = await withRules({ message: { chat: GROUP, thread: RULES, id: 500 } });
  t.tg.fail = (m) => (m === "editMessageText" ? { ok: false, description: "Bad Request: message is not modified" } : null);
  await t.update(inRules("/handbook"));
  assert.equal(handbookPosts(t).length, 0);
  assert.match(t.tg.texts()[0], /pinned at the top/);
  t.tg.fail = (m) => (m === "editMessageText" ? { ok: false, description: "Bad Request: message to edit not found" } : null);
  await t.update(inRules("/handbook"));
  assert.equal(handbookPosts(t).length, 1);
  assert.notEqual((await t.store.get("handbook")).message.id, 500);
});

test("pin notices in the Rules topic are removed; the cron posts the handbook if it isn't there yet", async () => {
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
