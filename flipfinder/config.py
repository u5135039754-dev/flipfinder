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

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)


@dataclass
class SubitoSettings:
    """Subito.it, local listings only (pickup). Off unless `subito: enabled: true`."""
    enabled: bool = False
    region: int = 5                  # Subito region id (5 = Home region)
    province: int = 2                # Subito province id (2 = Hometown)
    center: tuple[float, float] = (44.5000, 11.3000)   # home town coordinates (Hometown)
    radius_km: float = 30
    travel_cost: float = 5           # going to pick it up
    town_travel_costs: dict = field(default_factory=dict)   # e.g. {"Southtown": 8}
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
    telegram_token: str
    telegram_chat_id: str
    seen_file: Path
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
        ))
    if not searches:
        raise SystemExit("No searches in config.yaml, add at least one.")

    fees = raw.get("buyer_protection", {})
    e = raw.get("ebay") or {}
    sb = raw.get("subito") or {}
    return Config(
        domain=raw.get("domain", "www.vinted.it"),
        interval_minutes=float(raw.get("interval_minutes", 5)),
        request_delay=float(raw.get("request_delay", 2)),
        comparable_pages=int(raw.get("comparable_pages", 3)),
        pool_refresh_minutes=float(raw.get("pool_refresh_minutes", 60)),
        buyer_fee_fixed=float(fees.get("fixed", 0.70)),
        buyer_fee_pct=float(fees.get("percent", 5)),
        rules=rules,
        searches=searches,
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        seen_file=Path(raw.get("seen_file", "data/seen.json")),
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
        ),
        subito=SubitoSettings(
            enabled=bool(sb.get("enabled", False)),
            region=int(sb.get("region", 5)),
            province=int(sb.get("province", 2)),
            center=tuple(float(x) for x in sb.get("center", (44.5000, 11.3000))),
            radius_km=float(sb.get("radius_km", 30)),
            travel_cost=float(sb.get("travel_cost", 5)),
            town_travel_costs={str(k): float(v) for k, v in (sb.get("town_travel_costs") or {}).items()},
            default_category=int(sb.get("default_category", 39)),
            pool_refresh_minutes=float(sb.get("pool_refresh_minutes", 180)),
        ),
    )
