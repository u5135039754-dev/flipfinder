# flipFinder

Python 3.10+ app that finds underpriced Vinted listings and sends Telegram alerts.

## Layout
- `main.py` CLI entry (`--once`, `--dry-run`, `--test-telegram`, `-v`)
- `flipfinder/vinted.py` Vinted search client: loads the `/catalog` page and reads items from its embedded Next.js data (`/api/v2/catalog/items` is gone, 404)
- `flipfinder/analyzer.py` market value (median of comparables matched by query relevance, brand and model tokens from titles, IQR outlier removal, same-size preference), profit, ROI, 1-10 rating, filters
- `flipfinder/scanner.py` per-search scan, price pool cache in `data/pools.json`
- `flipfinder/ebay.py` eBay Browse API client (EBAY_IT, Buy It Now, app token), searched every 20 min, pools every 3 h (~3,050 of 5,000 calls/day), calls/day in `data/ebay.json`
- `flipfinder/subito.py` Subito app JSON API (hades.subito.it, app headers; the website is Akamai-blocked): new listings per category in the home province, kept within `radius_km`, pickup travel cost vs seller shipping
- `flipfinder/sold.py` sold-price tracking: pool history, gone listings checked via item page (`can_buy:false` = sold, 404 = deleted), listing dates from id anchors, adaptive checks per run (halve on 403/429), demand (analyzer.demand, /demand table sent to the Worker every 30 min)
- seller trust: `vinted.parse_item_page` (feedback_count/reputation/badges/last seen) + `parse_profile_page` (/member/<id>: given_item_count = sold), eBay `ebay_seller`; `analyzer.seller_check` skips 0 reviews + 0 sold at ROI >= 100%, warns otherwise; `telegram.seller_lines`
- `flipfinder/storage.py` seen items in `data/seen.json` as `platform:id` (14 day expiry)
- `flipfinder/telegram.py` deal alert text only (the Worker sends)
- `flipfinder/cloud.py` client for the Worker API (FLIPFINDER_API_URL / FLIPFINDER_API_KEY): state (Telegram settings, home area, pool, owned deals), catalog, deals, run report
- `flipfinder/settings.py` Telegram settings and the private home area applied over config.yaml
- `worker/` Cloudflare Worker (JS, D1): shared pot ledger (`src/pot.js`, append-only `ledger` table, /deposit /withdraw /pot /ledger /fix /undo /split; buys are approved by a manager, see team), team (`src/team.js`: roles in settings.roles/people, duty/schedule/tasks/health in kv, pinned duty message, 🙋 Request buy -> manager ✅ Approve (replaced the €50 👍 rule), duty pings/escalation in the 5-min cron, active only once a role is set; real names only in D1, never in code), handbook (`src/handbook.js`: kv "handbook" sections pinned in the Rules topic, /handbook, owner /handbook edit; text only in D1), quiet hours until 08:00, Mini App redesign (theme colours, icon tab bar, ring meter + profit line using the validated palette), Crypto topic (`src/crypto.js`, own hourly cron `7 * * * *` dispatched in index.js: 18:00 Rome digest from CoinDesk/Cointelegraph RSS + CoinGecko prices/volume/trending, ±10% and 3× volume alerts, /watch /unwatch; read-only, never touches the pot), Mini App (`public/app/index.html` served as static assets at /app/, API `/app/api/*` in `src/webapp.js`, initData HMAC-checked, actions reuse Bot code via `bot.capture`), Telegram webhook (all commands/buttons, answers in ~1 s), scanner API, 5-min cron (overnight queue, reminders, weekly report), deal tracking, pinned topic intros. Secrets: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OWNER_ID, API_KEY, WEBHOOK_SECRET. Tests: `cd worker && npm test` (Node's built-in SQLite stands in for D1). `src/index.js` may only export handlers; logic is in `src/app.js`
- `config.yaml` searches and rules (public: no home area, no ids); secrets in `.env` (FLIPFINDER_API_URL, FLIPFINDER_API_KEY, optional EBAY_CLIENT_ID, EBAY_CLIENT_SECRET); Subito needs no keys (`subito: enabled`, area from the Worker)
- `.github/workflows/flipfinder.yml` runs `main.py --once` every 5 min (read-only, commits nothing), `data/` (seen items, price pools, run stats) persisted with actions/cache

## Commands
- `pytest` run tests; `cd worker && npm test` Worker tests
- `python main.py --dry-run --once -v` one scan, print instead of send

## Notes
- Keep `request_delay` >= 2s. Handle 401/403 by refreshing the cookie, 429 by backing off.
- Market value: asking prices, blended with likely-sold prices once 5+ sold comparables exist (sold.py).

## Plan decisions (agreed with the owner; keep until done)
Working rules: the owner doesn't code. Build, test, dry run, show results in plain language, and ask before every push. Manual steps one at a time in simple words. Never ask for secrets in chat. Everything stays free (€0).

Order: Step 6 speed (done: 3 h pools, budget/guitar searches alternate) → topic intros + /intro (done) → Steps 1+2 (done: Worker live, history scrubbed) → 3 (done: shared pot, worker/src/pot.js) → 4 (Mini App built: worker/public/app/index.html + src/webapp.js) → 5.
1. Privacy: deals, settings, money data and the home area (location, radius, towns) move to free private storage (Cloudflare D1 via a Worker; the scanner uses a secret API key). Remove them from the repo and from git history, add to .gitignore. Nothing private (names, amounts, chat IDs, tokens) in the public repo or logs.
2. Instant replies: Telegram commands and buttons handled by a Cloudflare Worker webhook. Remove all getUpdates polling from the scanner (a webhook forbids it). The scanner keeps searching and sending deals.
3. SHARED POT, not per-person money. The owner holds all the money in their own bank account as treasurer; the bot only records numbers.
   - /deposit <name> <amount> and /withdraw <name> <amount>: owner only.
   - Every buy is paid from the pot, every sale goes back into the pot.
   - Each member's contribution is tracked; profit is split by contribution share (equal now, €100 each), as a setting.
   - /pot: balance, money tied up in stock, total profit, and per member: contribution, profit share, what they'd get back if we stopped today.
   - Budget-mode limit = the smaller of /budget and the money in the pot.
   - (Replaced by team management: every buy is a 🙋 Request buy that a manager approves.)
   - Every money action is posted in the group. Full history; corrections are new entries, never edits.
4. Telegram Mini App (Cloudflare Pages + the same Worker, initData verified server-side, allowed users only): tabs Deals, Stock, Pot (same info as /pot), Settings. Dark, phone-first.
5. Health check: healthchecks.io (period 5 min, grace 30 min), Telegram integration to the owner; ping on success, /fail on failure, setup-step failures included.
6. Keep runs under ~4 min; suggest trims and ask before changing.
- `outputs/` and `Claude outputs/` stay in .gitignore, never committed.
