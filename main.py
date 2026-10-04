"""
flipFinder entry point.

  python main.py                 run forever, scan every interval_minutes
  python main.py --once          one scan then exit (used by GitHub Actions)
  python main.py --dry-run       print deals in the terminal, don't send to Telegram
  python main.py --test-telegram send a sample message to check your bot works
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from flipfinder import config as config_mod
from flipfinder.analyzer import Deal
from flipfinder.scanner import Scanner
from flipfinder.telegram import Telegram, format_deal
from flipfinder.vinted import Item

MAX_ALERTS_PER_SCAN = 15


def sample_deal() -> Deal:
    item = Item(id=1, title="Nike Dunk Low Panda (test message)", price=45.0,
                total_price=47.95, currency="EUR", url="https://www.vinted.it/",
                brand="Nike", size="42", condition="Very good")
    return Deal(item, market_value=85.0, comparables=24, profit=37.05, roi=77.3, rating=8)


def explain(deal: Deal) -> str:
    """Why --dry-run thinks this is a deal: how comparables were picked, and a few of them."""
    lines = [f"   compared on {deal.basis}, {deal.comparables} listings, e.g.:"]
    lines += [f"     {c.price:8.2f}  {c.title[:70]}" for c in deal.sample]
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
    while True:
        deals = scanner.scan()
        logging.info("Found %d deal(s)", len(deals))
        for deal in deals[:MAX_ALERTS_PER_SCAN]:
            if tg:
                tg.send_deal(deal)
            else:
                print("\n" + format_deal(deal) + "\n" + explain(deal) + "\n")
        if args.once:
            return 0
        time.sleep(cfg.interval_minutes * 60)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
