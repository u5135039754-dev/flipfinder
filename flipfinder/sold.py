"""
Sold-price tracking (Vinted). Asking prices overstate what things sell for, so we keep a
history of the comparable listings in each search's price pool:

- every pool refresh records each listing (first seen, last seen, last price);
- a listing missing from the next refresh is only a *candidate*: most just dropped past the
  pages we read. A few candidates per run get their page opened: "can_buy": false (not
  hidden, not reserved) means sold, at the last price we saw; 404 means deleted;
- when it was listed comes from Vinted's ids, which only go up: the newest feeds give
  (id, time) anchors, so any later listing's date can be estimated, and with it days to sell.

Sold listings then count in market value (analyzer.blend_sold) and "⏱ sells in ~X days".
Everything lives in data/sold.json (public listing data only), kept 90 days.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, fields
from pathlib import Path
from statistics import median

from .vinted import Item

log = logging.getLogger(__name__)

DAY = 86400
KEEP_DAYS = 90            # sold records and listings not seen for this long are dropped
PENDING_DAYS = 5          # a candidate not checked within this is dropped
PENDING_MAX = 400
MAX_CHECKS = 4            # item pages opened per run, at most
PAUSE_HOURS = 6           # after blocks pushed the checks to 0
RAISE_AFTER_HOURS = 24    # this long without a block: one more check per run again
ANCHOR_GAP = 600          # seconds between id/time anchors

_ITEM_FIELDS = {f.name for f in fields(Item)}


def _item(d: dict) -> Item:
    return Item(**{k: v for k, v in d.items() if k in _ITEM_FIELDS})


class SoldTracker:
    def __init__(self, path: Path):
        self.path = path
        self.data = {"seen": {}, "pending": {}, "sold": {}, "anchors": [], "keys": {},
                     "checks": {"per_run": MAX_CHECKS, "changed_at": 0.0, "paused_until": 0.0}}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                log.warning("sold.json is unreadable, starting a new one")
        self.checked = 0
        self.notices: list[str] = []   # for the Summary topic: checks lowered or raised

    # --- when listings were created
    def anchor(self, items: list[Item], now: float):
        """The newest feeds: their highest id was listed just now."""
        ids = [i.id for i in items if i.source == "vinted"]
        if not ids:
            return
        top = max(ids)
        a = self.data["anchors"]
        if a and top <= a[-1][0]:
            return
        if len(a) > 1 and now - a[-2][1] < ANCHOR_GAP:
            a[-1] = [top, now]   # thin out: one anchor per ANCHOR_GAP, never replacing the first
        else:
            a.append([top, now])

    def listed_at(self, item_id: int) -> float | None:
        """Estimated listing time from the id anchors; None for listings older than our first anchor."""
        a = self.data["anchors"]
        if not a or item_id < a[0][0]:
            return None
        for (i0, t0), (i1, t1) in zip(a, a[1:]):
            if i0 <= item_id <= i1:
                return t0 + (t1 - t0) * ((item_id - i0) / (i1 - i0) if i1 > i0 else 0)
        return a[-1][1]

    # --- the price pools
    def observe(self, key: str, old: list[Item], items: list[Item], now: float, old_ts: float | None = None):
        """
        A fresh Vinted price pool for a search (`old` is the one it replaces): listings missing
        since last time become candidates. Only dates and prices are kept per listing; the full
        listing comes from the old pool when it goes missing.
        """
        before = {str(i.id): i for i in old if i.source == "vinted"}
        if key not in self.data["seen"] and before and old_ts:
            # first time for this search: the previous pool is its first record
            self.data["seen"][key] = {sid: [old_ts, old_ts, it.price] for sid, it in before.items()}
        counts = self.data["keys"].setdefault(key, {"since": (old_ts if before and old_ts else now),
                                                      "gone": 0, "decided": 0, "sold": 0})
        seen = self.data["seen"].setdefault(key, {})
        current = {str(i.id): i for i in items if i.source == "vinted"}
        pending = self.data["pending"]
        for sid, it in current.items():
            first = seen[sid][0] if sid in seen else now
            seen[sid] = [first, now, it.price]
            pending.pop(sid, None)   # back in the pool: it was only on a later page
        for sid in [x for x in seen if x not in current]:
            first, last, price = seen.pop(sid)
            if sid in before:
                pending[sid] = {"key": key, "gone": now, "last": last, "first": first,
                                "item": {**asdict(before[sid]), "price": price}, "tries": 0}
                counts["gone"] += 1

    def candidates(self, limit: int) -> list[str]:
        """Searches with the fewest sold records first, then the most recently gone."""
        sold_counts = {k: len(v) for k, v in self.data["sold"].items()}
        pend = self.data["pending"]
        order = sorted(pend, key=lambda sid: (sold_counts.get(pend[sid]["key"], 0), -pend[sid]["gone"]))
        return order[:limit]

    # --- opening listing pages
    def verify(self, client, now: float) -> dict:
        """Checks up to `per_run` candidates. Returns counts per result."""
        c = self.data["checks"]
        limit = c["per_run"] if now >= c.get("paused_until", 0) else 0
        if c["per_run"] == 0 and now >= c.get("paused_until", 0):
            limit = 1   # the pause is over: try one
        out = {"sold": 0, "active": 0, "deleted": 0, "reserved": 0, "unknown": 0, "blocked": 0}
        for sid in self.candidates(limit):
            p = self.data["pending"][sid]
            status = client.item_status(int(sid))
            self.checked += 1
            out[status] += 1
            if status == "blocked":
                break
            counts = self.data["keys"].setdefault(p["key"], {"since": now, "gone": 0, "decided": 0, "sold": 0})
            if status in ("sold", "active", "deleted"):
                counts["decided"] += 1
                counts["sold"] += status == "sold"
            if status == "sold":
                self.data["pending"].pop(sid)
                sold_at = (p["last"] + p["gone"]) / 2   # somewhere between last seen and gone
                listed = self.listed_at(int(sid))
                days = round(max(sold_at - listed, 0) / DAY, 1) if listed else None
                self.data["sold"].setdefault(p["key"], []).append(
                    {"item": p["item"], "at": round(sold_at), "days": days})
            elif status == "active":
                self.data["pending"].pop(sid)   # still for sale, just off our pages
            elif status == "deleted":
                self.data["pending"].pop(sid)
            else:   # reserved or unclear: look again later
                p["tries"] += 1
                if p["tries"] >= 3:
                    self.data["pending"].pop(sid)
        return out

    def adapt(self, blocked_requests: int, now: float):
        """Fewer page checks while Vinted refuses requests; back up slowly once it stops."""
        c = self.data["checks"]
        before = c["per_run"]
        if blocked_requests:
            c["per_run"] = before // 2
            c["changed_at"] = now
            if c["per_run"] == 0:
                c["paused_until"] = now + PAUSE_HOURS * 3600
            if before:
                self.notices.append(
                    f"⚠️ Vinted refused {blocked_requests} request(s) this run, so sold-price checks are lowered to "
                    + (f"{c['per_run']} per run." if c["per_run"] else f"none for {PAUSE_HOURS} h, then 1 per run."))
        elif before < MAX_CHECKS and now - c.get("changed_at", 0) >= RAISE_AFTER_HOURS * 3600 \
                and now >= c.get("paused_until", 0):
            c["per_run"] = before + 1
            c["changed_at"] = now
            self.notices.append(f"✅ No Vinted blocks for {RAISE_AFTER_HOURS} h: sold-price checks back up to "
                                f"{c['per_run']} per run.")

    # --- what the valuation uses
    def sold_for(self, key: str, now: float, days: int = 60) -> list[tuple[Item, float | None, float]]:
        """(listing, days to sell or None, when it sold) for the last `days` days."""
        return [(_item(r["item"]), r.get("days"), r["at"]) for r in self.data["sold"].get(key, [])
                if now - r["at"] <= days * DAY]

    def coverage(self, key: str, now: float) -> tuple[float, float | None]:
        """
        (days tracked, scale): we only open some gone listings' pages, so the sold ones we find
        are a sample. scale = listings gone / listings checked; None until 5 have been checked.
        """
        k = self.data["keys"].get(key)
        if not k:
            return 0.0, None
        scale = k["gone"] / k["decided"] if k["decided"] >= 5 else None
        return (now - k["since"]) / DAY, scale

    def prune(self, now: float):
        d = self.data
        for key in list(d["sold"]):
            d["sold"][key] = [r for r in d["sold"][key] if now - r["at"] <= KEEP_DAYS * DAY]
        for key in list(d["seen"]):
            d["seen"][key] = {k: v for k, v in d["seen"][key].items() if now - v[1] <= KEEP_DAYS * DAY}
        pend = {k: v for k, v in d["pending"].items() if now - v["gone"] <= PENDING_DAYS * DAY}
        if len(pend) > PENDING_MAX:   # keep the most recently gone
            pend = dict(sorted(pend.items(), key=lambda kv: -kv[1]["gone"])[:PENDING_MAX])
        d["pending"] = pend
        d["anchors"] = d["anchors"][-5000:]

    def stats(self) -> dict:
        d = self.data
        return {"sold": sum(len(v) for v in d["sold"].values()), "pending": len(d["pending"]),
                "tracked": sum(len(v) for v in d["seen"].values()), "per_run": d["checks"]["per_run"]}

    def save(self, now: float | None = None):
        self.prune(now or time.time())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.path)


def days_to_sell(sold: list[tuple], min_known: int = 3) -> float | None:
    known = [x[1] for x in sold if x[1] is not None]
    return round(median(known), 1) if len(known) >= min_known else None
