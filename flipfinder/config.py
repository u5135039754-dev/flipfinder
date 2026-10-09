"""Loads config.yaml and the Telegram secrets from .env / environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .analyzer import Rules


@dataclass
class Search:
    query: str
    price_from: float | None = None
    price_to: float | None = None
    filters: dict = field(default_factory=dict)   # brand_ids, size_ids, status_ids, catalog_ids
    exclude_keywords: list[str] = field(default_factory=list)
    shipping_cost: float | None = None   # overrides rules.shipping_cost for this search
    resell_costs: float | None = None    # overrides rules.resell_costs for this search
    max_roi: float | None = None         # overrides rules.max_roi (lower where fakes are common)
    match_brand: bool = True             # false: compare across brands (graphics cards)
    subito_category: int | None = None   # Subito category; by default from the Vinted catalog filter
    ebay_category: int | None = None     # eBay.it category; by default from the Vinted catalog filter
    budget: bool = False                 # budget mode: total cost <= config budget, budget_rules apply
    check: str = ""                      # what to check before buying, shown in alerts
    missing_part_cost: float = 0.0       # added when the title says no battery/charger
    every_minutes: float = 0             # scan this search at most this often (0 = every run)
    group: str = ""                      # category shown by the Telegram /categories command
    enabled: bool = True                 # turned off from Telegram (settings.json)
    added: bool = False                  # added from Telegram (settings.json)
    price_to_is_budget: bool = False     # budget search whose max price follows the budget/pool


@dataclass
class EbaySettings:
    """eBay Browse API. On when EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are set."""
    client_id: str = ""
    client_secret: str = ""
    marketplace: str = "EBAY_IT"
    item_location: str = "IT"        # "IT" (items in Italy) or "EU"
    interval_minutes: float = 20     # eBay is searched at most this often, to stay in the daily API limit
    pool_refresh_minutes: float = 180   # eBay price pools are refreshed this often
    new_per_search: int = 50         # newest listings checked per search
    pool_size: int = 200             # listings per search used for market value (one API call)
    default_category: int = 3858     # Chitarre e bassi, for searches without a category
    max_calls_per_day: int = 4500    # eBay allows 5,000; stop searching eBay for the day at this many

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)


@dataclass
class SubitoSettings:
    """
    Subito.it, local listings only (pickup). Off unless `subito: enabled: true`. The home area
    (region, province, center, radius, travel costs) is private: it comes from the Worker each
    run (settings.apply_area), never from config.yaml.
    """
    enabled: bool = False
    region: int | None = None        # Subito region id
    province: int | None = None      # Subito province id
    center: tuple[float, float] | None = None   # home coordinates
    radius_km: float = 30
    travel_cost: float = 5           # going to pick it up
    town_travel_costs: dict = field(default_factory=dict)   # e.g. {"Town": 8}
    default_category: int = 39       # Strumenti Musicali, for searches without a category
    pool_refresh_minutes: float = 180


@dataclass
class Config:
    domain: str
    interval_minutes: float
    request_delay: float
    comparable_pages: int
    pool_refresh_minutes: float
    buyer_fee_fixed: float
    buyer_fee_pct: float
    rules: Rules
    searches: list[Search]
    seen_file: Path
    stagger: bool = True                 # budget and guitar searches take turns, every other run
    budget: float = 72                   # max total cost for budget-mode searches (in effect)
    budget_setting: float = 72           # the configured /budget value (the pool can lower it)
    budget_rules: dict = field(default_factory=lambda: {"min_profit": 12, "min_roi": 35, "max_roi": 150})
    music_rules: dict = field(default_factory=lambda: {"min_profit": 20, "min_roi": 35})
    value_adjust: dict = field(default_factory=dict)   # category or search -> factor on market value (report suggestions)
    ebay: EbaySettings = field(default_factory=EbaySettings)
    subito: SubitoSettings = field(default_factory=SubitoSettings)


def _flat(values) -> list[str]:
    """Exclude lists may contain other lists (a shared list via a YAML anchor plus extras)."""
    out = []
    for v in values:
        out += _flat(v) if isinstance(v, list) else [str(v)]
    return out


def _opt_float(value) -> float | None:
    return None if value is None else float(value)


def load(path: str | Path = "config.yaml") -> Config:
    """config.yaml only; Telegram settings and the home area come from the Worker (settings.py)."""
    load_dotenv()
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    r = raw.get("rules", {})
    global_excl = raw.get("exclude_keywords", [])
    rules = Rules(
        min_profit=float(r.get("min_profit", 10)),
        min_roi=float(r.get("min_roi", 30)),
        min_rating=int(r.get("min_rating", 6)),
        min_comparables=int(r.get("min_comparables", 8)),
        max_roi=float(r.get("max_roi", 400)),
        max_price=float(r["max_price"]) if r.get("max_price") is not None else None,
        resell_costs=float(r.get("resell_costs", 0)),
        shipping_cost=float(r.get("shipping_cost", 15)),
        exclude_keywords=tuple(global_excl),
        # % and fixed fee you pay when you sell on each platform. Both are 0 for private
        # sellers: Vinted never charges sellers, eBay.it stopped for EEA private sellers in 2026.
        sell_fees={k: (float(v.get("percent", 0)), float(v.get("fixed", 0)))
                   for k, v in (raw.get("sell_fees") or {}).items()},
    )

    searches = []
    for s in raw.get("searches", []):
        if isinstance(s, str):
            s = {"query": s}
        searches.append(Search(
            query=s["query"],
            price_from=s.get("price_from"),
            price_to=s.get("price_to"),
            filters=s.get("filters", {}) or {},
            exclude_keywords=_flat(s.get("exclude_keywords", []) or []),
            shipping_cost=_opt_float(s.get("shipping_cost")),
            resell_costs=_opt_float(s.get("resell_costs")),
            max_roi=_opt_float(s.get("max_roi")),
            match_brand=bool(s.get("match_brand", True)),
            subito_category=int(s["subito_category"]) if s.get("subito_category") is not None else None,
            ebay_category=int(s["ebay_category"]) if s.get("ebay_category") is not None else None,
            budget=bool(s.get("budget", False)),
            check=str(s.get("check", "")),
            missing_part_cost=float(s.get("missing_part_cost", 0)),
            every_minutes=float(s.get("every_minutes", 0)),
            group=str(s.get("group", "")),
        ))
    if not searches:
        raise SystemExit("No searches in config.yaml, add at least one.")
    budget = float(raw.get("budget", 72))
    for x in searches:
        if x.budget and (x.price_to is None or x.price_to > budget):
            x.price_to, x.price_to_is_budget = budget, True

    fees = raw.get("buyer_protection", {})
    e = raw.get("ebay") or {}
    sb = raw.get("subito") or {}
    cfg = Config(
        domain=raw.get("domain", "www.vinted.it"),
        interval_minutes=float(raw.get("interval_minutes", 5)),
        request_delay=float(raw.get("request_delay", 2)),
        comparable_pages=int(raw.get("comparable_pages", 3)),
        pool_refresh_minutes=float(raw.get("pool_refresh_minutes", 60)),
        buyer_fee_fixed=float(fees.get("fixed", 0.70)),
        buyer_fee_pct=float(fees.get("percent", 5)),
        rules=rules,
        searches=searches,
        seen_file=Path(raw.get("seen_file", "data/seen.json")),
        stagger=bool(raw.get("stagger", True)),
        budget=float(raw.get("budget", 72)),
        budget_setting=float(raw.get("budget", 72)),
        budget_rules={"min_profit": 12, "min_roi": 35, "max_roi": 150, **(raw.get("budget_rules") or {})},
        music_rules={"min_profit": 20, "min_roi": 35, **(raw.get("music_rules") or {})},
        ebay=EbaySettings(
            client_id=os.getenv("EBAY_CLIENT_ID", ""),
            client_secret=os.getenv("EBAY_CLIENT_SECRET", ""),
            marketplace=e.get("marketplace", "EBAY_IT"),
            item_location=str(e.get("item_location", "IT")),
            interval_minutes=float(e.get("interval_minutes", 20)),
            pool_refresh_minutes=float(e.get("pool_refresh_minutes", 180)),
            new_per_search=int(e.get("new_per_search", 50)),
            pool_size=int(e.get("pool_size", 200)),
            default_category=int(e.get("default_category", 3858)),
            max_calls_per_day=int(e.get("max_calls_per_day", 4500)),
        ),
        subito=SubitoSettings(
            enabled=bool(sb.get("enabled", False)),
            default_category=int(sb.get("default_category", 39)),
            pool_refresh_minutes=float(sb.get("pool_refresh_minutes", 180)),
        ),
    )
    return cfg
