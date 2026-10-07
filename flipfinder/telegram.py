"""Deal alert text (HTML for Telegram). Sending is done by the Cloudflare Worker."""

from __future__ import annotations

import html
import logging

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
