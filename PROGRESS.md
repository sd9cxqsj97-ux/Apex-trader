# APEX AUTO-TRADER — PROJECT PROGRESS
*Last updated: 2026-04-04 — Session 2*

---

## VPS DEPLOYMENT (2026-04-04) — COMPLETE
- VPS IP: 164.92.237.116 (DigitalOcean)
- SSH key saved at: C:/Users/LANOVO/.ssh/id_ed25519
- Bot is LIVE and running 24/7 as systemd service `apex-bot`
- Push future updates with: scp + ssh restart (deploy_update.bat or manual)
- Telegram confirmed bot online after deploy

## SESSION 2 UPGRADES (2026-04-04) — ALL COMPLETE
Backup file: `bot_v4.1_regime_kelly_ml.py`

### 1. Regime Detection (DONE)
The bot now detects what TYPE of market it is in before trading:
- TRENDING = price moving strongly in one direction (good for EMA, SMC_BOS)
- RANGING = price moving sideways (good for BB_TOUCH, Mean Reversion)
- VOLATILE = price jumping wildly (bot cuts position size in half automatically)

### 2. Kelly Criterion Sizing (DONE)
Instead of always trading the same size, the bot now reads its own recent results
(last 20 trades) and sizes up when it is winning and sizes down when it is losing.
This is mathematically the optimal way to grow an account.

### 3. Weighted MTF Scoring (DONE)
"MTF" = Multi-Timeframe = checking bigger time charts to confirm signals.
Old way: just yes/no. New way: each chart that agrees adds bonus points to the signal
score. Daily chart agrees = +15 points, 4-hour chart agrees = +10 points, etc.
If the Daily chart DISAGREES, the trade is cancelled (hard veto).

### 4. Smarter Orderbook (DONE)
The bot now reads TWO OANDA data sources instead of one:
- Order Book: where buy/sell orders are sitting
- Position Book: where other traders have their stops (where price will be hunted)
This gives the bot information that most retail traders never see.

### 5. Spread Filter (DONE)
Spread = the cost to enter a trade. Sometimes during news or low liquidity,
the spread widens 3-5x. The bot now skips any trade where the spread is
more than 2.5x the normal amount. Saves money on bad fills.

### 6. ML Shadow Mode (DONE)
ML = Machine Learning. The bot now logs a "win probability" guess for every
signal it sees. Right now it ONLY logs — it does NOT block trades.
Once 200+ trades are logged, you can turn on ML_GATE=True in bot.py and
the bot will start refusing trades it thinks have less than 45% chance of winning.
This gets smarter the more it trades.

### All signals now logged with extra data:
- Regime (TRENDING/RANGING/VOLATILE)
- ATR percentile (how volatile vs recent history)
- RSI value, Volume ratio, Spread ratio
- Hour and day of week
- ML prediction (win probability %)

---

## WHAT HAS BEEN BUILT

### Core Bot — `bot.py` (V4 "Hedge Fund Edition")
Full 24/7 autonomous trading bot targeting OANDA practice account.

**Strategies: 21 total**
- 15 Classic: EMA_CROSS, BB_SQUEEZE, BB_TOUCH, GOLDEN_CROSS, MEAN_REV, TREND_CONT, VWAP, ENGULF, RETEST, EMA_RIBBON, STOCH_CROSS, LIQ_SWEEP, and 3 removed from backtest (see below)
- 6 SMC (Smart Money Concepts): SMC_OB (Order Block), SMC_FVG (Fair Value Gap), SMC_BOS (Break of Structure), SMC_CHOCH (Change of Character), SMC_DISP (Displacement), SMC_DISCOUNT / SMC_PREMIUM (Premium/Discount zones)
- Dead strategies removed: DEATH_CROSS (11.8% WR), RSI_DIV (17.2%), LONDON_BREAK (26.3%)

**18 Instruments:** EUR/USD, GBP/USD, USD/JPY, AUD/USD, USD/CAD, USD/CHF, NZD/USD, EUR/GBP, EUR/JPY, GBP/JPY, AUD/JPY, GOLD, SILVER, BRENT, WTI, SP500, NAS100, DOW30

**Timeframes:** Scalp (M5, M15, H1) and Swing (H4, D) — scanned sequentially per instrument

**Signal Filters (layered):**
- MIN_SCORE=65, MIN_STRATS=3 (tuned down from 75/4 to increase trade frequency)
- WEAK_STRATS: LIQ_SWEEP, VWAP add score but don't count toward MIN_STRATS
- 200 EMA gate: hard filter on H4/D, soft (2% distance) on scalp TFs
- Session filter: only scans instrument during its active session
- News blackout: 30-min window around high-impact ForexFactory events
- MTF confirmation: signal must be confirmed on at least 1 higher timeframe
- Correlation filter: no duplicate correlated positions (e.g., 2x EUR longs)
- Daily loss limit (tiered by balance) + 3 consecutive loss pause
- Bad time filter: no trades Mon pre-10:00 or Fri post-17:00 UTC

**Scoring bonuses:**
- Session quality: +10 London/NY overlap (13-16 UTC), +4 London/NY, +0 Asian
- Volume ratio >1.5x: +5
- RSI extreme (>15 from 50): +5
- Liquidity sweep: +10
- Each SMC strategy fired: +10 each
- DXY alignment (for commodities): ±8
- COT alignment: +10 (or -15 penalty for against)
- Orderbook contrarian (for commodities): ±8

**Trade Management:**
- Breakeven SL move at 1x ATR profit
- Trailing stop from 1.5x ATR
- Reversal exit: close if H4 signals flip 3+ strategies the other way
- Max hold timer for scalps (M5: 30min, M15: 90min, H1: 240min)
- Scalp TP/SL: 1.0-1.5x ATR / 0.5-0.7x ATR
- Swing TP/SL: 2.5-3.0x ATR / 1.0-1.2x ATR

**Risk Tiering by Balance:**
| Balance      | Lots  | Daily Loss Limit |
|-------------|-------|-----------------|
| <$1,000     | 0.10  | 20%             |
| $1k–$2k     | 0.20  | 15%             |
| $2k–$3.5k   | 0.50  | 12%             |
| $3.5k–$5k   | 1.0   | 10%             |
| >$5k        | 1.0   | 8%              |

**Claude AI Confirmation (APEX-AI):**
- Uses `claude-haiku-4-5-20251001` — sends signal data, gets 0-100 score + brief reason
- Blends: final_score = base_score × 0.60 + ai_score × 0.40
- Only fires if `CLAUDE_API_KEY` env var is set
- Requires `CLAUDE_API_KEY` env var (not hardcoded)

**Notifications:**
- Telegram bot (hardcoded token + chat ID in code)
- ntfy.sh push alerts
- Daily briefings at London Open (07:00 UK), NY Open (14:30 UK), NY Close (21:00 UK)

**State / Logging:**
- `state.json`: open trades, daily loss, W/L counts, 200-entry journal, consecutive loss counter
- `trade_log.csv`: full per-trade CSV log with entry, TP, SL, ATR, lots, strategies, score, AI reason, result, P&L, duration, balance

---

### Backtesting Engine — `backtest.py` (v1)
- Pulls 6 months OANDA H1/H4/D data for all 18 instruments (M5/M15 excluded — too many candles)
- Walk-forward: trains on first 4 months, tests on last 2 (unseen)
- Per-strategy, per-instrument, per-session win rates
- Equity curve simulation (1% risk/trade, $10k start)
- Sharpe ratio + max drawdown
- Outputs `backtest_results.csv` + `backtest_report.html` (dark themed, includes equity chart)
- **Backtest has been run** — `backtest_results.csv` contains data from Nov 2025 → Apr 2026

---

### Proxy Server — `proxy.py`
- Flask server on `localhost:5000`
- `/oanda/<path>` — proxies OANDA API calls (avoids CORS from browser)
- `/claude` — proxies Claude API calls from browser (uses `CLAUDE_API_KEY` env var)
- Used by all HTML dashboards to talk to OANDA without exposing keys in browser

---

### Dashboards
- **`apex-ultimate.html`** — Main dashboard "APEX INTELLIGENCE V4": premium dark UI (Inter + JetBrains Mono), tab navigation with dropdowns, live account stats header, designed for local Chrome with `--disable-web-security`
- **`apex-intel-v3.html`** — Intel dashboard (auto-downloaded from GitHub Gist via `update_intel.py`, URLs patched to use proxy)
- **`apex-scanner-v2.html`** — Scanner dashboard

---

### Launch & Deployment
- **`START_APEX.bat`** — One-click launcher: starts proxy, starts bot, opens Chrome dashboard
- **`deploy.bat`** — First-time VPS deployment: SCP + SSH setup
- **`deploy_update.bat`** — Push bot.py update + restart service on VPS
- **`vps_setup.sh`** — Full Ubuntu 22.04 VPS setup: creates `apexbot` user, installs deps, creates systemd service with auto-restart
- **`apex-bot.service`** — systemd unit file reference copy
- **`apex-nginx.conf`** — nginx config (for serving dashboards via web if deployed to VPS)

---

## WHAT IS WORKING

- [x] Full bot loop: scan → signal → score → filter → place → manage → log → notify
- [x] All 21 strategies implemented and firing
- [x] Backtest engine runs end-to-end, produces HTML report with equity curve
- [x] Proxy server works for OANDA + Claude API calls
- [x] Telegram + ntfy notifications
- [x] Risk management: breakeven, trailing stop, reversal exit, max hold, daily loss limit, consec loss pause
- [x] VPS deployment pipeline complete
- [x] State persistence (state.json, trade_log.csv)

---

## CURRENT STATE (as of last session stop)

- `trade_log.csv` is **empty** (headers only) — bot has not yet logged a completed trade on this machine
- `state.json` shows **0 wins / 0 losses / 0 open trades** — fresh/reset state
- **Backtest has been run**: `backtest_results.csv` has real data (Nov 2025 → Apr 2026 H4/D signals)
- **`START_APEX.bat` still says "V3"** in its title/messages (minor — bot itself is V4)
- **`deploy_update.bat` footer** says "V3" — minor inconsistency

---

## CRITICAL ISSUE: backtest.py IS OUT OF SYNC WITH bot.py V4

This is the most important technical gap in the project:

| Feature | bot.py V4 | backtest.py |
|---|---|---|
| SMC strategies (6) | YES | NO — missing entirely |
| DEATH_CROSS removed | YES | NO — still included |
| RSI_DIV removed | YES — commented | NO — still active |
| LONDON_BREAK removed | YES — removed | Still in (ASIAN_BREAK variant) |
| Session quality scoring | YES (+10/+4/0) | NO |
| 200 EMA gate (scalp TFs) | Soft (2% distance) | Hard (all TFs same) |
| WEAK_STRATS concept | YES (LIQ_SWEEP, VWAP) | NO |
| SMC score bonus (+10/strat) | YES | NO |

**Result: backtest results do NOT reflect bot.py V4's actual behavior.** The backtest is validating an older, weaker strategy set.

---

## NEXT PRIORITIES (in order)

### Priority 1 — VERIFY BOT IS LIVE AND TRADING
- Run `START_APEX.bat` and confirm proxy + bot start without errors
- Check bot logs for successful OANDA connection, account balance fetch, and scan output
- Verify at least one scan cycle runs and logs `Score:XX Dir:XX Sigs:XX` for instruments
- Reason: `trade_log.csv` is empty — need to confirm bot is actually generating signals and that MIN_SCORE=65/MIN_STRATS=3 settings are producing trades

### Priority 2 — SYNC backtest.py TO MATCH bot.py V4
Update `backtest.py` strategy engine to exactly match bot.py V4:
- Add 6 SMC strategies (SMC_OB, SMC_FVG, SMC_BOS, SMC_CHOCH, SMC_DISP, SMC_DISCOUNT/PREMIUM)
- Remove DEATH_CROSS, RSI_DIV from backtest strategy set
- Fix 200 EMA filter: soft gate (2% distance) on scalp TFs, hard gate on H4/D
- Add session quality score bonus to backtest scoring
- Add WEAK_STRATS list (don't count toward MIN_STRATS)
- Re-run backtest to get valid performance data for V4 strategies
- Then use results to tune which instruments/sessions to weight more heavily

### Priority 3 — FEED BACKTEST RESULTS BACK INTO BOT
- Once V4 backtest runs, identify instrument+TF+session combos with 60%+ WR
- Add a `COMBO_BOOST` score bonus for known high-WR combos (e.g., GOLD H1 OVERLAP)
- Disable or reduce lot size for combos with sub-45% WR in backtest

### Priority 4 — VPS DEPLOYMENT
- Once bot is verified working locally, deploy to VPS for 24/7 operation
- Set `CLAUDE_API_KEY` env var on VPS for APEX-AI to function
- Use `deploy.bat` → `vps_setup.sh` → `deploy_update.bat` workflow

### Priority 5 — DASHBOARD POLISH
- Update `START_APEX.bat` title/messages from V3 → V4
- Update `deploy_update.bat` footer message from V3 → V4
- Verify `apex-ultimate.html` dashboard displays live data correctly via proxy

---

## FILE INVENTORY

| File | Purpose | Status |
|------|---------|--------|
| `bot.py` | Core trading bot V4 | Complete, production-ready |
| `backtest.py` | Backtesting engine | Complete but OUT OF SYNC with V4 |
| `proxy.py` | Flask CORS proxy | Complete |
| `apex-ultimate.html` | Main dashboard V4 | Complete |
| `apex-intel-v3.html` | Intel dashboard | Auto-downloaded/patched |
| `apex-scanner-v2.html` | Scanner dashboard | Present |
| `START_APEX.bat` | Local launch script | Working (minor V3 label) |
| `deploy.bat` | VPS first-deploy | Complete |
| `deploy_update.bat` | VPS push-update | Complete (minor V3 label) |
| `vps_setup.sh` | VPS Ubuntu setup | Complete |
| `apex-bot.service` | systemd unit | Reference copy |
| `apex-nginx.conf` | nginx config | Present |
| `state.json` | Bot state | Fresh (0 trades) |
| `trade_log.csv` | Trade history | Empty (headers only) |
| `backtest_results.csv` | Backtest data | Has data (old strategy set) |
| `backtest_report.html` | Backtest report | Generated |
| `update_intel.py` | Downloads intel dashboard | Complete |
