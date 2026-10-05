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
