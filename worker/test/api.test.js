import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, DAYTIME, NIGHT, OWNER, GROUP, CATALOG } from "./helpers.js";

const deal = (n, profit, extra = {}) => ({
  key: `vinted:${n}`, text: `🔥 <b>Deal ${n}</b>`, photo: n % 2 ? "https://img/p.jpg" : "", group: "Guitars",
  record: { title: `Deal ${n}`, url: `https://www.vinted.it/items/${n}`, source: "vinted", cost: 50, value: 100,
    profit, query: "boss katana", score: [0, 0, 7, profit], item: { id: n, title: `Deal ${n}` }, ...extra },
});

test("the API needs the key", async () => {
  const t = await setup();
  assert.equal((await t.api("GET", "/api/state", undefined, { key: "wrong" })).status, 401);
  t.env.API_KEY = "";
  assert.equal((await t.api("GET", "/api/state", undefined, { key: "" })).status, 401);   // never open
});

test("deals go to every chat with lifecycle buttons, in their topic, numbered", async () => {
  const t = await setup({ settings: { topics: { guitars: 11 } } });
  const r = await t.api("POST", "/api/deal", deal(1, 40));
  assert.deepEqual(r.body, { status: "sent", n: 1 });
  const photos = t.tg.sent("sendPhoto");
  assert.deepEqual(photos.map((p) => String(p.chat_id)), [String(OWNER), GROUP]);
  assert.ok(!("message_thread_id" in photos[0]) && photos[1].message_thread_id === 11);   // topic only in the group
  assert.equal(photos[0].caption, "🔥 <b>Deal 1</b>\n🔢 #1");   // the scanner's full message
  assert.deepEqual(photos[0].reply_markup.inline_keyboard[0], [{ text: "✋ Claim", callback_data: "c:vinted:1" },
    { text: "Open on Vinted", url: "https://www.vinted.it/items/1" }]);
  const d = await t.store.deal("vinted:1");
  assert.deepEqual(d.messages.map((m) => [m.chat, m.photo, m.thread]), [[String(OWNER), true, null], [GROUP, true, 11]]);
  assert.deepEqual((await t.api("POST", "/api/deal", deal(1, 40))).body, { status: "exists" });   // never twice
});

test("a photo Telegram can't load falls back to text; a deleted topic falls back to General", async () => {
  const t = await setup({ settings: { topics: { guitars: 99 } } });
  t.tg.fail = (method, p) => (method === "sendPhoto" ? { ok: false, description: "wrong file" }
    : p.message_thread_id === 99 ? { ok: false, description: "message thread not found" } : null);
  const r = await t.api("POST", "/api/deal", deal(1, 40));
  assert.equal(r.body.status, "sent");
  const texts = t.tg.sent("sendMessage");
  assert.equal(texts.length, 3);   // owner, group in topic 99 (fails), group in General
  assert.ok(texts[1].message_thread_id === 99 && !("message_thread_id" in texts[2]));
});

test("quiet hours: deals wait, then go out best first after 08:00, at most 15 at a time", async () => {
  const t = await setup({ now: NIGHT });
  for (const [n, profit] of [[1, 20], [2, 45], [3, 30]]) {
    assert.equal((await t.api("POST", "/api/deal", deal(n, profit))).body.status, "queued");
  }
  assert.equal(t.tg.calls.length, 0);
  await t.cron(NIGHT);
  assert.equal(t.tg.calls.filter((c) => c.method !== "setMyCommands").length, 0);   // still night
  await t.cron(DAYTIME);
  const order = t.tg.calls.filter((c) => c.method.startsWith("send") && String(c.payload.chat_id) === String(OWNER))
    .map((c) => (c.payload.caption || c.payload.text).match(/Deal (\d)/)[1]);
  assert.deepEqual(order, ["2", "3", "1"]);                              // most profit first
  assert.equal((await t.store.deals("WHERE queued = 1")).length, 0);
  const run = await t.api("POST", "/api/run", { status: { runs: 1 } });
  assert.equal(run.body.flushed, 3);                                     // counted once in the daily summary
  assert.equal((await t.api("POST", "/api/run", {})).body.flushed, 0);
});

test("a big overnight queue is spread over several cron runs (request limit)", async () => {
  const t = await setup({ now: NIGHT });
  for (let n = 1; n <= 14; n++) await t.api("POST", "/api/deal", deal(n * 2 + 1, n));   // odd n: with a photo...
  t.tg.fail = (method) => (method === "sendPhoto" ? { ok: false, description: "wrong file" } : null);   // ...that fails
  await t.cron(DAYTIME);
  const first = t.tg.calls.length;
  assert.ok(first <= 45, `${first} Telegram calls in one run`);
  assert.ok((await t.store.deals("WHERE queued = 1")).length > 0);
  await t.cron(DAYTIME + 300);
  assert.equal((await t.store.deals("WHERE queued = 1")).length, 0);
});

test("state gives settings, the area, the pool and what we own; catalog and run status are stored", async () => {
  const t = await setup({ catalog: null, settings: { disabled: ["iphone 13"] } });
  await t.store.put("area", { city: "Paesino", center: [45, 11], radius_km: 30 });
  await t.api("POST", "/api/deal", deal(1, 40));
  const d = await t.store.deal("vinted:1");
  Object.assign(d, { status: "bought", who: "Marco", paid: 50 });
  await t.store.saveDeal("vinted:1", d);
  await t.api("PUT", "/api/catalog", CATALOG);
  await t.api("POST", "/api/run", { status: { last_run: DAYTIME, runs: 7, checked: 900, deals_sent: 2 },
    values: { "vinted:1": 95 } });
  const s = (await t.api("GET", "/api/state")).body;
  assert.deepEqual(s.settings.disabled, ["iphone 13"]);
  assert.equal(s.area.city, "Paesino");
  assert.equal(s.pool, null);
  assert.deepEqual(s.open, [{ key: "vinted:1", query: "boss katana", item: { id: 1, title: "Deal 1" } }]);
  assert.equal((await t.store.deal("vinted:1")).value_now, 95);
  await t.update(msg("/status"));
  const status = t.tg.texts()[0];
  assert.ok(status.includes("7 runs · 900 listings checked · 2 deals sent") && status.includes("07 Oct 10:00 UTC"));
  assert.ok(status.includes("Searches on: 2 of 3") && status.includes("Subito ✅ (30 km)"));
});

test("run messages go to Summary and say whether they arrived", async () => {
  const t = await setup({ settings: { topics: { summary: 44 } } });
  const r = await t.api("POST", "/api/run", { notify: [{ text: "📊 daily", topic: "summary" }] });
  assert.deepEqual(r.body.notified, [true]);
  assert.equal(t.tg.sent()[1].message_thread_id, 44);
});

test("import moves deals.json and settings.json, keeps numbers and the queue", async () => {
  const t = await setup();
  const deals = {
    "vinted:2": { n: 2, title: "B", url: "u", status: "new", sent: 20, votes: {}, messages: [], text: "b" },
    "vinted:1": { n: 1, title: "A", url: "u", status: "claimed", who: "Marco", sent: 10, votes: {}, messages: [], text: "a" },
  };
  const r = await t.api("POST", "/api/import", { deals, queue: ["vinted:2"], next_n: 2, settings: { allowed_users: [5] },
    area: { city: "Paesino" }, pool: { start: 300, since: 1 }, feedback: [{ key: "vinted:1", at: 5, title: "A" }] });
  assert.deepEqual(r.body, { ok: true, deals: 2, next_n: 2 });
  assert.equal((await t.store.deal("vinted:1")).who, "Marco");
  assert.equal((await t.store.deals("WHERE queued = 1"))[0][0], "vinted:2");
  assert.deepEqual((await t.settings()).allowed_users, [5]);
  assert.equal((await t.store.feedbackSince(0)).length, 1);
  assert.equal((await t.api("POST", "/api/deal", deal(3, 40))).body.n, 3);           // numbering continues
  assert.ok((await t.api("POST", "/api/import", { deals })).body.error);             // not twice by accident
});
