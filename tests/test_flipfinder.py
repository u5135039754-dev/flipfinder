import json
from pathlib import Path

from flipfinder.analyzer import (Rules, evaluate, find_comparables, is_relevant, market_value,
                                 is_pickup_only, model_tokens, rate, remove_outliers)
from flipfinder.storage import SeenStore
from flipfinder.telegram import format_deal
from flipfinder.vinted import Item, parse_catalog_page, parse_item_page


def item(i, price, size="42", title="Nike Dunk Low", total=None):
    return Item(id=i, title=title, price=price,
                total_price=total if total is not None else round(price * 1.05 + 0.7, 2),
                currency="EUR", url=f"https://www.vinted.it/items/{i}", size=size)


def pool(prices, size="42"):
    return [item(100 + n, p, size) for n, p in enumerate(prices)]


def test_from_api_handles_both_price_formats():
    a = Item.from_api({"id": 1, "title": "x", "price": {"amount": "20.0", "currency_code": "EUR"},
                       "total_item_price": {"amount": "21.70", "currency_code": "EUR"}},
                      "www.vinted.it", 0.7, 5)
    b = Item.from_api({"id": 2, "title": "y", "price": "20.0"}, "www.vinted.it", 0.7, 5)
    assert a.price == 20 and a.total_price == 21.70
    assert b.total_price == 21.70  # estimated fee: 20 + 0.70 + 5%


def test_outliers_removed():
    assert 999 not in remove_outliers([50, 52, 55, 58, 60, 61, 999])


def test_market_value_prefers_same_size():
    p = pool([80] * 10, "42") + pool([30] * 10, "38")
    for n, x in enumerate(p):
        x.id = n
    value, n = market_value(item(999, 40, "42"), p, 8)
    assert value == 80 and n == 10


def test_market_value_only_uses_same_brand():
    p = pool([300] * 10) + pool([100] * 10)
    for n, x in enumerate(p):
        x.id = n
        x.brand = "Fender" if n < 10 else "Harley Benton"
    hb = item(999, 60)
    hb.brand = "harley benton"
    assert market_value(hb, p, 8) == (100, 10)


def test_not_enough_comparables_means_no_deal():
    assert evaluate(item(1, 20), pool([80, 85, 90]), Rules()) is None


def test_good_deal_found():
    deal = evaluate(item(1, 40), pool([80, 82, 85, 88, 90, 79, 84, 86, 81, 83]), Rules())
    assert deal is not None
    assert deal.market_value == 83.5
    assert deal.profit == round(83.5 - deal.item.total_price, 2)
    assert 1 <= deal.rating <= 10


def test_too_good_to_be_true_skipped():
    assert evaluate(item(1, 5), pool([80] * 12), Rules()) is None


def test_overpriced_skipped():
    assert evaluate(item(1, 75), pool([80] * 12), Rules()) is None


def test_excluded_keyword():
    rules = Rules(exclude_keywords=("replica",))
    assert evaluate(item(1, 40, title="Dunk replica"), pool([80] * 12), rules) is None


def test_rating_scale():
    r = Rules()
    assert rate(500, 500, 100, r) == 10
    assert rate(0, 0, 0, r) == 1
    assert rate(20, 50, 15, r) < rate(40, 100, 30, r)


def test_message_format():
    deal = evaluate(item(1, 40), pool([80] * 12), Rules())
    msg = format_deal(deal)
    for part in ("Item price", "Original price", "Possible profit", "Percentage", "/10"):
        assert part in msg


def test_seen_store_roundtrip(tmp_path: Path):
    s = SeenStore(tmp_path / "seen.json")
    s.add(42)
    s.save()
    assert 42 in SeenStore(tmp_path / "seen.json")


def test_parse_catalog_page():
    product = {
        "id": 7, "title": "Fender Strat", "url": "/items/7-fender-strat",
        "price": {"amount": "200.00", "currencyCode": "EUR"},
        "totalItemPrice": {"amount": "210.70", "currencyCode": "EUR"},
        "photos": [{"url": "https://images1.vinted.net/x.webp"}],
        "itemBox": {"firstLine": "Fender", "secondLine": "Ottime"},
    }
    payload = '{"items":{"items":[' + json.dumps({"id": 7, "productItem": product}) + ']}}'
    html = "<script>self.__next_f.push([1," + json.dumps(payload) + "])</script>"
    it = Item.from_page(parse_catalog_page(html)[0], "www.vinted.it", 0.7, 5)
    assert (it.price, it.total_price, it.brand, it.size, it.condition) == (200, 210.70, "Fender", "", "Ottime")
    assert it.url == "https://www.vinted.it/items/7-fender-strat"


# --- Title matching, with real titles from a vinted.it dry run ---

def titled(i, title, price, brand=""):
    x = item(i, price, size="", title=title)
    x.brand = brand
    return x


def test_relevance_drops_other_models_and_junk():
    assert not is_relevant("Gibson Sg", "gibson les paul studio")
    assert not is_relevant("SX guitar", "chitarra acustica yamaha")
    assert not is_relevant("Clases de canto", "chitarra acustica fender")
    assert is_relevant("Gibson Les Paul Studio 2016 HP", "gibson les paul studio")


def test_relevance_normalizes_spelling():
    assert is_relevant("Guitarra eléctrica Epiphone LesPaul Special II", "epiphone les paul special")
    assert is_relevant("Epiphone LP Special", "epiphone les paul special")
    assert is_relevant("Soundbar Sony HT-G700", "sony g700")
    assert is_relevant("Guitare acoustique Takamine", "chitarra acustica takamine")
    assert is_relevant("Chitarra Fender acoustics", "chitarra acustica fender")


def test_model_tokens():
    q = "marshall amplificatore chitarra"
    assert model_tokens("Amplificatore chitarra Marshall MG15 CF", q) == {"mg15", "cf"}
    assert model_tokens("Amplificatore marshall mg15cdr 15W", q) == {"mg15cdr", "mg15"}
    assert model_tokens("Gibson Les Paul Studio 2016 HP usata ottime condizioni",
                        "gibson les paul studio") == {"hp"}
    assert model_tokens("Takamine G220 chitarra acustica come nuova", "chitarra acustica takamine") == {"g220"}
    assert model_tokens("Boss DS-1 distortion", "pedale boss chitarra") == {"ds1", "distortion"}


def test_mg15_only_compared_with_mg15s():
    q = "marshall amplificatore chitarra"
    mg15 = [titled(100 + n, f"Amplificatore chitarra Marshall MG15 {s}", p, "Marshall")
            for n, (s, p) in enumerate([("CF", 70), ("CDR", 75), ("", 80), ("CFX", 85), ("CF", 65),
                                        ("CDR", 78), ("", 72), ("FX", 90)])]
    big = [titled(200 + n, f"Amplificatore chitarra Marshall {m}", p, "Marshall")
           for n, (m, p) in enumerate([("DSL40CR", 450), ("JCM 800 2203", 1400), ("Origin 20C", 350),
                                       ("Code 50", 180), ("DSL20", 400), ("JVM 410", 1200),
                                       ("MG30 GFX", 160), ("AS50D", 300)])]
    sg = titled(300, "Gibson Sg", 600, "Gibson")
    candidate = titled(1, "Amplificatore chitarra Marshall MG15 CF", 70, "Marshall")
    comps, basis = find_comparables(candidate, mg15 + big + [sg], 8, q)
    assert {c.id for c in comps} == {c.id for c in mg15}
    assert "mg15" in basis
    # Worth about the same as other MG15s, so it's no deal, even though the
    # Marshall median overall is far higher
    assert evaluate(candidate, mg15 + big, Rules(), q) is None


def test_no_matching_model_and_wide_prices_means_skip():
    q = "gibson les paul studio"
    p = [titled(100 + n, f"Gibson Les Paul Studio {m}", price, "Gibson")
         for n, (m, price) in enumerate([("Faded", 700), ("Tribute", 800), ("Deluxe", 1300), ("Pro", 1500),
                                         ("Plus", 1900), ("Smartwood", 650), ("50s", 1100), ("Worn", 750),
                                         ("Raw Power", 900)])]
    candidate = titled(1, "Gibson Les Paul Studio Ebony", 500, "Gibson")
    assert find_comparables(candidate, p, 8, q) == ([], "no comparable model")
    assert evaluate(candidate, p, Rules(), q) is None


def test_model_numbers_must_match_exactly():
    q = "boss katana"
    candidate = titled(1, "BOSS Katana-50 MkII Gitarrenverstärker 50W – guter Zustand", 180, "Boss")
    mk2_50 = [titled(100 + n, f"Amplificatore chitarra Boss Katana 50 mkii {n}", 200 + n, "Boss")
              for n in range(8)]
    others = [titled(200, "Ampli guitare Boss Katana 50 Gen 1 (MK1)", 150, "Boss"),
              titled(201, "Amplificatore Boss Katana 100W Mk2, per chitarra elettrica", 300, "Boss"),
              titled(202, "Boss Katana 100 MkII", 320, "Boss"),
              titled(203, "Boss Katana Head MkII + Foot Controller", 260, "Boss")]
    comps, basis = find_comparables(candidate, mk2_50 + others, 8, q)
    assert {c.id for c in comps} == {c.id for c in mk2_50}
    assert basis == "model 50 mk2"


def test_maker_names_are_not_model_tokens():
    tokens = model_tokens("Chitarra Elettrica Squier by Fender Stratocaster (Affinity Series)",
                          "squier affinity stratocaster", "Fender")
    assert "fender" not in tokens and "squier" not in tokens


def test_player_not_valued_like_player_ii():
    q = "fender player stratocaster"
    player = [titled(100 + n, "Fender Player Stratocaster", p, "Fender")
              for n, p in enumerate([500, 520, 540, 560, 580, 600, 520, 550])]
    player_ii = [titled(200 + n, "Fender Player II Modified Stratocaster", 900, "Fender") for n in range(8)]
    candidate = titled(1, "Fender stratocaster player", 400, "Fender")
    comps, _ = find_comparables(candidate, player + player_ii, 8, q)
    assert {c.id for c in comps} == {c.id for c in player}


# --- Shipping and pickup ---

def test_shipping_counts_in_profit_roi_and_rating():
    rules = Rules(resell_costs=10)
    p = pool([200] * 12)
    free = evaluate(item(1, 100), p, rules, shipping=0.0)
    shipped = evaluate(item(1, 100), p, rules, shipping=25.0)
    assert shipped.profit == round(free.profit - 25, 2)
    assert shipped.roi == round(shipped.profit / (item(1, 100).total_price + 25) * 100, 1)
    assert shipped.rating <= free.rating
    # shipping can turn a deal into no deal
    assert evaluate(item(1, 130), p, rules, shipping=0.0) is not None
    assert evaluate(item(1, 130), p, rules, shipping=30.0) is None


def test_estimated_shipping_used_when_unknown_and_none_for_pickup():
    rules = Rules(shipping_cost=20)
    est = evaluate(item(1, 40), pool([120] * 12), rules)
    assert est.shipping == 20 and not est.shipping_known
    pickup = evaluate(item(1, 40), pool([120] * 12), rules, shipping=18.0, pickup_only=True, city="Milano")
    assert pickup.shipping == 0 and pickup.pickup_only
    msg = format_deal(pickup)
    assert "📍 <b>Pickup only</b> · Milano" in msg and "no shipping" in msg


def test_shipping_line_in_message():
    deal = evaluate(item(1, 40), pool([120] * 12), Rules(resell_costs=10), shipping=15.0)
    assert "📦 Shipping + packaging: <b>€25.00</b>" in format_deal(deal)


def test_pickup_only_phrases():
    assert is_pickup_only("Solo ritiro a mano zona Milano")
    assert is_pickup_only("Ritiro a mano solo, no spedizione")
    assert is_pickup_only("Only pickup, too fragile to ship")
    assert is_pickup_only("Nur Abholung")
    # real descriptions that only mention it or are about guitar pickups
    assert not is_pickup_only("Pickup al ponte: Fender Designed alnico humbucking")
    assert not is_pickup_only("Remise en main propre privilégiée pour ne pas abîmer la guitare. "
                              "Possibilité de faire un envoi")
    assert not is_pickup_only("Zona La Spezia possibilità di consegna hand carry gratuita")
    assert not is_pickup_only("Spedizione rapida e imballaggio accurato!")


def test_parse_item_page():
    payload = ('{"shipping":{"type":"shipping","originalPrice":{"amount":"10.49","currencyCode":"EUR"},'
               '"finalPrice":{"amount":"10.49","currencyCode":"EUR"},"isFree":false},'
               '"x":[{"data":{"description":"Solo ritiro a mano"},"exposures":[],"name":"description","section":"content"},'
               '{"data":{"name":"seller","user_info":[{"key":"location","text":"Torino, Italia"}]},'
               '"exposures":[],"name":"user_info_header","section":"sidebar"}],"isShippingAvailable":true}')
    html = "<script>self.__next_f.push([1," + json.dumps(payload) + "])</script>"
    d = parse_item_page(html)
    assert (d.shipping, d.shipping_available, d.description, d.city) == (10.49, True, "Solo ritiro a mano", "Torino, Italia")
