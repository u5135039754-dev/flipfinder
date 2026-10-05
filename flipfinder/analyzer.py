"""Works out market value, profit and a 1-10 rating for a listing."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from statistics import median

from .vinted import Item


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
    closeness: float = 1.0         # 0-1, how close a blocked listing came to passing
    by_platform: dict = field(default_factory=dict)   # platform -> (median, listings) when there are enough
    resell_on: str = ""            # platform where it nets the most after fees
    sell_fee: float = 0.0          # fee for selling it there


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
    # rating knobs: profit/roi at which that part of the score maxes out
    rating_profit_cap: float = 50.0
    rating_roi_cap: float = 150.0
    rating_comparables_cap: int = 30


def is_excluded(item: Item, keywords) -> bool:
    title = item.title.lower()
    return any(re.search(rf"\b{re.escape(k.lower())}\b", title) for k in keywords)


def remove_outliers(prices: list[float]) -> list[float]:
    """Drop prices outside 1.5x IQR so a few silly listings don't move the median."""
    if len(prices) < 4:
        return prices
    s = sorted(prices)
    q1 = s[len(s) // 4]
    q3 = s[(3 * len(s)) // 4]
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return [p for p in s if lo <= p <= hi]


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
    (re.compile(r"\bmk ?(ii|2)\b"), "mk2"),
    (re.compile(r"\bmk ?(iii|3)\b"), "mk3"),
]
SYNONYMS = {
    "lp": "lespaul", "strat": "stratocaster",
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
""".split())

_YEAR = re.compile(r"^(19[5-9]\d|20[0-3]\d)$")
_WATTS = re.compile(r"^\d+(w|watt|watts)$")
_MODEL_CORE = re.compile(r"^([a-z]+\d+)[a-z]+$")   # mg15cdr -> mg15, eg260c -> eg260


@lru_cache(maxsize=50_000)
def normalize(text: str) -> tuple[str, ...]:
    """Lowercase words without accents or punctuation, with equivalent spellings merged."""
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9]+", " ", t)
    for pattern, repl in PHRASES:
        t = pattern.sub(repl, t)
    out: list[str] = []
    for w in (SYNONYMS.get(w, w) for w in t.split()):
        # "MG 15", "DS-1" -> "mg15", "ds1"
        if (out and w.isdigit() and len(w) <= 3 and out[-1].isalpha()
                and len(out[-1]) <= 3 and out[-1] not in FILLER):
            out[-1] += w
        else:
            out.append(w)
    return tuple(out)


def is_relevant(title: str, query: str) -> bool:
    """True when the title contains every word of the search query."""
    return set(normalize(query)) <= set(normalize(title))


_ROMAN = {"ii", "iii", "iv"}   # "Player II", "Special-II" are different models too
_SERIAL = re.compile(r"^\d{5,}$")
MAKERS = {"fender", "squier", "gibson", "epiphone"}   # "Squier by Fender" says nothing about the model


@lru_cache(maxsize=50_000)
def model_tokens(title: str, query: str = "", brand: str = "") -> frozenset[str]:
    """Words in the title that tell models apart, e.g. {"mg15", "mg15cdr"} or {"studio", "50s"}."""
    skip = FILLER | MAKERS | set(normalize(query)) | set(normalize(brand))
    tokens = set()
    for w in normalize(title):
        if w in skip or len(w) < 2 or _YEAR.match(w) or _WATTS.match(w) or _SERIAL.match(w):
            continue
        tokens.add(w)
        core = _MODEL_CORE.match(w)
        if core:
            tokens.add(core.group(1))
    return frozenset(tokens)


def model_numbers(tokens: frozenset[str]) -> frozenset[str]:
    """The tokens that name a specific model: "mg15" (from mg15cdr too), "50", "mk2", "ii"."""
    out = set()
    for t in tokens:
        if t in _ROMAN:
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


def find_comparables(item: Item, pool: list[Item], min_comparables: int,
                     query: str = "") -> tuple[list[Item], str]:
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
        others = [p for p in others if is_relevant(p.title, query)]
    if item.brand:
        brand = item.brand.lower()
        others = [p for p in others if not p.brand or p.brand.lower() == brand]
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
    if numbers and len(same_number) >= min_comparables:
        return same_number, "model " + " ".join(sorted(numbers))
    if not numbers:
        words = mine - numbers
        sharing = [p for p in same_number if words & tokens(p)]
        if tight(sharing):
            shared = set().union(*(tokens(p) for p in sharing))
            return sharing, "model words " + " ".join(sorted(words & shared))
        if tight(same_number):
            return same_number, "listings without a model number (prices are tight)"
    if tight(others):
        return others, "whole pool (prices are tight)"
    return [], "no comparable model"


def _comparables(item: Item, pool: list[Item], min_comparables: int,
                 query: str) -> tuple[float | None, list[Item], str]:
    others, basis = find_comparables(item, pool, min_comparables, query)
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
    if is_excluded(item, rules.exclude_keywords):
        return None
    if query and not is_relevant(item.title, query):
        return None
    value, comps, basis = _comparables(item, pool, rules.min_comparables, query)
    if value is None:
        return None
    n = len(comps)
    by_platform = platform_values(comps, rules.min_comparables)
    resell_on, sell_fee = best_resale(value, by_platform, rules.sell_fees, item.source)
    if shipping is None and item.shipping is not None:
        shipping = item.shipping
    ship = 0.0 if pickup_only else (shipping if shipping is not None else rules.shipping_cost)
    cost = item.total_price + ship
    profit = round(value - sell_fee - cost - rules.resell_costs, 2)
    roi = round(profit / cost * 100, 1) if cost > 0 else 0.0
    rating = rate(profit, roi, n, rules)
    blocked = []
    if profit < rules.min_profit:
        blocked.append(f"profit under min_profit {rules.min_profit:g}")
    if roi < rules.min_roi:
        blocked.append(f"ROI under min_roi {rules.min_roi:g}%")
    if roi > rules.max_roi:
        blocked.append(f"ROI over max_roi {rules.max_roi:g}%")
    if rating < rules.min_rating:
        blocked.append(f"rating under min_rating {rules.min_rating}")
    by_price = sorted(comps, key=lambda p: p.price)
    sample = [by_price[i * (n - 1) // 4] for i in range(5)]   # cheapest, quartiles, priciest
    deal = Deal(item, value, n, profit, roi, rating, basis, sample,
                shipping=round(ship, 2), shipping_known=pickup_only or shipping is not None,
                packaging=rules.resell_costs, pickup_only=pickup_only, city=city, blocked=blocked,
                by_platform=by_platform, resell_on=resell_on, sell_fee=round(sell_fee, 2))
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
    return bool(deal and deal.blocked and deal.closeness >= NEAR_MISS_CLOSENESS
                and not any("max_roi" in b for b in deal.blocked))
