import json
from pathlib import Path

from flipfinder.analyzer import Rules, evaluate, market_value, rate, remove_outliers
from flipfinder.storage import SeenStore
from flipfinder.telegram import format_deal
from flipfinder.vinted import Item, parse_catalog_page


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
