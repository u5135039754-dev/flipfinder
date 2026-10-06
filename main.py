"""
flipFinder entry point.

  python main.py                 run forever, scan every interval_minutes
  python main.py --once          one scan then exit (used by GitHub Actions)
  python main.py --dry-run       print deals in the terminal, don't send to Telegram
  python main.py --test-telegram send a sample message to check your bot works
"""

from __future__ import annotations

import argparse
import html
import logging
import sys
import time

from flipfinder import config as config_mod
from flipfinder.commands import OWNER_ID, Commands, effective_budget, load_settings, save_settings, set_budget
from flipfinder.dealbook import DealBook
from flipfinder.analyzer import Deal
from flipfinder.health import FAIL_ALERT_AFTER, RunStats, miss_record
from flipfinder.scanner import Scanner, describe_miss
from flipfinder.telegram import Telegram, format_deal
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


def main() -> int:
    p = argparse.ArgumentParser(description="flipFinder - Vinted flip alerts")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--test-telegram", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    cfg = config_mod.load(args.config)

    tg = None
    if not args.dry_run:
        if not (cfg.telegram_token and cfg.telegram_chat_id):
            print("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. Put them in .env "
                  "or use --dry-run.", file=sys.stderr)
            return 1
        tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id)

    if args.test_telegram:
        if tg is None:
            print(format_deal(sample_deal()))
            return 0
        ok = tg.send_deal(sample_deal())
        print("Sent, check Telegram." if ok else "Failed, see the error above.")
        return 0 if ok else 1

    scanner = Scanner(cfg)
    # dry runs don't touch the stats, so testing locally doesn't skew the daily summary
    stats = RunStats(cfg.seen_file.parent / "stats.json") if tg else None
    book = DealBook() if tg else None
    while True:
        if tg:
            # Telegram commands (settings.json) first, so this scan already uses them
            try:
                if Commands(tg, cfg, load_settings(), cfg.seen_file.parent / "stats.json", book).run():
                    cfg = config_mod.load(args.config)
                    scanner = Scanner(cfg)
            except Exception:
                logging.exception("Telegram commands failed")
            # budget mode: the /budget value, but never more than the shared pool
            set_budget(cfg, effective_budget(cfg.budget_setting, book.pool))
        try:
            deals = scanner.scan()
            ok = not scanner.failed
            error = "every Vinted search failed (blocked or site changed?), see the Actions log"
        except Exception as e:
            logging.exception("Scan crashed")
            deals, ok, error = [], False, f"the scan crashed: {type(e).__name__}: {e}"
        logging.info("Found %d deal(s)", len(deals))
        sent = 0
        for deal in deals[:MAX_ALERTS_PER_SCAN]:
            if tg:
                text = format_deal(deal)
                book.record(deal, text, [])
                where = tg.send_deal(deal, book.keyboard(deal.item.key), text)
                book.record(deal, text, where)
                sent += bool(where)
            else:
                print("\n" + format_deal(deal) + "\n" + explain(deal) + "\n")
        if stats is None and scanner.near_misses:
            print("Closest near misses:")
            for query, m in scanner.near_misses[:5]:
                print("  " + describe_miss(query, m))
        if stats is not None:
            report(stats, tg, ok, error, scanner, sent)
        if tg and tg.migrations:
            remember_migrations(tg)
        if book:
            book.save()
        if args.once:
            return 0 if ok else 1
        time.sleep(cfg.interval_minutes * 60)


def remember_migrations(tg: Telegram):
    """A group became a supergroup: keep its new id (settings.json) and tell the owner."""
    settings = load_settings()
    settings.setdefault("chat_migrations", {}).update(tg.migrations)
    save_settings(settings)
    for old, new in tg.migrations.items():
        tg.send_text(f"ℹ️ Your Telegram group was upgraded to a supergroup, so its chat id changed from "
                     f"<code>{old}</code> to <code>{new}</code>. flipFinder switched to the new one "
                     f"(saved in settings.json); update TELEGRAM_CHAT_ID when convenient.", [str(OWNER_ID)])
    tg.migrations.clear()


def report(stats: RunStats, tg: Telegram, ok: bool, error: str, scanner: Scanner, sent: int):
    """Failure alerts and the daily summary."""
    if ok:
        streak = stats.record_success()
        if streak:
            tg.send_text(f"✅ flipFinder is working again after {streak} failed runs.")
        best = scanner.near_misses[0] if scanner.near_misses else None
        stats.record_run(scanner.checked, sent, miss_record(*best) if best else None)
    elif stats.record_failure():
        tg.send_text(f"⚠️ <b>flipFinder: the last {FAIL_ALERT_AFTER} runs failed.</b>\n"
                     f"Latest: {html.escape(error)}\n"
                     "You'll get a message when it works again.")
    if stats.summary_due():
        if tg.send_text(stats.summary_text(scanner.cfg.rules)):
            stats.mark_summary_sent()
    stats.save()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
