"""
Gaming PCs (Electronics): desktops and laptops, valued by what the same CPU + graphics card combo sold for.

Specs come from the title, the description and the listing's attributes, read here with patterns; when the RAM,
the graphics card or the SSD can't be found that way, the Worker's AI (Haiku) reads the description instead
(main.py, scanner.Scanner._pc_specs). A PC is only posted when every minimum is met:

- RAM 8 GB or more, DDR4 or newer (DDR3 is skipped; when the listing doesn't say, the CPU's generation does)
- a graphics card from GPU_OK: GTX 1050 or better (integrated graphics, GT 710/730/1030, GTX 9xx and older skipped)
- an SSD of 256 GB or more (HDD only is skipped, SSD + HDD is fine)
- Ethernet: only "no Ethernet" said outright is skipped. WiFi/Bluetooth not mentioned: posted with "ask the seller"
  and €10 for a USB dongle on the cost (laptops always have both)
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

DONGLE = 10.0                  # a USB WiFi + Bluetooth dongle, when the listing doesn't say it has them
MIN_RAM_GB = 8
MIN_SSD_GB = 256
DESKTOP_CATALOG, LAPTOP_CATALOG = 3581, 3580   # Vinted: Desktop computer, Computer portatili


def _t(text: str) -> str:
    """Lowercase, no accents, separators as spaces, "gtx1060" -> "gtx 1060", "16gb" -> "16 gb"."""
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[|/,;:()\[\]+*•·–—_]+", " ", t)
    t = re.sub(r"\b(gtx|rtx|rx|gt|arc|mx)-?(\d)", r"\1 \2", t)
    t = re.sub(r"(\d)\s*(?:giga ?bytes?|gigabytes?|gigas?)\b", r"\1 gb", t)
    t = re.sub(r"(\d)\s*(?:tera ?bytes?|tera)\b", r"\1 tb", t)
    t = re.sub(r"(\d)\s*(gb|go|g|tb|to)\b", r"\1 \2", t)
    t = re.sub(r"(\d)(ti|super|xt|s)\b", r"\1 \2", t)
    return re.sub(r"\s+", " ", t).strip()


# --- the graphics card --------------------------------------------------------------------------------------
_NV = re.compile(r"\b(?:(gtx|rtx|gt|mx)\s?)(\d{3,4})(?:\s?(ti|super|s))?\b")
_NV_BARE = re.compile(r"\b(?:geforce|nvidia|scheda video|scheda grafica|gpu|graphics card|carte graphique|grafikkarte)"
                      r"\s+(?:gtx\s|rtx\s)?(\d{4})(?:\s?(ti|super|s))?\b")
_AMD = re.compile(r"\b(?:radeon\s)?rx\s?(\d{3,4})(?:\s?(xt|xtx|gre))?\b")
_VEGA = re.compile(r"\bvega\s?(56|64)\b")
_ARC = re.compile(r"\barc\s?([ab])\s?(\d{3})\b")
_INTEGRATED = re.compile(r"\b(?:intel\s(?:u?hd|iris)|uhd graphics|hd graphics|integrat[ae]|onboard|radeon vega\s?[3-8]\b"
                         r"|grafica integrata|scheda video integrata)")
_NV_PREFIX = {"10": "gtx", "16": "gtx", "20": "rtx", "30": "rtx", "40": "rtx", "50": "rtx"}


def _nv_name(prefix: str, num: str, suffix: str | None) -> str:
    sfx = {"s": "super"}.get(suffix or "", suffix or "")
    return " ".join(x for x in (prefix, num, sfx) if x)


def gpus_in(text: str) -> list[str]:
    """Every graphics card named in the text, as 'gtx 1660 super', 'rtx 3060 ti', 'rx 580', 'arc a750' (first first)."""
    t = _t(text)
    found: list[tuple[int, str]] = []
    for m in _NV.finditer(t):
        prefix, num, sfx = m.group(1), m.group(2), m.group(3)
        if prefix == "mx" or (prefix == "gt" and len(num) <= 4):
            found.append((m.start(), f"{prefix} {num}"))                 # weak: gt 1030, mx 250
        else:
            found.append((m.start(), _nv_name(prefix, num, sfx)))
    for m in _NV_BARE.finditer(t):
        num = m.group(1)
        if num[:2] in _NV_PREFIX:
            found.append((m.start(), _nv_name(_NV_PREFIX[num[:2]], num, m.group(2))))
    for m in _AMD.finditer(t):
        found.append((m.start(), " ".join(x for x in ("rx", m.group(1), m.group(2)) if x)))
    for m in _VEGA.finditer(t):
        found.append((m.start(), f"vega {m.group(1)}"))
    for m in _ARC.finditer(t):
        found.append((m.start(), f"arc {m.group(1)}{m.group(2)}"))
    out: list[str] = []
    for _, name in sorted(found):
        if name not in out:
            out.append(name)
    return out


def gpu_ok(name: str) -> bool:
    """GTX 1050 / 1050 Ti and up, GTX 16xx, any RTX, RX 470/480/570/580/590 and newer RX, Vega 56/64, Arc A380 and up."""
    m = re.fullmatch(r"(gtx|rtx|gt|mx|rx|vega|arc) ?([ab]?)(\d+)(?: .*)?", name or "")
    if not m:
        return False
    kind, letter, num = m.group(1), m.group(2), int(m.group(3))
    if kind == "rtx":
        return True
    if kind == "gtx":
        return num in (1050, 1060, 1070, 1080) or 1630 < num < 1700   # 1650 / 1660 (not the 1630)
    if kind == "rx":
        return num in (470, 480, 570, 580, 590) or num >= 5500
    if kind == "vega":
        return True
    if kind == "arc":
        return (letter == "a" and num >= 380) or letter == "b"
    return False   # gt 710/730/1030, mx


def best_gpu(names: list[str]) -> str | None:
    """The card a listing is selling: the first one that qualifies (a listing naming 'ex gtx 960' is usually upgraded)."""
    ok = [n for n in names if gpu_ok(n)]
    return ok[0] if ok else (names[0] if names else None)


# --- the CPU ------------------------------------------------------------------------------------------------
_INTEL = re.compile(r"\b(?:intel\s)?(?:core\s)?(i[3579])\s?-?\s?(\d{4,5})([a-z]{0,2})\b")
_INTEL_GEN = re.compile(r"\b(?:intel\s)?(?:core\s)?(i[3579])\b(?:[^.\n]{0,25}?\b(\d{1,2})\s?(?:th|a|°|ª|nd|rd|st)?\s?(?:gen|generazione|generation|gen\.)\b)?")
_ULTRA = re.compile(r"\b(?:core\s)?ultra\s?([579])\s?(\d{3})([a-z]?)\b")
_RYZEN = re.compile(r"\bryzen\s?([3579])\s?(?:pro\s)?(\d{4})([a-z0-9]{0,3})\b|\bryzen\s?([3579])\b")
_XEON = re.compile(r"\bxeon\s?(?:e\s?([35]))?\s?-?\s?(\d{4})\s?(?:v\s?(\d))?")
_OLD_AMD = re.compile(r"\b(?:amd\s)?(?:fx\s?-?\s?\d{4}|phenom|athlon\s?(?:ii|x[24])|a(?:4|6|8|10)\s?-?\s?\d{4})\b")


def cpu_in(text: str) -> dict | None:
    """{'name': 'i5-9400F', 'tier': 'i5', 'gen': 9, 'ddr4': True/False/None} or None."""
    t = _t(text)
    m = _INTEL.search(t)
    if m:
        tier, digits, sfx = m.group(1), m.group(2), m.group(3)
        gen = int(digits[:2]) if len(digits) == 5 else int(digits[0])
        return {"name": f"{tier}-{digits}{sfx.upper()}", "tier": tier, "gen": gen, "ddr4": gen >= 6}
    m = _ULTRA.search(t)
    if m:
        return {"name": f"Ultra {m.group(1)} {m.group(2)}{m.group(3).upper()}", "tier": f"ultra {m.group(1)}",
                "gen": 100, "ddr4": True}
    m = _RYZEN.search(t)
    if m:
        if m.group(2):
            return {"name": f"Ryzen {m.group(1)} {m.group(2)}{m.group(3).upper()}", "tier": f"ryzen {m.group(1)}",
                    "gen": int(m.group(2)[0]), "ddr4": True}
        return {"name": f"Ryzen {m.group(4)}", "tier": f"ryzen {m.group(4)}", "gen": None, "ddr4": True}
    m = _XEON.search(t)
    if m:
        v = int(m.group(3)) if m.group(3) else None
        return {"name": f"Xeon {('E' + m.group(1) + '-') if m.group(1) else ''}{m.group(2)}{f' v{v}' if v else ''}",
                "tier": "xeon", "gen": v, "ddr4": None if v is None else v >= 3}
    if _OLD_AMD.search(t):
        return {"name": "older AMD", "tier": "old amd", "gen": None, "ddr4": False}
    m = _INTEL_GEN.search(t)
    if m:
        gen = int(m.group(2)) if m.group(2) else None
        return {"name": m.group(1) + (f" {gen}th gen" if gen else ""), "tier": m.group(1), "gen": gen,
                "ddr4": None if gen is None else gen >= 6}
    return None


# --- RAM, storage, connectivity -------------------------------------------------------------------------------
_RAM = [
    re.compile(r"\b(\d{1,3}) ?(?:gb|go|g)\b ?(?:di )?(?:ram|memoria|memory|ddr ?[345])"),
    re.compile(r"\b(?:ram|memoria ram|memoria|memory|memorias?|ddr ?[345])\s?(?:ddr ?[345]\s?)?(?:da |de |di )?"
               r"(?:[a-z]+ ){0,2}?(\d{1,3}) ?(?:gb|go|g)\b"),
]
_DDR = re.compile(r"\bddr ?([2345])l?\b")
_SIZE = r"(\d{1,4}(?:[.,]\d)?) ?(gb|go|g|tb|to|t)\b"
_SSD_BEFORE = re.compile(r"\b(?:ssd|nvme|m\.? ?2|m2|solid state)\s?(?:nvme\s|m\.?2\s|da |de |di )?(?:[a-z][a-z0-9]* ){0,3}?" + _SIZE)
_SSD_AFTER = re.compile(r"\b" + _SIZE + r" ?(?:di )?(?:ssd|nvme|m\.? ?2\b|m2)")
_HDD = re.compile(r"\b(?:hdd|hard ?disk|disco ?rigido|disque dur|festplatte)\b|\b" + _SIZE + r" ?(?:hdd|hard disk)")
_NO = r"(?:no|senza|non ha|niente|without|sans|sin|ohne|manca|mancante)"
_WIFI = re.compile(r"\b(?:wi ?-? ?fi|wifi|wireless|wlan|802\.11)\b")
_NO_WIFI = re.compile(rf"\b{_NO}\s(?:il\s|la\s|scheda\s|modulo\s)?(?:wi ?-? ?fi|wifi|wireless|wlan)\b|\b(?:wi ?-? ?fi|wifi) (?:non presente|assente|non incluso)\b")
_BT = re.compile(r"\b(?:bluetooth|bt ?[45](?:\.\d)?)\b")
_NO_BT = re.compile(rf"\b{_NO}\s(?:il\s)?bluetooth\b|\bbluetooth (?:non presente|assente)\b")
_NO_ETH = re.compile(rf"\b{_NO}\s(?:porta\s|presa\s|attacco\s)?(?:ethernet|lan|rj ?45)\b|\b(?:ethernet|porta lan) (?:non funziona|rotta|assente|non presente)\b")
_LAPTOP = re.compile(r"\b(?:notebook|portatile|laptop|portable|portatil|portatile gaming|ultrabook)\b|\b1[3-8](?:[.,]\d)? ?(?:\"|pollici|inch|zoll)")


def _gb(num: str, unit: str) -> int:
    n = float(num.replace(",", "."))
    return int(n * 1000) if unit in ("tb", "to", "t") else int(n)


RAM_SIZES = (4, 6, 8, 12, 16, 20, 24, 32, 48, 64, 96, 128)
# a bare "16 gb" that is the graphics card's memory ("rtx 3060 12 gb", "8 gb gddr6") or storage ("16 gb ssd")
_AFTER_GPU = re.compile(r"\b(?:gtx|rtx|rx|gt|arc)\s?([ab]?\d{3,4})(?:\s(?:ti|super|xt|xtx))?\s?$")
# the memory sizes each card comes with: "gtx 1650 8 gb" is the PC's RAM (a 1650 has 4), "gtx 1060 6 gb" the card's
VRAM = {"1030": {2}, "1050": {2, 3, 4}, "1060": {3, 5, 6}, "1070": {8}, "1080": {8, 11}, "1630": {4}, "1650": {4},
        "1660": {6}, "2050": {4}, "2060": {6, 12}, "2070": {8}, "2080": {8, 11}, "3050": {4, 6, 8}, "3060": {6, 8, 12},
        "3070": {8}, "3080": {8, 10, 12}, "4050": {6}, "4060": {8, 16}, "4070": {8, 12}, "470": {4, 8}, "480": {4, 8},
        "570": {4, 8}, "580": {4, 8}, "590": {8}, "5500": {4, 8}, "5600": {6}, "5700": {8}, "6500": {4}, "6600": {8},
        "6650": {8}, "6700": {10, 12}, "6750": {12}, "7600": {8}, "a380": {6}, "a580": {8}, "a750": {8}, "a770": {8, 16},
        "b570": {10}, "b580": {12}, "710": {1, 2}, "730": {2, 4}, "750": {1, 2, 4}, "950": {2, 4}, "960": {2, 4},
        "970": {4}, "980": {4, 6}}


def _is_vram(size: int, before: str) -> bool:
    m = _AFTER_GPU.search(before)
    if not m:
        return False
    return size in VRAM.get(m.group(1), set(range(1, VRAM_MAX + 1)))
_NOT_RAM_BEFORE = re.compile(r"(?:\bvram|\bgddr ?\d|\bssd|\bnvme|\bhdd)\s?$")
_NOT_RAM_AFTER = re.compile(r"^\s?(?:di\s)?(?:vram|gddr|emmc|storage|speicher|rom|di memoria interna)\b")
_BARE_GB = re.compile(r"\b(\d{1,3}) gb\b")
VRAM_MAX = 12   # "rtx 3060 12 gb" is the card's memory; "rtx 3060 16 gb" is the PC's RAM


def ram_in(t: str) -> int | None:
    sizes = [int(m.group(1)) for rx in _RAM for m in rx.finditer(t)]
    sizes = [s for s in sizes if s in RAM_SIZES]
    if sizes:
        return max(sizes)
    # "i5 9400f gtx 1660 16 gb 512 gb ssd": a plain 8-64 GB that isn't the card's memory or the disk
    bare = [int(m.group(1)) for m in _BARE_GB.finditer(t)
            if not _NOT_RAM_BEFORE.search(t[max(0, m.start() - 24):m.start()])
            and not _NOT_RAM_AFTER.search(t[m.end():m.end() + 24])
            and not _is_vram(int(m.group(1)), t[max(0, m.start() - 24):m.start()])]
    bare = [s for s in bare if 8 <= s <= 64 and s in RAM_SIZES]
    return max(bare) if bare else None


_SSD_NO_UNIT = re.compile(r"\b(?:ssd|nvme)\s(?:[a-z][a-z0-9]* ){0,2}?(\d{3,4})\b(?! ?(?:mb|mhz|w|rpm|eur)\b)(?! ?€)")


def ssd_in(t: str) -> int | None:
    # real SSDs start at ~120 GB: "16 gb ssd 500" is 16 GB of RAM and a 500 GB SSD
    sizes = [s for s in (_gb(m.group(1), m.group(2)) for rx in (_SSD_BEFORE, _SSD_AFTER) for m in rx.finditer(t))
             if 60 <= s <= 8000]
    if not sizes:   # "ssd 500"
        sizes = [int(m.group(1)) for m in _SSD_NO_UNIT.finditer(t) if 120 <= int(m.group(1)) <= 4000]
    return max(sizes) if sizes else None


def parse(title: str, description: str = "", attributes: str = "", form: str = "") -> dict:
    """Everything the text says. Missing facts are None; the AI can fill them in (merge)."""
    title_t, all_t = _t(title), _t(f"{title}\n{attributes}\n{description}")
    names = gpus_in(title) or gpus_in(f"{attributes}\n{description}")
    cpu = cpu_in(title) or cpu_in(f"{attributes}\n{description}")
    ddr = _DDR.findall(all_t)
    laptop = bool(_LAPTOP.search(title_t)) or (form == "laptop" and not re.search(r"\b(?:desktop|pc fisso|tower|case)\b", title_t))
    return {
        "form": "laptop" if laptop else "desktop",
        "gpu": best_gpu(names), "gpus": names, "integrated": bool(_INTEGRATED.search(all_t)) and not names,
        "cpu": cpu, "ram_gb": ram_in(all_t), "ram_type": f"DDR{max(ddr)}" if ddr else None,
        "ssd_gb": ssd_in(all_t), "hdd": bool(_HDD.search(all_t)),
        "wifi": False if _NO_WIFI.search(all_t) else (True if _WIFI.search(all_t) else None),
        "bt": False if _NO_BT.search(all_t) else (True if _BT.search(all_t) else None),
        "no_ethernet": bool(_NO_ETH.search(all_t)),
    }


def needs_ai(spec: dict) -> bool:
    """The text left RAM, the graphics card or the SSD open: the AI reads the description."""
    return spec["ram_gb"] is None or spec["gpu"] is None or spec["ssd_gb"] is None


def merge(spec: dict, ai: dict | None) -> dict:
    """The AI's reading fills what the patterns missed (never overrules a fact they found)."""
    if not ai:
        return spec
    out = dict(spec)
    if out["gpu"] is None and ai.get("gpu"):
        names = gpus_in(str(ai["gpu"]))
        out["gpu"] = best_gpu(names) if names else None
        out["gpus"] = names
    if out["cpu"] is None and ai.get("cpu"):
        out["cpu"] = cpu_in(str(ai["cpu"]))
    for k, conv in (("ram_gb", int), ("ssd_gb", int)):
        if out[k] is None and isinstance(ai.get(k), (int, float)) and ai[k] > 0:
            out[k] = conv(ai[k])
    if out["ram_type"] is None and re.fullmatch(r"DDR[2-5]", str(ai.get("ram_type") or "").upper()):
        out["ram_type"] = str(ai["ram_type"]).upper()
    for k in ("wifi", "bt"):
        if out[k] is None and isinstance(ai.get(k), bool):
            out[k] = ai[k]
    if ai.get("ethernet") is False:
        out["no_ethernet"] = True
    if ai.get("hdd") is True:
        out["hdd"] = True
    out["ai"] = True
    return out


def ddr4(spec: dict) -> bool | None:
    """DDR4 or newer: what the listing says, else what the CPU takes (Intel 6th gen+, any Ryzen); None if unknown."""
    if spec.get("ram_type"):
        return spec["ram_type"] >= "DDR4"
    return (spec.get("cpu") or {}).get("ddr4")


def problems(spec: dict) -> list[str]:
    """Why it can't be posted (empty = it meets every minimum)."""
    out = []
    if spec["gpu"] is None:
        out.append("integrated graphics" if spec.get("integrated") else "graphics card not found")
    elif not gpu_ok(spec["gpu"]):
        out.append(f"{label_gpu(spec['gpu'])} is under a GTX 1050")
    if spec["ram_gb"] is None:
        out.append("RAM not found")
    elif spec["ram_gb"] < MIN_RAM_GB:
        out.append(f"{spec['ram_gb']} GB RAM")
    if ddr4(spec) is False:
        out.append("DDR3 RAM")
    if spec["ssd_gb"] is None:
        out.append("HDD only" if spec["hdd"] else "SSD not found")
    elif spec["ssd_gb"] < MIN_SSD_GB:
        out.append(f"{spec['ssd_gb']} GB SSD (under 256)")
    if spec.get("no_ethernet"):
        out.append("no Ethernet")
    return out


def wifi_bt(spec: dict) -> tuple[bool | None, bool | None]:
    """Laptops always have both; desktops only when the listing says."""
    if spec["form"] == "laptop":
        return True, True
    return spec["wifi"], spec["bt"]


def needs_dongle(spec: dict) -> bool:
    wifi, bt = wifi_bt(spec)
    return wifi is not True or bt is not True


def label_gpu(name: str) -> str:
    return " ".join(w.upper() if w in ("gtx", "rtx", "gt", "mx", "rx", "xt", "xtx", "gre") else
                    w.title() if w in ("ti", "super", "vega", "arc") else w.upper() if re.fullmatch(r"[ab]\d+", w) else w
                    for w in name.split())


def line(spec: dict) -> str:
    """'i5-9400F · GTX 1660 Super · 16 GB DDR4 · 512 GB SSD · WiFi ✅ BT ❓'"""
    wifi, bt = wifi_bt(spec)
    mark = {True: "✅", False: "❌", None: "❓"}
    ram = f"{spec['ram_gb']} GB" + (f" {spec['ram_type']}" if spec.get("ram_type") else " DDR4" if ddr4(spec) else "")
    ssd = (f"{spec['ssd_gb'] / 1000:g} TB" if spec["ssd_gb"] >= 1000 else f"{spec['ssd_gb']} GB") + " SSD"
    parts = [(spec.get("cpu") or {}).get("name") or "CPU ❓", label_gpu(spec["gpu"]), ram,
             ssd + (" + HDD" if spec.get("hdd") else ""), f"WiFi {mark[wifi]} BT {mark[bt]}"]
    return " · ".join(parts)


# vinted.it shows listings from all over the EU and most PCs are from abroad (Italy only here). Only the seller's
# profile says the country (one more page), so a listing clearly written in another language is skipped first.
_LANG = {
    "it": {"il", "lo", "la", "di", "per", "con", "che", "perfetto", "perfettamente", "funzionante", "vendo", "usato",
           "ottime", "ottimo", "condizioni", "spedizione", "scheda", "disco", "fisso", "assemblato", "nuovo", "giochi", "anche"},
    "fr": {"le", "les", "des", "avec", "pour", "tres", "etat", "carte", "graphique", "vends", "ordinateur", "fixe", "jeux",
           "fonctionne", "parfait", "bon", "boitier", "alimentation", "disque", "dur", "neuf", "vend", "est", "sur",
           "go"},   # "16 Go" is how French listings write GB
    "de": {"und", "mit", "fur", "der", "die", "das", "ist", "sehr", "verkaufe", "zustand", "grafikkarte", "speicher",
           "netzteil", "gehause", "funktioniert", "einwandfrei", "neu", "rechner", "spiele", "ich", "nicht", "auch"},
    "es": {"con", "para", "muy", "estado", "tarjeta", "grafica", "ordenador", "sobremesa", "portatil", "funciona",
           "perfecto", "juegos", "nuevo", "fuente", "caja", "esta", "vendo", "los", "las", "del"},
    "pt": {"com", "para", "muito", "estado", "placa", "grafica", "computador", "funciona", "jogos", "novo", "fonte",
           "caixa", "esta", "vendo", "os", "as", "do", "da", "em", "nao"},
}


def foreign(text: str) -> bool:
    """Clearly written in French, German, Spanish or Portuguese, not Italian (shared words count for both)."""
    words = re.findall(r"[a-z]+", _t(text))
    counts = {lang: sum(w in vocab for w in words) for lang, vocab in _LANG.items()}
    italian = counts.pop("it")
    lang, n = max(counts.items(), key=lambda kv: kv[1])
    return n >= 2 and n > 2 * italian


_PC_WORDS = re.compile(r"\b(?:pc|computer|desktop|tower|notebook|laptop|portatile|fisso|gaming|gamer|assemblato|workstation)\b")
_NOT_A_PC = re.compile(r"\b(?:monitor|schermo|tastiera|mouse|cuffie|headset|sedia|scrivania|joystick|controller|volante|"
                       r"webcam|stampante|router|alimentatore|dissipatore|ventol[ae]|scheda madre|motherboard|solo case|"
                       r"case vuoto|borsa|zaino|custodia|cover|caricatore|ram ddr|banco ram|cpu cooler)\b")


def is_pc_title(title: str, form: str = "") -> bool:
    """A whole PC (not a part or accessory) of that form, from the title: for Subito's mixed Informatica category."""
    t = _t(title)
    if _NOT_A_PC.search(t) and not gpus_in(title):
        return False
    if not (_PC_WORDS.search(t) or gpus_in(title)):
        return False
    laptop = bool(_LAPTOP.search(t))
    return not form or laptop == (form == "laptop")


# --- comparing: the same CPU + graphics card combo -------------------------------------------------------------
def combo(spec: dict) -> str | None:
    """'desktop · gtx 1660 super · i5': the form, the exact card and the CPU's tier (None without a card)."""
    if not spec.get("gpu"):
        return None
    tier = (spec.get("cpu") or {}).get("tier")
    return " · ".join(x for x in (spec["form"], spec["gpu"], tier) if x)


@lru_cache(maxsize=50_000)
def title_combo(title: str, form: str = "") -> str | None:
    """A comparable's combo from its title alone (pools and sold history keep titles only)."""
    s = parse(title, form=form)
    return combo(s) if s["gpu"] and s["cpu"] else None
