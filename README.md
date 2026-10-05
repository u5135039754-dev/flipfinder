# flipFinder

Watches Vinted for listings priced well below what similar items usually sell for, and sends you a Telegram message like this:

```
🔥 Nike Dunk Low Panda
Nike · 42 · Very good

💶 Item price: €47.95 (listed €45.00)
📦 Shipping + packaging: €5.89
🏷 Original price: €85.00 (median of 24 listings)
💰 Possible profit: €31.16
📈 Percentage: +59%
🟢 Rating: 8/10

Open on Vinted
```

## How it decides

- **Item price** is what you'd actually pay, so it includes Vinted's buyer protection fee.
- **Original price** is the market value: the median price of the same model in the same search. Listings must contain every word of the search, and are only compared with listings of the same brand and model number (an MG15 against other MG15s, a Katana 50 MkII against other Katana 50 MkIIs). Listings without a model number are only compared when similar listings are priced close together. If there's no good match, the listing is skipped. Crazy outliers get dropped first, and the same size is preferred when there are enough. `--dry-run` shows which listings each deal was compared with.
- **Shipping** is the real delivery price from the listing's page (what Vinted charges you for the cheapest option). If the page can't be read, `shipping_cost` from the config is used and the alert says it's an estimate. Listings that say "solo ritiro a mano", "pickup only" and so on, or that Vinted won't ship, are marked 📍 Pickup only and counted without shipping.
- **Possible profit** = original price − item price − shipping − your `resell_costs` (packaging for when you sell it on; the buyer pays shipping then). Sellers don't pay fees on Vinted, so that's it.
- **Percentage** = profit ÷ (item price + shipping).
- **Rating** (1-10): up to 4 points for percentage, 4 for profit in €, 2 for how many listings the market value is based on.

Anything with too few comparable listings, too little profit, or a suspiciously huge discount (`max_roi`, usually fakes or broken stuff) gets skipped. Every item is only checked once.

## 1. Make a Telegram bot

1. In Telegram, message **@BotFather**, send `/newbot`, follow the steps. Copy the token it gives you.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and find `"chat":{"id":123456789`. That number is your chat ID.

## 2. Run it 24/7 for free (PC can be off)

This uses GitHub Actions, which runs the scan on GitHub's servers every 5 minutes.

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

- **Public vs private repo.** Public repos get unlimited Actions minutes. Private ones get 2,000 min/month, and every 5 min is about 8,600 runs, so if you go private change the cron in `.github/workflows/flipfinder.yml` to `*/30 * * * *`. Your token stays secret either way since it's in Secrets.
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

## Optional: eBay as a second source

flipFinder can also check eBay.it Buy It Now listings through eBay's official Browse API, and
compares every listing with the same model on both Vinted and eBay. Alerts then say where it's
listed (🛒 Vinted / 🛒 eBay), the median price on each platform, and where it's cheaper to buy
and sells for more.

1. Create a developer account at developer.ebay.com and a **Production** keyset.
2. On the keyset's **Notifications** page, turn on "Not persisting eBay data" (flipFinder only keeps
   listing IDs and prices, no eBay user data) and submit. eBay requires this before the first call.
3. Put the App ID and Cert ID in `.env` as `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET`, and add the
   same two as GitHub repository secrets.

eBay is searched at most every 20 minutes and its price pools are refreshed every 3 hours
(`ebay: interval_minutes`, `pool_refresh_minutes`), about 3,050 of the 5,000 daily API calls with
38 searches; each run logs how many calls were used today. Items located in Italy only by
default (`ebay: item_location: IT`, or `EU`). Selling on eBay.it is free for private sellers in the
EEA, so no fee is subtracted; set `sell_fees` in `config.yaml` if that changes for you.

## Optional: Subito, local pickup

With `subito: enabled: true`, flipFinder also checks Subito.it listings near you: new listings in
your province (`region`, `province`), kept if the town is within `radius_km` of `center`. The cost
to get an item is the cheaper of picking it up (`travel_cost`, with per-town overrides in
`town_travel_costs`) and the seller's shipping. Alerts show the town and distance, and
"💬 Negotiable" when the listing says *trattabile*. Market value compares the same model on Subito
(all of Italy), Vinted and eBay. Subito has no public API: this uses the JSON API of its app, which
works today but could change.

## Settings

Everything lives in `config.yaml`, with comments. The ones you'll touch most:

- `searches`: what to look for. Specific queries work way better ("nike dunk low" instead of "nike"), because the market value comes from that same search.
- `price_to`: max price you'd pay for that search.
- `rules.min_profit`, `rules.min_roi`, `rules.min_rating`: how picky the alerts are.
- `exclude_keywords`: skip titles with these words. Per search they can include a shared list
  through a YAML anchor, e.g. `exclude_keywords: [*electronics, portal]`.
- Per search you can also set `filters: {catalog: [ID]}` (Vinted's category, from the URL when you
  pick a category on the site), `shipping_cost`, `resell_costs`, `max_roi`, and `match_brand: false`
  where different brands sell the same thing (graphics cards).

Listings are only compared with the same model: model numbers, storage (128GB vs 256GB), sizes
(41mm/45mm), generations and suffixes like Pro, Max, Mini, Plus, OLED and Ti have to match. When you
add searches, the first run sends only the best 3 deals already listed across them and remembers the
rest, and after that at most 4 price pools are rebuilt per run so runs stay short.

## Tests

```
pip install -r requirements-dev.txt
pytest
```

## Heads up

This reads Vinted's public listing data the same way the website does. Vinted's terms don't allow automated scraping, so keep `request_delay` at 2+ seconds, keep the number of searches reasonable, and use it for yourself. Market value is an estimate from asking prices, not sold prices, so always check the listing (photos, condition, authenticity) before buying.
