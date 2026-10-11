"""Mercatino Musicale: the permission's limits (public pages only, the daily cap, the delay, the contact), parsing, the scan."""

import pytest

from flipfinder import config
from flipfinder.mercatino import MercatinoClient, parse_search_page
from flipfinder.scanner import Scanner


def box(i, title, price, cond="USATO PRIVATO", place="Trento", desc="Ottime condizioni, spedisco"):
    return (f'<a href="/mm/a_x_id{i}.html"  class="box_prod_link"  data-tracking="x" ><span class="inf"><span class="tit">'
            f'<span>{title}</span></span><span class="desc">{desc}</span></span><span class="opt"><span class="prz">'
            f'<span class="u">&euro;</span>{price}<span class="dec"><span class="comma">,</span>00</span></span>'
            f'<span class="cond cond_6">{cond}</span><span class="usr">someone</span><span class="place">{place}</span></span></a>')


SPONSORED = '<a href="https://shop.example/product.html?utm_source=mm" class="box_prod_link"><span class="tit"><span>New one</span></span></a>'


def page(*boxes):
    return "<html>" + SPONSORED + "".join(boxes) + "</html>"


def test_parsing_skips_sponsored_shop_boxes_and_keeps_no_photos():
    items = parse_search_page(page(box(1, "Jackson JS32 King V", "1.250"), box(2, "Boss Katana 50", 180, cond="NUOVO")))
    assert [(i.id, i.title, i.price, i.condition, i.location, i.source) for i in items] == [
        (1, "Jackson JS32 King V", 1250.0, "Usato privato", "Trento", "mercatino"), (2, "Boss Katana 50", 180.0, "Nuovo", "Trento", "mercatino")]
    assert all(not i.photo and not i.photos for i in items)      # their images are never passed on
    assert items[0].url == "https://www.mercatinomusicale.com/mm/a_x_id1.html"


def test_the_permission_limits(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        MercatinoClient("", tmp_path / "m.json")                   # no contact email: no client at all
    c = MercatinoClient("me@example.com", tmp_path / "m.json", daily_cap=2, delay=(0, 0))
    assert "contact: me@example.com" in c.session.headers["User-Agent"]
    with pytest.raises(ValueError):
        c._get("/ajax/anything", {})                               # never /ajax/
    with pytest.raises(ValueError):
        c._get("/my/account.asp", {})                              # nor anything that isn't a public listing page
    sleeps = []
    monkeypatch.setattr("flipfinder.mercatino.time.sleep", lambda s: sleeps.append(s))

    class R:
        status_code, content = 200, page(box(1, "x", 10)).encode()
        def raise_for_status(self): pass
    monkeypatch.setattr(c.session, "get", lambda *a, **k: R())
    c.search("boss"), c.search("boss")
    assert len(sleeps) == 2 and c.pages_today == 2
    with pytest.raises(RuntimeError):
        c.search("boss")                                           # the daily cap
    assert MercatinoClient("me@example.com", tmp_path / "x.json").delay == (5.0, 10.0)


def test_scan_seeds_first_then_alerts_new_used_listings_against_its_own_comparables(tmp_path, monkeypatch):
    cfg = config.load()
    cfg.seen_file = tmp_path / "seen.json"
    cfg.subito.enabled = False
    cfg.mercatino.enabled, cfg.mercatino.contact, cfg.mercatino.pages_per_run = True, "me@example.com", 1
    cfg.searches = [s for s in cfg.searches if s.query == "boss katana"]
    sc = Scanner(cfg)
    sc.first_run = False
    sc.seen.add("vinted:0")
    market = [box(100 + n, "Boss Katana 50 MkII", p) for n, p in enumerate([200, 210, 190, 205, 195, 200])]
    pages = [page(*market), page(box(1, "Boss Katana 50 MkII", 110), box(2, "Boss Katana 50 MkII", 100, cond="NUOVO"),
                                 box(3, "Boss Katana 50 MkII", 105, desc="solo ritiro a mano"), *market)]
    monkeypatch.setattr(sc.mercatino, "_get", lambda path, params: pages.pop(0))
    assert sc._scan_mercatino() == []                              # first page: remembered, no alerts
    deals = sc._scan_mercatino()
    assert [d.item.id for d in deals] == [1]                       # not the new (shop) one, not the pickup-only one
    d = deals[0]
    assert d.item.source == "mercatino" and d.market_value == 200 and not d.shipping_known
