import json
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

def subito_ad(n, subject, price, town="Hometown", lat=44.500500, lon=11.300800, ships=False, ship_cost=None,
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
    c = SubitoClient(town_travel_costs={"Southtown": 8, "Westtown": 8})
    it = item_from_subito(subito_ad(1, "Fender Stratocaster Player", 450, body="Prezzo trattabile"))
    assert (it.id, it.price, it.source, it.condition, it.negotiable, it.key) ==         (660000001, 450.0, "subito", "Ottimo", True, "subito:660000001")
    northtown = item_from_subito(subito_ad(2, "x", 10, "Northtown", 44.5745, 11.3000))
    southtown = item_from_subito(subito_ad(3, "x", 10, "Southtown", 44.3150, 11.3000))
    fartown = item_from_subito(subito_ad(4, "x", 10, "Fartown", 44.9500, 11.3000))
    assert c.in_area(northtown) and c.in_area(southtown) and not c.in_area(fartown)
    assert 7 < northtown.distance_km < 9 and 19 < southtown.distance_km < 22 and fartown.distance_km > 45
    assert (c.delivery(northtown).shipping, northtown.delivery) == (5.0, "pickup")
    assert c.delivery(southtown).shipping == 8.0                       # town override
    cheap_ship = item_from_subito(subito_ad(5, "x", 10, "Southtown", 44.3150, 11.3000, ships=True, ship_cost=6))
    c.in_area(cheap_ship)
    assert (c.delivery(cheap_ship).shipping, cheap_ship.delivery) == (6.0, "shipping")   # cheaper than €8 travel
    for body in ("Non trattabile", "Prezzo non è trattabile", "prezzo fisso, ritiro a mano", "Non tratto"):
        assert not item_from_subito(subito_ad(6, "Boss DS-1", 40, body=body)).negotiable, body
    assert item_from_subito(subito_ad(6, "Boss DS-1", 40, body="45€ tratt.")).negotiable


def test_subito_alert_lines():
    from flipfinder.subito import SubitoClient, item_from_subito
    c = SubitoClient()
    it = item_from_subito(subito_ad(7, "Boss DS-1 distortion", 20, "Northtown", 44.5745, 11.3000, body="trattabile"))
    c.in_area(it); c.delivery(it)
    p = pool([60] * 12)
    for x in p:
        x.title = "Boss DS-1 distortion"
    d = assess(it, p, Rules(resell_costs=1), "boss")
    msg = format_deal(d)
    assert "🛒 <b>Subito</b> · Northtown (8 km)" in msg and "💬 Negotiable" in msg
    assert "🚗 Pickup + packaging: <b>€6.00</b> (travel to Northtown)" in msg
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
    c = SubitoClient()
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


# --- Telegram commands ---

OWNER = 1000001


class FakeTG:
    """Records Bot API calls; getUpdates returns the queued updates once."""

    def __init__(self, updates):
        self.updates, self.calls = updates, []

    def call(self, method, payload):
        self.calls.append((method, payload))
        if method == "getUpdates":
            if "offset" in payload:
                return []
            out, self.updates = self.updates, []
            return out
        if method == "sendMessage":
            return {"message_id": 900 + len(self.calls), "chat": {"id": payload["chat_id"]}}
        return True

    @staticmethod
    def is_group(chat) -> bool:
        return str(chat).startswith("-")

    def sent(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method]


def msg(text, user=OWNER, chat=111, uid=[0]):
    uid[0] += 1
    return {"update_id": uid[0], "message": {"text": text, "from": {"id": user}, "chat": {"id": chat}}}


def tap(data, user=OWNER, chat=111, uid=[1000]):
    uid[0] += 1
    return {"update_id": uid[0], "callback_query": {"id": f"cq{uid[0]}", "data": data, "from": {"id": user},
                                                    "message": {"chat": {"id": chat}, "message_id": 55}}}


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
    return config_mod.load(tmp_path / "c.yaml", settings_path=None)


def run_cmds(tmp_path, updates, settings=None):
    import os
    from flipfinder.commands import Commands, load_settings
    cfg = cmd_cfg(tmp_path)
    tg = FakeTG(updates)
    cwd = os.getcwd()
    os.chdir(tmp_path)                       # settings.json is written next to the config
    try:
        s = settings if settings is not None else load_settings(tmp_path / "settings.json")
        from flipfinder.commands import COMMANDS_VERSION
        s["commands_version"] = COMMANDS_VERSION   # already registered
        c = Commands(tg, cfg, s)
        changed = c.run()
    finally:
        os.chdir(cwd)
    return tg, c, cfg, changed


def test_setprice_validates_and_saves(tmp_path: Path):
    tg, c, cfg, changed = run_cmds(tmp_path, [
        msg('/setprice "boss katana" 90 260'),
        msg("/setprice boss katana 300 100"),
        msg("/setprice boss katana ten 100"),
        msg("/setprice boss katna 90 260"),
        msg("/setprice boss ds 1 10 90"),
    ])
    replies = [p["text"] for p in tg.sent()]
    assert "✅ <b>boss katana</b>: €90 – €260" in replies[0]
    assert "must be lower than the maximum" in replies[1]
    assert "must be numbers" in replies[2]
    assert "No search called" in replies[3] and "boss katana" in replies[3]       # suggestion
    assert "budget search" in replies[4]
    assert changed and c.settings["prices"] == {"boss katana": [90.0, 260.0]}
    saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert saved["prices"] == {"boss katana": [90.0, 260.0]}


def test_strangers_are_ignored_and_allow_is_owner_only(tmp_path: Path):
    tg, c, cfg, changed = run_cmds(tmp_path, [
        msg("/budget 10", user=999),
        msg("/allow 555"),
        msg("/budget 50", user=555),
        msg("/allow 777", user=555),
    ])
    replies = [p["text"] for p in tg.sent()]
    assert len(replies) == 3                                  # nothing for the stranger
    assert "User 555 can now use commands" in replies[0]
    assert "Budget is now €50" in replies[1]                  # 555 is allowed now
    assert "Only the owner" in replies[2]
    assert c.settings["allowed_users"] == [555] and c.settings["budget"] == 50


def test_categories_buttons_toggle_searches(tmp_path: Path):
    from flipfinder.commands import apply_settings, search_id
    tg, c, cfg, changed = run_cmds(tmp_path, [msg("/categories"), tap(f"t:{search_id('iphone 13')}")])
    groups = {p["text"].split("</b>")[0].replace("<b>", ""): p for p in tg.sent()}
    assert set(groups) == {"Amps", "Electronics", "Budget"}
    assert groups["Electronics"]["reply_markup"]["inline_keyboard"][0][0]["text"] == "✅ iphone 13"
    assert c.settings["disabled"] == ["iphone 13"]
    edited = tg.sent("editMessageReplyMarkup")[0]["reply_markup"]["inline_keyboard"][0][0]["text"]
    assert edited == "❌ iphone 13"
    assert tg.sent("answerCallbackQuery")[0]["text"] == "iphone 13: off"
    cfg2 = apply_settings(cmd_cfg(tmp_path), c.settings)
    assert [s.enabled for s in cfg2.searches] == [True, False, True]


def test_rules_budget_add_remove(tmp_path: Path):
    from flipfinder.commands import apply_settings, search_id
    tg, c, cfg, changed = run_cmds(tmp_path, [
        msg("/setrule min_roi 25"), msg("/setrule min_rating 11"), msg("/setrule max_roi 20"),
        msg("/setrule speed 3"), msg("/budget abc"), msg("/budget 60"),
        msg('/add "zoom g1x four" 20 60'), msg("/add boss katana 10 20"),
        msg("/remove boss katana"), tap(f"rm:{search_id('boss katana')}:y"),
    ])
    r = [p["text"] for p in tg.sent()]
    assert "min_roi is now 25" in r[0] and "between 1 and 10" in r[1] and "below max_roi" in r[2]
    assert "Unknown rule" in r[3] and "must be a number" in r[4] and "Budget is now €60" in r[5]
    assert "Added <b>zoom g1x four</b>" in r[6] and "already a search" in r[7]
    assert "Remove <b>boss katana</b>?" in r[8] and tg.sent("sendMessage")[8]["reply_markup"]
    cfg2 = apply_settings(cmd_cfg(tmp_path), c.settings)
    assert [s.query for s in cfg2.searches] == ["iphone 13", "boss ds 1", "zoom g1x four"]
    assert cfg2.rules.min_roi == 25 and cfg2.budget == 60
    assert next(s for s in cfg2.searches if s.query == "boss ds 1").price_to == 60   # budget caps budget searches


def test_group_chat_bot_suffix_and_help(tmp_path: Path):
    tg, c, cfg, changed = run_cmds(tmp_path, [msg("/help@flipfinder_bot", chat=-100123), msg("hello there")])
    sent = tg.sent()
    assert len(sent) == 1 and sent[0]["chat_id"] == -100123 and "/setprice" in sent[0]["text"]
    assert not changed


def test_commands_registered_once_and_updates_confirmed(tmp_path: Path):
    from flipfinder.commands import Commands
    tg = FakeTG([msg("/rules")])
    c = Commands(tg, cmd_cfg(tmp_path), {"disabled": [], "prices": {}, "added": [], "removed": [], "rules": {},
                                         "allowed_users": [], "commands_version": 0})
    import os
    cwd = os.getcwd(); os.chdir(tmp_path)
    try:
        c.run()
    finally:
        os.chdir(cwd)
    from flipfinder.commands import COMMANDS_VERSION
    assert len(tg.sent("setMyCommands")) == 2 and c.settings["commands_version"] == COMMANDS_VERSION
    confirms = [p for m, p in tg.calls if m == "getUpdates" and "offset" in p]
    assert confirms and confirms[0]["offset"] > 0
    assert "min_rating: 5" in tg.sent()[0]["text"]


def test_price_change_keeps_search_known(tmp_path: Path):
    from flipfinder.config import Search
    from flipfinder.scanner import KnownSearches, PoolCache, _key
    s = Search("boss katana", 80, 300)
    (tmp_path / "searches.json").write_text(json.dumps({_key(s): 1.0}))   # old-style key with prices
    known = KnownSearches(tmp_path / "searches.json", PoolCache(tmp_path / "p.json", 60))
    s.price_from, s.price_to = 90, 260
    assert known.has(s)


# --- Several chats, supergroup upgrade, "I'm on it" ---

class FakeResponse:
    def __init__(self, ok, data):
        self.ok, self._data, self.status_code = ok, data, 200 if ok else 400
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(data)

    def json(self):
        return self._data


def test_deals_go_to_every_chat_with_claim_button(monkeypatch):
    import flipfinder.telegram as tgm
    sent = []

    def post(url, json=None, timeout=None):
        sent.append((url.rsplit("/", 1)[1], json))
        return FakeResponse(True, {"ok": True, "result": {"message_id": len(sent), "chat": {"id": json["chat_id"]}}})
    monkeypatch.setattr(tgm.requests, "post", post)
    tg = tgm.Telegram("TOKEN", "1000001, -100000000002")
    assert tg.chat_ids == ["1000001", "-100000000002"] and tg.chat_id == "1000001"
    d = evaluate(item(1, 40), pool([120] * 12), Rules())
    where = tg.send_deal(d)
    assert [w["chat"] for w in where] == ["1000001", "-100000000002"] and [w["id"] for w in where] == [1, 2]
    assert [p["chat_id"] for _, p in sent] == ["1000001", "-100000000002"]
    assert all(p["reply_markup"]["inline_keyboard"][0][0]["text"] == "I'm on it ✋" for _, p in sent)


def test_group_upgraded_to_supergroup_switches_id(monkeypatch):
    import flipfinder.telegram as tgm
    sent = []

    def post(url, json=None, timeout=None):
        sent.append(json["chat_id"])
        if json["chat_id"] == "-100000000002":
            return FakeResponse(False, {"ok": False, "error_code": 400, "description": "group chat was upgraded",
                                        "parameters": {"migrate_to_chat_id": -1001234567890}})
        return FakeResponse(True, {"ok": True, "result": {}})
    monkeypatch.setattr(tgm.requests, "post", post)
    tg = tgm.Telegram("TOKEN", "1000001,-100000000002")
    assert tg.send_text("hello")
    assert sent == ["1000001", "-100000000002", "-1001234567890"]          # retried on the new id
    assert tg.chat_ids == ["1000001", "-1001234567890"]
    assert tg.migrations == {"-100000000002": "-1001234567890"}
    from flipfinder.commands import apply_settings
    cfg = cmd_cfg_with_chat()
    apply_settings(cfg, {"chat_migrations": tg.migrations})
    assert cfg.telegram_chat_id == "1000001,-1001234567890"


def cmd_cfg_with_chat():
    from flipfinder.analyzer import Rules as R
    return type("C", (), {"searches": [], "telegram_chat_id": "1000001,-100000000002", "rules": R(), "budget": 72})()


def test_claim_button_shows_who(tmp_path: Path):
    claim = tap("claim", user=OWNER)                     # older alerts' single "I'm on it" button
    claim["callback_query"]["from"].update({"first_name": "Marco"})
    tg, c, cfg, changed = run_cmds(tmp_path, [tap("claim", user=4242), claim])   # 4242 isn't allowed: ignored
    edit = tg.sent("editMessageReplyMarkup")[0]
    label = edit["reply_markup"]["inline_keyboard"][0][0]
    assert label["text"].startswith("✋ Marco is on it") and label["callback_data"] == "claimed"
    answers = [p["text"] for p in tg.sent("answerCallbackQuery")]
    assert answers[0] == "It's yours, good luck!"
    assert not changed                                                      # claims don't touch settings



# --- Deal lifecycle, pool, votes ---

def _book_with_deal(tmp_path):
    from flipfinder.dealbook import DealBook
    book = DealBook(tmp_path / "deals.json")
    d = evaluate(titled(1, "Boss DS-1 distortion", 25, "Boss"), [titled(10 + i, "Boss DS-1 distortion", 60, "Boss") for i in range(12)],
                 Rules(min_profit=12, min_roi=35, max_roi=150))
    book.record(d, "🔥 <b>Boss DS-1 distortion</b>", [{"chat": "111", "id": 7, "photo": False},
                                                     {"chat": "-100", "id": 8, "photo": True}])
    return book, d.item.key


def run_with_book(tmp_path, book, updates):
    import os
    from flipfinder.commands import COMMANDS_VERSION, Commands, load_settings
    tg = FakeTG(updates)
    cwd = os.getcwd(); os.chdir(tmp_path)
    try:
        s = load_settings(tmp_path / "settings.json"); s["commands_version"] = COMMANDS_VERSION
        s["allowed_users"] = [555]
        Commands(tg, cmd_cfg(tmp_path), s, book=book).run()
    finally:
        os.chdir(cwd)
    return tg


def test_deal_lifecycle_with_prices_and_pool(tmp_path: Path):
    book, key = _book_with_deal(tmp_path)
    book.set_pool(300)
    claim = tap(f"c:{key}", user=555); claim["callback_query"]["from"]["first_name"] = "Marco"
    tg = run_with_book(tmp_path, book, [claim, tap(f"b:{key}", user=OWNER + 1)])
    d = book.data["deals"][key]
    assert d["status"] == "claimed" and d["who"] == "Marco"
    edits = tg.sent("editMessageText") + tg.sent("editMessageCaption")
    assert len(edits) == 2 and "✋ Claimed by Marco" in tg.sent("editMessageText")[0]["text"]
    assert tg.sent("editMessageCaption")[0]["reply_markup"]["inline_keyboard"][0][0]["text"] == "💸 Bought (Marco)"
    # a stranger's tap was ignored entirely; now Marco buys it: asked for the price, answers 30
    tg = run_with_book(tmp_path, book, [tap(f"b:{key}", user=555)])
    assert "How much did you pay" in tg.sent()[0]["text"] and "Known total" in tg.sent()[0]["text"]
    assert tg.sent()[0]["reply_markup"]["inline_keyboard"][0][0]["text"].startswith("✅ Use €")
    tg = run_with_book(tmp_path, book, [msg("abc", user=555), msg("30", user=555)])
    assert "need just the amount" in tg.sent()[0]["text"]
    assert "💸 Bought for €30.00 · pool now €270.00" in tg.sent()[1]["text"]
    assert d["status"] == "bought" and d["paid"] == 30 and book.pool == 270
    tg = run_with_book(tmp_path, book, [tap(f"l:{key}", user=555), tap(f"s:{key}", user=555), msg("75", user=555)])
    assert d["status"] == "sold" and d["sold_for"] == 75 and book.pool == 345
    assert "✅ Sold for €75.00, profit €45.00 · pool now €345.00" in tg.sent()[-1]["text"]
    assert "✅ Sold by Marco · paid €30.00 · sold for €75.00 · profit €45.00" in book.full_text(key)
    assert book.keyboard(key) == {"inline_keyboard": []}
    p = book.profit()
    assert p["total"] == 45 and p["month"] == 45 and p["people"] == {"Marco": 45} and p["sold"] == 1
    saved = json.loads((tmp_path / "deals.json").read_text(encoding="utf-8"))
    assert saved["deals"][key]["status"] == "sold"


def test_only_claimer_or_owner_advances_and_strangers_ignored(tmp_path: Path):
    book, key = _book_with_deal(tmp_path)
    tg = run_with_book(tmp_path, book, [tap(f"c:{key}", user=555), tap(f"b:{key}", user=777),
                                        tap(f"c:{key}", user=OWNER)])
    answers = [p["text"] for p in tg.sent("answerCallbackQuery")]
    assert answers == ["It's yours, good luck!", "user 555 already has this one"]   # 777 ignored
    tg = run_with_book(tmp_path, book, [tap(f"b:{key}", user=OWNER)])                # the owner may step in
    assert "How much did you pay" in tg.sent()[0]["text"]


def test_votes_feedback_and_seller_message(tmp_path: Path):
    book, key = _book_with_deal(tmp_path)
    tg = run_with_book(tmp_path, book, [tap(f"up:{key}", user=555), tap(f"dn:{key}", user=OWNER),
                                        tap(f"m:{key}", user=555)])
    kb = book.keyboard(key)["inline_keyboard"][1]
    assert [b["text"] for b in kb] == ["👍 1", "👎 1", "📩 Message seller"]
    fb = book.data["feedback"]
    assert len(fb) == 1 and fb[0]["title"] == "Boss DS-1 distortion" and fb[0]["url"].endswith("/items/1")
    seller = tg.sent()[0]["text"]
    assert "Ciao! L'articolo" in seller and "ancora disponibile" in seller and "video" in seller and "<code>" in seller


def test_stock_profit_pool_commands_and_budget_from_pool(tmp_path: Path):
    book, key = _book_with_deal(tmp_path)
    book.data["deals"][key].update(status="bought", who="Marco", who_id=555, paid=30, bought_at=1e10)
    tg = run_with_book(tmp_path, book, [msg("/pool"), msg("/pool abc"), msg("/pool 200"), msg("/stock"), msg("/profit")])
    r = [p["text"] for p in tg.sent()]
    assert "No pool set" in r[0] and "must be a number" in r[1] and "Pool set to €200.00" in r[2]
    assert "Boss DS-1 distortion" in r[3] and "Marco" in r[3] and "€30.00" in r[3]
    assert "Total: €0.00 (0 sold)" in r[4]
    from flipfinder.commands import set_budget
    cfg = cmd_cfg(tmp_path)
    set_budget(cfg, book.pool)
    assert book.pool == 170 and cfg.budget == 170   # bought after the pool started: 200 - 30
    from flipfinder.commands import effective_budget
    assert effective_budget(72, 170) == 72 and effective_budget(72, 50) == 50 and effective_budget(72, None) == 72



def test_budget_follows_the_pool_up_and_down(tmp_path: Path):
    from flipfinder.commands import apply_settings, set_budget
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



def test_bought_suggests_known_cost(tmp_path: Path):
    book, key = _book_with_deal(tmp_path)
    cost = book.data["deals"][key]["cost"]
    tg = run_with_book(tmp_path, book, [tap(f"c:{key}", user=555), tap(f"b:{key}", user=555)])
    q = tg.sent()[0]
    assert f"€{cost:,.2f}" in q["text"] and q["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"pay:{key}"
    tg = run_with_book(tmp_path, book, [tap(f"pay:{key}", user=OWNER)])          # not the owner's question
    assert tg.sent("answerCallbackQuery")[0]["text"].startswith("That's for")
    tg = run_with_book(tmp_path, book, [tap(f"pay:{key}", user=555)])
    d = book.data["deals"][key]
    assert d["status"] == "bought" and d["paid"] == cost
    assert f"💸 Bought for €{cost:,.2f}" in tg.sent()[-1]["text"]


def test_bought_typed_amount_overrides_suggestion(tmp_path: Path):
    book, key = _book_with_deal(tmp_path)
    run_with_book(tmp_path, book, [tap(f"c:{key}", user=555), tap(f"b:{key}", user=555)])
    run_with_book(tmp_path, book, [msg("27,50", user=555)])
    assert book.data["deals"][key]["paid"] == 27.5


def test_pool_shows_both_and_limit_is_the_smaller(tmp_path: Path):
    book, key = _book_with_deal(tmp_path)
    tg = run_with_book(tmp_path, book, [msg("/pool"), msg("/pool 50"), msg("/pool"), msg("/budget 40"), msg("/pool")])
    r = [p["text"] for p in tg.sent()]
    assert "No pool set" in r[0] and "Budget-mode limit: €72.00" in r[0]
    assert "Budget-mode limit: €50.00" in r[1]
    assert "Pool: €50.00" in r[2] and "/budget: €72.00" in r[2] and "<b>€50.00</b>" in r[2]
    assert "Budget is now €40" in r[3]
    assert "/budget: €40.00" in r[4] and "<b>€40.00</b>" in r[4]



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


def test_workflow_uses_latest_main_and_keeps_own_settings():
    import yaml
    wf = yaml.safe_load(Path(".github/workflows/flipfinder.yml").read_text(encoding="utf-8"))
    steps = wf["jobs"]["scan"]["steps"]
    assert steps[0]["uses"].startswith("actions/checkout") and steps[0]["with"]["ref"] == "main"
    save = next(x for x in steps if x.get("name", "").startswith("Save Telegram"))
    assert "-X theirs" in save["run"] and "deals.json" in save["run"]
    assert wf["permissions"]["contents"] == "write"


# --- Group features, part 2 ---

def test_quiet_hours_italy_time():
    from datetime import datetime, timezone
    from flipfinder.group import ROME, in_quiet_hours
    at = lambda h, m: datetime(2026, 10, 7, h, m, tzinfo=ROME)   # noqa: E731
    assert not in_quiet_hours(at(23, 59)) and in_quiet_hours(at(0, 0))
    assert in_quiet_hours(at(7, 29)) and not in_quiet_hours(at(7, 30))
    assert in_quiet_hours(datetime(2026, 1, 15, 6, 0, tzinfo=timezone.utc))      # 07:00 in Rome (winter)
    assert not in_quiet_hours(datetime(2026, 7, 15, 6, 0, tzinfo=timezone.utc))  # 08:00 in Rome (summer)


def test_reminder_schedule(tmp_path: Path):
    from flipfinder.group import due_reminders
    book, key = _book_with_deal(tmp_path)
    d = book.data["deals"][key]
    t0 = 1_000_000.0
    d.update(status="claimed", who="Marco", who_id=555, claimed_at=t0)
    assert due_reminders(book, t0 + 23 * 3600) == []
    assert due_reminders(book, t0 + 25 * 3600) == [("ping", key)]
    d["pinged_at"] = t0 + 25 * 3600
    assert due_reminders(book, t0 + 30 * 3600) == []                    # pinged once
    assert due_reminders(book, t0 + 49 * 3600) == [("release", key)]
    d["kept_at"] = t0 + 30 * 3600                                       # "keep it" restarts the clock
    assert due_reminders(book, t0 + 49 * 3600) == []
    d.update(status="bought", bought_at=t0)
    assert due_reminders(book, t0 + 2 * 86400) == [] and due_reminders(book, t0 + 3 * 86400) == [("list", key)]
    d.update(status="listed", listed_at=t0)
    assert due_reminders(book, t0 + 13 * 86400) == [] and due_reminders(book, t0 + 14 * 86400) == [("cut", key)]
    d["cut_at"] = t0 + 14 * 86400
    assert due_reminders(book, t0 + 18 * 86400) == [] and due_reminders(book, t0 + 21 * 86400) == [("cut", key)]


def run_reminders(tmp_path, book, now, value=None, updates=()):
    import os
    from flipfinder.commands import COMMANDS_VERSION, Commands, load_settings
    tg = FakeTG(list(updates))
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        s = load_settings(tmp_path / "settings.json")
        s["commands_version"] = COMMANDS_VERSION
        s["allowed_users"] = [555]
        c = Commands(tg, cmd_cfg(tmp_path), s, book=book, value_fn=lambda d: value)
        c.run()
        c.reminders(now)
    finally:
        os.chdir(cwd)
    return tg


def test_reminders_ping_keep_release_nudge_and_cut(tmp_path: Path):
    import time as _t
    book, key = _book_with_deal(tmp_path)
    d = book.data["deals"][key]
    now = _t.time()
    d.update(status="claimed", who="Marco", who_id=555, claimed_at=now - 25 * 3600)
    tg = run_reminders(tmp_path, book, now)
    ping = tg.sent()[0]
    assert 'href="tg://user?id=555">Marco</a>, still on it?' in ping["text"]
    assert [b["callback_data"] for b in ping["reply_markup"]["inline_keyboard"][0]] == [f"keep:{key}", f"rel:{key}"]
    run_reminders(tmp_path, book, now, updates=[tap(f"keep:{key}", user=777), tap(f"keep:{key}", user=555)])
    assert d.get("kept_at") and d["status"] == "claimed"
    d.update(claimed_at=now - 50 * 3600, kept_at=now - 49 * 3600)   # 49 h since the last "keep it"
    tg = run_reminders(tmp_path, book, now)
    assert d["status"] == "new" and "who" not in d
    assert "is free again" in tg.sent()[0]["text"] and tg.sent("editMessageText")       # deal message updated
    d.update(status="bought", who="Marco", who_id=555, paid=30, bought_at=now - 3 * 86400)
    tg = run_reminders(tmp_path, book, now)
    assert "Time to list it?" in tg.sent()[0]["text"] and f"/sell {d['n']}" in tg.sent()[0]["text"]
    d.update(status="listed", listed_at=now - 15 * 86400)
    tg = run_reminders(tmp_path, book, now, value=52.0)
    cut = tg.sent()[0]["text"]
    assert "listed for 15 days" in cut and "€52" in cut and "<b>€50</b>" in cut


def topic_msg(text, thread, created_name=None, user=OWNER):
    m = msg(text, user=user, chat=-100444)
    m["message"].update(message_thread_id=thread, is_topic_message=True)
    if created_name:
        m["message"]["reply_to_message"] = {"message_id": thread, "forum_topic_created": {"name": created_name}}
    return m


def test_topics_learned_from_messages_and_topic_command(tmp_path: Path):
    tg, c, cfg, changed = run_cmds(tmp_path, [
        topic_msg("/topic", 11, "🎸 Guitars"),
        topic_msg("/help", 22, "📱 Electronics"),       # any command in a topic teaches its id
        topic_msg("/topic summary", 44, "Riepilogo"),     # a topic with another name, named explicitly
        msg("/topic"),                                     # not in a topic
    ])
    assert c.settings["topics"] == {"guitars": 11, "electronics": 22, "summary": 44}
    texts = [p["text"] for m, p in tg.calls if m == "sendMessage"]
    assert any("This topic is now <b>Guitars</b>" in t for t in texts)
    assert any("Send /topic inside a group topic" in t for t in texts)
    assert changed


def test_send_to_uses_topic_in_group_only_and_falls_back(monkeypatch):
    import flipfinder.telegram as tgm
    sent = []

    def post(url, json=None, timeout=None):
        sent.append(dict(json))
        if json.get("message_thread_id") == 99:
            return FakeResponse(False, {"ok": False, "description": "Bad Request: message thread not found"})
        return FakeResponse(True, {"ok": True, "result": {"message_id": 1, "chat": {"id": json["chat_id"]}}})
    monkeypatch.setattr(tgm.requests, "post", post)
    tg = tgm.Telegram("TOKEN", "1000001,-100444")
    tg.topics = {"guitars": 11, "summary": 99}
    tg.send_text("deal", topic="guitars")
    assert "message_thread_id" not in sent[0] and sent[1]["message_thread_id"] == 11
    sent.clear()
    assert tg.send_text("summary", ["-100444"], topic="summary")
    assert sent[0]["message_thread_id"] == 99 and "message_thread_id" not in sent[1]   # retried in General


def test_sell_listing_by_number_or_name_in_three_languages(tmp_path: Path):
    book, key = _book_with_deal(tmp_path)
    d = book.data["deals"][key]
    d.update(status="bought", who="Marco", who_id=555, paid=30, condition="Ottime",
             title="🔥🔥 BOSS DS-1 Distortion pedale chitarra originale made in Taiwan anni 90 perfetto")
    tg = run_with_book(tmp_path, book, [msg(f"/sell {d['n']}"), msg("/sell boss ds-1 en"), msg("/sell ds-1 uk"),
                                        msg("/sell zoom g1x")])
    it, en, uk, missing = [p["text"] for p in tg.sent()]
    title = it.split("<code>")[1].split("</code>")[0]
    assert len(title) <= 60 and "🔥" not in title and title.startswith("BOSS DS-1 Distortion")
    assert "Condizioni: Ottime" in it and "Prezzo consigliato: <b>€60</b>" in it and "vendita veloce" in it
    assert "Condition: very good" in en and "Suggested price" in en
    assert "Стан: дуже добрий" in uk
    assert "No deal matching" in missing


def test_weekly_report_once_on_sunday_evening(tmp_path: Path):
    from datetime import datetime
    from flipfinder.group import ROME, weekly_due, weekly_report
    book, key = _book_with_deal(tmp_path)
    sun = datetime(2026, 10, 11, 20, 5, tzinfo=ROME)
    t = sun.timestamp()
    d = book.data["deals"][key]
    d.update(status="sold", who="Marco", who_id=555, paid=30, sold_for=75, sent=t - 86400,
             claimed_at=t - 80000, bought_at=t - 70000, sold_at=t - 3600, query="boss ds 1")
    book.data["deals"]["vinted:2"] = {"n": 2, "title": "Big Muff", "url": "u2", "status": "new", "sent": t - 5000,
                                      "query": "big muff", "votes": {}, "messages": [], "text": ""}
    book.data["feedback"] = [{"key": "vinted:2", "at": t - 4000}, {"key": "vinted:2", "at": t - 3000},
                             {"key": key, "at": t - 2000}]
    assert not weekly_due(book, datetime(2026, 10, 11, 19, 59, tzinfo=ROME))
    assert weekly_due(book, sun) and not weekly_due(book, datetime(2026, 10, 12, 20, 5, tzinfo=ROME))
    text = weekly_report(book, sun)
    assert "Deals found: 2 · claimed: 1 · bought: 1 · sold: 1" in text
    assert "Marco €45.00" in text and "Best flip" in text and "€30.00 → €75.00 (+€45.00)" in text
    assert "Most down-voted search: <b>big muff</b> (2×)" in text
    book.data["last_weekly"] = sun.date().isoformat()
    assert not weekly_due(book, sun)


def test_quiet_queue_sends_best_first_with_limit(tmp_path: Path):
    from flipfinder.dealbook import DealBook
    book = DealBook(tmp_path / "deals.json")
    pool_ = [titled(100 + i, "Boss DS-1", 100, "Boss") for i in range(12)]
    for i, price in enumerate([45, 30, 38], 1):
        book.queue(evaluate(titled(i, "Boss DS-1", price, "Boss"), pool_, Rules()), "alert")
    order = [book.data["deals"][k]["item"]["price"] for k in book.take_queue(2)]
    assert order == [30, 38]                                            # best (most profit) first
    assert len(book.data["queue"]) == 1                                 # the rest waits for the next run
    assert all(d["messages"] == [] and d["text"].startswith("alert\n🔢 #") for d in book.data["deals"].values())



def test_old_deals_get_numbers_on_load(tmp_path: Path):
    from flipfinder.dealbook import DealBook
    (tmp_path / "deals.json").write_text(json.dumps({"deals": {
        "vinted:2": {"title": "B", "url": "u", "status": "new", "sent": 20, "votes": {}, "messages": [], "text": ""},
        "vinted:1": {"title": "A", "url": "u", "status": "claimed", "sent": 10, "votes": {}, "messages": [], "text": ""},
    }}), encoding="utf-8")
    b = DealBook(tmp_path / "deals.json")
    assert b.data["deals"]["vinted:1"]["n"] == 1 and b.data["deals"]["vinted:2"]["n"] == 2 and b.data["next_n"] == 2
    assert b.find("1")[0] == "vinted:1" and b.changed



def test_budget_and_guitar_searches_take_turns(tmp_path: Path, monkeypatch):
    import flipfinder.scanner as sc_mod
    from flipfinder.config import Search
    s = _fake_scanner(tmp_path, 0, lambda searches: [])
    s.cfg.stagger = True
    budget = Search("boss ds 1", budget=True, every_minutes=10)
    guitar = Search("fender player stratocaster")
    phone = Search("iphone 13", filters={"catalog": [3661]})
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
    s.known.last[sc_mod._sid(budget)] = t0 + 330 - 13 * 60     # skipped runs: overdue, runs anyway
    assert s._turn(budget)
    s.cfg.stagger = False
    s.known.last[sc_mod._sid(guitar)] = t0 + 329
    assert s._turn(guitar)                                       # without stagger: every run as before


def test_gitignore_keeps_outputs_out():
    ignored = Path(".gitignore").read_text(encoding="utf-8").splitlines()
    assert "outputs/" in ignored and "Claude outputs/" in ignored



def test_topic_setup_pins_intros_and_intro_edits_them(tmp_path: Path):
    from flipfinder.group import INTROS
    tg, c, cfg, changed = run_cmds(tmp_path, [topic_msg("/topic", 11, "🎸 Guitars")])
    posted = c.settings["intros"]["-100444"]
    sends = tg.sent()
    assert [p["text"] for p in sends[1:]] == [INTROS["guitars"], INTROS["general"]]
    assert sends[1]["message_thread_id"] == 11 and "message_thread_id" not in sends[2]
    assert [p["message_id"] for p in tg.sent("pinChatMessage")] == [posted["guitars"]["id"], posted["general"]["id"]]
    s = c.settings
    tg, c, cfg, changed = run_cmds(tmp_path, [topic_msg("/topic summary", 44), msg("/intro", chat=-100444),
                                              msg("/intro", user=555, chat=-100444)], settings=s)
    texts = [p["text"] for p in tg.sent()]
    assert texts.count(INTROS["summary"]) == 1                   # new topic: posted once, then left alone
    assert INTROS["guitars"] not in texts and INTROS["general"] not in texts   # already there: not posted again
    assert "Intros pinned: Guitars, Summary, General" in texts[-1]
    assert "Not set up yet: Electronics, Budget" in texts[-1]
    assert len(tg.sent("pinChatMessage")) == 4                   # 1 for Summary + /intro re-pins all 3
    s["intros"]["-100444"]["guitars"]["text"] = "old intro"      # intro text changed since: edited in place
    tg, c, cfg, changed = run_cmds(tmp_path, [msg("/intro", chat=-100444)], settings=s)
    edits = tg.sent("editMessageText")
    assert [e["text"] for e in edits] == [INTROS["guitars"]] and not tg.sent()[:-1]


def test_intro_is_owner_only_and_hidden_from_help(tmp_path: Path):
    tg, c, cfg, changed = run_cmds(tmp_path, [msg("/intro", user=555), msg("/help", user=555)],
                                   settings={"allowed_users": [555]})
    texts = [p["text"] for p in tg.sent()]
    assert "Only the owner can use /intro" in texts[0] and "/intro" not in texts[1] and "/allow" not in texts[1]
