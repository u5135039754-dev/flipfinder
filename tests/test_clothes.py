"""Designer clothes: garment types, sizes, brand + type + size comparables and the fake checks."""

from dataclasses import replace

from flipfinder import config
from flipfinder.analyzer import (FAKE_SELLER, FAKE_TOO_CHEAP, Rules, assess, brand_matches, clothes_kind,
                                 fake_risk, find_clothes_comparables, size_rank)
from flipfinder.settings import apply_settings
from flipfinder.telegram import format_deal
from flipfinder.vinted import Item

RULES = Rules(min_profit=30, min_roi=40, min_rating=1, min_comparables=6, max_roi=400, clothes=True, fake_floor=0.4,
              seller_min_reviews=5, seller_min_stars=4.5, repairs=False,
              fake_words=("replica", "inspired", "simil", "aaa", "1:1", "stile", "tipo"))
GOOD_SELLER = {"source": "vinted", "reviews": 40, "stars": 4.9, "sold": 30}


def it(i, price, title="Felpa con cappuccio Stone Island", size="M", brand="Stone Island", seller=None, desc="", photos=4):
    return Item(id=i, title=title, price=price, total_price=price, currency="EUR", url=f"https://www.vinted.it/items/{i}",
                brand=brand, size=size, seller=seller, description=desc,
                photos=[f"https://img/{i}/{n}.jpg" for n in range(photos)])


def pool():
    hoodies = [it(100 + n, p) for n, p in enumerate([150, 160, 170, 155, 165, 175, 158])]   # M hoodies, median ~160
    other_sizes = [it(200 + n, 300, size="XXL") for n in range(6)]
    jackets = [it(300 + n, 400, title="Giacca Stone Island") for n in range(6)]
    mm6 = [it(400 + n, 40, brand="Stone Island Shadow Project") for n in range(6)]
    fakes = [it(500, 30, title="Felpa cappuccio stile Stone Island"), it(501, 25, title="Felpa replica Stone Island")]
    return hoodies + other_sizes + jackets + mm6 + fakes


def test_garment_types_and_what_is_not_one():
    assert clothes_kind("Felpa con cappuccio CP Company") == "hoodie"
    assert clothes_kind("Maglione Stone Island lana") == "knit"
    assert clothes_kind("Cardigan Acne Studios") == "cardigan"
    assert clothes_kind("Piumino Arc'teryx") == "jacket"
    assert clothes_kind("Overshirt Stone Island") == "jacket"
    assert clothes_kind("Sweatshirt Stussy") == "sweatshirt"
    assert clothes_kind("Sweat sans capuche Stone Island") == "sweatshirt"   # "without hood"
    assert clothes_kind("Felpa senza cappuccio CP Company") == "sweatshirt"
    for title in ("Polo Ralph Lauren", "Gilet Stone Island", "Maglia manica corta Stussy", "Scarpe Margiela", "Borsa Acne"):
        assert clothes_kind(title) is None, title


def test_sizes_and_brands():
    assert [size_rank(s) for s in ("S", "M", "XL / 42 / 14", "50", "2XL")] == [3, 4, 6, 5, 7]
    assert size_rank("Taglia unica") is None
    assert brand_matches("C.P. Company", "c.p. company") and brand_matches("Stüssy", "stussy")
    assert brand_matches("Polo Ralph Lauren", "ralph lauren")
    assert not brand_matches("Carhartt", "carhartt wip")   # plain Carhartt workwear isn't Carhartt WIP


def test_comparables_same_brand_type_and_size_without_fakes():
    comps, basis, kind = find_clothes_comparables(it(1, 80), pool(), RULES, "stone island")
    assert kind == "hoodie" and basis == "Stone Island hoodie, size M"
    assert {c.id for c in comps} == {100, 101, 102, 103, 104, 105, 106}   # no XXL, no jackets, no Shadow Project, no fakes
    # a size with too few listings and no neighbours: no market value rather than a wrong one
    assert find_clothes_comparables(it(2, 80, size="XS"), pool(), RULES, "stone island")[0] == []
    # one size either side when the exact size is short
    near = [it(600 + n, 160, size="L") for n in range(6)]
    comps, basis, _ = find_clothes_comparables(it(3, 80, size="XL"), near, RULES, "stone island")
    assert len(comps) == 6 and basis == "Stone Island hoodie, sizes L-XXL"


def test_a_real_deal_with_fake_risk():
    d = assess(it(1, 85, seller=GOOD_SELLER), pool(), RULES, "stone island", shipping=5)
    assert d and not d.blocked, d and d.blocked
    assert d.market_value == 160 and d.kind == "hoodie" and d.fake_risk == "medium"   # 53% of the market price
    text = format_deal(d)
    assert "Stone Island · hoodie · M" in text
    assert "⚠️ Fake risk: <b>medium</b> 🟡 (53% of the market price)" in text and "inside label, care tag and codes" in text


def test_too_cheap_is_a_fake_not_a_deal():
    d = assess(it(1, 55, seller=GOOD_SELLER), pool(), RULES, "stone island", shipping=5)
    assert any(b.startswith(FAKE_TOO_CHEAP) for b in d.blocked)


def test_seller_trust_and_fake_words():
    few = assess(it(1, 90, seller={**GOOD_SELLER, "reviews": 3}), pool(), RULES, "stone island", shipping=5)
    assert any(b.startswith(FAKE_SELLER) for b in few.blocked)
    low = assess(it(1, 90, seller={**GOOD_SELLER, "stars": 4.2}), pool(), RULES, "stone island", shipping=5)
    assert any("4.5★" in b for b in low.blocked)
    for title in ("Felpa con cappuccio Stone Island replica", "Felpa cappuccio stile Stone Island", "Hoodie Stone Island AAA 1:1"):
        assert assess(it(1, 90, title=title, seller=GOOD_SELLER), pool(), RULES, "stone island", shipping=5) is None, title
    # strong words count in the description too, everyday ones ("stile") only in titles
    assert assess(it(1, 90, seller=GOOD_SELLER, desc="Inspired by Stone Island"), pool(), RULES, "stone island", shipping=5) is None
    assert assess(it(1, 90, seller=GOOD_SELLER, desc="Stile casual, come nuova"), pool(), RULES, "stone island", shipping=5)
    # the listing's brand field has to be the brand
    assert assess(it(1, 90, brand="No brand", seller=GOOD_SELLER), pool(), RULES, "stone island", shipping=5) is None


def test_fake_risk_levels():
    assert fake_risk(it(1, 140, seller=GOOD_SELLER, desc="con scontrino"), 160)[0] == "low"
    level, why = fake_risk(it(1, 75, seller={"reviews": 6, "sold": 2}, photos=1), 160)
    assert level == "high" and "47% of the market price" in why and "1 photo" in why


def test_config_settings_and_scanner_rules(tmp_path):
    cfg = config.load()
    clothes = [s for s in cfg.searches if s.clothes]
    assert [s.query for s in clothes][:3] == ["maison margiela", "acne studios", "stone island"]
    assert all(s.vinted_only and s.group == "Clothes" and s.price_to == 200 for s in clothes)
    assert [s.slot for s in clothes[:4]] == [0, 1, 0, 1]   # half the brands each run
    apply_settings(cfg, {"added": [{"query": "moncler", "clothes": True}], "clothes_rules": {"min_profit": 35}})
    added = next(s for s in cfg.searches if s.query == "moncler")
    assert added.clothes and added.filters["catalog"] == cfg.clothes.catalog
    from flipfinder.scanner import Scanner
    cfg.seen_file = tmp_path / "seen.json"
    cfg.subito.enabled = False
    rules = Scanner(cfg)._rules(added)
    assert rules.clothes and rules.min_profit == 35 and rules.min_roi == 40 and rules.fake_floor == 0.4
    assert "felpa" not in rules.exclude_keywords                    # a sweatshirt here, merch elsewhere
    assert "felpa" in Scanner(cfg)._rules(cfg.searches[0]).exclude_keywords
    assert not rules.repairs
    assert rules.abroad and not Scanner(cfg)._rules(cfg.searches[0]).abroad   # EU listings for clothes only
