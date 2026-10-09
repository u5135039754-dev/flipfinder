"""Works out market value, profit and a 1-10 rating for a listing."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from statistics import median

from .vinted import Item
from .repair import HARD_PROFIT_FACTOR, Repair, damaged, is_damage_word, repair_need


@dataclass
class Deal:
    item: Item
    market_value: float      # "original price" = median of similar listings
    comparables: int         # how many listings that median is based on
    profit: float
    roi: float               # profit as % of what you pay (item + shipping)
    rating: int              # 1-10
    basis: str = ""          # how the comparables were picked, shown by --dry-run
    sample: list[Item] = field(default_factory=list)   # a few comparables, shown by --dry-run
    shipping: float = 0.0          # what you pay to get it delivered (0 for pickup)
    shipping_known: bool = True    # False when it's the config estimate, not Vinted's price
    packaging: float = 0.0         # resell_costs: your box/packaging when you sell it on
    pickup_only: bool = False
    city: str = ""
    blocked: list[str] = field(default_factory=list)   # rules it fails; empty for a real deal
    cost: float = 0.0              # everything you pay: price + fee + shipping/pickup + missing parts
    missing_part: float = 0.0      # cost of a battery/charger the listing says is missing
    budget: bool = False
    check: str = ""
    query: str = ""                # the search that found it (set by the scanner)
    group: str = ""                # its category, for the group topic

    @property
    def per_euro(self) -> float:
        """Profit for each euro spent, how budget deals are ranked."""
        return self.profit / self.cost if self.cost > 0 else 0.0
    closeness: float = 1.0         # 0-1, how close a blocked listing came to passing
    by_platform: dict = field(default_factory=dict)   # platform -> (median, listings) when there are enough
    resell_on: str = ""            # platform where it nets the most after fees
    sell_fee: float = 0.0          # fee for selling it there
    asking_value: float = 0.0      # median asking price, before sold prices were blended in
    sold_count: int = 0            # likely-sold comparables used in market_value
    sell_days: float | None = None   # comparables usually sell in this many days
    demand: dict = field(default_factory=dict)   # label, sold per week, listed now, sell-through, favourites
    seller_warnings: list = field(default_factory=list)   # "⚠️ New seller..." lines for the alert
    repair: Repair | None = None   # a damaged listing we can fix: the part and its price


@dataclass
class Rules:
    min_profit: float = 10.0
    min_roi: float = 30.0
    min_rating: int = 6
    min_comparables: int = 8
    max_roi: float = 400.0          # anything cheaper than this is probably fake/broken/wrong item
    resell_costs: float = 0.0       # your own extra costs per flip (packaging, etc.)
    shipping_cost: float = 0.0      # estimate of what you pay for delivery, if Vinted doesn't say
    exclude_keywords: tuple[str, ...] = ()
    sell_fees: dict = field(default_factory=dict)   # platform -> (percent, fixed) you pay when selling
    match_brand: bool = True        # off for e.g. graphics cards, where MSI/ASUS/Zotac sell the same chip
    max_cost: float | None = None   # budget: price + fee + shipping/pickup (+ missing parts) must fit
    missing_part_cost: float = 0.0  # cameras: added when the listing says no battery/charger
    repairs: bool = True            # damaged listings we can fix become repair deals (/repairs on|off)
    abroad: bool = False            # Vinted listings shipping from outside Italy (/abroad on|off)
    max_price: float | None = None  # main deals: the most we pay all in (/setrule max_price), budget has its own
    budget: bool = False            # a budget-mode search (ranked by profit per euro spent)
    check: str = ""                 # "what to check before buying", shown in the alert
    sold: list = field(default_factory=list)   # [(Item, days to sell or None, sold at)] likely sold lately (sold.py)
    tracked_days: float = 0.0       # how long sales have been tracked for this search
    sold_scale: float | None = None # listings gone / checked: sold ones found are a sample (None: too few checks)
    now: float = 0.0
    # rating knobs: profit/roi at which that part of the score maxes out
    rating_profit_cap: float = 50.0
    rating_roi_cap: float = 150.0
    rating_comparables_cap: int = 30


def is_excluded(item: Item, keywords) -> bool:
    title = item.title.lower().replace("’", "'").replace("`", "'")   # "ne s’allume" = "ne s'allume"
    return any(re.search(rf"\b{re.escape(k.lower())}\b", title) for k in keywords)


HIGH_ASK = 1.3   # asking prices this far above the median are wishful thinking, not the market


def remove_outliers(prices: list[float]) -> list[float]:
    """Drop prices outside 1.5x IQR, and any over 1.3x the median, so silly listings don't move the median."""
    if len(prices) < 4:
        return prices
    s = sorted(prices)
    q1 = s[len(s) // 4]
    q3 = s[(3 * len(s)) // 4]
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    kept = [p for p in s if lo <= p <= hi]
    top = median(kept) * HIGH_ASK
    return [p for p in kept if p <= top]


# --- Title matching ------------------------------------------------------------
# One search returns several models at very different prices (an MG15 practice amp
# and a valve head both match "marshall amplificatore"), so a listing is only
# compared with listings that share its model words ("mg15", "studio", "50s").

# Spellings that mean the same thing. Category words map to the Italian one because
# vinted.it also shows French, Spanish, German and Dutch listings.
PHRASES = [
    (re.compile(r"\bles ?paul\b"), "lespaul"),
    (re.compile(r"\bht ?g ?700\b"), "g700"),
    (re.compile(r"\bclassic ?vibe\b"), "classicvibe"),
    (re.compile(r"\bcyber ?shot\b"), "cybershot"),
    (re.compile(r"\bmk ?(ii|2)\b"), "mk2"),
    (re.compile(r"\bmk ?(iii|3)\b"), "mk3"),
    # consoles: "Playstation 5" is a PS5; the disc and digital versions sell at different prices
    (re.compile(r"\bplay ?station ?5\b|\bps ?5\b"), "ps5"),
    (re.compile(r"\b(?:con |avec |with |mit |versione |version )?(?:lettore|lecteur|lector|laufwerk)"
                r"(?: (?:cd|dvd|disco|disc|dischi|blu ?ray))?\b"), " disc "),
    (re.compile(r"\b(?:standard|disco|disk|disque|blu ?ray)(?: edition| edizione)?\b"), " disc "),
    (re.compile(r"\b(?:digitale|numerique|digital edition)\b"), " digital "),
]
SYNONYMS = {
    "lp": "lespaul", "strat": "stratocaster", "serie": "series",
    "guitar": "chitarra", "guitare": "chitarra", "guitarra": "chitarra", "gitarre": "chitarra",
    "gitaar": "chitarra", "chitarre": "chitarra",
    "acoustic": "acustica", "acoustics": "acustica", "acoustique": "acustica", "acustico": "acustica",
    "akustik": "acustica", "akoestische": "acustica", "akustische": "acustica",
    "amp": "amplificatore", "ampli": "amplificatore", "amplifier": "amplificatore",
    "amplificador": "amplificatore", "amplificateur": "amplificatore", "amplificatori": "amplificatore",
    "verstarker": "amplificatore", "versterker": "amplificatore",
    "pedal": "pedale", "pedalino": "pedale", "pedali": "pedale",
}
# Words that say nothing about the model (already normalized: lowercase, no accents)
FILLER = set("""
    chitarra elettrica elettrico electric electrique electrica elektrisch elektro
    acustica classica classical classique clasica amplificatore pedale combo testata head
    strumento instrument musicale
    vendo vende vendesi sell selling venta vente verkaufe usata usato used occasion gebraucht
    ottime ottimo ottima condizioni condizione stato state conditions etat estado zustand
    come nuova nuovo nuove new neuf nueva nuevo neu perfetta perfetto perfect parfait
    funzionante working fonctionne funciona bellissima bellissimo bella bello rara raro rare
    originale original modello model modele serie series edition edizione version versione
    con per ed di da del della dello dei degli delle il lo la le gli un una uno in su al
    the and with for of to on by from or
    avec pour et de du des les une sur
    para el los las
    mit und fur der die das ein eine
    met en voor het een
    colore color colour couleur nero nera black noir negro schwarz zwart bianco bianca white
    blanc blanco weiss wit rosso rossa red rouge rojo blu blue bleu azul sunburst natural
    naturale cherry
    custodia case gigbag borsa bag housse funda cavo cable incluso inclusa included compreso
    regalo omaggio prezzo price trattabile spedizione shipping consegna mano ritiro
    gen generazione generation generacion generatie gps wifi garanzia warranty fattura scontrino
    sbloccato unlocked libero operatore batteria battery
    mezzanotte midnight galassia starlight grafite graphite argento silver oro gold grigio gray grey
    siderale space viola purple verde green rosa pink azzurro giallo yellow
    fat console consola konsole
""".split())

# Words that make a different model (and price): an iPhone 13 Pro isn't an iPhone 13,
# a 3060 Ti isn't a 3060. They have to match exactly, like model numbers.
MODEL_WORDS = {"pro", "max", "mini", "plus", "oled", "ti", "super", "slim", "digital", "disc", "lite",
               "ultra", "se", "cellular", "lte", "xt", "xl",
               # special editions sell at their own prices
               "limited", "edition", "edizione", "custom"}

# variants a listing usually is when it doesn't say: a PS5 that doesn't say "digital" has a disc drive
DEFAULT_WORDS = {"disc"}

_YEAR = re.compile(r"^(19[5-9]\d|20[0-3]\d)$")
_WATTS = re.compile(r"^\d+(w|watt|watts)$")
_MODEL_CORE = re.compile(r"^([a-z]+\d+)[a-z]+$")   # mg15cdr -> mg15, eg260c -> eg260


# Done on the lowercased text before punctuation is stripped
PRE_CLEAN = [
    # screen sizes say nothing about the model: "10 pouces", "11 pollici", '13"'
    (re.compile(r"\b\d+(?:[.,]\d)?\s*(?:pouces|pollici|zoll|inch|inches|pulgadas|\"|'')"), " "),
    # decimals stay one token: "10.2" -> "10p2" (not "10" and "2")
    (re.compile(r"\b(\d+)[.,](\d)\b"), r"\1p\2"),
    # battery health is not a model number: "batteria 87%", "battery health: 90", "salute 100%"
    (re.compile(r"\b(?:salute\s+)?(?:batteria|battery|bateria|batterie|akku|salute)(?:\s+health)?\s*:?\s*\d+\s*%?"), " "),
    (re.compile(r"\d+\s*%"), " "),
    # ordinals: "2ª", "3°", "2nd", "10th" -> plain number
    (re.compile(r"(\d+)\s*[ªº°]"), r"\1 "),
    (re.compile(r"\b(\d+)\s*(?:st|nd|rd|th)\b"), r"\1"),
    (re.compile(r"\b(\d+)\s*a\s+(?=gen)"), r"\1 "),
    (re.compile(r"\b(\d+)\s*(?:a\s*)?g[eé]n[a-zé]*"), r"\1 "),            # "8génération", "9agen"
    (re.compile(r"\b(?:prima|first|premiere)\s+(?=gen)"), "1 "),
    (re.compile(r"\b(?:seconda|second|deuxieme|segunda)\s+(?=gen)"), "2 "),
    (re.compile(r"\b(?:terza|third|troisieme|tercera)\s+(?=gen)"), "3 "),
    (re.compile(r"\bgen(?:eration|erazione|eracion|eratie)?\.?\s*(\d+)\b"), r" \1"),   # "gen 2" -> "2"
    # sizes: "128 GB" / "128 Go" / "128 G" -> "128gb", "1 TB" / "1 To", "45 mm"
    (re.compile(r"\b(\d+)\s*(?:gb|go|giga)\b"), r"\1gb"),
    (re.compile(r"\b(16|32|64|128|256|512)\s*g\b"), r"\1gb"),
    (re.compile(r"\b(\d)\s*(?:tb|to)\b"), r"\1tb"),
    (re.compile(r"\b(\d+)\s*mm\b"), r"\1mm"),
]


@lru_cache(maxsize=50_000)
def normalize(text: str) -> tuple[str, ...]:
    """Lowercase words without accents or punctuation, with equivalent spellings merged."""
    t = text.lower()
    for pattern, repl in PRE_CLEAN:
        t = pattern.sub(repl, t)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9]+", " ", t)
    for pattern, repl in PHRASES:
        t = pattern.sub(repl, t)
    out: list[str] = []
    for w in (SYNONYMS.get(w, w) for w in t.split()):
        # "MG 15", "DS-1" -> "mg15", "ds1" (but "Pro 2", "SE 2" stay apart)
        if (out and w.isdigit() and len(w) <= 3 and out[-1].isalpha() and len(out[-1]) <= 3
                and out[-1] not in FILLER and out[-1] not in MODEL_WORDS):
            out[-1] += w
        else:
            out.append(w)
    return tuple(out)


def is_relevant(title: str, query: str, brand: str = "") -> bool:
    """
    True when the title (or the listing's brand) has every word of the search query. Category
    words in the query ("amplificatore", "chitarra", "pedale") may be missing: the search's
    Vinted category already says so, and "Marshall MG15CFR" is a Marshall amp.
    """
    words = set(normalize(query))
    needed = (words - FILLER) or words
    return needed <= set(normalize(title)) | set(normalize(brand))


_ROMAN = {"ii", "iii", "iv"}   # "Player II", "Special-II" are different models too
_SERIAL = re.compile(r"^\d{5,}$")
MAKERS = {"fender", "squier", "gibson", "epiphone"}   # "Squier by Fender" says nothing about the model


@lru_cache(maxsize=50_000)
def model_tokens(title: str, query: str = "", brand: str = "") -> frozenset[str]:
    """Words in the title that tell models apart, e.g. {"mg15", "mg15cdr"} or {"studio", "50s"}."""
    skip = FILLER | MAKERS | set(normalize(query)) | set(normalize(brand))
    tokens = set()
    for w in normalize(title):
        if (w in skip or (len(w) < 2 and not w.isdigit()) or _YEAR.match(w) or _WATTS.match(w)
                or _SERIAL.match(w)):
            continue
        tokens.add(w)
        core = _MODEL_CORE.match(w)
        if core:
            tokens.add(core.group(1))
    return frozenset(tokens)


def model_numbers(tokens: frozenset[str]) -> frozenset[str]:
    """
    The tokens that name a specific model: "mg15" (from mg15cdr too), "50", "mk2", "ii",
    "128gb", and suffix words like "pro", "max", "oled", "ti".
    """
    out = set()
    for t in tokens:
        if t in _ROMAN or t in MODEL_WORDS:
            out.add(t)
        elif any(c.isdigit() for c in t):
            core = _MODEL_CORE.match(t)
            out.add(core.group(1) if core else t)
    return frozenset(out)


def price_spread(prices: list[float]) -> float:
    """IQR / median: how much prices in a group vary."""
    s = sorted(prices)
    if len(s) < 4:
        return float("inf")
    return (s[(3 * len(s)) // 4] - s[len(s) // 4]) / median(s)


TIGHT_SPREAD = 0.35   # the whole pool only counts as comparables when it's this consistent


# a console with games or extra controllers sells for more: not a comparable for a plain one
BUNDLE = re.compile(r"\b(giochi|gioco|games?|jeux|jeu|juegos?|spiele|spiel|bundle)\b|"
                    r"\b(2|3|4|due|tre|deux|trois|dos|tres|zwei)\s*(controller|controllers|manette|manettes|mandos?|joysticks?|joypad)\b",
                    re.IGNORECASE)


def is_bundle(title: str) -> bool:
    return bool(BUNDLE.search(title))


def find_comparables(item: Item, pool: list[Item], min_comparables: int,
                     query: str = "", match_brand: bool = True) -> tuple[list[Item], str]:
    """
    Listings to compare against, plus a note on how they were picked.

    Same brand only. Model numbers ("mg15", "50 mk2", "ii") must match exactly, so a
    Katana 50 MkII isn't valued like a Katana 100. Listings without a model number are
    compared with other unnumbered ones, those sharing a model word ("studio") first,
    and only when their prices are tight. Last resort is the whole pool, again only
    when prices are tight. Otherwise nothing, since no alert beats a wrong one.
    """
    others = [p for p in pool if p.id != item.id]
    if query:
        others = [p for p in others if is_relevant(p.title, query, p.brand)]
    if item.brand and match_brand:
        brand = item.brand.lower()
        others = [p for p in others if not p.brand or p.brand.lower() == brand]
    if not is_bundle(item.title):
        plain = [p for p in others if not is_bundle(p.title)]
        if len(plain) >= min_comparables:
            others = plain
    if item.size:
        same_size = [p for p in others if p.size == item.size]
        if len(same_size) >= min_comparables:
            others = same_size

    def tokens(x: Item) -> frozenset[str]:
        return model_tokens(x.title, query, x.brand)

    def tight(group: list[Item]) -> bool:
        return (len(group) >= min_comparables
                and price_spread([p.price for p in group]) < TIGHT_SPREAD)

    mine = tokens(item)
    numbers = model_numbers(mine)
    same_number = [p for p in others if model_numbers(tokens(p)) == numbers]
    brand_note = f" ({item.brand})" if item.brand and match_brand else ""
    # A suffix on its own ("mini", "pro") isn't a model: a TC Electronic Spark Mini isn't
    # worth what the other "mini" pedals are. It still has to match, but the listing is
    # then treated like one without a model number.
    real = {t for t in numbers if t not in MODEL_WORDS}
    if real and len(same_number) >= min_comparables:
        return same_number, "model " + " ".join(sorted(numbers)) + brand_note
    if not real:
        words = mine - numbers
        sharing = [p for p in same_number if words & tokens(p)]
        if tight(sharing):
            shared = set().union(*(tokens(p) for p in sharing))
            return sharing, "model words " + " ".join(sorted((words & shared) | numbers))
        if tight(same_number):
            return same_number, "listings without a model number (prices are tight)"
    # Last resort, only when prices are tight, and never against a listing whose model
    # number contradicts this one (a 128GB is never valued like a 256GB or a Pro), nor one
    # missing this one's model words (a Pro isn't valued like a plain one; "disc" may be left out)
    must = (numbers & MODEL_WORDS) - DEFAULT_WORDS
    compatible = [p for p in others if model_numbers(tokens(p)) <= numbers and must <= model_numbers(tokens(p))]
    if tight(compatible):
        return compatible, "whole pool (prices are tight)"
    return [], "no comparable model"


def _comparables(item: Item, pool: list[Item], min_comparables: int,
                 query: str, match_brand: bool = True) -> tuple[float | None, list[Item], str]:
    others, basis = find_comparables(item, pool, min_comparables, query, match_brand)
    kept = remove_outliers([p.price for p in others])
    if kept:
        others = [p for p in others if kept[0] <= p.price <= kept[-1]]
    if len(kept) < min_comparables:
        return None, others, basis
    return round(median(kept), 2), others, basis


def market_value(item: Item, pool: list[Item], min_comparables: int,
                 query: str = "") -> tuple[float | None, int]:
    """Median listed price of comparable listings, preferring the same size when there are enough."""
    value, comps, _ = _comparables(item, pool, min_comparables, query)
    return value, len(comps)


SOLD_MIN = 3      # sold comparables needed before sold prices count
SOLD_FULL = 10    # from this many on, market value is the sold median alone
DAYS_MIN = 3      # known selling times needed for "sells in ~X days"
FAST_DAYS, SLOW_DAYS = 7, 30   # rating +1 / -1


def blend_sold(item: Item, asking: float, rules: Rules, query: str) -> tuple[float, int, float | None]:
    """
    (market value, sold comparables used, days to sell). With SOLD_MIN+ likely-sold comparables
    (same model rules as asking prices), value moves from asking towards what actually sold:
    half and half at SOLD_MIN, sold only at SOLD_FULL.
    """
    if not rules.sold:
        return asking, 0, None
    sold_items = [x[0] for x in rules.sold]
    value, comps, _ = _comparables(item, sold_items, SOLD_MIN, query, rules.match_brand)
    timed = [(x[0], x[1]) for x in rules.sold if x[1] is not None]
    same, _ = find_comparables(item, [it for it, _ in timed], DAYS_MIN, query, rules.match_brand)
    ids = {c.id for c in same}
    days = [d for it, d in timed if it.id in ids]
    sell_days = round(median(days), 1) if len(days) >= DAYS_MIN else None
    if value is None:
        return asking, 0, sell_days
    n = len(comps)
    w = min(1.0, 0.5 + 0.5 * (n - SOLD_MIN) / (SOLD_FULL - SOLD_MIN))
    return round(w * value + (1 - w) * asking, 2), n, sell_days


DEMAND_WINDOW = 28           # days of sales counted for "sold per week"
HIGH_RATIO, SLOW_RATIO = 0.5, 0.1


def demand(item: Item, comps: list[Item], rules: Rules, query: str, sell_days: float | None) -> dict:
    """
    How fast this model moves: sold per week (estimated: the sales we confirm are a sample,
    scaled by rules.sold_scale), how many are listed now, sell-through = sold/week ÷ listed,
    favourites. "learning" until a search has a week of tracking and enough checked listings.
    """
    active = len(comps)
    favs = round(sum(c.favourites for c in comps) / active) if active else 0
    out = {"label": "", "learning": True, "listed": active, "favourites": favs, "sold_week": None,
           "ratio": None, "days": sell_days}
    window = min(DEMAND_WINDOW, rules.tracked_days)
    recent = [x[0] for x in rules.sold if len(x) > 2 and x[2] >= rules.now - window * 86400]
    model = find_comparables(item, recent, 1, query, rules.match_brand)[0] if recent else []
    if rules.sold_scale is None or window < 7 or (rules.tracked_days < 14 and len(model) < 3):
        return out
    sold_week = round(len(model) * rules.sold_scale / (window / 7), 1)
    ratio = round(sold_week / active, 2) if active else None
    if (sell_days is not None and sell_days <= FAST_DAYS) or (ratio is not None and ratio >= HIGH_RATIO):
        label = "high"
    elif (sell_days is not None and sell_days >= SLOW_DAYS) or (ratio is not None and ratio < SLOW_RATIO):
        label = "slow"
    else:
        label = "normal"
    return {**out, "label": label, "learning": False, "sold_week": sold_week, "ratio": ratio}


NEW_SELLER_MAX_ROI = 100     # brand-new sellers (no reviews, nothing sold) are skipped from this ROI
LOW_STARS, LOW_STARS_MIN_REVIEWS = 4.5, 5
LOW_EBAY_PCT, LOW_EBAY_MIN = 97.0, 10
NEW_SELLER = "new seller"     # how the skip shows up in `blocked`


def seller_check(seller: dict | None, roi: float) -> tuple[str | None, list[str]]:
    """
    (blocked reason or None, warnings). A seller with no reviews and nothing sold, at a
    suspiciously big discount (ROI >= 100%), is the classic scam profile: skipped. Otherwise
    new sellers and low ratings only get a warning line.
    """
    if not seller:
        return None, []
    reviews, sold = seller.get("reviews"), seller.get("sold")
    if reviews == 0 and sold == 0 and roi >= NEW_SELLER_MAX_ROI:
        return f"{NEW_SELLER} (no reviews, nothing sold) at +{roi:.0f}%", []
    warn = []
    if reviews == 0:
        warn.append("⚠️ New seller: no reviews" + (", nothing sold yet" if sold == 0 else ""))
    elif seller.get("source") == "ebay":
        pct = seller.get("positive_pct")
        if pct is not None and pct < LOW_EBAY_PCT and (reviews or 0) >= LOW_EBAY_MIN:
            warn.append(f"⚠️ Low feedback: {pct:g}% positive")
    else:
        stars = seller.get("stars")
        if stars is not None and stars < LOW_STARS and (reviews or 0) >= LOW_STARS_MIN_REVIEWS:
            neg = seller.get("negative")
            warn.append(f"⚠️ Low rating: {stars:g}★" + (f" ({neg} negative)" if neg else ""))
    return None, warn


def speed_points(sell_days: float | None) -> int:
    if sell_days is None:
        return 0
    return 1 if sell_days <= FAST_DAYS else -1 if sell_days > SLOW_DAYS else 0


def rate(profit: float, roi: float, comparables: int, rules: Rules) -> int:
    """
    Up to 4 points for ROI, 4 for absolute profit, 2 for how confident the
    market value is (more comparable listings = more trust).
    """
    roi_pts = min(max(roi, 0) / rules.rating_roi_cap, 1) * 4
    profit_pts = min(max(profit, 0) / rules.rating_profit_cap, 1) * 4
    conf_pts = min(comparables / rules.rating_comparables_cap, 1) * 2
    return max(1, min(10, round(roi_pts + profit_pts + conf_pts)))


# "Only pickup" in the languages vinted.it shows. Whole phrases on purpose: "pickup"
# alone is also a guitar part, and "remise en main propre privilégiée" means preferred.
PICKUP_ONLY = re.compile(r"""\b(
      solo\ (ritiro|consegna|a\ mano|di\ persona|in\ zona|brevi\ mani)
    | (ritiro|consegna)\ (solo|esclusivamente|unicamente)
    | (ritiro|consegna)\ a\ mano\ (solo|esclusivamente|unicamente)
    | no\ spedizion[ei] | niente\ spedizion[ei] | non\ (spedisco|spedisce|spedibile|effettuo\ spedizion[ei])
    | (pick\ ?up|collection|collect)\ only | only\ (pick\ ?up|collection|collect) | no\ (shipping|postage)
    | (remise\ en\ )?main\ propre\ uniquement | uniquement\ (en\ )?(remise\ en\ )?main\ propre | pas\ d\ envoi
    | solo\ (entrega\ )?en\ mano | (no|sin)\ envios?
    | nur\ (selbst)?abholung | kein\ versand
    | alleen\ ophalen
)\b""", re.X)


def is_pickup_only(text: str) -> bool:
    return bool(PICKUP_ONLY.search(" ".join(normalize_text(text))))


def normalize_text(text: str) -> list[str]:
    """Lowercase words without accents or punctuation, no other merging (for phrase matching)."""
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", t).split()


# "No battery", "senza caricatore", "batteria non inclusa"... in the languages vinted.it shows
MISSING_PART = re.compile(
    r"\b(?:senza|no|without|sans|sin|ohne|zonder)\s+(?:la\s+|il\s+|the\s+)?"
    r"(?:batteria|batterie|battery|bateria|batería|akku|accu|caricatore|caricabatterie|charger|chargeur|"
    r"cargador|ladeger[aä]t|alimentatore)"
    r"|\b(?:batteria|caricatore|caricabatterie|battery|charger)\s+(?:non\s+inclus[oa]|not\s+included)",
    re.I)


HOME_COUNTRY = "IT"


def blocked_reasons(profit: float, roi: float, rating: int, rules: Rules,
                    cost: float | None = None) -> list[str]:
    """Which rules a listing fails, worded with the thresholds from config.yaml."""
    out = []
    if rules.max_cost is not None and cost is not None and cost > rules.max_cost:
        out.append(f"cost > €{rules.max_cost:g} budget")
    elif rules.max_price is not None and not rules.budget and cost is not None and cost > rules.max_price:
        out.append(f"cost > €{rules.max_price:g} max price")
    if profit < rules.min_profit:
        out.append(f"profit < €{rules.min_profit:g}")
    if roi < rules.min_roi:
        out.append(f"ROI < {rules.min_roi:g}%")
    if roi > rules.max_roi:
        out.append(f"ROI > {rules.max_roi:g}%")
    if rating < rules.min_rating:
        out.append(f"rating < {rules.min_rating}")
    return out


def platform_values(comps: list[Item], min_comparables: int) -> dict:
    """Median price per platform, for platforms with enough comparables of their own."""
    need = max(3, min_comparables // 2)
    out = {}
    for src in sorted({c.source for c in comps}):
        prices = [c.price for c in comps if c.source == src]
        if len(prices) >= need:
            out[src] = (round(median(prices), 2), len(prices))
    return out


def best_resale(value: float, by_platform: dict, fees: dict, default: str) -> tuple[str, float]:
    """
    Where to sell it: the platform with the highest median after its seller fee. The
    fee is charged on the market value (the price you'd list at).
    """
    def fee(src: str, price: float) -> float:
        pct, fixed = fees.get(src, (0.0, 0.0))
        return price * pct / 100 + fixed

    if not by_platform:
        return default, fee(default, value)
    best = max(by_platform, key=lambda src: by_platform[src][0] - fee(src, by_platform[src][0]))
    return best, fee(best, value)


def assess(item: Item, pool: list[Item], rules: Rules, query: str = "",
           shipping: float | None = None, pickup_only: bool = False, city: str = "") -> Deal | None:
    """
    Works out the numbers for any listing that has a market value, deal or not.
    Returns None when there's nothing to compare it with (excluded, irrelevant,
    no comparable model); otherwise a Deal whose `blocked` lists the rules it fails.
    `shipping` is the real delivery price from the item page; without it the
    config estimate is used.
    """
    # damage words ("rotto", "broken"...) no longer drop a listing: we repair, so it's priced with the part
    hard = [k for k in rules.exclude_keywords if not is_damage_word(k)]
    if is_excluded(item, hard):
        return None
    if query and not is_relevant(item.title, query, item.brand):
        return None
    if item.country and item.country != HOME_COUNTRY and not rules.abroad:
        return None   # ships from abroad: off unless /abroad on
    fix = None
    text = f"{item.title}\n{item.description}"
    if damaged(text) or is_excluded(item, [k for k in rules.exclude_keywords if is_damage_word(k)]):
        fix = repair_need(text, query) if rules.repairs else None
        if fix is None:
            return None          # repairs off, or a fault we can't name and price
    asking, comps, basis = _comparables(item, pool, rules.min_comparables, query, rules.match_brand)
    if asking is None:
        return None
    value, sold_n, sell_days = blend_sold(item, asking, rules, query)
    n = len(comps)
    by_platform = platform_values(comps, rules.min_comparables)
    resell_on, sell_fee = best_resale(value, by_platform, rules.sell_fees, item.source)
    if shipping is None and item.shipping is not None:
        shipping = item.shipping
    ship = 0.0 if pickup_only else (shipping if shipping is not None else rules.shipping_cost)
    missing = rules.missing_part_cost if rules.missing_part_cost and MISSING_PART.search(item.title) else 0.0
    cost = item.total_price + ship + missing + (fix.cost if fix else 0.0)
    if fix and fix.iphone_part:
        # iOS shows "unknown part" after a non-original screen/battery/camera: it sells for less
        value = round(value * fix.resale_factor, 2)
        resell_on, sell_fee = best_resale(value, by_platform, rules.sell_fees, item.source)   # same platform, fee on the lower price
    profit = round(value - sell_fee - cost - rules.resell_costs, 2)
    roi = round(profit / cost * 100, 1) if cost > 0 else 0.0
    rating = max(1, min(10, rate(profit, roi, n, rules) + speed_points(sell_days)))
    blocked = blocked_reasons(profit, roi, rating, rules, cost)
    if fix and fix.difficulty == "hard" and profit < HARD_PROFIT_FACTOR * rules.min_profit:
        blocked.append(f"🔴 hard repair ({fix.part}): profit under {HARD_PROFIT_FACTOR:g}× the minimum")
    seller_block, seller_warn = seller_check(item.seller, roi)
    if seller_block:
        blocked.append(seller_block)
    by_price = sorted(comps, key=lambda p: p.price)
    sample = [by_price[i * (n - 1) // 4] for i in range(5)]   # cheapest, quartiles, priciest
    deal = Deal(item, value, n, profit, roi, rating, basis, sample,
                shipping=round(ship, 2), shipping_known=pickup_only or shipping is not None,
                packaging=rules.resell_costs, pickup_only=pickup_only, city=city, blocked=blocked,
                by_platform=by_platform, resell_on=resell_on, sell_fee=round(sell_fee, 2),
                cost=round(cost, 2), missing_part=missing, budget=rules.budget, check=rules.check,
                asking_value=asking, sold_count=sold_n, sell_days=sell_days,
                demand=demand(item, comps, rules, query, sell_days), seller_warnings=seller_warn, repair=fix)
    # How close it came: the weakest of profit/ROI/rating as a share of what the rule needs
    deal.closeness = round(min(
        1.0,
        max(profit, 0) / rules.min_profit if rules.min_profit > 0 else 1.0,
        max(roi, 0) / rules.min_roi if rules.min_roi > 0 else 1.0,
        rating / rules.min_rating if rules.min_rating > 0 else 1.0,
    ), 3)
    return deal


def evaluate(item: Item, pool: list[Item], rules: Rules, query: str = "",
             shipping: float | None = None, pickup_only: bool = False, city: str = "") -> Deal | None:
    """Returns a Deal if the item passes every rule, otherwise None."""
    deal = assess(item, pool, rules, query, shipping, pickup_only, city)
    return deal if deal and not deal.blocked else None


NEAR_MISS_CLOSENESS = 0.5


def is_near_miss(deal: Deal | None) -> bool:
    """
    Blocked, but at least halfway on its weakest rule (profit, ROI or rating), and not
    blocked by max_roi (that's "too good to be true", not "almost").
    """
    return bool(deal and deal.blocked and deal.profit > 0 and deal.closeness >= NEAR_MISS_CLOSENESS
                and not any(b.startswith(("ROI >", "cost >", NEW_SELLER)) for b in deal.blocked))
