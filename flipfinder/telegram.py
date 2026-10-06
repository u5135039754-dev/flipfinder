"""Sends deals to a Telegram chat through a bot."""

from __future__ import annotations

import html
import logging

import requests

from .analyzer import Deal

log = logging.getLogger(__name__)

PLATFORMS = {"vinted": "Vinted", "ebay": "eBay", "subito": "Subito"}
SYMBOLS = {"EUR": "€", "GBP": "£", "USD": "$", "PLN": "zł "}


def money(amount: float, currency: str) -> str:
    return f"{SYMBOLS.get(currency, currency + ' ')}{amount:.2f}"


def stars(rating: int) -> str:
    return "🟢" if rating >= 8 else "🟡" if rating >= 6 else "🟠"


def format_deal(deal: Deal) -> str:
    it = deal.item
    c = it.currency
    extras = " · ".join(x for x in (it.brand, it.size, it.condition) if x)
    lines = [
        f"🔥 <b>{html.escape(it.title[:120])}</b>",   # photo captions max out at 1024 chars
    ]
    if extras:
        lines.append(f"<i>{html.escape(extras)}</i>")
    platform = PLATFORMS.get(it.source, it.source)
    where = html.escape(it.location) + (f" ({it.distance_km:.0f} km)" if it.distance_km is not None else "")
    lines.append(f"🛒 <b>{platform}</b>" + (f" · {where}" if it.location else ""))
    if it.negotiable:
        lines.append("💬 Negotiable")
    if deal.pickup_only:
        lines.append("📍 <b>Pickup only</b>" + (f" · {html.escape(deal.city)}" if deal.city else ""))
    if it.delivery == "pickup":
        delivery = (f"🚗 Pickup + packaging: <b>{money(deal.shipping + deal.packaging, c)}</b> "
                    f"(travel to {html.escape(it.location)})")
    elif deal.pickup_only:
        delivery = f"📦 Packaging: <b>{money(deal.packaging, c)}</b> (no shipping, pickup)"
    elif deal.shipping_known:
        delivery = f"📦 Shipping + packaging: <b>{money(deal.shipping + deal.packaging, c)}</b>"
    else:
        delivery = (f"📦 Shipping + packaging: <b>~{money(deal.shipping + deal.packaging, c)}</b> "
                    "(shipping estimated)")
    lines += [
        "",
        f"💶 Item price: <b>{money(it.total_price, c)}</b> (listed {money(it.price, c)})",
        delivery,
        f"🏷 Original price: <b>{money(deal.market_value, c)}</b> (median of {deal.comparables} listings)",
        *platform_lines(deal),
        *([f"🔋 No battery/charger: +{money(deal.missing_part, c)} to buy one (included)"]
          if deal.missing_part else []),
        f"💰 Possible profit: <b>{money(deal.profit, c)}</b>",
        f"📈 Percentage: <b>+{deal.roi:.0f}%</b>",
        f"{stars(deal.rating)} Rating: <b>{deal.rating}/10</b>",
        *([f"🎯 Budget: {money(deal.cost, c)} total · {money(deal.per_euro, c)} profit per € spent"]
          if deal.budget else []),
        *([f"🔍 Before buying: {html.escape(deal.check)}"] if deal.check else []),
        "",
        f'<a href="{html.escape(it.url)}">Open on {platform}</a>',
    ]
    return "\n".join(lines)


def platform_lines(deal: Deal) -> list[str]:
    """Same model's median price on each platform, and where to buy / resell."""
    c = deal.item.currency
    by = deal.by_platform
    if not by or (len(by) < 2 and not deal.sell_fee):
        return []   # nothing to compare and no fee to point out
    prices = " · ".join(f"{PLATFORMS.get(src, src)} {money(v, c)} ({n})" for src, (v, n) in by.items())
    lines = [f"📊 By platform: {prices}"]
    resell = PLATFORMS.get(deal.resell_on, deal.resell_on)
    fee = f", {money(deal.sell_fee, c)} fee included" if deal.sell_fee else ", no seller fee"
    if len(by) > 1:
        cheapest = min(by, key=lambda src: by[src][0])
        lines.append(f"↔️ Cheaper to buy on {PLATFORMS.get(cheapest, cheapest)}, "
                     f"sells for more on {resell}{fee}")
    else:
        lines.append(f"↔️ Resell on {resell}{fee}")
    return lines


CLAIM_BUTTON = {"inline_keyboard": [[{"text": "I'm on it ✋", "callback_data": "claim"}]]}


def parse_chat_ids(value: str) -> list[str]:
    """TELEGRAM_CHAT_ID can list several chats: "1000001,-100000000002"."""
    return [c.strip() for c in str(value).split(",") if c.strip()]


class Telegram:
    def __init__(self, token: str, chat_ids):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_ids = parse_chat_ids(chat_ids) if isinstance(chat_ids, str) else [str(c) for c in chat_ids]
        self.migrations: dict[str, str] = {}   # group -> supergroup ids learned this run

    @property
    def chat_id(self) -> str:
        """The first chat (the owner's private chat), e.g. for messages only they need."""
        return self.chat_ids[0] if self.chat_ids else ""

    def _request(self, method: str, payload: dict, quiet: bool = False):
        """POST to the Bot API; follows a group's upgrade to a supergroup. Returns (ok, result)."""
        for _ in range(2):
            try:
                r = requests.post(f"{self.base}/{method}", json=payload, timeout=20)
                data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            except (requests.RequestException, ValueError) as e:
                log.warning("Telegram %s error: %s", method, e)
                return False, None
            if r.ok and data.get("ok"):
                return True, data.get("result", True)
            new_id = (data.get("parameters") or {}).get("migrate_to_chat_id")
            old_id = str(payload.get("chat_id", ""))
            if new_id and old_id:
                # The group was upgraded to a supergroup and has a new id: use it from now on
                new_id = str(new_id)
                log.warning("Telegram chat %s moved to %s, switching", old_id, new_id)
                self.migrations[old_id] = new_id
                self.chat_ids = [new_id if c == old_id else c for c in self.chat_ids]
                payload = {**payload, "chat_id": new_id}
                continue
            (log.warning if quiet else log.error)("Telegram %s failed: %s %s", method, r.status_code, r.text[:200])
            return False, None
        return False, None

    def _post(self, method: str, payload: dict, quiet: bool = False) -> bool:
        return self._request(method, payload, quiet)[0]

    def call(self, method: str, payload: dict):
        """Any Bot API method; returns its result, or None on failure."""
        return self._request(method, payload, quiet=True)[1]

    def send_text(self, text: str, chat_ids=None, buttons: dict | None = None) -> bool:
        """Sends to every chat (or the given ones); True if at least one got it."""
        sent = False
        for chat in list(chat_ids or self.chat_ids):
            payload = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
            if buttons:
                payload["reply_markup"] = buttons
            sent = self._post("sendMessage", payload) or sent
        return sent

    def send_deal(self, deal: Deal) -> bool:
        text = format_deal(deal)
        sent = False
        for chat in list(self.chat_ids):
            ok = False
            if deal.item.photo:
                # Telegram can't load some listing photos (e.g. .webp); the text alert follows anyway
                ok = self._post("sendPhoto", {
                    "chat_id": chat, "photo": deal.item.photo, "caption": text,
                    "parse_mode": "HTML", "reply_markup": CLAIM_BUTTON,
                }, quiet=True)
            if not ok:
                ok = self.send_text(text, [self.migrations.get(chat, chat)], CLAIM_BUTTON)
            sent = ok or sent
        return sent
