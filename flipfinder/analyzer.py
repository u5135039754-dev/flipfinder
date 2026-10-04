"""Works out market value, profit and a 1-10 rating for a listing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from statistics import median

from .vinted import Item


@dataclass
class Deal:
    item: Item
    market_value: float      # "original price" = median of similar listings
    comparables: int         # how many listings that median is based on
    profit: float
    roi: float               # profit as % of what you pay
    rating: int              # 1-10


@dataclass
class Rules:
    min_profit: float = 10.0
    min_roi: float = 30.0
    min_rating: int = 6
    min_comparables: int = 8
    max_roi: float = 400.0          # anything cheaper than this is probably fake/broken/wrong item
    resell_costs: float = 0.0       # your own extra costs per flip (packaging, etc.)
    exclude_keywords: tuple[str, ...] = ()
    # rating knobs: profit/roi at which that part of the score maxes out
    rating_profit_cap: float = 50.0
    rating_roi_cap: float = 150.0
    rating_comparables_cap: int = 30


def is_excluded(item: Item, keywords) -> bool:
    title = item.title.lower()
    return any(re.search(rf"\b{re.escape(k.lower())}\b", title) for k in keywords)


def remove_outliers(prices: list[float]) -> list[float]:
    """Drop prices outside 1.5x IQR so a few silly listings don't move the median."""
    if len(prices) < 4:
        return prices
    s = sorted(prices)
    q1 = s[len(s) // 4]
    q3 = s[(3 * len(s)) // 4]
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return [p for p in s if lo <= p <= hi]


def market_value(item: Item, pool: list[Item], min_comparables: int) -> tuple[float | None, int]:
    """
    Median listed price of similar items. Only same-brand items count (a search like
    "fender stratocaster" also returns Squiers and Harley Bentons), and same-size
    items are preferred when there are enough.
    """
    others = [p for p in pool if p.id != item.id]
    if item.brand:
        brand = item.brand.lower()
        others = [p for p in others if p.brand.lower() == brand]
    if item.size:
        same_size = [p for p in others if p.size == item.size]
        if len(same_size) >= min_comparables:
            others = same_size
    prices = remove_outliers([p.price for p in others])
    if len(prices) < min_comparables:
        return None, len(prices)
    return round(median(prices), 2), len(prices)


def rate(profit: float, roi: float, comparables: int, rules: Rules) -> int:
    """
    Up to 4 points for ROI, 4 for absolute profit, 2 for how confident the
    market value is (more comparable listings = more trust).
    """
    roi_pts = min(max(roi, 0) / rules.rating_roi_cap, 1) * 4
    profit_pts = min(max(profit, 0) / rules.rating_profit_cap, 1) * 4
    conf_pts = min(comparables / rules.rating_comparables_cap, 1) * 2
    return max(1, min(10, round(roi_pts + profit_pts + conf_pts)))


def evaluate(item: Item, pool: list[Item], rules: Rules) -> Deal | None:
    """Returns a Deal if the item passes every rule, otherwise None."""
    if is_excluded(item, rules.exclude_keywords):
        return None
    value, n = market_value(item, pool, rules.min_comparables)
    if value is None:
        return None
    cost = item.total_price
    profit = round(value - cost - rules.resell_costs, 2)
    roi = round(profit / cost * 100, 1) if cost > 0 else 0.0
    if profit < rules.min_profit or roi < rules.min_roi or roi > rules.max_roi:
        return None
    rating = rate(profit, roi, n, rules)
    if rating < rules.min_rating:
        return None
    return Deal(item, value, n, profit, roi, rating)
