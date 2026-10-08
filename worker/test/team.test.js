import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, msg, tap, addDeal, OWNER, MARCO, GROUP } from "./helpers.js";
import { handleRequest } from "../src/app.js";
import { romeTs } from "../src/util.js";

const LUCA = 777;
const DAY = "2026-10-07";                  // a Wednesday
const at = (h, m = 0) => romeTs(DAY, h, m);
const team = {
  allowed_users: [MARCO, LUCA],
  roles: { [OWNER]: "manager", [MARCO]: "buyer", [LUCA]: "seller" },
  people: { [OWNER]: "Boss", [MARCO]: "Marco", [LUCA]: "Luca" },
  topics: { summary: 44, guitars: 11 },
};
const inGroup = (t) => t.tg.sent().filter((p) => String(p.chat_id) === GROUP).map((p) => p.text);
const tapAt = async (t, data, user, when, chat = Number(GROUP)) => t.update(tap(data, { user, chat }), when);

test("/roles and /setrole (owner only, people the bot knows)", async () => {
  const t = await setup({ settings: { allowed_users: [MARCO, LUCA], roles: {} } });
  await t.updates(msg("/roles", { user: MARCO, first: "Marco" }), msg("/roles"));
  assert.equal(t.tg.texts().length, 1);                                  // Marco has no role yet: ignored
  assert.match(t.tg.texts()[0], /Marco<\/b>: no role yet \(🔒 blocked\)/);   // but his name was learned
  t.tg.clear();
  await t.updates(msg("/setrole Marco buyer"), msg("/setrole Marco buyer", { user: MARCO }), msg("/setrole Nobody seller"),
    msg("/setrole Marco boss"), msg("/roles"));
  const r = t.tg.texts();
  assert.match(r[0], /Marco is now buyer: buying \+ main deal watcher/);
  assert.match(r[1], /Only the owner/);                                  // a role, but not the owner
  assert.match(r[2], /I don't know "Nobody" yet/);
  assert.match(r[3], /Roles: manager, buyer, seller/);
  assert.ok(r[4].includes("<b>Marco</b>: buying + main deal watcher · duty target 8 h/day"));
  assert.ok(r[4].includes("manager + bot + money (approves and pays all buys) · duty up to 2.8 h/day"));
});

test("nothing about duty happens until roles are set", async () => {
  const t = await setup({ settings: { allowed_users: [MARCO], roles: {} } });
  await t.cron(at(9));
  assert.equal(inGroup(t).length, 0);
  assert.equal(t.tg.sent("pinChatMessage").length, 0);
});

test("start and end duty: group posts, the pinned message, handover, who may end it", async () => {
  const t = await setup({ settings: team });
  await t.cron(at(9));                                                  // posts and pins "On duty now"
  const pin = t.tg.sent().find((p) => p.text?.startsWith("👮 <b>Nobody is on duty"));
  assert.ok(pin && t.tg.sent("pinChatMessage").length === 1);
  assert.deepEqual(pin.reply_markup.inline_keyboard.flat().map((b) => b.callback_data), ["duty:on", "duty:off", "duty:swap"]);
  t.tg.clear();
  await tapAt(t, "duty:on", MARCO, at(9, 5));
  assert.ok(inGroup(t).some((x) => x.includes('Marco</a> is on duty (until 22:00)')));
  assert.match(t.tg.sent("answerCallbackQuery")[0].text, /You're on duty until 22:00/);
  const edit = t.tg.sent("editMessageText").at(-1);
  assert.match(edit.text, /On duty now:<\/b> Marco \(since 09:05, until 22:00\)/);
  assert.match(edit.text, /Today: Boss 0\/2\.8 h max · Marco 0\/8 h · Luca 0\/2\.8 h max/);
  t.tg.clear();
  await tapAt(t, "duty:off", LUCA, at(10));                              // not Luca's shift
  assert.match(t.tg.sent("answerCallbackQuery")[0].text, /Marco is on duty, not you/);
  await tapAt(t, "duty:on", LUCA, at(11, 5));                            // takes over
  assert.ok(inGroup(t).some((x) => x.includes("Luca</a> is on duty (until 22:00), taking over from Marco")));
  await tapAt(t, "duty:off", LUCA, at(12, 5));
  assert.ok(inGroup(t).includes("Luca ended duty"));
  const duty = await t.store.get("duty");
  assert.deepEqual(duty.log.map((e) => [e.id, Math.round((e.end - e.start) / 60)]), [[MARCO, 120], [LUCA, 60]]);
  assert.equal(duty.on, null);
});

test("the shift runs until your last scheduled block, and ends by itself at 22:00", async () => {
  const t = await setup({ settings: team });
  await t.store.put("schedule", { [DAY]: { 14: MARCO, 15: MARCO, 16: MARCO, 18: MARCO } });
  await tapAt(t, "duty:on", MARCO, at(14, 2));
  assert.ok(inGroup(t).some((x) => x.includes("is on duty (until 17:00)")));
  t.tg.clear();
  await t.cron(at(22, 1));
  assert.ok(inGroup(t).some((x) => x.includes("🌙 Marco's shift ended at 22:00")));
  const duty = await t.store.get("duty");
  assert.equal(duty.on, null);
  assert.equal(duty.log[0].end, at(22));                                 // counted until 22:00, not later
});

test("nobody on duty: everyone pinged from 07:30, every 30 minutes, until someone starts", async () => {
  const t = await setup({ settings: team });
  await t.cron(at(7, 20));
  assert.ok(!inGroup(t).some((x) => x.includes("Nobody") || x.includes("nobody")));
  await t.cron(at(7, 30));
  const first = inGroup(t).find((x) => x.includes("Duty starts at 08:00 and nobody's on yet"));
  assert.ok(first && first.includes("Boss") && first.includes("Marco") && first.includes("Luca"));
  t.tg.clear();
  await t.cron(at(7, 50));
  assert.ok(!inGroup(t).some((x) => x.startsWith("👮 Nobody") || x.startsWith("👮 Duty starts")));
  await t.cron(at(8, 0));
  assert.ok(inGroup(t).some((x) => x.startsWith("👮 Nobody is on duty:")));
  await tapAt(t, "duty:on", MARCO, at(8, 10));
  t.tg.clear();
  await t.cron(at(8, 45));
  assert.ok(!inGroup(t).some((x) => x.startsWith("👮 Nobody")));
});

test("deal alerts @mention whoever is on duty (group only), or everyone when nobody is", async () => {
  const t = await setup({ settings: team, now: at(10) });
  const deal = (n) => ({ key: `vinted:${n}`, text: `🔥 <b>Deal ${n}</b>`, photo: "", group: "Guitars",
    record: { title: `Deal ${n}`, url: "u", cost: 20, value: 60, profit: 30, query: "q", score: [0, 0, 7, 30] } });
  await t.api("POST", "/api/deal", deal(1), { at: at(10) });
  let group = t.tg.sent().find((p) => String(p.chat_id) === GROUP);
  assert.match(group.text, /👮 Nobody on duty: .*Boss.*Marco.*Luca/);
  assert.ok(!t.tg.sent().find((p) => String(p.chat_id) === String(OWNER)).text.includes("👮"));   // private copy: no mention
  await tapAt(t, "duty:on", MARCO, at(10, 1));
  t.tg.clear();
  await t.api("POST", "/api/deal", deal(2), { at: at(10, 2) });
  group = t.tg.sent().find((p) => String(p.chat_id) === GROUP);
  assert.ok(group.text.endsWith('👮 <a href="tg://user?id=555">Marco</a>'));
});

test("not claimed within 10 minutes: the two who aren't on duty get pinged, once", async () => {
  const t = await setup({ settings: team });
  await tapAt(t, "duty:on", MARCO, at(10));
  await t.api("POST", "/api/deal", { key: "vinted:1", text: "🔥 Deal", photo: "", group: "Guitars",
    record: { title: "Deal one", url: "u", cost: 20, value: 60, profit: 30, query: "q" } }, { at: at(10, 1) });
  t.tg.clear();
  await t.cron(at(10, 6));
  assert.ok(!inGroup(t).some((x) => x.includes("isn't claimed")));
  await t.cron(at(10, 12));
  const ping = inGroup(t).find((x) => x.includes("#1 isn't claimed after 10 min"));
  assert.ok(ping && ping.includes("Boss") && ping.includes("Luca") && !ping.includes(">Marco<"));
  t.tg.clear();
  await t.cron(at(10, 20));
  assert.ok(!inGroup(t).some((x) => x.includes("isn't claimed")));
});

test("swaps: pick how long, first ✅ takes it, schedule and duty move, a warning over 20%", async () => {
  const t = await setup({ settings: team });
  await t.store.put("schedule", { [DAY]: { 14: MARCO, 15: MARCO, 16: MARCO, 17: MARCO } });
  await tapAt(t, "duty:on", MARCO, at(14));
  await t.store.put("duty", { ...(await t.store.get("duty")), log: [{ id: LUCA, start: at(8), end: at(10) }] });   // Luca did 2 h
  t.tg.clear();
  await tapAt(t, "duty:swap", MARCO, at(14, 30));
  const choices = t.tg.sent().find((p) => p.text?.includes("how long do you need covered"));
  assert.deepEqual(choices.reply_markup.inline_keyboard[0].map((b) => b.callback_data),
    [`swr:1:${MARCO}`, `swr:2:${MARCO}`, `swr:3:${MARCO}`, `swr:rest:${MARCO}`]);
  await tapAt(t, `swr:2:${MARCO}`, LUCA, at(14, 31));                   // not Luca's question
  assert.match(t.tg.sent("answerCallbackQuery").at(-1).text, /someone else's question/);
  t.tg.clear();
  await tapAt(t, `swr:2:${MARCO}`, MARCO, at(14, 31));
  const req = t.tg.sent().find((p) => p.text?.includes("needs a swap"));
  assert.match(req.text, /needs a swap: 14:31–16:31 today/);
  assert.equal(req.reply_markup.inline_keyboard[0][0].callback_data, "swt:1");
  await tapAt(t, "swt:1", MARCO, at(14, 32));
  assert.match(t.tg.sent("answerCallbackQuery").at(-1).text, /your own/);
  t.tg.clear();
  await tapAt(t, "swt:1", LUCA, at(14, 33));
  const done = inGroup(t).find((x) => x.includes("covers Marco's 14:31–16:31"));
  assert.ok(done && done.includes("⚠️ That puts Luca at 4 h on that day (max 2.8 h)"));
  const sched = (await t.store.get("schedule"))[DAY];
  assert.deepEqual([sched[14], sched[15], sched[16], sched[17]], [LUCA, LUCA, LUCA, MARCO]);   // every block it touches
  assert.equal((await t.store.get("duty")).on.id, LUCA);
  await tapAt(t, "swt:1", OWNER, at(14, 34));
  assert.match(t.tg.sent("answerCallbackQuery").at(-1).text, /already took it/);
});

test("scheduled blocks: a reminder 10 minutes before, the group 15 minutes late", async () => {
  const t = await setup({ settings: team });
  await t.store.put("schedule", { [DAY]: { 14: LUCA, 15: LUCA } });
  await tapAt(t, "duty:on", MARCO, at(9));
  t.tg.clear();
  await t.cron(at(13, 45));
  assert.equal(t.tg.sent().filter((p) => p.text?.includes("your duty starts")).length, 0);
  await t.cron(at(13, 50));
  const dm = t.tg.sent().find((p) => p.text?.includes("your duty starts at 14:00"));
  assert.equal(String(dm.chat_id), String(LUCA));
  await t.cron(at(13, 55));
  assert.equal(t.tg.sent().filter((p) => p.text?.includes("your duty starts")).length, 1);   // once
  await t.cron(at(14, 10));
  assert.ok(!inGroup(t).some((x) => x.includes("was due on duty")));
  await t.cron(at(14, 15));
  assert.ok(inGroup(t).some((x) => x.includes("Luca</a> was due on duty at 14:00 and hasn't started")));
  await t.cron(at(15, 5));                                               // 15:00 is the same run of blocks: no new ping
  assert.equal(inGroup(t).filter((x) => x.includes("was due on duty")).length, 1);
});

test("tasks: add with @name and a due date, list, done, a reminder when 3 days late", async () => {
  const t = await setup({ settings: team, now: at(10) });
  await t.updates(msg("/task add Photos for #12 @Luca by fri"), msg("/task add Answer the buyer @marco"),
    msg("/task add Something @nobody"), msg("/task add Thing @Luca by someday"), msg("/tasks"));
  const r = t.tg.texts();
  assert.ok(r[0].includes("Task 1 for") && r[0].includes(">Luca</a>: Photos for #12 · due Fri 09 Oct"));
  assert.match(r[1], /Task 2 for .*Marco.*: Answer the buyer$/);
  assert.match(r[2], /Who is it for\?/);
  assert.match(r[3], /I don't understand the date "someday"/);
  assert.ok(r[4].includes("1. Photos for #12 · Luca · due Fri 09 Oct") && r[4].includes("2. Answer the buyer · Marco"));
  t.tg.clear();
  await t.cron(romeTs("2026-10-12", 10));                                 // task 1 is 3 days past Friday? not yet (Mon)
  assert.ok(!inGroup(t).some((x) => x.includes("task 1")));
  assert.ok(inGroup(t).some((x) => x.includes("task 2 is open for 5 days: Answer the buyer")));   // no due date: 3 days after creation
  await t.cron(romeTs("2026-10-13", 10));
  assert.ok(inGroup(t).some((x) => x.includes("task 1 is 3 days late: Photos for #12")));
  t.tg.clear();
  await t.updates(msg("/task done 1", { user: LUCA }), msg("/task done 1"), msg("/task done 9"));
  const d = t.tg.texts();
  assert.match(d[0], /✅ Task 1 done: Photos for #12/);
  assert.match(d[1], /already done/);
  assert.match(d[2], /No task 9/);
});

test("the Sunday report adds the team: duty hours, deals caught, reaction time, tasks, stock, bot health", async () => {
  const sun = romeTs("2026-10-11", 20, 5);
  const t = await setup({ settings: team, now: sun });
  await t.store.put("duty", { on: null, pin: null, swaps: {}, next_swap: 1, reminded: {}, late: {}, nobody_ping: 0,
    log: [{ id: MARCO, start: romeTs("2026-10-10", 9), end: romeTs("2026-10-10", 17) },
      { id: LUCA, start: romeTs("2026-10-10", 17), end: romeTs("2026-10-10", 20) }] });
  const key = await addDeal(t.store);
  const d = await t.store.deal(key);
  Object.assign(d, { status: "bought", who: "Marco", who_id: MARCO, paid: 30, alerted_at: romeTs("2026-10-10", 10),
    claimed_at: romeTs("2026-10-10", 10, 4) });
  await t.store.saveDeal(key, d);
  await t.updates(msg("/task add Photos @Luca by tomorrow"));
  for (let i = 0; i < 3; i++) await t.api("POST", "/api/run", { status: { runs: 1 } }, { at: sun - 3600 });
  await t.api("POST", "/api/run", { notify: [{ text: "⚠️ <b>flipFinder: the last 3 runs failed.</b>" }] }, { at: sun - 3000 });
  await t.store.put("status", { last_run: sun - 120 });
  t.tg.clear();
  await t.cron(sun);
  const report = t.tg.texts().find((x) => x.includes("week to"));
  assert.ok(report.includes("· Marco: 8 h on duty (target 56) · 1 deal caught on shift"));
  assert.ok(report.includes("· Luca: 3 h on duty (max 19.6) · 0 deals caught on shift"));
  assert.ok(report.includes("⏱ Average reaction: 4 min (alert to claim, 1 deals)"));
  assert.ok(report.includes("📝 Open tasks: 1"));
  assert.ok(report.includes("📦 Bought, not listed: 1 · 🏷 Listed, not sold: 0"));
  assert.ok(report.includes("🩺 Bot health: ⚠️ 1 failure alert (3 scans)"));
});

test("the app's Schedule tab: start/end duty, claim and free blocks, a swap on your own block", async () => {
  const t = await setup({ settings: team, now: at(10) });
  const { verifyInitData } = await import("../src/webapp.js");
  assert.ok(verifyInitData);
  const enc = new TextEncoder();
  const hmac = async (key, data) => crypto.subtle.sign("HMAC", await crypto.subtle.importKey("raw",
    typeof key === "string" ? enc.encode(key) : key, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]), enc.encode(data));
  const init = async (user) => {
    const f = { auth_date: String(at(10) - 60), user: JSON.stringify(user) };
    const check = Object.keys(f).sort().map((k) => `${k}=${f[k]}`).join("\n");
    const hash = [...new Uint8Array(await hmac(await hmac("WebAppData", "TOKEN"), check))].map((b) => b.toString(16).padStart(2, "0")).join("");
    return new URLSearchParams({ ...f, hash }).toString();
  };
  const call = async (user, body, when = at(10)) => {
    const r = await handleRequest(new Request(`https://w/app/api/${body ? "action" : "state"}`, { method: body ? "POST" : "GET",
      headers: { authorization: `tma ${await init(user)}`, "content-type": "application/json" }, body: body ? JSON.stringify(body) : undefined }),
    t.env, { fetchFn: t.tg.fetch, now: when });
    return { status: r.status, body: await r.json() };
  };
  const marco = { id: MARCO, first_name: "Marco" };
  let r = await call(marco);
  const g = r.body.schedule;
  assert.equal(g.weeks.length, 2);
  assert.deepEqual(g.weeks[0].map((d) => d.label), ["Mon 05", "Tue 06", "Wed 07", "Thu 08", "Fri 09", "Sat 10", "Sun 11"]);
  assert.deepEqual(g.members.map((m) => [m.name, m.target ?? null, m.cap ?? null]), [["Boss", null, 2.8], ["Marco", 8, null], ["Luca", null, 2.8]]);
  r = await call(marco, { action: "block", date: DAY, hour: 15 });
  assert.equal(r.body.state.schedule.weeks[0][2].blocks[15], MARCO);
  assert.equal((await call({ id: LUCA, first_name: "Luca" }, { action: "block", date: DAY, hour: 15 })).body.error,
    "That's Marco's block: ask for a swap");
  assert.match((await call(marco, { action: "block", date: DAY, hour: 9 })).body.error, /That hour is over/);
  assert.match((await call(marco, { action: "block", date: DAY, hour: 23 })).body.error, /08:00–22:00/);
  r = await call(marco, { action: "swap_block", date: DAY, hour: 15 });
  assert.match(r.body.notice, /Swap request posted: 15:00–16:00 today/);
  assert.ok(inGroup(t).some((x) => x.includes("Marco</a> needs a swap: 15:00–16:00 today")));
  assert.equal((await call({ id: LUCA, first_name: "Luca" }, { action: "swap_block", date: DAY, hour: 15 })).status, 400);
  r = await call(marco, { action: "duty_start" });
  assert.match(r.body.notice, /You're on duty/);
  assert.equal(r.body.state.schedule.on.name, "Marco");
  r = await call(marco, { action: "duty_end" }, at(11));
  assert.equal(r.body.state.schedule.on, null);
  assert.equal(r.body.state.schedule.members.find((m) => m.id === MARCO).today, 1);
  r = await call(marco, { action: "block", date: DAY, hour: 15 });          // tap again: freed
  assert.equal(r.body.state.schedule.weeks[0][2].blocks[15], undefined);
});

test("every team message is valid Telegram HTML (no stray <...> tags)", async () => {
  const t = await setup({ settings: team, now: at(10) });
  await t.updates(msg("/roles"), msg("/task add Photos @Luca"), msg("/tasks"), msg("/duty"), msg("/help"));
  const ok = new Set(["b", "/b", "i", "/i", "code", "/code", "a", "/a"]);
  for (const text of t.tg.texts()) {
    for (const tag of text.matchAll(/<([^>\s]+)[^>]*>/g)) assert.ok(ok.has(tag[1]), `bad tag <${tag[1]}> in: ${text.slice(0, 80)}`);
  }
});
