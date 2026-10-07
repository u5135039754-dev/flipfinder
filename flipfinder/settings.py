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


def apply_settings(cfg, settings: dict):
    """Telegram settings over config.yaml: removed/added searches, on/off, prices, budget, rules."""
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
    return cfg
