"""One scan = for each search, grab new listings and check them against market value."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from .analyzer import (NEW_SELLER, Deal, Rules, assess, blend_sold, demand, is_near_miss, is_pickup_only, is_relevant,
                       model_numbers, model_tokens)
from .config import Config, Search
from .ebay import CATEGORY_FOR_VINTED_CATALOG as EBAY_CATEGORY_FOR_VINTED_CATALOG
from .ebay import EbayClient
from .subito import CATEGORY_FOR_VINTED_CATALOG, SubitoClient
from .sold import SoldTracker
from .storage import SeenStore
from .vinted import Item, VintedClient

log = logging.getLogger(__name__)

# Rebuilding a price pool costs 3 page loads (~11 s). With many searches they'd all
# expire together and push a run past the workflow timeout, so only this many are
# rebuilt per run; the rest keep their older pool until their turn.
MAX_POOL_REBUILDS = 4
FAST_PER_PAGE = 24   # the fast lane only needs the listings that appeared since the last pass
# A newly added search alerts on at most this many of the deals already listed
# (the best ones); the rest of its current listings are just remembered.
SEED_ALERTS = 3
# Which 5-minute slot (even 0 / odd 1) each group runs in when `stagger` is on
STAGGER_SLOTS = {"Budget": 0, "Guitars": 1, "Amps": 1, "Pedals": 1, "Audio": 1}
STAGGER_OVERDUE = 12   # minutes: a group this late takes the next run instead of the other one

# Subito and eBay price pools are one call each; at most this many are rebuilt per run
MAX_SUBITO_POOLS = 4
MAX_EBAY_POOLS = 4


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
        # photos and descriptions only matter for a deal's AI check: keep the cache small
        self.data[key] = {"ts": time.time(), "items": [{k: v for k, v in asdict(i).items() if k not in ("photos", "description")}
                                                       for i in items]}

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
        # searches whose eBay listings have been seen once (None: not tracked yet, see Scanner)
        self.seeded: set[str] | None = set(self.data["seeded"]) if "seeded" in self.data else None

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
        if self.seeded is not None:
            self.data["seeded"] = sorted(self.seeded)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data), encoding="utf-8")


class KnownSearches:
    """
    Searches that have run before (so a newly added one starts silently), and when
    each last ran (for searches that only run every N minutes).
    """

    def __init__(self, path: Path, pools: "PoolCache"):
        self.path = path
        self.last: dict[str, float] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                # older files were a plain list of keys
                self.last = dict.fromkeys(data, 0.0) if isinstance(data, list) else dict(data)
            except (json.JSONDecodeError, OSError, ValueError):
                pass
        else:
            # Before this file existed, a search that ran has a price pool
            self.last = dict.fromkeys((k for k in pools.data if not k.startswith(("ebay ", "subito "))), 0.0)
        # Older keys included the price range, so changing a price (e.g. from Telegram)
        # made a search look new; now it's query + filters only
        self.last = {_migrate_id(k): v for k, v in self.last.items()}

    @property
    def keys(self) -> set[str]:
        return set(self.last)

    def has(self, s: "Search") -> bool:
        return _sid(s) in self.last

    def due(self, s: "Search") -> bool:
        every = getattr(s, "every_minutes", 0) or 0
        return time.time() - self.last.get(_sid(s), 0.0) >= every * 60 - 30   # slack for run jitter

    def add(self, s: "Search"):
        self.last[_sid(s)] = time.time()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.last), encoding="utf-8")


def tag(deal: Deal, s: Search) -> Deal:
    """Which search found it, and its category (for the group topic)."""
    from .settings import search_group
    deal.query, deal.group = s.query, search_group(s)
    return deal


def _sid(s: Search) -> str:
    """A search's identity: query + filters. Changing its price range keeps it the same search."""
    return json.dumps([s.query, s.filters], sort_keys=True)


def _migrate_id(key: str) -> str:
    try:
        v = json.loads(key)
    except (json.JSONDecodeError, TypeError):
        return key
    if isinstance(v, list) and len(v) == 4:      # old [query, price_from, price_to, filters]
        return json.dumps([v[0], v[3]], sort_keys=True)
    return key


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
        self.sold = SoldTracker(data_dir / "sold.json")
        self.notices: list[str] = []   # for the Summary topic (e.g. sold-price checks lowered)
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
        old = None if pool is not None else (self.pools.get(key, any_age=True) or [])
        old_ts = (self.pools.data.get(key) or {}).get("ts")
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
            self.sold.observe(key, old, pool, time.time(), old_ts)   # listings gone since: maybe sold
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
        if s.budget:
            br = self.cfg.budget_rules
            rules = replace(rules, min_profit=float(br["min_profit"]), min_roi=float(br["min_roi"]),
                            max_roi=float(br["max_roi"]), max_cost=float(self.cfg.budget), budget=True)
        if s.max_roi is not None:
            rules = replace(rules, max_roi=s.max_roi)
        now = time.time()
        tracked, scale = self.sold.coverage(_key(s), now)
        return replace(rules, match_brand=s.match_brand, check=s.check, missing_part_cost=s.missing_part_cost,
                       sold=self.sold.sold_for(_key(s), now), tracked_days=tracked, sold_scale=scale, now=now)

    def scan_search(self, s: Search) -> list[Deal]:
        newest = self.client.search(s.query, order="newest_first",
                                    price_from=s.price_from, price_to=s.price_to,
                                    extra=s.filters)
        self.sold.anchor(newest, time.time())   # newest ids = listed just now: dates for days-to-sell
        ebay_newest: list[Item] = []
        if self.ebay and self.ebay_due:
            try:
                ebay_newest = self.ebay.search(s.query, price_from=s.price_from, price_to=s.price_to,
                                               limit=self.cfg.ebay.new_per_search,
                                               category=self._ebay_category(s))
            except Exception as e:   # eBay trouble shouldn't stop the Vinted scan
                log.error("eBay search '%s' failed: %s", s.query, e)
            else:
                ebay_newest = self._seed_ebay(s, ebay_newest)
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
        return [tag(d, s) for d in self._evaluate(s, fresh, pool, self._rules(s))]

    def _evaluate(self, s: Search, fresh: list[Item], pool: list[Item], rules: Rules) -> list[Deal]:
        """New listings against the price pool: the deals (with real Vinted shipping), near misses noted."""
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
            elif deal and any(b.startswith(NEW_SELLER) for b in deal.blocked):
                self.seller_skipped += 1
                log.info("'%s': skipped a deal, brand-new seller at +%.0f%% ROI (item %s)", s.query, deal.roi, item.id)
        return deals

    def fast_scan(self, queries: list[str]) -> list[Deal]:
        """
        The fast lane: only the first page of the newest Vinted listings for a few searches,
        checked against the cached price pools (Vinted, and eBay/Subito as last saved). No pool
        rebuilds, no eBay or Subito calls, no sold checks, and nothing is saved: the full scan
        owns the cache. A deal found here and again by the full scan is sent once (the Worker
        refuses a listing it already has).
        """
        self.checked, self.failed_searches, self.near_misses, self.seller_skipped = 0, 0, [], 0
        self.first_ebay = self.first_subito = False
        if self.seen.is_empty:
            log.info("Fast lane: no saved scan data yet, waiting for a full scan")
            return []
        wanted = {q.lower() for q in queries}
        deals: list[Deal] = []
        for s in sorted(self.cfg.searches, key=lambda x: not x.budget):
            if not s.enabled or s.query.lower() not in wanted or not self.known.has(s):
                continue
            pool = self.pools.get(_key(s), any_age=True)
            if not pool:
                continue
            try:
                newest = self.client.search(s.query, order="newest_first", per_page=FAST_PER_PAGE,
                                            price_from=s.price_from, price_to=s.price_to, extra=s.filters)
            except Exception as e:
                self.failed_searches += 1
                log.error("Fast lane: search '%s' failed: %s", s.query, e)
                continue
            fresh = [i for i in newest if i.key not in self.seen]
            log.info("Fast lane '%s': %d newest, %d new", s.query, len(newest), len(fresh))
            if not fresh:
                continue
            keys = ([f"ebay {self._ebay_category(s)} " + _key(s)] if self.ebay else []) + ["subito " + _key(s)]
            cached = [p for p in (self.pools.get(k, any_age=True) for k in keys) if p]
            pool = _merge(pool, [i for p in cached for i in p], newest)
            deals += [tag(d, s) for d in self._evaluate(s, fresh, pool, self._rules(s))]
        deals.sort(key=lambda d: (d.budget, d.per_euro if d.budget else 0, d.rating, d.profit), reverse=True)
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
            if deal and not deal.blocked and not s.budget:   # budget searches: sent by the one-time scan
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
                out.append(tag(deal, s))
            if len(out) == SEED_ALERTS:
                break
        log.info("New searches/platforms: %d listings already looked like deals, sending the best %d",
                 len(self.seed_candidates), len(out))
        return out

    def _seed_ebay(self, s: Search, items: list[Item]) -> list[Item]:
        """
        A search's first eBay pass (e.g. added while eBay was off-cycle): its current eBay
        listings are a backlog, so they're remembered without alerting.
        """
        st = self.ebay_state
        if st.seeded is None:
            # first run with this tracking: searches that already ran had their eBay passes
            st.seeded = {_sid(x) for x in self.cfg.searches if self.known.has(x)}
        if _sid(s) in st.seeded or self.first_ebay:
            return items
        for i in items:
            self.seen.add(i.key)
        st.seeded.add(_sid(s))
        log.info("'%s': first eBay pass, %d current eBay listings remembered", s.query, len(items))
        return []

    def _with_details(self, item: Item, pool: list[Item], rules, query: str) -> Deal | None:
        try:
            d = self.client.details(item)
        except Exception as e:
            log.warning("Couldn't open item %s, using estimated shipping: %s", item.id, e)
            return assess(item, pool, rules, query)
        pickup = not d.shipping_available or is_pickup_only(f"{item.title}\n{d.description}")
        item.seller = self._vinted_seller(d)
        item.description = (d.description or "")[:1500]
        log.info("Item %s: shipping %s%s", item.id,
                 "unknown" if d.shipping is None else f"{d.shipping:.2f}", ", pickup only" if pickup else "")
        return assess(item, pool, rules, query, shipping=d.shipping,
                      pickup_only=pickup, city=d.city)

    def _vinted_seller(self, d) -> dict | None:
        """Trust numbers from the item page, plus items sold from the seller's profile (one more page)."""
        from .vinted import seen_english
        if d.reviews is None:
            return None
        seller = {"source": "vinted", "reviews": d.reviews, "stars": round(d.reputation * 5, 1) if d.reviews else None,
                  "business": d.business, "seen": seen_english(d.last_seen), "fast": "SPEEDY_SHIPPING" in d.badges}
        if d.seller_id:
            try:
                prof = self.client.seller_profile(d.seller_id)
                seller.update(sold=prof.get("sold"), negative=prof.get("negative"))
            except Exception as e:   # never print the profile: public logs
                log.warning("Couldn't open a seller profile: %s", type(e).__name__)
        return seller

    def scan(self) -> list[Deal]:
        deals: list[Deal] = []
        self.checked, self.failed_searches, self.near_misses = 0, 0, []
        self.seller_skipped = 0
        self.pool_rebuilds = 0
        self.seed_candidates = []
        self._group_slot = self._pick_slot()   # which of budget / guitars runs this time
        self.ebay_due = bool(self.ebay) and self.ebay_state.due(self.cfg.ebay.interval_minutes)
        if self.ebay_due and self.cfg.stagger and self._group_slot == STAGGER_SLOTS["Budget"]:
            # the eBay pass waits for the next guitar run: budget runs are the longer ones
            self.ebay_due = False
        if self.ebay_due and self.ebay_state.calls_today >= self.cfg.ebay.max_calls_per_day:
            log.warning("eBay: %d calls used today (UTC), skipping eBay until tomorrow",
                        self.ebay_state.calls_today)
            self.ebay_due = False
        self.first_ebay = self.ebay_due and not self.seen.has_platform("ebay")
        calls_before = self.ebay.calls if self.ebay else 0
        self.subito_pools = self.ebay_pools = 0
        self._fetch_subito()
        # budget searches first: an overlapping normal search ("boss") would otherwise mark
        # their listings as seen under the normal rules before they're checked
        for s in sorted(self.cfg.searches, key=lambda x: not x.budget):
            if not s.enabled:
                continue
            if self.known.has(s) and not self._turn(s):
                continue
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
        self.check_sold()
        self.seen.save()
        self.pools.save()
        self.known.save()
        # budget deals by profit per euro spent, then the rest by rating
        deals.sort(key=lambda d: (d.budget, d.per_euro if d.budget else 0, d.rating, d.profit), reverse=True)
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
        for cat in sorted({self._subito_category(s) for s in self.cfg.searches if s.enabled}):
            try:
                for i in self.subito.newest_in_area(cat):
                    found.setdefault(i.key, i)
            except Exception as e:   # Subito trouble shouldn't stop the Vinted/eBay scan
                log.error("Subito category %s failed: %s", cat, e)
        self.subito_new = list(found.values())
        self.first_subito = bool(self.subito_new) and not self.seen.has_platform("subito")

    DEMAND_EVERY = 30 * 60   # the /demand table goes to the Worker this often

    def demand_due(self, now: float | None = None) -> bool:
        return (now or time.time()) - self.sold.data.get("demand_sent", 0) >= self.DEMAND_EVERY

    def demand_table(self, now: float | None = None) -> dict:
        """
        For /demand: per search, demand overall and for its biggest model groups (same storage,
        model number, Pro/Max...), worded like the alerts. Built from the price pools and sold history.
        """
        from .telegram import demand_text
        now = now or time.time()
        out = {}
        for s in self.cfg.searches:
            pool = [i for i in (self.pools.get(_key(s), any_age=True) or [])
                    if i.source == "vinted" and is_relevant(i.title, s.query)]
            if len(pool) < 3:
                continue
            rules = self._rules(s)
            groups: dict = {}
            for it in pool:
                groups.setdefault(model_numbers(model_tokens(it.title, s.query, it.brand)), []).append(it)
            rows = []
            for tokens, items in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:6]:
                if len(items) < 3:
                    continue
                first = items[0]
                _, _, days = blend_sold(first, first.price, rules, s.query)
                d = demand(first, items, rules, s.query, days)
                name = " ".join([s.query, *sorted(tokens)]).strip()
                rows.append({"model": name, "text": demand_text(d), "listed": len(items), "label": d["label"]})
            recent = [x for x in rules.sold if x[2] >= now - min(28, rules.tracked_days) * 86400]
            weeks = min(28, rules.tracked_days) / 7
            overall = {"label": "", "learning": rules.sold_scale is None or weeks < 1, "listed": len(pool),
                       "favourites": round(sum(i.favourites for i in pool) / len(pool)), "days": None,
                       "sold_week": round(len(recent) * (rules.sold_scale or 0) / weeks, 1) if weeks >= 1 else None,
                       "ratio": None}
            if not overall["learning"]:
                ratio = overall["sold_week"] / len(pool)
                overall.update(ratio=round(ratio, 2), label="high" if ratio >= 0.5 else "slow" if ratio < 0.1 else "normal")
            out[s.query] = {"tracked_days": round(rules.tracked_days, 1), "all": demand_text(overall), "models": rows}
        self.sold.data["demand_sent"] = now
        return out

    def check_sold(self):
        """Opens a few listings that left their price pool, to learn which sold (sold.py)."""
        now = time.time()
        blocked = self.client.blocked
        result = self.sold.verify(self.client, now) if not blocked else {}
        self.sold.adapt(self.client.blocked, now)
        self.notices += self.sold.notices
        st = self.sold.stats()
        log.info("Sold prices: %s | %d sold known, %d candidates, %d tracked, %d checks per run%s",
                 ", ".join(f"{v} {k}" for k, v in result.items() if v) or "no checks",
                 st["sold"], st["pending"], st["tracked"], st["per_run"],
                 f" (Vinted refused {self.client.blocked} request(s))" if self.client.blocked else "")
        self.sold.save(now)

    def _slot_of(self, s: Search):
        from .settings import search_group
        return STAGGER_SLOTS.get("Budget" if s.budget else search_group(s)) if self.cfg.stagger else None

    def _pick_slot(self) -> int:
        """
        Budget searches (slot 0) and guitar/amp/pedal/audio searches (slot 1) take turns by
        5-minute slot. After a gap (skipped runs) both can be overdue: then only the one that
        waited longer runs, and the other gets the next run, so a run never has both.
        """
        now = time.time()
        slot = int(now // 300) % 2
        last = {0: 0.0, 1: 0.0}
        for x in self.cfg.searches:
            k = self._slot_of(x)
            if k is not None and x.enabled and self.known.has(x):
                last[k] = max(last[k], self.known.last.get(_sid(x), 0.0))
        late = {k: now - (last[k] or now) for k in last}
        other = 1 - slot
        if late[other] >= STAGGER_OVERDUE * 60 and late[other] > late[slot]:
            return other
        return slot

    def _turn(self, s: Search) -> bool:
        """Whether a known search runs this time: electronics every run, the two staggered groups in turn."""
        want = self._slot_of(s)
        if want is None:
            return self.known.due(s)
        pick = getattr(self, "_group_slot", None)
        return want == (pick if pick is not None else self._pick_slot())

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
