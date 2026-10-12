"""Music: wider local Subito pickup with costs by distance, the shipping share rule, Jackson model lines, the slow lane."""

from dataclasses import replace

from flipfinder import config
from flipfinder.analyzer import Rules, assess, jackson_kind
from flipfinder.scanner import MAX_SLOW_PER_RUN, Scanner, _sid
from flipfinder.subito import SubitoClient
from flipfinder.telegram import format_deal
from flipfinder.vinted import Item

R = Rules(min_profit=20, min_roi=35, min_rating=1, min_comparables=5, max_roi=400, model="jackson")


def it(i, price, title="Jackson JS32 King V", **kw):
    return Item(id=i, title=title, price=price, total_price=price, currency="EUR", url=f"https://x/{i}", brand="Jackson", **kw)


def test_jackson_lines_and_shapes():
    assert jackson_kind("Jackson JS-32 King V") == jackson_kind("jackson js 32 king v") == "js32 king v"
    assert jackson_kind("Jackson KVX10") == "x series king v"
    assert jackson_kind("Jackson Dinky JS22 mancina") == "js22 dinky lefty"
    assert jackson_kind("Jackson Pro Soloist") == "pro soloist"


def test_jackson_comparables_stay_in_the_line_and_shape_and_floyd_is_noted():
    pool = ([it(100 + n, 250) for n in range(6)]                                   # JS32 King V ~250
            + [it(200 + n, 120, title="Jackson JS1X King V") for n in range(6)]       # cheaper line
            + [it(300 + n, 400, title="Jackson JS32 Rhoads") for n in range(6)])      # other shape
    d = assess(it(1, 150, description="Floyd Rose, suona benissimo"), pool, R, "jackson king v", shipping=15)
    assert d.market_value == 250 and d.comparables == 6 and d.kind == "js32 king v"
    assert "Floyd Rose: harder to set up" in d.check and "Floyd Rose" in format_deal(d)
    assert assess(it(2, 150, title="Jackson JS11 King V"), pool, R, "jackson king v", shipping=15) is None   # no JS11s to compare


def test_guitar_shipping_over_20_percent_is_skipped_on_vinted():
    pool = [it(100 + n, 250) for n in range(6)]
    rules = replace(R, max_ship_share=0.2)
    assert not assess(it(1, 150), pool, rules, "jackson king v", shipping=25).blocked            # 17%
    d = assess(it(1, 150), pool, rules, "jackson king v", shipping=35)                          # 23%
    assert "shipping €35 over 20% of the price" in d.blocked
    assert not assess(it(1, 150), pool, rules, "jackson king v", shipping=0.0).blocked           # the free pre-check


def subito(**kw):
    return SubitoClient(region=5, province=2, center=(46.07, 11.12), radius_km=30, travel_cost=5, request_delay=0,
                        wide_regions=[5, 6], wide_radius_km=100, tiers=[[45, 5], [100, 15]], **kw)


def test_pickup_cost_by_distance():
    sb = subito()
    near, far = it(1, 100, location="Rovereto"), it(2, 100, location="Verona")
    near.distance_km, far.distance_km = 22, 92
    assert sb.travel(near) == 5 and sb.travel(far) == 15
    assert subito(town_travel_costs={"Verona": 12}).travel(far) == 12   # a town's own cost wins


def test_music_category_reads_the_wide_regions_within_the_radius(monkeypatch):
    sb = subito()
    calls = []

    def fake_get(params):
        calls.append(params)
        town = {5: ("Bolzano", 46.50, 11.35), 6: ("Venezia", 45.44, 12.33)}[params["r"]]
        return [{"urn": f"id:ad:{params['r']}", "subject": "Fender amp", "type": {"key": "s"},
                 "features": [{"uri": "/price", "values": [{"key": "100"}]}],
                 "geo": {"town": {"value": town[0], "lat": town[1], "lon": town[2]}, "city": {"value": town[0]}}}]
    monkeypatch.setattr(sb, "_get", fake_get)
    items = sb.newest_in_area(39)
    assert [c["r"] for c in calls] == [5, 6] and all("ci" not in c for c in calls)
    assert [i.location for i in items] == ["Bolzano"] and items[0].delivery == "pickup" and items[0].shipping == 15
    text = format_deal(assess(items[0], [it(10 + n, 220, title="Fender amp") for n in range(6)],
                              replace(R, model=""), "fender amp"))
    assert "📍 <b>Pickup Bolzano</b> (" in text


def test_slow_lane_spreads_the_searches(tmp_path, monkeypatch):
    cfg = config.load()
    cfg.seen_file = tmp_path / "seen.json"
    cfg.subito.enabled = False
    sc = Scanner(cfg)
    slow = [s for s in cfg.searches if s.slow_minutes]
    assert len(slow) > MAX_SLOW_PER_RUN
    for s in cfg.searches:
        sc.known.add(s)
        sc.known.last[_sid(s)] = 0.0   # all overdue
    ran = []
    monkeypatch.setattr(sc, "scan_search", lambda s: ran.append(s.query) or [])
    monkeypatch.setattr(sc, "_fetch_subito", lambda: None)
    sc.cfg.stagger = False
    sc.scan()
    assert sum(1 for q in ran if q in {s.query for s in slow}) == MAX_SLOW_PER_RUN
    jackson = next(s for s in cfg.searches if s.query == "jackson js32")
    assert sc._rules(jackson).max_price == 300 and sc._price_to(jackson) == 300 and sc._rules(jackson).model == "jackson"
    katana = next(s for s in cfg.searches if s.query == "boss katana")
    assert sc._rules(katana).max_price == 250 and sc._rules(katana).max_ship_share == 0.2


def test_a_search_priced_over_its_max_is_skipped_not_failed(tmp_path, monkeypatch):
    from flipfinder.config import Search
    cfg = config.load()
    cfg.seen_file = tmp_path / "seen.json"
    cfg.subito.enabled = False
    sc = Scanner(cfg)
    gibson = next(s for s in cfg.searches if s.query == "gibson les paul studio")
    assert sc._price_to(gibson) == 800 and sc._rules(gibson).max_price == 800   # its own max, over the music €250
    pricey = Search("gibson custom", price_from=900, group="Guitars")              # starts over the music max
    cfg.searches, cfg.stagger = [pricey], False
    sc.known.add(pricey)
    sc.known.last[_sid(pricey)] = 0.0
    ran = []
    monkeypatch.setattr(sc, "scan_search", lambda s: ran.append(s.query) or [])
    monkeypatch.setattr(sc, "_fetch_subito", lambda: None)
    sc.scan()
    assert ran == [] and sc.failed_searches == 0


def test_new_searches_start_a_few_a_run(tmp_path, monkeypatch):
    from flipfinder.scanner import MAX_NEW_PER_RUN
    cfg = config.load()
    cfg.seen_file = tmp_path / "seen.json"
    cfg.subito.enabled = False
    cfg.stagger = False
    sc = Scanner(cfg)
    sc.first_run = False
    ran = []
    monkeypatch.setattr(sc, "scan_search", lambda s: ran.append(s.query) or [])
    monkeypatch.setattr(sc, "_fetch_subito", lambda: None)
    sc.seen.add("vinted:0")                    # not the very first run
    sc.first_run = False
    sc.scan()
    assert len(ran) == MAX_NEW_PER_RUN          # none of them ran before: only a few start this run


def test_time_budget_order_and_carry_over(tmp_path, monkeypatch):
    """Electronics/clothes, then budget, then music; past the budget nothing new starts, and the rest go first next run."""
    import json
    from flipfinder.scanner import _sid
    from flipfinder.settings import search_group
    cfg = config.load()
    cfg.seen_file = tmp_path / "seen.json"
    cfg.subito.enabled = False
    cfg.stagger = False
    for s in cfg.searches:
        s.every_minutes = s.slow_minutes = 0
    sc = Scanner(cfg)
    for s in cfg.searches:
        sc.known.add(s)                          # all known: no "new search" cap in the way
    sc.seen.add("vinted:0")
    sc.first_run = False
    ran = []
    def fake(s):
        ran.append(s)
        if len(ran) == 5:
            sc.deadline = 0                      # time's up after the 5th search
        return []
    monkeypatch.setattr(sc, "scan_search", fake)
    monkeypatch.setattr(sc, "_fetch_subito", lambda: None)
    monkeypatch.setattr(sc, "check_sold", lambda: None)
    sc.scan()
    tiers = [sc._tier(s) for s in ran]
    assert len(ran) == 5 and tiers == sorted(tiers) and tiers[0] == 0       # fast sellers first
    carried = json.loads((tmp_path / "carry.json").read_text())
    enabled = [s for s in cfg.searches if s.enabled]
    ran_ids = {_sid(s) for s in ran}
    runnable = [s for s in enabled if not (s.price_from is not None and sc._price_to(s) is not None
                                           and s.price_from >= sc._price_to(s))]
    assert set(carried) == {_sid(s) for s in runnable} - ran_ids                # everything not reached waits
    # next run: the carried ones go first, even the music ones whose turn it isn't
    sc2 = Scanner(cfg)
    sc2.seen.add("vinted:0")
    sc2.first_run = False
    ran2 = []
    monkeypatch.setattr(sc2, "scan_search", lambda s: ran2.append(s) or [])
    monkeypatch.setattr(sc2, "_fetch_subito", lambda: None)
    monkeypatch.setattr(sc2, "check_sold", lambda: None)
    sc2.scan()
    assert [_sid(s) for s in ran2[:len(carried)]] == [_sid(s) for s in sorted((s for s in enabled if _sid(s) in carried),
                                                                              key=sc2._tier)]
    assert json.loads((tmp_path / "carry.json").read_text()) == []


def test_reverb_every_third_run(tmp_path, monkeypatch):
    import time as t
    cfg = config.load()
    cfg.seen_file = tmp_path / "seen.json"
    cfg.subito.enabled = False
    sc = Scanner(cfg)
    sc.reverb = object()                          # pretend Reverb is set up
    sc.seen.add("vinted:0")
    sc.first_run = False
    calls = []
    monkeypatch.setattr(sc, "scan_search", lambda s: [])
    monkeypatch.setattr(sc, "_fetch_subito", lambda: None)
    monkeypatch.setattr(sc, "check_sold", lambda: None)
    monkeypatch.setattr(sc, "_scan_reverb", lambda: calls.append(1) or [])
    sc.scan()
    sc.deadline = t.time() + 300
    sc.scan()                                     # 5 minutes later in real life: too soon
    assert calls == [1]
    sc.reverb_state["last_pass"] -= 15 * 60
    sc.deadline = t.time() + 300
    sc.scan()
    assert calls == [1, 1]


def test_fast_lane_subito_timing():
    from flipfinder import fastlane
    fs = {**fastlane.DEFAULTS, "enabled": True}
    assert not fastlane.subito_due(fs, 1000.0)                              # off until /fast subito on
    fs["subito"] = True
    assert fastlane.subito_due(fs, 1000.0)
    fs["subito_backoff_until"] = 5000.0                                      # backing off after a block
    assert not fastlane.subito_due(fs, 1100.0, last_subito=1000.0)          # only every 5 min
    assert fastlane.subito_due(fs, 1300.0, last_subito=1000.0)


def test_subito_fast_mode_stops_at_once_on_a_block(monkeypatch):
    import time as t
    from flipfinder.subito import SubitoBlocked
    sb = SubitoClient(region=1, province=1, center=(45.0, 9.0), fast=True)
    slept = []
    monkeypatch.setattr(t, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(sb.session, "get", lambda *a, **k: type("R", (), {"status_code": 429})())
    try:
        sb.newest_shipping(10)
        assert False, "should have stopped"
    except SubitoBlocked:
        pass
    assert sb.blocked == 1 and sb.calls == 1 and not any(s >= 30 for s in slept)   # no 30 s back-off wait


def test_fast_subito_rotates_within_its_time_and_finds_deals(tmp_path, monkeypatch):
    import time as t
    from flipfinder.scanner import _key
    cfg = config.load()
    cfg.seen_file = tmp_path / "seen.json"
    sc = Scanner(cfg)
    sc.seen.add("vinted:0")
    sc.first_run = False
    phone = next(s for s in cfg.searches if s.query == "iphone 13")
    for s in cfg.searches:
        sc.known.add(s)
    cat = sc._subito_category(phone)
    sc.pools.put(_key(phone), [Item(id=900 + i, title="iPhone 13 128GB", price=300, total_price=300, currency="EUR",
                                    url="u", brand="Apple") for i in range(12)])
    calls = []   # (market ~€300, so a €185 phone is a deal within the €200 cap and the 60% ROI limit)

    class FakeSubito:
        blocked = 0
        calls = 0
        fast = False

        def newest_nearby(self, c, radius):
            calls.append(("near", c, radius))
            return [Item(id=5, title="iPhone 13 128GB", price=185, total_price=185, currency="EUR", url="u", source="subito",
                         delivery="pickup", shipping=8.0, location="Town", distance_km=40)] if c == cat else []

        def newest_shipping(self, c):
            calls.append(("ship", c))
            return []

    sc.subito = FakeSubito()
    deals, done, total = sc.fast_subito([phone], t.time() + 30, start=0, radius=100)
    assert total == 2 and done == 2 and calls[0] == ("near", cat, 100)
    assert len(deals) == 1 and deals[0].item.source == "subito"
    # out of time: nothing more starts; the next pass picks up from `start`
    calls.clear()
    deals, done, total = sc.fast_subito([phone], t.time() - 1, start=1)
    assert (done, calls) == (0, [])
