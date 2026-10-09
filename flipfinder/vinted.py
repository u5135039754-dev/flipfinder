"""
Small client for Vinted search results.

The old JSON endpoint (/api/v2/catalog/items) now returns 404. The website renders
search results server-side instead, so we load the /catalog page (same query
params) and read the items out of the Next.js data embedded in it.
"""

from __future__ import annotations

import json
import logging
import re
import random
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)


@dataclass
class Item:
    id: int
    title: str
    price: float          # listed price
    total_price: float    # price + buyer protection fee (what you actually pay)
    currency: str
    url: str
    brand: str = ""
    size: str = ""
    condition: str = ""
    photo: str = ""
    favourites: int = 0
    source: str = "vinted"            # which platform it's listed on: "vinted", "ebay" or "subito"
    shipping: float | None = None     # what getting it costs when the listing says (eBay shipping, Subito pickup/shipping)
    location: str = ""
    lat: float | None = None          # Subito: the town's coordinates
    lon: float | None = None
    distance_km: float | None = None  # Subito: from the home town
    delivery: str = ""                # Subito: "pickup" (travel cost) or "shipping"
    negotiable: bool = False          # Subito: "trattabile"
    seller: dict | None = None        # trust: reviews, rating, items sold... (seller_line in telegram.py)
    photos: list = field(default_factory=list)   # up to 4 photo URLs, for the AI check (not kept in the pools)
    description: str = ""             # the seller's text (Vinted: from the item page), for the AI check
    country: str = ""                 # where it ships from, ISO code ("IT", "ES"): Vinted, from the seller's profile

    @property
    def key(self) -> str:
        """Unique across platforms, for the seen store."""
        return f"{self.source}:{self.id}"

    @classmethod
    def from_api(cls, raw: dict, domain: str, fee_fixed: float, fee_pct: float) -> "Item | None":
        price, currency = _money(raw.get("price"))
        if price is None:
            return None
        total, _ = _money(raw.get("total_item_price"))
        if total is None:
            # Vinted didn't give us the total, so estimate the buyer protection fee
            total = round(price + fee_fixed + price * fee_pct / 100, 2)
        photo = (raw.get("photo") or {}).get("url", "") if isinstance(raw.get("photo"), dict) else ""
        url = raw.get("url") or f"https://{domain}/items/{raw.get('id')}"
        return cls(
            id=int(raw["id"]),
            title=str(raw.get("title", "")).strip(),
            price=price,
            total_price=total,
            currency=currency or "EUR",
            url=url,
            brand=str(raw.get("brand_title") or ""),
            size=str(raw.get("size_title") or ""),
            condition=str(raw.get("status") or ""),
            photo=photo,
            favourites=int(raw.get("favourite_count") or 0),
        )

    @classmethod
    def from_page(cls, raw: dict, domain: str, fee_fixed: float, fee_pct: float) -> "Item | None":
        """Item from the catalog page data (camelCase fields, brand/size/condition in itemBox)."""
        price, currency = _money(raw.get("price"))
        if price is None:
            return None
        total, _ = _money(raw.get("totalItemPrice"))
        if total is None:
            total = round(price + fee_fixed + price * fee_pct / 100, 2)
        box = raw.get("itemBox") or {}
        # secondLine is "42,5 · Nuovo senza cartellino", or just the condition when there's no size
        parts = [x.strip() for x in str(box.get("secondLine") or "").split("·") if x.strip()]
        condition = parts[-1] if parts else ""
        size = " · ".join(parts[:-1])
        photos = raw.get("photos") or []
        photo = photos[0].get("url", "") if photos and isinstance(photos[0], dict) else ""
        url = raw.get("url") or f"/items/{raw.get('id')}"
        if url.startswith("/"):
            url = f"https://{domain}{url}"
        return cls(
            id=int(raw["id"]),
            title=str(raw.get("title", "")).strip(),
            price=price,
            total_price=total,
            currency=currency or "EUR",
            url=url,
            brand=_text(box.get("firstLine")),
            size=size,
            condition=condition,
            photo=photo or _text(raw.get("thumbnailUrl")),
            photos=[p["url"] for p in photos[:4] if isinstance(p, dict) and p.get("url")],
            favourites=int(raw.get("favouriteCount") or 0),
        )


def _text(value) -> str:
    # The page data uses "$undefined" for missing values
    return "" if value in (None, "$undefined") else str(value)


def _money(value) -> tuple[float | None, str]:
    """Vinted returns prices as '12.0' or {'amount': '12.0', 'currency_code': 'EUR'}."""
    if value is None:
        return None, ""
    if isinstance(value, dict):
        try:
            return float(value.get("amount")), value.get("currency_code") or value.get("currencyCode") or ""
        except (TypeError, ValueError):
            return None, ""
    try:
        return float(value), ""
    except (TypeError, ValueError):
        return None, ""


@dataclass
class VintedClient:
    domain: str = "www.vinted.it"
    request_delay: float = 2.0
    fee_fixed: float = 0.70
    fee_pct: float = 5.0
    session: requests.Session = field(default_factory=requests.Session)

    def __post_init__(self):
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
        })
        self._has_cookie = False
        self.blocked = 0   # 403/429 answers this run (sold-price checks back off when there are any)
        self.requests = 0  # page loads this run (for the fast lane's numbers)

    def _refresh_cookie(self):
        # Visiting the homepage gives us the anonymous session cookie the API needs
        self.session.cookies.clear()
        self.requests += 1
        r = self.session.get(f"https://{self.domain}/", timeout=20)
        r.raise_for_status()
        self._has_cookie = True
        log.debug("Got Vinted cookies: %s", list(self.session.cookies.keys()))

    def _get(self, path: str, params: dict) -> requests.Response:
        if not self._has_cookie:
            self._refresh_cookie()
        url = f"https://{self.domain}{path}"
        for attempt in range(4):
            time.sleep(self.request_delay + random.uniform(0, 1))
            self.requests += 1
            r = self.session.get(url, params=params, timeout=20)
            if r.status_code in (401, 403):
                self.blocked += r.status_code == 403
                log.info("Vinted said %s, getting a new cookie", r.status_code)
                self._refresh_cookie()
                continue
            if r.status_code == 429:
                self.blocked += 1
                wait = 30 * (attempt + 1)
                log.warning("Rate limited by Vinted, waiting %ss", wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        raise RuntimeError(f"Vinted kept refusing {path} after retries")

    def details(self, item: "Item") -> "ItemDetails":
        r = self._get(f"/items/{item.id}", {})
        r.encoding = "utf-8"
        return parse_item_page(r.text)

    def item_status(self, item_id: int) -> str:
        """
        "sold", "active", "reserved", "deleted", "unknown", or "blocked" (403/429: stop checking).
        One request, no retries: these checks are optional and must never add to a block.
        """
        if not self._has_cookie:
            try:
                self._refresh_cookie()
            except requests.RequestException:
                return "unknown"
        time.sleep(self.request_delay + random.uniform(0, 1))
        try:
            self.requests += 1
            r = self.session.get(f"https://{self.domain}/items/{item_id}", timeout=20)
        except requests.RequestException:
            return "unknown"
        if r.status_code in (403, 429):
            self.blocked += 1
            return "blocked"
        if r.status_code in (404, 410):
            return "deleted"
        if r.status_code != 200 or f"/items/{item_id}" not in r.url:
            return "unknown"
        r.encoding = "utf-8"
        return item_page_status("".join(json.loads(c) for c in _RSC_CHUNK.findall(r.text)))

    def seller_profile(self, seller_id: int) -> dict:
        r = self._get(f"/member/{seller_id}", {})
        r.encoding = "utf-8"
        return parse_profile_page(r.text)

    def search(self, query: str, *, order: str = "newest_first", page: int = 1,
               per_page: int = 96, price_from: float | None = None,
               price_to: float | None = None, extra: dict | None = None) -> list[Item]:
        params: dict = {
            "search_text": query,
            "order": order,
            "page": page,
            "per_page": per_page,
        }
        if price_from is not None:
            params["price_from"] = price_from
        if price_to is not None:
            params["price_to"] = price_to
        # extra filters like brand_ids[], size_ids[], status_ids[], catalog_ids[]
        for key, val in (extra or {}).items():
            params[f"{key}[]" if isinstance(val, list) else key] = val

        r = self._get("/catalog", params)
        r.encoding = "utf-8"
        items = []
        for raw in parse_catalog_page(r.text):
            item = Item.from_page(raw, self.domain, self.fee_fixed, self.fee_pct)
            if item:
                items.append(item)
        return items


_RSC_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
_ITEMS_MARKER = '"items":{"items":['


def parse_catalog_page(html: str) -> list[dict]:
    """Pull the search result items out of a /catalog page's embedded Next.js data."""
    data = "".join(json.loads(c) for c in _RSC_CHUNK.findall(html))
    start = data.find(_ITEMS_MARKER)
    if start < 0:
        if "self.__next_f" not in html:
            raise RuntimeError("Vinted catalog page has no Next.js data (blocked or captcha page?)")
        raise RuntimeError("Couldn't find items in the Vinted catalog page, the layout may have changed")
    raw, _ = json.JSONDecoder().raw_decode(data, start + len(_ITEMS_MARKER) - 1)
    return [x["productItem"] for x in raw
            if isinstance(x, dict) and isinstance(x.get("productItem"), dict)]


@dataclass
class ItemDetails:
    """What only the item page tells us: shipping price, description, seller city."""
    shipping: float | None = None      # cheapest delivery option for the buyer, None if unknown
    shipping_available: bool = True
    description: str = ""
    city: str = ""
    seller_id: int | None = None
    reviews: int | None = None         # feedback_count
    reputation: float | None = None    # 0-1 (x5 = stars)
    business: bool = False
    last_seen: str = ""                # "Ultima visita 2 ore fa"
    badges: list = field(default_factory=list)   # e.g. SPEEDY_SHIPPING, ACTIVE_LISTER
    photos: list = field(default_factory=list)   # every photo of the listing, 800 px (the most Vinted serves), main first


def parse_item_page(html: str) -> ItemDetails:
    data = "".join(json.loads(c) for c in _RSC_CHUNK.findall(html))
    details = ItemDetails()

    i = data.find('"shipping":{"type":"shipping"')
    if i >= 0:
        shipping, _ = json.JSONDecoder().raw_decode(data, i + len('"shipping":'))
        details.shipping, _ = _money(shipping.get("finalPrice") or shipping.get("originalPrice"))
    if '"isShippingAvailable":false' in data:
        details.shipping_available = False

    # the listing's own photos come first on the page (similar items' photos follow)
    i = data.find('"photos":[')
    if i >= 0:
        try:
            photos, _ = json.JSONDecoder().raw_decode(data, i + len('"photos":'))
            photos = sorted((p for p in photos if isinstance(p, dict) and p.get("url")), key=lambda p: not p.get("is_main"))
            details.photos = [p["url"] for p in photos]
        except ValueError:
            pass

    for name, key in (("description", "description"), ("user_info_header", "user_info"),
                      ("seller_badges_info", "badges")):
        m = re.search(r'"name":"%s","section"' % name, data)
        if not m:
            continue
        start = data.rfind('{"data":', 0, m.start())
        try:
            block, _ = json.JSONDecoder().raw_decode(data, start)
        except ValueError:
            continue
        block = block.get("data", {})
        value = block.get(key)
        if name == "description":
            details.description = _text(value)
        elif name == "seller_badges_info":
            details.badges = [str(b.get("type")) for b in value or [] if isinstance(b, dict) and b.get("type")]
        else:
            for entry in value or []:
                if entry.get("key") == "location":   # only there when the seller chose to show their city
                    details.city = _text(entry.get("text"))
                elif entry.get("key") == "last-logged-in":
                    details.last_seen = _text(entry.get("text"))
            try:
                details.seller_id = int(block["seller_id"]) if block.get("seller_id") else None
            except (TypeError, ValueError):
                details.seller_id = None
            if block.get("feedback_count") is not None:
                details.reviews = int(block["feedback_count"])
                details.reputation = float(block.get("feedback_reputation") or 0)
            details.business = bool(block.get("business"))
    return details


_PROFILE = {"sold": "given_item_count", "bought": "taken_item_count", "listed": "item_count",
            "positive": "positive_feedback_count", "negative": "negative_feedback_count",
            "reviews": "feedback_count"}


def parse_profile_page(html: str) -> dict:
    """Seller numbers from /member/<id>: items sold (given), reviews by kind, items for sale."""
    data = "".join(json.loads(c) for c in _RSC_CHUNK.findall(html))
    out = {}
    for name, key in _PROFILE.items():
        m = re.search(r'"%s"\s*:\s*(\d+)' % key, data)
        if m:
            out[name] = int(m.group(1))
    m = re.search(r'"country_iso_code"\s*:\s*"([A-Z]{2})"', data)   # vinted.it also lists other countries' items
    if m:
        out["country"] = m.group(1)
    return out


_SEEN = [(r"(\d+) minut", "{} min"), (r"un minuto", "1 min"), (r"(\d+) or[ae]", "{} h"), (r"un'ora", "1 h"),
         (r"(\d+) giorn", "{} days"), (r"un giorno|ieri", "1 day"), (r"(\d+) settiman", "{} weeks"),
         (r"una settimana", "1 week"), (r"(\d+) mes", "{} months"), (r"un mese", "1 month"), (r"adesso|ora$|online", "now")]


def seen_english(text: str) -> str:
    """'Ultima visita 2 ore fa' -> 'seen 2 h ago' (Vinted shows it in Italian)."""
    low = text.lower()
    for pattern, out in _SEEN:
        m = re.search(pattern, low)
        if m:
            return "online now" if out == "now" else "seen " + out.format(*m.groups()) + " ago"
    return ""


def item_page_status(data: str) -> str:
    """From an item page's data: sold listings can't be bought but aren't hidden or reserved."""
    if '"is_reserved":true' in data:
        return "reserved"
    if '"is_hidden":true' in data:
        return "deleted"
    if '"can_buy":true' in data or '"availability":"InStock"' in data:
        return "active"
    if '"can_buy":false' in data and '"is_hidden":false' in data:
        return "sold"
    return "unknown"
