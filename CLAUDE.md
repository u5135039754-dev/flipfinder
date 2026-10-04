# flipFinder

Python 3.10+ app that finds underpriced Vinted listings and sends Telegram alerts.

## Layout
- `main.py` CLI entry (`--once`, `--dry-run`, `--test-telegram`, `-v`)
- `flipfinder/vinted.py` Vinted catalog API client (`/api/v2/catalog/items`, cookie from homepage)
- `flipfinder/analyzer.py` market value (median of comparables, IQR outlier removal, same-size preference), profit, ROI, 1-10 rating, filters
- `flipfinder/scanner.py` per-search scan, price pool cache in `data/pools.json`
- `flipfinder/storage.py` seen item IDs in `data/seen.json` (14 day expiry)
- `flipfinder/telegram.py` message format + Bot API
- `config.yaml` searches and rules; secrets in `.env` (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
- `.github/workflows/flipfinder.yml` (shipped as `deploy/flipfinder.yml`, move it there) runs `main.py --once` every 10 min, `data/` persisted with actions/cache

## Commands
- `pytest` run tests
- `python main.py --dry-run --once -v` one scan, print instead of send

## Notes
- Keep `request_delay` >= 2s. Handle 401/403 by refreshing the cookie, 429 by backing off.
- Market value uses asking prices, not sold prices.
