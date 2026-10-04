"""One scan = for each search, grab new listings and check them against market value."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, replace
from pathlib import Path

from .analyzer import Deal, evaluate
from .config import Config, Search
from .storage import SeenStore
from .vinted import Item, VintedClient

log = logging.getLogger(__name__)


class PoolCache:
    """Comparable listings per search, saved to disk so cloud runs can reuse them."""

    def __init__(self, path: Path, max_age_min: float):
        self.path = path
        self.max_age = max_age_min * 60
        self.data: dict = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def get(self, key: str) -> list[Item] | None:
        entry = self.data.get(key)
        if not entry or time.time() - entry["ts"] > self.max_age:
            return None
        return [Item(**i) for i in entry["items"]]

    def put(self, key: str, items: list[Item]):
        self.data[key] = {"ts": time.time(), "items": [asdict(i) for i in items]}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data), encoding="utf-8")


def _key(s: Search) -> str:
    return json.dumps([s.query, s.price_from, s.price_to, s.filters], sort_keys=True)


class Scanner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = VintedClient(
            domain=cfg.domain, request_delay=cfg.request_delay,
            fee_fixed=cfg.buyer_fee_fixed, fee_pct=cfg.buyer_fee_pct,
        )
        self.seen = SeenStore(cfg.seen_file)
        self.first_run = self.seen.is_empty
        self.pools = PoolCache(cfg.seen_file.parent / "pools.json", cfg.pool_refresh_minutes)

    def _pool(self, s: Search, newest: list[Item]) -> list[Item]:
        key = _key(s)
        pool = self.pools.get(key)
        if pool is None:
            log.info("Building price pool for '%s'", s.query)
            pool = []
            for page in range(1, self.cfg.comparable_pages + 1):
                # no price_to here: we want the full market, not just cheap ones
                batch = self.client.search(s.query, order="relevance", page=page,
                                           price_from=s.price_from, extra=s.filters)
                pool += batch
                if len(batch) < 96:
                    break
            # relevance order shifts between requests, so pages can overlap a bit
            pool = list({i.id: i for i in pool}.values())
            self.pools.put(key, pool)
        by_id = {i.id: i for i in pool}
        for i in newest:
            by_id.setdefault(i.id, i)
        return list(by_id.values())

    def scan_search(self, s: Search) -> list[Deal]:
        newest = self.client.search(s.query, order="newest_first",
                                    price_from=s.price_from, price_to=s.price_to,
                                    extra=s.filters)
        fresh = [i for i in newest if i.id not in self.seen]
        log.info("'%s': %d listings, %d new", s.query, len(newest), len(fresh))
        if not fresh:
            return []
        pool = self._pool(s, newest)
        rules = replace(self.cfg.rules,
                        exclude_keywords=self.cfg.rules.exclude_keywords + tuple(s.exclude_keywords))
        if self.first_run:
            # Nothing seen yet: remember what's listed now instead of alerting on all of it
            for item in fresh:
                self.seen.add(item.id)
            return []
        deals = []
        for item in fresh:
            self.seen.add(item.id)
            deal = evaluate(item, pool, rules)
            if deal:
                deals.append(deal)
        return deals

    def scan(self) -> list[Deal]:
        deals: list[Deal] = []
        for s in self.cfg.searches:
            try:
                deals += self.scan_search(s)
            except Exception as e:  # one broken search shouldn't kill the rest
                log.error("Search '%s' failed: %s", s.query, e)
        if self.first_run:
            log.info("First run: saved %d current listings as seen, alerts start next scan",
                     len(self.seen.data))
            self.first_run = self.seen.is_empty
        self.seen.save()
        self.pools.save()
        deals.sort(key=lambda d: (d.rating, d.profit), reverse=True)
        return deals
