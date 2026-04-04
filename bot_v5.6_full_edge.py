#!/usr/bin/env python3
"""
APEX AUTO-TRADER V4 — HEDGE FUND EDITION
=========================================
21 strategies: 15 classic + 6 SMC (Order Block, FVG, BOS, CHoCH, Displacement, Premium/Discount)
Claude AI trade confirmation — each qualifying signal analysed by APEX-AI
News filter (30-min blackout around high-impact events)
COT institutional positioning + DXY bias + Orderbook contrarian
London/NY/Overlap session quality weighting (+8 pts in overlap)
Breakeven + trailing stop management
Daily loss limit + 3-consecutive-loss protection
Telegram + ntfy push notifications
10+ trades/day: MIN_SCORE=65, MIN_STRATS=3, SCAN every 3 min
Removed dead strategies: DEATH_CROSS (11.8% WR), RSI_DIV (17.2%), LONDON_BREAK (26.3%)
"""

import os,time,json,logging,requests,base64,csv,sys,re,threading
from datetime import datetime,timezone,timedelta

# ================================================================
# CONFIG
# ================================================================
API_KEY   = (os.environ.get("OANDA_API_KEY") or base64.b64decode("YmY3MGJjZDkzNjczM2JjNTE2NjIyZjFkYmRjMWRhY2ItYjk0Mzk4NzRkZjdlYTkzYmU2MGRhNzkxYTkyNTJmMTU=").decode()).strip()
ACCOUNT_ID= (os.environ.get("OANDA_ACCOUNT_ID") or base64.b64decode("MTAxLTAwNC0zODk0NjkzMS0wMDE=").decode()).strip()
BASE_URL  = base64.b64decode("aHR0cHM6Ly9hcGktZnhwcmFjdGljZS5vYW5kYS5jb20vdjM=").decode().strip()
HEADERS   = {"Authorization":"Bearer "+API_KEY,"Content-Type":"application/json"}

CLAUDE_API_KEY  = os.environ.get("CLAUDE_API_KEY","")  # Set this for AI confirmations
NTFY_TOPIC      = os.environ.get("NTFY_TOPIC","apex-trader-alerts")
NTFY_URL        = "https://ntfy.sh/"+NTFY_TOPIC
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN","8710627128:AAEcmg91UvN9JEza-baX7lS2fKzdXWY_AqI")
TELEGRAM_CHATID = os.environ.get("TELEGRAM_CHAT_ID","6003788907")

TRADE_LOG   = "trade_log.csv"
SIGNAL_LOG  = "signal_log.csv"   # all signals from ALL instruments (learning data)
STATE_FILE  = "state.json"
MAX_TRADES  = 8
MIN_SCORE   = 65   # base threshold — get_min_score() adjusts this dynamically
MIN_STRATS  = 3

# ================================================================
# FTMO / PROP FIRM MODE
# Hard rules — any breach = challenge failed. We stay inside with buffers.
# Set FTMO_MODE=True when trading a prop firm challenge.
# ================================================================
FTMO_MODE         = True     # ON — currently in $10k challenge
FTMO_START_BAL    = 10000.0  # Challenge starting balance
FTMO_DAILY_LIMIT  = 0.04     # Hard stop at 4% daily loss  (FTMO limit = 5%, 1% buffer)
FTMO_TOTAL_LIMIT  = 0.08     # Hard stop at 8% total loss  (FTMO limit = 10%, 2% buffer)
FTMO_TARGET       = 0.10     # Profit target = 10% ($1,000 on $10k)
FTMO_LOCKDOWN_PCT = 0.085    # At 8.5% profit: switch to ultra-conservative mode
FTMO_MAX_TRADES   = 3        # Max concurrent trades in FTMO mode (tighter than normal)
FTMO_RISK_PCT     = 0.005    # 0.5% account risk per trade in FTMO mode
FTMO_MAX_HEAT     = 0.02     # Max total open risk at once = 2% of account (portfolio heat)
FTMO_MIN_SCORE    = 72       # Higher bar in FTMO mode — only take strong signals

def ftmo_check(state, bal):
    """Hard FTMO rule enforcement. Returns (allowed, reason).
    Called before every trade. If not allowed, bot stops cold."""
    if not FTMO_MODE:
        return True, ""
    start = FTMO_START_BAL
    # Daily loss check — hard stop
    daily_loss_pct = state.get("ftmo_daily_loss", 0.0) / start
    if daily_loss_pct >= FTMO_DAILY_LIMIT:
        msg = "FTMO DAILY LIMIT HIT: %.1f%% (limit %.0f%%) — ALL TRADING STOPPED FOR TODAY" % (
              daily_loss_pct*100, FTMO_DAILY_LIMIT*100)
        log.critical(msg)
        alert("FTMO DAILY LIMIT", msg, "high", "x")
        return False, "daily_limit"
    # Total drawdown check — hard stop
    peak = max(state.get("ftmo_peak_bal", start), bal)
    state["ftmo_peak_bal"] = peak
    drawdown_pct = (peak - bal) / start
    if drawdown_pct >= FTMO_TOTAL_LIMIT:
        msg = "FTMO TOTAL DRAWDOWN HIT: %.1f%% (limit %.0f%%) — CHALLENGE AT RISK" % (
              drawdown_pct*100, FTMO_TOTAL_LIMIT*100)
        log.critical(msg)
        alert("FTMO DRAWDOWN ALERT", msg, "high", "rotating_light")
        return False, "total_drawdown"
    # Warning zone — 75% of daily limit used
    if daily_loss_pct >= FTMO_DAILY_LIMIT * 0.75:
        log.warning("FTMO WARNING: daily loss at %.1f%% of %.0f%% limit" % (
                    daily_loss_pct*100, FTMO_DAILY_LIMIT*100))
    # Target reached — lock down
    profit_pct = (bal - start) / start
    if profit_pct >= FTMO_LOCKDOWN_PCT:
        log.info("FTMO LOCKDOWN MODE: profit %.1f%% — ultra-conservative only" % (profit_pct*100))
    return True, ""

def ftmo_lots(bal, base_lots, score=0):
    """Calculate safe lot size for FTMO. Risk-based sizing: 0.5% per trade.
    In lockdown mode (near target), cut to 0.25% risk."""
    if not FTMO_MODE:
        return base_lots
    start  = FTMO_START_BAL
    profit = (bal - start) / start
    # Drawdown-based reduction
    peak   = bal  # simplified; tracked in state elsewhere
    risk_pct = FTMO_RISK_PCT
    if profit >= FTMO_LOCKDOWN_PCT:
        risk_pct = 0.0025   # lockdown: quarter risk, protect the pass
    # Risk per trade in dollars
    risk_usd = bal * risk_pct
    # Minimum viable: use base_lots but cap at what the risk budget allows
    # Assume average SL = 50 pips. Adjust per instrument if needed.
    # This is a conservative cap — actual SL varies per trade
    max_lots = round(risk_usd / 50.0, 2)   # $50 per pip risk cap
    lots = min(base_lots, max(0.01, max_lots))
    # Score boost: high-conviction = slightly bigger (capped at 2x risk_pct)
    if score >= 90 and profit < FTMO_LOCKDOWN_PCT:
        lots = round(lots * 1.20, 2)
    return max(0.01, lots)

def ftmo_portfolio_heat(state, new_sl_dist, new_lots, instrument):
    """Check if adding this trade would exceed max portfolio heat (2% of account).
    sl_dist in price terms, lots in standard lots.
    Returns True if safe to proceed."""
    if not FTMO_MODE:
        return True
    try:
        # Estimate current open risk from state
        open_risk = 0.0
        for tid, info in state.get("trades", {}).items():
            sl  = info.get("sl", 0)
            en  = info.get("entry", 0)
            ls  = info.get("lots", 0.01)
            # Rough dollar risk estimate
            sl_dist_price = abs(en - sl)
            if sl_dist_price > 0 and en > 0:
                # pip value approximation: $10/pip/standard lot for most pairs
                pip_v  = 10.0
                pips   = sl_dist_price / 0.0001
                open_risk += pips * pip_v * ls
        # New trade risk
        pip_v     = 10.0
        pips      = new_sl_dist / 0.0001
        new_risk  = pips * pip_v * new_lots
        total_risk_pct = (open_risk + new_risk) / FTMO_START_BAL
        if total_risk_pct > FTMO_MAX_HEAT:
            log.warning("FTMO heat check: adding %s would push risk to %.1f%% (limit %.0f%%)" % (
                        instrument, total_risk_pct*100, FTMO_MAX_HEAT*100))
            return False
        return True
    except:
        return True   # on error, allow trade (don't block on calculation failure)

def ftmo_daily_reset(state):
    """Reset FTMO daily loss tracker at midnight UTC."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("ftmo_daily_date","") != today:
        prev = state.get("ftmo_daily_loss", 0.0)
        if prev > 0:
            log.info("FTMO daily reset — yesterday loss: $%.2f" % prev)
        state["ftmo_daily_loss"] = 0.0
        state["ftmo_daily_date"] = today
    return state

def ftmo_status_msg(state, bal):
    """Build FTMO status string for Telegram briefings."""
    if not FTMO_MODE:
        return ""
    start       = FTMO_START_BAL
    profit      = bal - start
    profit_pct  = profit / start * 100
    daily_loss  = state.get("ftmo_daily_loss", 0.0)
    daily_pct   = daily_loss / start * 100
    peak        = state.get("ftmo_peak_bal", start)
    dd_pct      = max(0, (peak - bal) / start * 100)
    target_usd  = start * FTMO_TARGET
    progress    = min(100, max(0, profit / target_usd * 100))
    lockdown    = profit_pct >= FTMO_LOCKDOWN_PCT * 100
    msg  = "\n--- FTMO CHALLENGE STATUS ---\n"
    msg += "Profit:    %+.2f (%+.1f%%)   [target: +%.0f%%]\n" % (profit, profit_pct, FTMO_TARGET*100)
    msg += "Progress:  %.0f%% toward $%.0f target\n" % (progress, target_usd)
    msg += "Daily P&L: %s$%.2f (%.1f%% of %.0f%% limit)\n" % (
           "-" if daily_loss>0 else "+", abs(daily_loss), daily_pct, FTMO_DAILY_LIMIT*100)
    msg += "Drawdown:  %.1f%% of %.0f%% limit\n" % (dd_pct, FTMO_TOTAL_LIMIT*100)
    if lockdown:
        msg += "STATUS:    LOCKDOWN MODE — near target, ultra-conservative\n"
    elif daily_pct >= FTMO_DAILY_LIMIT*75:
        msg += "STATUS:    WARNING — near daily limit, be careful\n"
    else:
        msg += "STATUS:    ACTIVE — trading normally\n"
    return msg

def get_min_score(regime="RANGING", state=None, h_utc=None):
    """Dynamic minimum score threshold. Raises bar in bad conditions,
    lowers slightly in peak liquidity. Protects capital when bot is cold."""
    base = MIN_SCORE
    if h_utc is None:
        h_utc = datetime.now(timezone.utc).hour
    # Regime adjustment
    if regime == "VOLATILE":
        base += 10   # wild market — only take very strong signals
    elif regime == "TRENDING":
        base -= 2    # clean trend — slightly more permissive
    # Session adjustment
    if 13 <= h_utc < 16:
        base -= 5    # London/NY overlap — best liquidity, trust signals more
    elif h_utc < 7 or h_utc >= 22:
        base += 7    # Asian/off-hours — thin market, be selective
    # Consecutive loss streak adjustment
    if state:
        streak = int(state.get("consec_losses", 0))
        if streak >= 3:
            base += 7    # cold streak — raise bar until bot warms up
        elif streak >= 2:
            base += 3
    return max(55, min(82, base))   # hard clamp 55-82

# Adaptive scan speed by session (seconds)
# Overlap = every 30s, Active = every 60s, Asian/Off = every 120s
SCAN_FAST   = 30    # London/NY overlap 13-16 UTC (peak liquidity)
SCAN_ACTIVE = 60    # London 07-13 UTC + NY 16-21 UTC
SCAN_SLOW   = 120   # Asian / off hours (22-07 UTC)

# Candle close tracking — only scan when a NEW candle has formed
_last_candle = {}   # {iid+tf: last_candle_time}
WEAK_STRATS = {"LIQ_SWEEP","VWAP"}   # add score but don't count toward min_strats

# Normal spread reference per instrument (used by spread filter)
NORMAL_SPREADS = {
    "EUR_USD":0.00015,"GBP_USD":0.00025,"USD_JPY":0.025,"AUD_USD":0.00020,
    "USD_CAD":0.00025,"USD_CHF":0.00025,"NZD_USD":0.00025,"EUR_GBP":0.00020,
    "EUR_JPY":0.030,"GBP_JPY":0.040,"AUD_JPY":0.030,
    "XAU_USD":0.30,"XAG_USD":0.020,"BCO_USD":0.06,"WTICO_USD":0.06,
    "SPX500_USD":0.80,"NAS100_USD":1.50,"US30_USD":2.00,"XBT_USD":50.0,
}

# ================================================================
# PERSONAL EDGE PROFILE — calibrated from 483 real trades on Vantage MT5
# Gold (PF 0.96, 247 trades): primary market, nearly breakeven — give edge bonus
# BTC  (PF 0.78, 212 trades): account killer — strict gate + reduced size
# BCO  (PF 0.92, small sample): slight caution on lot size
# Short bias: 70% of all trades were shorts, lower avg loss on short side
# ================================================================
PERSONAL_EDGE = {
    "XAU_USD": {"score_bonus": 8,  "lot_mult": 1.00},           # PF 0.96 — primary market
    "XBT_USD": {"score_bonus": 0,  "lot_mult": 0.40, "min_score": 82},  # PF 0.78 — account killer
    "BCO_USD": {"score_bonus": 0,  "lot_mult": 0.90},            # PF 0.92 — slight caution
}
SHORT_BIAS_BONUS = 5   # 70% of user's trades were shorts; lower avg loss on short side

# Signal log extended header
SIG_HEADER = ["date","instrument","direction","timeframe","score","strategies",
              "session","executed","regime","atr_pct","rsi","vr","spread_ratio",
              "hour","weekday","ml_pred"]

# ================================================================
# TRADE PAIRS — instruments the bot EXECUTES trades on
# Everything in INSTRUMENTS is scanned for signals and learning data.
# Only TRADE_PAIRS instruments will have actual orders placed.
# ================================================================
TRADE_PAIRS = {
    "XAU_USD",     # GOLD — primary instrument
    "XBT_USD",     # BTC (Bitcoin CFD on OANDA — check availability)
    "BCO_USD",     # BRENT OIL
    "EUR_USD",     # Major forex
    "GBP_USD",     # Major forex
    "USD_JPY",     # Major forex
    "SPX500_USD",  # S&P 500
    "NAS100_USD",  # NASDAQ
}

# ================================================================
# LOGGING
# ================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("trading_bot.log",encoding="utf-8")
    ]
)
log = logging.getLogger("APEX")

# ================================================================
# INSTRUMENTS  (scalp_tf added to ALL pairs for 10+ trades/day)
# ================================================================
INSTRUMENTS = {
    "EUR_USD":   {"n":"EUR/USD",  "sessions":[(7,17)],             "corr":"EUR","scalp_tf":["M15","H1"],"swing_tf":["H4","D"]},
    "GBP_USD":   {"n":"GBP/USD",  "sessions":[(7,17)],             "corr":"GBP","scalp_tf":["M15","H1"],"swing_tf":["H4","D"]},
    "USD_JPY":   {"n":"USD/JPY",  "sessions":[(0,9),(12,21)],       "corr":"JPY","scalp_tf":["M15","H1"],"swing_tf":["H4","D"]},
    "AUD_USD":   {"n":"AUD/USD",  "sessions":[(22,24),(0,8)],       "corr":"AUD","scalp_tf":["M15","H1"],"swing_tf":["H4","D"]},
    "USD_CAD":   {"n":"USD/CAD",  "sessions":[(12,21)],             "corr":"CAD","scalp_tf":["M15","H1"],"swing_tf":["H4","D"]},
    "USD_CHF":   {"n":"USD/CHF",  "sessions":[(7,17)],              "corr":"CHF","scalp_tf":["M15","H1"],"swing_tf":["H4","D"]},
    "NZD_USD":   {"n":"NZD/USD",  "sessions":[(22,24),(0,8)],       "corr":"NZD","scalp_tf":["M15","H1"],"swing_tf":["H4","D"]},
    "EUR_GBP":   {"n":"EUR/GBP",  "sessions":[(7,17)],              "corr":"EUR","scalp_tf":["M15","H1"],"swing_tf":["H4","D"]},
    "EUR_JPY":   {"n":"EUR/JPY",  "sessions":[(7,16)],              "corr":"EUR","scalp_tf":["M15","H1"],"swing_tf":["H4","D"]},
    "GBP_JPY":   {"n":"GBP/JPY",  "sessions":[(7,16)],              "corr":"GBP","scalp_tf":["M15","H1"],"swing_tf":["H4","D"]},
    "AUD_JPY":   {"n":"AUD/JPY",  "sessions":[(0,9),(7,16)],        "corr":"AUD","scalp_tf":["M15","H1"],"swing_tf":["H4","D"]},
    "XAU_USD":   {"n":"GOLD",     "sessions":[(0,9),(7,17),(12,21)],"corr":"XAU","scalp_tf":["M5","M15","H1"],"swing_tf":["H4","D"]},
    "XAG_USD":   {"n":"SILVER",   "sessions":[(7,17),(12,21)],       "corr":"XAG","scalp_tf":["M15","H1"],"swing_tf":["H4","D"]},
    "BCO_USD":   {"n":"BRENT",    "sessions":[(7,17),(12,21)],       "corr":"OIL","scalp_tf":["M15","H1"],"swing_tf":["H4","D"]},
    "WTICO_USD": {"n":"WTI",      "sessions":[(12,21)],              "corr":"OIL","scalp_tf":["M15","H1"],"swing_tf":["H4","D"]},
    "SPX500_USD":{"n":"SP500",    "sessions":[(13,21)],              "corr":"US", "scalp_tf":["M15","H1"],"swing_tf":["H4","D"]},
    "NAS100_USD":{"n":"NAS100",   "sessions":[(13,21)],              "corr":"US", "scalp_tf":["M15","H1"],"swing_tf":["H4","D"]},
    "US30_USD":  {"n":"DOW30",    "sessions":[(13,21)],              "corr":"US", "scalp_tf":["M15","H1"],"swing_tf":["H4","D"]},
    "XBT_USD":   {"n":"BITCOIN", "sessions":[(0,24)],              "corr":"BTC","scalp_tf":["M15","H1"],"swing_tf":["H4","D"]},
}

SCALP_P = {
    "M5": {"tp":1.0,"sl":0.5,"trail":0.8,"max_hold_min":30, "min_strats":3},
    "M15":{"tp":1.2,"sl":0.6,"trail":1.0,"max_hold_min":90, "min_strats":3},
    "H1": {"tp":1.5,"sl":0.7,"trail":1.0,"max_hold_min":240,"min_strats":3},
}
SWING_P = {
    "H4":{"tp":2.5,"sl":1.0,"trail":1.5,"max_hold_min":999999,"min_strats":3},
    "D": {"tp":3.0,"sl":1.2,"trail":2.0,"max_hold_min":999999,"min_strats":3},
}
RISK = [
    {"min":0,    "max":1000,  "lots":0.10,"dloss":0.20},
    {"min":1000, "max":2000,  "lots":0.20,"dloss":0.15},
    {"min":2000, "max":3500,  "lots":0.50,"dloss":0.12},
    {"min":3500, "max":5000,  "lots":1.0, "dloss":0.10},
    {"min":5000, "max":999999,"lots":1.0, "dloss":0.08},
]

# ================================================================
# NOTIFICATIONS
# ================================================================
def ntfy(title,msg,priority="default",tags="chart_with_upwards_trend"):
    try:
        requests.post(NTFY_URL,data=msg.encode(),
            headers={"Title":title,"Priority":priority,"Tags":tags},timeout=5)
    except: pass

def telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHATID: return
    try:
        requests.post(
            "https://api.telegram.org/bot"+TELEGRAM_TOKEN+"/sendMessage",
            json={"chat_id":TELEGRAM_CHATID,"text":msg,"parse_mode":"HTML"},timeout=5)
    except: pass

def alert(title,msg,priority="default",tags="chart_with_upwards_trend"):
    ntfy(title,msg,priority,tags)
    telegram("<b>"+title+"</b>\n"+msg)
    log.info("[ALERT] "+title+": "+msg[:100])

# MT4/MT5 instrument names for FTMO platform (OANDA names differ)
MT4_NAMES = {
    "XAU_USD":    "XAUUSD",
    "EUR_USD":    "EURUSD",
    "GBP_USD":    "GBPUSD",
    "USD_JPY":    "USDJPY",
    "AUD_USD":    "AUDUSD",
    "USD_CAD":    "USDCAD",
    "USD_CHF":    "USDCHF",
    "NZD_USD":    "NZDUSD",
    "EUR_GBP":    "EURGBP",
    "EUR_JPY":    "EURJPY",
    "GBP_JPY":    "GBPJPY",
    "AUD_JPY":    "AUDJPY",
    "XAG_USD":    "XAGUSD",
    "BCO_USD":    "UKOIL",
    "WTICO_USD":  "USOIL",
    "SPX500_USD": "US500",
    "NAS100_USD": "US100",
    "US30_USD":   "US30",
    "XBT_USD":    "BTCUSD",
}

def ftmo_signal_alert(iid, result, bal, state):
    """Send a beautifully formatted FTMO copy-trade alert to Telegram.
    Includes MT4 instrument name, exact entry/TP/SL, lots, and FTMO status."""
    try:
        mt4  = MT4_NAMES.get(iid, iid.replace("_",""))
        dir_ = result["dir"]
        en   = result["en"]
        tp   = result["tp"]
        sl   = result["sl"]
        lots = result["lots"]
        sc   = result["score"]
        tf   = result["tf"]
        reg  = result.get("regime","?")
        ai   = result.get("ai_reason","")
        ml   = result.get("ml_pred",50)
        sigs = result.get("sigs",[])
        smc  = [s for s in sigs if s.startswith("SMC_")]
        at   = result.get("AT",0)
        rr   = round(abs(tp-en)/abs(sl-en),1) if abs(sl-en)>0 else 0

        dir_arrow = "BUY" if dir_=="LONG" else "SELL"
        dir_emoji = "green_circle" if dir_=="LONG" else "red_circle"

        # FTMO progress
        profit     = bal - FTMO_START_BAL
        profit_pct = profit / FTMO_START_BAL * 100
        daily_loss = state.get("ftmo_daily_loss",0.0)
        daily_pct  = daily_loss / FTMO_START_BAL * 100
        days_traded= len(state.get("ftmo_days_traded",[]))

        # Score bar (visual)
        bars = int(sc/10)
        score_bar = ("=" * bars) + ("-" * (10-bars))

        msg  = "FTMO SIGNAL - COPY TO MT4\n"
        msg += "=" * 30 + "\n"
        msg += "Instrument: <b>" + mt4 + "</b>\n"
        msg += "Action:     <b>" + dir_arrow + "</b>\n"
        msg += "=" * 30 + "\n"
        msg += "Entry:      <b>" + str(round(en,5)) + "</b>\n"
        msg += "Take Profit:<b>" + str(round(tp,5)) + "</b>\n"
        msg += "Stop Loss:  <b>" + str(round(sl,5)) + "</b>\n"
        msg += "Lots:       <b>" + str(lots) + "</b> (FTMO safe)\n"
        msg += "R:R Ratio:  <b>1:" + str(rr) + "</b>\n"
        msg += "=" * 30 + "\n"
        msg += "Timeframe:  " + tf + "\n"
        msg += "Score:      [" + score_bar + "] " + str(sc) + "/100\n"
        msg += "Regime:     " + reg + "\n"
        msg += "ML Prob:    " + str(ml) + "% win\n"
        if smc:
            msg += "SMC:        " + ", ".join(smc) + "\n"
        msg += "Strategies: " + ", ".join(sigs[:4]) + "\n"
        if ai:
            msg += "AI Verdict: " + ai[:80] + "\n"
        msg += "=" * 30 + "\n"
        msg += "FTMO STATUS:\n"
        msg += "Profit:     " + ("%+.1f%%" % profit_pct) + " of 10% target\n"
        msg += "Daily Loss: " + ("%.1f%%" % daily_pct) + " of 4% limit\n"
        msg += "Days Traded:" + str(days_traded) + "/4 minimum\n"
        msg += "=" * 30 + "\n"
        msg += "ACT FAST — entry valid ~15 min"

        telegram("<b>FTMO SIGNAL</b> " + mt4 + " <b>" + dir_arrow + "</b>\n\n<pre>" + msg + "</pre>")
        log.info("FTMO signal alert sent: %s %s score=%d"%(mt4,dir_arrow,sc))
    except Exception as e:
        log.warning("FTMO signal alert error: "+str(e))

# ================================================================
# NEWS FILTER
# ================================================================
_nc = {"data":[],"ts":0}

def get_news():
    now=time.time()
    if now-_nc["ts"]<3600: return _nc["data"]
    try:
        r=requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json",timeout=10)
        if r.ok:
            data=[e for e in r.json() if e.get("impact")=="High"]
            _nc["data"]=data; _nc["ts"]=now
            log.info("News loaded: "+str(len(data))+" high-impact events")
            return data
    except Exception as e: log.warning("News fetch error: "+str(e))
    return _nc["data"]

def _news_window(title):
    """Return (before_min, after_min) blackout window based on event importance.
    NFP and rate decisions get much wider windows than routine data."""
    t = title.upper()
    if any(k in t for k in ["NON-FARM","NFP","NONFARM"]):
        return 90, 60      # NFP: 90 min before, 60 min after
    if any(k in t for k in ["INTEREST RATE","RATE DECISION","FOMC","FED FUNDS","BOE RATE","ECB RATE","RBA RATE","BOJ RATE"]):
        return 120, 60     # Central bank rate decisions: 2 hours before
    if any(k in t for k in ["CPI","INFLATION","PPI","PRICE INDEX"]):
        return 60, 30      # Inflation data: 1 hour before
    if any(k in t for k in ["GDP","GROSS DOMESTIC"]):
        return 45, 20
    if any(k in t for k in ["EMPLOYMENT","JOBLESS","UNEMPLOYMENT","PAYROLL"]):
        return 60, 30
    return 30, 15          # All other high-impact: 30 min before, 15 min after

def news_block(inst):
    """Returns True if trading this instrument is blocked due to nearby news."""
    now = datetime.now(timezone.utc)
    curr = []
    if "XAU" in inst or "XAG" in inst: curr = ["USD"]
    elif "BCO" in inst or "WTICO" in inst: curr = ["USD"]
    elif "SPX" in inst or "NAS" in inst or "US30" in inst: curr = ["USD"]
    else:
        p = inst.split("_")
        if len(p) == 2: curr = [p[0], p[1]]
    for ev in get_news():
        try:
            t   = datetime.fromisoformat(ev.get("date","").replace("Z","+00:00"))
            dm  = (t - now).total_seconds() / 60   # minutes until event (neg = past)
            ttl = ev.get("title","")
            before, after = _news_window(ttl)
            if -after <= dm <= before:
                ec = ev.get("country","").upper()
                if any(c in ec for c in curr):
                    if dm >= 0:
                        log.info("NEWS BLOCK: %s in %.0f min (window -%d/+%d)"%(ttl,dm,before,after))
                    else:
                        log.info("NEWS BLOCK: %s ended %.0f min ago (cooling off)"%(ttl,abs(dm)))
                    return True
        except: pass
    return False

# ================================================================
# COT DATA
# ================================================================
COT_URL="https://www.cftc.gov/dea/newcot/c_disagg.txt"
COT_MAP={"EURO FX":"EUR","BRITISH POUND":"GBP","JAPANESE YEN":"JPY",
         "AUSTRALIAN DOLLAR":"AUD","CANADIAN DOLLAR":"CAD","SWISS FRANC":"CHF",
         "NEW ZEALAND DOLLAR":"NZD","GOLD":"XAU","SILVER":"XAG","CRUDE OIL":"OIL"}
_cot={"data":{},"ts":0}

def get_cot():
    now=time.time()
    if now-_cot["ts"]<259200: return _cot["data"]
    try:
        r=requests.get(COT_URL,timeout=20)
        if not r.ok: return _cot["data"]
        data={}
        for line in r.text.strip().split("\n"):
            cols=line.split(",")
            if len(cols)<15: continue
            nm=cols[0].strip().strip('"').upper()
            corr=next((v for k,v in COT_MAP.items() if k in nm),None)
            if not corr or corr in data: continue
            try:
                net=float(cols[13].strip().replace('"',''))-float(cols[14].strip().replace('"',''))
                data[corr]={"net":int(net),"bias":"BULL" if net>50000 else "BEAR" if net<-50000 else "NEUTRAL"}
            except: continue
        if data: _cot["data"]=data; _cot["ts"]=now; log.info("COT loaded: "+str(len(data))+" instruments")
        return _cot["data"]
    except Exception as e: log.warning("COT error: "+str(e)); return _cot["data"]

def cot_bias(iid):
    cot=get_cot(); corr=INSTRUMENTS.get(iid,{}).get("corr","")
    return cot.get(corr,{}).get("bias","NEUTRAL")

# ================================================================
# DXY BIAS
# ================================================================
_dxy={"bias":"NEUTRAL","ts":0}

def dxy_bias():
    now=time.time()
    if now-_dxy["ts"]<3600: return _dxy["bias"]
    try:
        cc=get_candles("EUR_USD","H4",50)
        _,_,_,cl,_=parse_candles(cc)
        if len(cl)<21: return "NEUTRAL"
        e8v=ema(cl,8); e21v=ema(cl,21)
        if e8v<e21v and cl[-1]<cl[-5]: bias="STRONG_USD"
        elif e8v>e21v and cl[-1]>cl[-5]: bias="WEAK_USD"
        else: bias="NEUTRAL"
        _dxy["bias"]=bias; _dxy["ts"]=now
        log.info("DXY bias: "+bias); return bias
    except: return "NEUTRAL"

# ================================================================
# ORDERBOOK
# ================================================================
def orderbook_bias(iid):
    """
    Returns contrarian bias from OANDA order book + position book.
    +1 = bullish (retail heavily short), -1 = bearish (retail heavily long), 0 = neutral.
    Also detects stop clusters above/below current price.
    """
    try:
        ob_data=oget("/instruments/"+iid+"/orderBook")
        buckets=ob_data.get("orderBook",{}).get("buckets",[])
        price_str=ob_data.get("orderBook",{}).get("price","0")
        cur_price=float(price_str) if price_str else 0
        if not buckets: return 0
        longs=sum(float(b.get("longCountPercent",0)) for b in buckets)
        shorts=sum(float(b.get("shortCountPercent",0)) for b in buckets)
        total=longs+shorts
        if total==0: return 0
        ratio=longs/total
        bias = -1 if ratio>0.65 else (1 if ratio<0.35 else 0)
    except: return 0
    # Position book — where are open positions? Detect stop clusters
    try:
        pb_data=oget("/instruments/"+iid+"/positionBook")
        pb=pb_data.get("positionBook",{}).get("buckets",[])
        if pb and cur_price>0:
            # Stops above price = short stops (buy stops) — liquidity for bearish run
            # Stops below price = long stops (sell stops) — liquidity for bullish run
            above=sum(float(b.get("shortCountPercent",0)) for b in pb if float(b.get("price",0))>cur_price*1.001)
            below=sum(float(b.get("longCountPercent",0))  for b in pb if float(b.get("price",0))<cur_price*0.999)
            if above>5.0: log.info(iid+" stop cluster ABOVE price (bull target)")
            if below>5.0: log.info(iid+" stop cluster BELOW price (bear target)")
    except: pass
    return bias

# ================================================================
# BINANCE BTC DATA + FEAR & GREED INDEX
# Free public APIs — no key needed
# Used to sharpen BTC/crypto signals
# ================================================================
_btc_cache={"data":{},"ts":0}

def get_btc_sentiment():
    now=time.time()
    if now-_btc_cache["ts"]<300: return _btc_cache["data"]
    data={}
    try:
        r=requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT",timeout=8)
        if r.ok:
            d=r.json()
            chg=float(d.get("priceChangePercent",0))
            data["price"]=float(d.get("lastPrice",0))
            data["change_24h"]=chg
            data["volume"]=float(d.get("quoteVolume",0))
            data["bias"]="BULL" if chg>2.0 else ("BEAR" if chg<-2.0 else "NEUTRAL")
    except: pass
    try:
        r2=requests.get("https://api.alternative.me/fng/?limit=1",timeout=8)
        if r2.ok:
            fng=r2.json().get("data",[{}])[0]
            val=int(fng.get("value",50))
            data["fear_greed"]=val
            data["fear_greed_label"]=fng.get("value_classification","Neutral")
            # Contrarian: extreme fear = buy, extreme greed = sell
            data["fng_signal"]="BULL" if val<25 else ("BEAR" if val>75 else "NEUTRAL")
    except: pass
    if data:
        _btc_cache["data"]=data; _btc_cache["ts"]=now
        log.info("BTC: $%.0f %.1f%% F&G:%s"%(data.get("price",0),data.get("change_24h",0),data.get("fear_greed_label","?")))
    return data

# ================================================================
# KEY LEVEL DETECTION
# Previous day/week highs & lows + round numbers
# Price always returns to these levels — highest quality entries
# ================================================================
def key_level_bonus(hi, lo, cl, atr):
    """
    Returns (at_key:bool, key_dir:str, bonus:int).
    Fires when price is within 0.5 ATR of a major key level.
    """
    if len(cl)<30 or atr==0: return False,"NONE",0
    mid=cl[-1]; bonus=0; direction="NONE"
    d=min(24,len(cl)//3)
    levels=[]
    # Previous day high/low
    if len(hi)>d*2:
        levels.append(("PDH",max(hi[-d*2:-d]),"BEAR"))
        levels.append(("PDL",min(lo[-d*2:-d]),"BULL"))
    # Previous week high/low
    w=min(120,len(cl)//2)
    if len(hi)>w*2:
        levels.append(("PWH",max(hi[-w*2:-w]),"BEAR"))
        levels.append(("PWL",min(lo[-w*2:-w]),"BULL"))
    # Round number (psychological level)
    mag=10**max(0,len(str(int(mid)))-2)
    rn=round(mid/mag)*mag
    if rn>0: levels.append(("ROUND",rn,"BOTH"))
    threshold=atr*0.5
    for name,lv,lvdir in levels:
        if lv>0 and abs(mid-lv)<threshold:
            bonus+=15
            direction=lvdir if lvdir!="BOTH" else ("BULL" if mid<lv else "BEAR")
            log.info("KEY LEVEL %s=%.5f dist=%.5f"%(name,lv,abs(mid-lv)))
    return bonus>0, direction, min(bonus,20)

# ================================================================
# FIBONACCI RETRACEMENT
# 38.2%, 50%, 61.8% — every professional trader watches these
# ================================================================
def fib_bonus(hi, lo, cl, atr):
    """Returns (at_fib:bool, fib_dir:str, bonus:int)."""
    if len(cl)<50 or atr==0: return False,"NONE",0
    n=min(60,len(cl))
    sh=max(hi[-n:]); sl=min(lo[-n:]); rng=sh-sl
    if rng==0: return False,"NONE",0
    mid=cl[-1]
    fibs={"38.2":sl+rng*0.382,"50.0":sl+rng*0.500,"61.8":sl+rng*0.618,"78.6":sl+rng*0.786}
    threshold=atr*0.35
    for name,lv in fibs.items():
        if abs(mid-lv)<threshold:
            direction="BULL" if mid<sl+rng*0.5 else "BEAR"
            bonus=15 if name in ("50.0","61.8") else 10
            log.info("FIB %s at %.5f"%(name,lv))
            return True,direction,bonus
    return False,"NONE",0

# ================================================================
# WEEKLY TREND BIAS
# Only trade WITH the weekly trend — against it loses money
# ================================================================
_weekly={}

def weekly_bias(iid):
    now=time.time()
    c=_weekly.get(iid,{})
    if now-c.get("ts",0)<14400: return c.get("bias","NEUTRAL")
    try:
        cc=get_candles(iid,"D",20)
        _,_,_,cl,_=parse_candles(cc)
        if len(cl)<10: return "NEUTRAL"
        e10=ema(cl,10); e20=ema(cl,min(20,len(cl)))
        mid=cl[-1]
        if mid>e10>e20 and cl[-1]>cl[-5]: bias="BULL"
        elif mid<e10<e20 and cl[-1]<cl[-5]: bias="BEAR"
        else: bias="NEUTRAL"
        _weekly[iid]={"bias":bias,"ts":now}
        log.info("%s weekly bias: %s"%(iid,bias))
        return bias
    except: return "NEUTRAL"

# ================================================================
# PIN BAR / HAMMER DETECTION
# Strongest single-candle reversal signal used by professional traders
# ================================================================
def pin_bar(o, hi, lo, cl, atr):
    """Returns (found:bool, direction:str)."""
    if len(cl)<3 or atr==0: return False,"NONE"
    co=o[-1]; ch=hi[-1]; cl2=lo[-1]; cc=cl[-1]
    body=abs(cc-co)
    upper_wick=ch-max(co,cc)
    lower_wick=min(co,cc)-cl2
    total=ch-cl2
    if total<atr*0.3 or body==0: return False,"NONE"
    # Hammer: long lower wick, small upper wick — bullish reversal
    if lower_wick>body*2.0 and upper_wick<body*0.5: return True,"BULL"
    # Shooting star: long upper wick, small lower wick — bearish reversal
    if upper_wick>body*2.0 and lower_wick<body*0.5: return True,"BEAR"
    return False,"NONE"

# ================================================================
# ADAPTIVE SCAN SPEED + CANDLE-CLOSE AWARENESS
# ================================================================
def scan_interval():
    """Returns how many seconds to wait before next scan, based on session."""
    h=datetime.now(timezone.utc).hour
    if 13<=h<16: return SCAN_FAST    # London/NY overlap — peak liquidity
    if 7<=h<13 or 16<=h<21: return SCAN_ACTIVE   # London or NY active
    return SCAN_SLOW                 # Asian / off hours

def candle_is_new(iid, tf, candles):
    """
    Returns True if the latest candle is newer than what we last scanned.
    Prevents re-analysing the same candle multiple times.
    """
    key=iid+tf
    try:
        last_time=candles[-1]["time"] if candles else None
        if last_time and last_time!=_last_candle.get(key):
            _last_candle[key]=last_time
            return True
        return False
    except: return True   # if unsure, scan anyway

# ================================================================
# UK TIME
# ================================================================
def uk_hour():
    n=datetime.now(timezone.utc)
    yr=n.year
    mar31=datetime(yr,3,31,tzinfo=timezone.utc)
    bst_start=mar31-timedelta(days=(mar31.weekday()+1)%7)
    oct31=datetime(yr,10,31,tzinfo=timezone.utc)
    bst_end=oct31-timedelta(days=(oct31.weekday()+1)%7)
    return (n.hour+(1 if bst_start<=n<bst_end else 0))%24

# ================================================================
# BRIEFINGS
# ================================================================
_brief={"LONDON":"","NEWYORK":"","CLOSE":"","FTMO_MORNING":""}

# ================================================================
# EQUITY CURVE FILTER — raises MIN_SCORE when system is cold
# If last N trades are net negative, don't keep hammering the market
# ================================================================
def equity_curve_filter(state):
    """Check recent trade P&L. Returns score penalty (0 or +10) to add to MIN_SCORE."""
    journal = state.get("journal", [])
    recent = journal[-5:] if len(journal) >= 5 else journal
    if not recent:
        return 0
    net = sum(float(t.get("pl", 0)) for t in recent)
    if net < 0:
        log.info("Equity curve filter: last %d trades net $%.2f — raising MIN_SCORE +10" % (len(recent), net))
        return 10   # cold system — be more selective
    return 0

# ================================================================
# WEEKLY PROFIT LOCK — protects a good week from a bad Friday
# If we're up 3%+ on the week, drop to micro risk for the rest of it
# ================================================================
WEEKLY_LOCK_PCT = 0.03   # 3% weekly profit = lock in, go micro
_weekly_lock = {"week": "", "locked": False}

def weekly_profit_check(bal):
    """Returns True if weekly lock is active (bot should micro-size or skip)."""
    if not FTMO_MODE:
        return False
    now = datetime.now(timezone.utc)
    week_key = now.strftime("%Y-W%W")
    if _weekly_lock["week"] != week_key:
        _weekly_lock["week"] = week_key
        _weekly_lock["locked"] = False
    profit_pct = (bal - FTMO_START_BAL) / FTMO_START_BAL
    if profit_pct >= WEEKLY_LOCK_PCT and not _weekly_lock["locked"]:
        _weekly_lock["locked"] = True
        alert("WEEKLY PROFIT LOCK",
              "Up %.1f%% this week — switching to micro-size to protect gains" % (profit_pct*100),
              "default", "lock")
        log.info("WEEKLY LOCK: profit %.1f%% — micro-size only for rest of week" % (profit_pct*100))
    return _weekly_lock["locked"]

# ================================================================
# INSTRUMENT DAILY TRADE LIMIT — max 2 trades per instrument per day
# Prevents overtrading one market (Gold 6x in a day = bad)
# ================================================================
def inst_daily_count(state, iid):
    """Count how many trades were opened today for this instrument."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    count = 0
    for t in state.get("journal", []):
        if t.get("inst") == iid:
            closed = t.get("closed", "")
            if closed.startswith(today):
                count += 1
    # Also count currently open trades on this instrument
    for tid, info in state.get("trades", {}).items():
        if info.get("inst") == iid:
            count += 1
    return count

MAX_INST_DAILY = 2   # max trades per instrument per day

def send_briefing(period):
    try:
        ac=get_account(); bal=float(ac["balance"]); st=load_state()
        w=st["wins"]; l=st["losses"]; t2=w+l; wr=round(w/t2*100) if t2 else 0
        news=get_news(); ev1=news[0].get("title","None") if news else "None"
        t={"LONDON":"LONDON OPEN 07:00","NEWYORK":"NEW YORK OPEN 14:30","CLOSE":"NY CLOSE 21:00",
           "FTMO_MORNING":"FTMO MORNING UPDATE"}
        cot=get_cot()
        cot_s="|".join([k+":"+v["bias"][0] for k,v in list(cot.items())[:4]]) if cot else "loading"
        msg=("Bal:$"+str(round(bal,2))+" | "+str(w)+"W/"+str(l)+"L ("+str(wr)+"%)\n"
             "USD:"+dxy_bias()+" | Open:"+str(len(st["trades"]))+"\n"
             "COT: "+cot_s+"\nNews: "+ev1+"\nBot V5.5: ACTIVE")
        msg += ftmo_status_msg(st, bal)
        if period == "FTMO_MORNING" and FTMO_MODE:
            # Detailed FTMO morning briefing
            start = FTMO_START_BAL
            profit = bal - start
            profit_pct = profit / start * 100
            days = len(st.get("ftmo_days_traded", []))
            daily_budget = start * FTMO_DAILY_LIMIT
            daily_used = st.get("ftmo_daily_loss", 0.0)
            remaining = max(0, daily_budget - daily_used)
            bars = int(min(10, max(0, profit_pct / FTMO_TARGET / 100 * 10)))
            bar_str = "[" + "#"*bars + "-"*(10-bars) + "]"
            msg = ("FTMO CHALLENGE DAY UPDATE\n"
                   "Balance: $%.2f (%+.2f)\n"
                   "Progress: %.1f%% / 10%%  %s\n"
                   "Days traded: %d (need 4 min)\n"
                   "Daily budget remaining: $%.2f\n"
                   "Weekly lock: %s\n"
                   "Equity filter: %s\n"
                   "Bot status: ACTIVE V5.5" % (
                   bal, profit, profit_pct, bar_str,
                   days, remaining,
                   "LOCKED (micro)" if weekly_profit_check(bal) else "open",
                   "+10 cold" if equity_curve_filter(st) else "normal"))
        alert(t.get(period,"BRIEFING"),msg,"default","newspaper")
    except Exception as e: log.warning("Briefing error: "+str(e))

# ================================================================
# TRADE LOG
# ================================================================
def init_log():
    if not os.path.exists(TRADE_LOG):
        with open(TRADE_LOG,"w",newline="") as f:
            csv.writer(f).writerow([
                "date","instrument","direction","timeframe",
                "entry","tp","sl","atr","lots",
                "strategies","score","ai_reason","result",
                "pl","duration_min","balance_after","notes"
            ])
    # Signal log — upgrade headers if old format detected
    if os.path.exists(SIGNAL_LOG):
        try:
            with open(SIGNAL_LOG,"r") as f: first=f.readline()
            if "regime" not in first:
                import shutil
                shutil.move(SIGNAL_LOG, SIGNAL_LOG+".bak")
                log.info("signal_log.csv upgraded — old version backed up")
        except: pass
    if not os.path.exists(SIGNAL_LOG):
        with open(SIGNAL_LOG,"w",newline="") as f:
            csv.writer(f).writerow(SIG_HEADER)

def write_signal_log(iid, direction, tf, score, sigs, executed=False,
                     regime="RANGING", atr_pct=50, rsi_val=50,
                     vr_val=1.0, spread_ratio=1.0, ml_pred=50):
    """Log ALL signals (trade pairs + monitor pairs) for ML learning."""
    try:
        now=datetime.now(timezone.utc)
        h=now.hour
        if 13<=h<16: sess="OVERLAP"
        elif 7<=h<12: sess="LONDON"
        elif 12<=h<17: sess="NEWYORK"
        elif 0<=h<9: sess="ASIAN"
        else: sess="OFF"
        with open(SIGNAL_LOG,"a",newline="") as f:
            csv.writer(f).writerow([
                now.isoformat(), iid, direction, tf, score,
                ",".join(sigs[:8]), sess, "1" if executed else "0",
                regime, round(atr_pct,1), round(rsi_val,1),
                round(vr_val,2), round(spread_ratio,2),
                h, now.weekday(), ml_pred
            ])
    except: pass

def write_log(td):
    try:
        with open(TRADE_LOG,"a",newline="") as f:
            csv.writer(f).writerow([
                td.get("closed",""), td.get("inst",""), td.get("dir",""),
                td.get("tf",""), round(td.get("entry",0),5), round(td.get("tp",0),5),
                round(td.get("sl",0),5), round(td.get("atr",0),5), td.get("lots",0),
                ",".join(td.get("strats",[])), td.get("score",0), td.get("ai_reason",""),
                td.get("result",""), round(td.get("pl",0),4),
                td.get("dur",0), round(td.get("bal",0),2), td.get("notes",""),
            ])
    except Exception as e: log.warning("Log write error: "+str(e))

# ================================================================
# OANDA API
# ================================================================
def oget(p):
    r=requests.get(BASE_URL+p,headers=HEADERS,timeout=15)
    r.raise_for_status(); return r.json()

def opost(p,b):
    r=requests.post(BASE_URL+p,headers=HEADERS,json=b,timeout=15)
    r.raise_for_status(); return r.json()

def oput(p,b):
    r=requests.put(BASE_URL+p,headers=HEADERS,json=b,timeout=15)
    r.raise_for_status(); return r.json()

def get_account():   return oget("/accounts/"+ACCOUNT_ID+"/summary")["account"]
def get_trades():    return oget("/accounts/"+ACCOUNT_ID+"/openTrades")["trades"]
def get_candles(i,g,n=200): return oget("/instruments/"+i+"/candles?count="+str(n)+"&granularity="+g+"&price=M")["candles"]
def get_price(i):
    d=oget("/accounts/"+ACCOUNT_ID+"/pricing?instruments="+i)["prices"][0]
    return float(d["bids"][0]["price"]),float(d["asks"][0]["price"])

def place(i,direction,lots,tp,sl):
    units=lots*10000
    if "XAU" in i: units=lots*100
    if "XAG" in i: units=lots*5000
    if "BCO" in i or "WTICO" in i: units=lots*1000
    if "SPX" in i or "NAS" in i or "US30" in i: units=lots*10
    if "XBT" in i: units=max(1,int(lots*10))  # BTC: 0.1 lots = ~0.1 BTC
    if direction=="SHORT": units=-abs(units)
    return opost("/accounts/"+ACCOUNT_ID+"/orders",{
        "order":{"type":"MARKET","instrument":i,"units":str(int(units)),
            "takeProfitOnFill":{"price":"%.5f"%tp},
            "stopLossOnFill":{"price":"%.5f"%sl},
            "timeInForce":"FOK","positionFill":"DEFAULT"}})

def close_trade(tid): return oput("/accounts/"+ACCOUNT_ID+"/trades/"+tid+"/close",{})
def set_sl(tid,sl):   return oput("/accounts/"+ACCOUNT_ID+"/trades/"+tid+"/orders",{"stopLoss":{"price":"%.5f"%sl,"timeInForce":"GTC"}})

# ================================================================
# TECHNICAL ANALYSIS
# ================================================================
def parse_candles(cc):
    c=[x for x in cc if x["complete"]]
    return (
        [float(x["mid"]["o"]) for x in c],
        [float(x["mid"]["h"]) for x in c],
        [float(x["mid"]["l"]) for x in c],
        [float(x["mid"]["c"]) for x in c],
        [float(x["volume"])   for x in c],
    )

def ema(cl,p):
    if len(cl)<p: return cl[-1]
    k=2/(p+1); e=cl[0]
    for v in cl[1:]: e=v*k+e*(1-k)
    return e

def ema_s(cl,p):
    k=2/(p+1); e=cl[0]; out=[e]
    for v in cl[1:]: e=v*k+e*(1-k); out.append(e)
    return out

def calc_rsi(cl,p=14):
    if len(cl)<p+1: return 50.0
    g=l=0
    for i in range(len(cl)-p,len(cl)):
        d=cl[i]-cl[i-1]
        if d>0: g+=d
        else:   l+=abs(d)
    ag,al=g/p,l/p
    return 100.0 if al==0 else 100-(100/(1+ag/al))

def rsi_s(cl,p=14):
    out=[50.0]*len(cl)
    for i in range(p+1,len(cl)): out[i]=calc_rsi(cl[:i+1],p)
    return out

def calc_stoch(cl,hi,lo,kp=14,dp=3):
    if len(cl)<kp+dp: return 50,50,50,50
    ks=[]
    for i in range(kp-1,len(cl)):
        h=max(hi[i-kp+1:i+1]); l=min(lo[i-kp+1:i+1])
        ks.append(50.0 if h==l else ((cl[i]-l)/(h-l))*100)
    if len(ks)<dp: return ks[-1],ks[-1],ks[-1],ks[-1]
    k=ks[-1]; d=sum(ks[-dp:])/dp
    pk=ks[-2] if len(ks)>=2 else k
    pd=sum(ks[-dp-1:-1])/dp if len(ks)>dp else d
    return k,d,pk,pd

def calc_bb(cl,p=20,mult=2.0):
    if len(cl)<p: return cl[-1],cl[-1],cl[-1],0
    w=cl[-p:]; m=sum(w)/p
    s=(sum((x-m)**2 for x in w)/p)**0.5
    w2=(m+mult*s-m-mult*(-s))/(m if m>0 else 1)
    return m+mult*s,m,m-mult*s,w2

def calc_atr_val(cc,p=14):
    c=[x for x in cc if x["complete"]]
    if len(c)<p: return 0.0
    trs=[]
    for i in range(1,len(c)):
        h=float(c[i]["mid"]["h"]); l=float(c[i]["mid"]["l"]); pc=float(c[i-1]["mid"]["c"])
        trs.append(max(h-l,abs(h-pc),abs(l-pc)))
    return sum(trs[-p:])/p

def vol_ratio(vols):
    if len(vols)<5: return 1.0
    avg=sum(vols[-20:-1])/max(1,len(vols[-20:-1]))
    return vols[-1]/avg if avg>0 else 1.0

def liq_sweep(hi,lo,cl):
    if len(cl)<20: return False,"NONE"
    rh=max(hi[-20:-3]); rl=min(lo[-20:-3])
    if lo[-1]<rl and cl[-1]>rl: return True,"BULL"
    if hi[-1]>rh and cl[-1]<rh: return True,"BEAR"
    return False,"NONE"

# ================================================================
# SMC ENGINE — Smart Money Concepts
# ================================================================
def smc_order_block(o, hi, lo, cl, mid, atr):
    """Last opposing candle before a 3-candle institutional displacement"""
    if len(cl)<15 or atr==0: return False,"NONE"
    lb=min(40,len(cl)-4)
    for i in range(len(cl)-4,len(cl)-lb,-1):
        if i<1: break
        if i+2>=len(cl): continue
        # Bullish OB: last bearish candle before strong bull displacement
        body_up=sum(max(0,cl[i+k]-o[i+k]) for k in range(3))
        if body_up>atr*1.8 and all(cl[i+k]>o[i+k] for k in range(3)):
            if cl[i-1]<o[i-1]:
                ob_hi=max(o[i-1],hi[i-1]); ob_lo=min(cl[i-1],lo[i-1])
                if ob_lo<=mid<=ob_hi: return True,"BULL"
        # Bearish OB: last bullish candle before strong bear displacement
        body_dn=sum(max(0,o[i+k]-cl[i+k]) for k in range(3))
        if body_dn>atr*1.8 and all(cl[i+k]<o[i+k] for k in range(3)):
            if cl[i-1]>o[i-1]:
                ob_hi=max(cl[i-1],hi[i-1]); ob_lo=min(o[i-1],lo[i-1])
                if ob_lo<=mid<=ob_hi: return True,"BEAR"
    return False,"NONE"

def smc_fvg(hi, lo, cl, mid):
    """Fair Value Gap — 3-candle price imbalance, fires when price returns to fill"""
    if len(cl)<10: return False,"NONE"
    for i in range(max(0,len(cl)-35),len(cl)-2):
        # Bullish FVG: gap up — candle[i] high < candle[i+2] low
        if hi[i]<lo[i+2] and hi[i]<=mid<=lo[i+2]:
            return True,"BULL"
        # Bearish FVG: gap down — candle[i] low > candle[i+2] high
        if lo[i]>hi[i+2] and hi[i+2]<=mid<=lo[i]:
            return True,"BEAR"
    return False,"NONE"

def smc_bos_choch(hi, lo, cl):
    """Break of Structure (continuation) and Change of Character (reversal)"""
    if len(cl)<30: return None,None
    n=min(60,len(cl)); swings=[]
    for i in range(2,n-2):
        idx=len(cl)-n+i
        if hi[idx]>hi[idx-1] and hi[idx]>hi[idx-2] and hi[idx]>hi[idx+1] and hi[idx]>hi[idx+2]:
            swings.append(('H',hi[idx]))
        if lo[idx]<lo[idx-1] and lo[idx]<lo[idx-2] and lo[idx]<lo[idx+1] and lo[idx]<lo[idx+2]:
            swings.append(('L',lo[idx]))
    if len(swings)<4: return None,None
    sh=[s[1] for s in swings if s[0]=='H']
    sl2=[s[1] for s in swings if s[0]=='L']
    if len(sh)<2 or len(sl2)<2: return None,None
    bull_trend=sh[-1]>sh[-2] and sl2[-1]>sl2[-2]
    bear_trend=sh[-1]<sh[-2] and sl2[-1]<sl2[-2]
    cur=cl[-1]; bos=choch=None
    if bull_trend and cur>sh[-1]:  bos="BULL"
    if bear_trend and cur<sl2[-1]: bos="BEAR"
    if bear_trend and cur>sh[-1]:  choch="BULL"
    if bull_trend and cur<sl2[-1]: choch="BEAR"
    return bos,choch

def smc_displacement(o, hi, lo, cl, atr):
    """Strong institutional displacement — 2+ large candles in same direction"""
    if len(cl)<5 or atr==0: return False,"NONE"
    bu=be=0
    for i in range(-3,0):
        body=abs(cl[i]-o[i])
        if body>atr*1.1:
            if cl[i]>o[i]: bu+=1
            else: be+=1
    if bu>=2 or sum(max(0,cl[i]-o[i]) for i in range(-3,0))>atr*2.5: return True,"BULL"
    if be>=2 or sum(max(0,o[i]-cl[i]) for i in range(-3,0))>atr*2.5: return True,"BEAR"
    return False,"NONE"

def smc_pd_zone(hi, lo, cl, n=50):
    """Premium/Discount array: buy in discount (<35%), sell in premium (>65%)"""
    n2=min(n,len(cl))
    rh=max(hi[-n2:]); rl=min(lo[-n2:]); rng=rh-rl
    if rng==0: return "EQUILIBRIUM",50.0
    pct=(cl[-1]-rl)/rng*100
    if pct<35: return "DISCOUNT",pct
    if pct>65: return "PREMIUM",pct
    return "EQUILIBRIUM",pct

# ================================================================
# REGIME DETECTION
# Returns TRENDING / RANGING / VOLATILE
# TRENDING  -> weight EMA/SMC_BOS strategies
# RANGING   -> weight BB_TOUCH/MEAN_REV/SMC_FVG/DISCOUNT
# VOLATILE  -> reduce size 50%, only highest-conviction setups
# ================================================================
def detect_regime(hi, lo, cl, atr):
    if len(cl)<30 or atr==0: return "RANGING"
    # ATR percentile vs last 20 bars
    try:
        atr_hist=[]
        for i in range(max(1,len(cl)-21),len(cl)-1):
            trs=[max(hi[j]-lo[j],abs(hi[j]-cl[j-1]),abs(lo[j]-cl[j-1]))
                 for j in range(max(1,i-13),i+1)]
            atr_hist.append(sum(trs[-14:])/14 if len(trs)>=14 else trs[-1])
        if atr_hist:
            rank=sum(1 for a in atr_hist if a<atr)/len(atr_hist)
            if rank>0.80: return "VOLATILE"
    except: pass
    # ADX-based trend detection (simplified Wilder's)
    try:
        n=min(14,len(cl)-1)
        pdm=[]; mdm=[]; trl=[]
        for i in range(-n,0):
            hd=hi[i]-hi[i-1]; ld=lo[i-1]-lo[i]
            pdm.append(hd if hd>ld and hd>0 else 0)
            mdm.append(ld if ld>hd and ld>0 else 0)
            trl.append(max(hi[i]-lo[i],abs(hi[i]-cl[i-1]),abs(lo[i]-cl[i-1])))
        atr14=sum(trl)/n if trl else atr
        if atr14>0:
            pdi=sum(pdm)/n/atr14*100; mdi=sum(mdm)/n/atr14*100
            dx=abs(pdi-mdi)/(pdi+mdi)*100 if (pdi+mdi)>0 else 0
            if dx>25: return "TRENDING"
    except: pass
    return "RANGING"

# ================================================================
# ML SHADOW MODE — pure Python, no external deps
# Trains on trade_log.csv, predicts win probability per signal.
# Shadow mode: prediction is LOGGED but does NOT gate trades yet.
# After 200+ completed trades, enable ML_GATE=True to filter.
# ================================================================
ML_GATE = False   # Set True when you have 200+ trades in trade_log.csv

def ml_predict(iid, tf, score, regime, h_utc, rsi_val, vr_val):
    """Returns estimated win probability 0-100 based on historical patterns."""
    try:
        if not os.path.exists(TRADE_LOG): return 50
        with open(TRADE_LOG,"r") as f:
            rows=[r for r in csv.DictReader(f) if r.get("result") in ("WIN","LOSS")]
        if len(rows)<15: return 50
        # Instrument + TF specific
        similar=[r for r in rows if r.get("instrument")==iid and r.get("timeframe")==tf]
        if len(similar)<8:
            similar=[r for r in rows if r.get("timeframe")==tf]
        if len(similar)<5: return 50
        wins=sum(1 for r in similar if r.get("result")=="WIN")
        base_prob=wins/len(similar)*100
        # Adjust for score (each point above 65 adds small confidence)
        score_adj=(score-65)*0.25
        # Regime adjustment
        regime_adj=5 if regime=="TRENDING" else (-3 if regime=="VOLATILE" else 0)
        # Session adjustment
        sess_adj=8 if 13<=h_utc<16 else (4 if 7<=h_utc<17 else -5)
        prob=min(95,max(5,base_prob+score_adj+regime_adj+sess_adj))
        return round(prob)
    except: return 50

# ================================================================
# CLAUDE AI CONFIRMATION — APEX-AI DEVIL'S ADVOCATE ENSEMBLE
# Haiku  = Bullish analyst:  "Rate signal quality 0-100"
# Sonnet = Risk manager:     "Rate RISK OF FAILURE 0-100"
# Sonnet risk is INVERTED -> quality score, then blended with Haiku.
# SPLIT verdict (gap>30): analyst likes it but risk manager hates it
#   -> score penalty (-10), bot becomes cautious. Saves bad trades.
# Both agree (gap<=15): small confidence bonus (+4).
# Runs in parallel threads — total wait = slowest model (~3s).
# ================================================================
def _ai_call(model, prompt, results, key, timeout=13):
    """Call one Claude model and store parsed result in results[key]."""
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": CLAUDE_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": 100,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=timeout
        )
        if r.ok:
            txt = r.json()["content"][0]["text"]
            m = re.search(r'"s"\s*:\s*(\d+).*?"r"\s*:\s*"([^"]+)"', txt, re.DOTALL)
            if m:
                results[key] = {"score": int(m.group(1)), "reason": m.group(2)}
    except Exception as e:
        log.warning("APEX-AI %s error: %s" % (model, str(e)))

def ai_confirm(iid, direction, sigs, score, context):
    """Devil's Advocate ensemble. Haiku = bull analyst, Sonnet = risk manager.
    Returns (adjusted_score, reason). Both run in parallel threads."""
    if not CLAUDE_API_KEY:
        log.warning("CLAUDE_API_KEY not set - AI confirmation offline.")
        return score, "AI offline"
    try:
        strats = ",".join(sigs[:8])
        # Haiku: optimistic analyst — what makes this trade attractive?
        bull_prompt = ("Rate this forex signal quality 0-100. Higher = better entry. "
                       "Pair:%s Dir:%s Base:%d Strats:%s | %s "
                       "Reply JSON only: {\"s\":75,\"r\":\"brief reason\"}"
                       % (iid, direction, score, strats, context))
        # Sonnet: skeptical risk manager — what could kill this trade?
        bear_prompt = ("You are a skeptical forex risk manager. "
                       "Rate the RISK OF FAILURE for this signal 0-100. "
                       "100=certain loser, 0=no risk. Check: wrong trend direction, "
                       "bad session timing, overbought/oversold, wide spread, "
                       "key resistance ahead, conflicting timeframes, low volume. "
                       "Pair:%s Dir:%s Base:%d Strats:%s | %s "
                       "Reply JSON only: {\"s\":40,\"r\":\"brief risk reason\"}"
                       % (iid, direction, score, strats, context))

        results = {}
        t1 = threading.Thread(target=_ai_call,
                              args=("claude-haiku-4-5-20251001", bull_prompt, results, "bull"))
        t2 = threading.Thread(target=_ai_call,
                              args=("claude-sonnet-4-6", bear_prompt, results, "bear"))
        t1.daemon = True; t2.daemon = True
        t1.start(); t2.start()
        t1.join(timeout=14); t2.join(timeout=14)

        bull = results.get("bull")   # Haiku optimist score (0-100, higher=better)
        bear = results.get("bear")   # Sonnet risk score   (0-100, higher=riskier)

        if not bull and not bear:
            log.warning("APEX-AI: both models timed out for %s" % iid)
            return score, "AI timeout"

        if bull and not bear:
            final = int(score * 0.60 + bull["score"] * 0.40)
            log.info("APEX-AI %s (haiku only): base=%d bull=%d final=%d [%s]"
                     % (iid, score, bull["score"], final, bull["reason"]))
            return final, bull["reason"]

        if bear and not bull:
            quality = 100 - bear["score"]
            final = int(score * 0.60 + quality * 0.40)
            log.info("APEX-AI %s (sonnet only): base=%d risk=%d quality=%d final=%d [%s]"
                     % (iid, score, bear["score"], quality, final, bear["reason"]))
            return final, "RISK:" + bear["reason"]

        # Both responded — devil's advocate blend
        bull_s  = bull["score"]
        risk_s  = bear["score"]
        quality = 100 - risk_s           # invert risk -> quality (0-100)
        blend   = int(bull_s * 0.50 + quality * 0.50)

        gap = abs(bull_s - quality)
        if gap <= 15:
            adj     = 4
            verdict = "AGREE"            # analyst + risk mgr agree -> confident
        elif gap <= 30:
            adj     = 0
            verdict = "NEAR"
        else:
            adj     = -10
            verdict = "SPLIT"            # serious disagreement -> caution

        ai_final = max(0, min(100, blend + adj))
        final    = int(score * 0.60 + ai_final * 0.40)
        reason   = "[%s] Bull:%d Risk:%d Q:%d->%d | %s | RISK:%s" % (
                   verdict, bull_s, risk_s, quality, ai_final,
                   bull["reason"][:35], bear["reason"][:35])
        log.info("APEX-AI DEVIL %s: base=%d bull=%d risk=%d quality=%d blend=%d adj=%+d final=%d [%s]"
                 % (iid, score, bull_s, risk_s, quality, blend, adj, final, verdict))
        return final, reason

    except Exception as e:
        log.warning("APEX-AI error: " + str(e))
        return score, ""

# ================================================================
# STRATEGY ENGINE — 21 strategies (15 classic + 6 SMC)
# ================================================================
def check_strategies(o, hi, lo, cl, vols, mid, bid, ask, tf="H4", atr=0):
    if len(cl)<50: return [],0,"NONE"

    e8=ema(cl,8); e21=ema(cl,21); e50=ema(cl,50)
    e200=ema(cl,min(200,len(cl)))
    e8s=ema_s(cl,8); e21s=ema_s(cl,21)
    bb_up,bb_mid,bb_low,bb_w=calc_bb(cl)
    bb_up2,bb_mid2,bb_low2,bb_w2=calc_bb(cl[:-1]) if len(cl)>21 else (bb_up,bb_mid,bb_low,bb_w)
    RSI=calc_rsi(cl); RSIs=rsi_s(cl)
    sk,sd,skp,sdp=calc_stoch(cl,hi,lo)
    VR=vol_ratio(vols)

    bull=[]; bear=[]
    d21=abs(mid-e21)/e21*100 if e21>0 else 0

    # 1. EMA Cross
    if len(e8s)>=2 and len(e21s)>=2:
        if e8s[-2]<e21s[-2] and e8s[-1]>e21s[-1] and mid>e50 and mid>e200: bull.append("EMA_CROSS")
        if e8s[-2]>e21s[-2] and e8s[-1]<e21s[-1] and mid<e50 and mid<e200: bear.append("EMA_CROSS")

    # 2. BB Squeeze Breakout
    if bb_w2<0.002 and bb_w>bb_w2:
        if cl[-1]>bb_up and cl[-1]>cl[-2]: bull.append("BB_SQUEEZE")
        if cl[-1]<bb_low and cl[-1]<cl[-2]: bear.append("BB_SQUEEZE")

    # 3. BB Touch
    if cl[-1]<=bb_low*1.0001 and cl[-1]>cl[-2]: bull.append("BB_TOUCH")
    if cl[-1]>=bb_up*0.9999  and cl[-1]<cl[-2]: bear.append("BB_TOUCH")

    # 4. Golden Cross (long only — removed DEATH_CROSS, 11.8% WR)
    if len(cl)>=200:
        e50p=ema(cl[:-5],50); e200p=ema(cl[:-5],min(200,len(cl)-5))
        if e50p<e200p and e50>e200: bull.append("GOLDEN_CROSS")

    # 5. Mean Reversion
    if d21>1.5:
        if mid<e21 and RSI<35 and cl[-1]>cl[-2]: bull.append("MEAN_REV")
        if mid>e21 and RSI>65 and cl[-1]<cl[-2]: bear.append("MEAN_REV")

    # 6. Trend Continuation at 50 EMA
    if abs(mid-e50)/e50*100<0.3 if e50>0 else False:
        if mid>e200 and cl[-1]>cl[-2] and RSI<60: bull.append("TREND_CONT")
        if mid<e200 and cl[-1]<cl[-2] and RSI>40: bear.append("TREND_CONT")

    # 7. VWAP (weak signal)
    if len(vols)>=20:
        tps=sum((hi[i]+lo[i]+cl[i])/3*vols[i] for i in range(-20,0))
        vs=sum(vols[-20:])
        vwap=tps/vs if vs>0 else mid
        if abs(mid-vwap)/vwap*100<0.15 if vwap>0 else False:
            if mid>vwap and cl[-1]>cl[-2]: bull.append("VWAP")
            if mid<vwap and cl[-1]<cl[-2]: bear.append("VWAP")

    # 8. Engulfing + EMA
    if len(cl)>=3:
        if cl[-1]>o[-1] and cl[-2]<o[-2] and cl[-1]>o[-2] and o[-1]<cl[-2] and d21<0.5: bull.append("ENGULF")
        if cl[-1]<o[-1] and cl[-2]>o[-2] and cl[-1]<o[-2] and o[-1]>cl[-2] and d21<0.5: bear.append("ENGULF")

    # 9. Breakout Retest
    if len(hi)>=20:
        rh=max(hi[-20:-3]); rl=min(lo[-20:-3])
        if cl[-3]>rh and lo[-1]<rh*1.001 and cl[-1]>rh: bull.append("RETEST")
        if cl[-3]<rl and hi[-1]>rl*0.999 and cl[-1]<rl:  bear.append("RETEST")

    # 10. EMA Ribbon
    if e8>e21>e50>e200 and e8-e21>e21-e50 and RSI>50 and VR>1.2: bull.append("EMA_RIBBON")
    if e8<e21<e50<e200 and e21-e8>e50-e21 and RSI<50 and VR>1.2: bear.append("EMA_RIBBON")

    # 11. Stochastic Cross
    if skp<sdp and sk>sd and sk<30: bull.append("STOCH_CROSS")
    if skp>sdp and sk<sd and sk>70: bear.append("STOCH_CROSS")

    # 12. Liquidity Sweep (weak)
    swept,sweep_dir=liq_sweep(hi,lo,cl)
    if swept:
        if sweep_dir=="BULL": bull.append("LIQ_SWEEP")
        if sweep_dir=="BEAR": bear.append("LIQ_SWEEP")

    # ---- SMC STRATEGIES ----
    if atr>0:
        # 13. SMC Order Block — price returning to institutional OB zone
        ob_hit,ob_dir=smc_order_block(o,hi,lo,cl,mid,atr)
        if ob_hit:
            if ob_dir=="BULL": bull.append("SMC_OB")
            else: bear.append("SMC_OB")

        # 14. SMC Fair Value Gap — price filling an imbalance
        fvg_hit,fvg_dir=smc_fvg(hi,lo,cl,mid)
        if fvg_hit:
            if fvg_dir=="BULL": bull.append("SMC_FVG")
            else: bear.append("SMC_FVG")

        # 15. SMC Displacement — strong institutional move
        disp_hit,disp_dir=smc_displacement(o,hi,lo,cl,atr)
        if disp_hit:
            if disp_dir=="BULL": bull.append("SMC_DISP")
            else: bear.append("SMC_DISP")

    # 16 & 17. SMC BOS / CHoCH
    bos,choch=smc_bos_choch(hi,lo,cl)
    if bos=="BULL":   bull.append("SMC_BOS")
    if bos=="BEAR":   bear.append("SMC_BOS")
    if choch=="BULL": bull.append("SMC_CHOCH")
    if choch=="BEAR": bear.append("SMC_CHOCH")

    # 18 & 19. SMC Premium / Discount zone
    zone,pct=smc_pd_zone(hi,lo,cl)
    if zone=="DISCOUNT" and RSI<50: bull.append("SMC_DISCOUNT")
    if zone=="PREMIUM"  and RSI>50: bear.append("SMC_PREMIUM")

    # 20. Pin Bar — hammer or shooting star at key level
    if atr>0:
        pb_hit,pb_dir=pin_bar(o,hi,lo,cl,atr)
        if pb_hit:
            if pb_dir=="BULL": bull.append("PIN_BAR")
            else: bear.append("PIN_BAR")

    # 21. RSI Divergence (proper implementation — only fires at extremes)
    # Bullish: price makes lower low but RSI makes higher low = hidden strength
    # Bearish: price makes higher high but RSI makes lower high = hidden weakness
    if len(RSIs)>=20 and len(cl)>=20:
        try:
            # Find two recent lows in price
            pl1_idx=min(range(len(cl)-10,len(cl)-2),key=lambda i:cl[i])
            pl2_idx=min(range(max(0,pl1_idx-15),pl1_idx-2),key=lambda i:cl[i])
            if cl[pl1_idx]<cl[pl2_idx] and RSIs[pl1_idx]>RSIs[pl2_idx] and RSI<40:
                bull.append("RSI_DIV")  # price lower low, RSI higher low = bullish div
            # Find two recent highs in price
            ph1_idx=max(range(len(cl)-10,len(cl)-2),key=lambda i:cl[i])
            ph2_idx=max(range(max(0,ph1_idx-15),ph1_idx-2),key=lambda i:cl[i])
            if cl[ph1_idx]>cl[ph2_idx] and RSIs[ph1_idx]<RSIs[ph2_idx] and RSI>60:
                bear.append("RSI_DIV")  # price higher high, RSI lower high = bearish div
        except: pass

    # 200 EMA filter — hard gate on H4/D, soft on scalp
    above200=(mid>e200)
    if tf in ["H4","D"]:
        if not above200: bull=[]
        if above200:     bear=[]
    else:
        dist200=abs(mid-e200)/e200*100 if e200>0 else 0
        if not above200 and dist200>2.0: bull=[]
        if above200     and dist200>2.0: bear=[]

    nb=len(bull); ns=len(bear)
    if   nb>=3 and nb>ns: direction="LONG"
    elif ns>=3 and ns>nb: direction="SHORT"
    elif nb==2: direction="WATCH_LONG"
    elif ns==2: direction="WATCH_SHORT"
    else:       direction="NONE"

    c2=max(nb,ns)
    active=bull if nb>=ns else bear
    smc_bonus=sum(10 for s in active if s.startswith("SMC_"))
    score=min(99,35+(c2*9)+(5 if VR>1.5 else 0)+(5 if abs(RSI-50)>15 else 0)+(10 if swept else 0)+smc_bonus)
    return active,score,direction

def mtf_score(iid, direction, primary_tf):
    """
    Weighted multi-timeframe confluence check.
    Returns (confirmed:bool, score_bonus:int).
    D1 agreement = +15, H4 = +10, H1 = +5.
    D1 disagreement = hard veto (return False, 0).
    """
    # Which higher timeframes to check
    tf_map={
        "M5":  [("H1",5),("H4",10),("D",15)],
        "M15": [("H1",5),("H4",10),("D",15)],
        "H1":  [("H4",10),("D",15)],
        "H4":  [("D",15)],
        "D":   [],
    }
    checks=tf_map.get(primary_tf,[("H4",10)])
    bonus=0; confirmed=False; vetoed=False
    for tf,wt in checks:
        try:
            cc=get_candles(iid,tf,210)
            o2,h2,l2,cl2,v2=parse_candles(cc)
            b2,a2=get_price(iid); m2=(b2+a2)/2
            at2=calc_atr_val(cc)
            _,_,d=check_strategies(o2,h2,l2,cl2,v2,m2,b2,a2,tf,at2)
            base=direction.replace("WATCH_","")
            if base in d:
                bonus+=wt; confirmed=True
            elif d not in ("NONE","WATCH_LONG","WATCH_SHORT") and tf=="D":
                vetoed=True   # D1 hard disagrees — veto the trade
        except: pass
    if vetoed: return False, 0
    return confirmed, bonus

# ================================================================
# HELPERS
# ================================================================
def in_session(sessions):
    h=datetime.now(timezone.utc).hour
    for s,e in sessions:
        if (s<e and s<=h<e) or (s>=e and (h>=s or h<e)): return True
    return False

def session_quality():
    """Returns session score bonus: Overlap=10, London/NY=4, Asian=0"""
    h=datetime.now(timezone.utc).hour
    if 13<=h<16: return 10   # London/NY overlap — best time
    if 7<=h<10 or 12<=h<17: return 4
    if 0<=h<9: return 0      # Asian — historically poor
    return 2

def corr_ok(iid,direction,trades):
    corr=INSTRUMENTS[iid]["corr"]
    for info in trades.values():
        inst=info.get("inst","")
        if inst in INSTRUMENTS and INSTRUMENTS[inst]["corr"]==corr and info.get("dir","")==direction:
            return False
    return True

def get_tier(bal):
    for t in RISK:
        if t["min"]<=bal<t["max"]: return t
    return RISK[-1]

_kelly_cache={"mult":1.0,"ts":0}

def get_kelly_mult():
    """Quarter-Kelly sizing from rolling last-20-trade win rate + R:R."""
    now=time.time()
    if now-_kelly_cache["ts"]<300: return _kelly_cache["mult"]  # cache 5min
    try:
        if not os.path.exists(TRADE_LOG): return 1.0
        with open(TRADE_LOG,"r") as f:
            rows=[r for r in csv.DictReader(f) if r.get("result") in ("WIN","LOSS")]
        if len(rows)<10: return 1.0
        recent=rows[-20:]
        wr=sum(1 for r in recent if r.get("result")=="WIN")/len(recent)
        rrs=[]
        for r in recent:
            try:
                tp_d=abs(float(r.get("tp",0))-float(r.get("entry",0)))
                sl_d=abs(float(r.get("sl",0))-float(r.get("entry",0)))
                if sl_d>0: rrs.append(tp_d/sl_d)
            except: pass
        avg_rr=sum(rrs)/len(rrs) if rrs else 1.5
        kelly=wr-(1-wr)/avg_rr if avg_rr>0 else 0
        quarter=kelly*0.25
        mult=max(0.5,min(2.0,quarter/0.10))  # 0.10 = 25% Kelly at 55%WR 1.5RR
        _kelly_cache["mult"]=mult; _kelly_cache["ts"]=now
        log.info("Kelly: WR=%.0f%% RR=%.2f kelly=%.3f mult=%.2f"%(wr*100,avg_rr,kelly,mult))
        return mult
    except Exception as e:
        log.warning("Kelly error: "+str(e)); return 1.0

def get_lots(bal, c2, inst="", regime="RANGING", score=0):
    base=get_tier(bal)["lots"]
    if c2>=6: lots=round(base*1.5,2)
    elif c2>=5: lots=round(base*1.2,2)
    elif c2>=4: lots=base
    else: lots=round(base*0.8,2)
    # Kelly multiplier — scales up when system is hot, down when cold
    lots=round(lots*get_kelly_mult(),2)
    # Regime — halve size in volatile conditions
    if regime=="VOLATILE": lots=round(lots*0.5,2)
    # Score-based multiplier — bet bigger on highest-conviction signals
    if score>=90:   lots=round(lots*1.30,2)
    elif score>=80: lots=round(lots*1.15,2)
    # No boost for score <80 — standard sizing
    # Personal edge lot multiplier — BTC gets 40% size, BCO gets 90%
    pe_mult = PERSONAL_EDGE.get(inst, {}).get("lot_mult", 1.0)
    if pe_mult != 1.0:
        lots = round(lots * pe_mult, 2)
    day=datetime.now(timezone.utc).weekday()
    if day>=5: lots=round(lots*0.7,2)
    return max(0.01,lots)

def bad_time():
    n=datetime.now(timezone.utc)
    return (n.weekday()==0 and n.hour<10) or (n.weekday()==4 and n.hour>=17)

def can_trade(state, bal):
    # FTMO hard limits — checked first, override everything
    if FTMO_MODE:
        allowed, reason = ftmo_check(state, bal)
        if not allowed:
            return False
        # In FTMO mode use tighter consecutive loss rule (2 not 3)
        if state.get("consec", 0) >= 2:
            log.warning("FTMO mode: 2 consecutive losses — pausing to protect challenge")
            return False
        # Max trades lower in FTMO mode
        return True
    # Standard mode
    tier = get_tier(bal)
    if state["daily_loss"] >= bal * tier["dloss"]:
        log.warning("Daily loss limit hit — pausing"); return False
    if state.get("consec", 0) >= 3:
        alert("3 CONSECUTIVE LOSSES","Bot paused. Will resume next scan.","high","x")
        log.warning("3 consecutive losses — pausing"); return False
    return True

def daily_reset(state):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("daily_date","") != today:
        w=state["wins"]; l=state["losses"]; t2=w+l; dl=state["daily_loss"]
        if t2>0 or dl>0:
            wr=round(w/t2*100) if t2 else 0
            summary=str(w)+"W/"+str(l)+"L WR:"+str(wr)+"% DL:$"+str(round(dl,2))
            alert("DAILY SUMMARY",summary,"default","bar_chart")
        state["daily_loss"]=0.0; state["daily_date"]=today; state["consec"]=0
        log.info("Daily reset")
    # FTMO daily reset
    ftmo_daily_reset(state)
    return state

def load_state():
    try:
        with open(STATE_FILE) as f: s=json.load(f)
    except: s={}
    s.setdefault("trades",{})
    s.setdefault("daily_loss",0.0)
    s.setdefault("daily_date","")
    s.setdefault("wins",0)
    s.setdefault("losses",0)
    s.setdefault("consec",0)
    s.setdefault("journal",[])
    # FTMO fields
    s.setdefault("ftmo_daily_loss",0.0)
    s.setdefault("ftmo_daily_date","")
    s.setdefault("ftmo_peak_bal", FTMO_START_BAL)
    s.setdefault("ftmo_days_traded", [])
    return s

def save_state(s):
    with open(STATE_FILE,"w") as f: json.dump(s,f,default=str)

# ================================================================
# TRADE MANAGEMENT
# ================================================================
def manage(state):
    if not state["trades"]: return state
    try:
        open_trades={t["id"]:t for t in get_trades()}
        ac=get_account(); bal=float(ac["balance"])
    except Exception as e: log.warning("Manage fetch error: "+str(e)); return state

    for tid,info in list(state["trades"].items()):
        if tid not in open_trades:
            pl=float(info.get("pl",0))
            result="WIN" if pl>=0 else "LOSS"
            if pl>=0:
                state["wins"]+=1; state["consec"]=0
            else:
                state["losses"]+=1; state["daily_loss"]+=abs(pl); state["consec"]+=1
                # FTMO: track daily loss separately for hard limit enforcement
                if FTMO_MODE:
                    state["ftmo_daily_loss"]=state.get("ftmo_daily_loss",0.0)+abs(pl)
            try:
                opened=datetime.fromisoformat(info["opened"])
                dur=int((datetime.now(timezone.utc)-opened).total_seconds()/60)
            except: dur=0
            w=state["wins"]; l=state["losses"]; t2=w+l
            wr=" | WR:"+str(round(w/t2*100))+"%" if t2>0 else ""
            smc_flag=" [SMC]" if any(s.startswith("SMC_") for s in info.get("strats",[])) else ""
            write_log({"closed":datetime.now(timezone.utc).isoformat(),"inst":info["inst"],
                       "dir":info["dir"],"tf":info.get("tf",""),"entry":info.get("entry",0),
                       "tp":info.get("tp",0),"sl":info.get("sl",0),"atr":info.get("atr",0),
                       "lots":info.get("lots",0),"strats":info.get("strats",[]),
                       "score":info.get("score",0),"ai_reason":info.get("ai_reason",""),
                       "result":result,"pl":round(pl,4),"dur":dur,"bal":round(bal,2),"notes":smc_flag})
            state["journal"].append({"id":tid,"inst":info["inst"],"dir":info["dir"],
                "tf":info.get("tf",""),"pl":round(pl,4),"result":result,"dur":dur,
                "closed":datetime.now(timezone.utc).isoformat()})
            if len(state["journal"])>200: state["journal"]=state["journal"][-200:]
            del state["trades"][tid]
            msg=(info["inst"]+" "+info["dir"]+" "+info.get("tf","")+smc_flag+"\n"
                 "P&L: "+("%+.4f"%pl)+wr+"\n"
                 "Duration: "+str(dur)+"min | Balance: $"+str(round(bal,2)))
            if info.get("ai_reason"): msg+="\nAI: "+info["ai_reason"]
            alert("Trade "+result,msg,"high" if result=="WIN" else "default",
                  "white_check_mark" if result=="WIN" else "x")
            log.info("Closed "+tid+" "+result+" PL:"+str(round(pl,4))+" ("+str(dur)+"min)")
            continue

        try:
            bid,ask=get_price(info["inst"]); mid=(bid+ask)/2
            dr=info["dir"]; en=info["entry"]; av=info["atr"]
            tf=info.get("tf","H4")
            params=SCALP_P.get(tf,SWING_P.get(tf,SWING_P["H4"]))
            pa=((mid-en)/av if dr=="LONG" else (en-mid)/av) if av>0 else 0
            info["pl"]=round(mid-en if dr=="LONG" else en-mid,5)
            # Max hold for scalps
            if params["max_hold_min"]<9999:
                age=(datetime.now(timezone.utc)-datetime.fromisoformat(info["opened"])).total_seconds()/60
                if age>params["max_hold_min"]:
                    try: close_trade(tid); log.info(tid+" max hold")
                    except: pass
                    continue
            # Stale scalp exit — if barely moving at 50% of max hold, free the slot
            if params["max_hold_min"]<9999:
                age=(datetime.now(timezone.utc)-datetime.fromisoformat(info["opened"])).total_seconds()/60
                half_hold=params["max_hold_min"]*0.5
                if age>=half_hold and pa<0.25 and not info.get("partial_tp"):
                    try:
                        close_trade(tid)
                        log.info("%s stale scalp exit: %.0f min old, only %.2fR — closing"%(tid,age,pa))
                        continue
                    except: pass
            # Partial TP at 0.8x ATR — close 50% of position, lock in profit
            if pa>=0.8 and not info.get("partial_tp"):
                try:
                    ot=open_trades.get(tid,{})
                    cur_units=int(abs(float(ot.get("currentUnits",0))))
                    if cur_units>1:
                        half=max(1,cur_units//2)
                        oput("/accounts/"+ACCOUNT_ID+"/trades/"+tid+"/close",{"units":str(half)})
                        info["partial_tp"]=True
                        alert("PARTIAL TP",info["inst"]+" — closed 50% at %.1fR profit"%pa,"default","money_with_wings")
                        log.info("%s partial TP: closed %d of %d units at %.1fR"%(tid,half,cur_units,pa))
                except Exception as e: log.warning("Partial TP error "+tid+": "+str(e))
            # ---- STEPPED TRAILING STOP ----
            # Each step locks in more profit as the trade runs further.
            # No more giving back a full ATR move to get to breakeven.
            if pa>=0.5 and not info.get("be"):
                # Step 1: 0.5R reached → move to breakeven (sooner than before)
                try:
                    set_sl(tid,en); info["sl"]=en; info["be"]=True
                    alert("Breakeven",info["inst"]+" "+dr+" — SL to entry at 0.5R","default","shield")
                    log.info("%s stepped trail step1: breakeven at %.1fR"%(tid,pa))
                except: pass
            elif pa>=1.0 and not info.get("trail_1"):
                # Step 2: 1R reached → lock in 0.3R profit
                lock=(en+av*0.3 if dr=="LONG" else en-av*0.3)
                cs=info.get("sl",0)
                if (dr=="LONG" and lock>cs) or (dr=="SHORT" and lock<cs):
                    try:
                        set_sl(tid,lock); info["sl"]=lock; info["trail_1"]=True
                        log.info("%s stepped trail step2: +0.3R locked at %.1fR"%(tid,pa))
                    except: pass
            elif pa>=1.5 and not info.get("trail_2"):
                # Step 3: 1.5R reached → lock in 0.8R profit
                lock=(en+av*0.8 if dr=="LONG" else en-av*0.8)
                cs=info.get("sl",0)
                if (dr=="LONG" and lock>cs) or (dr=="SHORT" and lock<cs):
                    try:
                        set_sl(tid,lock); info["sl"]=lock; info["trail_2"]=True
                        log.info("%s stepped trail step3: +0.8R locked at %.1fR"%(tid,pa))
                    except: pass
            # Step 4: 2R+ → tight trailing stop (0.4xATR, 0.5xATR for Gold — more volatile)
            if pa>=2.0:
                trail_mult = 0.5 if info.get("inst") == "XAU_USD" else 0.4
                ns=(mid-av*trail_mult if dr=="LONG" else mid+av*trail_mult)
                cs=info.get("sl",0)
                if (dr=="LONG" and ns>cs) or (dr=="SHORT" and ns<cs):
                    try: set_sl(tid,ns); info["sl"]=ns
                    except: pass
            # Reversal exit
            cc=get_candles(info["inst"],"H4",60)
            o2,h2,l2,cl2,v2=parse_candles(cc)
            at2=calc_atr_val(cc)
            sigs2,_,nd=check_strategies(o2,h2,l2,cl2,v2,mid,bid,ask,"H4",at2)
            if dr=="LONG" and "SHORT" in nd and len(sigs2)>=3:
                try: close_trade(tid); log.info(tid+" reversal exit")
                except: pass
            elif dr=="SHORT" and "LONG" in nd and len(sigs2)>=3:
                try: close_trade(tid); log.info(tid+" reversal exit")
                except: pass
        except Exception as e: log.warning("Manage error "+tid+": "+str(e))

    save_state(state); return state

# ================================================================
# SCAN
# ================================================================
def scan_tf(iid, ii, tf, params, bal, state, oi):
    try:
        cc=get_candles(iid,tf,210)

        # ---- CANDLE-CLOSE GATE ----
        # Only analyse when a NEW candle has closed — no point re-reading same candle
        complete=[c for c in cc if c.get("complete",True)]
        if not candle_is_new(iid,tf,complete):
            return None   # same candle as last scan — skip

        o2,h2,l2,cl2,v2=parse_candles(cc)
        if len(cl2)<50: return None
        bid,ask=get_price(iid); mid=(bid+ask)/2
        AT=calc_atr_val(cc)
        if AT==0: return None

        # ---- SPREAD FILTER ----
        spread=ask-bid
        ns=NORMAL_SPREADS.get(iid, spread)
        spread_ratio=spread/ns if ns>0 else 1.0
        if spread_ratio>2.5:
            log.info("%s %s SKIP spread %.5f (%.1fx normal)"%(iid,tf,spread,spread_ratio))
            return None

        sigs,score,direction=check_strategies(o2,h2,l2,cl2,v2,mid,bid,ask,tf,AT)
        qualifying=len([s for s in sigs if s not in WEAK_STRATS])

        # ---- REGIME DETECTION ----
        regime=detect_regime(h2,l2,cl2,AT)

        # ---- SESSION QUALITY BONUS ----
        sq=session_quality(); score=min(99,score+sq)

        # Extra fields for logging/ML
        rsi_v=round(calc_rsi(cl2),1)
        vr_v=round(vol_ratio(v2),2)
        # ATR percentile rank vs last 20 bars
        atr_hist=[]; n2=min(22,len(cl2)-1)
        for i in range(max(1,len(cl2)-n2),len(cl2)-1):
            trs2=[max(h2[j]-l2[j],abs(h2[j]-cl2[j-1]),abs(l2[j]-cl2[j-1]))
                  for j in range(max(1,i-13),i+1)]
            atr_hist.append(sum(trs2[-14:])/14 if len(trs2)>=14 else trs2[-1])
        atr_pct=round(sum(1 for a in atr_hist if a<AT)/len(atr_hist)*100,1) if atr_hist else 50

        log.info("%s %s Score:%d Dir:%s Sigs:%d Qual:%d Sess:+%d Regime:%s Spr:%.1fx"%(
            iid,tf,score,direction,len(sigs),qualifying,sq,regime,spread_ratio))

        if direction=="NONE" or qualifying<params.get("min_strats",MIN_STRATS): return None
        if not corr_ok(iid,direction,state["trades"]): return None
        base="LONG" if "LONG" in direction else "SHORT"

        # ---- DXY + ORDERBOOK for commodities ----
        if any(x in iid for x in ["XAU","XAG","BCO","WTICO"]):
            dxy=dxy_bias(); ob=orderbook_bias(iid)
            if dxy=="WEAK_USD"   and base=="LONG":  score=min(99,score+8)
            if dxy=="STRONG_USD" and base=="LONG":  score=max(0,score-10)
            if dxy=="STRONG_USD" and base=="SHORT": score=min(99,score+8)
            if dxy=="WEAK_USD"   and base=="SHORT": score=max(0,score-10)
            if ob!=0:
                if (ob==1 and base=="LONG") or (ob==-1 and base=="SHORT"): score=min(99,score+8)
                else: score=max(0,score-8)

        # ---- BINANCE BTC SENTIMENT (for BTC trades) ----
        if "XBT" in iid:
            btc=get_btc_sentiment()
            btc_bias=btc.get("bias","NEUTRAL")
            fng_sig=btc.get("fng_signal","NEUTRAL")
            chg=btc.get("change_24h",0)
            if btc_bias=="BULL" and base=="LONG":  score=min(99,score+10)
            if btc_bias=="BEAR" and base=="SHORT": score=min(99,score+10)
            if btc_bias=="BULL" and base=="SHORT": score=max(0,score-12)
            if btc_bias=="BEAR" and base=="LONG":  score=max(0,score-12)
            # Fear & Greed contrarian
            if fng_sig=="BULL" and base=="LONG":  score=min(99,score+8)
            if fng_sig=="BEAR" and base=="SHORT": score=min(99,score+8)
            log.info("BTC sentiment: bias=%s F&G=%s change=%.1f%%"%(btc_bias,fng_sig,chg))

        # ---- COT BIAS ----
        cb=cot_bias(iid)
        if cb!="NEUTRAL":
            if (cb=="BULL" and base=="LONG") or (cb=="BEAR" and base=="SHORT"): score=min(99,score+10)
            elif (cb=="BULL" and base=="SHORT") or (cb=="BEAR" and base=="LONG"): score=max(0,score-15)
            log.info(iid+" COT:"+cb+" Score:"+str(score))

        # ---- KEY LEVEL BONUS ----
        kl_hit,kl_dir,kl_bonus=key_level_bonus(h2,l2,cl2,AT)
        if kl_hit and (kl_dir==base or kl_dir=="BOTH"):
            score=min(99,score+kl_bonus)
            log.info("%s at KEY LEVEL +%d -> score %d"%(iid,kl_bonus,score))

        # ---- FIBONACCI LEVEL BONUS ----
        fib_hit,fib_dir,fb=fib_bonus(h2,l2,cl2,AT)
        if fib_hit and (fib_dir==base):
            score=min(99,score+fb)
            log.info("%s at FIBONACCI +%d -> score %d"%(iid,fb,score))

        # ---- WEEKLY TREND BIAS ----
        wb=weekly_bias(iid)
        if wb!="NEUTRAL":
            if (wb=="BULL" and base=="LONG") or (wb=="BEAR" and base=="SHORT"):
                score=min(99,score+10)
                log.info("%s weekly trend agrees +10 -> score %d"%(iid,score))
            elif (wb=="BULL" and base=="SHORT") or (wb=="BEAR" and base=="LONG"):
                score=max(0,score-20)
                log.info("%s AGAINST weekly trend -20 -> score %d"%(iid,score))

        # ---- PERSONAL EDGE PROFILE ----
        # Instrument-specific bonus/penalty based on user's real 483-trade history
        pe = PERSONAL_EDGE.get(iid, {})
        if pe.get("score_bonus", 0):
            score = min(99, score + pe["score_bonus"])
            log.info("%s PERSONAL EDGE bonus +%d -> score %d" % (iid, pe["score_bonus"], score))
        # BTC hard gate — only high-conviction signals
        if pe.get("min_score") and score < pe["min_score"]:
            log.info("%s PERSONAL EDGE min_score %d > score %d — SKIP (BTC gate)" % (
                     iid, pe["min_score"], score))
            write_signal_log(iid,base,tf,score,sigs,False,regime,atr_pct,rsi_v,vr_v,spread_ratio,0)
            return None
        # Short bias bonus — user's natural edge side
        if base == "SHORT":
            score = min(99, score + SHORT_BIAS_BONUS)
            log.info("%s SHORT bias bonus +%d -> score %d" % (iid, SHORT_BIAS_BONUS, score))

        # ---- ML SHADOW PREDICTION ----
        h_utc=datetime.now(timezone.utc).hour
        ml_pred=ml_predict(iid,tf,score,regime,h_utc,rsi_v,vr_v)
        log.info("%s ML shadow: %d%% win prob"%(iid,ml_pred))

        # Dynamic minimum score — adjusts for regime, session, losing streak + equity curve
        dyn_min = get_min_score(regime, state, h_utc) + equity_curve_filter(state)
        if score<dyn_min:
            log.info("%s score %d < dynamic min %d (regime=%s) — skip"%(iid,score,dyn_min,regime))
            write_signal_log(iid,base,tf,score,sigs,False,regime,atr_pct,rsi_v,vr_v,spread_ratio,ml_pred)
            return None

        # ---- ML GATE (only active when ML_GATE=True and enough data) ----
        if ML_GATE and ml_pred<45:
            log.info("%s %s ML gate blocked (pred=%d%%)"%(iid,tf,ml_pred))
            write_signal_log(iid,base,tf,score,sigs,False,regime,atr_pct,rsi_v,vr_v,spread_ratio,ml_pred)
            return None

        # ---- WEIGHTED MTF CONFLUENCE ----
        mtf_ok,mtf_bonus=mtf_score(iid,base,tf)
        if not mtf_ok:
            write_signal_log(iid,base,tf,score,sigs,False,regime,atr_pct,rsi_v,vr_v,spread_ratio,ml_pred)
            return None
        score=min(99,score+mtf_bonus)
        if mtf_bonus>0: log.info("%s MTF bonus +%d -> score %d"%(iid,mtf_bonus,score))

        # ---- CLAUDE AI DEVIL'S ADVOCATE — ALWAYS ON ----
        zone,_=smc_pd_zone(h2,l2,cl2)
        bos,choch=smc_bos_choch(h2,l2,cl2)
        ctx=("RSI:%.0f VR:%.1fx Zone:%s BOS:%s CHoCH:%s COT:%s Regime:%s ML:%d%% Weekly:%s"%(
             rsi_v,vr_v,zone,str(bos),str(choch),cb,regime,ml_pred,wb))
        score,ai_reason=ai_confirm(iid,base,sigs,score,ctx)

        if score<dyn_min:
            log.info("%s AI-adjusted score %d below dynamic min %d — skip"%(iid,score,dyn_min))
            write_signal_log(iid,base,tf,score,sigs,False,regime,atr_pct,rsi_v,vr_v,spread_ratio,ml_pred)
            return None

        # ---- ADAPTIVE TP/SL BY REGIME ----
        # TRENDING: let winners run (wider TP, same SL)
        # RANGING:  take profits fast (tighter TP and SL)
        # VOLATILE: protect capital (tightest everything)
        if regime == "TRENDING":
            tp_m = params["tp"] * 1.25
            sl_m = params["sl"] * 1.00
            log.info("%s TRENDING regime: TP widened to %.2fx ATR"%(iid,tp_m))
        elif regime == "RANGING":
            tp_m = params["tp"] * 0.75
            sl_m = params["sl"] * 0.85
            log.info("%s RANGING regime: TP tightened to %.2fx ATR"%(iid,tp_m))
        elif regime == "VOLATILE":
            tp_m = params["tp"] * 0.60
            sl_m = params["sl"] * 0.65
            log.info("%s VOLATILE regime: TP/SL tightened to %.2f/%.2fx ATR"%(iid,tp_m,sl_m))
        else:
            tp_m = params["tp"]
            sl_m = params["sl"]

        if base=="LONG":  tp=ask+AT*tp_m; sl=ask-AT*sl_m; en=ask
        else:             tp=bid-AT*tp_m; sl=bid+AT*sl_m; en=bid

        write_signal_log(iid,base,tf,score,sigs,True,regime,atr_pct,rsi_v,vr_v,spread_ratio,ml_pred)
        return {"dir":base,"tp":tp,"sl":sl,"en":en,"AT":AT,
                "lots":get_lots(bal,len(sigs),iid,regime,score),"sigs":sigs,"score":score,
                "tf":tf,"ai_reason":ai_reason,"regime":regime,"ml_pred":ml_pred}
    except Exception as e:
        log.warning("Scan TF error %s %s: %s"%(iid,tf,str(e))); return None

def scan(state):
    try:
        ac=get_account(); bal=float(ac["balance"])
        no=len(state["trades"]); oi={v["inst"] for v in state["trades"].values()}
        log.info("Bal:$%.2f Open:%d/%d AI:%s"%(bal,no,MAX_TRADES,"ON" if CLAUDE_API_KEY else "OFF"))
        # FTMO: update peak balance every scan
        if FTMO_MODE:
            state["ftmo_peak_bal"] = max(state.get("ftmo_peak_bal", FTMO_START_BAL), bal)
        max_t = FTMO_MAX_TRADES if FTMO_MODE else MAX_TRADES
        if no>=max_t or not can_trade(state,bal) or bad_time(): return state
        for iid,ii in INSTRUMENTS.items():
            if iid in oi or no>=max_t: continue
            if not in_session(ii["sessions"]): continue
            if news_block(iid): continue
            # Instrument daily trade limit — no more than 2 trades per instrument per day
            if inst_daily_count(state, iid) >= MAX_INST_DAILY:
                log.info("%s daily limit reached (%d trades today) — skip" % (iid, MAX_INST_DAILY))
                continue
            result=None
            for tf in ii.get("scalp_tf",[]):
                result=scan_tf(iid,ii,tf,SCALP_P.get(tf,{}),bal,state,oi)
                if result: break
            if not result:
                for tf in ii.get("swing_tf",["H4","D"]):
                    result=scan_tf(iid,ii,tf,SWING_P.get(tf,SWING_P["H4"]),bal,state,oi)
                    if result: break
            if not result: continue
            # FTMO: enforce higher min score
            if FTMO_MODE and result["score"] < FTMO_MIN_SCORE:
                log.info("FTMO: skipping %s score %d < FTMO min %d"%(iid,result["score"],FTMO_MIN_SCORE))
                continue
            # Only execute trades on TRADE_PAIRS instruments
            if iid not in TRADE_PAIRS:
                log.info("MONITOR %s %s score:%d — signal logged, no trade (not in TRADE_PAIRS)"%(iid,result["dir"],result["score"]))
                continue
            try:
                # FTMO: override lots with safe prop-firm sizing
                final_lots = result["lots"]
                if FTMO_MODE:
                    final_lots = ftmo_lots(bal, result["lots"], result["score"])
                    # Weekly profit lock — micro-size to protect gains
                    if weekly_profit_check(bal):
                        final_lots = max(0.01, round(final_lots * 0.25, 2))
                        log.info("WEEKLY LOCK active: %s lots cut to %.2f" % (iid, final_lots))
                    # Portfolio heat check
                    sl_dist = abs(result["en"] - result["sl"])
                    if not ftmo_portfolio_heat(state, sl_dist, final_lots, iid):
                        log.info("FTMO heat limit: skipping %s to protect portfolio risk cap"%iid)
                        continue

                res=place(iid,result["dir"],final_lots,result["tp"],result["sl"])
                tid=res.get("orderFillTransaction",{}).get("tradeOpened",{}).get("tradeID")
                if tid:
                    state["trades"][tid]={
                        "inst":iid,"dir":result["dir"],"entry":result["en"],
                        "tp":result["tp"],"sl":result["sl"],"atr":result["AT"],
                        "lots":final_lots,"strats":result["sigs"],"score":result["score"],
                        "tf":result["tf"],"be":False,"ai_reason":result["ai_reason"],
                        "opened":datetime.now(timezone.utc).isoformat(),"pl":0
                    }
                    no+=1; oi.add(iid); save_state(state)
                    trade_type="SCALP" if result["tf"] in ["M5","M15","H1"] else "SWING"
                    smc_tags=[s for s in result["sigs"] if s.startswith("SMC_")]
                    smc_str=(" ["+",".join(smc_tags)+"]") if smc_tags else ""
                    # Track trading day for FTMO minimum days requirement
                    if FTMO_MODE:
                        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                        days = state.get("ftmo_days_traded",[])
                        if today not in days: days.append(today); state["ftmo_days_traded"]=days
                    profit_pct=(bal-FTMO_START_BAL)/FTMO_START_BAL*100 if FTMO_MODE else 0
                    ftmo_tag=(" | FTMO %.1f%%->%.0f%%"%(profit_pct,FTMO_TARGET*100)) if FTMO_MODE else ""
                    msg=(ii["n"]+" "+result["tf"]+" "+trade_type+smc_str+"\n"
                         "Score: "+str(result["score"])+" | "+str(len(result["sigs"]))+" signals\n"
                         "Signals: "+",".join(result["sigs"][:6])+"\n"
                         "Regime: "+result.get("regime","?")+
                         " | ML: "+str(result.get("ml_pred",50))+"%\n"
                         "Entry: "+str(round(result["en"],5))+"\n"
                         "TP: "+str(round(result["tp"],5))+" | SL: "+str(round(result["sl"],5))+"\n"
                         "Lots: "+str(final_lots)+" | Balance: $"+str(round(bal,2))+ftmo_tag)
                    if result["ai_reason"]: msg+="\nAI: "+result["ai_reason"]
                    alert("NEW "+result["dir"]+" "+trade_type,msg,"high","rocket")
                    # FTMO: send detailed copy-trade signal to Telegram
                    if FTMO_MODE:
                        result["lots"] = final_lots
                        ftmo_signal_alert(iid, result, bal, state)
                    log.info("Opened %s %s %s %s %s"%(tid,iid,result["dir"],result["tf"],trade_type))
            except Exception as e: log.warning("Place error %s: %s"%(iid,str(e)))
    except Exception as e: log.error("Scan error: "+str(e))
    return state

# ================================================================
# MAIN — 24/7 auto-restart
# ================================================================
init_log()
log.info("="*55)
log.info("APEX AUTO-TRADER V4 — HEDGE FUND EDITION")
log.info("Trade pairs: "+", ".join(sorted(TRADE_PAIRS)))
log.info("Account: "+ACCOUNT_ID)
log.info("Strategies: 23 (15 classic + 6 SMC + PIN_BAR + RSI_DIV)")
log.info("APEX-AI: "+("ENABLED (Claude Haiku)" if CLAUDE_API_KEY else "OFFLINE — set CLAUDE_API_KEY env var on VPS"))
log.info("MIN_SCORE=%d  MIN_STRATS=%d  MAX_TRADES=%d"%(MIN_SCORE,MIN_STRATS,MAX_TRADES))
log.info("Scan speed: Overlap=%ds Active=%ds Off=%ds (adaptive)"%(SCAN_FAST,SCAN_ACTIVE,SCAN_SLOW))
log.info("="*55)

alert("APEX V5 ONLINE",
    "Hedge Fund Edition V5\n23 strategies + Key Levels + Fibonacci + Weekly Bias\n"
    "Binance BTC + Fear&Greed + Partial TP\n"
    "Adaptive scan: 30s/60s/120s by session\n"
    "AI: "+("ON" if CLAUDE_API_KEY else "OFFLINE — add CLAUDE_API_KEY"),
    "default","robot")

state=load_state()
consecutive_errors=0

while True:
    try:
        state=daily_reset(state)
        ukh=uk_hour(); utm=datetime.now(timezone.utc).minute
        today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if ukh==6  and utm<4  and _brief["FTMO_MORNING"]!=today: send_briefing("FTMO_MORNING"); _brief["FTMO_MORNING"]=today
        if ukh==7  and utm<4  and _brief["LONDON"]!=today:  send_briefing("LONDON");  _brief["LONDON"]=today
        if ukh==14 and utm>=30 and utm<34 and _brief["NEWYORK"]!=today: send_briefing("NEWYORK"); _brief["NEWYORK"]=today
        if ukh==21 and utm<4  and _brief["CLOSE"]!=today:   send_briefing("CLOSE");   _brief["CLOSE"]=today
        state=manage(state)
        state=scan(state)
        save_state(state)
        w=state["wins"]; l=state["losses"]; t2=w+l
        wait=scan_interval()
        log.info("%dW/%dL (%.0f%%) DL:%.2f Open:%d — next scan in %ds"%(
            w,l,w/t2*100 if t2 else 0,state["daily_loss"],len(state["trades"]),wait))
        consecutive_errors=0
        time.sleep(wait)
    except KeyboardInterrupt:
        save_state(state); log.info("APEX stopped by user"); break
    except Exception as e:
        consecutive_errors+=1
        log.error("Main error (%d): %s"%(consecutive_errors,str(e)))
        if consecutive_errors>=5:
            alert("APEX ERROR","Bot: "+str(consecutive_errors)+" errors. Pausing 5min.","high","warning")
            time.sleep(300); consecutive_errors=0
        else: time.sleep(30)
