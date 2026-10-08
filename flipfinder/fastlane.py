"""
The fast lane: between full scans, the newest listings of the top searches, every 1-2 minutes.

The settings live in the Worker (/fast 2, /fast off, /fast groups ...) and come with the run's
state, with when the last pass started and how many ran (for the rotation). cron-job.org starts
the fast workflow every minute; a run that isn't due (off, quiet hours, too soon, backing off
after a block) stops in a second.
"""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")
DEFAULTS = {
    "enabled": False,
    "interval": 2,           # minutes between passes
    # Guitars first, then electronics; budget searches stay with the full scan
    "groups": ["Guitars", "Amps", "Pedals", "Electronics", "Audio"],
    "per_run": 8,            # searches per pass (~4 s each: a pass stays under 40 s); the list rotates
    "backoff_until": 0,      # after a Vinted 403/429: every BACKOFF_INTERVAL min until then
}
BACKOFF_INTERVAL = 5
BACKOFF_SECONDS = 3600
QUIET_UNTIL_HOUR = 8         # same as the Worker: no deal alerts 00:00-08:00 Italy time
SLACK_SECONDS = 30           # runs start 20-60 s after their trigger: a pass this close to due goes


def settings(state: dict | None) -> dict:
    return {**DEFAULTS, **(((state or {}).get("settings") or {}).get("fast") or {})}


def interval(fs: dict, now: float) -> int:
    return BACKOFF_INTERVAL if now < (fs.get("backoff_until") or 0) else max(1, int(fs["interval"]))


def due(fs: dict, now: float | None = None, last_started: float = 0) -> tuple[bool, str]:
    """Whether a pass runs now (the last one started `last_started`), and why not."""
    now = time.time() if now is None else now
    if not fs["enabled"]:
        return False, "the fast lane is off (/fast 2 turns it on)"
    if datetime.fromtimestamp(now, ROME).hour < QUIET_UNTIL_HOUR:
        return False, "quiet hours (deals wait until 08:00 anyway)"
    every = interval(fs, now)
    if now - (last_started or 0) < every * 60 - SLACK_SECONDS:
        return False, f"too soon (every {every} min" + (", backing off after a Vinted block)"
                                                        if now < (fs.get("backoff_until") or 0) else ")")
    return True, ""


def searches_for(cfg, fs: dict, group_of) -> list:
    """The searches in the chosen groups, in the order of the groups (guitars first)."""
    order = [g.lower() for g in fs["groups"]]
    picked = [s for s in cfg.searches if s.enabled and not s.budget and group_of(s).lower() in order]
    return sorted(picked, key=lambda s: order.index(group_of(s).lower()))


def this_pass(cfg, fs: dict, group_of, passes: int = 0) -> list[str]:
    """This pass's share of the list: per_run searches, the next share each pass, so every one gets a turn."""
    pool = [s.query for s in searches_for(cfg, fs, group_of)]
    per = max(1, int(fs["per_run"]))
    if len(pool) <= per:
        return pool
    chunks = (len(pool) + per - 1) // per
    slot = int(passes) % chunks
    return pool[slot * per:(slot + 1) * per]
