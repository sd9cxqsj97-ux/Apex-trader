#!/usr/bin/env python3
"""
APEX BACKTESTING ENGINE v1
- Pulls 6 months of H1/H4/D candle data from OANDA for all 18 instruments
- Runs all 16 strategies on historical candles (M5/M15 excluded: too many candles)
- Walk-forward validation: train on first 4 months, test on last 2
- Per-strategy, per-instrument, per-session win rates
- Equity curve simulation (1% risk/trade, $10k start)
- Sharpe ratio + max drawdown
- Outputs: backtest_results.csv + backtest_report.html
- Flags instrument+TF+session combos with 60%+ win rate
"""

import os, csv, math, base64, requests, time, sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ================================================================
# CONFIG
# ================================================================
API_KEY  = (os.environ.get("OANDA_API_KEY") or base64.b64decode(
    "YmY3MGJjZDkzNjczM2JjNTE2NjIyZjFkYmRjMWRhY2ItYjk0Mzk4NzRkZjdlYTkzYmU2MGRhNzkxYTkyNTJmMTU="
).decode()).strip()
BASE_URL = base64.b64decode(
    "aHR0cHM6Ly9hcGktZnhwcmFjdGljZS5vYW5kYS5jb20vdjM="
).decode().strip()
HEADERS  = {"Authorization": "Bearer " + API_KEY}

LOOKBACK_DAYS = 183   # ~6 months
TRAIN_DAYS    = 122   # ~4 months  (leaves 2 months for test)
MIN_SCORE     = 65
MIN_STRATS    = 3
WEAK_STRATS   = {"LIQ_SWEEP", "VWAP"}   # add score but don't count toward MIN_STRATS
START_BALANCE = 10000.0
RISK_PCT      = 0.01  # 1% account risk per trade in equity simulation

INSTRUMENTS = {
    "EUR_USD":    {"n": "EUR/USD",   "sessions": [(7,17)],              "tfs": ["H4","D"]},
    "GBP_USD":    {"n": "GBP/USD",   "sessions": [(7,17)],              "tfs": ["H4","D"]},
    "USD_JPY":    {"n": "USD/JPY",   "sessions": [(0,9),(12,21)],        "tfs": ["H4","D"]},
    "AUD_USD":    {"n": "AUD/USD",   "sessions": [(22,24),(0,8)],        "tfs": ["H4","D"]},
    "USD_CAD":    {"n": "USD/CAD",   "sessions": [(12,21)],              "tfs": ["H4","D"]},
    "USD_CHF":    {"n": "USD/CHF",   "sessions": [(7,17)],               "tfs": ["H4","D"]},
    "NZD_USD":    {"n": "NZD/USD",   "sessions": [(22,24),(0,8)],        "tfs": ["H4","D"]},
    "EUR_GBP":    {"n": "EUR/GBP",   "sessions": [(7,17)],               "tfs": ["H4","D"]},
    "EUR_JPY":    {"n": "EUR/JPY",   "sessions": [(7,16)],               "tfs": ["H4","D"]},
    "GBP_JPY":    {"n": "GBP/JPY",   "sessions": [(7,16)],               "tfs": ["H4","D"]},
    "AUD_JPY":    {"n": "AUD/JPY",   "sessions": [(0,9),(7,16)],         "tfs": ["H4","D"]},
    "XAU_USD":    {"n": "GOLD",      "sessions": [(0,9),(7,17),(12,21)], "tfs": ["H1","H4","D"]},
    "XAG_USD":    {"n": "SILVER",    "sessions": [(7,17),(12,21)],        "tfs": ["H1","H4","D"]},
    "BCO_USD":    {"n": "BRENT",     "sessions": [(7,17),(12,21)],        "tfs": ["H1","H4","D"]},
    "WTICO_USD":  {"n": "WTI",       "sessions": [(12,21)],               "tfs": ["H1","H4","D"]},
    "SPX500_USD": {"n": "SP500",     "sessions": [(13,21)],               "tfs": ["H1","H4","D"]},
    "NAS100_USD": {"n": "NAS100",    "sessions": [(13,21)],               "tfs": ["H1","H4","D"]},
    "US30_USD":   {"n": "DOW30",     "sessions": [(13,21)],               "tfs": ["H1","H4","D"]},
}

TF_PARAMS = {
    "H1": {"tp": 1.5, "sl": 0.7, "max_hold": 48},
    "H4": {"tp": 2.5, "sl": 1.0, "max_hold": 120},
    "D":  {"tp": 3.0, "sl": 1.2, "max_hold": 60},
}

# ================================================================
# DATA FETCH
# ================================================================
def fetch_candles(instrument, granularity, from_dt, to_dt):
    """
    Fetch candles from OANDA using from+count pagination.
    OANDA does not allow from+to+count together - use from+count only.
    """
    all_candles = []
    current = from_dt
    while current < to_dt:
        params = {
            "granularity": granularity,
            "from":  current.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "count": "5000",
            "price": "M",
        }
        try:
            r = requests.get(
                BASE_URL + "/instruments/" + instrument + "/candles",
                headers=HEADERS, params=params, timeout=30)
            r.raise_for_status()
            candles = r.json().get("candles", [])
        except Exception as e:
            print("  [WARN] Fetch {}/{}: {}".format(instrument, granularity, e))
            break
        if not candles:
            break
        # Only keep candles within our date range
        filtered = []
        for c in candles:
            try:
                ct = datetime.fromisoformat(c["time"].replace("Z", "+00:00"))
                if ct <= to_dt:
                    filtered.append(c)
            except Exception:
                filtered.append(c)
        all_candles.extend(filtered)
        # Stop if we've passed the end date or got fewer than max
        if len(candles) < 4999:
            break
        if filtered and len(filtered) < len(candles):
            break  # last batch contained candles past to_dt
        try:
            last_dt = datetime.fromisoformat(
                candles[-1]["time"].replace("Z", "+00:00"))
            current = last_dt + timedelta(seconds=1)
        except Exception:
            break
    return all_candles

# ================================================================
# TECHNICAL INDICATORS (matches bot.py exactly)
# ================================================================
def ema(cl, p):
    if not cl: return 0.0
    if len(cl) < p: return cl[-1]
    k = 2.0 / (p + 1); e = cl[0]
    for v in cl[1:]: e = v * k + e * (1 - k)
    return e

def ema_s(cl, p):
    if not cl: return []
    k = 2.0 / (p + 1); e = cl[0]; out = [e]
    for v in cl[1:]: e = v * k + e * (1 - k); out.append(e)
    return out

def calc_rsi(cl, p=14):
    if len(cl) < p + 1: return 50.0
    g = l = 0.0
    for i in range(len(cl) - p, len(cl)):
        d = cl[i] - cl[i-1]
        if d > 0: g += d
        else:     l += abs(d)
    ag, al = g / p, l / p
    return 100.0 if al == 0 else 100 - (100 / (1 + ag / al))

def rsi_s(cl, p=14):
    out = [50.0] * len(cl)
    for i in range(p + 1, len(cl)):
        out[i] = calc_rsi(cl[:i+1], p)
    return out

def calc_stoch(cl, hi, lo, kp=14, dp=3):
    if len(cl) < kp + dp: return 50.0, 50.0, 50.0, 50.0
    ks = []
    for i in range(kp - 1, len(cl)):
        h = max(hi[i-kp+1:i+1]); l = min(lo[i-kp+1:i+1])
        ks.append(50.0 if h == l else ((cl[i] - l) / (h - l)) * 100)
    if len(ks) < dp: return ks[-1], ks[-1], ks[-1], ks[-1]
    k = ks[-1]; d = sum(ks[-dp:]) / dp
    pk = ks[-2] if len(ks) >= 2 else k
    pd = sum(ks[-dp-1:-1]) / dp if len(ks) > dp else d
    return k, d, pk, pd

def calc_bb(cl, p=20, mult=2.0):
    if len(cl) < p: return cl[-1], cl[-1], cl[-1], 0.0
    w = cl[-p:]; m = sum(w) / p
    s = (sum((x - m) ** 2 for x in w) / p) ** 0.5
    bw = (2 * mult * s) / (m if m > 0 else 1)
    return m + mult * s, m, m - mult * s, bw

def calc_atr(hi, lo, cl, p=14):
    if len(cl) < p + 1: return 0.0
    trs = []
    for i in range(1, len(cl)):
        trs.append(max(hi[i] - lo[i],
                       abs(hi[i] - cl[i-1]),
                       abs(lo[i] - cl[i-1])))
    if len(trs) < p: return 0.0
    return sum(trs[-p:]) / p

def vol_ratio(vols):
    if len(vols) < 5: return 1.0
    avg = sum(vols[-20:-1]) / max(1, len(vols[-20:-1]))
    return vols[-1] / avg if avg > 0 else 1.0

def liq_sweep(hi, lo, cl):
    if len(cl) < 20: return False, "NONE"
    rh = max(hi[-20:-3]); rl = min(lo[-20:-3])
    if lo[-1] < rl and cl[-1] > rl: return True, "BULL"
    if hi[-1] > rh and cl[-1] < rh: return True, "BEAR"
    return False, "NONE"

# ================================================================
# SMC ENGINE — Smart Money Concepts (matches bot.py V4 exactly)
# ================================================================
def smc_order_block(o, hi, lo, cl, mid, atr):
    if len(cl) < 15 or atr == 0: return False, "NONE"
    lb = min(40, len(cl) - 4)
    for i in range(len(cl) - 4, len(cl) - lb, -1):
        if i < 1: break
        if i + 2 >= len(cl): continue
        body_up = sum(max(0, cl[i+k] - o[i+k]) for k in range(3))
        if body_up > atr * 1.8 and all(cl[i+k] > o[i+k] for k in range(3)):
            if cl[i-1] < o[i-1]:
                ob_hi = max(o[i-1], hi[i-1]); ob_lo = min(cl[i-1], lo[i-1])
                if ob_lo <= mid <= ob_hi: return True, "BULL"
        body_dn = sum(max(0, o[i+k] - cl[i+k]) for k in range(3))
        if body_dn > atr * 1.8 and all(cl[i+k] < o[i+k] for k in range(3)):
            if cl[i-1] > o[i-1]:
                ob_hi = max(cl[i-1], hi[i-1]); ob_lo = min(o[i-1], lo[i-1])
                if ob_lo <= mid <= ob_hi: return True, "BEAR"
    return False, "NONE"

def smc_fvg(hi, lo, cl, mid):
    if len(cl) < 10: return False, "NONE"
    for i in range(max(0, len(cl) - 35), len(cl) - 2):
        if hi[i] < lo[i+2] and hi[i] <= mid <= lo[i+2]: return True, "BULL"
        if lo[i] > hi[i+2] and hi[i+2] <= mid <= lo[i]:  return True, "BEAR"
    return False, "NONE"

def smc_bos_choch(hi, lo, cl):
    if len(cl) < 30: return None, None
    n = min(60, len(cl)); swings = []
    for i in range(2, n - 2):
        idx = len(cl) - n + i
        if hi[idx]>hi[idx-1] and hi[idx]>hi[idx-2] and hi[idx]>hi[idx+1] and hi[idx]>hi[idx+2]:
            swings.append(('H', hi[idx]))
        if lo[idx]<lo[idx-1] and lo[idx]<lo[idx-2] and lo[idx]<lo[idx+1] and lo[idx]<lo[idx+2]:
            swings.append(('L', lo[idx]))
    if len(swings) < 4: return None, None
    sh  = [s[1] for s in swings if s[0] == 'H']
    sl2 = [s[1] for s in swings if s[0] == 'L']
    if len(sh) < 2 or len(sl2) < 2: return None, None
    bull_trend = sh[-1] > sh[-2]  and sl2[-1] > sl2[-2]
    bear_trend = sh[-1] < sh[-2]  and sl2[-1] < sl2[-2]
    cur = cl[-1]; bos = choch = None
    if bull_trend and cur > sh[-1]:  bos   = "BULL"
    if bear_trend and cur < sl2[-1]: bos   = "BEAR"
    if bear_trend and cur > sh[-1]:  choch = "BULL"
    if bull_trend and cur < sl2[-1]: choch = "BEAR"
    return bos, choch

def smc_displacement(o, hi, lo, cl, atr):
    if len(cl) < 5 or atr == 0: return False, "NONE"
    bu = be = 0
    for i in range(-3, 0):
        body = abs(cl[i] - o[i])
        if body > atr * 1.1:
            if cl[i] > o[i]: bu += 1
            else: be += 1
    if bu >= 2 or sum(max(0, cl[i]-o[i]) for i in range(-3,0)) > atr*2.5: return True, "BULL"
    if be >= 2 or sum(max(0, o[i]-cl[i]) for i in range(-3,0)) > atr*2.5: return True, "BEAR"
    return False, "NONE"

def smc_pd_zone(hi, lo, cl, n=50):
    n2 = min(n, len(cl))
    rh = max(hi[-n2:]); rl = min(lo[-n2:]); rng = rh - rl
    if rng == 0: return "EQUILIBRIUM", 50.0
    pct = (cl[-1] - rl) / rng * 100
    if pct < 35: return "DISCOUNT", pct
    if pct > 65: return "PREMIUM", pct
    return "EQUILIBRIUM", pct

# ================================================================
# STRATEGY ENGINE — 21 strategies (15 classic + 6 SMC)
# Synced exactly with bot.py V4. Takes h_utc + atr as parameters.
# Removed dead strategies: DEATH_CROSS (11.8%), RSI_DIV (17.2%),
#   LONDON_BREAK (26.3%), ASIAN_BREAK, MEAN_REV_SCALP (scalp only)
# ================================================================
def check_strategies(o, hi, lo, cl, vols, mid, h_utc, tf="H4", atr=0):
    if len(cl) < 50: return [], 0, "NONE"

    e8   = ema(cl, 8);   e21  = ema(cl, 21)
    e50  = ema(cl, 50);  e200 = ema(cl, min(200, len(cl)))
    e8s  = ema_s(cl, 8); e21s = ema_s(cl, 21)
    bb_up, bb_mid, bb_low, bb_w = calc_bb(cl)
    if len(cl) > 21:
        bb_up2, bb_mid2, bb_low2, bb_w2 = calc_bb(cl[:-1])
    else:
        bb_up2, bb_mid2, bb_low2, bb_w2 = bb_up, bb_mid, bb_low, bb_w
    RSI  = calc_rsi(cl)
    sk, sd, skp, sdp = calc_stoch(cl, hi, lo)
    VR   = vol_ratio(vols)

    bull = []; bear = []
    d21  = abs(mid - e21) / e21 * 100 if e21 > 0 else 0.0

    # 1. EMA Cross
    if len(e8s) >= 2 and len(e21s) >= 2:
        if e8s[-2]<e21s[-2] and e8s[-1]>e21s[-1] and mid>e50 and mid>e200: bull.append("EMA_CROSS")
        if e8s[-2]>e21s[-2] and e8s[-1]<e21s[-1] and mid<e50 and mid<e200: bear.append("EMA_CROSS")

    # 2. BB Squeeze Breakout
    if bb_w2 < 0.002 and bb_w > bb_w2:
        if cl[-1] > bb_up  and cl[-1] > cl[-2]: bull.append("BB_SQUEEZE")
        if cl[-1] < bb_low and cl[-1] < cl[-2]: bear.append("BB_SQUEEZE")

    # 3. BB Touch
    if cl[-1] <= bb_low * 1.0001 and cl[-1] > cl[-2]: bull.append("BB_TOUCH")
    if cl[-1] >= bb_up  * 0.9999 and cl[-1] < cl[-2]: bear.append("BB_TOUCH")

    # 4. Golden Cross (long only — DEATH_CROSS removed: 11.8% WR)
    if len(cl) >= 200:
        e50p  = ema(cl[:-5], 50)
        e200p = ema(cl[:-5], min(200, len(cl) - 5))
        if e50p < e200p and e50 > e200: bull.append("GOLDEN_CROSS")

    # 5. Mean Reversion
    if d21 > 1.5:
        if mid < e21 and RSI < 35 and cl[-1] > cl[-2]: bull.append("MEAN_REV")
        if mid > e21 and RSI > 65 and cl[-1] < cl[-2]: bear.append("MEAN_REV")

    # 6. Trend Continuation at 50 EMA
    if e50 > 0 and abs(mid - e50) / e50 * 100 < 0.3:
        if mid > e200 and cl[-1] > cl[-2] and RSI < 60: bull.append("TREND_CONT")
        if mid < e200 and cl[-1] < cl[-2] and RSI > 40: bear.append("TREND_CONT")

    # 7. VWAP (weak signal)
    if len(vols) >= 20:
        tps  = sum((hi[i]+lo[i]+cl[i])/3*vols[i] for i in range(-20, 0))
        vs   = sum(vols[-20:])
        vwap = tps / vs if vs > 0 else mid
        if vwap > 0 and abs(mid - vwap) / vwap * 100 < 0.15:
            if mid > vwap and cl[-1] > cl[-2]: bull.append("VWAP")
            if mid < vwap and cl[-1] < cl[-2]: bear.append("VWAP")

    # 8. Engulfing + EMA
    if len(cl) >= 3:
        be2 = (cl[-1]>o[-1] and cl[-2]<o[-2] and cl[-1]>o[-2] and o[-1]<cl[-2] and d21<0.5)
        se  = (cl[-1]<o[-1] and cl[-2]>o[-2] and cl[-1]<o[-2] and o[-1]>cl[-2] and d21<0.5)
        if be2: bull.append("ENGULF")
        if se:  bear.append("ENGULF")

    # 9. Breakout Retest
    if len(hi) >= 20:
        rh = max(hi[-20:-3]); rl = min(lo[-20:-3])
        if cl[-3] > rh and lo[-1] < rh*1.001 and cl[-1] > rh: bull.append("RETEST")
        if cl[-3] < rl and hi[-1] > rl*0.999 and cl[-1] < rl: bear.append("RETEST")

    # 10. EMA Ribbon
    if e8>e21>e50>e200 and e8-e21>e21-e50 and RSI>50 and VR>1.2: bull.append("EMA_RIBBON")
    if e8<e21<e50<e200 and e21-e8>e50-e21 and RSI<50 and VR>1.2: bear.append("EMA_RIBBON")

    # 11. Stochastic Cross
    if skp < sdp and sk > sd and sk < 30: bull.append("STOCH_CROSS")
    if skp > sdp and sk < sd and sk > 70: bear.append("STOCH_CROSS")

    # 12. Liquidity Sweep (weak signal)
    swept, sweep_dir = liq_sweep(hi, lo, cl)
    if swept:
        if sweep_dir == "BULL": bull.append("LIQ_SWEEP")
        if sweep_dir == "BEAR": bear.append("LIQ_SWEEP")

    # ---- SMC STRATEGIES ----
    if atr > 0:
        # 13. SMC Order Block
        ob_hit, ob_dir = smc_order_block(o, hi, lo, cl, mid, atr)
        if ob_hit:
            if ob_dir == "BULL": bull.append("SMC_OB")
            else:                bear.append("SMC_OB")

        # 14. SMC Fair Value Gap
        fvg_hit, fvg_dir = smc_fvg(hi, lo, cl, mid)
        if fvg_hit:
            if fvg_dir == "BULL": bull.append("SMC_FVG")
            else:                  bear.append("SMC_FVG")

        # 15. SMC Displacement
        disp_hit, disp_dir = smc_displacement(o, hi, lo, cl, atr)
        if disp_hit:
            if disp_dir == "BULL": bull.append("SMC_DISP")
            else:                   bear.append("SMC_DISP")

    # 16 & 17. SMC BOS / CHoCH
    bos, choch = smc_bos_choch(hi, lo, cl)
    if bos   == "BULL": bull.append("SMC_BOS")
    if bos   == "BEAR": bear.append("SMC_BOS")
    if choch == "BULL": bull.append("SMC_CHOCH")
    if choch == "BEAR": bear.append("SMC_CHOCH")

    # 18 & 19. SMC Premium / Discount
    zone, pct = smc_pd_zone(hi, lo, cl)
    if zone == "DISCOUNT" and RSI < 50: bull.append("SMC_DISCOUNT")
    if zone == "PREMIUM"  and RSI > 50: bear.append("SMC_PREMIUM")

    # 200 EMA filter — hard gate on H4/D, soft (2% distance) on H1
    above200 = (mid > e200)
    if tf in ("H4", "D"):
        if not above200: bull = []
        if above200:     bear = []
    else:   # H1 — soft gate only if >2% away
        dist200 = abs(mid - e200) / e200 * 100 if e200 > 0 else 0
        if not above200 and dist200 > 2.0: bull = []
        if above200     and dist200 > 2.0: bear = []

    nb = len(bull); ns = len(bear)
    if   nb >= 3 and nb > ns: direction = "LONG"
    elif ns >= 3 and ns > nb: direction = "SHORT"
    elif nb == 2:              direction = "WATCH_LONG"
    elif ns == 2:              direction = "WATCH_SHORT"
    else:                      direction = "NONE"

    c2     = max(nb, ns)
    active = bull if nb >= ns else bear
    smc_bonus = sum(10 for s in active if s.startswith("SMC_"))
    score  = min(99, 35 + (c2 * 9)
                 + (5  if VR > 1.5 else 0)
                 + (5  if abs(RSI - 50) > 15 else 0)
                 + (10 if swept else 0)
                 + smc_bonus)
    sigs   = bull if nb >= ns else bear
    return sigs, score, direction

# ================================================================
# SESSION HELPERS
# ================================================================
def in_session(sessions, h_utc):
    for s, e in sessions:
        if (s < e and s <= h_utc < e) or (s >= e and (h_utc >= s or h_utc < e)):
            return True
    return False

def session_name(h):
    if 0  <= h < 7:  return "ASIAN"
    if 7  <= h < 12: return "LONDON"
    if 12 <= h < 17: return "OVERLAP"
    if 17 <= h < 22: return "NEWYORK"
    return "OFFHOURS"

# ================================================================
# TRADE SIMULATION
# ================================================================
def simulate_trade(direction, entry, atr, params, future_candles):
    """
    Walk forward through raw OANDA candle objects to find first TP or SL hit.
    Returns: (result, rr_achieved, bars_held)
    """
    tp_dist = atr * params["tp"]
    sl_dist = atr * params["sl"]
    if direction == "LONG":
        tp = entry + tp_dist
        sl = entry - sl_dist
    else:
        tp = entry - tp_dist
        sl = entry + sl_dist

    for i, fc in enumerate(future_candles[:params["max_hold"]]):
        try:
            fh = float(fc["mid"]["h"])
            fl = float(fc["mid"]["l"])
        except Exception:
            continue
        if direction == "LONG":
            if fh >= tp: return "WIN",  round(tp_dist / sl_dist, 3), i + 1
            if fl <= sl: return "LOSS", -1.0,                         i + 1
        else:
            if fl <= tp: return "WIN",  round(tp_dist / sl_dist, 3), i + 1
            if fh >= sl: return "LOSS", -1.0,                         i + 1
    return "TIMEOUT", 0.0, params["max_hold"]

# ================================================================
# BACKTEST ONE INSTRUMENT + TIMEFRAME
# ================================================================
def backtest_tf(iid, ii, tf, candles, train_cutoff):
    """Walk through candles and record simulated trade outcomes."""
    trades  = []
    params  = TF_PARAMS.get(tf, TF_PARAMS["H4"])
    lookback = 210

    complete = [c for c in candles if c.get("complete", True)]

    for i in range(lookback, len(complete) - 1):
        window = complete[:i]
        if len(window) < 50:
            continue

        try:
            o2  = [float(c["mid"]["o"]) for c in window]
            h2  = [float(c["mid"]["h"]) for c in window]
            l2  = [float(c["mid"]["l"]) for c in window]
            cl2 = [float(c["mid"]["c"]) for c in window]
            v2  = [float(c["volume"])   for c in window]
        except Exception:
            continue

        mid = cl2[-1]
        atr = calc_atr(h2, l2, cl2)
        if atr == 0:
            continue

        try:
            ctime = datetime.fromisoformat(
                complete[i]["time"].replace("Z", "+00:00"))
            h_utc = ctime.hour
        except Exception:
            h_utc = 12

        if not in_session(ii["sessions"], h_utc):
            continue

        sigs, score, direction = check_strategies(o2, h2, l2, cl2, v2, mid, h_utc, tf, atr)

        if direction not in ("LONG", "SHORT"):
            continue

        # Session quality bonus — matches bot.py V4 session_quality()
        sq = 10 if 13 <= h_utc < 16 else (4 if (7 <= h_utc < 10 or 12 <= h_utc < 17) else 0)
        score = min(99, score + sq)

        # WEAK_STRATS count toward score but not toward MIN_STRATS threshold
        qualifying = len([s for s in sigs if s not in WEAK_STRATS])
        if score < MIN_SCORE or qualifying < MIN_STRATS:
            continue

        phase = "TRAIN" if ctime < train_cutoff else "TEST"

        result, rr, hold = simulate_trade(
            direction, mid, atr, params, complete[i+1:])

        trades.append({
            "time":         complete[i]["time"],
            "instrument":   iid,
            "name":         ii["n"],
            "tf":           tf,
            "direction":    direction,
            "hour":         h_utc,
            "session":      session_name(h_utc),
            "score":        score,
            "signals":      ",".join(sigs),
            "signal_count": len(sigs),
            "result":       result,
            "rr":           rr,
            "hold_bars":    hold,
            "phase":        phase,
        })

    return trades

# ================================================================
# STATISTICS
# ================================================================
def win_rate(trades):
    decided = [t for t in trades if t["result"] != "TIMEOUT"]
    if not decided: return 0.0
    wins = sum(1 for t in decided if t["result"] == "WIN")
    return round(wins / len(decided) * 100, 1)

def avg_rr(trades):
    decided = [t for t in trades if t["result"] != "TIMEOUT"]
    if not decided: return 0.0
    return round(sum(t["rr"] for t in decided) / len(decided), 3)

def build_equity(trades):
    """Simulate equity using fixed fractional 1% risk per trade."""
    balance = START_BALANCE
    curve   = [round(balance, 2)]
    for t in sorted(trades, key=lambda x: x["time"]):
        if t["result"] == "WIN":
            balance += balance * RISK_PCT * abs(t["rr"])
        elif t["result"] == "LOSS":
            balance -= balance * RISK_PCT
        curve.append(round(balance, 2))
    return curve

def calc_sharpe(equity):
    if len(equity) < 2: return 0.0
    returns = [(equity[i] - equity[i-1]) / equity[i-1]
               for i in range(1, len(equity))]
    if not returns: return 0.0
    avg = sum(returns) / len(returns)
    std = (sum((r - avg) ** 2 for r in returns) / len(returns)) ** 0.5
    return round(avg / std * (252 ** 0.5), 3) if std > 0 else 0.0

def calc_maxdd(equity):
    if not equity: return 0.0
    peak = equity[0]; max_dd = 0.0
    for v in equity:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd: max_dd = dd
    return round(max_dd, 2)

def strategy_stats(trades):
    data = defaultdict(list)
    for t in trades:
        for s in t["signals"].split(","):
            s = s.strip()
            if s: data[s].append(t)
    out = {}
    for s, ts in data.items():
        out[s] = {"trades": len(ts), "wr": win_rate(ts), "rr": avg_rr(ts)}
    return dict(sorted(out.items(), key=lambda x: x[1]["wr"], reverse=True))

def instrument_stats(trades):
    data = defaultdict(list)
    for t in trades: data[t["name"]].append(t)
    out = {}
    for name, ts in data.items():
        out[name] = {"trades": len(ts), "wr": win_rate(ts), "rr": avg_rr(ts)}
    return dict(sorted(out.items(), key=lambda x: x[1]["wr"], reverse=True))

def session_stats(trades):
    data = defaultdict(list)
    for t in trades: data[t["session"]].append(t)
    out = {}
    for sess, ts in data.items():
        out[sess] = {"trades": len(ts), "wr": win_rate(ts), "rr": avg_rr(ts)}
    return out

def top_combos(trades, min_wr=60.0, min_trades=5):
    data = defaultdict(list)
    for t in trades:
        key = t["name"] + " | " + t["tf"] + " | " + t["session"]
        data[key].append(t)
    out = []
    for key, ts in data.items():
        wr = win_rate(ts)
        if wr >= min_wr and len(ts) >= min_trades:
            out.append({"combo": key, "trades": len(ts), "wr": wr, "rr": avg_rr(ts)})
    return sorted(out, key=lambda x: x["wr"], reverse=True)

# ================================================================
# CSV OUTPUT
# ================================================================
def write_csv(trades):
    path = "backtest_results.csv"
    fields = ["time","instrument","name","tf","direction","hour","session",
              "score","signals","signal_count","result","rr","hold_bars","phase"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(trades)
    return path

# ================================================================
# HTML REPORT
# ================================================================
def _pct_color(v):
    if v >= 60: return "#00c853"
    if v >= 50: return "#ffd600"
    return "#ff6b35"

def build_html(all_trades, strat_s, inst_s, sess_s, combos,
               equity, train_trades, test_trades,
               period_start, period_end, train_cutoff):

    total     = len(all_trades)
    wr_all    = win_rate(all_trades)
    rr_all    = avg_rr(all_trades)
    sharpe    = calc_sharpe(equity)
    dd        = calc_maxdd(equity)
    final_bal = equity[-1] if equity else START_BALANCE
    pnl_pct   = round((final_bal - START_BALANCE) / START_BALANCE * 100, 1)
    train_wr  = win_rate(train_trades)
    test_wr   = win_rate(test_trades)

    eq_json  = str(equity)
    lbl_json = str(list(range(len(equity))))

    def trow4(a, b, c, d, bold=False):
        w = "font-weight:bold;" if bold else ""
        return "<tr><td style='" + w + "'>" + str(a) + "</td><td>" + str(b) + \
               "</td><td style='color:" + _pct_color(c) + "'>" + str(c) + "%" + \
               "</td><td>" + str(d) + "</td></tr>"

    strat_rows = ""
    for s, d in strat_s.items():
        flag = " &star;" if d["wr"] >= 60 and d["trades"] >= 5 else ""
        strat_rows += trow4(s + flag, d["trades"], d["wr"], d["rr"],
                            bold=(d["wr"] >= 60 and d["trades"] >= 5))

    inst_rows = ""
    for name, d in inst_s.items():
        inst_rows += trow4(name, d["trades"], d["wr"], d["rr"])

    sess_rows = ""
    for sess, d in sess_s.items():
        sess_rows += trow4(sess, d["trades"], d["wr"], d["rr"])

    combo_rows = ""
    for c in combos[:25]:
        combo_rows += trow4(c["combo"], c["trades"], c["wr"], c["rr"])
    if not combo_rows:
        combo_rows = "<tr><td colspan='4' style='color:#546e7a'>No combos met 60% WR + {} trade minimum</td></tr>".format(5)

    html = (
        "<!DOCTYPE html><html lang='en'><head>"
        "<meta charset='UTF-8'>"
        "<title>APEX Backtest Report</title>"
        "<style>"
        "*{margin:0;padding:0;box-sizing:border-box}"
        "body{background:#0a0e1a;color:#e0e6ff;font-family:monospace;padding:20px;font-size:13px}"
        "h1{color:#00c853;font-size:20px;margin-bottom:4px}"
        "h2{color:#7986cb;font-size:13px;margin:22px 0 8px;text-transform:uppercase;letter-spacing:1px}"
        ".note{color:#546e7a;font-size:11px;margin-bottom:16px}"
        ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:16px}"
        ".card{background:#131929;border:1px solid #1e2a45;border-radius:5px;padding:12px;text-align:center}"
        ".card .val{font-size:20px;font-weight:bold;color:#00c853}"
        ".card .lbl{font-size:10px;color:#7986cb;margin-top:3px}"
        ".wf{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:16px}"
        ".wf-card{background:#131929;border:1px solid #1e2a45;border-radius:5px;padding:14px;text-align:center}"
        ".wf-lbl{color:#7986cb;font-size:11px}"
        ".wf-val{font-size:26px;font-weight:bold;margin:6px 0}"
        ".wf-sub{font-size:11px;color:#546e7a}"
        "table{width:100%;border-collapse:collapse;font-size:12px;margin-bottom:16px}"
        "th{background:#0f1724;color:#7986cb;padding:7px 8px;text-align:left;border-bottom:1px solid #1e2a45}"
        "td{padding:5px 8px;border-bottom:1px solid #111827}"
        "tr:hover td{background:#0f1724}"
        "canvas{display:block;width:100%;height:200px;background:#0a0e1a;border:1px solid #1e2a45;border-radius:4px}"
        ".warn{background:#1a1200;border:1px solid #3d2e00;border-radius:4px;padding:10px;color:#ffd600;font-size:11px;margin-bottom:14px}"
        "</style></head><body>"
        "<h1>APEX Backtest Report</h1>"
        "<p class='note'>Generated: " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") +
        " | Period: " + period_start.strftime("%Y-%m-%d") +
        " to " + period_end.strftime("%Y-%m-%d") +
        " | Walk-forward split: " + train_cutoff.strftime("%Y-%m-%d") + "</p>"
        "<div class='warn'>Backtest covers H1/H4/D timeframes. M5/M15 scalp data excluded (too many candles for single pull). "
        "Results reflect swing and intermediate signals only. Session filters and 200 EMA gate applied exactly as in bot.py. "
        "Simulated TP/SL uses candle High/Low - actual fills may vary due to spread and slippage.</div>"
        "<div class='grid'>"
        "<div class='card'><div class='val'>" + str(total) + "</div><div class='lbl'>Total Signals</div></div>"
        "<div class='card'><div class='val' style='color:" + _pct_color(wr_all) + "'>" + str(wr_all) + "%</div><div class='lbl'>Overall Win Rate</div></div>"
        "<div class='card'><div class='val'>" + str(rr_all) + "</div><div class='lbl'>Avg Risk:Reward</div></div>"
        "<div class='card'><div class='val'>" + str(sharpe) + "</div><div class='lbl'>Sharpe Ratio</div></div>"
        "<div class='card'><div class='val' style='color:#ff6b35'>" + str(dd) + "%</div><div class='lbl'>Max Drawdown</div></div>"
        "<div class='card'><div class='val' style='color:" + ("#00c853" if pnl_pct >= 0 else "#ff6b35") + "'>" + str(pnl_pct) + "%</div><div class='lbl'>Sim P&amp;L (1% risk)</div></div>"
        "</div>"

        "<h2>Walk-Forward Validation</h2>"
        "<div class='wf'>"
        "<div class='wf-card'><div class='wf-lbl'>TRAIN PERIOD (4 months)</div>"
        "<div class='wf-val' style='color:" + _pct_color(train_wr) + "'>" + str(train_wr) + "%</div>"
        "<div class='wf-sub'>" + str(len(train_trades)) + " signals</div></div>"
        "<div class='wf-card'><div class='wf-lbl'>TEST PERIOD (2 months, unseen)</div>"
        "<div class='wf-val' style='color:" + _pct_color(test_wr) + "'>" + str(test_wr) + "%</div>"
        "<div class='wf-sub'>" + str(len(test_trades)) + " signals</div></div>"
        "</div>"

        "<h2>Equity Curve (simulated, 1% risk/trade, $10k start)</h2>"
        "<canvas id='eq'></canvas>"

        "<h2>Strategy Performance (&star; = 60%+ WR, 5+ trades)</h2>"
        "<table><tr><th>Strategy</th><th>Signals</th><th>Win Rate</th><th>Avg RR</th></tr>"
        + strat_rows + "</table>"

        "<h2>Instrument Performance</h2>"
        "<table><tr><th>Instrument</th><th>Signals</th><th>Win Rate</th><th>Avg RR</th></tr>"
        + inst_rows + "</table>"

        "<h2>Session Performance</h2>"
        "<table><tr><th>Session</th><th>Signals</th><th>Win Rate</th><th>Avg RR</th></tr>"
        + sess_rows + "</table>"

        "<h2>Top Combos (60%+ WR, 5+ signals)</h2>"
        "<table><tr><th>Instrument | Timeframe | Session</th><th>Signals</th><th>Win Rate</th><th>Avg RR</th></tr>"
        + combo_rows + "</table>"

        "<script>"
        "(function(){"
        "var eq=" + eq_json + ";"
        "var cv=document.getElementById('eq');"
        "cv.width=cv.parentElement.clientWidth||900;"
        "cv.height=200;"
        "var ctx=cv.getContext('2d');"
        "if(!eq||eq.length<2)return;"
        "var W=cv.width,H=cv.height,pad=32;"
        "var mn=Math.min.apply(null,eq),mx=Math.max.apply(null,eq);"
        "var rng=mx-mn||1;"
        "function px(i){return pad+(i/(eq.length-1))*(W-2*pad);}"
        "function py(v){return pad+(1-(v-mn)/rng)*(H-2*pad);}"
        "ctx.fillStyle='#0a0e1a';ctx.fillRect(0,0,W,H);"
        "var peak=eq[0];"
        "ctx.beginPath();"
        "for(var i=0;i<eq.length;i++){if(eq[i]>peak)peak=eq[i];var x=px(i),y=py(peak);if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);}"
        "for(var i=eq.length-1;i>=0;i--){ctx.lineTo(px(i),py(eq[i]));}"
        "ctx.closePath();ctx.fillStyle='rgba(255,107,53,0.12)';ctx.fill();"
        "ctx.beginPath();ctx.strokeStyle='#00c853';ctx.lineWidth=1.5;"
        "for(var i=0;i<eq.length;i++){var x=px(i),y=py(eq[i]);if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);}"
        "ctx.stroke();"
        "var base=" + str(int(START_BALANCE)) + ";"
        "if(mn<base&&mx>base){"
        "var by=py(base);"
        "ctx.beginPath();ctx.strokeStyle='rgba(255,255,255,0.18)';ctx.lineWidth=0.8;ctx.setLineDash([5,5]);"
        "ctx.moveTo(pad,by);ctx.lineTo(W-pad,by);ctx.stroke();ctx.setLineDash([]);"
        "}"
        "ctx.fillStyle='#546e7a';ctx.font='10px monospace';"
        "ctx.fillText('$'+Math.round(mx).toLocaleString(),pad+4,pad+12);"
        "ctx.fillText('$'+Math.round(mn).toLocaleString(),pad+4,H-pad-4);"
        "ctx.fillText('$'+Math.round(eq[eq.length-1]).toLocaleString(),W-pad-60,py(eq[eq.length-1])-6);"
        "})();"
        "</script>"
        "</body></html>"
    )
    return html

# ================================================================
# MAIN
# ================================================================
def main():
    now          = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    period_start = now - timedelta(days=LOOKBACK_DAYS)
    train_cutoff = period_start + timedelta(days=TRAIN_DAYS)

    print("=" * 58)
    print("APEX BACKTESTING ENGINE")
    print("Period : {} -> {}".format(
        period_start.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")))
    print("Train  : {} -> {}".format(
        period_start.strftime("%Y-%m-%d"), train_cutoff.strftime("%Y-%m-%d")))
    print("Test   : {} -> {}".format(
        train_cutoff.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")))
    print("=" * 58)

    all_trades    = []
    total_instr   = len(INSTRUMENTS)
    skipped_tfs   = []

    for idx, (iid, ii) in enumerate(INSTRUMENTS.items(), 1):
        print("\n[{}/{}] {}".format(idx, total_instr, ii["n"]))

        for tf in ii["tfs"]:
            print("  {} {} - fetching...".format(iid, tf), end=" ", flush=True)
            candles = fetch_candles(iid, tf, period_start, now)

            if len(candles) < 60:
                print("SKIP ({} candles - not enough data)".format(len(candles)))
                skipped_tfs.append("{} {}".format(iid, tf))
                continue

            print("{} candles  |  backtesting...".format(len(candles)), end=" ", flush=True)
            trades = backtest_tf(iid, ii, tf, candles, train_cutoff)
            all_trades.extend(trades)

            decided = [t for t in trades if t["result"] != "TIMEOUT"]
            wr = win_rate(trades)
            print("{} signals, WR: {}%".format(len(trades), wr))
            time.sleep(0.25)  # respect OANDA rate limits

    print("\n" + "=" * 58)
    print("COMPLETE: {} total signals across all instruments/TFs".format(len(all_trades)))

    if not all_trades:
        print("No signals generated. Check API key and data availability.")
        return

    train_trades = [t for t in all_trades if t["phase"] == "TRAIN"]
    test_trades  = [t for t in all_trades if t["phase"] == "TEST"]

    strat_s  = strategy_stats(all_trades)
    inst_s   = instrument_stats(all_trades)
    sess_s   = session_stats(all_trades)
    combos   = top_combos(all_trades)
    equity   = build_equity(all_trades)

    print("\n--- SUMMARY ---")
    print("Overall WR   : {}%  (decided trades only)".format(win_rate(all_trades)))
    print("Train WR     : {}%  ({} signals)".format(win_rate(train_trades), len(train_trades)))
    print("Test WR      : {}%  ({} signals)".format(win_rate(test_trades), len(test_trades)))
    print("Avg RR       : {}".format(avg_rr(all_trades)))
    print("Sharpe       : {}".format(calc_sharpe(equity)))
    print("Max Drawdown : {}%".format(calc_maxdd(equity)))
    print("Final Balance: ${:.2f}  (started ${:.2f})".format(equity[-1], START_BALANCE))
    print("60%+ combos  : {}".format(len(combos)))

    print("\n--- TOP COMBOS ---")
    for c in combos[:10]:
        print("  {}  |  {}t  |  {}% WR  |  {} RR".format(
            c["combo"], c["trades"], c["wr"], c["rr"]))

    if skipped_tfs:
        print("\n--- SKIPPED (insufficient data) ---")
        for s in skipped_tfs: print("  " + s)

    print("\n--- TOP STRATEGIES ---")
    for s, d in list(strat_s.items())[:8]:
        flag = " *" if d["wr"] >= 60 and d["trades"] >= 5 else ""
        print("  {:<16} {}t  {}% WR  {} RR{}".format(
            s, d["trades"], d["wr"], d["rr"], flag))

    csv_path  = write_csv(all_trades)
    print("\nCSV  -> " + csv_path)

    html_path = "backtest_report.html"
    html = build_html(all_trades, strat_s, inst_s, sess_s, combos,
                      equity, train_trades, test_trades,
                      period_start, now, train_cutoff)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print("HTML -> " + html_path)
    print("\nDone. Open backtest_report.html in Chrome.")

if __name__ == "__main__":
    main()
