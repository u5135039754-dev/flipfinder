"""
The private side of flipFinder: a Cloudflare Worker that keeps settings, deals and the home
area out of the public repo and does all the Telegram talking. Each run reads its settings
here and hands over the deals it found.

FLIPFINDER_API_URL / FLIPFINDER_API_KEY (GitHub secrets, or .env) point at it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict

import requests

from .analyzer import Deal
from .settings import search_group
from .telegram import demand_line, seller_lines

log = logging.getLogger(__name__)


def deal_record(deal: Deal) -> dict:
    """What the Worker keeps about a deal, besides its alert text."""
    it = deal.item
    sample = sorted(c.price for c in (deal.sample or []))
    return {
        "title": it.title, "url": it.url, "source": it.source,
        "cost": deal.cost or round(it.total_price + deal.shipping, 2), "value": deal.market_value,
        "low": sample[1] if len(sample) > 1 else None,   # ~25th percentile: a quick-sale price
        "profit": deal.profit, "rating": deal.rating, "query": getattr(deal, "query", ""), "condition": it.condition,
        "item": asdict(it), "sell_days": deal.sell_days, "sold_count": deal.sold_count,
        "demand": demand_line(deal) if deal.demand else "",
        "seller": " · ".join(x.replace("👤 ", "") for x in seller_lines(deal)),
        # what the market value was worked out from, for the AI check
        "comparables": [{"title": c.title[:80], "price": c.price, "condition": c.condition, "source": c.source}
                        for c in (deal.sample or [])[:8]],
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
        self.url, self.timeout, self.retry_wait = url.rstrip("/"), timeout, 3
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {key}"

    def _call(self, method: str, path: str, body=None) -> dict | None:
        """
        One retry: always when the connection failed (nothing reached the Worker), and on a
        timeout or server error only for reads, since a deal or message may already have gone out.
        """
        for attempt in (1, 2):
            try:
                r = self.session.request(method, self.url + path, json=body, timeout=self.timeout)
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as e:
                retry = attempt == 1 and (
                    isinstance(e, requests.ConnectionError) and not isinstance(e, requests.ReadTimeout)
                    or method in ("GET", "PUT") and (isinstance(e, requests.Timeout) or
                                                     getattr(getattr(e, "response", None), "status_code", 0) >= 500))
                # never print the response or URL in full: the run logs are public
                log.log(logging.WARNING if retry else logging.ERROR, "flipFinder Worker %s %s failed: %s%s",
                        method, path, type(e).__name__, ", retrying" if retry else "")
                if not retry:
                    return None
                time.sleep(self.retry_wait)
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

    def analyze(self, keys: list[str]) -> dict:
        """AI checks for new deals (the Worker decides: only when /ai auto is on). Never stops a run."""
        try:
            res = self.session.post(f"{self.url}/api/analyze", json={"keys": keys}, timeout=120)
            return res.json() if res.ok else {}
        except (requests.RequestException, ValueError) as e:
            log.warning("AI check request failed: %s", e)
            return {}

    def put_demand(self, table: dict):
        self._call("PUT", "/api/demand", {"updated": time.time(), "searches": table})

    def report_run(self, status: dict | None = None, values: dict | None = None,
                   notify: list[dict] | None = None) -> dict:
        """Run status, current values of what we own, messages for Summary; {'notified': [...], 'flushed': n}."""
        body = {k: v for k, v in (("status", status), ("values", values), ("notify", notify)) if v}
        return self._call("POST", "/api/run", body) or {}
