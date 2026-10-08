"""
Run bookkeeping so silence is never a mystery: a Telegram alert after 3 failed
runs in a row, and a daily summary. Kept in data/stats.json next to seen.json.
"""

from __future__ import annotations

import html
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .analyzer import Deal, Rules, blocked_reasons

FAIL_ALERT_AFTER = 3
SUMMARY_TZ = ZoneInfo("Europe/Rome")
SUMMARY_HOUR = 9
RUNS_PER_HOUR = 12   # cron-job.org (and the backup GitHub cron) trigger a run every 5 minutes


def pct(value: float) -> str:
    """29.7 -> "29.7%", 21.0 -> "21%" (a rounded 30% next to "ROI < 30%" reads like a bug)."""
    return f"{value:.1f}".rstrip("0").rstrip(".") + "%"


def miss_record(query: str, m: Deal) -> dict:
    return {
        "title": m.item.title, "url": m.item.url, "query": query, "currency": m.item.currency,
        "pay": round(m.item.total_price + m.shipping, 2), "value": m.market_value,
        "profit": m.profit, "roi": m.roi, "rating": m.rating, "closeness": m.closeness,
    }   # no "blocked" text: it's worded at summary time with the config values then


class RunStats:
    def __init__(self, path: Path):
        self.path = path
        self.data = {
            "since": time.time(), "runs": 0, "failed_runs": 0, "checked": 0, "deals_sent": 0,
            "best_miss": None, "consecutive_failures": 0, "last_summary_day": "", "seller_skipped": 0,
        }
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass

    def record_run(self, checked: int, deals_sent: int, best_miss: dict | None, seller_skipped: int = 0):
        d = self.data
        d["seller_skipped"] = d.get("seller_skipped", 0) + seller_skipped
        d["last_run"] = time.time()
        d["runs"] += 1
        d["checked"] += checked
        d["deals_sent"] += deals_sent
        old = d["best_miss"]
        if best_miss and best_miss["profit"] > 0 and (not old or (best_miss["closeness"], best_miss["profit"])
                          > (old["closeness"], old["profit"])):
            d["best_miss"] = best_miss

    def record_failure(self) -> bool:
        """Count a failed run. True exactly when the streak reaches the alert threshold."""
        d = self.data
        d["last_run"] = time.time()
        d["runs"] += 1
        d["failed_runs"] += 1
        d["consecutive_failures"] += 1
        return d["consecutive_failures"] == FAIL_ALERT_AFTER

    def record_success(self) -> int:
        """Ends a failure streak. Returns its length if an alert went out for it, else 0."""
        streak = self.data["consecutive_failures"]
        self.data["consecutive_failures"] = 0
        return streak if streak >= FAIL_ALERT_AFTER else 0

    def summary_due(self, now: datetime | None = None) -> bool:
        now = (now or datetime.now(SUMMARY_TZ)).astimezone(SUMMARY_TZ)
        return now.hour >= SUMMARY_HOUR and self.data["last_summary_day"] != now.date().isoformat()

    def summary_text(self, rules: Rules, now: datetime | None = None) -> str:
        """Short, phone-friendly summary. Blocking reasons use the current rules."""
        now = (now or datetime.now(SUMMARY_TZ)).astimezone(SUMMARY_TZ)
        d = self.data
        hours = max((now - datetime.fromtimestamp(d["since"], SUMMARY_TZ)).total_seconds() / 3600, 0.1)
        period = "last 24h" if 23 <= hours <= 25 else f"last {hours:.0f}h"
        expected = round(RUNS_PER_HOUR * hours)
        lines = [
            f"📊 <b>flipFinder · {period}</b>",
            f"Runs: {d['runs']:,} · Listings checked: {d['checked']:,} · Deals: {d['deals_sent']:,}",
        ]
        if d.get("seller_skipped"):
            n = d["seller_skipped"]
            lines.append(f"🛡 {n} deal{'s' if n > 1 else ''} skipped: brand-new seller and suspiciously cheap")
        if d["failed_runs"]:
            lines.append(f"⚠️ {d['failed_runs']:,} run(s) failed")
        if d["runs"] < expected * 0.8:
            lines.append(f"⚠️ Expected ~{expected:,} runs ({RUNS_PER_HOUR}/hour), the trigger may be off")
        m = d["best_miss"]
        blocked = blocked_reasons(m["profit"], m["roi"], m["rating"], rules) if m else []
        if m and m["profit"] > 0 and blocked:
            sym = "€" if m["currency"] == "EUR" else m["currency"] + " "
            lines += [
                f'Closest miss: <a href="{html.escape(m["url"], quote=True)}">{html.escape(m["title"][:60])}</a>',
                f"{sym}{m['pay']:,.0f} → worth {sym}{m['value']:,.0f} · +{sym}{m['profit']:,.0f} "
                f"({pct(m['roi'])}) · blocked: {html.escape(', '.join(blocked))}",
            ]
        else:
            lines.append("No near misses")
        return "\n".join(lines)

    def mark_summary_sent(self, now: datetime | None = None):
        now = (now or datetime.now(SUMMARY_TZ)).astimezone(SUMMARY_TZ)
        streak = self.data["consecutive_failures"]
        self.data.update({"since": now.timestamp(), "runs": 0, "failed_runs": 0, "checked": 0, "seller_skipped": 0,
                          "deals_sent": 0, "best_miss": None, "consecutive_failures": streak,
                          "last_summary_day": now.date().isoformat()})

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data), encoding="utf-8")
        tmp.replace(self.path)
