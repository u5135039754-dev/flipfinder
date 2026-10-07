"""
Group features: topics, reminders, quiet hours, the weekly report and /sell listings.
State lives in deals.json (DealBook); everything here is called once per run.
"""

from __future__ import annotations

import html
import re
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")
QUIET_FROM, QUIET_UNTIL = dtime(0, 0), dtime(7, 30)
DAY = 86400

# which topic a search category posts in (topics learned with /topic, see commands.py)
TOPIC_FOR_GROUP = {"Guitars": "guitars", "Amps": "guitars", "Pedals": "guitars", "Added from Telegram": "guitars",
                   "Audio": "electronics", "Electronics": "electronics", "Budget": "budget"}
TOPIC_NAMES = {"guitars": "Guitars", "electronics": "Electronics", "budget": "Budget", "summary": "Summary"}

# Pinned intro per topic ("general" = the main chat), posted when a topic is set with /topic
# and again with /intro; an intro that's already there is edited, not posted twice
INTROS = {
    "guitars": "\n".join([
        "🎸 <b>Guitars</b>",
        "Underpriced guitars, amps, pedals and soundbars land here.",
        "Tap ✋ Claim if you're on it → 💸 Bought → 🏷 Listed → ✅ Sold. First claim wins 🏃",
        "👍/👎 tells the bot if a deal was any good, 📩 gives you a ready Italian message for the seller.",
    ]),
    "electronics": "\n".join([
        "📱 <b>Electronics</b>",
        "Phones, AirPods, iPads, consoles and GPUs land here.",
        "Before you pay: 🔋 battery health, 🔓 iCloud/account unlocked, 🔍 no cracks, 🎥 ask for a video of it working.",
        "No video, no deal 😉",
    ]),
    "budget": "\n".join([
        "💰 <b>Budget</b>",
        "Cheap flips that fit our budget: pedals, calculators, cameras, DS, Kindles, controllers.",
        "/budget shows or changes the limit, /pot shows how much money we have.",
        "Small buys, quick flips 🔁",
    ]),
    "summary": "\n".join([
        "📊 <b>Summary</b>",
        "Daily summary, weekly report (Sunday 20:00), pot updates and failure alerts land here.",
        "/status is the bot running · /stock what we own · /profit how we're doing · /pot our money",
    ]),
    "general": "\n".join([
        "💸 <b>Welcome to FLIP MAFIA</b>",
        "This chat is just for talking. Deals go to their topics: Guitars, Electronics, Budget and Summary.",
        "/help lists everything the bot can do.",
    ]),
}


def now_rome(now: datetime | None = None) -> datetime:
    return (now or datetime.now(ROME)).astimezone(ROME)


def in_quiet_hours(now: datetime | None = None) -> bool:
    """No deal alerts 00:00-07:30 Italy time."""
    t = now_rome(now).time()
    return QUIET_FROM <= t < QUIET_UNTIL


def mention(name: str, user_id) -> str:
    """Clickable mention that works without a @username."""
    return f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>' if user_id else html.escape(name)


# --- reminders ---------------------------------------------------------------------

def due_reminders(book, now: float | None = None) -> list[tuple[str, str]]:
    """(kind, deal key) for: ping after 24 h claimed, release after 48 h, list nudge, price cut."""
    now = now or time.time()
    out = []
    for key, d in book.data["deals"].items():
        st = d["status"]
        if st == "claimed":
            since = max(d.get("claimed_at") or 0, d.get("kept_at") or 0)
            if now - since >= 2 * DAY:
                out.append(("release", key))
            elif now - since >= DAY and (d.get("pinged_at") or 0) < since:
                out.append(("ping", key))
        elif st == "bought" and now - (d.get("bought_at") or now) >= 3 * DAY \
                and now - (d.get("nudged_at") or 0) >= 3 * DAY:
            out.append(("list", key))
        elif st == "listed" and now - (d.get("listed_at") or now) >= 14 * DAY \
                and now - (d.get("cut_at") or 0) >= 7 * DAY:
            out.append(("cut", key))
    return out


def nice_price(x: float) -> float:
    """Prices people actually ask: €75, €120, €19."""
    if x >= 20:
        return float(max(5, round(x / 5) * 5))
    return float(max(1, round(x)))


def prices_for(d: dict, value_now: float | None) -> tuple[float, float, float]:
    """(market value used, suggested price, quick-sale price)."""
    value = value_now or d.get("value") or 0
    suggested = nice_price(value)
    low = d.get("low")
    quick = nice_price(min(value * 0.85, low) if low else value * 0.85)
    if quick >= suggested:
        quick = nice_price(suggested * 0.85)
    return value, suggested, quick


# --- /sell -------------------------------------------------------------------------

_EMOJI = re.compile(r"[^\w\s\-\+\.,'/&()|:%€]", re.UNICODE)


def clean_title(title: str, limit: int = 60) -> str:
    t = re.sub(r"\s+", " ", _EMOJI.sub(" ", title)).strip(" -–|")
    if len(t) <= limit:
        return t
    cut = t[:limit + 1].rsplit(" ", 1)[0]
    return cut[:limit].rstrip(" -–,|")


CONDITION_IT = {"new": "Nuovo", "nuovo": "Nuovo", "ottime": "Ottime", "buone": "Buone", "discrete": "Discrete"}
TEMPLATES = {
    "it": ("<b>Titolo</b>\n<code>{title}</code>\n\n<b>Descrizione</b>\n<code>{title}.\n"
           "Condizioni: {cond}. Testato e funzionante [conferma prima di pubblicare].\n"
           "[Aggiungi eventuali segni d'uso o difetti, e cosa è incluso.]\n"
           "Spedizione rapida con imballaggio accurato, oppure ritiro a mano a Hometown.</code>\n\n"
           "💶 Prezzo consigliato: <b>€{price:g}</b> · vendita veloce: <b>€{quick:g}</b>"),
    "en": ("<b>Title</b>\n<code>{title}</code>\n\n<b>Description</b>\n<code>{title}.\n"
           "Condition: {cond}. Tested and working [confirm before posting].\n"
           "[Add any signs of wear or faults, and what's included.]\n"
           "Fast shipping, carefully packed, or pickup in Hometown.</code>\n\n"
           "💶 Suggested price: <b>€{price:g}</b> · quick sale: <b>€{quick:g}</b>"),
    "uk": ("<b>Назва</b>\n<code>{title}</code>\n\n<b>Опис</b>\n<code>{title}.\n"
           "Стан: {cond}. Перевірено, працює [підтвердіть перед публікацією].\n"
           "[Додайте сліди використання чи дефекти, і що входить у комплект.]\n"
           "Швидка доставка з надійним пакуванням або самовивіз у місто.</code>\n\n"
           "💶 Рекомендована ціна: <b>€{price:g}</b> · швидкий продаж: <b>€{quick:g}</b>"),
}


def sell_listing(d: dict, value_now: float | None, lang: str = "it") -> str:
    value, price, quick = prices_for(d, value_now)
    cond = d.get("condition") or "[indica le condizioni]"
    if lang != "it":
        cond = {"Nuovo con cartellino": "new with tags", "Nuovo senza cartellino": "new without tags",
                "Ottime": "very good", "Buone": "good", "Discrete": "fair"}.get(cond, cond) if lang == "en" else \
               {"Nuovo con cartellino": "новий з біркою", "Nuovo senza cartellino": "новий без бірки",
                "Ottime": "дуже добрий", "Buone": "добрий", "Discrete": "задовільний"}.get(cond, cond)
    title = clean_title(d.get("title", ""))
    body = TEMPLATES[lang].format(title=html.escape(title), cond=html.escape(cond), price=price, quick=quick)
    basis = (f"\n\n<i>Market value €{value:,.0f} ({'current comparables' if value_now else 'when the deal was found'})"
             + (f", paid €{d['paid']:,.2f}" if d.get("paid") is not None else "") + "</i>")
    return f"🏷 <b>Listing for #{d.get('n', '?')}</b>\n\n" + body + basis


# --- weekly report ------------------------------------------------------------------

def weekly_due(book, now: datetime | None = None) -> bool:
    n = now_rome(now)
    return n.weekday() == 6 and n.hour >= 20 and book.data.get("last_weekly") != n.date().isoformat()


def weekly_report(book, now: datetime | None = None) -> str:
    n = now_rome(now)
    since = (n - timedelta(days=7)).timestamp()
    deals = book.data["deals"].values()
    found = sum(1 for d in deals if (d.get("sent") or 0) >= since)
    claimed = sum(1 for d in deals if (d.get("claimed_at") or 0) >= since)
    bought = sum(1 for d in deals if (d.get("bought_at") or 0) >= since)
    sold = [d for d in deals if d["status"] == "sold" and (d.get("sold_at") or 0) >= since]
    people: dict[str, float] = {}
    for d in sold:
        people[d["who"]] = people.get(d["who"], 0) + d["sold_for"] - (d.get("paid") or 0)
    lines = [f"🗓 <b>flipFinder · week to {n:%a %d %b}</b>",
             f"Deals found: {found} · claimed: {claimed} · bought: {bought} · sold: {len(sold)}"]
    if people:
        lines.append("Profit: " + " · ".join(f"{html.escape(w)} €{p:,.2f}" for w, p in
                                              sorted(people.items(), key=lambda x: -x[1]))
                     + f" · total €{sum(people.values()):,.2f}")
    else:
        lines.append("Profit: nothing sold this week")
    if sold:
        best = max(sold, key=lambda d: d["sold_for"] - (d.get("paid") or 0))
        lines.append(f'🏆 Best flip: <a href="{html.escape(best["url"])}">{html.escape(best["title"][:50])}</a> '
                     f"by {html.escape(best['who'])}: €{best.get('paid') or 0:,.2f} → €{best['sold_for']:,.2f} "
                     f"(+€{best['sold_for'] - (best.get('paid') or 0):,.2f})")
    downs: dict[str, int] = {}
    for f in book.data.get("feedback", []):
        if (f.get("at") or 0) >= since:
            q = f.get("query") or book.data["deals"].get(f.get("key"), {}).get("query") or "(unknown search)"
            downs[q] = downs.get(q, 0) + 1
    if downs:
        q, c = max(downs.items(), key=lambda x: x[1])
        lines.append(f"👎 Most down-voted search: <b>{html.escape(q)}</b> ({c}×) – worth tuning")
    else:
        lines.append("👎 No down-votes this week")
    return "\n".join(lines)
