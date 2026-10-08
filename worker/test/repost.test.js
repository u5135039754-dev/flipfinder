import { test } from "node:test";
import assert from "node:assert/strict";
import { setup, DAYTIME } from "./helpers.js";
import { findRepost, sameSeller, titleSimilarity } from "../src/deals.js";

const seller = (extra) => ({ source: "vinted", reviews: 5, stars: 4.8, sold: 1, ...extra });
const deal = (n, title, price, s, sent = DAYTIME) => [`vinted:${n}`, { n, title, sent, item: { price, seller: s } }];

test("same seller + very similar title + similar price within 7 days = a repost", () => {
  const recent = [deal(207, "Apple Watch Series SE 3", 80, seller({ id: 42 }))];
  assert.equal(findRepost({ title: "Apple Watch Series SE 3", item: { price: 80, seller: seller({ id: 42 }) } }, recent, DAYTIME)[1].n, 207);
  assert.equal(findRepost({ title: "apple watch series se3 3", item: { price: 84, seller: seller({ id: 42 }) } }, recent, DAYTIME), null);
  assert.equal(findRepost({ title: "Apple Watch Series SE 3", item: { price: 80, seller: seller({ id: 43 }) } }, recent, DAYTIME), null);
  assert.equal(findRepost({ title: "Apple Watch Series SE 3", item: { price: 120, seller: seller({ id: 42 }) } }, recent, DAYTIME), null);
  assert.equal(findRepost({ title: "Apple Watch Series SE 3", item: { price: 80, seller: seller({ id: 42 }) } }, recent,
    DAYTIME + 8 * 86400), null);                                              // a week later: a new alert
});

test("two PS5 Slims from different sellers at different prices are two deals", () => {
  const recent = [deal(206, "Ps5 Slim", 240, seller({ id: 1, reviews: 8, stars: 4.5, sold: 8 }))];
  assert.equal(findRepost({ title: "Ps5 slim", item: { price: 280, seller: seller({ id: 2, reviews: 10, stars: 3.8, sold: 9 }) } },
    recent, DAYTIME), null);
});

test("no account number (eBay, Subito): the same trust numbers count, but never for brand-new sellers", () => {
  assert.ok(sameSeller(seller(), seller()));
  assert.ok(!sameSeller(seller({ reviews: 0, stars: null, sold: 0 }), seller({ reviews: 0, stars: null, sold: 0 })));
  assert.ok(!sameSeller(seller({ id: 1 }), seller()));
  assert.equal(titleSimilarity("iPhone 14 Pro, schwarz 128GB", "iPhone 14 Pro schwarz 128GB"), 1);
});

test("the Worker sends the first and refuses the repost", async () => {
  const t = await setup();
  const post = (key, at) => t.api("POST", "/api/deal", { key, text: "🔥 Apple Watch", photo: "", group: "Electronics",
    record: { title: "Apple Watch Series SE 3", cost: 84.7, value: 140, item: { price: 80, seller: seller({ id: 42 }) } } }, { at });
  assert.equal((await post("vinted:10290525774", DAYTIME)).body.status, "sent");
  assert.deepEqual((await post("vinted:10290563205", DAYTIME + 240)).body, { status: "repost", of: 1 });
  assert.equal(t.tg.sent().filter((p) => p.text?.includes("Apple Watch")).length, 2);   // one alert: private chat + group
});
