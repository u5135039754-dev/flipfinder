import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, tap, OWNER, MARCO, GROUP, topicMsg } from "./helpers.js";
import { applySettings, effectiveBudget, searchId, setBudget } from "../src/searches.js";
import { CATALOG } from "./helpers.js";
import { COMMANDS_VERSION } from "../src/bot.js";
import { INTROS } from "../src/group.js";

test("setprice validates and saves", async () => {
  const t = await setup();
  await t.updates(msg('/setprice "boss katana" 90 260'), msg("/setprice boss katana 300 100"),
    msg("/setprice boss katana ten 100"), msg("/setprice boss katna 90 260"), msg("/setprice boss ds 1 10 90"));
  const r = t.tg.texts();
  assert.match(r[0], /✅ <b>boss katana<\/b>: €90 – €260/);
  assert.match(r[1], /must be lower than the maximum/);
  assert.match(r[2], /must be numbers/);
  assert.ok(r[3].includes("No search called") && r[3].includes("boss katana"));   // suggestion
  assert.match(r[4], /budget search/);
  assert.deepEqual((await t.settings()).prices, { "boss katana": [90, 260] });
});

test("strangers are ignored and /allow is owner only", async () => {
  const t = await setup();
  await t.updates(msg("/budget 10", { user: 999 }), msg("/allow 555"), msg("/budget 50", { user: MARCO }),
    msg("/allow 777", { user: MARCO }));
  const r = t.tg.texts();
  assert.equal(r.length, 3);                                   // nothing for the stranger
  assert.match(r[0], /User 555 can now use commands/);
  assert.match(r[1], /Budget is now €50/);                     // 555 is allowed now
  assert.match(r[2], /Only the owner/);
  const s = await t.settings();
  assert.deepEqual(s.allowed_users, [MARCO]);
  assert.equal(s.budget, 50);
});

test("categories buttons toggle searches", async () => {
  const t = await setup();
  await t.updates(msg("/categories"), tap(`t:${await searchId("iphone 13")}`));
  const groups = Object.fromEntries(t.tg.sent().map((p) => [p.text.split("</b>")[0].replace("<b>", ""), p]));
  assert.deepEqual(Object.keys(groups).sort(), ["Amps", "Budget", "Electronics"]);
  assert.equal(groups.Electronics.reply_markup.inline_keyboard[0][0].text, "✅ iphone 13");
  const s = await t.settings();
  assert.deepEqual(s.disabled, ["iphone 13"]);
  assert.equal(t.tg.sent("editMessageReplyMarkup")[0].reply_markup.inline_keyboard[0][0].text, "❌ iphone 13");
  assert.equal(t.tg.sent("answerCallbackQuery")[0].text, "iphone 13: off");
  assert.deepEqual(applySettings(CATALOG, s).searches.map((x) => x.enabled), [true, false, true]);
});

test("rules, budget, add and remove", async () => {
  const t = await setup();
  await t.updates(msg("/setrule min_roi 25"), msg("/setrule min_rating 11"), msg("/setrule max_roi 20"),
    msg("/setrule speed 3"), msg("/budget abc"), msg("/budget 60"), msg('/add "zoom g1x four" 20 60'),
    msg("/add boss katana 10 20"), msg("/remove boss katana"), tap(`rm:${await searchId("boss katana")}:y`));
  const r = t.tg.texts();
  assert.ok(r[0].includes("min_roi is now 25") && r[1].includes("between 1 and 10") && r[2].includes("below max_roi"));
  assert.ok(r[3].includes("Unknown rule") && r[4].includes("must be a number") && r[5].includes("Budget is now €60"));
  assert.ok(r[6].includes("Added <b>zoom g1x four</b>") && r[7].includes("already a search"));
  assert.ok(r[8].includes("Remove <b>boss katana</b>?") && t.tg.sent()[8].reply_markup);
  const view = applySettings(CATALOG, await t.settings());
  assert.deepEqual(view.searches.map((s) => s.query), ["iphone 13", "boss ds 1", "zoom g1x four"]);
  assert.equal(view.rules.min_roi, 25);
  assert.equal(view.budget, 60);
  assert.equal(view.searches.find((s) => s.query === "boss ds 1").price_to, 60);   // budget caps budget searches
});

test("group chat bot suffix and help; chatting is ignored", async () => {
  const t = await setup();
  await t.updates(msg("/help@flipfinder_bot", { chat: -100123 }), msg("hello there"));
  const sent = t.tg.sent();
  assert.equal(sent.length, 1);
  assert.equal(sent[0].chat_id, -100123);
  assert.ok(sent[0].text.includes("/setprice"));
});

test("help for others hides owner-only commands", async () => {
  const t = await setup({ settings: { allowed_users: [MARCO] } });
  await t.updates(msg("/intro", { user: MARCO }), msg("/help", { user: MARCO }), msg("/help"));
  const [intro, marco, owner] = t.tg.texts();
  assert.match(intro, /Only the owner can use \/intro/);
  assert.ok(!marco.includes("/allow") && !marco.includes("/intro"));
  assert.ok(owner.includes("/allow") && owner.includes("/intro"));
});

test("commands are registered once, by the cron", async () => {
  const t = await setup();
  await t.cron();
  assert.equal(t.tg.sent("setMyCommands").length, 2);
  assert.equal((await t.settings()).commands_version, COMMANDS_VERSION);
  t.tg.clear();
  await t.cron();
  assert.equal(t.tg.sent("setMyCommands").length, 0);
});

test("the webhook needs Telegram's secret header", async () => {
  const t = await setup();
  const { handleRequest } = await import("../src/app.js");
  const r = await handleRequest(new Request("https://w/telegram", { method: "POST", body: JSON.stringify(msg("/help")) }),
    t.env, { fetchFn: t.tg.fetch });
  assert.equal(r.status, 403);
  assert.equal(t.tg.calls.length, 0);
});

test("budget follows the pool up and down; hand-set prices are kept but capped", () => {
  const view = applySettings(CATALOG, {});
  const ds1 = view.searches.find((s) => s.query === "boss ds 1");
  assert.ok(ds1.price_to === 72 && ds1.price_to_is_budget);
  setBudget(view, 241);
  assert.ok(view.budget === 241 && ds1.price_to === 241);     // a bigger pool raises it
  setBudget(view, 50);
  assert.equal(ds1.price_to, 50);                              // and a smaller one lowers it
  const hand = applySettings(CATALOG, { prices: { "boss ds 1": [15, 40] } });
  const ds = hand.searches.find((s) => s.query === "boss ds 1");
  setBudget(hand, 241);
  assert.equal(ds.price_to, 40);                               // set by hand: kept
  setBudget(hand, 30);
  assert.equal(ds.price_to, 30);                               // but never above the budget
  assert.equal(effectiveBudget(72, 170), 72);
  assert.equal(effectiveBudget(72, 50), 50);
  assert.equal(effectiveBudget(72, null), 72);
});

test("topics are learned from messages and /topic", async () => {
  const t = await setup();
  await t.updates(topicMsg("/topic", 11, "🎸 Guitars"), topicMsg("/help", 22, "📱 Electronics"),
    topicMsg("/topic summary", 44, "Riepilogo"), msg("/topic"));
  assert.deepEqual((await t.settings()).topics, { guitars: 11, electronics: 22, summary: 44 });
  const texts = t.tg.texts();
  assert.ok(texts.some((x) => x.includes("This topic is now <b>Guitars</b>")));
  assert.ok(texts.some((x) => x.includes("Send /topic inside a group topic")));
});

test("topic setup pins intros, /intro edits instead of duplicating", async () => {
  const t = await setup();
  await t.update(topicMsg("/topic", 11, "🎸 Guitars"));
  let posted = (await t.settings()).intros[GROUP];
  const sends = t.tg.sent();
  assert.deepEqual(sends.slice(1).map((p) => p.text), [INTROS.guitars, INTROS.general]);
  assert.ok(sends[1].message_thread_id === 11 && !("message_thread_id" in sends[2]));
  assert.deepEqual(t.tg.sent("pinChatMessage").map((p) => p.message_id), [posted.guitars.id, posted.general.id]);
  t.tg.clear();
  await t.updates(topicMsg("/topic summary", 44), msg("/intro", { chat: Number(GROUP) }),
    msg("/intro", { user: MARCO, chat: Number(GROUP) }));
  const texts = t.tg.texts();
  assert.equal(texts.filter((x) => x === INTROS.summary).length, 1);       // new topic: posted once
  assert.ok(!texts.includes(INTROS.guitars) && !texts.includes(INTROS.general));
  assert.match(texts.at(-1), /Intros pinned: Guitars, Summary, General/);
  assert.match(texts.at(-1), /Not set up yet: Electronics, Budget/);
  assert.equal(t.tg.sent("pinChatMessage").length, 4);                      // Summary + /intro re-pins all 3
  const s = await t.settings();
  s.intros[GROUP].guitars.text = "old intro";                                 // text changed since: edited in place
  await t.store.put("settings", s);
  t.tg.clear();
  await t.update(msg("/intro"));                                               // from the private chat: the group
  assert.deepEqual(t.tg.sent("editMessageText").map((e) => e.text), [INTROS.guitars]);
  assert.equal(t.tg.sent("editMessageText")[0].chat_id, GROUP);
  assert.equal(t.tg.texts().length, 1);                                       // just the report
  posted = (await t.settings()).intros[GROUP];
  assert.equal(posted.guitars.text, INTROS.guitars);
});

test("a group upgraded to a supergroup switches id and is remembered", async () => {
  const t = await setup({ chatIds: `${OWNER},-200111` });
  t.tg.fail = (method, p) => (String(p.chat_id) === "-200111"
    ? { ok: false, error_code: 400, description: "group chat was upgraded", parameters: { migrate_to_chat_id: -1001234567890 } }
    : null);
  const { status } = await t.api("POST", "/api/run", { notify: [{ text: "hello" }] });
  assert.equal(status, 200);
  assert.deepEqual(t.tg.sent().map((p) => String(p.chat_id)).slice(0, 3), [String(OWNER), "-200111", "-1001234567890"]);
  assert.deepEqual((await t.settings()).chat_migrations, { "-200111": "-1001234567890" });
  assert.ok(t.tg.texts().some((x) => x.includes("upgraded to a supergroup")));
  t.tg.clear();
  await t.api("POST", "/api/run", { notify: [{ text: "again" }] });
  assert.deepEqual(t.tg.sent().map((p) => String(p.chat_id)), [String(OWNER), "-1001234567890"]);   // straight to the new id
});

test("older alerts' I'm on it button shows who", async () => {
  const t = await setup();
  await t.updates(tap("claim", { user: 4242 }), tap("claim", { first: "Marco" }));   // 4242 isn't allowed: ignored
  const label = t.tg.sent("editMessageReplyMarkup")[0].reply_markup.inline_keyboard[0][0];
  assert.ok(label.text.startsWith("✋ Marco is on it") && label.callback_data === "claimed");
  assert.deepEqual(t.tg.sent("answerCallbackQuery").map((p) => p.text), ["It's yours, good luck!"]);
});

test("owner id comes from the environment, not the code", async () => {
  const t = await setup();
  t.env.OWNER_ID = "42";
  await t.update(msg("/allow 7"));
  assert.equal(t.tg.texts().length, 0);   // 1000001 isn't the owner any more: ignored
  await t.update(msg("/allow 7", { user: 42 }));
  assert.match(t.tg.texts()[0], /User 7 can now use commands/);
  assert.notEqual(OWNER, 42);
});
