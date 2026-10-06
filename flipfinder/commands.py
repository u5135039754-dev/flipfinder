"""
Telegram commands: change searches, prices, budget and rules from Telegram.

At the start of each run the bot reads new messages and button taps (getUpdates),
applies them to settings.json and replies. settings.json overrides config.yaml and is
committed back to the repo by the workflow, so changes survive. Only the owner and
users they /allow can use commands; everyone else is ignored.
"""

from __future__ import annotations

import difflib
import hashlib
import html
import json
import logging
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

OWNER_ID = 1000001
SETTINGS_FILE = Path("settings.json")
COMMANDS_VERSION = 2      # bump when the list below changes, so it's registered again

COMMANDS = [
    ("help", "List all commands"),
    ("status", "Last run, runs today, listings checked, deals sent, platforms"),
    ("categories", "Searches by category, with buttons to turn them on or off"),
    ("prices", "Show a search's price range: /prices boss katana"),
    ("setprice", "Change a price range: /setprice \"boss katana\" 80 250"),
    ("budget", "Change the budget-mode budget: /budget 72"),
    ("rules", "Show the deal rules"),
    ("setrule", "Change a rule: /setrule min_roi 25"),
    ("add", "Add a search: /add \"zoom g1x four\" 20 60"),
    ("remove", "Remove a search: /remove zoom g1x four"),
    ("stock", "Items we own now (bought or listed), who has them, what we paid"),
    ("profit", "Profit in total, this month and per person"),
    ("pool", "Shared money: /pool shows it, /pool 300 sets the starting amount"),
    ("allow", "Owner only: let another user use commands: /allow 123456789"),
]
RULES = {   # name: (type, min, max) for /setrule
    "min_profit": (float, 0, 10_000),
    "min_roi": (float, 0, 1_000),
    "min_rating": (int, 1, 10),
    "max_roi": (float, 1, 10_000),
}


# --- settings.json --------------------------------------------------------------

def load_settings(path: Path = SETTINGS_FILE) -> dict:
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.error("settings.json is unreadable, ignoring it")
    for k, default in (("disabled", []), ("prices", {}), ("added", []), ("removed", []),
                       ("rules", {}), ("allowed_users", []), ("commands_version", 0)):
        data.setdefault(k, default)
    return data


def save_settings(settings: dict, path: Path = SETTINGS_FILE):
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def search_group(s) -> str:
    """Category shown by /categories (config `group:` wins)."""
    if getattr(s, "group", ""):
        return s.group
    if s.budget:
        return "Budget"
    cats = set(s.filters.get("catalog", []) or [])
    if cats & {3661, 3678, 3035, 3728, 3580, 3602, 3025}:
        return "Electronics"
    if cats & {4840}:
        return "Pedals"
    if s.subito_category == 11 or s.ebay_category == 14969:
        return "Audio"
    q = s.query.lower()
    if any(w in q for w in ("amplificatore", "katana", "mustang", "vox", "marshall")):
        return "Amps"
    if getattr(s, "added", False):
        return "Added from Telegram"
    return "Guitars"


def search_id(query: str) -> str:
    """Short stable id for button data (Telegram allows 64 bytes)."""
    return hashlib.sha1(query.lower().encode()).hexdigest()[:10]


# --- argument parsing --------------------------------------------------------------

def split_args(text: str) -> list[str]:
    text = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def query_and_range(args: list[str]) -> tuple[str, float, float]:
    """'boss katana 80 250' or '"boss katana" 80 250' -> ('boss katana', 80, 250)."""
    if len(args) < 3:
        raise ValueError("I need a search and two prices, e.g. /setprice \"boss katana\" 80 250")
    try:
        lo, hi = float(args[-2].replace(",", ".")), float(args[-1].replace(",", "."))
    except ValueError:
        raise ValueError(f"The prices must be numbers, I got {args[-2]!r} and {args[-1]!r}")
    if lo < 0 or hi < 0:
        raise ValueError("Prices can't be negative")
    if lo >= hi:
        raise ValueError(f"The minimum ({lo:g}) must be lower than the maximum ({hi:g})")
    return " ".join(args[:-2]).strip().lower(), lo, hi


# --- the bot -------------------------------------------------------------------------

class Commands:
    def __init__(self, tg, cfg, settings: dict, stats_path: Path | None = None, book=None):
        from .dealbook import DealBook
        self.tg, self.cfg, self.settings = tg, cfg, settings
        self.stats_path = stats_path
        self.book = book if book is not None else DealBook()
        self.changed = False

    # --- Telegram plumbing
    def _call(self, method: str, payload: dict):
        return self.tg.call(method, payload)

    def reply(self, chat_id, text: str, buttons: list[list[dict]] | None = None):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        self._call("sendMessage", payload)

    def allowed(self, user_id) -> bool:
        return user_id == OWNER_ID or user_id in self.settings["allowed_users"]

    def register_commands(self):
        if self.settings.get("commands_version") == COMMANDS_VERSION:
            return
        cmds = [{"command": c, "description": d} for c, d in COMMANDS]
        ok = all(self._call("setMyCommands", {"commands": cmds, "scope": {"type": scope}}) is not None
                 for scope in ("default", "all_group_chats"))
        if ok:
            self.settings["commands_version"] = COMMANDS_VERSION
            self.changed = True

    def run(self) -> bool:
        """Handle pending updates. True when settings changed (reload the config)."""
        self.register_commands()
        result = self._call("getUpdates", {"timeout": 0, "allowed_updates": ["message", "callback_query"]})
        updates = result or []
        last = None
        for u in updates:
            last = u["update_id"]
            try:
                if "callback_query" in u:
                    self.on_button(u["callback_query"])
                elif "message" in u:
                    self.on_message(u["message"])
            except Exception as e:   # one bad update mustn't stop the scan
                log.exception("Telegram update %s failed: %s", last, e)
        if last is not None:
            # confirms everything up to `last`, so it isn't delivered again next run
            self._call("getUpdates", {"offset": last + 1, "timeout": 0, "limit": 1})
            log.info("Telegram: handled %d update(s)%s", len(updates), ", settings changed" if self.changed else "")
        if self.changed:
            save_settings(self.settings)
        self.book.save()
        return self.changed

    # --- messages
    def on_message(self, msg: dict):
        text = (msg.get("text") or "").strip()
        user = (msg.get("from") or {}).get("id")
        chat = msg["chat"]["id"]
        if not self.allowed(user):
            return   # not someone we take commands from: ignore quietly
        if not text.startswith("/"):
            return self.on_price_reply(chat, user, text)
        head, _, rest = text.partition(" ")
        cmd = head[1:].split("@")[0].lower()
        handler = getattr(self, f"cmd_{cmd}", None)
        if handler is None:
            return self.reply(chat, f"I don't know /{html.escape(cmd)}. Try /help")
        try:
            handler(chat, split_args(rest), user)
        except ValueError as e:
            self.reply(chat, f"⚠️ {html.escape(str(e))}")

    def find(self, name: str):
        name = name.strip().strip('"').lower()
        for s in self.cfg.searches:
            if s.query.lower() == name:
                return s
        close = difflib.get_close_matches(name, [s.query for s in self.cfg.searches], n=3, cutoff=0.5)
        hint = f" Did you mean: {', '.join(close)}?" if close else " /categories lists them all."
        raise ValueError(f"No search called \"{name}\".{hint}")

    # --- commands
    def cmd_help(self, chat, args, user):
        lines = ["<b>flipFinder commands</b>", ""]
        lines += [f"/{c} – {html.escape(d)}" for c, d in COMMANDS if c != "allow" or user == OWNER_ID]
        lines += ["", "Changes apply at the start of the next run (every ~5 min)."]
        self.reply(chat, "\n".join(lines))

    def cmd_status(self, chat, args, user):
        st = {}
        if self.stats_path and self.stats_path.exists():
            try:
                st = json.loads(self.stats_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        last = st.get("last_run")
        last_txt = (datetime.fromtimestamp(last, timezone.utc).strftime("%d %b %H:%M UTC") if last else "unknown")
        on = sum(1 for s in self.cfg.searches if s.enabled)
        platforms = ["Vinted ✅", f"eBay {'✅' if self.cfg.ebay.enabled else '❌'}",
                     f"Subito {'✅ (' + format(self.cfg.subito.radius_km, 'g') + ' km)' if self.cfg.subito.enabled else '❌'}"]
        self.reply(chat, "\n".join([
            "📊 <b>flipFinder status</b>",
            f"Last run: {last_txt}",
            f"Since the last daily summary: {st.get('runs', 0):,} runs · {st.get('checked', 0):,} listings checked"
            f" · {st.get('deals_sent', 0):,} deals sent",
            f"Searches on: {on} of {len(self.cfg.searches)} · budget €{self.cfg.budget:g}",
            "Platforms: " + " · ".join(platforms),
        ]))

    def cmd_categories(self, chat, args, user):
        groups: dict[str, list] = {}
        for s in self.cfg.searches:
            groups.setdefault(search_group(s), []).append(s)
        for group, searches in groups.items():
            buttons = [[{"text": f"{'✅' if s.enabled else '❌'} {s.query}", "callback_data": f"t:{search_id(s.query)}"}]
                       for s in searches]
            on = sum(s.enabled for s in searches)
            self.reply(chat, f"<b>{html.escape(group)}</b> ({on}/{len(searches)} on) – tap to turn on/off", buttons)

    def cmd_prices(self, chat, args, user):
        if not args:
            raise ValueError("Which search? e.g. /prices boss katana")
        s = self.find(" ".join(args))
        rng = f"€{s.price_from:g}" if s.price_from is not None else "any"
        rng += f" – €{s.price_to:g}" if s.price_to is not None else " – no max"
        self.reply(chat, f"<b>{html.escape(s.query)}</b>: {rng}{' (budget mode)' if s.budget else ''}")

    def cmd_setprice(self, chat, args, user):
        name, lo, hi = query_and_range(args)
        s = self.find(name)
        if s.budget and hi > self.cfg.budget:
            raise ValueError(f"\"{s.query}\" is a budget search: the max can't be above the €{self.cfg.budget:g} budget")
        self.settings["prices"][s.query] = [lo, hi]
        s.price_from, s.price_to = lo, hi
        self.changed = True
        self.reply(chat, f"✅ <b>{html.escape(s.query)}</b>: €{lo:g} – €{hi:g}")

    def cmd_budget(self, chat, args, user):
        if len(args) != 1:
            raise ValueError("Give one amount, e.g. /budget 72")
        try:
            amount = float(args[0].replace("€", "").replace(",", "."))
        except ValueError:
            raise ValueError(f"The budget must be a number, I got {args[0]!r}")
        if not 1 <= amount <= 100_000:
            raise ValueError("The budget must be between €1 and €100,000")
        self.settings["budget"] = amount
        self.cfg.budget_setting = amount
        pool = self.book.pool
        set_budget(self.cfg, effective_budget(amount, pool))
        self.changed = True
        note = (f"\nℹ️ The pool is €{pool:,.2f}, so the limit in effect is €{self.cfg.budget:,.2f} "
                "(the smaller of the two)." if pool is not None and pool < amount else "")
        self.reply(chat, f"✅ Budget is now €{amount:g} (budget-mode searches only){note}")

    def cmd_rules(self, chat, args, user):
        r = self.cfg.rules
        br = self.cfg.budget_rules
        self.reply(chat, "\n".join([
            "<b>Deal rules</b>",
            f"min_profit: €{r.min_profit:g}", f"min_roi: {r.min_roi:g}%", f"min_rating: {r.min_rating}",
            f"max_roi: {r.max_roi:g}%",
            "", f"<i>Budget mode (€{self.cfg.budget:g}): min_profit €{br['min_profit']:g}, "
                f"min_roi {br['min_roi']:g}%, max_roi {br['max_roi']:g}%</i>",
        ]))

    def cmd_setrule(self, chat, args, user):
        if len(args) != 2:
            raise ValueError("Use /setrule <name> <value>, e.g. /setrule min_roi 25")
        name, raw = args[0].lower(), args[1].replace(",", ".").replace("%", "").replace("€", "")
        if name not in RULES:
            raise ValueError(f"Unknown rule {name!r}. Rules: {', '.join(RULES)}")
        typ, lo, hi = RULES[name]
        try:
            value = typ(float(raw)) if typ is int and float(raw).is_integer() else typ(raw)
        except ValueError:
            raise ValueError(f"{name} must be {'a whole number' if typ is int else 'a number'}, I got {args[1]!r}")
        if not lo <= value <= hi:
            raise ValueError(f"{name} must be between {lo} and {hi}")
        r = self.cfg.rules
        if name == "max_roi" and value <= r.min_roi or name == "min_roi" and value >= r.max_roi:
            raise ValueError("min_roi must stay below max_roi")
        self.settings["rules"][name] = value
        setattr(r, name, value)
        self.changed = True
        self.reply(chat, f"✅ {name} is now {value:g}")

    def cmd_add(self, chat, args, user):
        name, lo, hi = query_and_range(args)
        if not name:
            raise ValueError("The search can't be empty")
        if any(s.query.lower() == name for s in self.cfg.searches):
            raise ValueError(f"There's already a search called \"{name}\". Change it with /setprice")
        self.settings["added"] = [a for a in self.settings["added"] if a["query"] != name] + \
            [{"query": name, "price_from": lo, "price_to": hi}]
        self.settings["removed"] = [q for q in self.settings["removed"] if q != name]
        self.changed = True
        self.reply(chat, f"✅ Added <b>{html.escape(name)}</b> (€{lo:g} – €{hi:g}). Its current listings are "
                         "checked on the next run, then new ones alert as usual.")

    def cmd_remove(self, chat, args, user):
        if not args:
            raise ValueError("Which search? e.g. /remove boss katana")
        s = self.find(" ".join(args))
        sid = search_id(s.query)
        self.reply(chat, f"Remove <b>{html.escape(s.query)}</b>? You can /add it again later.",
                   [[{"text": "Yes, remove it", "callback_data": f"rm:{sid}:y"},
                     {"text": "No", "callback_data": f"rm:{sid}:n"}]])

    def cmd_allow(self, chat, args, user):
        if user != OWNER_ID:
            raise ValueError("Only the owner can use /allow")
        if len(args) != 1 or not args[0].isdigit():
            raise ValueError("Give a numeric Telegram user id, e.g. /allow 123456789")
        uid = int(args[0])
        if uid not in self.settings["allowed_users"] and uid != OWNER_ID:
            self.settings["allowed_users"].append(uid)
            self.changed = True
        self.reply(chat, f"✅ User {uid} can now use commands")

    # --- deal lifecycle: ✋ Claim -> 💸 Bought -> 🏷 Listed -> ✅ Sold
    def answer(self, cq: dict, text: str):
        self._call("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": text})

    def refresh_deal(self, key: str):
        """Edit every copy of the deal (private chat and group) to the current status."""
        d = self.book.data["deals"][key]
        text, kb = self.book.full_text(key), self.book.keyboard(key)
        for m in d.get("messages", []):
            if m.get("photo"):
                self._call("editMessageCaption", {"chat_id": m["chat"], "message_id": m["id"],
                                                  "caption": text[:1024], "parse_mode": "HTML", "reply_markup": kb})
            else:
                self._call("editMessageText", {"chat_id": m["chat"], "message_id": m["id"], "text": text,
                                               "parse_mode": "HTML", "reply_markup": kb})

    def on_deal_button(self, cq: dict, chat, user, kind: str, key: str):
        from .dealbook import SELLER_MESSAGE
        deals = self.book.data["deals"]
        d = deals.get(key)
        if d is None:
            return self.answer(cq, "I don't have this deal any more")
        who = claimer_name(cq.get("from") or {})
        if kind in ("up", "dn"):
            vote = "up" if kind == "up" else "down"
            d["votes"][str(user)] = vote
            if vote == "down":
                self.book.data["feedback"].append({"key": key, "title": d["title"], "url": d["url"], "by": who,
                                                   "at": time.time(), "value": d.get("value"),
                                                   "cost": d.get("cost")})
            self.book.changed = True
            self.refresh_deal(key)
            return self.answer(cq, "👍 Noted" if vote == "up" else "👎 Noted, it's logged to tune the filters")
        if kind == "m":
            text = SELLER_MESSAGE.format(title=d["title"])
            self.reply(chat, "📩 Copy and send this to the seller:\n\n<code>" + html.escape(text, quote=False) + "</code>\n\n"
                             f'<a href="{html.escape(d["url"])}">Open the listing</a>')
            return self.answer(cq, "Message ready below")
        if kind == "c":
            if d["status"] != "new":
                return self.answer(cq, f"{d.get('who', 'Someone')} already has this one")
            d.update(status="claimed", who=who, who_id=user, claimed_at=time.time())
            self.book.changed = True
            self.refresh_deal(key)
            return self.answer(cq, "It's yours, good luck!")
        # the next steps belong to whoever claimed it (or the owner)
        if user not in (d.get("who_id"), OWNER_ID):
            return self.answer(cq, f"{d.get('who', 'Someone else')} has this one")
        expected = {"b": "claimed", "l": "bought", "s": "listed"}[kind]
        if d["status"] != expected:
            return self.answer(cq, "That step is already done")
        if kind == "l":
            d.update(status="listed", listed_at=time.time())
            self.book.changed = True
            self.refresh_deal(key)
            return self.answer(cq, "🏷 Listed")
        ask = "paid" if kind == "b" else "sold_for"
        title = html.escape(d["title"][:60])
        cost = d.get("cost") if ask == "paid" else None
        if cost:
            # suggest what we know it costs; a different amount can still be typed as a reply
            q = (f"💸 How much did you pay for <b>{title}</b>?\n"
                 f"Known total (price + buyer fee + shipping/pickup): <b>€{cost:,.2f}</b>.\n"
                 "Tap ✅ to use it, or reply to this message with a different amount.")
            markup = {"inline_keyboard": [[{"text": f"✅ Use €{cost:,.2f}", "callback_data": f"pay:{key}"}]]}
        elif ask == "paid":
            q = f"💸 How much did you pay for <b>{title}</b>? Reply with the amount, e.g. 45"
            markup = {"force_reply": True, "input_field_placeholder": "amount in €"}
        else:
            q = f"✅ How much did <b>{title}</b> sell for? Reply with the amount, e.g. 80"
            markup = {"force_reply": True, "input_field_placeholder": "amount in €"}
        sent = self._call("sendMessage", {"chat_id": chat, "text": q, "parse_mode": "HTML", "reply_markup": markup})
        self.book.data["pending"][str(user)] = {
            "key": key, "ask": ask, "chat": chat,
            "ask_msg": sent.get("message_id") if isinstance(sent, dict) else None}
        self.book.changed = True
        return self.answer(cq, "Tap ✅ or reply with the price" if cost else "Reply with the price")

    def on_use_cost(self, cq: dict, chat, user, key: str):
        """✅ Use €X under the "how much did you pay?" question."""
        pending = self.book.data["pending"].get(str(user))
        d = self.book.data["deals"].get(key)
        if not pending or pending["key"] != key or pending["ask"] != "paid" or d is None:
            return self.answer(cq, f"That's for {d.get('who', 'someone else') if d else 'someone else'}")
        self.answer(cq, "Done")
        self.apply_price(chat, user, float(d["cost"]))

    def on_price_reply(self, chat, user, text: str):
        pending = self.book.data["pending"].get(str(user))
        if not pending or str(pending["chat"]) != str(chat):
            return   # just chatting
        raw = text.replace("€", "").replace(",", ".").strip()
        try:
            amount = float(raw)
        except ValueError:
            return self.reply(chat, f"⚠️ I need just the amount, e.g. 45 (got {html.escape(text[:30])!s})")
        if not 0 <= amount <= 100_000:
            return self.reply(chat, "⚠️ That amount doesn't look right")
        self.apply_price(chat, user, amount)

    def apply_price(self, chat, user, amount: float):
        pending = self.book.data["pending"].pop(str(user))
        key, ask = pending["key"], pending["ask"]
        d = self.book.data["deals"].get(key)
        self.book.changed = True
        if pending.get("ask_msg"):
            # the question is answered: drop its "✅ Use €X" button
            self._call("editMessageReplyMarkup", {"chat_id": pending["chat"], "message_id": pending["ask_msg"],
                                                  "reply_markup": {"inline_keyboard": []}})
        if d is None:
            return
        now = time.time()
        if ask == "paid":
            d.update(status="bought", paid=amount, bought_at=now)
            done = f"💸 Bought for €{amount:,.2f}"
        else:
            d.update(status="sold", sold_for=amount, sold_at=now)
            done = f"✅ Sold for €{amount:,.2f}, profit €{amount - (d.get('paid') or 0):,.2f}"
        pool = self.book.pool
        self.refresh_deal(key)
        self.reply(chat, done + (f" · pool now €{pool:,.2f}" if pool is not None else ""))

    def cmd_stock(self, chat, args, user):
        items = self.book.stock()
        if not items:
            return self.reply(chat, "📦 Nothing in stock right now")
        lines = ["📦 <b>In stock</b>"]
        for d in items:
            lines.append(f"{'💸' if d['status'] == 'bought' else '🏷'} <a href=\"{html.escape(d['url'])}\">"
                         f"{html.escape(d['title'][:50])}</a> · {html.escape(d['who'])} · paid €{(d.get('paid') or 0):,.2f}")
        lines.append(f"\nTied up: €{sum(d.get('paid') or 0 for d in items):,.2f}")
        self.reply(chat, "\n".join(lines))

    def cmd_profit(self, chat, args, user):
        p = self.book.profit()
        lines = ["💰 <b>Profit</b>", f"Total: €{p['total']:,.2f} ({p['sold']} sold)", f"This month: €{p['month']:,.2f}"]
        lines += [f"· {html.escape(who)}: €{v:,.2f}" for who, v in sorted(p["people"].items(), key=lambda x: -x[1])]
        pool = self.book.pool
        if pool is not None:
            lines.append(f"\nPool: €{pool:,.2f}")
        self.reply(chat, "\n".join(lines))

    def cmd_pool(self, chat, args, user):
        if not args:
            pool = self.book.pool
            setting = self.cfg.budget_setting
            if pool is None:
                return self.reply(chat, "No pool set (start one with /pool 300).\n"
                                        f"Budget-mode limit: €{setting:,.2f} (/budget)")
            return self.reply(chat, "\n".join([
                f"💶 Pool: €{pool:,.2f} (started with €{self.book.data['pool']['start']:,.2f})",
                f"🎯 /budget: €{setting:,.2f}",
                f"Budget-mode limit in effect: <b>€{effective_budget(setting, pool):,.2f}</b> (the smaller of the two)",
            ]))
        try:
            amount = float(args[0].replace("€", "").replace(",", "."))
        except ValueError:
            raise ValueError(f"The amount must be a number, I got {args[0]!r}")
        if not 0 <= amount <= 1_000_000:
            raise ValueError("The pool must be between €0 and €1,000,000")
        self.book.set_pool(amount)
        limit = effective_budget(self.cfg.budget_setting, amount)
        set_budget(self.cfg, limit)
        self.reply(chat, f"✅ Pool set to €{amount:,.2f}. 💸 Bought takes from it, ✅ Sold adds to it.\n"
                         f"Budget-mode limit: €{limit:,.2f} (the smaller of the pool and /budget "
                         f"€{self.cfg.budget_setting:,.2f})")

    # --- "I'm on it" on deals
    def on_claim(self, cq: dict, msg: dict, chat, data: str):
        """Anyone who can see the deal can claim it; the button then shows who did."""
        if data == "claimed":
            label = (((msg.get("reply_markup") or {}).get("inline_keyboard") or [[{}]])[0][0]).get("text", "")
            self._call("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": label or "Already claimed"})
            return
        who = claimer_name(cq.get("from") or {})
        when = datetime.now(timezone.utc).strftime("%H:%M UTC")
        self._call("editMessageReplyMarkup", {
            "chat_id": chat, "message_id": msg.get("message_id"),
            "reply_markup": {"inline_keyboard": [[{"text": f"✋ {who} is on it ({when})", "callback_data": "claimed"}]]},
        })
        self._call("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": "It's yours, good luck!"})
        log.info("Deal claimed by %s in chat %s", who, chat)

    # --- buttons
    def on_button(self, cq: dict):
        user = (cq.get("from") or {}).get("id")
        data = cq.get("data") or ""
        msg = cq.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        if not self.allowed(user):
            return   # only allowed users can press buttons
        if data in ("claim", "claimed"):
            return self.on_claim(cq, msg, chat, data)
        kind, _, key = data.partition(":")
        if kind == "pay":
            return self.on_use_cost(cq, chat, user, key)
        if kind in ("c", "b", "l", "s", "up", "dn", "m"):
            return self.on_deal_button(cq, chat, user, kind, key)
        by_id = {search_id(s.query): s for s in self.cfg.searches}
        kind, _, rest = data.partition(":")
        note = ""
        if kind == "t" and rest in by_id:
            s = by_id[rest]
            s.enabled = not s.enabled
            dis = set(self.settings["disabled"])
            dis.discard(s.query) if s.enabled else dis.add(s.query)
            self.settings["disabled"] = sorted(dis)
            self.changed = True
            note = f"{s.query}: {'on' if s.enabled else 'off'}"
            # refresh this category's buttons
            group = search_group(s)
            buttons = [[{"text": f"{'✅' if x.enabled else '❌'} {x.query}", "callback_data": f"t:{search_id(x.query)}"}]
                       for x in self.cfg.searches if search_group(x) == group]
            self._call("editMessageReplyMarkup", {"chat_id": chat, "message_id": msg.get("message_id"),
                                                  "reply_markup": {"inline_keyboard": buttons}})
        elif kind == "rm":
            sid, _, answer = rest.partition(":")
            s = by_id.get(sid)
            if s is None:
                note = "That search is already gone"
            elif answer == "y":
                self.settings["removed"] = sorted(set(self.settings["removed"]) | {s.query})
                self.settings["added"] = [a for a in self.settings["added"] if a["query"] != s.query]
                self.changed = True
                note = f"Removed {s.query}"
                self.cfg.searches = [x for x in self.cfg.searches if x is not s]
            else:
                note = "Kept it"
            self._call("editMessageText", {"chat_id": chat, "message_id": msg.get("message_id"),
                                           "text": ("🗑 " if answer == "y" and s else "") + note})
        else:
            note = "That button is out of date, send the command again"
        self._call("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": note})


def effective_budget(setting: float, pool: float | None) -> float:
    """Budget-mode limit: the /budget value, but never more than what's in the pool."""
    return setting if pool is None else min(setting, pool)


def set_budget(cfg, amount: float):
    """
    Budget-mode budget (from /budget, or the shared pool). Budget searches look up to it,
    in both directions, unless their max price was set by hand; they never look above it.
    """
    cfg.budget = max(0.0, amount)
    for s in cfg.searches:
        if s.budget and (s.price_to_is_budget or s.price_to is None or s.price_to > cfg.budget):
            s.price_to, s.price_to_is_budget = cfg.budget, s.price_to_is_budget or s.price_to is None


def claimer_name(user: dict) -> str:
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    return name or (f"@{user['username']}" if user.get("username") else f"user {user.get('id')}")


def apply_settings(cfg, settings: dict):
    """settings.json over config.yaml: removed/added searches, on/off, prices, budget, rules."""
    from .config import Search
    removed = {q.lower() for q in settings.get("removed", [])}
    cfg.searches = [s for s in cfg.searches if s.query.lower() not in removed]
    existing = {s.query.lower() for s in cfg.searches}
    for a in settings.get("added", []):
        if a["query"].lower() not in existing:
            cfg.searches.append(Search(a["query"], a.get("price_from"), a.get("price_to"), added=True))
    disabled = {q.lower() for q in settings.get("disabled", [])}
    prices = {k.lower(): v for k, v in settings.get("prices", {}).items()}
    for s in cfg.searches:
        s.enabled = s.query.lower() not in disabled
        if s.query.lower() in prices:
            s.price_from, s.price_to = prices[s.query.lower()]
            s.price_to_is_budget = False   # set by hand: the budget only caps it
    if "budget" in settings:
        cfg.budget_setting = float(settings["budget"])
        set_budget(cfg, cfg.budget_setting)
    for name, value in settings.get("rules", {}).items():
        if name in RULES:
            setattr(cfg.rules, name, RULES[name][0](value))
    # groups upgraded to supergroups get a new chat id (learned from Telegram, see main.py)
    moves = settings.get("chat_migrations", {})
    if moves and cfg.telegram_chat_id:
        ids = [moves.get(c.strip(), c.strip()) for c in cfg.telegram_chat_id.split(",") if c.strip()]
        cfg.telegram_chat_id = ",".join(ids)
    return cfg
