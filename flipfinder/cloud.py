"""
The private side of flipFinder: a Cloudflare Worker that keeps settings, deals and the home
area out of the public repo and does all the Telegram talking. Each run reads its settings
here and hands over the deals it found.

FLIPFINDER_API_URL / FLIPFINDER_API_KEY (GitHub secrets, or .env) point at it.
"""

from __future__ import annotations

import logging
from dataclasses import asdict

import requests

from .analyzer import Deal
from .settings import search_group

log = logging.getLogger(__name__)


def deal_record(deal: Deal) -> dict:
    """What the Worker keeps about a deal, besides its alert text."""
    it = deal.item
    sample = sorted(c.price for c in (deal.sample or []))
    return {
        "title": it.title, "url": it.url, "source": it.source,
        "cost": deal.cost or round(it.total_price + deal.shipping, 2), "value": deal.market_value,
        "low": sample[1] if len(sample) > 1 else None,   # ~25th percentile: a quick-sale price
        "profit": deal.profit, "query": getattr(deal, "query", ""), "condition": it.condition,
        "item": asdict(it),
        # queue order overnight: budget deals by profit per euro, the rest by rating, then profit
        "score": [int(deal.budget), round(deal.per_euro, 4) if deal.budget else 0, deal.rating, deal.profit],
    }


def catalog(cfg) -> dict:
    """Searches and rules from config.yaml (before Telegram changes), for /categories, /prices, /rules."""
    return {
        "searches": [{"query": s.query, "price_from": s.price_from, "price_to": s.price_to, "budget": s.budget,
                      "group": search_group(s), "price_to_is_budget": s.price_to_is_budget} for s in cfg.searches],
        "rules": {"min_profit": cfg.rules.min_profit, "min_roi": cfg.rules.min_roi,
                  "min_rating": cfg.rules.min_rating, "max_roi": cfg.rules.max_roi},
        "budget_rules": cfg.budget_rules, "budget": cfg.budget_setting,
        "ebay": cfg.ebay.enabled, "subito": cfg.subito.enabled,
    }


class Cloud:
    def __init__(self, url: str, key: str, timeout: float = 30):
        self.url, self.timeout = url.rstrip("/"), timeout
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {key}"

    def _call(self, method: str, path: str, body=None) -> dict | None:
        try:
            r = self.session.request(method, self.url + path, json=body, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            # never print the response or URL in full: the run logs are public
            log.error("flipFinder Worker %s %s failed: %s", method, path, type(e).__name__)
            return None

    def state(self) -> dict | None:
        """Telegram settings, the home area, the pool, and deals we own (to value them)."""
        return self._call("GET", "/api/state")

    def put_catalog(self, cfg):
        self._call("PUT", "/api/catalog", catalog(cfg))

    def send_deal(self, deal: Deal, text: str) -> str:
        """'sent', 'queued' (quiet hours), 'exists' or 'failed'."""
        res = self._call("POST", "/api/deal", {
            "key": deal.item.key, "text": text, "photo": deal.item.photo, "group": getattr(deal, "group", ""),
            "record": deal_record(deal)})
        return (res or {}).get("status", "failed")

    def report_run(self, status: dict | None = None, values: dict | None = None,
                   notify: list[dict] | None = None) -> dict:
        """Run status, current values of what we own, messages for Summary; {'notified': [...], 'flushed': n}."""
        body = {k: v for k, v in (("status", status), ("values", values), ("notify", notify)) if v}
        return self._call("POST", "/api/run", body) or {}
