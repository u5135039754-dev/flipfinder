"""One scan = for each search, grab new listings and check them against market value."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from .analyzer import Deal, Rules, assess, is_near_miss, is_pickup_only, is_relevant
from .config import Config, Search
from .ebay import CATEGORY_FOR_VINTED_CATALOG as EBAY_CATEGORY_FOR_VINTED_CATALOG
from .ebay import EbayClient
from .subito import CATEGORY_FOR_VINTED_CATALOG, SubitoClient
from .storage import SeenStore
from .vinted import Item, VintedClient

log = logging.getLogger(__name__)

# Rebuilding a price pool costs 3 page loads (~11 s). With many searches they'd all
# expire together and push a run past the workflow timeout, so only this many are
# rebuilt per run; the rest keep their older pool until their turn.
MAX_POOL_REBUILDS = 4
# A newly added search alerts on at most this many of the deals already listed
# (the best ones); the rest of its current listings are just remembered.
SEED_ALERTS = 3
# Subito and eBay price pools are one call each; at most this many are rebuilt per run
MAX_SUBITO_POOLS = 6
MAX_EBAY_POOLS = 10


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

    def has(self, key: str) -> bool:
        return key in self.data

    def get(self, key: str, any_age: bool = False, max_age_min: float | None = None) -> list[Item] | None:
        entry = self.data.get(key)
        max_age = self.max_age if max_age_min is None else max_age_min * 60
        if not entry or (not any_age and time.time() - entry["ts"] > max_age):
            return None
        return [Item(**i) for i in entry["items"]]

    def put(self, key: str, items: list[Item]):
        self.data[key] = {"ts": time.time(), "items": [asdict(i) for i in items]}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data), encoding="utf-8")


class EbayState:
    """When eBay was last searched, and API calls per day (eBay's limit is per day)."""

    def __init__(self, path: Path):
        self.path = path
        self.data = {"last_scan": 0.0, "calls": {}}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass

    def due(self, interval_min: float) -> bool:
        return time.time() - self.data["last_scan"] >= interval_min * 60 - 30   # a little slack for run jitter

    def record(self, calls: int, scanned: bool):
        today = datetime.now(timezone.utc).date().isoformat()
        self.data["calls"] = {today: self.data["calls"].get(today, 0) + calls}   # keep today only
        if scanned:
            self.data["last_scan"] = time.time()

    @property
    def calls_today(self) -> int:
        return self.data["calls"].get(datetime.now(timezone.utc).date().isoformat(), 0)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data), encoding="utf-8")


class KnownSearches:
    """Searches that have run before, so a newly added one starts silently."""

    def __init__(self, path: Path, pools: "PoolCache"):
        self.path = path
        self.keys: set[str] = set()
        if path.exists():
            try:
                self.keys = set(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass
        else:
            # Before this file existed, a search that ran has a price pool
            self.keys = {k for k in pools.data if not k.startswith("ebay ")}

    def has(self, s: "Search") -> bool:
        return _key(s) in self.keys

    def add(self, s: "Search"):
        self.keys.add(_key(s))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sorted(self.keys)), encoding="utf-8")


def _key(s: Search) -> str:
    return json.dumps([s.query, s.price_from, s.price_to, s.filters], sort_keys=True)


def _merge(*groups: list[Item]) -> list[Item]:
    by_key = {}
    for group in groups:
        for i in group:
            by_key.setdefault(i.key, i)
    return list(by_key.values())


class Scanner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = VintedClient(
            domain=cfg.domain, request_delay=cfg.request_delay,
            fee_fixed=cfg.buyer_fee_fixed, fee_pct=cfg.buyer_fee_pct,
        )
        e = cfg.ebay
        self.ebay = EbayClient(e.client_id, e.client_secret, marketplace=e.marketplace,
                               item_location=e.item_location) if e.enabled else None
        sb = cfg.subito
        self.subito = SubitoClient(region=sb.region, province=sb.province, center=sb.center,
                                   radius_km=sb.radius_km, travel_cost=sb.travel_cost,
                                   town_travel_costs=sb.town_travel_costs,
                                   request_delay=cfg.request_delay) if sb.enabled else None
        self.subito_new: list[Item] = []
        self.first_subito = False
        self.subito_pools = 0
        self.ebay_pools = 0
        data_dir = cfg.seen_file.parent
        self.seen = SeenStore(cfg.seen_file)
        self.first_run = self.seen.is_empty
        self.pools = PoolCache(data_dir / "pools.json", cfg.pool_refresh_minutes)
        self.known = KnownSearches(data_dir / "searches.json", self.pools)
        self.ebay_state = EbayState(data_dir / "ebay.json")
        self.ebay_due = False
        self.pool_rebuilds = 0
        self.first_ebay = False
        # filled by each scan(), for logs and the daily summary
        self.checked = 0
        self.failed_searches = 0
        self.near_misses: list[tuple[str, Deal]] = []   # (query, deal it would have been)

    # --- price pools -----------------------------------------------------------

    def _vinted_pool(self, s: Search, force: bool = False) -> list[Item] | None:
        """
        The price pool, or None if it doesn't exist yet and this run's rebuilds are used
        up. `force` builds it anyway (a new search's first run).
        """
        key = _key(s)
        pool = self.pools.get(key)
        if pool is None and self.pool_rebuilds >= MAX_POOL_REBUILDS and not force:
            old = self.pools.get(key, any_age=True)
            if old is None:
                return None
            return old   # a bit stale, refreshed in a later run
        if pool is None:
            self.pool_rebuilds += 1
            log.info("Building price pool for '%s'", s.query)
            pool = []
            for page in range(1, self.cfg.comparable_pages + 1):
                # no price_to here: we want the full market, not just cheap ones
                try:
                    batch = self.client.search(s.query, order="relevance", page=page,
                                               price_from=s.price_from, extra=s.filters)
                except Exception as e:
                    if page == 1:
                        raise
                    log.warning("'%s': price pool page %d failed (%s), using %d listings",
                                s.query, page, e, len(pool))
                    break
                pool += batch
                if len(batch) < 96:
                    break
            # relevance order shifts between requests, so pages can overlap a bit
            pool = _merge(pool)
            self.pools.put(key, pool)
        return pool

    def _ebay_category(self, s: Search) -> int:
        if s.ebay_category is not None:
            return s.ebay_category
        for cat in s.filters.get("catalog", []) or []:
            if int(cat) in EBAY_CATEGORY_FOR_VINTED_CATALOG:
                return EBAY_CATEGORY_FOR_VINTED_CATALOG[int(cat)]
        return self.cfg.ebay.default_category

    def _ebay_pool(self, s: Search) -> list[Item]:
        cat = self._ebay_category(s)
        key = f"ebay {cat} " + _key(s)   # the category is in the key: pools from before it are ignored
        pool = self.pools.get(key, max_age_min=self.cfg.ebay.pool_refresh_minutes)
        if pool is None and self.ebay_due and self.ebay_pools < MAX_EBAY_POOLS:
            self.ebay_pools += 1
            log.info("Building eBay price pool for '%s'", s.query)
            pool = self.ebay.search(s.query, newest=False, price_from=s.price_from,
                                    limit=self.cfg.ebay.pool_size, category=cat)
            self.pools.put(key, pool)
        # between eBay scans an older pool is fine for comparing Vinted listings
        return pool if pool is not None else (self.pools.get(key, any_age=True) or [])

    def _subito_category(self, s: Search) -> int:
        if s.subito_category is not None:
            return s.subito_category
        for cat in s.filters.get("catalog", []) or []:
            if int(cat) in CATEGORY_FOR_VINTED_CATALOG:
                return CATEGORY_FOR_VINTED_CATALOG[int(cat)]
        return self.cfg.subito.default_category

    def _subito_pool(self, s: Search) -> list[Item]:
        """Italy-wide Subito listings for market value; an older pool is fine meanwhile."""
        key = "subito " + _key(s)
        pool = self.pools.get(key, max_age_min=self.cfg.subito.pool_refresh_minutes)
        if pool is None and self.subito_pools < MAX_SUBITO_POOLS:
            self.subito_pools += 1
            log.info("Building Subito price pool for '%s'", s.query)
            pool = self.subito.pool(s.query, self._subito_category(s), s.price_from)
            self.pools.put(key, pool)
        return pool if pool is not None else (self.pools.get(key, any_age=True) or [])

    def _other_pools(self, s: Search) -> list[Item]:
        """eBay and Subito pools; trouble with either shouldn't stop the scan."""
        out: list[Item] = []
        for name, get in (("eBay", self._ebay_pool if self.ebay else None),
                          ("Subito", self._subito_pool if self.subito else None)):
            if get:
                try:
                    out += get(s)
                except Exception as e:
                    log.error("%s price pool '%s' failed: %s", name, s.query, e)
        return out

    def _subito_for(self, s: Search) -> list[Item]:
        """This run's new local Subito listings that belong to this search."""
        return [i for i in self.subito_new
                if is_relevant(i.title, s.query)
                and (s.price_from is None or i.price >= s.price_from)
                and (s.price_to is None or i.price <= s.price_to)]

    # --- scanning ----------------------------------------------------------------

    def _rules(self, s: Search) -> Rules:
        rules = replace(self.cfg.rules,
                        exclude_keywords=self.cfg.rules.exclude_keywords + tuple(s.exclude_keywords))
        if s.shipping_cost is not None:
            rules = replace(rules, shipping_cost=s.shipping_cost)
        if s.resell_costs is not None:
            rules = replace(rules, resell_costs=s.resell_costs)
        if s.max_roi is not None:
            rules = replace(rules, max_roi=s.max_roi)
        return replace(rules, match_brand=s.match_brand)

    def scan_search(self, s: Search) -> list[Deal]:
        newest = self.client.search(s.query, order="newest_first",
                                    price_from=s.price_from, price_to=s.price_to,
                                    extra=s.filters)
        ebay_newest: list[Item] = []
        if self.ebay and self.ebay_due:
            try:
                ebay_newest = self.ebay.search(s.query, price_from=s.price_from, price_to=s.price_to,
                                               limit=self.cfg.ebay.new_per_search,
                                               category=self._ebay_category(s))
            except Exception as e:   # eBay trouble shouldn't stop the Vinted scan
                log.error("eBay search '%s' failed: %s", s.query, e)
        local = self._subito_for(s) if self.subito else []
        fresh = [i for i in newest + ebay_newest + local if i.key not in self.seen]
        log.info("'%s': %d Vinted + %d eBay + %d Subito listings, %d new",
                 s.query, len(newest), len(ebay_newest), len(local), len(fresh))
        if not self.first_run and not self.known.has(s):
            self._seed(s, newest + ebay_newest + local, fresh)
            return []
        self.known.add(s)
        if not fresh:
            return []
        pool = self._vinted_pool(s)
        if pool is None:
            # no price pool yet and this run's rebuilds are used up: leave these
            # listings unseen so the next run checks them
            log.info("'%s': price pool waits for the next run", s.query)
            return []
        pool = _merge(pool, self._other_pools(s), newest, ebay_newest, local)
        rules = self._rules(s)

        deals = []
        for item in fresh:
            self.seen.add(item.key)
            if self.first_run or (item.source == "ebay" and self.first_ebay):
                # Nothing seen yet from this platform: remember what's listed now
                # instead of alerting on all of it
                continue
            if item.source == "subito" and self.first_subito:
                # First Subito pass: like a new search, only the best few already-listed
                # deals get sent
                deal = assess(item, pool, rules, s.query)
                if deal and not deal.blocked:
                    self.seed_candidates.append((s, item, pool, rules))
                continue
            self.checked += 1
            if item.source in ("ebay", "subito"):
                deal = assess(item, pool, rules, s.query)   # cost to get it comes with the listing
            else:
                # Vinted shipping is only on the item page, so only open it for listings
                # that would be a deal even with free shipping (a handful per scan)
                free = assess(item, pool, rules, s.query, shipping=0.0)
                if free is None:
                    continue
                deal = (self._with_details(item, pool, rules, s.query) if not free.blocked
                        else assess(item, pool, rules, s.query))   # estimated shipping
            if deal and not deal.blocked:
                deals.append(deal)
            elif is_near_miss(deal):
                self.near_misses.append((s.query, deal))
        return deals

    def _seed(self, s: Search, listed: list[Item], fresh: list[Item]):
        """
        A search added to config.yaml: its current listings are a backlog, not news. Keep
        the ones that already look like deals as candidates (only the best SEED_ALERTS of
        all new searches get sent, after real shipping is checked) and remember the rest.
        """
        pool = _merge(self._vinted_pool(s, force=True), self._other_pools(s), listed)
        rules = self._rules(s)
        found = 0
        for item in fresh:
            self.seen.add(item.key)
            deal = assess(item, pool, rules, s.query)   # estimated shipping for now
            if deal and not deal.blocked:
                self.seed_candidates.append((s, item, pool, rules))
                found += 1
        self.known.add(s)
        log.info("'%s' is new: %d current listings remembered, %d already look like deals "
                 "(the best %d of all new searches get sent)", s.query, len(fresh), found, SEED_ALERTS)

    def _best_seed_deals(self) -> list[Deal]:
        """Real shipping for the strongest candidates, then the top SEED_ALERTS that still pass."""
        ranked = []
        for s, item, pool, rules in self.seed_candidates:
            d = assess(item, pool, rules, s.query)
            ranked.append(((d.rating, d.profit), s, item, pool, rules))
        ranked.sort(key=lambda r: r[0], reverse=True)
        out = []
        for _, s, item, pool, rules in ranked[:SEED_ALERTS * 2]:
            deal = (self._with_details(item, pool, rules, s.query) if item.source == "vinted"
                    else assess(item, pool, rules, s.query))
            if deal and not deal.blocked:
                out.append(deal)
            if len(out) == SEED_ALERTS:
                break
        log.info("New searches/platforms: %d listings already looked like deals, sending the best %d",
                 len(self.seed_candidates), len(out))
        return out

    def _with_details(self, item: Item, pool: list[Item], rules, query: str) -> Deal | None:
        try:
            d = self.client.details(item)
        except Exception as e:
            log.warning("Couldn't open item %s, using estimated shipping: %s", item.id, e)
            return assess(item, pool, rules, query)
        pickup = not d.shipping_available or is_pickup_only(f"{item.title}\n{d.description}")
        log.info("Item %s: shipping %s%s", item.id,
                 "unknown" if d.shipping is None else f"{d.shipping:.2f}", ", pickup only" if pickup else "")
        return assess(item, pool, rules, query, shipping=d.shipping,
                      pickup_only=pickup, city=d.city)

    def scan(self) -> list[Deal]:
        deals: list[Deal] = []
        self.checked, self.failed_searches, self.near_misses = 0, 0, []
        self.pool_rebuilds = 0
        self.seed_candidates = []
        self.ebay_due = bool(self.ebay) and self.ebay_state.due(self.cfg.ebay.interval_minutes)
        self.first_ebay = self.ebay_due and not self.seen.has_platform("ebay")
        calls_before = self.ebay.calls if self.ebay else 0
        self.subito_pools = self.ebay_pools = 0
        self._fetch_subito()
        for s in self.cfg.searches:
            try:
                deals += self.scan_search(s)
            except Exception as e:  # one broken search shouldn't kill the rest
                self.failed_searches += 1
                log.error("Search '%s' failed: %s", s.query, e)
        if self.seed_candidates:
            deals += self._best_seed_deals()
        if self.first_run:
            log.info("First run: saved %d current listings as seen, alerts start next scan",
                     len(self.seen.data))
            self.first_run = self.seen.is_empty
        elif self.first_ebay:
            log.info("First eBay scan: saved current eBay listings as seen, eBay alerts start next scan")
        if self.subito:
            log.info("Subito: %d new local listing(s) in %g km, %d API calls this run%s",
                     len(self.subito_new), self.cfg.subito.radius_km, self.subito.calls - self._subito_calls0,
                     " (first pass: only the best already-listed deals are sent)" if self.first_subito else "")
        if self.ebay:
            self.ebay_state.record(self.ebay.calls - calls_before, scanned=self.ebay_due)
            self.ebay_state.save()
            log.info("eBay: %s, %d API calls today (UTC)",
                     "searched this run" if self.ebay_due else "skipped this run (searched every "
                     f"{self.cfg.ebay.interval_minutes:g} min)", self.ebay_state.calls_today)
        self.seen.save()
        self.pools.save()
        self.known.save()
        deals.sort(key=lambda d: (d.rating, d.profit), reverse=True)
        self.near_misses.sort(key=lambda m: (m[1].closeness, m[1].profit), reverse=True)
        log.info("Checked %d new listing(s), %d near miss(es)", self.checked, len(self.near_misses))
        for query, m in self.near_misses[:5]:
            log.info("Near miss: %s", describe_miss(query, m))
        return deals

    def _fetch_subito(self):
        """Newest local Subito listings, one call per category the searches use."""
        self.subito_new, self.first_subito = [], False
        if not self.subito:
            return
        self._subito_calls0 = self.subito.calls
        found: dict[str, Item] = {}
        for cat in sorted({self._subito_category(s) for s in self.cfg.searches}):
            try:
                for i in self.subito.newest_in_area(cat):
                    found.setdefault(i.key, i)
            except Exception as e:   # Subito trouble shouldn't stop the Vinted/eBay scan
                log.error("Subito category %s failed: %s", cat, e)
        self.subito_new = list(found.values())
        self.first_subito = bool(self.subito_new) and not self.seen.has_platform("subito")

    @property
    def failed(self) -> bool:
        """Every search errored, e.g. Vinted blocking us."""
        return self.failed_searches == len(self.cfg.searches)


PLATFORM_NAMES = {"vinted": "Vinted", "ebay": "eBay", "subito": "Subito"}


def describe_miss(query: str, m: Deal) -> str:
    c = m.item.currency
    return (f"[{PLATFORM_NAMES.get(m.item.source, m.item.source)}] '{m.item.title[:50]}' [{query}] "
            f"pay {m.item.total_price + m.shipping:.2f} {c} incl. shipping, "
            f"value {m.market_value:.2f}, profit {m.profit:.2f}, ROI {m.roi:.1f}%, rating {m.rating}, "
            f"blocked by {', '.join(m.blocked)} {m.item.url}")
