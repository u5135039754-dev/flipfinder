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
