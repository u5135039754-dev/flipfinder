"""
flipFinder entry point.

  python main.py                 run forever, scan every interval_minutes
  python main.py --once          one scan then exit (used by GitHub Actions)
  python main.py --dry-run       print deals in the terminal, don't send them
  python main.py --test-telegram send a sample message to check the Worker and the bot work

Settings changed from Telegram, deal tracking and the home area live in the Cloudflare
Worker's private storage (FLIPFINDER_API_URL / FLIPFINDER_API_KEY); the Worker also sends
every Telegram message. This program only searches.
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import sys
import time

from flipfinder import config as config_mod
from flipfinder.analyzer import Deal, market_value
from flipfinder.cloud import Cloud
from flipfinder.health import FAIL_ALERT_AFTER, RunStats, miss_record
from flipfinder.scanner import Scanner, _key, describe_miss
from flipfinder.settings import apply_area, apply_settings, effective_budget, set_budget
from flipfinder.telegram import format_deal
from flipfinder.vinted import Item

MAX_ALERTS_PER_SCAN = 15


def sample_deal() -> Deal:
    item = Item(id=1, title="Nike Dunk Low Panda (test message)", price=45.0,
                total_price=47.95, currency="EUR", url="https://www.vinted.it/",
                brand="Nike", size="42", condition="Very good")
    return Deal(item, market_value=85.0, comparables=24, profit=31.16, roi=59.0, rating=8,
                shipping=4.89, packaging=1.0)


def explain(deal: Deal) -> str:
    """Why --dry-run thinks this is a deal: how comparables were picked, and a few of them."""
    lines = [f"   compared on {deal.basis}, {deal.comparables} listings, e.g.:"]
    lines += [f"     {c.price:8.2f}  [{c.source[0].upper()}] {c.title[:66]}" for c in deal.sample]
    return "\n".join(lines)


def cloud_from_env() -> Cloud | None:
    url, key = os.getenv("FLIPFINDER_API_URL", ""), os.getenv("FLIPFINDER_API_KEY", "")
    return Cloud(url, key) if url and key else None


def prepare(cfg, state: dict | None):
    """config.yaml + Telegram settings + the private home area + the pool's cap on the budget."""
    state = state or {}
    apply_settings(cfg, state.get("settings") or {})
    apply_area(cfg, state.get("area"))
    set_budget(cfg, effective_budget(cfg.budget_setting, state.get("pool")))
    return cfg


def main() -> int:
    p = argparse.ArgumentParser(description="flipFinder - flip alerts")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--test-telegram", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    config_mod.load_dotenv()
    cloud = cloud_from_env()
    if cloud is None and not args.dry_run:
        print("Missing FLIPFINDER_API_URL / FLIPFINDER_API_KEY (the Cloudflare Worker). Put them in .env "
              "or use --dry-run.", file=sys.stderr)
        return 1

    if args.test_telegram:
        if args.dry_run:
            print(format_deal(sample_deal()))
            return 0
        res = cloud.report_run(notify=[{"text": format_deal(sample_deal()), "topic": "summary"}])
        ok = any(res.get("notified", []))
        print("Sent, check Telegram." if ok else "Failed, see the error above.")
        return 0 if ok else 1

    stats = None if args.dry_run else RunStats(config_mod.load(args.config).seen_file.parent / "stats.json")
    catalog_sent = False
    while True:
        cfg = config_mod.load(args.config)
        state = cloud.state() if cloud else None
        if cloud and not args.dry_run and not catalog_sent:
            cloud.put_catalog(cfg)           # before Telegram settings: the Worker applies them itself
            catalog_sent = True
        if cloud and state is None:
            logging.warning("No settings from the Worker this run: config.yaml only, Subito off")
        prepare(cfg, state)
        scanner = Scanner(cfg)
        try:
            deals = scanner.scan()
            ok = not scanner.failed
            error = "every Vinted search failed (blocked or site changed?), see the Actions log"
        except Exception as e:
            logging.exception("Scan crashed")
            deals, ok, error = [], False, f"the scan crashed: {type(e).__name__}: {e}"
        logging.info("Found %d deal(s)", len(deals))
        sent = queued = 0
        for deal in deals[:MAX_ALERTS_PER_SCAN]:
            if args.dry_run:
                print("\n" + format_deal(deal) + "\n" + explain(deal) + "\n")
                continue
            result = cloud.send_deal(deal, format_deal(deal))
            sent += result == "sent"
            queued += result == "queued"
        if queued:
            logging.info("Quiet hours: %d deal(s) queued for 07:30", queued)
        if args.dry_run and scanner.near_misses:
            print("Closest near misses:")
            for query, m in scanner.near_misses[:5]:
                print("  " + describe_miss(query, m))
        if stats is not None:
            values = {d["key"]: current_value(d, cfg, scanner) for d in (state or {}).get("open", [])}
            report(stats, cloud, ok, error, scanner, sent, {k: v for k, v in values.items() if v is not None})
        if args.once:
            return 0 if ok else 1
        time.sleep(cfg.interval_minutes * 60)


def current_value(d: dict, cfg, scanner: Scanner) -> float | None:
    """Market value of something we own, from the price pools we already have (no new requests)."""
    s = next((x for x in cfg.searches if x.query == d.get("query")), None)
    if s is None or not d.get("item"):
        return None
    pools = scanner.pools
    pool = (pools.get(_key(s), any_age=True) or []) + (pools.get("subito " + _key(s), any_age=True) or [])
    if scanner.ebay:
        pool += pools.get(f"ebay {scanner._ebay_category(s)} " + _key(s), any_age=True) or []
    try:
        value, _ = market_value(Item(**d["item"]), pool, cfg.rules.min_comparables, s.query)
    except (TypeError, ValueError):
        return None
    return value


def report(stats: RunStats, cloud: Cloud, ok: bool, error: str, scanner: Scanner, sent: int, values: dict):
    """Failure alerts and the daily summary (posted in Summary by the Worker), and /status numbers."""
    notify = []
    if ok:
        streak = stats.record_success()
        if streak:
            notify.append(f"✅ flipFinder is working again after {streak} failed runs.")
        best = scanner.near_misses[0] if scanner.near_misses else None
        stats.record_run(scanner.checked, sent, miss_record(*best) if best else None)
    elif stats.record_failure():
        notify.append(f"⚠️ <b>flipFinder: the last {FAIL_ALERT_AFTER} runs failed.</b>\n"
                      f"Latest: {html.escape(error)}\nYou'll get a message when it works again.")
    summary = stats.summary_text(scanner.cfg.rules) if stats.summary_due() else None
    d = stats.data
    status = {"last_run": d.get("last_run"), "runs": d["runs"], "checked": d["checked"], "deals_sent": d["deals_sent"]}
    res = cloud.report_run(status, values, [{"text": t, "topic": "summary"} for t in notify + [summary] if t])
    if summary and (res.get("notified") or [False])[-1]:
        stats.mark_summary_sent()
    # deals the Worker sent from the overnight queue count towards the next summary
    stats.data["deals_sent"] += int(res.get("flushed") or 0)
    stats.save()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
