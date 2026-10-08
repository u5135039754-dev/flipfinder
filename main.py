"""
flipFinder entry point.

  python main.py                 run forever, scan every interval_minutes
  python main.py --once          one scan then exit (used by GitHub Actions)
  python main.py --dry-run       print deals in the terminal, don't send them
  python main.py --test-telegram send a sample message to check the Worker and the bot work
  python main.py --fast          one fast-lane pass: newest listings of the top searches (see fastlane.py)

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
from flipfinder import fastlane
from flipfinder.analyzer import Deal, market_value
from flipfinder.cloud import Cloud
from flipfinder.health import FAIL_ALERT_AFTER, RunStats, miss_record
from flipfinder.scanner import Scanner, _key, describe_miss
from flipfinder.settings import apply_area, apply_settings, effective_budget, search_group, set_budget
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


def fast_pass(cloud: Cloud | None, args) -> int:
    """One fast-lane pass. Never fails the workflow for Vinted trouble: it reports and backs off."""
    started = time.time()
    state = cloud.state() if cloud else None
    fs = fastlane.settings(state)
    last = (state or {}).get("fast") or {}
    if args.force:
        fs = {**fs, "enabled": True}
    run, why = fastlane.due(fs, started, last.get("last_started", 0))
    if not run and not args.force:
        logging.info("Fast lane: skipped, %s", why)
        if cloud and fs["enabled"] and not why.startswith("quiet"):
            cloud.fast_report({"skipped": True, "reason": why[:60]})   # counted in the owner's weekly line
        return 0
    cfg = prepare(config_mod.load(args.config), state)
    queries = fastlane.this_pass(cfg, fs, search_group, last.get("passes", 0))
    scanner = Scanner(cfg)
    deals = scanner.fast_scan(queries)
    sent_keys = []
    for deal in deals[:MAX_ALERTS_PER_SCAN]:
        if args.dry_run:
            print("\n" + format_deal(deal) + "\n")
            continue
        if cloud.send_deal(deal, format_deal(deal)) == "sent":
            sent_keys.append(deal.item.key)
    secs = round(time.time() - started, 1)
    vinted = scanner.client
    logging.info("Fast lane: %d searches, %d Vinted page loads, %d blocked (403/429), %d new listing(s) checked, "
                 "%d deal(s), %.1fs", len(queries), vinted.requests, vinted.blocked, scanner.checked, len(deals), secs)
    if cloud and not args.dry_run:
        if sent_keys:
            cloud.analyze(sent_keys)
        cloud.fast_report({"started": started, "secs": secs, "requests": vinted.requests, "blocked": vinted.blocked,
                           "searches": len(queries), "checked": scanner.checked, "deals": len(deals), "sent": len(sent_keys)})
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="flipFinder - flip alerts")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--test-telegram", action="store_true")
    p.add_argument("--fast", action="store_true", help="one fast-lane pass")
    p.add_argument("--force", action="store_true", help="with --fast: run even if it isn't due")
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

    if args.fast:
        return fast_pass(cloud, args)

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
        sent_keys = []
        for deal in deals[:MAX_ALERTS_PER_SCAN]:
            if args.dry_run:
                print("\n" + format_deal(deal) + "\n" + explain(deal) + "\n")
                continue
            result = cloud.send_deal(deal, format_deal(deal))
            sent += result == "sent"
            queued += result == "queued"
            if result == "sent":
                sent_keys.append(deal.item.key)
        if cloud and not args.dry_run:
            cloud.analyze(sent_keys)   # nothing happens unless the owner turned on /ai auto
        if queued:
            logging.info("Quiet hours: %d deal(s) queued for 07:30", queued)
        if cloud and not args.dry_run and scanner.demand_due():
            cloud.put_demand(scanner.demand_table())       # for /demand in Telegram
            scanner.sold.save()
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
        stats.record_run(scanner.checked, sent, miss_record(*best) if best else None,
                         getattr(scanner, "seller_skipped", 0))
    elif stats.record_failure():
        notify.append(f"⚠️ <b>flipFinder: the last {FAIL_ALERT_AFTER} runs failed.</b>\n"
                      f"Latest: {html.escape(error)}\nYou'll get a message when it works again.")
    notify += getattr(scanner, "notices", [])   # e.g. sold-price checks lowered after Vinted blocks
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
