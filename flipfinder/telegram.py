"""Sends deals to a Telegram chat through a bot."""

from __future__ import annotations

import html
import logging

import requests

from .analyzer import Deal

log = logging.getLogger(__name__)

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
    if deal.pickup_only:
        lines.append("📍 <b>Pickup only</b>" + (f" · {html.escape(deal.city)}" if deal.city else ""))
    if deal.pickup_only:
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
        f"💰 Possible profit: <b>{money(deal.profit, c)}</b>",
        f"📈 Percentage: <b>+{deal.roi:.0f}%</b>",
        f"{stars(deal.rating)} Rating: <b>{deal.rating}/10</b>",
        "",
        f'<a href="{html.escape(it.url)}">Open on Vinted</a>',
    ]
    return "\n".join(lines)


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id

    def _post(self, method: str, payload: dict) -> bool:
        try:
            r = requests.post(f"{self.base}/{method}", json=payload, timeout=20)
            if r.ok:
                return True
            log.error("Telegram %s failed: %s %s", method, r.status_code, r.text[:200])
        except requests.RequestException as e:
            log.error("Telegram %s error: %s", method, e)
        return False

    def send_text(self, text: str) -> bool:
        return self._post("sendMessage", {
            "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": False,
        })

    def send_deal(self, deal: Deal) -> bool:
        text = format_deal(deal)
        if deal.item.photo:
            ok = self._post("sendPhoto", {
                "chat_id": self.chat_id, "photo": deal.item.photo,
                "caption": text, "parse_mode": "HTML",
            })
            if ok:
                return True
        return self.send_text(text)
