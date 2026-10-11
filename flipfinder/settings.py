"""
Settings changed from Telegram (/setprice, /add, /budget, /setrule, on/off buttons...) over
config.yaml. The Cloudflare Worker keeps them in private storage; each run reads them.
"""

from __future__ import annotations

RULES = {   # name: (type, min, max), like /setrule allows
    "min_profit": (float, 0, 10_000),
    "min_roi": (float, 0, 1_000),
    "min_rating": (int, 1, 10),
    "max_roi": (float, 1, 10_000),
    "max_price": (float, 1, 10_000),
}


def search_group(s) -> str:
    """Category shown by /categories (config `group:` wins); also picks the group topic."""
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


PLATFORMS = ("vinted", "ebay", "subito")


def search_extras(s, a: dict):
    """The app's extras for a search: platforms next to Vinted, must-have words, words to exclude, size."""
    if isinstance(a.get("platforms"), list):
        s.platforms = [p for p in a["platforms"] if p in PLATFORMS]
        s.vinted_only = s.vinted_only and not ({"ebay", "subito"} & set(s.platforms))
    s.must_words = [str(w) for w in a.get("must") or [] if str(w).strip()][:10]
    s.exclude_keywords = list(dict.fromkeys(s.exclude_keywords + [str(w) for w in a.get("exclude") or [] if str(w).strip()][:20]))
    s.size = str(a.get("size") or "")[:10]


def search_from_spec(cfg, spec: dict):
    """A search from the app's form (for a preview), built like a saved one: its topic's group, rules and extras."""
    from .config import Search, clothes_search
    group = str(spec.get("group") or "")
    if group == "Clothes":
        s = clothes_search(cfg.clothes, spec["query"], 0, added=True)
        if spec.get("price_from") is not None or spec.get("price_to") is not None:
            s.price_from, s.price_to = spec.get("price_from"), spec.get("price_to")
    else:
        s = Search(spec["query"], spec.get("price_from"), spec.get("price_to"), added=True,
                   group="" if group in ("", "Added from Telegram") else group, budget=group == "Budget")
        if s.budget and (s.price_to is None or s.price_to > cfg.budget):
            s.price_to = cfg.budget
    search_extras(s, spec)
    return s


def apply_settings(cfg, settings: dict):
    """Telegram settings over config.yaml: removed/added searches, on/off, prices, budget, rules."""
    from .config import Search, clothes_search
    removed = {q.lower() for q in settings.get("removed", [])}
    cfg.searches = [s for s in cfg.searches if s.query.lower() not in removed]
    existing = {s.query.lower() for s in cfg.searches}
    n_clothes = sum(1 for s in cfg.searches if getattr(s, "clothes", False))
    for a in settings.get("added", []):
        if a["query"].lower() in existing:
            continue
        group = str(a.get("group") or "")
        if a.get("clothes") or group == "Clothes":   # /add clothes <brand>, or the app's Clothes card
            s = clothes_search(cfg.clothes, a["query"], n_clothes, added=True)
            n_clothes += 1
            if a.get("price_from") is not None or a.get("price_to") is not None:
                s.price_from, s.price_to = a.get("price_from"), a.get("price_to")
        else:   # /add (Added from Telegram) or the app (its topic's group: Guitars, Electronics, Budget...)
            s = Search(a["query"], a.get("price_from"), a.get("price_to"), added=True,
                       group="" if group in ("", "Added from Telegram") else group, budget=group == "Budget")
        cfg.searches.append(s)
        existing.add(s.query.lower())
        search_extras(s, a)
    overrides = {k.lower(): v for k, v in (settings.get("overrides") or {}).items()}
    for s in cfg.searches:   # the app's changes to config.yaml searches (platforms, words, size)
        if s.query.lower() in overrides:
            search_extras(s, overrides[s.query.lower()])
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
    cfg.rules.repairs = settings.get("repairs", True) is not False   # /repairs on|off (on unless turned off)
    cfg.rules.abroad = settings.get("abroad") is True                  # /abroad on|off (Italy only unless turned on)
    # applied from a report's suggestions (✅ Apply): music minimums, and values per category or search
    cfg.music_rules = {**cfg.music_rules, **{k: float(v) for k, v in (settings.get("music_rules") or {}).items()
                                             if k in ("min_profit", "min_roi", "max_price")}}
    if getattr(cfg, "budget_rules", None) is not None:
        cfg.budget_rules = {**cfg.budget_rules, **{k: float(v) for k, v in (settings.get("budget_rules") or {}).items()
                                                   if k in ("min_profit", "min_roi")}}
    # /setrule clothes <name> <value>
    if getattr(cfg, "clothes", None) is not None:
        cfg.clothes.rules = {**cfg.clothes.rules, **{k: (int(v) if k == "min_rating" else float(v))
                                                     for k, v in (settings.get("clothes_rules") or {}).items()
                                                     if k in ("min_profit", "min_roi", "min_rating", "max_price")}}
    cfg.value_adjust = {str(k).lower(): float(v) for k, v in (settings.get("value_adjust") or {}).items() if 0.5 <= float(v) <= 1.5}
    return cfg


def apply_area(cfg, area: dict | None):
    """
    The home area for Subito (private, from the Worker): where we pick things up and how far.
    Without it Subito is off for the run.
    """
    sb = cfg.subito
    if not area or not area.get("center"):
        sb.enabled = False
        return cfg
    sb.region = int(area["region"])
    sb.province = int(area["province"])
    sb.center = tuple(float(x) for x in area["center"])
    sb.radius_km = float(area.get("radius_km", sb.radius_km))
    sb.travel_cost = float(area.get("travel_cost", sb.travel_cost))
    sb.town_travel_costs = {str(k): float(v) for k, v in (area.get("town_travel_costs") or {}).items()}
    music = area.get("music") or {}   # guitars and amps: picked up from further away, the cost by distance
    sb.wide_regions = [int(r) for r in music.get("regions") or []]
    sb.wide_radius_km = float(music.get("radius_km") or 0)
    sb.tiers = [[float(k), float(c)] for k, c in music.get("tiers") or []]
    return cfg
