"""Reverb: listings from its API (EUR, shipping to Italy, no photos), the 25%-under-the-median rule, resale fees, the scan."""

from dataclasses import replace

import pytest

from flipfinder import config
from flipfinder.analyzer import Rules
from flipfinder.reverb import DISCOUNT, ReverbClient, assess_reverb, item_from_reverb
from flipfinder.scanner import Scanner

R = Rules(min_profit=20, min_roi=35, min_rating=1, max_roi=400, max_price=300, shipping_cost=22, resell_costs=10)


def raw(i, price, model="Katana 50 MkII", cond="excellent", make="Boss", ship_it=15.0, title=None):
    return {"id": i, "title": title or f"{make} {model}", "make": make, "model": model, "condition": {"slug": cond},
            "price": {"amount": f"{price:.2f}", "currency": "EUR"}, "photos": [{"_links": {"full": {"href": "https://img"}}}],
            "shipping": {"rates": [{"region_code": "IT", "rate": {"amount": f"{ship_it:.2f}", "currency": "EUR"}}]},
            "_links": {"web": {"href": f"https://reverb.com/item/{i}"}}}


def test_listing_in_euros_with_shipping_to_italy_and_no_photos():
    it = item_from_reverb(raw(1, 120))
    assert (it.price, it.shipping, it.condition, it.source, it.url) == (120.0, 15.0, "excellent", "reverb", "https://reverb.com/item/1")
    assert not it.photo and not it.photos
    assert item_from_reverb({**raw(2, 99), "price": {"amount": "99", "currency": "USD"}}) is None   # only euro prices


def pool(n=6, price=240, **kw):
    return [item_from_reverb(raw(100 + k, price + k, **kw)) for k in range(n)]


def test_25_percent_under_the_median_of_the_same_model_and_condition():
    comps = pool() + pool(price=400, model="Katana 100") + pool(price=150, cond="good")
    d = assess_reverb(item_from_reverb(raw(1, 120)), comps, R)
    assert d.market_value == 242.5 and d.comparables == 6 and not d.blocked
    # fees: (242.5 + 22) x 8.19% + 0.49; cost 120 + 15; profit = 242.5 - fee - 135 - 10
    assert d.sell_fee == round(264.5 * 0.0819 + 0.49, 2) and d.profit == round(242.5 - d.sell_fee - 135 - 10, 2)
    assert d.resell_on == "reverb" and "Reverb" in d.basis
    near = assess_reverb(item_from_reverb(raw(2, 242.5 * (1 - DISCOUNT) + 5)), comps, replace(R, min_profit=0, min_roi=0))
    assert any("25% under" in b for b in near.blocked)
    assert assess_reverb(item_from_reverb(raw(3, 150, cond="fair")), comps, R) is None    # under Good
    assert assess_reverb(item_from_reverb(raw(4, 150, model="Katana Air")), comps, R) is None   # no comparables


def test_jackson_on_reverb_matches_line_and_shape():
    comps = [item_from_reverb(raw(100 + k, 300, make="Jackson", model="JS32 King V")) for k in range(6)]
    comps += [item_from_reverb(raw(200 + k, 150, make="Jackson", model="JS1X King V")) for k in range(6)]
    d = assess_reverb(item_from_reverb(raw(1, 180, make="Jackson", model="JS-32 KV")), comps, R, "jackson")
    assert d.market_value == 300 and d.kind == "jackson js32 king v"


def test_client_asks_for_used_eu_listings_shipping_to_italy(monkeypatch):
    with pytest.raises(ValueError):
        ReverbClient("")
    c = ReverbClient("t", "eu", delay=0)
    assert c.session.headers["Authorization"] == "Bearer t" and c.session.headers["X-Display-Currency"] == "EUR"
    seen = []
    monkeypatch.setattr(c, "_get", lambda params: seen.append(params) or [raw(1, 100), raw(2, 90, cond="fair")])
    items = c.newest("boss katana", 80, 250)
    assert [i.id for i in items] == [1]                                   # "fair" is under Good
    assert seen[0]["item_region"] == "EUR_EU" and seen[0]["ships_to"] == "IT" and seen[0]["condition"] == "used"
    ReverbClient("t", "all", delay=0)._get.__func__                        # anywhere: no region filter
    c2 = ReverbClient("t", "all", delay=0)
    seen.clear()
    monkeypatch.setattr(c2, "_get", lambda params: seen.append(params) or [])
    c2.newest("x", None, None)
    assert "item_region" not in seen[0]


def test_scan_seeds_then_alerts_and_keeps_reverb_prices_to_reverb(tmp_path, monkeypatch):
    cfg = config.load()
    cfg.seen_file = tmp_path / "seen.json"
    cfg.subito.enabled = False
    cfg.reverb.enabled, cfg.reverb.token, cfg.reverb.searches_per_run = True, "t", 1
    cfg.searches = [s for s in cfg.searches if s.query == "boss katana"]
    sc = Scanner(cfg)
    sc.first_run = False
    newest = [[raw(1, 120)], [raw(1, 120), raw(2, 110)]]
    monkeypatch.setattr(sc.reverb, "newest", lambda q, a, b: [item_from_reverb(x) for x in newest.pop(0)])
    monkeypatch.setattr(sc.reverb, "pool", lambda q, pages: pool())
    assert sc._scan_reverb() == []                                        # first pass: remembered
    deals = sc._scan_reverb()
    assert [d.item.id for d in deals] == [2] and deals[0].item.source == "reverb"
    assert sc.pools.get("vinted " + "boss katana") is None                # Reverb prices never join other pools
    assert all(k.startswith("reverb ") for k in sc.pools.data)
