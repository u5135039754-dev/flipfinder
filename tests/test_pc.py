"""Gaming PCs: specs read from the listing, the minimums, valuing on sold PCs of the same combo, the card, the scan."""

import time
from dataclasses import replace

from flipfinder import config, fastlane, pc
from flipfinder.analyzer import Rules, assess
from flipfinder.scanner import Scanner, new_pc_stats
from flipfinder.settings import search_group
from flipfinder.telegram import format_deal
from flipfinder.vinted import Item

R = Rules(min_profit=50, min_roi=40, min_rating=1, min_comparables=8, max_roi=150, max_price=200, model="pc",
          pc_form="desktop", pc_ship_min=15, repairs=False, shipping_cost=25, resell_costs=10)


def it(i, price, title, **kw):
    return Item(id=i, title=title, price=price, total_price=price, currency="EUR", url=f"https://x/{i}", **kw)


def sold(i, price, title):
    return (it(i, price, title), 5.0, time.time() - 86400)


def test_specs_from_titles_and_descriptions():
    s = pc.parse("PC ufficio gaming i5 6500 1050 ti", "8 giga RAM ddr4\nGTX 1050 ti\nSSD kinkstone 480 GB")
    assert pc.line(s) == "i5-6500 · GTX 1050 Ti · 8 GB DDR4 · 480 GB SSD · WiFi ❓ BT ❓" and not pc.problems(s)
    s = pc.parse("PC i5 9400F GTX 1660S 16gb ssd 1tb wifi bluetooth")
    assert pc.line(s) == "i5-9400F · GTX 1660 Super · 16 GB DDR4 · 1 TB SSD · WiFi ✅ BT ✅"
    assert pc.combo(s) == "desktop · gtx 1660 super · i5"
    # the card's memory isn't the PC's RAM ("GTX 1650 8GB": a 1650 has 4 GB, so 8 is RAM; "RTX 3060 12GB" is the card)
    assert pc.parse("Notebook MSI GF63 i5-9300H GTX 1650 8GB SSD 256GB")["ram_gb"] == 8
    assert pc.parse("PC Ryzen 5 RTX 3060 12GB, SSD 512")["ram_gb"] is None
    assert pc.parse("Asus TUF i7-10750H RTX 3060 16GB 512GB SSD")["ram_gb"] == 16
    # "16gb ssd 500": 16 GB of RAM and a 500 GB SSD; a laptop by its words
    s = pc.parse("PC i5 10400 GTX 1060 16gb ssd 500")
    assert (s["ram_gb"], s["ssd_gb"]) == (16, 500)
    assert pc.parse("Notebook gaming Lenovo Legion")["form"] == "laptop"


def test_the_minimums():
    def why(title, desc=""):
        return pc.problems(pc.parse(title, desc))
    assert why("PC Gaming i5-6600K 16 GB RAM GTX 750 Ti SSD 512") == ["GTX 750 Ti is under a GTX 1050"]
    assert why("PC i7 4790 GTX 1060 16gb ssd 500") == ["DDR3 RAM"]                 # a 4th-gen Intel takes DDR3
    assert why("PC i5 9400 GTX 1060 16 GB DDR3 SSD 512") == ["DDR3 RAM"]
    assert why("PC GT 1030 i3 8100 8GB SSD 240") == ["GT 1030 is under a GTX 1050", "240 GB SSD (under 256)"]
    assert why("PC Ryzen 5 3600 GTX 1660 16 GB HDD 1 TB") == ["HDD only"]
    assert why("PC Ryzen 5 3600 GTX 1660 4 GB RAM SSD 512") == ["4 GB RAM"]
    assert why("PC i5 8400 Intel UHD 630 16 GB SSD 512") == ["integrated graphics"]
    assert why("PC Xeon E5-2670 v3 GTX 1070 32GB SSD 480GB no porta ethernet") == ["no Ethernet"]
    assert why("PC Ryzen 5 RX 580 16 GB SSD 512 + HDD 1TB") == []                     # SSD + HDD is fine
    for ok in ("gtx 1050", "gtx 1050 ti", "gtx 1060", "gtx 1660 super", "rtx 2060", "rtx 3050", "rx 470", "rx 580",
               "rx 6600 xt", "vega 56", "arc a380", "arc b580"):
        assert pc.gpu_ok(ok), ok
    for weak in ("gt 710", "gt 730", "gt 1030", "gtx 960", "gtx 970", "gtx 750", "gtx 1630", "rx 560", "rx 550", "mx 250",
                 "arc a310"):
        assert not pc.gpu_ok(weak), weak


def test_wifi_bluetooth_and_the_dongle():
    desk = pc.parse("PC Ryzen 5 3600 RTX 2060 16 GB SSD 512")
    assert pc.needs_dongle(desk) and pc.line(desk).endswith("WiFi ❓ BT ❓")
    assert not pc.needs_dongle(pc.parse("PC Ryzen 5 3600 RTX 2060 16 GB SSD 512 WiFi Bluetooth"))
    assert pc.parse("PC RTX 2060 senza wifi")["wifi"] is False
    laptop = pc.parse("Notebook Acer Nitro i5-9300H GTX 1650 8 GB SSD 512", form="laptop")
    assert not pc.needs_dongle(laptop)                                   # laptops have both


def test_the_ai_fills_only_what_the_patterns_missed():
    s = pc.parse("MSI MAG", "i5 10400f, 8gb ram, gtx 1660 super")
    assert pc.needs_ai(s)                                                 # no SSD in the text
    m = pc.merge(s, {"ssd_gb": 512, "gpu": "RTX 4090", "ram_gb": 64, "ethernet": False, "wifi": True})
    assert (m["ssd_gb"], m["gpu"], m["ram_gb"], m["wifi"], m["no_ethernet"], m["ai"]) == (512, "gtx 1660 super", 8, True, True, True)
    assert pc.problems(m) == ["no Ethernet"]


def test_valued_on_what_the_same_combo_sold_for():
    item = it(1, 150, "PC gaming i5 9400F GTX 1660 Super 16GB SSD 512")
    item.specs = pc.parse(item.title)
    combo_sold = [sold(100 + n, p, "PC i5 10400F GTX 1660 Super 16GB") for n, p in enumerate((290, 300, 310, 320))]
    others = [sold(200 + n, 600, "PC i7 RTX 3070 32GB") for n in range(5)] + [sold(300, 180, "PC i5 GTX 1060")]
    asking = [it(400 + n, 450, "PC i5 9600K GTX 1660 Super") for n in range(10)]   # asking prices don't count
    d = assess(item, asking, replace(R, sold=combo_sold + others), "pc gaming", shipping=8)
    assert d.market_value == 305 and d.sold_count == 4 and d.comparables == 4
    # €150 + €15 courier (a tower: never under €15) + €10 dongle + €10 packing on resale
    assert d.shipping == 15 and d.missing_part == 10 and d.cost == 175
    assert d.profit == 120 and not d.blocked
    assert d.pc == {"form": "desktop", "line": "i5-9400F · GTX 1660 Super · 16 GB DDR4 · 512 GB SSD · WiFi ❓ BT ❓",
                    "dongle": True, "combo": "desktop · gtx 1660 super · i5", "pickup": False, "ai": False}
    # too few sold: no value (asking prices only with pc.asking_factor)
    assert assess(item, asking, replace(R, sold=combo_sold[:2]), "pc gaming") is None
    d = assess(item, asking, replace(R, sold=combo_sold[:2], pc_asking_factor=0.85), "pc gaming", shipping=8)
    assert d.market_value == round(450 * 0.85, 2) and d.sold_count == 0
    # the CPU not stated: the same card, any CPU
    nocpu = it(2, 150, "PC gaming GTX 1660 Super 16GB SSD 512")
    nocpu.specs = pc.parse(nocpu.title)
    d = assess(nocpu, [], replace(R, sold=combo_sold + [sold(500, 300, "PC Ryzen 5 GTX 1660 Super")]), "pc gaming")
    assert d.sold_count == 5 and d.pc["combo"] == "desktop · gtx 1660 super · any CPU"
    # a pickup: no courier, no shipping floor
    d = assess(item, [], replace(R, sold=combo_sold), "pc gaming", pickup_only=True)
    assert d.shipping == 0 and d.pc["pickup"]


def test_the_deal_card():
    item = it(1, 150, "PC gaming i5 9400F GTX 1660 Super 16GB SSD 512", condition="Ottime condizioni")
    item.specs = pc.parse(item.title)
    sold_list = [sold(100 + n, p, "PC i5 10400F GTX 1660 Super 16GB") for n, p in enumerate((290, 300, 310, 320))]
    text = format_deal(assess(item, [], replace(R, sold=sold_list), "pc gaming", shipping=8))
    assert "🖥 <b>Desktop</b> · i5-9400F · GTX 1660 Super · 16 GB DDR4 · 512 GB SSD · WiFi ❓ BT ❓" in text
    assert "📶 WiFi/Bluetooth not stated: ask the seller · +€10.00 for a USB dongle (included)" in text
    assert "🏷 Resale: <b>€305.00</b> (median of 4 sold GTX 1660 Super + i5 desktops)" in text
    assert "desktop · gtx" not in text                                    # the combo isn't shown as an extra


def test_searches_electronics_topic_italy_only_and_not_in_the_fast_lane():
    cfg = config.load()
    pcs = [s for s in cfg.searches if s.model == "pc"]
    assert len(pcs) == 9 and {search_group(s) for s in pcs} == {"Electronics"}
    assert {s.max_price for s in pcs} == {200}
    sc = Scanner(cfg)
    r = sc._rules(next(s for s in pcs if s.query == "pc gaming"))
    assert (r.model, r.pc_form, r.abroad, r.repairs, r.max_price, r.pc_ship_min) == ("pc", "desktop", False, False, 200, 15)
    assert sc._rules(next(s for s in pcs if s.query == "notebook gtx")).pc_form == "laptop"
    assert sc._ebay_category(pcs[0]) == 179 and sc._subito_category(pcs[0]) == 10
    fs = {**fastlane.DEFAULTS, "groups": ["Electronics"]}
    assert not [s for s in fastlane.searches_for(cfg, fs, search_group) if s.model == "pc"]
    assert pc.is_pc_title("PC gaming Ryzen 5 RTX 2060", "desktop") and not pc.is_pc_title("Monitor gaming 27 pollici")
    assert pc.is_pc_title("Notebook MSI GTX 1650", "laptop") and not pc.is_pc_title("Notebook MSI GTX 1650", "desktop")


def test_the_scan_reads_specs_opens_a_few_asks_the_ai_and_skips_abroad(tmp_path, monkeypatch):
    cfg = config.load()
    cfg.seen_file = tmp_path / "seen.json"
    cfg.pc = {**cfg.pc, "details_per_run": 3, "ai_per_run": 5}
    s = next(x for x in cfg.searches if x.query == "pc gaming")
    sc = Scanner(cfg)
    sc.seen.add("vinted:0")
    sc.first_run = False
    sc.pc_stats = new_pc_stats()
    sold_list = [sold(100 + n, p, "PC i5 10400F GTX 1660 Super 16GB") for n, p in enumerate((290, 300, 310, 320))]
    rules = replace(sc._rules(s), sold=sold_list, min_rating=1)
    fresh = [it(1, 150, "PC i5-6600K GTX 750 Ti 16 GB SSD 512"),           # weak card in the title: never opened
             it(2, 150, "PC gaming i5 9400F GTX 1660 Super"),               # opened, the AI finds RAM and SSD
             it(3, 140, "Pc gaming"),                                       # no card in the title: last, waits
             it(4, 150, "PC i5 9400F GTX 1660 Super 16GB SSD 512"),         # opened: abroad
             it(5, 150, "PC gaming i5 GTX 1660 Super 16GB SSD 512")]        # opened: every spec in the title
    pages = {2: ("Funziona benissimo, ram e disco nuovi", "IT"), 4: ("Perfetto", "FR"), 5: ("Wifi e bluetooth", "IT")}
    opened = []

    def fake_open(item, form):
        opened.append(item.id)
        text, country = pages[item.id]
        item.country = country
        return pc.parse(item.title, text, form=form), {"shipping": 8.0, "pickup_only": False, "city": "", "text": text,
                                                      "attributes": ""}

    class Cloud:
        asked = []

        def pc_specs(self, listings):
            self.asked += [x["key"] for x in listings]
            return {"vinted:2": {"ram_gb": 16, "ram_type": "DDR4", "ssd_gb": 512, "wifi": True, "bt": True}}

    monkeypatch.setattr(sc, "_pc_open", fake_open)
    sc.cloud = Cloud()
    deals = sc._evaluate_pc(s, fresh, [], rules)
    assert opened == [2, 4, 5]                                              # a good card in the title first
    assert sc.cloud.asked == ["vinted:2"]                                   # only what the patterns couldn't read
    assert [d.item.id for d in deals] == [2, 5] and deals[0].pc["dongle"] is False
    st = sc.pc_stats
    assert (st["found"], st["opened"], st["waiting"], st["ai"], st["passed"]) == (5, 3, 1, 1, 2)
    assert st["skipped"] == {"weak card": 1, "abroad": 1}
    assert "vinted:3" not in sc.seen.data and "vinted:1" in sc.seen.data   # the waiting one comes back next run


def test_listings_clearly_in_another_language_are_from_abroad():
    assert pc.foreign("Ich verkaufe meinen Gaming-PC wegen Nichtbenutzung. Der PC wurde 2016 gekauft")
    assert pc.foreign("Ordenador de sobremesa en perfecto estado, montado en torre nueva")
    assert pc.foreign("PC Gamer Intel, RX 580 8Go, 16Go RAM, SSD 512Go avec Windows")
    assert not pc.foreign("Vendo PC gaming, perfettamente funzionante, scheda video GTX 1660 con la scatola")
    assert not pc.foreign("Gaming PC i5 GTX 1060 16GB SSD")                 # not enough to tell: the profile decides
