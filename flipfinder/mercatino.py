"""
Mercatino Musicale (mercatinomusicale.com), with the site's written permission for personal use, on its
conditions (keep to them, they're the reason we may read it at all):
- only the public search and listing pages, never /ajax/, no account, no internal APIs;
- one request every 5-10 seconds at most, and no more than ~1,000 pages a day in total (DAILY_CAP stays under);
- no republishing or redistribution of the data, the listings or the images (deals go out as text, without
  their photos, and link to the listing on their site);
- a clearly identifiable User-Agent with a contact email (from the Worker's private settings, never the repo).

Only search result pages are read (25 listings each, newest first): each gives title, a short description,
price, condition, city and date, enough to value a listing, so no listing pages are opened. The listings seen
are kept per search as Mercatino's own comparables (data/mercatino.json).
"""

from __future__ import annotations

import html
import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from .vinted import Item

log = logging.getLogger(__name__)

BASE = "https://www.mercatinomusicale.com"
SEARCH_PATH = "/ann/search.asp"
DAILY_CAP = 900                 # their limit is ~1,000 pages a day; stay under it
DELAY = (5.0, 10.0)             # seconds before every request, as they asked
POOL_DAYS = 30                  # how long a seen listing stays a comparable
POOL_MAX = 300                  # per search

# only listings on the site (/mm/...): the sponsored "turbo" boxes link to a shop's new products and are skipped
_BOX = re.compile(r'<a href="(/mm/a_[^"]+_id(\d+)\.html)"\s+class="box_prod_link"[^>]*>.*?</a>', re.S)
_TIT = re.compile(r'<span class="tit"><span>(.*?)</span></span>', re.S)
_DESC = re.compile(r'<span class="desc">(.*?)</span>', re.S)
_PRICE = re.compile(r'<span class="prz">.*?</span>([\d.]+)<span class="dec"><span class="comma">,</span>(\d+)</span>', re.S)
_COND = re.compile(r'<span class="cond[^"]*">(.*?)<span class="usr', re.S)
_PLACE = re.compile(r'<span class="place">(.*?)</span>', re.S)


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def parse_search_page(page: str) -> list[Item]:
    """Listings on a search result page: id, title, description snippet, price, condition, city. No photos."""
    items = []
    for m in _BOX.finditer(page):
        block = m.group(0)
        price = _PRICE.search(block)
        title = _TIT.search(block)
        if not price or not title:
            continue
        value = float(price.group(1).replace(".", "") + "." + price.group(2))
        place = _PLACE.search(block)
        items.append(Item(
            id=int(m.group(2)), title=_text(title.group(1)), price=value, total_price=value, currency="EUR",
            url=BASE + m.group(1), condition=_text(_COND.search(block).group(1)).capitalize() if _COND.search(block) else "",
            location=_text(place.group(1)) if place else "", description=_text(_DESC.search(block).group(1)) if _DESC.search(block) else "",
            source="mercatino"))
    return items


class MercatinoClient:
    """Polite reader for Mercatino Musicale's public search pages, within the permission's limits."""

    def __init__(self, contact: str, state_path: Path, daily_cap: int = DAILY_CAP, delay: tuple = DELAY):
        if not contact or "@" not in contact:
            raise ValueError("Mercatino Musicale needs a contact email for the User-Agent")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": f"flipFinder (personal use; contact: {contact})",
                                     "Accept-Language": "it-IT,it;q=0.9"})
        self.path, self.daily_cap, self.delay = state_path, daily_cap, delay
        self.state = {"day": "", "pages": 0, "pools": {}, "seeded": [], "turn": 0}
        if state_path.exists():
            try:
                self.state.update(json.loads(state_path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        self.requests = 0

    @property
    def pages_today(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        if self.state["day"] != today:
            self.state["day"], self.state["pages"] = today, 0
        return self.state["pages"]

    def can_fetch(self) -> bool:
        return self.pages_today < self.daily_cap

    def _get(self, path: str, params: dict) -> str:
        if path.startswith("/ajax/") or not path.startswith(("/ann/", "/mm/", "/cerca/")):
            raise ValueError(f"not a public listing page: {path}")   # the permission covers nothing else
        if not self.can_fetch():
            raise RuntimeError("Mercatino Musicale: today's page limit reached")
        time.sleep(random.uniform(*self.delay))
        self.state["pages"] += 1
        self.requests += 1
        r = self.session.get(BASE + path, params=params, timeout=30)
        if r.status_code in (403, 429):
            raise RuntimeError(f"Mercatino Musicale answered {r.status_code}")
        r.raise_for_status()
        return r.content.decode("utf-8", "replace")

    def search(self, query: str, page: int = 1) -> list[Item]:
        """Newest listings for a search (25 a page)."""
        return parse_search_page(self._get(SEARCH_PATH, {"kw": query, **({"pg": page} if page > 1 else {})}))

    # --- Mercatino's own comparables: what we've seen per search, for 30 days
    def remember(self, query: str, items: list[Item]):
        now = time.time()
        pool = {str(x["id"]): x for x in self.state["pools"].get(query, []) if now - x["at"] < POOL_DAYS * 86400}
        for i in items:
            pool[str(i.id)] = {"id": i.id, "title": i.title, "price": i.price, "condition": i.condition, "at": now}
        self.state["pools"][query] = sorted(pool.values(), key=lambda x: -x["at"])[:POOL_MAX]

    def pool(self, query: str) -> list[Item]:
        return [Item(id=x["id"], title=x["title"], price=x["price"], total_price=x["price"], currency="EUR",
                     url=f"{BASE}/mm/a_x_id{x['id']}.html", condition=x.get("condition", ""), source="mercatino")
                for x in self.state["pools"].get(query, [])]

    def seeded(self, query: str) -> bool:
        return query in self.state["seeded"]

    def mark_seeded(self, query: str):
        self.state["seeded"] = sorted(set(self.state["seeded"]) | {query})

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state), encoding="utf-8")
