"""One scan = for each search, grab new listings and check them against market value."""

from __future__ import annotations

import json
import os
import logging
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from .analyzer import (FAKE_SELLER, NEW_SELLER, Deal, Rules, assess, blend_sold, demand, is_near_miss, is_pickup_only, is_relevant,
                       model_numbers, model_tokens)
from .config import Config, Search
from .ebay import CATEGORY_FOR_VINTED_CATALOG as EBAY_CATEGORY_FOR_VINTED_CATALOG
from .ebay import EbayClient
from .subito import CATEGORY_FOR_VINTED_CATALOG, SubitoBlocked, SubitoClient
from .mercatino import MercatinoClient
from .reverb import ReverbClient, assess_reverb
from .sold import SoldTracker
from .storage import SeenStore
from .settings import search_group
from .vinted import Item, VintedClient

log = logging.getLogger(__name__)

# Rebuilding a price pool costs 3 page loads (~11 s). With many searches they'd all
# expire together and push a run past the workflow timeout, so only this many are
# rebuilt per run; the rest keep their older pool until their turn.
MAX_POOL_REBUILDS = 4
# A run stops starting new searches this long after it began (the workflow passes its start time), finishes
# cleanly and saves everything; the searches it didn't reach go first next run. Keeps a run near 5 minutes
# with the job's own setup, so one slow machine can't hit the workflow's time limit.
RUN_BUDGET = 270
REVERB_EVERY_MIN = 15      # Reverb: every 3rd run (it rarely finds anything)
# the last part of the budget is kept for Mercatino and Reverb (they come last), so searches stop this much
# earlier when either runs this run; they may still start up to TAIL_GRACE after the budget
TAIL_RESERVE = 30
TAIL_GRACE = 15
MUSIC = {"Guitars", "Amps", "Pedals", "Studio"}
BIG_MUSIC = {"Guitars", "Amps"}   # shipping costs a lot: skipped on Vinted when it's over MAX_SHIP_SHARE of the price
MAX_SHIP_SHARE = 0.2
# "slow lane" searches (slow_minutes): at most this many per run, the longest-waiting first, so a long list
# of them spreads over the runs instead of all landing in one
MAX_SLOW_PER_RUN = 8
# searches that never ran (just added): their first run builds a price pool (3 page loads) on top of the search,
# so at most this many a run; the rest wait for the next runs instead of pushing one run past the time limit
MAX_NEW_PER_RUN = 6
MUSIC_MIN_COMPARABLES = 5   # same-model listings needed to value music gear
FAST_PER_PAGE = 24   # the fast lane only needs the listings that appeared since the last pass
# A newly added search alerts on at most this many of the deals already listed
# (the best ones); the rest of its current listings are just remembered.
SEED_ALERTS = 3
# Which 5-minute slot (even 0 / odd 1) each group runs in when `stagger` is on
STAGGER_SLOTS = {"Budget": 0, "Guitars": 1, "Amps": 1, "Pedals": 1, "Audio": 1, "Studio": 1}
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

    def due_slow(self, s: "Search") -> bool:
        return time.time() - self.last.get(_sid(s), 0.0) >= (s.slow_minutes or 0) * 60 - 30

    def add(self, s: "Search"):
        self.last[_sid(s)] = time.time()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.last), encoding="utf-8")


def tag(deal: Deal, s: Search) -> Deal:
    """Which search found it, and its category (for the group topic)."""
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
                                   town_travel_costs=sb.town_travel_costs, wide_regions=list(sb.wide_regions),
                                   wide_radius_km=sb.wide_radius_km, tiers=list(sb.tiers),
                                   request_delay=cfg.request_delay) if sb.enabled else None
        mm = getattr(cfg, "mercatino", None)
        try:
            self.mercatino = (MercatinoClient(mm.contact, cfg.seen_file.parent / "mercatino.json", daily_cap=mm.daily_cap)
                              if mm and mm.enabled and mm.contact else None)
        except ValueError:
            self.mercatino = None
        self.mercatino_pages = 0
        rv = getattr(cfg, "reverb", None)
        try:
            self.reverb = ReverbClient(rv.token, rv.region) if rv and rv.enabled and rv.token else None
        except ValueError:
            self.reverb = None
        self.reverb_state = {"turn": 0, "seeded": [], "last_pass": 0.0}
        if self.reverb:
            try:
                self.reverb_state.update(json.loads((cfg.seen_file.parent / "reverb.json").read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        self.subito_new: list[Item] = []
        self.first_subito = False
        self.subito_pools = 0
        self.ebay_pools = 0
        data_dir = cfg.seen_file.parent
        self.seen = SeenStore(cfg.seen_file)
        self.first_run = self.seen.is_empty
        self.pools = PoolCache(data_dir / "pools.json", cfg.pool_refresh_minutes)
        self.known = KnownSearches(data_dir / "searches.json", self.pools)
        self.run_start = float(os.environ.get("FLIPFINDER_RUN_START") or time.time())
        self.deadline = self.run_start + RUN_BUDGET
        self.carry_path = data_dir / "carry.json"
        try:
            self.carry = set(json.loads(self.carry_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            self.carry = set()
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
        for name, get in (("eBay", self._ebay_pool if self.ebay and s.uses("ebay") else None),
                          ("Subito", self._subito_pool if self.subito and s.uses("subito") else None)):
            if get:
                try:
                    out += get(s)
                except Exception as e:
                    log.error("%s price pool '%s' failed: %s", name, s.query, e)
        return out

    def _subito_for(self, s: Search) -> list[Item]:
        """This run's new Subito listings (pickup or shipping) that belong to this search."""
        def cap(i):
            return self._pickup_cap(s) if self._is_music_pickup(s, i) else self._price_to(s)
        out = [i for i in self.subito_new
               if is_relevant(i.title, s.query, i.brand) and self._subito_category(s) == i.category
               and (s.price_from is None or i.price >= s.price_from)
               and (cap(i) is None or i.price <= cap(i))]
        if s.clothes:   # Subito has no brand field: the search's brand, so the clothes checks can compare it
            out = [replace(i, brand=i.brand or s.query.title()) for i in out]
        return out

    # --- scanning ----------------------------------------------------------------

    def _rules(self, s: Search) -> Rules:
        allow = {k.lower() for k in s.allow_keywords}
        rules = replace(self.cfg.rules,
                        exclude_keywords=tuple(k for k in self.cfg.rules.exclude_keywords if k.lower() not in allow)
                        + tuple(s.exclude_keywords))
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
        va = getattr(self.cfg, "value_adjust", None) or {}
        factor = va.get(s.query.lower(), va.get(search_group(s).lower(), 1.0))
        if factor != 1.0:
            rules = replace(rules, value_factor=factor)
        if search_group(s) in MUSIC:
            # one search ("boss", "marshall amplificatore") covers dozens of models: fewer of each
            rules = replace(rules, min_comparables=min(rules.min_comparables, MUSIC_MIN_COMPARABLES))
            if not s.budget:   # music gear's own minimums (config music_rules); budget pedals keep budget_rules
                mr = self.cfg.music_rules
                rules = replace(rules, min_profit=float(mr["min_profit"]), min_roi=float(mr["min_roi"]))
                if mr.get("max_price"):   # the app's Music card
                    rules = replace(rules, max_price=float(mr["max_price"]))
            if search_group(s) in BIG_MUSIC:
                rules = replace(rules, max_ship_share=MAX_SHIP_SHARE)
        if s.max_price is not None and not s.budget:   # one search's own max (Jackson: €300 for the X Series)
            rules = replace(rules, max_price=float(s.max_price))
        if s.model and not s.clothes:
            rules = replace(rules, model=s.model)
        if s.clothes:   # designer clothes: their own minimums (/setrule clothes ...) and the fake checks
            c = self.cfg.clothes
            rules = replace(rules, min_profit=float(c.rules["min_profit"]), min_roi=float(c.rules["min_roi"]),
                            min_rating=int(c.rules["min_rating"]), min_comparables=c.min_comparables, clothes=True,
                            fake_floor=c.fake_floor, seller_min_reviews=c.seller_min_reviews,
                            seller_min_stars=c.seller_min_stars, repairs=False, model=s.model,
                            # "replica" is the model's name in the Margiela Replica search, a fake word everywhere else
                            fake_words=tuple(w for w in c.fake_words if not (s.model == "replica" and w.lower() == "replica")),
                            abroad=rules.abroad or c.abroad, max_price=float(c.rules.get("max_price") or 200))
        now = time.time()
        tracked, scale = self.sold.coverage(_key(s), now)
        return replace(rules, match_brand=s.match_brand, check=s.check, missing_part_cost=s.missing_part_cost,
                       sold=self.sold.sold_for(_key(s), now), tracked_days=tracked, sold_scale=scale, now=now)

    def _is_music_pickup(self, s: Search, item: Item) -> bool:
        return item.source == "subito" and item.delivery == "pickup" and search_group(s) in MUSIC and not s.budget

    def _pickup_cap(self, s: Search) -> float | None:
        """Music picked up in person may cost more (music_rules.pickup_max_price), never under the search's own max."""
        music = getattr(self.cfg, "music_rules", None) or {}
        cap = self._price_to(s)
        if music.get("pickup_max_price"):
            return max(float(music["pickup_max_price"]), cap or 0)
        return cap

    def _pickup_rules(self, s: Search, item: Item, rules: Rules) -> Rules:
        """A music pickup: the higher max price, but a higher min profit too (most of the pot goes into one)."""
        if not self._is_music_pickup(s, item):
            return rules
        music = getattr(self.cfg, "music_rules", None) or {}
        return replace(rules, max_price=self._pickup_cap(s),
                       min_profit=max(rules.min_profit, float(music.get("pickup_min_profit") or rules.min_profit)))

    def _max_price(self, s: Search) -> float | None:
        """The most a deal from this search may cost: its topic's max price (music, clothes), else the main one."""
        if s.max_price is not None and not s.budget:
            return float(s.max_price)
        if s.clothes:
            return float(self.cfg.clothes.rules.get("max_price") or 200)
        music = getattr(self.cfg, "music_rules", None) or {}
        if search_group(s) in MUSIC and not s.budget and music.get("max_price"):
            return float(music["max_price"])
        return self.cfg.rules.max_price

    # --- Reverb (official API; its terms: see reverb.py) -----------------------------------
    def _scan_reverb(self) -> list[Deal]:
        """
        A few music searches a run, in turn: the newest used listings that ship to Italy (EU sellers unless /reverb all),
        each against the median asking price of the same model and condition on Reverb. Reverb prices value Reverb
        deals only. A search's first pass is remembered without alerting.
        """
        rv, st = self.reverb, self.reverb_state
        music = [s for s in self.cfg.searches if s.enabled and search_group(s) in MUSIC and not s.budget and s.uses("reverb")]
        deals: list[Deal] = []
        rebuilt = 0
        for _ in range(min(self.cfg.reverb.searches_per_run, len(music))):
            s = music[st["turn"] % len(music)]
            st["turn"] += 1
            items = [i for i in rv.newest(s.query, s.price_from, self._price_to(s)) if s.wants(i)]
            key = "reverb " + _key(s)
            pool = self.pools.get(key)
            if pool is None and rebuilt < 3:
                pool = rv.pool(s.query, self.cfg.reverb.pool_pages)
                self.pools.put(key, pool)
                rebuilt += 1
            pool = (pool if pool is not None else self.pools.get(key, any_age=True) or []) + items
            if s.query not in st["seeded"] and not self.first_run:
                for i in items:
                    self.seen.add(i.key)
                st["seeded"] = sorted(set(st["seeded"]) | {s.query})
                continue
            rules = self._rules(s)
            for item in items:
                if item.key in self.seen:
                    continue
                self.seen.add(item.key)
                self.checked += 1
                deal = assess_reverb(item, pool, rules, s.model)
                if deal and not deal.blocked:
                    deals.append(tag(deal, s))
                elif is_near_miss(deal):
                    self.near_misses.append((s.query, deal))
        log.info("Reverb: %d request(s) this run (%s sellers)", rv.requests, "EU" if rv.region == "eu" else "all")
        return deals

    # --- Mercatino Musicale (permission on its conditions: see mercatino.py) ---------------
    def _scan_mercatino(self) -> list[Deal]:
        """
        A few music searches a run, in turn (pages_per_run search pages, newest first), used listings only. A search's
        first page is remembered without alerting; its listings build Mercatino's own comparables over the days.
        """
        mm = self.mercatino
        music = [s for s in self.cfg.searches if s.enabled and search_group(s) in MUSIC and not s.budget and s.uses("mercatino")]
        deals: list[Deal] = []
        for _ in range(self.cfg.mercatino.pages_per_run):
            if not music or not mm.can_fetch():
                break
            s = music[mm.state["turn"] % len(music)]
            mm.state["turn"] += 1
            cap = self._price_to(s)
            items = [i for i in mm.search(s.query) if i.condition.lower().startswith("usato") and s.wants(i)
                     and (s.price_from is None or i.price >= s.price_from)]
            self.mercatino_pages += 1
            mm.remember(s.query, items)
            if not mm.seeded(s.query) and not self.first_run:   # its current listings are a backlog, not news
                for i in items:
                    self.seen.add(i.key)
                mm.mark_seeded(s.query)
                continue
            fresh = [i for i in items if i.key not in self.seen and (cap is None or i.price <= cap)]
            pool = (self.pools.get(_key(s), any_age=True) or []) + mm.pool(s.query)
            rules = self._rules(s)
            for item in fresh:
                self.seen.add(item.key)
                if is_pickup_only(f"{item.title}\n{item.description}"):
                    continue   # hand delivery somewhere in Italy: not for us
                self.checked += 1
                deal = assess(item, pool, rules, s.query)   # shipping: the search's estimate (the page doesn't say)
                if deal and not deal.blocked:
                    deals.append(tag(deal, s))
                elif is_near_miss(deal):
                    self.near_misses.append((s.query, deal))
        log.info("Mercatino Musicale: %d page(s) this run, %d today (limit %d)", self.mercatino_pages, mm.pages_today, mm.daily_cap)
        return deals

    # --- the app's ➕ Add search preview ---------------------------------------------
    def preview(self, s: Search) -> dict:
        """
        For a search not saved yet: how many listings Vinted has now (newest page) and how many would pass
        the topic's rules, before shipping and seller checks (no item pages opened). Builds its price pool.
        """
        newest = [i for i in self.client.search(s.query, order="newest_first", price_from=s.price_from,
                                                price_to=self._price_to(s), extra=s.filters) if s.wants(i)]
        pool = self._vinted_pool(s, force=True) + newest
        if s.must_words:
            pool = [p for p in pool if s.has_words(p)]
        rules = self._rules(s)
        valued, passing = 0, []
        for item in newest:
            d = assess(item, pool, rules, s.query, shipping=0.0)
            valued += d is not None
            if d and not d.blocked:
                passing.append(d)
        passing.sort(key=lambda d: d.profit, reverse=True)
        return {"listings": len(newest), "valued": valued, "pass": len(passing), "pool": len(pool),
                "best": [{"title": d.item.title[:60], "price": d.item.total_price, "profit": d.profit} for d in passing[:3]]}

    def _price_to(self, s: Search) -> float | None:
        """
        The search's max price, never above the main deals' max price: no point loading what we
        wouldn't buy. Only at search time: the saved pools and sold history keep their keys.
        """
        cap = self._max_price(s)
        if s.budget or cap is None:
            return s.price_to
        return cap if s.price_to is None else min(s.price_to, cap)

    def scan_search(self, s: Search) -> list[Deal]:
        newest = self.client.search(s.query, order="newest_first",
                                    price_from=s.price_from, price_to=self._price_to(s),
                                    extra=s.filters)
        self.sold.anchor(newest, time.time())   # newest ids = listed just now: dates for days-to-sell
        ebay_newest: list[Item] = []
        if self.ebay and self.ebay_due and s.uses("ebay"):
            try:
                ebay_newest = self.ebay.search(s.query, price_from=s.price_from, price_to=self._price_to(s),
                                               limit=self.cfg.ebay.new_per_search,
                                               category=self._ebay_category(s))
            except Exception as e:   # eBay trouble shouldn't stop the Vinted scan
                log.error("eBay search '%s' failed: %s", s.query, e)
            else:
                ebay_newest = self._seed_ebay(s, ebay_newest)
        local = self._subito_for(s) if self.subito and s.uses("subito") else []
        fresh = [i for i in newest + ebay_newest + local if i.key not in self.seen]
        for i in fresh:
            if not s.wants(i):   # the app's must-have words or size: seen, never checked
                self.seen.add(i.key)
        fresh = [i for i in fresh if s.wants(i)]
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
        if s.must_words:   # compared only with listings that have the same must-have words
            pool = [p for p in pool if s.has_words(p)]
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
                deal = assess(item, pool, self._pickup_rules(s, item, rules), s.query)   # cost to get it comes with the listing
                if item.source == "subito":   # the per-run Subito line in the log (pickup vs shipping)
                    counts = self.__dict__.setdefault("subito_counts", {})
                    how = item.delivery or "pickup"
                    counts[f"{how} checked"] = counts.get(f"{how} checked", 0) + 1
                    if deal and not deal.blocked:
                        counts[f"{how} deals"] = counts.get(f"{how} deals", 0) + 1
            else:
                # Vinted shipping is only on the item page, so only open it for listings
                # that would be a deal even with free shipping (a handful per scan)
                free = assess(item, pool, rules, s.query, shipping=0.0)
                if free is None:
                    continue
                deal = (self._with_details(item, pool, rules, s.query) if not free.blocked
                        else assess(item, pool, rules, s.query))   # estimated shipping
            if deal and not deal.blocked and s.clothes and item.source == "vinted" and not item.seller:
                deal.blocked.append(f"{FAKE_SELLER} {rules.seller_min_reviews} reviews (couldn't check the seller)")
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
                                            price_from=s.price_from, price_to=self._price_to(s), extra=s.filters)
            except Exception as e:
                self.failed_searches += 1
                log.error("Fast lane: search '%s' failed: %s", s.query, e)
                continue
            fresh = [i for i in newest if i.key not in self.seen and s.wants(i)]
            log.info("Fast lane '%s': %d newest, %d new", s.query, len(newest), len(fresh))
            if not fresh:
                continue
            keys = (([f"ebay {self._ebay_category(s)} " + _key(s)] if self.ebay and s.uses("ebay") else [])
                    + (["subito " + _key(s)] if s.uses("subito") else []))
            cached = [p for p in (self.pools.get(k, any_age=True) for k in keys) if p]
            pool = _merge(pool, [i for p in cached for i in p], newest)
            deals += [tag(d, s) for d in self._evaluate(s, fresh, pool, self._rules(s))]
        deals.sort(key=lambda d: (d.budget, d.per_euro if d.budget else 0, d.rating, d.profit), reverse=True)
        return deals

    def fast_subito(self, searches: list[Search], until: float, start: int = 0, radius: float = 100) -> tuple[list[Deal], int, int]:
        """
        The fast lane's Subito part: for the categories of the fast-lane searches, the newest listings nearby (pickup
        within `radius` km) and Italy-wide with shipping, as many of those calls as fit before `until`, starting at
        `start` so the rest come first next pass. New ones are checked against the saved price pools. Returns the deals,
        the calls made and how many calls a full round has. A 403/429 stops Subito for this pass at once.
        """
        if not self.subito:
            return [], 0, 0
        self.subito.fast = True
        subs = [x for x in searches if x.enabled and x.uses("subito") and self.known.has(x)]
        cats = sorted({self._subito_category(x) for x in subs})
        tasks = [(c, kind) for c in cats for kind in ("near", "ship")]
        found: dict[str, Item] = {}
        done = 0
        for k in range(len(tasks)):
            if time.time() >= until:
                break
            cat, kind = tasks[(start + k) % len(tasks)]
            try:
                items = self.subito.newest_nearby(cat, radius) if kind == "near" else self.subito.newest_shipping(cat)
            except SubitoBlocked as e:
                log.warning("Fast lane: %s, Subito stops for this pass", e)
                done += 1
                break
            except Exception as e:
                log.error("Fast lane: Subito category %s failed: %s", cat, e)
                done += 1
                continue
            done += 1
            for i in items:
                i.category = cat
                found.setdefault(i.key, i)
        self.subito_new = [i for i in found.values() if i.key not in self.seen]
        deals: list[Deal] = []
        for x in subs:
            fresh = [i for i in self._subito_for(x) if i.key not in self.seen and x.wants(i)]
            if not fresh:
                continue
            keys = [_key(x), "subito " + _key(x)] + ([f"ebay {self._ebay_category(x)} " + _key(x)] if self.ebay and x.uses("ebay") else [])
            cached = [i for p in (self.pools.get(k, any_age=True) for k in keys) if p for i in p]
            if not cached:
                continue
            log.info("Fast lane Subito '%s': %d new", x.query, len(fresh))
            deals += [tag(d, x) for d in self._evaluate(x, fresh, _merge(cached, fresh), self._rules(x))]
        return deals, done, len(tasks)

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
        item.country = (item.seller or {}).pop("country", "") or ""
        item.description = (d.description or "")[:1500]
        if d.photos:
            item.photos = d.photos[:4]   # the search page only has the first photo
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
            seller["id"] = d.seller_id   # only for spotting reposts (kept in the Worker's private storage, never logged)
            try:
                prof = self.client.seller_profile(d.seller_id)
                seller.update(sold=prof.get("sold"), negative=prof.get("negative"), country=prof.get("country", ""))
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
        slow = sorted((x for x in self.cfg.searches if x.slow_minutes and x.enabled and self.known.has(x)
                       and self.known.due_slow(x) and self._slot_of(x) in (None, self._group_slot)),
                      key=lambda x: self.known.last.get(_sid(x), 0.0))
        self._slow_picks = {x.query for x in slow[:MAX_SLOW_PER_RUN]}
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
        # What sells fastest first: electronics and clothes, then budget, then music (Mercatino and Reverb come
        # after the loop). Budget stays before music: an overlapping music search ("boss") would otherwise mark
        # their listings as seen under the music rules first. Searches the last run didn't reach go first of all.
        new_this_run = 0
        left: list[str] = []
        ran = 0
        self.carry = getattr(self, "carry", set())
        self.run_start = getattr(self, "run_start", time.time())
        self.deadline = getattr(self, "deadline", self.run_start + RUN_BUDGET)
        reverb_due = time.time() - getattr(self, "reverb_state", {}).get("last_pass", 0.0) >= REVERB_EVERY_MIN * 60 - 30
        tail = bool(getattr(self, "mercatino", None)) or bool(getattr(self, "reverb", None) and reverb_due)
        reserve = TAIL_RESERVE if tail else 0
        for s in sorted(self.cfg.searches, key=lambda x: (_sid(x) not in self.carry, self._tier(x))):
            if not s.enabled:
                continue
            carried = _sid(s) in self.carry
            if self.known.has(s) and not carried and not self._turn(s):
                continue
            cap = self._price_to(s)
            if s.price_from is not None and cap is not None and s.price_from >= cap:
                # e.g. a €500+ Gibson under the music max price: nothing to find, and Vinted refuses the range
                log.info("'%s' skipped: its prices start at €%g, over its max price €%g", s.query, s.price_from, cap)
                continue
            if time.time() >= self.deadline - reserve:
                if self.known.has(s):
                    left.append(_sid(s))   # due but not reached: first next run
                continue
            if not self.known.has(s) and not self.first_run:
                if new_this_run >= MAX_NEW_PER_RUN:
                    continue
                new_this_run += 1
            ran += 1
            try:
                deals += self.scan_search(s)
            except Exception as e:  # one broken search shouldn't kill the rest
                self.failed_searches += 1
                log.error("Search '%s' failed: %s", s.query, e)
        self.carry_next = left
        if getattr(self, "mercatino", None) and time.time() < self.deadline + TAIL_GRACE:
            try:
                deals += self._scan_mercatino()
            except Exception as e:   # never stops the scan
                log.error("Mercatino Musicale failed: %s", e)
            self.mercatino.save()
        if getattr(self, "reverb", None) and reverb_due and time.time() < self.deadline + TAIL_GRACE:
            self.reverb_state["last_pass"] = time.time()
            try:
                deals += self._scan_reverb()
            except Exception as e:   # never stops the scan
                log.error("Reverb failed: %s", e)
            (self.cfg.seen_file.parent / "reverb.json").write_text(json.dumps(self.reverb_state), encoding="utf-8")
        log.info("RUN-PLAN searched=%d carried_in=%d left_for_next=%d elapsed=%.0fs budget=%ds%s", ran, len(self.carry),
                 len(left), time.time() - self.run_start, RUN_BUDGET,
                 " (time budget reached: the rest go first next run)" if left else "")
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
            seen_by = getattr(self, "subito_fresh", {"pickup": 0, "shipping": 0})
            c = getattr(self, "subito_counts", {})
            log.info("SUBITO-STATS pickup seen=%d checked=%d deals=%d | shipping seen=%d checked=%d deals=%d",
                     seen_by["pickup"], c.get("pickup checked", 0), c.get("pickup deals", 0),
                     seen_by["shipping"], c.get("shipping checked", 0), c.get("shipping deals", 0))
            self.subito_counts = {}
        if self.ebay:
            self.ebay_state.record(self.ebay.calls - calls_before, scanned=self.ebay_due)
            self.ebay_state.save()
            log.info("eBay: %s, %d API calls today (UTC)",
                     "searched this run" if self.ebay_due else "skipped this run (searched every "
                     f"{self.cfg.ebay.interval_minutes:g} min)", self.ebay_state.calls_today)
        if time.time() < self.deadline + 30:
            self.check_sold()
        else:
            log.info("Sold-price checks skipped this run (time budget)")
        self.seen.save()
        self.pools.save()
        self.known.save()
        if getattr(self, "carry_path", None):
            self.carry_path.write_text(json.dumps(self.carry_next), encoding="utf-8")
        # budget deals by profit per euro spent, then the rest by rating
        deals.sort(key=lambda d: (d.budget, d.per_euro if d.budget else 0, d.rating, d.profit), reverse=True)
        self.near_misses.sort(key=lambda m: (m[1].closeness, m[1].profit), reverse=True)
        log.info("Checked %d new listing(s), %d near miss(es)", self.checked, len(self.near_misses))
        for query, m in self.near_misses[:5]:
            log.info("Near miss: %s", describe_miss(query, m))
        return deals

    def _fetch_subito(self):
        """
        Newest Subito listings, two calls per category the searches use: local ones (pickup, by distance) and
        Italy-wide ones that ship. A category's first Italy-wide pass is remembered without alerting.
        """
        self.subito_new, self.first_subito = [], False
        if not self.subito:
            return
        self._subito_calls0 = self.subito.calls
        found: dict[str, Item] = {}
        state_path = self.cfg.seen_file.parent / "subito_shipping.json"
        try:
            seeded = set(json.loads(state_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            seeded = set()
        for cat in sorted({self._subito_category(s) for s in self.cfg.searches if s.enabled and s.uses("subito")}):
            try:
                for i in self.subito.newest_in_area(cat):
                    i.category = cat
                    found.setdefault(i.key, i)   # the local one: it already chose pickup or shipping
                shipping = self.subito.newest_shipping(cat)
                for i in shipping:
                    i.category = cat
                if cat not in seeded and not self.first_run:
                    for i in shipping:
                        if i.key not in found:
                            self.seen.add(i.key)
                    seeded.add(cat)
                    continue
                seeded.add(cat)
                for i in shipping:
                    found.setdefault(i.key, i)
            except Exception as e:   # Subito trouble shouldn't stop the Vinted/eBay scan
                log.error("Subito category %s failed: %s", cat, e)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(sorted(seeded)), encoding="utf-8")
        self.subito_new = list(found.values())
        self.subito_fresh = {"pickup": 0, "shipping": 0}   # new to us this run, for the SUBITO-STATS line
        for i in self.subito_new:
            if i.key not in self.seen:
                self.subito_fresh[i.delivery or "pickup"] = self.subito_fresh.get(i.delivery or "pickup", 0) + 1
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
                    if i.source == "vinted" and is_relevant(i.title, s.query, i.brand)]
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
        if not self.cfg.stagger:
            return None
        return s.slot if s.slot is not None else STAGGER_SLOTS.get("Budget" if s.budget else search_group(s))

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

    def _tier(self, s: Search) -> int:
        """Order inside a run: 0 electronics, clothes and the rest, 1 budget, 2 music."""
        from .settings import search_group
        if s.budget:
            return 1
        return 2 if search_group(s) in MUSIC else 0

    def _turn(self, s: Search) -> bool:
        """
        Whether a known search runs this time: electronics every run, the two staggered groups in turn, and
        slow-lane searches (slow_minutes) only when due, at most MAX_SLOW_PER_RUN a run.
        """
        if s.slow_minutes:
            if not self.known.due_slow(s) or s.query not in getattr(self, "_slow_picks", {s.query}):
                return False
        want = self._slot_of(s)
        if want is None:
            return self.known.due(s)
        pick = getattr(self, "_group_slot", None)
        return want == (pick if pick is not None else self._pick_slot())

    @property
    def failed(self) -> bool:
        """Every search errored, e.g. Vinted blocking us."""
        return self.failed_searches == len(self.cfg.searches)


PLATFORM_NAMES = {"vinted": "Vinted", "ebay": "eBay", "subito": "Subito", "mercatino": "Mercatino Musicale", "reverb": "Reverb"}


def describe_miss(query: str, m: Deal) -> str:
    c = m.item.currency
    return (f"[{PLATFORM_NAMES.get(m.item.source, m.item.source)}] '{m.item.title[:50]}' [{query}] "
            f"pay {m.item.total_price + m.shipping:.2f} {c} incl. shipping, "
            f"value {m.market_value:.2f}, profit {m.profit:.2f}, ROI {m.roi:.1f}%, rating {m.rating}, "
            f"blocked by {', '.join(m.blocked)} {m.item.url}")
