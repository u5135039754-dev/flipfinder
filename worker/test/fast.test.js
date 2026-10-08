import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, OWNER, MARCO, DAYTIME } from "./helpers.js";

test("/fast: the owner turns it on and off and picks the searches; members can look", async () => {
  const t = await setup({ settings: { allowed_users: [MARCO] } });
  await t.store.put("catalog", { searches: [{ query: "boss katana", group: "Amps" }, { query: "iphone 13", group: "Electronics" },
    { query: "fender strat", group: "Guitars" }], rules: {} });
  await t.updates(msg("/fast 2", { user: MARCO }), msg("/fast 2"), msg("/fast per 6"), msg("/fast groups guitars, electronics"),
    msg("/fast groups pianos"), msg("/fast 30"), msg("/fast", { user: MARCO }));
  const r = t.tg.texts();
  assert.match(r[0], /Only the owner/);
  assert.match(r[1], /Fast lane on: the newest listings every 2 min \(8 searches a pass\)/);
  assert.match(r[2], /6 searches a pass/);
  assert.match(r[3], /Fast lane searches: Guitars, Electronics/);
  assert.match(r[4], /Groups: Amps, Electronics, Guitars/);
  assert.match(r[5], /Every 1 to 10 minutes/);
  assert.match(r[6], /Fast lane<\/b>: on, every 2 min\n6 searches a pass from: Guitars, Electronics\nNo passes in the last hour/);
  assert.deepEqual((await t.settings()).fast, { enabled: true, interval: 2, per_run: 6, groups: ["Guitars", "Electronics"] });
  // the scanner gets them with its state
  assert.equal((await t.api("GET", "/api/state")).body.settings.fast.interval, 2);
  await t.update(msg("/fast off"));
  assert.equal((await t.settings()).fast.enabled, false);
});

test("pass reports: averages for /fast; a Vinted block backs off for an hour and tells the owner once", async () => {
  const t = await setup({ settings: { fast: { enabled: true, interval: 2 } } });
  const pass = (at, extra = {}) => t.api("POST", "/api/fast", { secs: 30, requests: 9, blocked: 0, searches: 8, checked: 4,
    deals: 0, sent: 0, ...extra }, { at });
  await pass(DAYTIME - 240);
  await pass(DAYTIME - 120, { secs: 36, sent: 1 });
  const blocked = await pass(DAYTIME, { blocked: 2 });
  assert.deepEqual(blocked.body, { backoff: true });
  const s = await t.settings();
  assert.equal(s.fast.backoff_until, DAYTIME + 3600);
  const told = t.tg.sent().filter((p) => p.chat_id === OWNER);
  assert.equal(told.length, 1);
  assert.match(told[0].text, /Vinted refused 2 request\(s\) \(403\/429\)\. It slows to every 5 min for an hour/);
  await pass(DAYTIME + 300, { blocked: 1 });                         // still backing off: no second message
  assert.equal(t.tg.sent().filter((p) => p.chat_id === OWNER).length, 1);
  t.tg.clear();
  t.now = DAYTIME + 400;
  await t.update(msg("/fast"));
  const text = t.tg.texts()[0];
  assert.match(text, /Backing off after a Vinted block: every 5 min until/);
  assert.match(text, /Last hour: 4 passes, 31\.5 s each on average, 36 Vinted page loads, 3 blocked, 1 deal\(s\) sent/);
  // the scanner gets when the last pass started and how many ran
  await pass(DAYTIME + 500, { started: DAYTIME + 470 });
  assert.deepEqual((await t.api("GET", "/api/state")).body.fast, { last_started: DAYTIME + 470, passes: 5 });
});
