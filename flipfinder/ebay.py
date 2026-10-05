"""
eBay Buy It Now listings through the official Browse API (no scraping).

Needs an app keyset from developer.ebay.com in .env: EBAY_CLIENT_ID, EBAY_CLIENT_SECRET.
The app token (client credentials, public data only) lasts 2 hours and is reused.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass, field

import requests

from .vinted import Item

log = logging.getLogger(__name__)

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"


def item_from_ebay(raw: dict) -> Item | None:
    price = raw.get("price") or {}
    try:
        amount = float(price["value"])
        item_id = int(raw.get("legacyItemId") or str(raw["itemId"]).split("|")[1])
    except (KeyError, ValueError, TypeError, IndexError):
        return None
    loc = raw.get("itemLocation") or {}
    image = raw.get("image") or {}
    return Item(
        id=item_id,
        title=str(raw.get("title", "")).strip(),
        price=amount,
        total_price=amount,   # eBay shows prices with any buyer protection fee already in
        currency=price.get("currency", "EUR"),
        url=raw.get("itemWebUrl", f"https://www.ebay.it/itm/{item_id}"),
        condition=str(raw.get("condition") or ""),
        photo=image.get("imageUrl", ""),
        source="ebay",
        shipping=cheapest_shipping(raw.get("shippingOptions")),
        location=", ".join(x for x in (loc.get("city"), loc.get("country")) if x),
    )


def cheapest_shipping(options) -> float | None:
    """Lowest shipping cost offered, None if eBay doesn't say (e.g. calculated or pickup only)."""
    costs = []
    for o in options or []:
        try:
            costs.append(float(o["shippingCost"]["value"]))
        except (KeyError, TypeError, ValueError):
            continue
    return min(costs) if costs else None


@dataclass
class EbayClient:
    client_id: str
    client_secret: str
    marketplace: str = "EBAY_IT"
    item_location: str = "IT"          # "IT" or "EU"
    delivery_country: str = "IT"
    request_delay: float = 1.0
    calls: int = 0                     # API calls made by this client (searches + tokens)
    session: requests.Session = field(default_factory=requests.Session)

    def __post_init__(self):
        self._token = ""
        self._token_expires = 0.0

    def _auth(self) -> str:
        if time.time() < self._token_expires - 60:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        r = self.session.post(TOKEN_URL, timeout=20, data={"grant_type": "client_credentials", "scope": SCOPE},
                              headers={"Authorization": f"Basic {basic}",
                                       "Content-Type": "application/x-www-form-urlencoded"})
        self.calls += 1
        r.raise_for_status()
        data = r.json()
        self._token = data["access_token"]
        self._token_expires = time.time() + int(data.get("expires_in", 7200))
        return self._token

    def _filter(self, price_from: float | None, price_to: float | None) -> str:
        parts = ["buyingOptions:{FIXED_PRICE}", f"deliveryCountry:{self.delivery_country}"]
        if self.item_location.upper() == "EU":
            parts.append("itemLocationRegion:EUROPEAN_UNION")
        else:
            parts.append(f"itemLocationCountry:{self.item_location.upper()}")
        if price_from is not None or price_to is not None:
            lo = "" if price_from is None else f"{price_from:g}"
            hi = "" if price_to is None else f"{price_to:g}"
            parts += [f"price:[{lo}..{hi}]", "priceCurrency:EUR"]
        return ",".join(parts)

    def search(self, query: str, *, newest: bool = True, price_from: float | None = None,
               price_to: float | None = None, limit: int = 50, offset: int = 0) -> list[Item]:
        params = {"q": query, "filter": self._filter(price_from, price_to), "limit": limit, "offset": offset}
        if newest:
            params["sort"] = "newlyListed"
        headers = {
            "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
            # shipping costs are worked out for a buyer in this country
            "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country={self.delivery_country}",
        }
        for attempt in range(3):
            time.sleep(self.request_delay)
            headers["Authorization"] = f"Bearer {self._auth()}"
            r = self.session.get(SEARCH_URL, params=params, headers=headers, timeout=20)
            self.calls += 1
            if r.status_code == 401:
                self._token_expires = 0      # token expired early, get a new one
                continue
            if r.status_code == 429:
                log.warning("eBay rate limit hit, waiting")
                time.sleep(30 * (attempt + 1))
                continue
            r.raise_for_status()
            return [i for i in map(item_from_ebay, r.json().get("itemSummaries", [])) if i]
        raise RuntimeError(f"eBay kept refusing the search for {query!r}")
