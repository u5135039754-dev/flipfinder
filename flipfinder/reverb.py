"""
Reverb (reverb.com) through its official API, for music searches, with a personal access token (REVERB_TOKEN, a
GitHub secret; read-only "public" scope). Under Reverb's API terms:
- deals go out as text with a link to the listing, never with Reverb's photos;
- Reverb prices are only used to value Reverb deals, never Vinted/Subito/Mercatino ones (the API isn't used to
  send buyers to other sites);
- nothing is sold, shared or published; requests stay far under the rate limit (2,000 GETs a minute).

Reverb has no public sold prices any more (the price guide endpoint was closed), so a listing is compared with the
median ASKING price of the same make + model (Jackson: model line and body shape) in the same condition, and only
alerts when it's at least DISCOUNT under that median, because asking prices run high. Profit assumes reselling on
Reverb: its selling fee (5% of the sale incl. shipping) and payment processing (3.19% + €0.49 in Italy).
"""

from __future__ import annotations

import logging
import re
import time
from statistics import median

import requests

from .analyzer import Deal, Rules, blocked_reasons, jackson_kind, normalize, rate, remove_outliers
from .vinted import Item

log = logging.getLogger(__name__)

API = "https://api.reverb.com/api"
GOOD = ("mint", "excellent", "very-good", "good")   # used, Good or better
DISCOUNT = 0.25            # at least 25% under the median asking price
SELL_PCT = 5.0 + 3.19      # Reverb selling fee + payment processing (Italy, EUR)
SELL_FIXED = 0.49
MIN_COMPS = 5
PER_PAGE = 50
CONDITION_NAMES = {"mint": "Mint", "excellent": "Excellent", "very-good": "Very good", "good": "Good"}


def _money(m: dict | None) -> float | None:
    try:
        return float(m["amount"]) if m and m.get("currency") == "EUR" else None
    except (KeyError, TypeError, ValueError):
        return None


def item_from_reverb(raw: dict) -> Item | None:
    """A listing as an Item (no photos: Reverb's images aren't passed on). Shipping to Italy (else the EU rate) when listed."""
    price = _money(raw.get("price"))
    cond = (raw.get("condition") or {}).get("slug", "")
    if price is None or not raw.get("id"):
        return None
    rates = {r.get("region_code"): _money(r.get("rate")) for r in (raw.get("shipping") or {}).get("rates") or []}
    ship = rates.get("IT", rates.get("EUR_EU"))
    url = ((raw.get("_links") or {}).get("web") or {}).get("href") or f"https://reverb.com/item/{raw['id']}"
    return Item(id=int(raw["id"]), title=str(raw.get("title") or "").strip(), price=price, total_price=price, currency="EUR",
                url=url, brand=str(raw.get("make") or ""), size=str(raw.get("model") or ""), condition=cond,
                source="reverb", shipping=ship, description=str(raw.get("description") or "")[:600])


def model_key(item: Item, model: str = "") -> str:
    """Same make + model (Reverb's own fields); Jackson: the model line and body shape."""
    if model == "jackson":
        return "jackson " + jackson_kind(f"{item.title} {item.size}")
    words = [w for w in normalize(f"{item.brand} {item.size}") if w not in ("guitar", "electric", "chitarra")]
    return " ".join(words)


class ReverbClient:
    def __init__(self, token: str, region: str = "eu", delay: float = 1.0):
        if not token:
            raise ValueError("Reverb needs REVERB_TOKEN")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/hal+json",
                                     "Accept-Version": "3.0", "X-Display-Currency": "EUR"})
        self.region, self.delay, self.requests = region, delay, 0

    def _get(self, params: dict) -> list[dict]:
        time.sleep(self.delay)
        self.requests += 1
        r = self.session.get(f"{API}/listings", params=params, timeout=30)
        if r.status_code == 429:
            raise RuntimeError("Reverb rate limit")
        r.raise_for_status()
        return r.json().get("listings") or []

    def newest(self, query: str, price_from: float | None, price_to: float | None) -> list[Item]:
        """Newest used listings that ship to Italy, from the EU (or anywhere with /reverb all), Good or better."""
        params = {"query": query, "condition": "used", "ships_to": "IT", "sort": "published_at|desc", "per_page": PER_PAGE}
        if self.region == "eu":
            params["item_region"] = "EUR_EU"
        if price_from is not None:
            params["price_min"] = f"{price_from:g}"
        if price_to is not None:
            params["price_max"] = f"{price_to:g}"
        return [i for i in map(item_from_reverb, self._get(params)) if i and i.condition in GOOD]

    def pool(self, query: str, pages: int = 2) -> list[Item]:
        """Asking prices for the same search, any region, used (the comparables)."""
        out = []
        for page in range(1, pages + 1):
            batch = self._get({"query": query, "condition": "used", "per_page": PER_PAGE, "page": page})
            out += [i for i in map(item_from_reverb, batch) if i]
            if len(batch) < PER_PAGE:
                break
        return out


def assess_reverb(item: Item, pool: list[Item], rules: Rules, model: str = "") -> Deal | None:
    """
    A Reverb listing against the median asking price of the same model and condition: None without enough of them;
    otherwise a Deal whose `blocked` says what fails (incl. not 25% under the median).
    """
    key = model_key(item, model)
    if not key or item.condition not in GOOD:
        return None
    comps = [p for p in pool if p.id != item.id and p.condition == item.condition and model_key(p, model) == key]
    kept = remove_outliers([p.price for p in comps])
    if len(kept) < MIN_COMPS:
        return None
    comps = [p for p in comps if kept[0] <= p.price <= kept[-1]]
    asking = round(median(kept), 2)
    ship_in = item.shipping if item.shipping is not None else rules.shipping_cost
    cost = round(item.price + ship_in, 2)
    sell_fee = round((asking + rules.shipping_cost) * SELL_PCT / 100 + SELL_FIXED, 2)   # the fee counts shipping too
    profit = round(asking - sell_fee - cost - rules.resell_costs, 2)
    roi = round(profit / cost * 100, 1) if cost else 0.0
    rating = rate(profit, roi, len(comps), rules)
    blocked = blocked_reasons(profit, roi, rating, rules, cost)
    if item.price > asking * (1 - DISCOUNT):
        blocked.append(f"not {DISCOUNT:.0%} under the Reverb median (€{asking:.0f})")
    deal = Deal(item, asking, len(comps), profit, roi, rating, f"Reverb {key}, {CONDITION_NAMES.get(item.condition, item.condition)}",
                comps[:5], shipping=round(ship_in, 2), shipping_known=item.shipping is not None, packaging=rules.resell_costs,
                blocked=blocked, by_platform={"reverb": (asking, len(comps))}, resell_on="reverb", sell_fee=sell_fee, cost=cost,
                asking_value=asking, check=rules.check)
    deal.kind = key if model == "jackson" else ""
    return deal
