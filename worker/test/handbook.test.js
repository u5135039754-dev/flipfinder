import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, topicMsg, MARCO, GROUP } from "./helpers.js";

const BOOK = { sections: [
  { key: "buying", title: "🛒 BUYING (Anna)", text: "1. Claim first.\n2. Ask for a video. No video = no buy." },
  { key: "money", title: "💰 MONEY (Bob)", text: "1. Every buy needs approval.\n2. Profit < costs? Tell Bob." },
], posted: {} };

test("an empty handbook says how to write it", async () => {
  const t = await setup();
  await t.update(msg("/handbook"));
  assert.match(t.tg.texts()[0], /No handbook yet\. The owner writes it with \/handbook edit &lt;section&gt;/);
});

test("/topic rules pins one message per section, and doesn't duplicate them later", async () => {
  const t = await setup({ settings: { allowed_users: [MARCO] } });
  await t.store.put("handbook", BOOK);
  await t.update(topicMsg("/topic", 66, "📖 Rules"));
  const posts = t.tg.sent().filter((p) => p.message_thread_id === 66 && p.text.startsWith("<b>"));
  assert.deepEqual(posts.map((p) => p.text.split("\n")[0]), ["<b>🛒 BUYING (Anna)</b>", "<b>💰 MONEY (Bob)</b>"]);
  assert.ok(posts[1].text.includes("Profit &lt; costs? Tell Bob."));              // escaped, not broken HTML
  assert.equal(t.tg.sent("pinChatMessage").length, 3);                             // 2 sections + the General intro
  assert.equal((await t.settings()).topics.rules, 66);
  t.tg.clear();
  await t.update(msg("/intro", { chat: Number(GROUP) }));
  assert.equal(t.tg.sent().filter((p) => p.text?.startsWith("<b>🛒")).length, 0);  // already there: not reposted
});

test("/handbook reposts it anywhere, unpinned; /rules is unchanged", async () => {
  const t = await setup({ settings: { allowed_users: [MARCO] } });
  await t.store.put("handbook", BOOK);
  await t.updates(msg("/handbook", { user: MARCO }), msg("/rules", { user: MARCO }));
  const texts = t.tg.texts();
  assert.ok(texts[0].startsWith("<b>🛒 BUYING") && texts[1].startsWith("<b>💰 MONEY"));
  assert.match(texts[2], /<b>Deal rules<\/b>/);
  assert.equal(t.tg.sent("pinChatMessage").length, 0);
});

test("/handbook edit: owner only, new text in the same message or as a reply, pinned copies updated", async () => {
  const t = await setup({ settings: { allowed_users: [MARCO] } });
  await t.store.put("handbook", BOOK);
  await t.update(topicMsg("/topic rules", 66));
  const pinnedId = (await t.store.get("handbook")).posted[`${GROUP}:66`].buying.id;
  t.tg.clear();
  await t.updates(msg("/handbook edit buying\n1. Claim first.\n2. Video, always.", { user: MARCO }),
    msg("/handbook edit buying\n1. Claim first.\n2. Video, always."), msg("/handbook edit shipping\nx"));
  let r = t.tg.texts();
  assert.match(r[0], /Only the owner can edit the handbook/);
  assert.match(r[1], /✅ Updated "🛒 BUYING \(Anna\)" and its pinned copy/);
  assert.match(r[2], /No section "shipping"\. Sections: buying, money/);
  const edit = t.tg.sent("editMessageText").at(-1);
  assert.equal(edit.message_id, pinnedId);
  assert.equal(edit.text, "<b>🛒 BUYING (Anna)</b>\n1. Claim first.\n2. Video, always.");
  t.tg.clear();
  await t.update(msg("/handbook edit mon"));                                         // a prefix is enough
  assert.match(t.tg.texts()[0], /Reply to this message with the new text for <b>💰 MONEY \(Bob\)<\/b>/);
  assert.ok(t.tg.sent()[0].reply_markup.force_reply);
  await t.update(msg("1. Bob approves every buy."));
  r = t.tg.texts();
  assert.match(r.at(-1), /✅ Updated "💰 MONEY \(Bob\)"/);
  assert.equal((await t.store.get("handbook")).sections[1].text, "1. Bob approves every buy.");
});
