# GoodSkrapper 🛒 → 💰

Finds **ShopGoodwill** auctions you can flip on **eBay** for more than your target profit (default 40 %),
sends them to your private **Telegram** channel through `@GoodSkrapper_alert_bot`, and gives you a small
**web dashboard** to manage what it finds.

Built to run all the time on a **Raspberry Pi 4 with Raspberry Pi OS Lite**:

- No headless browser: it calls the same JSON API that shopgoodwill.com uses (`buyerapi.shopgoodwill.com`).
- One lightweight Python worker (~40–60 MB RAM) plus an optional dashboard (~40 MB), SQLite in WAL mode.
- It scans results sorted by *ending soonest* and stops once it passes your time horizon, so it makes fewer requests.
- eBay lookups are cached per query, Goodwill shipping quotes are only requested for promising items, and requests are throttled.
- systemd units with `Nice`, idle I/O priority and memory caps. Logs go to journald by default, which saves SD-card writes.

---

## How it works

```
 targets (keywords / categories)
            │
            ▼
 ShopGoodwill search API ──► filter (exclusions, horizon, price range)
            │
            ▼
 eBay price research (Browse API or sold listings) ──► median of matching comps
            │
            ▼
 profit model ──► deal? ──► SQLite DB ──► Telegram (hourly digest / urgent < 8 h)
                                 ▲
                                 └── Dashboard (CRUD)
```

**Profit model** (all values configurable in `profit:`):

```
total cost  = projected bid × (1 + sales tax) + Goodwill shipping & handling (real quote for your ZIP)
eBay fees   = sale × (final value fee % + promoted %) + fixed fee
profit      = eBay sale price − eBay fees − outbound shipping − total cost
ROI %       = profit / total cost × 100          → deal when ROI ≥ target_profit_pct and profit ≥ min_profit_usd
max bid     = the highest bid that still gives you the target ROI (shown in every alert)
```

**Max bid** is the most useful number: bid up to it and you still hit your target.

---

## Installation & Deployment

### Option A: Windows (10 / 11 / Server)

#### 1. Automated Setup (PowerShell)
Open PowerShell in the `goodSkrapper` folder and run:
```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\install.ps1
```
This automatically:
- Detects Python 3.10+
- Creates a virtual environment in `.venv\`
- Installs dependencies from `requirements.txt` (including native Windows certificate store support via `truststore`)
- Generates `config.yaml` from `config.example.yaml`
- Creates the `data\` directory

#### 2. Configuration & Telegram Setup
1. Edit `config.yaml` in your preferred editor (Notepad, VS Code, etc.).
2. Set your Telegram bot token and discover your private channel ID:
   ```powershell
   .\.venv\Scripts\python -m goodskrapper telegram-chat-id
   ```
   Copy the channel ID into `telegram.chat_id` in `config.yaml`.
3. Verify connection:
   ```powershell
   .\.venv\Scripts\python -m goodskrapper telegram-test
   ```

#### 3. Running on Windows

**Method 1: Interactive (Quickest)**
Double-click `deploy\run-all.bat` (or run in terminal):
```cmd
.\deploy\run-all.bat
```
Runs both the background worker and web dashboard concurrently in a single terminal. Access the dashboard at `http://localhost:8080`.

**Method 2: Background Service (Windows Task Scheduler - Auto-starts at login)**
Run the included Task Scheduler script (uses `pythonw.exe` for quiet, windowless background operation):
```powershell
# Register and start the background task
powershell -ExecutionPolicy Bypass -File .\deploy\setup-windows-service.ps1

# To view status:
Get-ScheduledTask GoodSkrapper*

# To stop or uninstall:
powershell -ExecutionPolicy Bypass -File .\deploy\setup-windows-service.ps1 -Uninstall
```

**Method 3: Native Windows Service (via NSSM)**
If you prefer a true Windows NT Service that runs before user login:
```cmd
nssm install GoodSkrapper "%CD%\.venv\Scripts\python.exe" "-m goodskrapper all"
nssm set GoodSkrapper AppDirectory "%CD%"
nssm start GoodSkrapper
```

---

### Option B: Raspberry Pi OS Lite (Raspberry Pi 4)

```bash
sudo apt-get install -y git          # or copy the folder with scp
git clone <your-repo> goodSkrapper   # or: scp -r goodSkrapper pi@raspberrypi.local:~
cd goodSkrapper
bash deploy/install.sh
nano config.yaml
```

The installer creates a virtualenv, installs dependencies (prebuilt ARM wheels from piwheels),
copies `config.example.yaml` → `config.yaml`, and installs two systemd services.

### 1. Telegram setup (existing bot `@GoodSkrapper_alert_bot`)

1. Put the bot token from @BotFather in `telegram.bot_token`.
2. Add the bot to your private channel as an **administrator** with the *Post messages* permission.
3. Post any message in the channel, then run:
   ```bash
   .venv/bin/python -m goodskrapper telegram-chat-id
   ```
   Copy the id that looks like `-1001234567890` into `telegram.chat_id`.
4. Test it: `.venv/bin/python -m goodskrapper telegram-test`

**Alert behaviour**

| Type | When | Content |
|------|------|---------|
| Hourly digest | every `digest_interval_minutes` (60) | New deals not sent before, best ROI first |
| Urgent alert | as soon as a deal **ends in < `urgent_threshold_hours` (8 h)** | Photo + price, ROI, max bid, time left |

The worker re-checks the current bid of ending-soon items every `urgent_check_minutes`. If the bid has gone
up, it recalculates profit before alerting, so you are not alerted about items that are no longer worth it.
Each item gets at most one digest entry and one urgent alert. Use **Re-send to Telegram** in the dashboard to send one again.

### 2. eBay price source

| `ebay.provider` | Accuracy | Setup |
|-----------------|----------|-------|
| `browse_api` *(recommended)* | Uses **active** listings, multiplied by `price_adjustment_factor` (0.85) | Free keys at <https://developer.ebay.com> → *Application Keys* (Production App ID + Cert ID) |
| `sold_scrape` | Uses **sold** listings (true market price) | None. eBay can block automated page requests at any time. The tool then pauses that source for 1 h and does not try to get around the block |

`fallback_provider` is used when the main one fails (for example, when no API keys are configured yet).
Every item still includes an **"eBay sold comps" link** so you can check the price yourself before you bid.

### 3. Targets

```yaml
targets:
  - name: "Video games"
    keywords: ["nintendo switch", "gameboy advance"]   # each keyword = one search
    categories: []                                     # optional: restrict to category IDs
    exclude_keywords: ["case only"]
    min_price: 5
    max_price: 250
    target_profit_pct: 50                              # optional per-target override
```

List category IDs with: `.venv/bin/python -m goodskrapper categories` (or `categories camera` to filter).

### 4. Start

```bash
sudo systemctl start goodskrapper-worker goodskrapper-dashboard
journalctl -u goodskrapper-worker -f
```

Dashboard: `http://<pi-ip>:8080` (HTTP Basic auth, using `dashboard.username` / `dashboard.password`).

---

## Dashboard

- **Items list**: filter by text, status, active/ended, and deals only. Sort by ROI, profit, end time, and more. Shows totals for items, active deals, watching, and pending Telegram alerts.
- **Create**: paste a ShopGoodwill URL or ID. The dashboard fetches the details, gets the shipping quote, and prices the item on eBay. You can also enter everything by hand.
- **Read / Update**: edit any field. Set a **manual eBay price** (overrides the automatic one), edit the eBay search query, set the status (`new`, `watching`, `bid_placed`, `won`, `lost`, `sold`, `ignored`), and add notes. Profit is recalculated when you save.
- **Delete**: one item or many at once. Bulk actions can also change status or re-send items to Telegram.
- **Refresh**: update the price from Goodwill, optionally re-price on eBay.
- **Run scrape now**: asks the worker to start a scrape right away.
- **Settings**: shows the active configuration with secrets hidden.
- JSON API: `GET /api/items?deals=1&active=1`.

## Database retention

Items are deleted `database.retention_days` after they were first seen, **unless** their status is in
`database.keep_statuses` (default: watching, bid_placed, won, sold). Cleanup runs every 6 hours, or on demand with
`python -m goodskrapper cleanup`.

---

## CLI reference

```bash
python -m goodskrapper worker            # scheduler: scrape + alerts (systemd)
python -m goodskrapper dashboard         # web UI
python -m goodskrapper all               # both in one process
python -m goodskrapper scrape-once [--notify]
python -m goodskrapper digest            # send pending Telegram alerts now
python -m goodskrapper telegram-test
python -m goodskrapper telegram-chat-id
python -m goodskrapper categories [text]
python -m goodskrapper price "Canon EF 50mm f/1.8 STM lens"   # test eBay pricing
python -m goodskrapper cleanup
# global flags: -c /path/config.yaml   -v (debug logging)
```

Secrets can be set as environment variables instead of in the file:
`GOODSKRAPPER_TELEGRAM_TOKEN`, `GOODSKRAPPER_TELEGRAM_CHAT_ID`, `GOODSKRAPPER_EBAY_CLIENT_ID`,
`GOODSKRAPPER_EBAY_CLIENT_SECRET`, `GOODSKRAPPER_DASHBOARD_PASSWORD`, `GOODSKRAPPER_SECRET_KEY`.

Run tests: `python -m unittest discover tests`

## Tips & caveats

- **Auction prices rise near the end.** Set `expected_final_price_multiplier` (for example 1.3) for more conservative projections, and treat *max bid* as your limit.
- Matching uses Goodwill titles, which can be messy. Lots and bundles are hard to price automatically. Check the comps link or set a manual price.
- Keep `request_delay_seconds` and `update_frequency_minutes` reasonable to stay polite to ShopGoodwill and eBay.
- Use the scraper for personal research only and follow the ShopGoodwill and eBay terms of service.
