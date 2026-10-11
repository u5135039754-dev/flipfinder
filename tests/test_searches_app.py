"""Searches from the Mini App: topic groups, platforms, must-have words, size, topic max prices, previews."""

from flipfinder import config
from flipfinder.scanner import Scanner
from flipfinder.settings import apply_settings, search_from_spec
from flipfinder.vinted import Item


def item(i, price, title="Boss Katana Mini amp", size="", brand="Boss"):
    return Item(id=i, title=title, price=price, total_price=price, currency="EUR", url=f"https://www.vinted.it/items/{i}",
                brand=brand, size=size)


def cfg_with(settings, tmp_path):
    cfg = config.load()
    cfg.seen_file = tmp_path / "seen.json"
    cfg.subito.enabled = False
    return apply_settings(cfg, settings)


def test_added_searches_take_their_topic_group_and_extras(tmp_path):
    cfg = cfg_with({"added": [
        {"query": "katana mini", "price_from": 30, "price_to": None, "group": "Guitars", "platforms": ["ebay"],
         "must": ["mini"], "exclude": ["cover"], "size": ""},
        {"query": "ipad air", "group": "Electronics", "platforms": []},
        {"query": "ds-1 cheap", "group": "Budget"},
        {"query": "moncler", "group": "Clothes", "clothes": True, "size": "M"}],
        "overrides": {"boss katana": {"platforms": [], "must": ["50"], "exclude": [], "size": ""}}}, tmp_path)
    by = {s.query: s for s in cfg.searches}
    k = by["katana mini"]
    assert k.group == "Guitars" and k.uses("ebay") and not k.uses("subito") and k.uses("vinted")
    assert k.must_words == ["mini"] and "cover" in k.exclude_keywords
    assert not by["ipad air"].uses("ebay") and by["ipad air"].group == "Electronics"
    assert by["ds-1 cheap"].budget
    assert by["moncler"].clothes and by["moncler"].size == "M"
    assert by["boss katana"].must_words == ["50"] and not by["boss katana"].uses("ebay")   # a config.yaml search, changed in the app


def test_must_words_and_size():
    cfg = config.load()
    s = search_from_spec(cfg, {"query": "katana", "group": "Guitars", "must": ["mini"]})
    assert s.wants(item(1, 50)) and not s.wants(item(2, 50, title="Boss Katana 50 MkII"))
    c = search_from_spec(cfg, {"query": "stone island", "group": "Clothes", "size": "M"})
    assert c.wants(item(3, 90, title="Felpa", size="M · Ottime")) and not c.wants(item(4, 90, title="Felpa", size="L"))
    shoe = search_from_spec(cfg, {"query": "margiela gat", "group": "Clothes", "size": "42"})
    assert shoe.wants(item(5, 90, size="42")) and not shoe.wants(item(6, 90, size="43"))


def test_topic_max_prices(tmp_path):
    cfg = cfg_with({"music_rules": {"max_price": 250}, "clothes_rules": {"max_price": 180}, "budget_rules": {"min_roi": 30}}, tmp_path)
    sc = Scanner(cfg)
    katana = next(s for s in cfg.searches if s.query == "boss katana")
    clothes = next(s for s in cfg.searches if s.clothes)
    assert sc._rules(katana).max_price == 250 and sc._price_to(katana) == 250
    assert sc._rules(clothes).max_price == 180 and sc._price_to(clothes) == 180
    assert cfg.budget_rules["min_roi"] == 30


class FakeClient:
    """Vinted stand-in: the same listings for every search page."""
    requests = blocked = 0

    def __init__(self, items):
        self.items = items

    def search(self, query, **kw):
        return list(self.items) if kw.get("page", 1) == 1 else []


def test_preview_counts_listings_and_what_would_pass(tmp_path):
    cfg = cfg_with({}, tmp_path)
    sc = Scanner(cfg)
    market = [item(100 + n, p) for n, p in enumerate([120, 125, 130, 118, 122, 128, 126, 121, 124])]
    sc.client = FakeClient(market + [item(1, 60), item(2, 119, title="Boss Katana Mini cover")])
    s = search_from_spec(cfg, {"query": "boss katana mini", "group": "Guitars", "exclude": ["cover"]})
    r = sc.preview(s)
    assert r["listings"] == 11 and r["pass"] == 1 and r["best"][0]["price"] == 60
