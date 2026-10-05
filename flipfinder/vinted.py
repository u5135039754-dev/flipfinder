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

    def _refresh_cookie(self):
        # Visiting the homepage gives us the anonymous session cookie the API needs
        self.session.cookies.clear()
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
            r = self.session.get(url, params=params, timeout=20)
            if r.status_code in (401, 403):
                log.info("Vinted said %s, getting a new cookie", r.status_code)
                self._refresh_cookie()
                continue
            if r.status_code == 429:
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


def parse_item_page(html: str) -> ItemDetails:
    data = "".join(json.loads(c) for c in _RSC_CHUNK.findall(html))
    details = ItemDetails()

    i = data.find('"shipping":{"type":"shipping"')
    if i >= 0:
        shipping, _ = json.JSONDecoder().raw_decode(data, i + len('"shipping":'))
        details.shipping, _ = _money(shipping.get("finalPrice") or shipping.get("originalPrice"))
    if '"isShippingAvailable":false' in data:
        details.shipping_available = False

    for name, key in (("description", "description"), ("user_info_header", "user_info")):
        m = re.search(r'"name":"%s","section"' % name, data)
        if not m:
            continue
        start = data.rfind('{"data":', 0, m.start())
        try:
            block, _ = json.JSONDecoder().raw_decode(data, start)
        except ValueError:
            continue
        value = block.get("data", {}).get(key)
        if name == "description":
            details.description = _text(value)
        else:
            # only there when the seller chose to show their city
            for entry in value or []:
                if entry.get("key") == "location":
                    details.city = _text(entry.get("text"))
    return details
