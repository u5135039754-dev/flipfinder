# flipFinder

Watches Vinted for listings priced well below what similar items usually sell for, and sends you a Telegram message like this:

```
🔥 Nike Dunk Low Panda
Nike · 42 · Very good

💶 Item price: €47.95 (listed €45.00)
🏷 Original price: €85.00 (median of 24 listings)
💰 Possible profit: €37.05
📈 Percentage: +77%
🟢 Rating: 8/10

Open on Vinted
```

## How it decides

- **Item price** is what you'd actually pay, so it includes Vinted's buyer protection fee.
- **Original price** is the market value: the median price of the same model in the same search. Listings must contain every word of the search, and are only compared with listings of the same brand and model number (an MG15 against other MG15s, a Katana 50 MkII against other Katana 50 MkIIs). Listings without a model number are only compared when similar listings are priced close together. If there's no good match, the listing is skipped. Crazy outliers get dropped first, and the same size is preferred when there are enough. `--dry-run` shows which listings each deal was compared with.
- **Possible profit** = original price − item price − your `resell_costs`. Sellers don't pay fees on Vinted, so that's it.
- **Percentage** = profit ÷ item price.
- **Rating** (1-10): up to 4 points for percentage, 4 for profit in €, 2 for how many listings the market value is based on.

Anything with too few comparable listings, too little profit, or a suspiciously huge discount (`max_roi`, usually fakes or broken stuff) gets skipped. Every item is only checked once.

## 1. Make a Telegram bot

1. In Telegram, message **@BotFather**, send `/newbot`, follow the steps. Copy the token it gives you.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and find `"chat":{"id":123456789`. That number is your chat ID.

## 2. Run it 24/7 for free (PC can be off)

This uses GitHub Actions, which runs the scan on GitHub's servers every 10 minutes.

1. Move `deploy/flipfinder.yml` into a `.github/workflows/` folder (create it). In PowerShell, from the FlipFinder folder:
   ```
   mkdir .github\workflows
   move deploy\flipfinder.yml .github\workflows\flipfinder.yml
   ```
2. Create a new repo on GitHub and push this folder to it:
   ```
   git init
   git add .
   git commit -m "flipFinder"
   git branch -M main
   git remote add origin https://github.com/<you>/flipfinder.git
   git push -u origin main
   ```
3. In the repo go to **Settings → Secrets and variables → Actions → New repository secret** and add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
4. Go to the **Actions** tab, open **flipFinder**, click **Run workflow** once to test it. After that it runs by itself.

To change searches later, edit `config.yaml` and push. No need to touch anything else.

Things to know:

- **Public vs private repo.** Public repos get unlimited Actions minutes. Private ones get 2,000 min/month, and every 10 min is about 4,300 runs, so if you go private change the cron in `.github/workflows/flipfinder.yml` to `*/30 * * * *`. Your token stays secret either way since it's in Secrets.
- GitHub can delay scheduled runs by a few minutes when it's busy, and it pauses schedules in repos with no commits for 60 days. Push any small change to wake it up.
- Vinted sometimes blocks requests from data center IPs. If the Actions logs show 403 errors on every run, use the VPS option below or run it on your PC.

### Other option: a VPS

On any Linux server (Oracle Cloud free tier, Hetzner, etc.) with Docker:

```
cp .env.example .env    # fill in your token and chat ID
docker compose up -d
```

## 3. Run it on your PC

Windows: copy `.env.example` to `.env`, fill it in, then double-click `run.bat`. Or by hand:

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py --test-telegram   # sends a sample message
python main.py --dry-run --once  # one scan, prints deals instead of sending
python main.py                   # runs forever
```

## Settings

Everything lives in `config.yaml`, with comments. The ones you'll touch most:

- `searches`: what to look for. Specific queries work way better ("nike dunk low" instead of "nike"), because the market value comes from that same search.
- `price_to`: max price you'd pay for that search.
- `rules.min_profit`, `rules.min_roi`, `rules.min_rating`: how picky the alerts are.
- `exclude_keywords`: skip titles with these words.

## Tests

```
pip install -r requirements-dev.txt
pytest
```

## Heads up

This reads Vinted's public listing data the same way the website does. Vinted's terms don't allow automated scraping, so keep `request_delay` at 2+ seconds, keep the number of searches reasonable, and use it for yourself. Market value is an estimate from asking prices, not sold prices, so always check the listing (photos, condition, authenticity) before buying.
