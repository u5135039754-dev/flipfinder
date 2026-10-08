"""
Repair deals: we fix things ourselves, so a damaged listing can still be a deal once the part
is paid for. Repair cost = the part (a typical AliExpress/Amazon/iFixit price in Italy, for an
aftermarket part) + REPAIR_TOOLS for small tools and adhesive; no labour.

Only faults we can name and price count: "screen broken" is a repair deal, "doesn't turn on"
or "for parts" is not (no way to know what it needs). Locked/iCloud listings stay excluded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REPAIR_TOOLS = 5.0             # small tools, adhesive, screws
IPHONE_PART_DROP = 0.15        # an iPhone with a non-original screen/battery/camera shows "unknown part"
HARD_PROFIT_FACTOR = 2.0       # 🔴 hard repairs only when the profit is at least twice our minimum
DIFFICULTY = {"easy": "🟢", "medium": "🟡", "hard": "🔴"}

# Words that mean "damaged": in the exclude lists they used to drop the listing; now they make it
# a repair candidate. Italian first, then the other languages vinted.it shows.
DAMAGE_WORDS = {
    "rotto", "rotta", "rotti", "difettoso", "difettosa", "guasto", "guasta", "non funzionante", "non funziona",
    "per ricambi", "ricambi", "per pezzi", "da riparare", "crepato", "crepata", "incrinato", "scheggiato",
    "schermo rotto", "display rotto", "vetro rotto", "ne s'allume", "ne fonctionne", "en panne", "hs", "défectueux",
    "cassé", "cassée", "fissuré", "pièces", "no funciona", "no enciende", "averiado", "roto", "rota", "piezas",
    "para reparar", "pantalla rota", "não funciona", "defekt", "kaputt", "kapot", "broken", "cracked",
    "not working", "for parts", "faulty",
}

# The fault, from the listing's words: (part, words). First match wins, so specific before general.
FAULTS = [
    ("face id", r"face ?id"),
    ("board", r"scheda madre|logic ?board|motherboard|saldatur|non si accende|no enciende|ne s'allume|doesn'?t turn on"),
    ("camera", r"fotocamera|camera posteriore|obiettivo|camera\b|cámara|caméra"),
    ("back glass", r"vetro posteriore|scocca posteriore|retro rotto|back ?glass|back cover"),
    ("battery", r"batteri[ae]|battery|batería|akku|\bbatt\b"),
    ("charging port", r"porta di ricarica|connettore di ricarica|non carica|charging port|lightning|\busb-?c\b"),
    ("hdmi port", r"\bhdmi\b"),
    ("stick", r"\bdrift\b|levett|analogic|stick"),
    ("button", r"tasto|tasti|pulsant|bottone|button"),
    ("speaker", r"altoparlante|speaker|casse?\b"),
    ("jack", r"\bjack\b|ingresso|potenziometr|\bpot\b|switch"),
    ("tube", r"valvol[ae]|\btube\b"),
    ("screen", r"schermo|display|vetro|lcd|touch|écran|pantalla|tela|bildschirm|screen|glass|crepat|cracked|fissur"),
]

# Part price (EUR, aftermarket) and difficulty per kind of device. Missing = we don't repair that.
PARTS = {
    "iphone": {"screen": (None, "medium"), "battery": (17, "easy"), "back glass": (15, "medium"),
               "camera": (30, "hard"), "charging port": (12, "medium"), "speaker": (8, "medium"), "button": (8, "medium")},
    "ipad": {"screen": (25, "easy"), "battery": (25, "medium"), "charging port": (12, "medium"), "button": (6, "easy")},
    "watch": {"screen": (30, "medium"), "battery": (15, "medium")},
    "switch": {"screen": (20, "medium"), "stick": (6, "easy"), "battery": (15, "easy"), "button": (5, "easy"),
               "charging port": (8, "hard")},
    "console": {"hdmi port": (8, "hard"), "button": (6, "easy"), "charging port": (8, "hard")},
    "controller": {"stick": (6, "easy"), "button": (5, "easy"), "battery": (12, "easy"), "charging port": (6, "medium")},
    "kindle": {"screen": (30, "medium"), "battery": (12, "easy")},
    "camera": {"screen": (20, "medium"), "battery": (12, "easy")},
    "calculator": {"screen": (15, "medium"), "button": (5, "easy"), "battery": (5, "easy")},
    "music": {"jack": (5, "easy"), "button": (5, "easy"), "tube": (20, "easy")},
}

# iPhone screens (aftermarket "incell") get pricier with each generation; Pro/Max a little more
IPHONE_SCREEN = {11: 20, 12: 30, 13: 35, 14: 40, 15: 55, 16: 70}
IPHONE_PARTS = {"screen", "battery", "camera"}   # these show iOS's "unknown part" message


@dataclass
class Repair:
    part: str
    parts: float          # the part's price, EUR (tools come on top)
    difficulty: str       # easy / medium / hard
    iphone_part: bool     # an iPhone part iOS flags as non-original: resale drops ~15%

    @property
    def cost(self) -> float:
        return round(self.parts + REPAIR_TOOLS, 2)

    @property
    def icon(self) -> str:
        return DIFFICULTY[self.difficulty]

    @property
    def resale_factor(self) -> float:
        return 1 - IPHONE_PART_DROP if self.iphone_part else 1.0

    def line(self) -> str:
        return f"parts ~€{self.cost:g}, {self.icon} {self.part}"


def _norm(text: str) -> str:
    return text.lower().replace("’", "'").replace("`", "'")


def is_damage_word(keyword: str) -> bool:
    return _norm(keyword).strip() in DAMAGE_WORDS


def damaged(text: str) -> bool:
    t = _norm(text)
    return any(re.search(rf"\b{re.escape(w)}\b", t) for w in DAMAGE_WORDS)


def device(text: str) -> str | None:
    t = _norm(text)
    for kind, pattern in (("iphone", r"iphone"), ("ipad", r"ipad"), ("watch", r"apple watch|\bwatch\b"),
                          ("controller", r"dualsense|dualshock|controller|joy-?con|manette|mando"),
                          ("switch", r"nintendo|switch (?:lite|oled)"), ("console", r"\bps[45]\b|playstation|xbox"),
                          ("kindle", r"kindle"), ("calculator", r"\bti[- ]?8\d|casio fx|calcolatric"),
                          ("camera", r"powershot|ixus|coolpix|cybershot|lumix|fotocamera digitale|compatta"),
                          ("music", r"chitarra|guitar|amplificator|\bamp\b|pedale|pedal|boss|fender|marshall|ampli")):
        if re.search(pattern, t):
            return kind
    return None


def repair_need(text: str, query: str = "") -> Repair | None:
    """What a damaged listing needs and what it costs, or None when we can't name or price the fault."""
    t = _norm(text)
    kind = device(f"{query} {text}")
    if not kind:
        return None
    part = next((name for name, pattern in FAULTS if re.search(pattern, t)), None)
    table = PARTS.get(kind, {})
    if part not in table:
        return None                      # "doesn't turn on", "for parts", Face ID...: can't price it
    price, difficulty = table[part]
    if kind == "iphone" and part == "screen":
        gen = re.search(r"iphone\s*(1[1-6])", _norm(f"{text} {query}"))
        if not gen:
            return None
        price = IPHONE_SCREEN[int(gen.group(1))] + (10 if re.search(r"\bpro\b|\bmax\b", t) else 0)
    return Repair(part, float(price), difficulty, kind == "iphone" and part in IPHONE_PARTS)
