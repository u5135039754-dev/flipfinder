// The shared pot: real money sits in the treasurer's bank account (/settreasurer; the owner until set);
// the bot only keeps
// the numbers. Every money action is a ledger entry and entries are never changed: a correction
// is a new entry. Each entry says what it does to the pot's cash and to members' accounts:
//   deposited / withdrawn / profit: {member name: euros}
// A sale's profit is split when it happens (by contribution or equally), so later deposits and
// withdrawals never change how earlier profit was shared.
// Bought with a member's own money: a "personal" entry puts the price back into the cash and the pot
// owes them (owed: {name: euros}); /refund pays it back (cash out, owed down).

import { esc, euro } from "./util.js";


const round2 = (x) => Math.round(x * 100) / 100;

/** "marco" and "Marco" are the same member; the first spelling used is the one shown. */
export function memberKey(name) {
  return name.trim().toLowerCase();
}

/** Cash, stock at cost, profit and every member's account from the ledger and the deals. */
export function summarize(entries, deals) {
  const members = {};
  const member = (name) => (members[memberKey(name)] ??= { name, deposited: 0, withdrawn: 0, profit: 0, owed: 0 });
  let cash = 0;
  let profit = 0;
  for (const e of entries) {
    cash += e.amount;
    for (const [name, x] of Object.entries(e.deposited || {})) member(name).deposited += x;
    for (const [name, x] of Object.entries(e.withdrawn || {})) member(name).withdrawn += x;
    for (const [name, x] of Object.entries(e.profit || {})) {
      member(name).profit += x;
      profit += x;
    }
    for (const [name, x] of Object.entries(e.owed || {})) member(name).owed += x;
  }
  const stock = deals.filter(([, d]) => ["bought", "listed"].includes(d.status))
    .reduce((s, [, d]) => s + (d.paid || 0), 0);
  const list = Object.values(members).map((m) => ({
    ...m, net: round2(m.deposited - m.withdrawn), account: round2(m.deposited - m.withdrawn + m.profit + m.owed),
    deposited: round2(m.deposited), withdrawn: round2(m.withdrawn), profit: round2(m.profit), owed: round2(m.owed),
  }));
  const owed = round2(list.reduce((s, m) => s + m.owed, 0));
  return { cash: round2(cash), stock: round2(stock), profit: round2(profit), owed, members: list, started: entries.length > 0 };
}

/** Who gets what share of profit now: by net contribution, or equally among members with money in. */
export function shares(summary, mode = "contribution") {
  const inMembers = summary.members.filter((m) => m.net > 0);
  if (!inMembers.length) return {};
  const total = inMembers.reduce((s, m) => s + m.net, 0);
  return Object.fromEntries(inMembers.map((m) => [m.name, mode === "equal" ? 1 / inMembers.length : m.net / total]));
}

/** Split euros by shares to the cent; leftover cents go to the biggest shares. */
export function allocate(amount, shareMap) {
  const names = Object.keys(shareMap);
  if (!names.length) return {};
  const cents = Math.round(amount * 100);
  const out = Object.fromEntries(names.map((n) => [n, Math.trunc(cents * shareMap[n])]));
  let left = cents - Object.values(out).reduce((s, x) => s + x, 0);
  const order = [...names].sort((a, b) => shareMap[b] - shareMap[a]);
  for (let i = 0; left !== 0; i = (i + 1) % order.length) {
    out[order[i]] += Math.sign(left);
    left -= Math.sign(left);
  }
  return Object.fromEntries(names.map((n) => [n, out[n] / 100]));
}

/** Negates everything an entry did (for /undo). */
export function reverse(e) {
  const neg = (m) => (m ? Object.fromEntries(Object.entries(m).map(([k, v]) => [k, -v])) : undefined);
  return { amount: -e.amount, deposited: neg(e.deposited), withdrawn: neg(e.withdrawn), profit: neg(e.profit), owed: neg(e.owed) };
}

export function potText(summary, mode, budget, treasurer = "") {
  const s = summary;
  const lines = [
    "💰 <b>The pot</b>",
    ...(treasurer ? [`🏦 ${esc(treasurer)} holds the pot's money (treasurer); the bot only keeps the numbers`] : []),
    `Cash: <b>${euro(s.cash)}</b> · in stock (at cost): ${euro(s.stock)} · total: ${euro(s.cash + s.stock)}`,
    `Profit so far: ${euro(s.profit)} · split ${mode === "equal" ? "equally" : "by contribution"}`,
  ];
  if (budget !== undefined) lines.push(`Budget-mode limit: ${euro(budget)} (the smaller of /budget and the cash)`);
  const owing = s.members.filter((m) => m.owed > 0.004);
  if (owing.length) {
    lines.push(`👛 <b>Owed to members: ${euro(s.owed)}</b> (bought with their own money, not paid back yet: ` +
      `${owing.map((m) => `${esc(m.name)} ${euro(m.owed)}`).join(", ")}). /refund name amount deal# when paid`);
  }
  const sh = shares(s, mode);
  if (s.members.length) {
    lines.push("", "<b>Members</b> · put in · profit share · back if we stopped today");
    for (const m of [...s.members].sort((a, b) => b.account - a.account)) {
      const pct = sh[m.name] ? ` (${Math.round(sh[m.name] * 100)}%)` : "";
      lines.push(`· ${esc(m.name)}: ${euro(m.net)} · ${euro(m.profit)}${pct} · <b>${euro(m.account)}</b>`);
    }
    lines.push("<i>Stock counts at what we paid until it sells.</i>");
  } else {
    lines.push("", "No money in yet. The owner records it with /deposit name amount");
  }
  return lines.join("\n");
}

/** One line for the ledger list and the group post. */
export function entryLine(e) {
  const sign = e.amount >= 0 ? "+" : "−";
  const who = e.member ? ` ${esc(e.member)}` : "";
  const deal = e.n ? ` #${e.n}` : "";
  const what = {
    deposit: `deposit from${who}`, withdraw: `withdrawal to${who}`, buy: `bought${deal}`, sale: `sold${deal}`,
    fix: `correction${deal}`, undo: `undo of entry ${e.ref}`,
    personal: `${deal.trim()} paid with${who}'s own money (the pot owes it)`, refund: `refund to${who}${deal ? ` for${deal}` : ""}`,
  }[e.kind] || e.kind;
  return `${e.id ? `${e.id}. ` : ""}${sign}${euro(Math.abs(e.amount))} ${what}${e.note ? ` (${esc(e.note)})` : ""}`;
}
