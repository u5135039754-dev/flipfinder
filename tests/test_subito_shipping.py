"""Subito: local pickup and Italy-wide shipping, the cheaper within range, the first shipping pass remembered, labels."""

from flipfinder import config
from flipfinder.analyzer import assess, Rules
from flipfinder.scanner import Scanner
from flipfinder.subito import SubitoClient
from flipfinder.telegram import format_deal


def ad(i, town, lat, lon, price=100, ship=None):
    feats = [{"uri": "/price", "values": [{"key": str(price)}]}]
    if ship is not None:
        feats += [{"uri": "/item_shippable", "values": [{"key": "1"}]}, {"uri": "/item_shipping_cost", "values": [{"key": str(ship)}]}]
    return {"urn": f"id:ad:{i}", "subject": "Boss Katana 50", "features": feats,
            "geo": {"town": {"value": town, "lat": lat, "lon": lon}}}


def client(**kw):
    return SubitoClient(region=5, province=2, center=(46.07, 11.12), radius_km=30, travel_cost=5, request_delay=0,
                        wide_regions=[5, 6], wide_radius_km=100, tiers=[[45, 5], [100, 15]], **kw)


def test_shipping_italy_wide_and_the_cheaper_within_range(monkeypatch):
    sb = client()
    calls = []
    ads = [ad(1, "Rovereto", 45.89, 11.04, ship=9), ad(2, "Verona", 45.44, 10.99, ship=12), ad(3, "Napoli", 40.85, 14.27, ship=10)]
    monkeypatch.setattr(sb, "_get", lambda params: calls.append(params) or ads)
    items = {i.location: i for i in sb.newest_shipping(39)}
    assert calls[0]["shp"] == "true" and "r" not in calls[0]                  # anywhere in Italy, shippable only
    assert (items["Rovereto"].delivery, items["Rovereto"].shipping) == ("pickup", 5)     # €5 pickup beats €9 shipping
    assert (items["Verona"].delivery, items["Verona"].shipping) == ("shipping", 12)      # €12 shipping beats €15 pickup
    assert (items["Napoli"].delivery, items["Napoli"].shipping) == ("shipping", 10)      # far: shipping only


def test_labels_for_pickup_and_shipping():
    sb = client()
    far = sb.newest_shipping.__func__   # noqa: B018 (just the same client)
    rules = Rules(min_profit=10, min_roi=10, min_rating=1, min_comparables=5, max_roi=400, shipping_cost=10)
    pool = [__import__("flipfinder.vinted", fromlist=["Item"]).Item(id=100 + n, title="Boss Katana 50", price=200, total_price=200,
                                                                   currency="EUR", url="u") for n in range(6)]
    from flipfinder.subito import item_from_subito
    ship = item_from_subito(ad(3, "Napoli", 40.85, 14.27, ship=10))
    ship.delivery = "shipping"
    text = format_deal(assess(ship, pool, rules, "boss katana"))
    assert "📦 <b>Ships from Napoli</b>" in text and "Pickup" not in text
    pick = item_from_subito(ad(1, "Rovereto", 45.89, 11.04))
    pick.delivery, pick.shipping, pick.distance_km = "pickup", 5, 22
    assert "📍 <b>Pickup Rovereto</b> (22 km)" in format_deal(assess(pick, pool, rules, "boss katana"))


def test_first_shipping_pass_is_remembered_then_new_ones_count(tmp_path, monkeypatch):
    cfg = config.load()
    cfg.seen_file = tmp_path / "seen.json"
    cfg.subito.enabled, cfg.subito.region, cfg.subito.province, cfg.subito.center = True, 5, 2, (46.07, 11.12)
    cfg.searches = [s for s in cfg.searches if s.query == "boss katana"]
    sc = Scanner(cfg)
    sc.first_run = False
    batches = [[ad(1, "Napoli", 40.85, 14.27, ship=10)], [ad(1, "Napoli", 40.85, 14.27, ship=10), ad(2, "Bari", 41.12, 16.87, ship=10)]]
    monkeypatch.setattr(sc.subito, "newest_in_area", lambda cat: [])
    monkeypatch.setattr(sc.subito, "_get", lambda params: batches.pop(0))
    sc._fetch_subito()
    assert sc.subito_new == [] and ("subito:1" in sc.seen)                    # first pass: remembered only
    sc._fetch_subito()
    assert [i.id for i in sc.subito_new if i.key not in sc.seen] == [2]
    assert sc.subito_new[0].category == 39


def test_clothes_on_subito_take_the_search_brand(tmp_path):
    cfg = config.load()
    cfg.seen_file = tmp_path / "seen.json"
    cfg.subito.enabled = False
    sc = Scanner(cfg)
    stone = next(s for s in cfg.searches if s.query == "stone island")
    from flipfinder.subito import item_from_subito
    i = item_from_subito({**ad(5, "Milano", 45.46, 9.19, ship=8), "subject": "Felpa Stone Island L"})
    i.category, i.delivery = 16, "shipping"
    sc.subito_new = [i]
    got = sc._subito_for(stone)
    assert [x.brand for x in got] == ["Stone Island"] and i.brand == ""      # a copy: the shared listing is untouched
    assert sc._subito_for(next(s for s in cfg.searches if s.query == "boss katana")) == []   # other category


def test_music_pickups_up_to_300_with_40_profit_shipped_stay_at_250(tmp_path):
    from dataclasses import replace as rep
    from flipfinder.subito import item_from_subito
    cfg = config.load()
    cfg.seen_file = tmp_path / "seen.json"
    cfg.subito.enabled = False
    sc = Scanner(cfg)
    katana = next(s for s in cfg.searches if s.query == "boss katana")
    pick = item_from_subito(ad(1, "Rovereto", 45.89, 11.04, price=280))
    pick.category, pick.delivery, pick.shipping = 39, "pickup", 5
    ship = rep(pick, id=2, delivery="shipping", shipping=12)
    sc.subito_new = [pick, ship]
    assert [i.id for i in sc._subito_for(katana)] == [1]                     # €280: only the pickup fits
    r = sc._pickup_rules(katana, pick, sc._rules(katana))
    assert (r.max_price, r.min_profit) == (300, 40)
    assert sc._pickup_rules(katana, ship, sc._rules(katana)).max_price == 250
    jackson = next(s for s in cfg.searches if s.query == "jackson js32")
    assert sc._pickup_cap(jackson) == 300 and sc._pickup_cap(next(s for s in cfg.searches if s.query == "gibson les paul studio")) == 800
