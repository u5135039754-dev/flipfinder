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
            exclude_keywords=s.get("exclude_keywords", []) or [],
            shipping_cost=_opt_float(s.get("shipping_cost")),
            resell_costs=_opt_float(s.get("resell_costs")),
        ))
    if not searches:
        raise SystemExit("No searches in config.yaml, add at least one.")

    fees = raw.get("buyer_protection", {})
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
    )
