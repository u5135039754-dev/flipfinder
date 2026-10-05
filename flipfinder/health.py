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

from .analyzer import Deal

FAIL_ALERT_AFTER = 3
SUMMARY_TZ = ZoneInfo("Europe/Rome")
SUMMARY_HOUR = 9
EXPECTED_RUNS_PER_DAY = 24 * 60 // 5   # the workflow cron is every 5 minutes


def miss_record(query: str, m: Deal) -> dict:
    return {
        "title": m.item.title, "url": m.item.url, "query": query, "currency": m.item.currency,
        "pay": round(m.item.total_price + m.shipping, 2), "value": m.market_value,
        "profit": m.profit, "roi": m.roi, "rating": m.rating,
        "blocked": m.blocked, "closeness": m.closeness,
    }


class RunStats:
    def __init__(self, path: Path):
        self.path = path
        self.data = {
            "since": time.time(), "runs": 0, "failed_runs": 0, "checked": 0, "deals_sent": 0,
            "best_miss": None, "consecutive_failures": 0, "last_summary_day": "",
        }
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass

    def record_run(self, checked: int, deals_sent: int, best_miss: dict | None):
        d = self.data
        d["runs"] += 1
        d["checked"] += checked
        d["deals_sent"] += deals_sent
        old = d["best_miss"]
        if best_miss and (not old or (best_miss["closeness"], best_miss["profit"])
                          > (old["closeness"], old["profit"])):
            d["best_miss"] = best_miss

    def record_failure(self) -> bool:
        """Count a failed run. True exactly when the streak reaches the alert threshold."""
        d = self.data
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

    def summary_text(self, now: datetime | None = None) -> str:
        now = (now or datetime.now(SUMMARY_TZ)).astimezone(SUMMARY_TZ)
        d = self.data
        since = datetime.fromtimestamp(d["since"], SUMMARY_TZ)
        hours = max((now - since).total_seconds() / 3600, 0.1)
        expected = round(EXPECTED_RUNS_PER_DAY * min(hours, 24) / 24)
        lines = [
            "📊 <b>flipFinder daily summary</b>",
            f"<i>since {since:%a %d %b %H:%M}</i>",
            "",
            f"🔁 Runs: <b>{d['runs']}</b>" + (f" ({d['failed_runs']} failed)" if d["failed_runs"] else "")
            + (f" · schedule should give ~{expected}" if d["runs"] < expected * 0.8 else ""),
            f"🔎 Listings checked: <b>{d['checked']}</b>",
            f"🔥 Deals sent: <b>{d['deals_sent']}</b>",
        ]
        m = d["best_miss"]
        if m:
            sym = "€" if m["currency"] == "EUR" else m["currency"] + " "
            lines += [
                "",
                f"🥈 Best near miss: <a href=\"{html.escape(m['url'])}\">{html.escape(m['title'][:80])}</a>",
                f"pay {sym}{m['pay']:.2f} incl. shipping · value {sym}{m['value']:.2f} · "
                f"profit {sym}{m['profit']:.2f} · ROI {m['roi']:.1f}% · rating {m['rating']}/10",
                f"blocked by: {html.escape(', '.join(m['blocked']))}",
            ]
        else:
            lines += ["", "🥈 No near misses: nothing came close to the rules."]
        return "\n".join(lines)

    def mark_summary_sent(self, now: datetime | None = None):
        now = (now or datetime.now(SUMMARY_TZ)).astimezone(SUMMARY_TZ)
        streak = self.data["consecutive_failures"]
        self.data.update({"since": now.timestamp(), "runs": 0, "failed_runs": 0, "checked": 0,
                          "deals_sent": 0, "best_miss": None, "consecutive_failures": streak,
                          "last_summary_day": now.date().isoformat()})

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data), encoding="utf-8")
        tmp.replace(self.path)
