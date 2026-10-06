"""
Group deal tracking (deals.json, committed back by the workflow):
  ✋ Claim -> 💸 Bought (price asked) -> 🏷 Listed -> ✅ Sold (price asked)
plus a shared money pool, 👍/👎 feedback and a ready-to-copy message for the seller.
"""

from __future__ import annotations

import html
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DEALS_FILE = Path("deals.json")
STATUS = {"new": "", "claimed": "✋ Claimed", "bought": "💸 Bought", "listed": "🏷 Listed", "sold": "✅ Sold"}
NEXT = {"claimed": ("b", "💸 Bought"), "bought": ("l", "🏷 Listed"), "listed": ("s", "✅ Sold")}
SELLER_MESSAGE = ("Ciao! L'articolo \"{title}\" è ancora disponibile? Se sì, potresti mandarmi un breve video "
                  "in cui si vede che funziona? Grazie mille!")


def euro(x: float) -> str:
    return f"€{x:,.2f}"


class DealBook:
    def __init__(self, path: Path = DEALS_FILE):
        self.path = path
        self.data = {"pool": None, "deals": {}, "feedback": [], "pending": {}}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                log.error("deals.json is unreadable, starting a new one")
        self.changed = False

    def save(self):
        # deals nobody touched (no claim, no votes) are dropped after 30 days
        old = time.time() - 30 * 86400
        stale = [k for k, d in self.data["deals"].items()
                 if d["status"] == "new" and not d["votes"] and d.get("sent", 0) < old]
        for k in stale:
            del self.data["deals"][k]
        self.changed = self.changed or bool(stale)
        if self.changed:
            self.path.write_text(json.dumps(self.data, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
                                 encoding="utf-8")
            self.changed = False

    # --- recording sent deals
    def record(self, deal, text: str, messages: list[dict]):
        """A deal alert went out: remember it and where (so buttons can edit every copy)."""
        it = deal.item
        d = self.data["deals"].setdefault(it.key, {
            "title": it.title, "url": it.url, "source": it.source, "status": "new",
            "cost": deal.cost or round(it.total_price + deal.shipping, 2), "value": deal.market_value,
            "profit": deal.profit, "sent": time.time(), "text": text, "messages": [], "votes": {},
        })
        d["messages"] += messages
        self.changed = True

    # --- pool
    @property
    def pool(self) -> float | None:
        """Shared money: starting amount - what was paid + what things sold for."""
        p = self.data.get("pool")
        if p is None:
            return None
        spent = sum(d.get("paid") or 0 for d in self.data["deals"].values() if d["status"] in ("bought", "listed", "sold")
                    and (d.get("bought_at") or 0) >= p["since"])
        earned = sum(d.get("sold_for") or 0 for d in self.data["deals"].values() if d["status"] == "sold"
                     and (d.get("sold_at") or 0) >= p["since"])
        return round(p["start"] - spent + earned, 2)

    def set_pool(self, amount: float):
        self.data["pool"] = {"start": amount, "since": time.time()}
        self.changed = True

    # --- keyboards and status text
    def keyboard(self, key: str) -> dict:
        d = self.data["deals"][key]
        up = sum(1 for v in d["votes"].values() if v == "up")
        down = sum(1 for v in d["votes"].values() if v == "down")
        rows = []
        if d["status"] == "new":
            rows.append([{"text": "✋ Claim", "callback_data": f"c:{key}"}])
        elif d["status"] in NEXT:
            code, label = NEXT[d["status"]]
            rows.append([{"text": f"{label} ({d['who']})", "callback_data": f"{code}:{key}"}])
        if d["status"] != "sold":
            rows.append([{"text": f"👍 {up}" if up else "👍", "callback_data": f"up:{key}"},
                         {"text": f"👎 {down}" if down else "👎", "callback_data": f"dn:{key}"},
                         {"text": "📩 Message seller", "callback_data": f"m:{key}"}])
        return {"inline_keyboard": rows}

    def status_line(self, key: str) -> str:
        d = self.data["deals"][key]
        st = d["status"]
        if st == "new":
            return ""
        line = f"{STATUS[st]} by {html.escape(d['who'])}"
        if d.get("paid") is not None:
            line += f" · paid {euro(d['paid'])}"
        if st == "sold" and d.get("sold_for") is not None:
            line += f" · sold for {euro(d['sold_for'])} · profit {euro(d['sold_for'] - (d.get('paid') or 0))}"
        return line

    def full_text(self, key: str) -> str:
        d = self.data["deals"][key]
        line = self.status_line(key)
        return d["text"] + (f"\n\n<b>{line}</b>" if line else "")

    # --- reports
    def stock(self) -> list[dict]:
        return [dict(d, key=k) for k, d in self.data["deals"].items() if d["status"] in ("bought", "listed")]

    def profit(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        out = {"total": 0.0, "month": 0.0, "people": {}, "sold": 0}
        for d in self.data["deals"].values():
            if d["status"] != "sold" or d.get("sold_for") is None:
                continue
            p = d["sold_for"] - (d.get("paid") or 0)
            out["total"] += p
            out["sold"] += 1
            out["people"][d["who"]] = out["people"].get(d["who"], 0) + p
            sold = datetime.fromtimestamp(d.get("sold_at") or 0, timezone.utc)
            if (sold.year, sold.month) == (now.year, now.month):
                out["month"] += p
        return out
