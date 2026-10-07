# flipFinder

Python 3.10+ app that finds underpriced Vinted listings and sends Telegram alerts.

## Layout
- `main.py` CLI entry (`--once`, `--dry-run`, `--test-telegram`, `-v`)
- `flipfinder/vinted.py` Vinted search client: loads the `/catalog` page and reads items from its embedded Next.js data (`/api/v2/catalog/items` is gone, 404)
- `flipfinder/analyzer.py` market value (median of comparables matched by query relevance, brand and model tokens from titles, IQR outlier removal, same-size preference), profit, ROI, 1-10 rating, filters
- `flipfinder/scanner.py` per-search scan, price pool cache in `data/pools.json`
- `flipfinder/ebay.py` eBay Browse API client (EBAY_IT, Buy It Now, app token), searched every 20 min, pools every 3 h (~3,050 of 5,000 calls/day), calls/day in `data/ebay.json`
- `flipfinder/subito.py` Subito app JSON API (hades.subito.it, app headers; the website is Akamai-blocked): new listings per category in the home province, kept within `radius_km`, pickup travel cost vs seller shipping
- `flipfinder/storage.py` seen items in `data/seen.json` as `platform:id` (14 day expiry)
- `flipfinder/telegram.py` message format + Bot API; several chats (TELEGRAM_CHAT_ID comma-separated), follows group->supergroup migrations, "I'm on it" claim button
- `flipfinder/dealbook.py` deal lifecycle (claim/bought/listed/sold), pool, 👍/👎 feedback in `deals.json` (committed by the workflow); pool = budget-mode budget when set
- `flipfinder/group.py` group topics (learned with /topic, stored in settings.json; pinned intro per topic, /intro reposts), reminders, quiet hours 00:00-07:30 Rome (queue in deals.json), weekly report, /sell listings
- `flipfinder/commands.py` Telegram commands (getUpdates at the start of each run), owner 1000001 + /allow list; changes go to `settings.json` (overrides config.yaml, committed by the workflow)
- `config.yaml` searches and rules; secrets in `.env` (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, optional EBAY_CLIENT_ID, EBAY_CLIENT_SECRET); Subito needs no keys (`subito: enabled`)
- `.github/workflows/flipfinder.yml` (shipped as `deploy/flipfinder.yml`, move it there) runs `main.py --once` every 5 min, `data/` persisted with actions/cache

## Commands
- `pytest` run tests
- `python main.py --dry-run --once -v` one scan, print instead of send

## Notes
- Keep `request_delay` >= 2s. Handle 401/403 by refreshing the cookie, 429 by backing off.
- Market value uses asking prices, not sold prices.

## Plan decisions (agreed with the owner, the owner; keep until done)
Working rules: the owner doesn't code. Build, test, dry run, show results in plain language, and ask before every push. Manual steps one at a time in simple words. Never ask for secrets in chat. Everything stays free (€0).

Order: Step 6 speed (done: 3 h pools, budget/guitar searches alternate) → topic intros + /intro → Steps 1+2 → 3 → 4 → 5.
1. Privacy: deals, settings, money data and the Hometown area (location, radius, towns) move to free private storage (Cloudflare D1 via a Worker; the scanner uses a secret API key). Remove them from the repo and from git history, add to .gitignore. Nothing private (names, amounts, chat IDs, tokens) in the public repo or logs.
2. Instant replies: Telegram commands and buttons handled by a Cloudflare Worker webhook. Remove all getUpdates polling from the scanner (a webhook forbids it). The scanner keeps searching and sending deals.
3. SHARED POT, not per-person money. the owner holds all the money in his own bank account as treasurer; the bot only records numbers.
   - /deposit <name> <amount> and /withdraw <name> <amount>: owner only.
   - Every buy is paid from the pot, every sale goes back into the pot.
   - Each member's contribution is tracked; profit is split by contribution share (equal now, €100 each), as a setting.
   - /pot: balance, money tied up in stock, total profit, and per member: contribution, profit share, what they'd get back if we stopped today.
   - Budget-mode limit = the smaller of /budget and the money in the pot.
   - Buys over €50 need a 👍 from another member before 💸 Bought works.
   - Every money action is posted in the group. Full history; corrections are new entries, never edits.
4. Telegram Mini App (Cloudflare Pages + the same Worker, initData verified server-side, allowed users only): tabs Deals, Stock, Pot (same info as /pot), Settings. Dark, phone-first.
5. Health check: healthchecks.io (period 5 min, grace 30 min), Telegram integration to the owner; ping on success, /fail on failure, setup-step failures included.
6. Keep runs under ~4 min; suggest trims and ask before changing.
- `outputs/` and `Claude outputs/` stay in .gitignore, never committed.
