"""
Subito.it listings through the JSON API the Subito app uses (hades.subito.it).

The website and browser-style requests are blocked (Akamai), but app-style requests
work, from a PC and from GitHub's servers. It isn't a documented public API, so it
could change or get blocked; the scan carries on without Subito if it fails.

New listings are fetched once per category for the whole home area (one call each,
newest first) and then matched to the searches locally, so Subito adds only a few
calls per run however many searches there are. Market value uses Italy-wide pools.
"""

from __future__ import annotations

import logging
import math
import random
import re
import time
from dataclasses import dataclass, field

import requests

from .vinted import Item

log = logging.getLogger(__name__)

SEARCH_URL = "https://hades.subito.it/v1/search/items"
HEADERS = {"User-Agent": "Subito/8.0 (Android)", "X-Subito-Channel": "20", "Accept": "application/json"}

# Vinted catalog filter -> Subito category, so most searches need no extra config
CATEGORY_FOR_VINTED_CATALOG = {
    3661: 12, 3678: 12, 3035: 12,     # phones, earphones, smartwatches -> Telefonia
    3728: 10, 3580: 10, 3602: 10,     # tablets, laptops, graphics cards -> Informatica
    3025: 44,                         # consoles -> Console e Videogiochi
    4840: 39,                         # pedals -> Strumenti Musicali
    5433: 10,                         # calculators -> Informatica
    3075: 40,                         # digital cameras -> Fotografia
}
NEGOTIABLE = re.compile(r"\btrattabil[ei]\b|\bprezzo\s+tratt\b|\btratt\.", re.I)
NOT_NEGOTIABLE = re.compile(r"\bnon\s+(?:è\s+|e\s+)?trattabil|\bprezzo\s+fisso\b|\bnon\s+tratto\b", re.I)


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Straight-line distance (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def _feature(ad: dict, uri: str, part: str = "key") -> str:
    """A listing attribute: "key" is the machine value ("190", "1"), "value" the text ("190 €", "Sì")."""
    for f in ad.get("features") or []:
        if f.get("uri") == uri and f.get("values"):
            return str(f["values"][0].get(part) or "")
    return ""


def _number(text: str) -> float | None:
    m = re.search(r"\d+(?:[.,]\d+)?", text.replace(".", "").replace(" ", "")) if text else None
    return float(m.group(0).replace(",", ".")) if m else None


def item_from_subito(ad: dict) -> Item | None:
    try:
        item_id = int(str(ad["urn"]).rsplit(":", 1)[1])
    except (KeyError, ValueError, IndexError):
        return None
    price = _number(_feature(ad, "/price"))
    if price is None:
        return None
    ships = "1" in (_feature(ad, "/item_shippable"), _feature(ad, "/item_shipping_allowed"))
    # seller-managed shipping, or Subito's own (TuttoSubito); the cheaper one
    costs = [c for c in (_number(_feature(ad, "/item_shipping_cost")),
                         _number(_feature(ad, "/item_shipping_cost_tuttosubito"))) if c is not None]
    ship_cost = min(costs) if ships and costs else None
    town = ((ad.get("geo") or {}).get("town") or {})
    images = ad.get("images") or []
    photo = ""
    if images:
        photo = (images[0].get("cdn_base_url") or "") + "?rule=gallery-desktop-2x-auto" if images[0].get("cdn_base_url") else ""
    text = f"{ad.get('subject', '')}\n{ad.get('body', '')}"
    return Item(
        id=item_id,
        title=str(ad.get("subject", "")).strip(),
        price=price,
        total_price=price,          # no buyer fee for a local pickup
        currency="EUR",
        url=(ad.get("urls") or {}).get("default", ""),
        condition=_feature(ad, "/item_condition", "value").split(" - ")[0],
        photo=photo,
        photos=[i["cdn_base_url"] + "?rule=gallery-desktop-2x-auto" for i in images[:4] if i.get("cdn_base_url")],
        description=str(ad.get("body") or "")[:1500],
        source="subito",
        shipping=ship_cost,
        location=str(town.get("value") or ""),
        lat=town.get("lat"),
        lon=town.get("lon"),
        negotiable=bool(NEGOTIABLE.search(text)) and not NOT_NEGOTIABLE.search(text),
    )


@dataclass
class SubitoClient:
    region: int                           # Subito ids of the home area (private, from the Worker)
    province: int
    center: tuple[float, float]           # home coordinates
    radius_km: float = 30
    travel_cost: float = 5.0
    town_travel_costs: dict = field(default_factory=dict)   # e.g. {"Town": 8}
    request_delay: float = 2.0
    calls: int = 0
    session: requests.Session = field(default_factory=requests.Session)

    def __post_init__(self):
        self.session.headers.update(HEADERS)
        self._town_costs = {k.lower(): float(v) for k, v in self.town_travel_costs.items()}

    def _get(self, params: dict) -> list[dict]:
        for attempt in range(3):
            time.sleep(self.request_delay + random.uniform(0, 1))
            r = self.session.get(SEARCH_URL, params={"t": "s", **params}, timeout=20)
            self.calls += 1
            if r.status_code == 429:
                log.warning("Subito rate limit, waiting")
                time.sleep(30 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json().get("ads", [])
        raise RuntimeError("Subito kept refusing the search")

    def in_area(self, item: Item) -> bool:
        if item.lat is None or item.lon is None:
            return False
        item.distance_km = round(distance_km(item.lat, item.lon, *self.center), 1)
        return item.distance_km <= self.radius_km

    def delivery(self, item: Item) -> Item:
        """Cheaper of picking it up (travel cost) and the seller's shipping, if they ship."""
        travel = self._town_costs.get(item.location.lower(), self.travel_cost)
        if item.shipping is not None and item.shipping < travel:
            item.delivery = "shipping"
        else:
            item.shipping, item.delivery = travel, "pickup"
        return item

    def newest_in_area(self, category: int, limit: int = 100) -> list[Item]:
        """Newest listings of one category in the home province, within the radius."""
        ads = self._get({"c": category, "r": self.region, "ci": self.province,
                         "sort": "datedesc", "lim": limit, "start": 0})
        items = [i for i in map(item_from_subito, ads) if i]
        # Subito's own condition field: "Danneggiato" = damaged / for parts
        items = [i for i in items if not i.condition.lower().startswith("danneggiat")]
        return [self.delivery(i) for i in items if self.in_area(i)]

    def pool(self, query: str, category: int, price_from: float | None, limit: int = 100) -> list[Item]:
        """Italy-wide listings for market value (location doesn't matter for prices)."""
        params = {"q": query, "c": category, "lim": limit, "start": 0}
        if price_from is not None:
            params["ps"] = f"{price_from:g}"
        return [i for i in map(item_from_subito, self._get(params)) if i]
