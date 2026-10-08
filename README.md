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

## 2. The private side: a Cloudflare Worker (free)

Everything private lives in a Cloudflare Worker with a D1 database, not in the (public) repo:
settings changed from Telegram, deal tracking and money, and your home area for Subito. The Worker
also does all the Telegram talking: commands and buttons arrive by webhook and are answered in about
a second, and deals the scanner finds are sent from there. The scanner on GitHub only searches; it
reads its settings from the Worker and hands deals over, with a secret API key.

From the `worker` folder (Node.js needed), after `npm install`:

```
npx wrangler login                          # once, opens the browser
npx wrangler d1 create flipfinder           # put the id it prints in wrangler.toml
npx wrangler d1 execute flipfinder --remote --file schema.sql
npx wrangler secret put TELEGRAM_BOT_TOKEN  # and TELEGRAM_CHAT_ID, OWNER_ID, API_KEY, WEBHOOK_SECRET
npx wrangler deploy
```

Then point Telegram at it once with `setWebhook` (url `<worker url>/telegram`, `secret_token` =
WEBHOOK_SECRET). The free plan (100,000 requests a day) is far more than flipFinder uses.
`npm test` runs its tests.

## 3. Run the scanner 24/7 for free (PC can be off)

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
3. In the repo go to **Settings → Secrets and variables → Actions → New repository secret** and add `FLIPFINDER_API_URL` (the Worker's address) and `FLIPFINDER_API_KEY` (its `API_KEY`).
4. Go to the **Actions** tab, open **flipFinder**, click **Run workflow** once to test it. After that it runs by itself.

To change searches later, edit `config.yaml` and push. No need to touch anything else.

Things to know:

- **Public vs private repo.** Public repos get unlimited Actions minutes. Private ones get 2,000 min/month, and every 5 min is about 8,600 runs, so if you go private change the cron in `.github/workflows/flipfinder.yml` to `*/30 * * * *`. Nothing private is in the repo or the run logs either way: keys are in Secrets, data in the Worker.
- GitHub can delay scheduled runs by a few minutes when it's busy, and it pauses schedules in repos with no commits for 60 days. Push any small change to wake it up.
- Vinted sometimes blocks requests from data center IPs. If the Actions logs show 403 errors on every run, use the VPS option below or run it on your PC.

### Other option: a VPS

On any Linux server (Oracle Cloud free tier, Hetzner, etc.) with Docker:

```
cp .env.example .env    # fill in the Worker's address and API key
docker compose up -d
```

## 4. Run it on your PC

Windows: copy `.env.example` to `.env`, fill it in, then double-click `run.bat`. Or by hand:

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py --test-telegram   # sends a sample message through the Worker
python main.py --dry-run --once  # one scan, prints deals instead of sending
python main.py                   # runs forever
```

## Sold prices and demand

Asking prices overstate what things sell for, so flipFinder learns what actually sells on Vinted.
Each price-pool refresh records its listings; when one has gone from the next refresh, a few such
listings per run get their page opened (most just dropped off the pages we read): "can't be bought"
means sold, at the last price seen. Listing dates come from Vinted's ids, which only go up.

- With 5+ sold listings of the same model in 60 days, market value moves from the asking median
  towards the sold median (sold only from 10). Alerts say "🏷 Market value: €85 (7 sold · asking €95)".
- Demand per model on every alert and in the app: 🔥 high (sells in ≤7 days or sold per week ÷
  listed now ≥ 0.5), 👍 normal, 🐢 slow (30+ days or < 0.1), e.g. "🔥 High demand · ~12 sold/week ·
  8 listed · ⏱ sells in ~4 days · ❤️ 9". Sold per week is an estimate: the sales we confirm are a
  sample, scaled by how many gone listings we could check. The first 1-2 weeks say "still learning".
- Selling speed moves the rating by one point: +1 within 7 days, -1 over 30.
- /demand iphone 13 128gb shows the numbers for any search and its models.
- Page checks: 4 per run (about 12 s). Any Vinted refusal (403/429) halves them, down to a 6 h
  pause, with a note in Summary; after 24 h without one they go back up one at a time.
- History in `data/sold.json` (public listing data only, 90 days). Vinted only: eBay doesn't show
  sales and Subito ads just disappear. Vinted shows favourites but no view counts.

## Seller trust

Every deal shows who sells it: "👤 4.9★ · 256 reviews · 251 sold · ⚡ fast shipper · seen 2 h ago"
(Vinted: from the listing page plus the seller's profile, one extra page per deal; eBay: "% positive"
and feedback score from the search results; Subito shows nothing usable), and "🏪 Business seller"
for shops. A seller with no reviews and nothing sold, at a suspiciously big discount (ROI 100% or
more), is skipped as the classic scam profile and counted in the daily summary ("🛡 2 deals skipped").
Otherwise new sellers and low ratings (under 4.5★ with 5+ reviews, or under 97% positive on eBay with
10+ feedback) get a ⚠️ line. Vinted doesn't publish when an account was created.

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
your province, kept if the town is within `radius_km` of home. The cost to get an item is the
cheaper of picking it up (`travel_cost`, with per-town overrides in `town_travel_costs`) and the
seller's shipping. The home area (`region`, `province`, `center`, `radius_km`, `travel_cost`,
`town_travel_costs`, `city` for /sell) is private: it's kept in the Worker (key `area`), never in
`config.yaml`, and without it Subito is skipped. Alerts show the town and distance, and
"💬 Negotiable" when the listing says *trattabile*. Market value compares the same model on Subito
(all of Italy), Vinted and eBay. Subito has no public API: this uses the JSON API of its app, which
works today but could change.

## Telegram commands

Change things from Telegram instead of editing files: /help, /status, /categories (buttons to turn
searches on or off), /prices, /setprice "boss katana" 80 250, /budget 72, /rules, /setrule min_roi 25,
/add "zoom g1x four" 20 60, /remove (asks first). The Worker answers right away; search and price
changes apply from the next run. Changes are kept in the Worker's private storage and override
`config.yaml`. Only the owner (`OWNER_ID`) and users the owner adds with /allow can use them; they
work in the private chat and in the group.

`TELEGRAM_CHAT_ID` (a Worker secret) can list several chats, comma-separated (private chat first).
If a group is upgraded to a supergroup (new chat id), flipFinder switches automatically, saves it
and tells the owner.

## Group deal tracking

Every deal has buttons: ✋ Claim → 💸 Bought (the bot asks what you paid) → 🏷 Listed → ✅ Sold (asks
the sale price). The message, in every chat, shows the current status and who has it; only whoever
claimed it (or the owner) moves it on. 👍/👎 votes are counted on the buttons and every 👎 is logged
with the listing to tune the filters. 📩 Message seller replies with a ready-to-copy Italian message
asking if it's still available and for a video of it working.

/stock lists what's bought or listed, who has it and what was paid; /profit shows profit in total,
this month and per person. Only allowed users can press the buttons.

### The shared pot

The money itself sits in the treasurer's (owner's) bank account; the bot only keeps the numbers,
in an append-only ledger in the Worker's private storage.

- /deposit Marco 100 and /withdraw Marco 50 (owner only) record money put in or paid out. A
  withdrawal can't exceed what that member has in the pot, or the cash on hand.
- 💸 Bought takes the price out of the pot, ✅ Sold puts the sale price back in.
- Each sale's profit is split when it happens, by how much each member has put in (/split equal
  to share equally instead); later deposits and withdrawals never change earlier splits.
- /pot shows the cash, the stock (at what we paid), total profit and, per member, what they put in,
  their profit share and what they'd get back if we stopped today. /ledger lists every entry.
- Buys over €50 need a 👍 on the deal from another member before 💸 Bought works.
- Every money action is posted in the group's Summary topic. Entries are never edited: /undo 7
  cancels a deposit, withdrawal or fix with a new entry, /fix 12 paid 45 or /fix 12 sold 80
  corrects a price with a new entry (shared like the original sale).
- The budget-mode limit is the smaller of /budget and the cash in the pot.

### The app

Tap **📱 Open app** next to the message box in the private chat with the bot (or send /app) for a
Telegram Mini App with four tabs: **Deals** (filter by category, platform and price; claim, vote,
mark bought/listed/sold), **Stock**, **Pot** (cash, stock, profit, each member's share and the
ledger) and **Settings** (searches on/off, price ranges, budget, rules, profit split). It's served
by the same Worker (`worker/public/app/`), every request is checked against Telegram's signed login
data, and only allowed users get in. Everything it does goes through the same code as the chat
buttons, so deal messages update and money moves are posted in Summary the same way.

### The team: roles, buying, duty, tasks

Kept in the Worker's private storage (names and roles too, never in the repo).

- **Roles:** /roles shows who does what (manager: bot + money, approves and pays every buy; buyer:
  buying + main deal watcher; seller: selling, listings, buyer messages, shipping). The owner sets
  them with /setrole <name> manager|buyer|seller; people must have messaged the bot once.
- **Buying:** whoever claimed a deal taps 🙋 Request buy with the agreed price; the manager gets
  ✅ Approve / ❌ Reject (privately and in the deal's topic). On approve it's 💸 Bought, paid from
  the pot (Vinted/eBay: the manager pays online; Subito: sends the buyer the money for the pickup),
  and the seller is pinged to list it. A manager's own buys need no approval.
- **Duty, 08:00-22:00 Italy time:** a pinned "👮 On duty now" message with 🟢 Start / 🔴 End /
  🙋 Need a swap (first ✅ I'll take it gets it, with a warning past someone's 20%). The buyer's
  target is 8 h a day, the others up to 2.8 h (20% of the 14 h). New deals @mention whoever is on
  duty; unclaimed after 10 min, the other two are pinged; nobody on duty, everyone is pinged from
  07:30 and every 30 min. Shifts end at 22:00. All of this starts once a role is set.
- **Schedule** (📅 tab in the app): a Mon-Sun grid of 1-hour blocks, tap to claim or free, swap your
  own; a reminder 10 min before your block, the group is told if you're 15 min late.
- **Tasks:** /task add Photos for #12 @Anna by fri, /task done 3, /tasks; a reminder 3 days after
  the due date (or 3 days after it was added).
- **Sunday report** adds duty hours vs target, deals caught per shift, average reaction time, open
  tasks, stock waiting to be listed or sold, and bot health.

### Rules topic (team handbook)

Create a topic, send /topic rules in it, and the handbook is posted there as ONE pinned message
(pinned silently, the "pinned a message" notice removed), each section's rules in a collapsed quote.
/handbook shows a copy anywhere; /handbook edit <section> (owner only) replaces a section's rules,
on the lines below or as a reply, and the pinned message is edited in place. In the Rules topic,
commands and the bot's answers are deleted after 10 seconds, so only the handbook stays (the bot
needs the "Delete messages" admin right). The text lives only in the Worker's private storage
(key `handbook`), never in the repo. /rules still shows the deal rules.

### Crypto topic (news only)

A separate, read-only Crypto topic (create it, then send /topic crypto there). It never touches the
pot, deals or settings, and gives no buy/sell suggestions. The Worker's own hourly timer (at :07)
does it, not the scanner:

- **18:00 digest** (Italy time): the 5 newest headlines from the CoinDesk and Cointelegraph RSS
  feeds, the watchlist's EUR prices with 24 h change and 24 h volume vs its 7-day average
  ("volume 2.3× normal"), and CoinGecko's top 5 trending coins ("trending = lots of searches, not
  a reason to buy"). Every digest ends with "News only, not financial advice."
- **Alerts:** a watched coin moving ±10% in 24 h, or trading 3× its normal volume; at most once
  per coin and kind every 12 hours.
- **/watch** shows the watchlist (BTC, ETH, SOL to start) or adds a coin, **/unwatch** removes one;
  any allowed member, up to 10 coins, checked against CoinGecko.
- Free: CoinGecko's public API (no key) and RSS. If CoinGecko starts refusing (it limits shared
  IPs), a free CoinGecko "Demo" key as the Worker secret COINGECKO_KEY fixes it; the news still
  goes out meanwhile.

### Topics, reminders, quiet hours, weekly report

- **Topics:** turn on Topics in the group and create Guitars, Electronics, Budget and Summary, then
  send /topic once in each (or /topic guitars to name one). Deals post in their category's topic;
  the daily summary, weekly report and failure alerts in Summary. The private chat gets everything.
- **Intros:** /topic also pins a short intro in that topic (and one in the main chat). /intro (owner
  only) posts them again; an intro that's already there is edited, not duplicated. The bot needs the
  "Pin messages" admin right.
- **Reminders:** a claim with no update for 24 h pings its owner (keep it or release it) and is
  released after 48 h; something bought but not listed after 3 days gets a nudge; something listed
  but not sold after 14 days gets a price-cut suggestion based on the current market value.
- **/sell 12** (or a name) writes a ready-to-copy listing: title, honest description to complete,
  a suggested price and a quick-sale price. Add en or uk for English or Ukrainian.
- **Quiet hours:** no deal alerts 00:00-08:00 Italy time; deals found overnight are sent at 08:00,
  best first (by the Worker's 5-minute timer, which also sends reminders and the weekly report).
  Failure alerts still go out.
- **Weekly report** on Sunday at 20:00 in Summary: deals found/claimed/bought/sold, profit per
  person, the best flip and the most down-voted search.

## Budget mode

Searches with `budget: true` only alert when everything you pay (price, buyer fee, shipping or
pickup) fits `budget`, use `budget_rules` (min profit/ROI, a max ROI against fakes) and are ranked
by profit per euro spent. Each can have a `check:` line, shown in the alert as what to check before
buying.

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
