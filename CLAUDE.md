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
- `flipfinder/telegram.py` message format + Bot API
- `config.yaml` searches and rules; secrets in `.env` (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, optional EBAY_CLIENT_ID, EBAY_CLIENT_SECRET); Subito needs no keys (`subito: enabled`)
- `.github/workflows/flipfinder.yml` (shipped as `deploy/flipfinder.yml`, move it there) runs `main.py --once` every 5 min, `data/` persisted with actions/cache

## Commands
- `pytest` run tests
- `python main.py --dry-run --once -v` one scan, print instead of send

## Notes
- Keep `request_delay` >= 2s. Handle 401/403 by refreshing the cookie, 429 by backing off.
- Market value uses asking prices, not sold prices.
