import json
import time
import sys
from pathlib import Path

from flipfinder.analyzer import (Rules, assess, evaluate, find_comparables, is_near_miss, is_pickup_only,
                                 is_relevant, market_value, model_tokens, rate, remove_outliers)
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
    s.add("vinted:42")
    s.save()
    assert "vinted:42" in SeenStore(tmp_path / "seen.json")


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
    mk2_50 = [titled(100 + n, f"Amplificatore chitarra Boss Katana 50 mkii {chr(97 + n) * 2}", 200 + n, "Boss")
              for n in range(8)]
    others = [titled(200, "Ampli guitare Boss Katana 50 Gen 1 (MK1)", 150, "Boss"),
              titled(201, "Amplificatore Boss Katana 100W Mk2, per chitarra elettrica", 300, "Boss"),
              titled(202, "Boss Katana 100 MkII", 320, "Boss"),
              titled(203, "Boss Katana Head MkII + Foot Controller", 260, "Boss")]
    comps, basis = find_comparables(candidate, mk2_50 + others, 8, q)
    assert {c.id for c in comps} == {c.id for c in mk2_50}
    assert basis == "model 50 mk2 (Boss)"


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


# --- Health: failure alerts, daily summary, near misses ---

def test_failure_alert_fires_once_at_three_and_recovery_reports_streak(tmp_path: Path):
    from flipfinder.health import RunStats
    s = RunStats(tmp_path / "stats.json")
    assert [s.record_failure() for _ in range(5)] == [False, False, True, False, False]
    assert s.record_success() == 5
    assert s.record_success() == 0
    assert not s.record_failure() and s.record_success() == 0   # a single failure isn't news


def test_summary_due_once_a_day_after_9_rome(tmp_path: Path):
    from datetime import datetime, timezone
    from flipfinder.health import RunStats
    s = RunStats(tmp_path / "stats.json")
    summer_0859 = datetime(2026, 7, 1, 6, 59, tzinfo=timezone.utc)   # 08:59 in Rome (CEST)
    summer_0900 = datetime(2026, 7, 1, 7, 0, tzinfo=timezone.utc)
    winter_0800utc = datetime(2026, 12, 1, 8, 0, tzinfo=timezone.utc)  # 09:00 in Rome (CET)
    assert not s.summary_due(summer_0859)
    assert s.summary_due(summer_0900)
    s.mark_summary_sent(summer_0900)
    assert not s.summary_due(datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc))
    assert s.summary_due(winter_0800utc)


def _summary_stats(tmp_path, miss_title="Squier Affinity Strat <Nero & Bianco>"):
    from datetime import datetime, timezone
    from flipfinder.health import RunStats, miss_record
    rules = Rules(min_profit=25, min_roi=30, min_rating=3)
    p = pool([160] * 12)
    close = assess(titled(2, miss_title, 125, "Fender"), p, rules, shipping=0.0)
    s = RunStats(tmp_path / "stats.json")
    s.data["since"] = datetime(2026, 10, 4, 7, 0, tzinfo=timezone.utc).timestamp()
    for _ in range(270):
        s.record_run(7, 0, None)
    s.record_run(-50, 2, miss_record("squier affinity stratocaster", close))   # totals: 1,840 checked, 2 deals
    return s, close


def test_summary_format(tmp_path: Path):
    from datetime import datetime, timezone
    from flipfinder.health import pct
    s, close = _summary_stats(tmp_path)
    text = s.summary_text(Rules(min_profit=25, min_roi=30, min_rating=3),
                          datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc))
    lines = text.split("\n")
    assert lines[0] == "📊 <b>flipFinder · last 24h</b>"
    assert lines[1] == "Runs: 271 · Listings checked: 1,840 · Deals: 2"
    # HTML link with the title escaped, never a Markdown-style [text](url)
    assert lines[2] == ('Closest miss: <a href="https://www.vinted.it/items/2">'
                        'Squier Affinity Strat &lt;Nero &amp; Bianco&gt;</a>')
    assert "](" not in text
    assert close.blocked == ["ROI < 30%"]
    assert lines[3] == (f"€{close.item.total_price:.0f} → worth €160 · +€{close.profit:.0f} "
                        f"({pct(close.roi)}) · blocked: ROI &lt; 30%")
    assert len(lines) == 4   # 271 of the expected 288 runs is fine, no warning


def test_summary_uses_current_rules_and_warns_on_missing_runs(tmp_path: Path):
    from datetime import datetime, timezone
    s, _ = _summary_stats(tmp_path)
    s.data["runs"] = 40
    # with looser rules the stored miss would pass, so it isn't a miss any more
    text = s.summary_text(Rules(min_profit=25, min_roi=20, min_rating=3),
                          datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc))
    assert "No near misses" in text and "Closest miss" not in text
    assert "⚠️ Expected ~288 runs (12/hour)" in text


def test_negative_profit_is_never_a_near_miss(tmp_path: Path):
    from flipfinder.health import RunStats, miss_record
    losing = assess(item(1, 99), pool([100] * 12), Rules(min_profit=25, min_roi=30))
    s = RunStats(tmp_path / "stats.json")
    s.record_run(10, 0, miss_record("x", losing))
    assert s.data["best_miss"] is None
    assert "No near misses" in s.summary_text(Rules())


def test_assess_reports_blocking_rules():
    d = assess(item(1, 80), pool([100] * 12), Rules(min_profit=25, min_roi=30))
    assert d.blocked == ["profit < €25", "ROI < 30%", "rating < 6"]
    assert evaluate(item(1, 80), pool([100] * 12), Rules(min_profit=25, min_roi=30)) is None
    too_cheap = assess(item(1, 5), pool([100] * 12), Rules())
    assert not is_near_miss(too_cheap)   # blocked by max_roi: suspicious, not "almost"
    losing = assess(item(1, 99), pool([100] * 12), Rules(min_profit=25, min_roi=30))
    assert losing.profit < 0 and not is_near_miss(losing)


# --- eBay ---

EBAY_SUMMARY = {
    "itemId": "v1|123456789012|0", "legacyItemId": "123456789012",
    "title": "Boss DS-1 Distortion pedale", "price": {"value": "35.00", "currency": "EUR"},
    "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": "6.90", "currency": "EUR"}},
                        {"shippingCostType": "FIXED", "shippingCost": {"value": "4.50", "currency": "EUR"}}],
    "itemLocation": {"city": "Torino", "country": "IT"}, "condition": "Usato",
    "itemWebUrl": "https://www.ebay.it/itm/123456789012", "image": {"imageUrl": "https://i.ebayimg.com/x.jpg"},
    "buyingOptions": ["FIXED_PRICE"],
}


def test_ebay_item_parsing():
    from flipfinder.ebay import item_from_ebay
    it = item_from_ebay(EBAY_SUMMARY)
    assert (it.id, it.price, it.total_price, it.shipping, it.source, it.location) == \
        (123456789012, 35.0, 35.0, 4.5, "ebay", "Torino, IT")
    assert it.key == "ebay:123456789012"
    no_ship = item_from_ebay({**EBAY_SUMMARY, "shippingOptions": [{"shippingCostType": "CALCULATED"}]})
    assert no_ship.shipping is None


def test_ebay_filter_buy_it_now_and_location():
    from flipfinder.ebay import EbayClient
    it = EbayClient("id", "secret")._filter(25, 90)
    assert it == "buyingOptions:{FIXED_PRICE},deliveryCountry:IT,itemLocationCountry:IT,price:[25..90],priceCurrency:EUR"
    eu = EbayClient("id", "secret", item_location="EU")._filter(None, 200)
    assert "itemLocationRegion:EUROPEAN_UNION" in eu and "price:[..200]" in eu


def test_vinted_and_ebay_compared_together_by_model():
    from flipfinder.ebay import item_from_ebay
    q = "boss"
    vinted = [titled(100 + n, f"Boss DS-1 distortion {chr(97 + n) * 2}", p, "Boss") for n, p in enumerate([40, 42, 45, 44, 41])]
    ebay = [item_from_ebay({**EBAY_SUMMARY, "legacyItemId": str(900 + n), "title": f"Pedale Boss DS-1 {chr(97 + n) * 2}",
                            "price": {"value": str(p), "currency": "EUR"}}) for n, p in enumerate([55, 58, 60, 52, 57])]
    other = [titled(300 + n, "Boss RC-30 loop station", 150, "Boss") for n in range(5)]
    candidate = titled(1, "Boss DS-1", 15, "Boss")
    comps, basis = find_comparables(candidate, vinted + ebay + other, 8, q)
    assert {c.key for c in comps} == {c.key for c in vinted + ebay}   # eBay has no brand but same model
    d = assess(candidate, vinted + ebay + other, Rules(min_comparables=8), q, shipping=5.0)
    assert d.by_platform == {"ebay": (57.0, 5), "vinted": (42.0, 5)}
    assert d.resell_on == "ebay" and d.sell_fee == 0
    msg = format_deal(d)
    assert "🛒 <b>Vinted</b>" in msg and "Cheaper to buy on Vinted, sells for more on eBay, no seller fee" in msg


def test_seller_fee_changes_best_resale_and_profit():
    from flipfinder.analyzer import best_resale
    by = {"vinted": (100.0, 6), "ebay": (105.0, 6)}
    assert best_resale(100, by, {}, "vinted") == ("ebay", 0.0)
    where, fee = best_resale(100, by, {"ebay": (10.0, 0.35)}, "vinted")   # 105 - 10.85 < 100
    assert where == "vinted" and fee == 0.0
    where, fee = best_resale(100, {"ebay": (105.0, 6)}, {"ebay": (10.0, 0.35)}, "vinted")
    assert where == "ebay" and round(fee, 2) == 10.35


def test_seen_store_migrates_old_vinted_ids(tmp_path: Path):
    (tmp_path / "seen.json").write_text(json.dumps({"42": 1e12, "ebay:7": 1e12}))
    s = SeenStore(tmp_path / "seen.json")
    assert "vinted:42" in s and "ebay:7" in s and s.has_platform("ebay")


def test_ebay_runs_every_15_minutes(tmp_path: Path):
    import time
    from flipfinder.scanner import EbayState
    st = EbayState(tmp_path / "ebay.json")
    assert st.due(15)
    st.record(17, scanned=True)
    assert not st.due(15) and st.calls_today == 17
    st.data["last_scan"] = time.time() - 15 * 60
    assert st.due(15)
    st.record(3, scanned=False)
    assert st.calls_today == 20


def test_summary_roi_keeps_decimal_near_threshold(tmp_path: Path):
    from flipfinder.health import RunStats
    s = RunStats(tmp_path / "stats.json")
    s.data["best_miss"] = {"title": "Squier CV 60s", "url": "https://www.vinted.it/items/1", "currency": "EUR",
                           "pay": 262.09, "value": 350.0, "profit": 77.91, "roi": 29.7, "rating": 5}
    assert "+€78 (29.7%) · blocked: ROI &lt; 30%" in s.summary_text(Rules(min_roi=30, min_rating=5))


# --- Electronics matching ---

def test_storage_battery_and_generation_normalized():
    from flipfinder.analyzer import model_numbers
    nums = lambda t, q: model_numbers(model_tokens(t, q))   # noqa: E731
    assert nums("iPhone 13 Pro 128 GB batteria 87% grafite", "iphone 13 pro") == {"128gb"}
    assert nums("iPhone 13 Pro 128GB salute batteria 100%", "iphone 13 pro") == {"128gb"}
    assert nums("MacBook Air M1 8GB 256 GB", "macbook air m1") == {"8gb", "256gb"}
    assert nums("Apple Watch Serie 7 45 mm GPS + Cellular", "apple watch series 7") == {"45mm", "cellular"}
    for title in ("AirPods Pro 2ª generazione", "AirPods Pro (2nd Gen)", "Airpods pro seconda generazione",
                  "AirPods Pro gen 2", "AirPods Pro 2"):
        assert is_relevant(title, "airpods pro 2"), title
    assert not is_relevant("AirPods Pro", "airpods pro 2")
    assert is_relevant("iPad 9a generazione 64GB", "ipad 9") and is_relevant("iPad 10th gen", "ipad 10")


def test_suffix_models_never_compared_with_base_model():
    q = "iphone 13"
    base = [titled(100 + n, "iPhone 13 128GB", p, "Apple") for n, p in enumerate([200, 210, 220, 230, 205, 215, 225, 235])]
    pro = [titled(200 + n, "iPhone 13 Pro 128GB", 320, "Apple") for n in range(8)]
    big = [titled(300 + n, "iPhone 13 256GB", 280, "Apple") for n in range(8)]
    comps, basis = find_comparables(titled(1, "iPhone 13 128 GB batteria 89%", 150, "Apple"), base + pro + big, 8, q)
    assert {c.id for c in comps} == {c.id for c in base} and basis == "model 128gb (Apple)"
    comps, _ = find_comparables(titled(2, "iPhone 13 Pro 128GB", 250, "Apple"), base + pro + big, 8, q)
    assert {c.id for c in comps} == {c.id for c in pro}


def test_graphics_cards_compared_across_brands_but_not_ti():
    q = "rtx 3060"
    msi = [titled(100 + n, "MSI RTX 3060 12GB Ventus", 250, "MSI") for n in range(4)]
    asus = [titled(200 + n, "ASUS Dual RTX 3060 12GB", 260, "ASUS") for n in range(4)]
    ti = [titled(300 + n, "Gigabyte RTX 3060 Ti 8GB", 300, "Gigabyte") for n in range(8)]
    cand = titled(1, "Zotac RTX 3060 12GB", 180, "Zotac")
    assert find_comparables(cand, msi + asus + ti, 8, q, match_brand=True)[0] == []
    comps, _ = find_comparables(cand, msi + asus + ti, 8, q, match_brand=False)
    assert {c.id for c in comps} == {c.id for c in msi + asus}


def test_config_shared_excludes_and_per_search_max_roi(tmp_path: Path):
    from flipfinder import config as config_mod
    (tmp_path / "c.yaml").write_text("""
searches:
  - query: iphone 13
    exclude_keywords: &electronics [cover, icloud, "1:1"]
  - query: airpods 3
    exclude_keywords: [*electronics, pro]
    max_roi: 60
    match_brand: false
""", encoding="utf-8")
    c = config_mod.load(tmp_path / "c.yaml")
    assert c.searches[0].exclude_keywords == ["cover", "icloud", "1:1"]
    assert c.searches[1].exclude_keywords == ["cover", "icloud", "1:1", "pro"]
    assert c.searches[1].max_roi == 60 and c.searches[1].match_brand is False
    assert is_excluded_title("AirPods 3 replica 1:1", c.searches[0].exclude_keywords)


def is_excluded_title(title, keywords):
    from flipfinder.analyzer import is_excluded
    return is_excluded(item(1, 10, title=title), keywords)


def test_more_title_formats():
    from flipfinder.analyzer import model_numbers
    nums = lambda t, q: model_numbers(model_tokens(t, q))   # noqa: E731
    assert nums("iPhone 13 Pro 128 Go Bleu", "iphone 13 pro") == {"128gb"}
    assert nums("iPhone 13 blanc 128 G", "iphone 13") == {"128gb"}
    assert nums("iPhone 12 5G 64GB", "iphone 12") == {"5g", "64gb"}   # network, not storage
    assert not is_relevant("Ipad 10.2 (2021) 9a generazione", "ipad 10")
    assert not is_relevant("Tablette iPad 10 pouces 8génération", "ipad 10")
    assert is_relevant("Ipad 10.2 (2021) 9a generazione", "ipad 9")


def test_fallback_never_contradicts_model_numbers():
    q = "iphone 13"
    others = [titled(100 + n, "iPhone 13 256GB", 260, "Apple") for n in range(10)]
    unknown = [titled(200 + n, "iPhone 13 ottimo", p, "Apple") for n, p in enumerate([200, 205, 210, 215, 208, 212, 202, 207])]
    cand = titled(1, "iPhone 13 128GB", 150, "Apple")
    comps, basis = find_comparables(cand, others + unknown, 8, q)
    assert {c.id for c in comps} == {c.id for c in unknown} and basis.startswith("whole pool")
    assert find_comparables(cand, others, 8, q) == ([], "no comparable model")


class FakeVinted:
    """Pool listings at 300, newest at 100-104: every new listing looks like a deal."""

    def __init__(self):
        self.detail_calls = 0
        self.blocked = 0
        self.statuses = {}          # item id -> what its page says (default: still for sale)

    def item_status(self, item_id):
        return self.statuses.get(item_id, "active")

    def search(self, query, order="newest_first", page=1, **kw):
        if order == "relevance":
            return [titled(abs(hash((query, n))) % 10**9, f"{query} 128GB", 300, "X") for n in range(10)]
        return [titled(abs(hash((query, "new", n))) % 10**9, f"{query} 128GB", 100 + n, "X") for n in range(5)]

    def details(self, item):
        from flipfinder.vinted import ItemDetails
        self.detail_calls += 1
        return ItemDetails(shipping=5.0)


def _fake_scanner(tmp_path, n_searches, known_keys):
    from flipfinder import scanner as sc_mod
    from flipfinder.config import Search
    seen = tmp_path / "seen.json"
    seen.write_text(json.dumps({"vinted:1": 1e12}))
    cfg = type("C", (), {})()
    cfg.rules, cfg.seen_file, cfg.pool_refresh_minutes, cfg.comparable_pages = Rules(), seen, 60, 1
    cfg.stagger = False
    cfg.searches = [Search(f"thing {n}") for n in range(n_searches)]
    cfg.ebay = type("E", (), {"enabled": False})()
    (tmp_path / "searches.json").write_text(json.dumps(known_keys(cfg.searches)))
    s = sc_mod.Scanner.__new__(sc_mod.Scanner)
    s.cfg, s.client, s.ebay = cfg, FakeVinted(), None
    s.seen, s.first_run = SeenStore(seen), False
    s.pools = sc_mod.PoolCache(tmp_path / "pools.json", 60)
    s.ebay_state = sc_mod.EbayState(tmp_path / "ebay.json")
    s.known = sc_mod.KnownSearches(tmp_path / "searches.json", s.pools)
    s.ebay_due = s.first_ebay = False
    s.subito, s.subito_new, s.first_subito, s.subito_pools = None, [], False, 0
    s.sold, s.notices = sc_mod.SoldTracker(tmp_path / "sold.json"), []
    return s


def test_new_searches_send_only_their_best_3_current_deals(tmp_path: Path):
    from flipfinder import scanner as sc_mod
    s = _fake_scanner(tmp_path, 6, lambda searches: [])          # all 6 searches are new
    deals = s.scan()
    assert len(deals) == sc_mod.SEED_ALERTS == 3                 # 30 look like deals, best 3 sent
    assert s.client.detail_calls <= 2 * sc_mod.SEED_ALERTS       # real shipping only for the top few
    assert all(d.shipping == 5.0 for d in deals)
    assert s.checked == 0 and len(s.seen.data) == 1 + 6 * 5      # the whole backlog is remembered
    assert s.scan() == []                                        # next run: nothing new
    assert s.pool_rebuilds == 0                                  # pools were built on the first run


def test_pool_rebuilds_are_capped_for_known_searches(tmp_path: Path):
    from flipfinder import scanner as sc_mod
    from flipfinder.scanner import _key
    s = _fake_scanner(tmp_path, 6, lambda searches: [_key(x) for x in searches])   # known, no pools
    s.scan()
    assert s.pool_rebuilds == sc_mod.MAX_POOL_REBUILDS            # only 4 of 6 pools built
    assert s.checked == 4 * 5                                     # the other 2 searches wait, unseen
    s.scan()
    assert s.pool_rebuilds == 2 and s.checked == 2 * 5


def test_known_searches_bootstrap_from_existing_pools(tmp_path: Path):
    from flipfinder.config import Search
    from flipfinder.scanner import KnownSearches, PoolCache, _key
    pools = PoolCache(tmp_path / "pools.json", 60)
    running, added = Search("boss katana"), Search("iphone 13")
    pools.put(_key(running), [])
    pools.put("ebay " + _key(added), [])          # an eBay pool doesn't make a search known
    known = KnownSearches(tmp_path / "searches.json", pools)   # no file yet: first deploy
    assert known.has(running) and not known.has(added)


def test_curly_apostrophe_excluded():
    from flipfinder.analyzer import is_excluded
    assert is_excluded(item(1, 100, title="iPhone 12 Pro qui ne s’allume plus"), ["ne s'allume"])


def test_ebay_schedule_defaults_and_separate_pool_age(tmp_path: Path):
    import time
    from flipfinder import config as config_mod
    from flipfinder.scanner import PoolCache
    (tmp_path / "c.yaml").write_text("searches: [{query: boss}]\n", encoding="utf-8")
    c = config_mod.load(tmp_path / "c.yaml")
    assert (c.ebay.interval_minutes, c.ebay.pool_refresh_minutes) == (20, 180)
    pools = PoolCache(tmp_path / "pools.json", 60)
    pools.put("ebay x", [])
    pools.data["ebay x"]["ts"] = time.time() - 2 * 3600          # 2 hours old
    assert pools.get("ebay x") is None                            # too old for Vinted's 60 min
    assert pools.get("ebay x", max_age_min=180) == []             # fine for eBay's 3 h


# --- Subito ---

AREA = {"region": 1, "province": 1, "center": (44.5, 11.3)}   # made up: the real area is private


def subito_ad(n, subject, price, town="Centro", lat=44.5005, lon=11.3008, ships=False, ship_cost=None,
              body="", category=("39", "Strumenti Musicali")):
    features = [{"uri": "/price", "values": [{"key": str(price), "value": f"{price} €"}]},
                {"uri": "/item_condition", "values": [{"key": "30", "value": "Ottimo - poco usato e ben conservato"}]},
                {"uri": "/item_shippable", "values": [{"key": "1" if ships else "0", "value": "Sì" if ships else "No"}]}]
    if ship_cost is not None:
        features.append({"uri": "/item_shipping_cost", "values": [{"key": str(ship_cost), "value": f"{ship_cost} €"}]})
    return {"urn": f"id:ad:abc-{n}:list:{660000000 + n}", "subject": subject, "body": body,
            "category": {"key": category[0], "value": category[1]},
            "geo": {"town": {"value": town, "lat": lat, "lon": lon}},
            "urls": {"default": f"https://www.subito.it/strumenti-musicali/x-{660000000 + n}.htm"},
            "features": features, "images": []}


def test_subito_parsing_area_delivery_and_negotiable():
    from flipfinder.subito import SubitoClient, item_from_subito
    c = SubitoClient(**AREA, town_travel_costs={"Sudbury": 8, "Westby": 8})
    it = item_from_subito(subito_ad(1, "Fender Stratocaster Player", 450, body="Prezzo trattabile"))
    assert (it.id, it.price, it.source, it.condition, it.negotiable, it.key) ==         (660000001, 450.0, "subito", "Ottimo", True, "subito:660000001")
    near = item_from_subito(subito_ad(2, "x", 10, "Nordville", 44.5745, 11.3))
    far = item_from_subito(subito_ad(3, "x", 10, "Sudbury", 44.315, 11.3))
    outside = item_from_subito(subito_ad(4, "x", 10, "Lontano", 44.95, 11.3))
    assert c.in_area(near) and c.in_area(far) and not c.in_area(outside)
    assert 7 < near.distance_km < 9 and 19 < far.distance_km < 22 and outside.distance_km > 45
    assert (c.delivery(near).shipping, near.delivery) == (5.0, "pickup")
    assert c.delivery(far).shipping == 8.0                       # town override
    cheap_ship = item_from_subito(subito_ad(5, "x", 10, "Sudbury", 44.315, 11.3, ships=True, ship_cost=6))
    c.in_area(cheap_ship)
    assert (c.delivery(cheap_ship).shipping, cheap_ship.delivery) == (6.0, "shipping")   # cheaper than €8 travel
    for body in ("Non trattabile", "Prezzo non è trattabile", "prezzo fisso, ritiro a mano", "Non tratto"):
        assert not item_from_subito(subito_ad(6, "Boss DS-1", 40, body=body)).negotiable, body
    assert item_from_subito(subito_ad(6, "Boss DS-1", 40, body="45€ tratt.")).negotiable


def test_subito_alert_lines():
    from flipfinder.subito import SubitoClient, item_from_subito
    c = SubitoClient(**AREA)
    it = item_from_subito(subito_ad(7, "Boss DS-1 distortion", 20, "Nordville", 44.5745, 11.3, body="trattabile"))
    c.in_area(it); c.delivery(it)
    p = pool([60] * 12)
    for x in p:
        x.title = "Boss DS-1 distortion"
    d = assess(it, p, Rules(resell_costs=1), "boss")
    msg = format_deal(d)
    assert "🛒 <b>Subito</b> · Nordville (8 km)" in msg and "💬 Negotiable" in msg
    assert "🚗 Pickup + packaging: <b>€6.00</b> (travel to Nordville)" in msg
    assert d.shipping == 5.0 and d.profit == round(60 - 20 - 5 - 1, 2)


def test_first_subito_pass_sends_only_best_3(tmp_path: Path):
    from flipfinder import scanner as sc_mod
    from flipfinder.scanner import _key
    from flipfinder.subito import item_from_subito
    s = _fake_scanner(tmp_path, 2, lambda searches: [_key(x) for x in searches])
    s.pools.put(_key(s.cfg.searches[0]), [titled(900 + n, "thing 0 128GB", 300, "X") for n in range(10)])
    s.pools.put(_key(s.cfg.searches[1]), [titled(950 + n, "thing 1 128GB", 300, "X") for n in range(10)])
    local = [item_from_subito(subito_ad(10 + n, f"thing {n % 2} 128GB", 100 + n)) for n in range(8)]
    for i in local:
        i.shipping, i.delivery, i.distance_km = 5.0, "pickup", 0.0

    class FakeSubito:
        calls = 0
        def newest_in_area(self, cat):
            return local
        def pool(self, *a, **kw):
            return []
    s.subito = FakeSubito()
    s.cfg.subito = type("SB", (), {"default_category": 39, "pool_refresh_minutes": 180, "radius_km": 30})()
    deals = s.scan()
    subito_deals = [d for d in deals if d.item.source == "subito"]
    assert len(subito_deals) == sc_mod.SEED_ALERTS                    # 8 qualify, best 3 sent
    assert all(f"subito:{i.id}" in s.seen for i in local)              # the rest remembered
    assert s.scan() == [] or all(d.item.source != "subito" for d in s.scan())


def test_ebay_category_from_vinted_catalog_or_config():
    from flipfinder.config import Search
    from flipfinder.scanner import Scanner
    s = Scanner.__new__(Scanner)
    s.cfg = type("C", (), {"ebay": type("E", (), {"default_category": 3858})()})()
    assert s._ebay_category(Search("iphone 13", filters={"catalog": [3661]})) == 9355
    assert s._ebay_category(Search("rtx 3060", filters={"catalog": [3602]})) == 27386
    assert s._ebay_category(Search("fender player stratocaster")) == 3858
    assert s._ebay_category(Search("soundbar sony", ebay_category=14969)) == 14969


def test_subito_skips_damaged_condition():
    from flipfinder.subito import SubitoClient
    c = SubitoClient(**AREA)
    ok, damaged = subito_ad(20, "iPhone 13 128GB", 200), subito_ad(21, "iPhone 13 128GB", 160)
    damaged["features"][1]["values"][0]["value"] = "Danneggiato - non funzionante o con parti rotte"
    c._get = lambda params: [ok, damaged]
    assert [i.id for i in c.newest_in_area(12)] == [660000020]


# --- Budget mode ---

def test_budget_total_cost_and_rules():
    rules = Rules(min_profit=12, min_roi=35, max_roi=150, max_cost=72, budget=True)
    p = pool([110] * 12)
    ok = assess(item(1, 60), p, rules, shipping=5.0)            # 63.70 + 5 = 68.70 total
    assert ok.cost == 68.7 and not ok.blocked and ok.budget
    over = assess(item(2, 66), p, rules, shipping=5.0)          # 70 + 5 = 74.0 > 72
    assert "cost > €72 budget" in over.blocked
    from flipfinder.analyzer import is_near_miss
    assert not is_near_miss(over)                               # over budget isn't "almost"
    fake = assess(item(3, 20), p, rules, shipping=5.0)          # far too cheap
    assert any(b.startswith("ROI >") for b in fake.blocked)
    assert round(ok.per_euro, 3) == round(ok.profit / ok.cost, 3)


def test_camera_missing_battery_adds_cost():
    from flipfinder.analyzer import MISSING_PART
    for t in ("Canon IXUS 185 senza batteria", "Sony Cyber-shot no charger", "Nikon Coolpix S3700 batteria non inclusa",
              "Canon PowerShot sans chargeur"):
        assert MISSING_PART.search(t), t
    assert not MISSING_PART.search("Canon IXUS 185 con batteria e caricatore")
    rules = Rules(min_profit=12, min_roi=35, max_roi=150, max_cost=72, missing_part_cost=15)
    p = [titled(100 + n, "Canon IXUS 185", 80, "Canon") for n in range(12)]
    full = assess(titled(1, "Canon IXUS 185", 30, "Canon"), p, rules, shipping=5.0)
    bare = assess(titled(2, "Canon IXUS 185 senza batteria", 30, "Canon"), p, rules, shipping=5.0)
    assert bare.missing_part == 15 and bare.cost == full.cost + 15 and bare.profit == round(full.profit - 15, 2)
    assert "🔋 No battery/charger: +€15.00" in format_deal(bare)


def test_budget_config_and_alert_lines(tmp_path: Path):
    from flipfinder import config as config_mod
    (tmp_path / "c.yaml").write_text("""
budget: 60
budget_rules: {min_profit: 12}
searches:
  - query: boss ds 1
    budget: true
    check: ask for a short video of it working
    every_minutes: 10
  - query: boss katana
""", encoding="utf-8")
    c = config_mod.load(tmp_path / "c.yaml")
    b, n = c.searches
    assert (b.budget, b.price_to, b.every_minutes, b.check) == (True, 60, 10, "ask for a short video of it working")
    assert (n.budget, n.price_to) == (False, None)
    assert c.budget_rules == {"min_profit": 12, "min_roi": 35, "max_roi": 150}
    d = assess(titled(1, "Boss DS-1", 25, "Boss"), [titled(10 + i, "Boss DS-1", 55, "Boss") for i in range(12)],
               Rules(min_profit=12, min_roi=35, max_roi=150, max_cost=60, budget=True,
                     check="ask for a short video of it working"), "boss ds 1", shipping=5.0)
    msg = format_deal(d)
    assert "🎯 Budget: €" in msg and "profit per € spent" in msg
    assert "🔍 Before buying: ask for a short video of it working" in msg


def test_budget_searches_run_first_and_every_n_minutes(tmp_path: Path):
    from flipfinder.config import Search
    from flipfinder.scanner import _sid
    s = _fake_scanner(tmp_path, 0, lambda searches: [])
    normal, budget = Search("thing 0"), Search("thing 1", budget=True, every_minutes=10)
    s.cfg.searches = [normal, budget]
    s.known.add(normal); s.known.add(budget)
    s.known.last[_sid(budget)] = 0       # due
    s.cfg.budget, s.cfg.budget_rules = 72, {"min_profit": 12, "min_roi": 35, "max_roi": 150}
    order = []
    s.scan_search = lambda x: order.append(x.query) or []
    s.scan()
    assert order == ["thing 1", "thing 0"]                      # budget first
    s.known.add(budget)                                          # just ran
    order.clear(); s.scan()
    assert order == ["thing 0"]                                  # not due again for 10 min


def test_suffix_alone_is_not_a_model_and_editions_are_separate():
    q = "tc electronic"
    others = [titled(100 + n, f"TC Electronic {m} mini", p, "TC Electronic")
              for n, (m, p) in enumerate([("Skysurfer", 58), ("Hall of Fame", 69), ("Flashback", 79), ("Mimiq", 107),
                                          ("Flashback X4", 120), ("Ditto", 55), ("Spark", 40), ("Hall of Fame", 72)])]
    spark = titled(1, "Spark mini boost TC Electronic", 36, "TC Electronic")
    assert find_comparables(spark, others, 8, q)[0] == []        # not valued against other "mini" pedals
    q = "dualsense"
    plain = [titled(200 + n, "Controller DualSense PS5 nero", p, "Sony") for n, p in enumerate([40, 42, 45, 43, 44, 41, 46, 40])]
    special = [titled(300 + n, "DualSense PS5 Limited Edition GTA", 160, "Sony") for n in range(8)]
    comps, _ = find_comparables(titled(1, "DualSense PS5 bianco", 25, "Sony"), plain + special, 8, q)
    assert {c.id for c in comps} == {c.id for c in plain}


def test_ebay_skipped_for_the_day_at_the_call_cap(tmp_path: Path):
    s = _fake_scanner(tmp_path, 0, lambda searches: [])
    s.ebay = type("FakeEbay", (), {"calls": 0})()       # "configured"
    s.cfg.ebay = type("E", (), {"interval_minutes": 20, "max_calls_per_day": 4500})()
    s.cfg.budget, s.cfg.budget_rules = 72, {"min_profit": 12, "min_roi": 35, "max_roi": 150}
    s.ebay_state.record(4500, scanned=False)
    s.scan()
    assert s.ebay_due is False


# --- Settings from the Worker (changed in Telegram) ---


def cmd_cfg(tmp_path):
    from flipfinder import config as config_mod
    (tmp_path / "c.yaml").write_text("""
rules: {min_profit: 25, min_roi: 30, min_rating: 5, max_roi: 120}
budget: 72
searches:
  - query: boss katana
    price_from: 80
    price_to: 300
  - query: iphone 13
    price_from: 140
    price_to: 250
    filters: {catalog: [3661]}
  - query: boss ds 1
    budget: true
    price_from: 15
""", encoding="utf-8")
    return config_mod.load(tmp_path / "c.yaml")


def test_telegram_settings_apply_over_config(tmp_path: Path):
    from flipfinder.settings import apply_settings
    cfg = apply_settings(cmd_cfg(tmp_path), {
        "rules": {"min_roi": 25}, "budget": 60, "removed": ["boss katana"], "disabled": ["iphone 13"],
        "added": [{"query": "zoom g1x four", "price_from": 20, "price_to": 60}],
        "prices": {"iphone 13": [150, 240]}})
    assert [s.query for s in cfg.searches] == ["iphone 13", "boss ds 1", "zoom g1x four"]
    assert [s.enabled for s in cfg.searches] == [False, True, True]
    assert cfg.rules.min_roi == 25 and cfg.budget == 60
    assert (cfg.searches[0].price_from, cfg.searches[0].price_to) == (150, 240)
    assert next(s for s in cfg.searches if s.query == "boss ds 1").price_to == 60   # budget caps budget searches


def test_catalog_for_the_worker_is_config_before_telegram_changes(tmp_path: Path):
    from flipfinder.cloud import catalog
    c = catalog(cmd_cfg(tmp_path))
    assert [(s["query"], s["group"]) for s in c["searches"]] == [
        ("boss katana", "Amps"), ("iphone 13", "Electronics"), ("boss ds 1", "Budget")]
    assert c["searches"][2]["price_to"] == 72 and c["searches"][2]["price_to_is_budget"]
    assert c["rules"]["min_profit"] == 25 and c["budget"] == 72
    json.dumps(c)   # sent as JSON


def test_home_area_comes_from_the_worker_and_subito_is_off_without_it(tmp_path: Path):
    from flipfinder.settings import apply_area
    (tmp_path / "c.yaml").write_text("subito: {enabled: true}" + chr(10) + "searches: [{query: boss katana}]" + chr(10), encoding="utf-8")
    from flipfinder import config as config_mod
    cfg = config_mod.load(tmp_path / "c.yaml")
    assert cfg.subito.center is None
    apply_area(cfg, {**AREA, "radius_km": 25, "travel_cost": 6, "town_travel_costs": {"Sudbury": 9}})
    assert cfg.subito.enabled and cfg.subito.center == (44.5, 11.3) and cfg.subito.radius_km == 25
    assert cfg.subito.town_travel_costs == {"Sudbury": 9.0}
    cfg = config_mod.load(tmp_path / "c.yaml")
    apply_area(cfg, None)                        # Worker unreachable: no area, no Subito
    assert not cfg.subito.enabled


def test_config_yaml_has_no_home_area():
    import yaml
    sb = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))["subito"]
    assert not {"center", "region", "province", "town_travel_costs"} & set(sb)


def test_price_change_keeps_search_known(tmp_path: Path):
    from flipfinder.config import Search
    from flipfinder.scanner import KnownSearches, PoolCache, _key
    s = Search("boss katana", 80, 300)
    (tmp_path / "searches.json").write_text(json.dumps({_key(s): 1.0}))   # old-style key with prices
    known = KnownSearches(tmp_path / "searches.json", PoolCache(tmp_path / "p.json", 60))
    s.price_from, s.price_to = 90, 260
    assert known.has(s)


# --- The Worker link ---

class FakeResponse:
    def __init__(self, ok, data):
        self.ok, self._data, self.status_code = ok, data, 200 if ok else 400
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(data)

    def json(self):
        return self._data


def test_budget_follows_the_pool_up_and_down(tmp_path: Path):
    from flipfinder.settings import apply_settings, set_budget
    cfg = cmd_cfg(tmp_path)
    ds1 = next(s for s in cfg.searches if s.query == "boss ds 1")
    assert ds1.price_to == 72 and ds1.price_to_is_budget
    set_budget(cfg, 241)
    assert cfg.budget == 241 and ds1.price_to == 241          # a bigger pool raises it
    set_budget(cfg, 50)
    assert ds1.price_to == 50                                  # and a smaller one lowers it
    cfg = apply_settings(cmd_cfg(tmp_path), {"prices": {"boss ds 1": [15, 40]}})
    ds1 = next(s for s in cfg.searches if s.query == "boss ds 1")
    set_budget(cfg, 241)
    assert ds1.price_to == 40                                  # set by hand: kept
    set_budget(cfg, 30)
    assert ds1.price_to == 30                                  # but never above the budget


def test_first_ebay_pass_of_a_search_is_silent(tmp_path: Path):
    from flipfinder.scanner import _sid
    s = _fake_scanner(tmp_path, 2, lambda searches: [])
    old, new = s.cfg.searches
    s.known.add(old)                                    # ran before this tracking existed
    items = [titled(500 + n, "thing 1 128GB", 50, "X") for n in range(3)]
    for i in items:
        i.source = "ebay"
    assert s._seed_ebay(old, list(items)) == items     # already had eBay passes: checked as usual
    s.ebay_state.seeded.discard(_sid(new))
    assert s._seed_ebay(new, list(items)) == []        # first eBay pass for it: remembered, no alerts
    assert all(i.key in s.seen for i in items) and _sid(new) in s.ebay_state.seeded
    assert s._seed_ebay(new, list(items)) == items     # later passes are normal
    s.ebay_state.save()
    assert json.loads((tmp_path / "ebay.json").read_text())["seeded"]


# --- Group features, part 2 ---


def test_budget_and_guitar_searches_take_turns(tmp_path: Path, monkeypatch):
    import flipfinder.scanner as sc_mod
    from flipfinder.config import Search
    s = _fake_scanner(tmp_path, 0, lambda searches: [])
    s.cfg.stagger = True
    budget = Search("boss ds 1", budget=True, every_minutes=10)
    guitar = Search("fender player stratocaster")
    phone = Search("iphone 13", filters={"catalog": [3661]})
    s.cfg.searches = [budget, guitar, phone]
    t0 = 1_800_000_000 - (1_800_000_000 % 300)              # start of an even 5-minute slot
    for x in (budget, guitar, phone):
        s.known.add(x)
        s.known.last[sc_mod._sid(x)] = t0 - 300              # all ran in the previous slot
    monkeypatch.setattr(sc_mod.time, "time", lambda: t0 + 30)
    assert [s._turn(x) for x in (budget, guitar, phone)] == [True, False, True]
    monkeypatch.setattr(sc_mod.time, "time", lambda: t0 + 330)  # next (odd) slot
    for x in (budget, guitar, phone):
        s.known.last[sc_mod._sid(x)] = t0 + 30
    assert [s._turn(x) for x in (budget, guitar, phone)] == [False, True, True]
    s.known.last[sc_mod._sid(budget)] = t0 + 330 - 13 * 60     # skipped runs: overdue, runs anyway...
    assert [s._turn(x) for x in (budget, guitar, phone)] == [True, False, True]   # ...instead of guitars, not as well
    s.known.last[sc_mod._sid(guitar)] = t0 + 330 - 20 * 60     # both overdue: the one that waited longer
    assert [s._turn(x) for x in (budget, guitar, phone)] == [False, True, True]
    s.cfg.stagger = False
    s.known.last[sc_mod._sid(guitar)] = t0 + 329
    assert s._turn(guitar)                                       # without stagger: every run as before


def test_gitignore_keeps_outputs_out():
    ignored = Path(".gitignore").read_text(encoding="utf-8").splitlines()
    assert "outputs/" in ignored and "Claude outputs/" in ignored




# --- One run end to end, with the Worker faked ---

class FakeCloud:
    def __init__(self, state):
        self._state, self.calls = state, []

    def state(self):
        self.calls.append(("state",))
        return self._state

    def put_catalog(self, cfg):
        from flipfinder.cloud import catalog
        self.calls.append(("catalog", catalog(cfg)))

    def send_deal(self, deal, text):
        self.calls.append(("deal", deal.item.key, text))
        return "sent"

    def put_demand(self, table):
        self.calls.append(("demand", table))

    def analyze(self, keys):
        self.calls.append(("analyze", keys))
        return {}

    def report_run(self, status=None, values=None, notify=None):
        self.calls.append(("run", status, values, notify))
        return {"notified": [True] * len(notify or []), "flushed": 2}


def test_a_run_uses_worker_settings_and_hands_over_deals(tmp_path: Path, monkeypatch):
    import main as main_mod
    (tmp_path / "c.yaml").write_text(f"""
seen_file: {(tmp_path / 'data' / 'seen.json').as_posix()}
subito: {{enabled: true}}
budget: 72
searches:
  - query: boss katana
    price_from: 80
    price_to: 300
  - query: boss ds 1
    budget: true
    price_from: 15
""", encoding="utf-8")
    deal = evaluate(titled(1, "Boss DS-1 distortion", 25, "Boss"),
                    [titled(10 + i, "Boss DS-1 distortion", 60, "Boss") for i in range(12)],
                    Rules(min_profit=12, min_roi=35, max_roi=150))
    seen = {}

    class FakeScanner:
        def __init__(self, cfg):
            seen["cfg"] = cfg
            self.cfg, self.failed, self.near_misses, self.checked, self.pools, self.ebay = cfg, False, [], 40, None, None

        def scan(self):
            return [deal]

        def demand_due(self):
            return True

        def demand_table(self):
            return {"boss ds 1": {"all": "📊 Demand: still learning", "models": []}}

        @property
        def sold(self):
            return type("S", (), {"save": lambda self: None})()

    cloud = FakeCloud({"settings": {"disabled": ["boss katana"]}, "area": {**AREA, "radius_km": 20},
                       "pool": 50.0, "open": []})
    monkeypatch.setattr(main_mod, "cloud_from_env", lambda: cloud)
    monkeypatch.setattr(main_mod, "Scanner", FakeScanner)
    monkeypatch.setattr(main_mod.RunStats, "summary_due", lambda self: False)   # not 9:00 yet
    monkeypatch.setattr(sys, "argv", ["main.py", "--once", "--config", str(tmp_path / "c.yaml")])
    assert main_mod.main() == 0
    cfg = seen["cfg"]
    assert [s.enabled for s in cfg.searches] == [False, True]               # /categories off-switch applied
    assert cfg.subito.enabled and cfg.subito.radius_km == 20                 # private area from the Worker
    assert cfg.budget == 50                                                  # the pool caps the budget
    kinds = [c[0] for c in cloud.calls]
    assert kinds == ["state", "catalog", "deal", "analyze", "demand", "run"]
    assert cloud.calls[3][1] == [deal.item.key]                               # new deals offered for an AI check
    catalog_sent = cloud.calls[1][1]
    assert all("enabled" not in s for s in catalog_sent["searches"])         # config.yaml as is
    assert cloud.calls[2][1] == deal.item.key and "Boss DS-1" in cloud.calls[2][2]
    assert "boss ds 1" in cloud.calls[4][1]
    status = cloud.calls[5][1]
    assert status["runs"] == 1 and status["checked"] == 40 and status["deals_sent"] == 1
    stats = json.loads((tmp_path / "data" / "stats.json").read_text())
    assert stats["deals_sent"] == 3                                          # + 2 sent from the overnight queue


def test_worker_errors_never_print_the_url_or_key(caplog, monkeypatch):
    import requests
    from flipfinder.cloud import Cloud
    c = Cloud("https://secret-worker.example.workers.dev", "KEY123")
    c.retry_wait = 0

    def boom(*a, **k):
        raise requests.ConnectionError("https://secret-worker.example.workers.dev/api/state KEY123")
    monkeypatch.setattr(c.session, "request", boom)
    with caplog.at_level("INFO"):
        assert c.state() is None
        assert c.report_run(notify=[{"text": "x"}]) == {}
    assert "secret-worker" not in caplog.text and "KEY123" not in caplog.text
    assert "ConnectionError" in caplog.text


def test_workflow_has_no_telegram_secrets_and_cannot_push():
    import yaml
    text = Path(".github/workflows/flipfinder.yml").read_text(encoding="utf-8")
    wf = yaml.safe_load(text)
    assert wf["permissions"]["contents"] == "read"
    assert "TELEGRAM" not in text and "git push" not in text and "deals.json" not in text
    scan = next(x for x in wf["jobs"]["scan"]["steps"] if x.get("name") == "Scan")
    assert set(scan["env"]) >= {"FLIPFINDER_API_URL", "FLIPFINDER_API_KEY"}


def test_private_files_are_never_committed():
    ignored = Path(".gitignore").read_text(encoding="utf-8").splitlines()
    assert {"settings.json", "deals.json", ".env"} <= set(ignored)


def test_worker_calls_retry_once_but_never_resend_after_a_timeout(monkeypatch):
    import requests
    from flipfinder.cloud import Cloud
    c = Cloud("https://w.example", "K")
    c.retry_wait = 0
    calls = []

    def flaky(errors):
        def request(method, url, **k):
            calls.append(method)
            if errors:
                raise errors.pop(0)
            r = requests.Response()
            r.status_code, r._content = 200, b'{"status": "sent"}'
            return r
        return request
    monkeypatch.setattr(c.session, "request", flaky([requests.ConnectionError()]))
    assert c.report_run(notify=[{"text": "x"}]) == {"status": "sent"} and calls == ["POST", "POST"]
    calls.clear()
    monkeypatch.setattr(c.session, "request", flaky([requests.ReadTimeout()]))
    assert c.report_run(notify=[{"text": "x"}]) == {} and calls == ["POST"]          # may have arrived: no resend
    calls.clear()
    monkeypatch.setattr(c.session, "request", flaky([requests.ReadTimeout()]))
    assert c.state() == {"status": "sent"} and calls == ["GET", "GET"]               # a read is safe to repeat
    calls.clear()
    monkeypatch.setattr(c.session, "request", flaky([requests.ConnectionError(), requests.ConnectionError()]))
    assert c.state() is None and calls == ["GET", "GET"]                              # only once


def test_workflow_pings_healthchecks_on_start_success_and_any_failure():
    import yaml
    wf = yaml.safe_load(Path(".github/workflows/flipfinder.yml").read_text(encoding="utf-8"))
    job = wf["jobs"]["scan"]
    steps = job["steps"]
    assert job["env"]["HEALTHCHECK_URL"] == "${{ secrets.HEALTHCHECK_URL }}"
    assert steps[0]["name"] == "Health check start" and "/start" in steps[0]["run"]
    last = steps[-1]
    assert last["name"] == "Health check result" and last["if"] == "always()"
    assert "job.status" in last["run"] and "/fail" in last["run"]
    assert all("|| true" in x["run"] for x in (steps[0], last))       # a healthchecks.io hiccup never fails a run


def test_ebay_pass_waits_for_a_guitar_run(tmp_path: Path, monkeypatch):
    import flipfinder.scanner as sc_mod
    s = _fake_scanner(tmp_path, 0, lambda searches: [])
    s.ebay = type("FakeEbay", (), {"calls": 0})()
    s.cfg.ebay = type("E", (), {"interval_minutes": 20, "max_calls_per_day": 4500})()
    s.cfg.budget, s.cfg.budget_rules = 72, {"min_profit": 12, "min_roi": 35, "max_roi": 150}
    s.cfg.stagger = True
    t0 = 1_800_000_000 - (1_800_000_000 % 300)              # an even slot: budget's turn
    monkeypatch.setattr(sc_mod.time, "time", lambda: t0 + 30)
    s.scan()
    assert s.ebay_due is False                               # due, but it waits...
    monkeypatch.setattr(sc_mod.time, "time", lambda: t0 + 330)
    s.scan()
    assert s.ebay_due is True                                # ...for the guitar run 5 minutes later
    s.cfg.stagger = False
    s.ebay_state.data["last_scan"] = 0                       # due again
    monkeypatch.setattr(sc_mod.time, "time", lambda: t0 + 30)
    s.scan()
    assert s.ebay_due is True                                # without turns, as before


# --- Sold-price tracking ---

def test_item_page_status_from_vinted_page_data():
    from flipfinder.vinted import item_page_status
    for_sale = '"can_buy":true,"is_hidden":false,"is_reserved":false,"availability":"InStock"'
    sold = '"can_buy":false,"is_hidden":false,"is_reserved":false'
    assert item_page_status(for_sale) == "active"
    assert item_page_status(sold) == "sold"
    assert item_page_status('"can_buy":false,"is_hidden":false,"is_reserved":true') == "reserved"
    assert item_page_status('"is_hidden":true') == "deleted"
    assert item_page_status('{"hidden":true}') == "unknown"


def _tracker(tmp_path):
    from flipfinder.sold import SoldTracker
    return SoldTracker(tmp_path / "sold.json")


def test_listing_dates_come_from_vinted_ids(tmp_path: Path):
    t = _tracker(tmp_path)
    t.anchor([titled(1000, "x", 1, "X")], 0)
    t.anchor([titled(2000, "x", 1, "X")], 100)          # the first anchor is always kept
    t.anchor([titled(2600, "x", 1, "X")], 300)          # within 10 min of the one before: replaces the last
    t.anchor([titled(3000, "x", 1, "X")], 1000)
    t.anchor([titled(2500, "x", 1, "X")], 2000)         # not newer: ignored
    assert t.data["anchors"] == [[1000, 0], [2600, 300], [3000, 1000]]
    assert t.listed_at(999) is None                      # older than tracking: unknown
    assert t.listed_at(1800) == 150.0 and t.listed_at(5000) == 1000


def test_gone_listings_become_candidates_and_page_checks_decide(tmp_path: Path):
    t = _tracker(tmp_path)
    day = 86400
    t.anchor([titled(100, "x", 1, "X")], 0)
    pool1 = [titled(i, f"Boss DS-1 #{i}", 30 + i - 100, "Boss") for i in (101, 102, 103, 104, 105)]
    t.anchor([titled(110, "x", 1, "X")], 2 * day)
    t.observe("ds1", [], pool1, 2 * day)
    pool2 = [i for i in pool1 if i.id in (101, 105)]
    t.observe("ds1", pool1, pool2, 4 * day)
    assert sorted(t.data["pending"]) == ["102", "103", "104"]
    t.observe("ds1", pool2, pool2 + [pool1[2]], 4.1 * day)   # 103 is back: only fell off a page
    assert sorted(t.data["pending"]) == ["102", "104"]
    client = FakeVinted()
    client.statuses = {102: "sold", 104: "reserved"}
    out = t.verify(client, 4.2 * day)
    assert out["sold"] == 1 and out["reserved"] == 1
    rec = t.data["sold"]["ds1"][0]
    assert rec["item"]["price"] == 32 and rec["at"] == 3 * day     # between last seen and gone
    assert rec["days"] == 2.6                                        # listed ~0.4 d (id 102), sold day 3
    assert list(t.data["pending"]) == ["104"] and t.data["pending"]["104"]["tries"] == 1
    sold = t.sold_for("ds1", 4.2 * day)
    assert [(i.title, d, at) for i, d, at in sold] == [("Boss DS-1 #102", 2.6, 3 * day)]
    assert t.sold_for("ds1", 64 * day) == []                         # only the last 60 days count


def test_candidates_favour_searches_with_few_sales_and_fresh_gaps(tmp_path: Path):
    t = _tracker(tmp_path)
    t.data["pending"] = {"1": {"key": "a", "gone": 10}, "2": {"key": "b", "gone": 5}, "3": {"key": "b", "gone": 20}}
    t.data["sold"] = {"a": [{}] * 3}
    assert t.candidates(2) == ["3", "2"]


def test_page_checks_back_off_when_vinted_blocks_and_recover(tmp_path: Path):
    from flipfinder import sold as sold_mod
    t = _tracker(tmp_path)
    h = 3600
    t.adapt(1, 0)
    assert t.data["checks"]["per_run"] == 2 and "lowered to 2 per run" in t.notices[-1]
    t.adapt(2, h)
    t.adapt(1, 2 * h)
    assert t.data["checks"]["per_run"] == 0 and "none for 6 h" in t.notices[-1]
    client = FakeVinted()
    t.data["pending"] = {"7": {"key": "a", "gone": 0, "last": 0, "first": 0, "item": {}, "tries": 0}}
    assert sum(t.verify(client, 3 * h).values()) == 0                # paused
    assert sum(t.verify(client, 9 * h).values()) == 1                # pause over: one check
    t.adapt(0, 9 * h)
    assert t.data["checks"]["per_run"] == 0                           # not 24 h quiet yet
    t.adapt(0, 2 * h + 25 * h)
    assert t.data["checks"]["per_run"] == 1 and "back up to 1" in t.notices[-1]
    for k in range(5):
        t.adapt(0, (60 + 25 * k) * h)
    assert t.data["checks"]["per_run"] == sold_mod.MAX_CHECKS


def test_blocked_page_check_stops_the_checks(tmp_path: Path):
    t = _tracker(tmp_path)
    t.data["pending"] = {str(i): {"key": "a", "gone": i, "last": 0, "first": 0, "item": {}, "tries": 0} for i in range(4)}
    client = FakeVinted()
    client.statuses = {3: "blocked"}                                  # the newest gone is checked first
    out = t.verify(client, 10)
    assert out["blocked"] == 1 and sum(out.values()) == 1 and len(t.data["pending"]) == 4


def _sold(n, title, price, brand="Boss", days=None):
    return [(titled(9000 + n * 100 + i, title, price, brand), days) for i in range(n)]


def test_sold_prices_pull_market_value_towards_what_sold(tmp_path: Path):
    from dataclasses import replace as dc_replace
    item = titled(1, "Boss DS-1 distortion", 25, "Boss")
    pool = [titled(10 + i, "Boss DS-1 distortion", 60, "Boss") for i in range(12)]
    rules = Rules(min_profit=1, min_roi=1, max_roi=500)
    base = assess(item, pool, rules)
    assert base.market_value == 60 and base.sold_count == 0 and base.sell_days is None
    four = assess(item, pool, dc_replace(rules, sold=_sold(4, "Boss DS-1 distortion", 40)))
    assert four.market_value == 60 and four.sold_count == 0                 # too few sold to count
    five = assess(item, pool, dc_replace(rules, sold=_sold(5, "Boss DS-1 distortion", 40)))
    assert five.market_value == 50 and five.sold_count == 5 and five.asking_value == 60   # half and half
    ten = assess(item, pool, dc_replace(rules, sold=_sold(10, "Boss DS-1 distortion", 40)))
    assert ten.market_value == 40                                           # sold only
    other = assess(item, pool, dc_replace(rules, sold=_sold(10, "Boss DS-2 turbo distortion", 40)))
    assert other.market_value == 60                                         # another model: not counted
    assert ten.profit < base.profit


def test_selling_speed_shows_and_moves_the_rating_one_point(tmp_path: Path):
    from dataclasses import replace as dc_replace
    item = titled(1, "Boss DS-1 distortion", 25, "Boss")
    pool = [titled(10 + i, "Boss DS-1 distortion", 60, "Boss") for i in range(12)]
    rules = Rules(min_profit=1, min_roi=1, max_roi=500)
    base = assess(item, pool, rules)
    fast = assess(item, pool, dc_replace(rules, sold=_sold(3, "Boss DS-1 distortion", 60, days=4.0)))
    slow = assess(item, pool, dc_replace(rules, sold=_sold(3, "Boss DS-1 distortion", 60, days=45.0)))
    mid = assess(item, pool, dc_replace(rules, sold=_sold(3, "Boss DS-1 distortion", 60, days=15.0)))
    assert fast.sell_days == 4.0 and fast.rating == min(10, base.rating + 1)
    assert slow.rating == max(1, base.rating - 1) and mid.rating == base.rating
    assert assess(item, pool, dc_replace(rules, sold=_sold(2, "Boss DS-1 distortion", 60, days=4.0))).sell_days is None
    msg = format_deal(fast)
    assert "⏱ sells in ~4 days" in msg and "🏷 Original price" in msg       # 3 sold: speed yes, value not yet
    ten = assess(item, pool, dc_replace(rules, sold=_sold(10, "Boss DS-1 distortion", 40, days=1.2)))
    msg = format_deal(ten)
    assert "🏷 Market value: <b>€40.00</b> (10 sold · asking €60.00, median of 12)" in msg
    assert "⏱ sells in ~1 day" in msg


def test_a_scan_tracks_pools_and_checks_gone_listings(tmp_path: Path, caplog):
    import flipfinder.scanner as sc_mod
    s = _fake_scanner(tmp_path, 1, lambda searches: [sc_mod._key(x) for x in searches])
    key = sc_mod._key(s.cfg.searches[0])
    old = [titled(500 + i, "thing 0 128GB", 300, "X") for i in range(3)]
    s.pools.put(key, old)
    s.pools.data[key]["ts"] = 0                                      # stale: rebuilt this run
    s.sold.observe(key, [], old, time.time() - 3600)                 # tracked since the last refresh
    s.client.statuses = {500: "sold", 501: "deleted"}
    with caplog.at_level("INFO"):
        s.scan()
    assert s.sold.stats()["sold"] == 1
    assert "Sold prices: 1 sold, 1 active, 1 deleted" in caplog.text
    assert s.sold.data["anchors"]                                    # the newest feed gave a date anchor
    saved = json.loads((tmp_path / "sold.json").read_text())
    assert len(saved["sold"][key]) == 1
    s.client.blocked = 2                                             # Vinted refused requests this run
    s.sold.data["pending"]["9"] = {"key": key, "gone": time.time(), "last": 0, "first": 0, "item": {}, "tries": 0}
    s.scan()
    assert "9" in s.sold.data["pending"]                              # no page checks while blocked
    assert any("lowered to 2 per run" in n for n in s.notices)



def test_the_first_refresh_uses_the_previous_pool_as_history(tmp_path: Path):
    t = _tracker(tmp_path)
    old = [titled(i, f"Boss DS-1 #{i}", 30, "Boss") for i in (1, 2, 3)]
    t.observe("ds1", old, old[:1], 1000, old_ts=400)
    assert sorted(t.data["pending"]) == ["2", "3"]
    assert t.data["pending"]["2"]["last"] == 400 and t.data["seen"]["ds1"]["1"] == [400, 1000, 30]
    t2 = _tracker(tmp_path / "x")
    t2.observe("ds1", old, old[:1], 1000)                 # no age known: no guessing
    assert t2.data["pending"] == {}


def _sold_at(n, title, price, at, days=None, brand="Boss"):
    return [(titled(7000 + n * 100 + i, title, price, brand), days, at) for i in range(n)]


def test_demand_is_still_learning_at_first(tmp_path: Path):
    from dataclasses import replace as dc_replace
    item = titled(1, "Boss DS-1 distortion", 25, "Boss")
    pool = [titled(10 + i, "Boss DS-1 distortion", 60, "Boss") for i in range(12)]
    for i, p in enumerate(pool):
        p.favourites = i                                          # 0..11: average 5.5 -> 6
    rules = Rules(min_profit=1, min_roi=1, max_roi=500, now=30 * 86400)
    d = assess(item, pool, dc_replace(rules, tracked_days=3, sold_scale=2.0,
                                      sold=_sold_at(9, "Boss DS-1 distortion", 60, 29 * 86400)))
    assert d.demand["learning"] and d.demand["listed"] == 12 and d.demand["favourites"] == 6
    assert "📊 Demand: still learning · 12 listed · ❤️ 6 avg favourites" in format_deal(d)
    # a week tracked but no checks yet (no scale): still learning
    d = assess(item, pool, dc_replace(rules, tracked_days=8, sold_scale=None))
    assert d.demand["learning"]


def test_demand_labels_from_sell_through_and_speed(tmp_path: Path):
    from dataclasses import replace as dc_replace
    item = titled(1, "Boss DS-1 distortion", 25, "Boss")
    pool = [titled(10 + i, "Boss DS-1 distortion", 60, "Boss") for i in range(10)]
    now = 30 * 86400
    rules = Rules(min_profit=1, min_roi=1, max_roi=500, now=now, tracked_days=28, sold_scale=2.5)
    # 8 confirmed in 4 weeks x 2.5 (we check 1 in 2.5 gone listings) = 5/week; 10 listed -> 0.5: high
    hi = assess(item, pool, dc_replace(rules, sold=_sold_at(8, "Boss DS-1 distortion", 60, now - 5 * 86400)))
    assert hi.demand["label"] == "high" and hi.demand["sold_week"] == 5.0 and hi.demand["ratio"] == 0.5
    assert "🔥 High demand · ~5 sold/week · 10 listed · ❤️ 0" in format_deal(hi)
    # 3 sold, 9 days each: 1.9/week -> 0.19: normal, and the speed shows
    mid = assess(item, pool, dc_replace(rules, sold=_sold_at(3, "Boss DS-1 distortion", 60, now - 86400, days=9.0)))
    assert mid.demand["label"] == "normal"
    assert "👍 Normal demand · ~2 sold/week · 10 listed · ⏱ sells in ~9 days" in format_deal(mid)
    # 1 sold in 4 weeks: 0.6/week -> 0.06: slow
    slow = assess(item, pool, dc_replace(rules, sold=_sold_at(1, "Boss DS-1 distortion", 60, now - 86400)))
    assert slow.demand["label"] == "slow" and "🐢 Slow · ~0.6 sold/week" in format_deal(slow)
    # quick sellers are high demand even when few sell
    fast = assess(item, pool, dc_replace(rules, sold=_sold_at(3, "Boss DS-1 distortion", 60, now - 86400, days=3.0)))
    assert fast.demand["label"] == "high"
    # other models' sales don't count; old sales (over 28 days) neither
    other = assess(item, pool, dc_replace(rules, sold=_sold_at(8, "Boss DS-2 turbo distortion", 60, now - 86400)))
    old = assess(item, pool, dc_replace(rules, sold=_sold_at(8, "Boss DS-1 distortion", 60, now - 40 * 86400)))
    assert other.demand["sold_week"] == 0 and old.demand["sold_week"] == 0


def test_coverage_scales_the_sales_we_confirm(tmp_path: Path):
    t = _tracker(tmp_path)
    old = [titled(i, f"Boss DS-1 #{i}", 30, "Boss") for i in range(1, 11)]
    t.observe("ds1", old, old[:2], 2 * 86400, old_ts=86400)            # 8 gone
    assert t.coverage("ds1", 3 * 86400) == (2.0, None)                   # tracked since the old pool, no checks yet
    client = FakeVinted()
    client.statuses = {3: "sold", 4: "deleted"}
    t.data["checks"]["per_run"] = 4
    t.verify(client, 3 * 86400)
    t.verify(client, 3 * 86400)                                          # 8 checked: 1 sold, 1 deleted, 6 active
    assert t.data["keys"]["ds1"] == {"since": 86400, "gone": 8, "decided": 8, "sold": 1}
    assert t.coverage("ds1", 3 * 86400) == (2.0, 1.0)



def test_demand_table_per_search_and_model(tmp_path: Path):
    import flipfinder.scanner as sc_mod
    from flipfinder.config import Search
    s = _fake_scanner(tmp_path, 0, lambda searches: [])
    q = Search("iphone 13")
    s.cfg.searches = [q]
    key = sc_mod._key(q)
    pool = ([titled(100 + i, "iPhone 13 128GB blu", 400, "Apple") for i in range(6)]
            + [titled(200 + i, "iPhone 13 256GB nero", 480, "Apple") for i in range(4)]
            + [titled(300 + i, "iPhone 13 Pro 128GB", 600, "Apple") for i in range(2)])
    s.pools.put(key, pool)
    table = s.demand_table()
    rows = table["iphone 13"]["models"]
    assert [(r["model"], r["listed"]) for r in rows] == [("iphone 13 128gb", 6), ("iphone 13 256gb", 4)]
    assert "still learning · 6 listed" in rows[0]["text"]
    assert "still learning · 12 listed" in table["iphone 13"]["all"]
    assert not s.demand_due()                                       # sent: the next one in 30 min


# --- Seller trust ---

def _rsc(payload: str) -> str:
    return "<script>self.__next_f.push([1," + json.dumps(payload) + "])</script>"


def test_item_page_reads_the_seller():
    payload = ('{"x":[{"data":{"badges":[],"business":false,"feedback_count":256,"feedback_reputation":0.98,'
               '"name":"someone","seller_id":"123","user_info":[{"key":"last-logged-in","text":"Ultima visita 2 ore fa"}]},'
               '"exposures":[],"name":"user_info_header","section":"sidebar"},'
               '{"data":{"badges":[{"type":"SPEEDY_SHIPPING"},{"type":"ACTIVE_LISTER"}],"username":"someone"},'
               '"exposures":[],"name":"seller_badges_info","section":"sidebar"}]}')
    d = parse_item_page(_rsc(payload))
    assert (d.seller_id, d.reviews, d.reputation, d.business) == (123, 256, 0.98, False)
    assert d.last_seen == "Ultima visita 2 ore fa" and d.badges == ["SPEEDY_SHIPPING", "ACTIVE_LISTER"]
    assert parse_item_page(_rsc('{"x":1}')).reviews is None                      # no seller block: unknown


def test_last_seen_in_english():
    from flipfinder.vinted import seen_english
    assert seen_english("Ultima visita 2 ore fa") == "seen 2 h ago"
    assert seen_english("Ultima visita un'ora fa") == "seen 1 h ago"
    assert seen_english("Ultima visita 15 minuti fa") == "seen 15 min ago"
    assert seen_english("Ultima visita 3 giorni fa") == "seen 3 days ago"
    assert seen_english("Ultima visita ieri") == "seen 1 day ago"
    assert seen_english("qualcosa di nuovo") == ""


def test_profile_page_gives_items_sold():
    from flipfinder.vinted import parse_profile_page
    payload = ('{"user":{"feedback_count":256,"given_item_count":251,"taken_item_count":146,"item_count":127,'
               '"positive_feedback_count":251,"negative_feedback_count":5}}')
    assert parse_profile_page(_rsc(payload)) == {"sold": 251, "bought": 146, "listed": 127, "positive": 251,
                                                  "negative": 5, "reviews": 256}


def test_ebay_seller_feedback():
    from flipfinder.ebay import ebay_seller, item_from_ebay
    raw = {"itemId": "v1|123|0", "title": "x", "price": {"value": "10", "currency": "EUR"},
           "seller": {"username": "shop", "feedbackPercentage": "99.8", "feedbackScore": 1234}}
    assert item_from_ebay(raw).seller == {"source": "ebay", "positive_pct": 99.8, "reviews": 1234, "sold": None}
    assert ebay_seller({"feedbackScore": 0}) == {"source": "ebay", "positive_pct": None, "reviews": 0, "sold": 0}
    assert ebay_seller(None) is None


def test_brand_new_sellers_are_skipped_only_when_suspiciously_cheap():
    from flipfinder.analyzer import seller_check
    new = {"source": "vinted", "reviews": 0, "sold": 0}
    assert seller_check(new, 120)[0].startswith("new seller (no reviews, nothing sold) at +120%")
    assert seller_check(new, 99) == (None, ["⚠️ New seller: no reviews, nothing sold yet"])
    assert seller_check({**new, "sold": 4}, 150) == (None, ["⚠️ New seller: no reviews"])   # has sold things: warn only
    assert seller_check({"reviews": 0, "sold": None}, 150) == (None, ["⚠️ New seller: no reviews"])   # unknown: no skip
    assert seller_check({"source": "vinted", "reviews": 12, "stars": 3.9, "negative": 4}, 50) == \
        (None, ["⚠️ Low rating: 3.9★ (4 negative)"])
    assert seller_check({"source": "vinted", "reviews": 3, "stars": 3.0}, 50) == (None, [])   # too few to judge
    assert seller_check({"source": "ebay", "reviews": 40, "positive_pct": 94.1}, 50) == \
        (None, ["⚠️ Low feedback: 94.1% positive"])
    assert seller_check(None, 300) == (None, [])


def test_seller_lines_in_the_alert():
    pool = [titled(10 + i, "Boss DS-1 distortion", 60, "Boss") for i in range(12)]
    item = titled(1, "Boss DS-1 distortion", 25, "Boss")
    item.seller = {"source": "vinted", "reviews": 256, "stars": 4.9, "sold": 251, "fast": True, "seen": "seen 2 h ago",
                   "business": True}
    msg = format_deal(assess(item, pool, Rules(min_profit=1, min_roi=1, max_roi=500)))
    assert "👤 4.9★ · 256 reviews · 251 sold · ⚡ fast shipper · seen 2 h ago" in msg and "🏪 Business seller" in msg
    item.seller = {"source": "vinted", "reviews": 0, "sold": 0, "stars": None}
    d = assess(item, pool, Rules(min_profit=1, min_roi=1, max_roi=500))
    assert d.blocked and d.blocked[-1].startswith("new seller")                     # 25 -> 60: ROI well over 100%
    item.seller = {"source": "ebay", "reviews": 1234, "positive_pct": 99.8}
    assert "👤 99.8% positive · 1,234 feedback" in format_deal(assess(item, pool, Rules(min_profit=1, min_roi=1, max_roi=500)))


def test_a_scan_skips_brand_new_sellers_and_counts_them(tmp_path: Path, caplog):
    import flipfinder.scanner as sc_mod
    from flipfinder.vinted import ItemDetails
    s = _fake_scanner(tmp_path, 1, lambda searches: [sc_mod._key(x) for x in searches])
    s.client.details = lambda item: ItemDetails(shipping=5.0, seller_id=9, reviews=0, reputation=0.0)
    s.client.seller_profile = lambda sid: {"sold": 0, "negative": 0}
    with caplog.at_level("INFO"):
        deals = s.scan()
    assert deals == [] and s.seller_skipped > 0
    assert "skipped a deal, brand-new seller" in caplog.text and "near" not in [m for m in s.near_misses]
    (tmp_path / "b").mkdir()
    s2 = _fake_scanner(tmp_path / "b", 1, lambda searches: [sc_mod._key(x) for x in searches])
    s2.client.details = lambda item: ItemDetails(shipping=5.0, seller_id=9, reviews=40, reputation=1.0, last_seen="Ultima visita 5 minuti fa")
    s2.client.seller_profile = lambda sid: {"sold": 38, "negative": 0}
    deals = s2.scan()
    assert deals and deals[0].item.seller == {"source": "vinted", "reviews": 40, "stars": 5.0, "business": False,
                                              "seen": "seen 5 min ago", "fast": False, "sold": 38, "negative": 0}
    assert "👤 5★ · 40 reviews · 38 sold · seen 5 min ago" in format_deal(deals[0])


def test_summary_counts_skipped_sellers(tmp_path: Path):
    from flipfinder.health import RunStats
    s = RunStats(tmp_path / "stats.json")
    s.record_run(10, 0, None, seller_skipped=2)
    s.record_run(10, 0, None)
    assert "🛡 2 deals skipped: brand-new seller and suspiciously cheap" in s.summary_text(Rules())


def test_deal_record_carries_what_the_ai_check_needs_and_pools_stay_small(tmp_path: Path):
    from flipfinder.cloud import deal_record
    from flipfinder.scanner import PoolCache
    item = titled(1, "Boss DS-1 distortion", 25, "Boss")
    item.photos, item.description = ["https://img/1.jpg", "https://img/2.jpg"], "Works fine, a few scratches"
    deal = evaluate(item, [titled(10 + i, "Boss DS-1 distortion", 60, "Boss") for i in range(12)],
                    Rules(min_profit=12, min_roi=35, max_roi=150))
    rec = deal_record(deal)
    assert rec["item"]["photos"] == ["https://img/1.jpg", "https://img/2.jpg"]
    assert rec["item"]["description"] == "Works fine, a few scratches"
    assert 1 <= len(rec["comparables"]) <= 8 and rec["comparables"][0]["price"] == 60
    cache = PoolCache(tmp_path / "pools.json", 180)
    cache.put("k", [item])
    cache.save()
    saved = json.loads((tmp_path / "pools.json").read_text())["k"]["items"][0]
    assert "photos" not in saved and "description" not in saved
    assert cache.get("k")[0].title == "Boss DS-1 distortion"


# --- the fast lane

def test_fast_lane_timing_rules():
    from datetime import datetime
    from flipfinder import fastlane
    rome = lambda h, m, sec=0: datetime(2026, 10, 7, h, m, sec, tzinfo=fastlane.ROME).timestamp()
    off = fastlane.settings({"settings": {}})
    assert off["enabled"] is False and off["interval"] == 2 and off["per_run"] == 8      # off until /fast 2
    assert fastlane.due(off, rome(12, 0)) == (False, "the fast lane is off (/fast 2 turns it on)")
    on = fastlane.settings({"settings": {"fast": {"enabled": True, "interval": 2}}})
    last = rome(12, 0)
    assert not fastlane.due(on, rome(12, 1, 10), last)[0]          # triggered a minute later: too soon
    assert fastlane.due(on, rome(12, 1, 40), last)[0]              # late start of the next one: goes
    assert fastlane.due(on, rome(12, 2, 30), last)[0]
    assert fastlane.due(on, rome(7, 58), 0)[1].startswith("quiet hours")
    backing = {**on, "backoff_until": rome(13, 0)}
    assert fastlane.interval(backing, rome(12, 30)) == 5 and fastlane.interval(backing, rome(13, 1)) == 2
    assert not fastlane.due(backing, rome(12, 3), last)[0] and "backing off" in fastlane.due(backing, rome(12, 3), last)[1]
    assert fastlane.due(backing, rome(12, 5), last)[0]


def test_fast_lane_picks_guitars_first_and_rotates_through_the_list():
    from types import SimpleNamespace as NS
    from flipfinder import fastlane
    searches = ([NS(query=f"phone {n}", enabled=True, budget=False, g="Electronics") for n in range(6)] +
                [NS(query=f"guitar {n}", enabled=True, budget=False, g="Guitars") for n in range(6)] +
                [NS(query="pedal cheap", enabled=True, budget=True, g="Budget"), NS(query="off", enabled=False, budget=False, g="Guitars")])
    cfg = NS(searches=searches)
    fs = {**fastlane.DEFAULTS, "enabled": True, "per_run": 8}
    group = lambda s: s.g
    first = fastlane.this_pass(cfg, fs, group, 0)
    second = fastlane.this_pass(cfg, fs, group, 1)
    assert first == [f"guitar {n}" for n in range(6)] + ["phone 0", "phone 1"]      # guitars first, 8 a pass
    assert second == [f"phone {n}" for n in range(2, 6)]                            # then the rest
    assert fastlane.this_pass(cfg, fs, group, 2) == first                            # and round again
    assert "pedal cheap" not in first + second and "off" not in first + second     # budget + disabled: full scan only


def test_fast_scan_reads_the_cache_and_never_rebuilds_or_saves(tmp_path: Path):
    from flipfinder.scanner import _key
    s = _fake_scanner(tmp_path, 3, lambda searches: [_key(x) for x in searches])
    s.pools.put(_key(s.cfg.searches[0]), [titled(900 + n, "thing 0 128GB", 300, "X") for n in range(10)])
    s.pools.save()
    before = (tmp_path / "pools.json").read_text(), (tmp_path / "seen.json").read_text()
    deals = s.fast_scan(["thing 0", "thing 1"])      # thing 1 has no cached pool: skipped, not rebuilt
    assert len(deals) == 5 and s.checked == 5
    assert ((tmp_path / "pools.json").read_text(), (tmp_path / "seen.json").read_text()) == before
    assert not hasattr(s, "pool_rebuilds") or s.pool_rebuilds == 0
    s.seen = SeenStore(tmp_path / "empty.json")       # no saved scan yet: nothing to compare with
    assert s.fast_scan(["thing 0"]) == []


def test_a_fast_pass_sends_reports_and_skips_when_not_due(tmp_path: Path, monkeypatch):
    import main as main_mod
    from flipfinder import fastlane
    (tmp_path / "c.yaml").write_text(f"""
seen_file: {(tmp_path / 'data' / 'seen.json').as_posix()}
searches:
  - query: boss katana
    group: Amps
""", encoding="utf-8")
    deal = evaluate(titled(1, "Boss Katana 50", 80, "Boss"), [titled(10 + i, "Boss Katana 50", 200, "Boss") for i in range(12)],
                    Rules(min_profit=12, min_roi=35, max_roi=150))

    class FastScanner:
        def __init__(self, cfg):
            self.client, self.checked = type("V", (), {"requests": 9, "blocked": 1})(), 3

        def fast_scan(self, queries):
            assert queries == ["boss katana"]
            return [deal]

    cloud = FakeCloud({"settings": {"fast": {"enabled": True, "interval": 1}}, "fast": {"last_started": 0, "passes": 3}})
    cloud.fast_report = lambda body: cloud.calls.append(("fast", body)) or {}
    monkeypatch.setattr(main_mod, "cloud_from_env", lambda: cloud)
    monkeypatch.setattr(main_mod, "Scanner", FastScanner)
    monkeypatch.setattr(fastlane, "due", lambda fs, now, last: (True, ""))
    monkeypatch.setattr(sys, "argv", ["main.py", "--fast", "--config", str(tmp_path / "c.yaml")])
    assert main_mod.main() == 0
    assert [c[0] for c in cloud.calls] == ["state", "deal", "analyze", "fast"]
    report = cloud.calls[-1][1]
    assert report["requests"] == 9 and report["blocked"] == 1 and report["sent"] == 1 and report["searches"] == 1
    assert report["started"] > 0
    cloud.calls.clear()
    monkeypatch.setattr(fastlane, "due", lambda fs, now, last: (False, "quiet hours"))
    assert main_mod.main() == 0
    assert [c[0] for c in cloud.calls] == ["state"]                 # not due: nothing searched, nothing sent
