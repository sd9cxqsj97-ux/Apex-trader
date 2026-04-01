#!/usr/bin/env python3

# APEX AUTO-TRADER

# Swing: all forex H4/D

# Scalp+Swing: Gold, Oil H1/H4/D

# Dynamic risk | Stoch+RSI+EMA | 3-of-5 exit

import os, time, json, logging, math, requests
from datetime import datetime, timezone

# CONFIG

API_KEY    = os.environ.get('OANDA_API_KEY')
ACCOUNT_ID = os.environ.get('OANDA_ACCOUNT_ID')
BASE_URL   = “https://api-fxpractice.oanda.com/v3”
HEADERS    = {“Authorization”: f”Bearer {API_KEY}”, “Content-Type”: “application/json”}

# Instrument profiles

INSTRUMENTS = {
# Forex  swing only
“EUR_USD”:   {“name”:“EUR/USD”,  “type”:“forex”,  “scalp”:False},
“GBP_USD”:   {“name”:“GBP/USD”,  “type”:“forex”,  “scalp”:False},
“USD_JPY”:   {“name”:“USD/JPY”,  “type”:“forex”,  “scalp”:False},
“AUD_USD”:   {“name”:“AUD/USD”,  “type”:“forex”,  “scalp”:False},
“USD_CAD”:   {“name”:“USD/CAD”,  “type”:“forex”,  “scalp”:False},
“USD_CHF”:   {“name”:“USD/CHF”,  “type”:“forex”,  “scalp”:False},
“NZD_USD”:   {“name”:“NZD/USD”,  “type”:“forex”,  “scalp”:False},
“EUR_GBP”:   {“name”:“EUR/GBP”,  “type”:“forex”,  “scalp”:False},
“EUR_JPY”:   {“name”:“EUR/JPY”,  “type”:“forex”,  “scalp”:False},
“GBP_JPY”:   {“name”:“GBP/JPY”,  “type”:“forex”,  “scalp”:False},
“AUD_JPY”:   {“name”:“AUD/JPY”,  “type”:“forex”,  “scalp”:False},
“EUR_AUD”:   {“name”:“EUR/AUD”,  “type”:“forex”,  “scalp”:False},
“GBP_CAD”:   {“name”:“GBP/CAD”,  “type”:“forex”,  “scalp”:False},
# Gold + Silver  swing + scalp
“XAU_USD”:   {“name”:“Gold”,     “type”:“metal”,  “scalp”:True},
“XAG_USD”:   {“name”:“Silver”,   “type”:“metal”,  “scalp”:False},
# Oil  swing + scalp
“BCO_USD”:   {“name”:“Brent”,    “type”:“energy”, “scalp”:True},
“WTICO_USD”: {“name”:“WTI Oil”,  “type”:“energy”, “scalp”:True},
# Indices  swing only
“SPX500_USD”:{“name”:“S&P 500”,  “type”:“index”,  “scalp”:False},
“NAS100_USD”:{“name”:“Nasdaq”,   “type”:“index”,  “scalp”:False},
“US30_USD”:  {“name”:“Dow 30”,   “type”:“index”,  “scalp”:False},
}

# Timeframes per instrument type

TIMEFRAMES = {
“swing_only”: [“H4”, “D”],
“swing_scalp”: [“H1”, “H4”, “D”],
}

# DYNAMIC RISK TIERS

RISK_TIERS = [
{“min”:    0, “max”:  1000, “lots”:0.10, “risk_pct”:0.10, “daily_loss_pct”:0.20},
{“min”: 1000, “max”:  2000, “lots”:0.20, “risk_pct”:0.07, “daily_loss_pct”:0.15},
{“min”: 2000, “max”:  3500, “lots”:0.50, “risk_pct”:0.05, “daily_loss_pct”:0.12},
{“min”: 3500, “max”:  5000, “lots”:1.00, “risk_pct”:0.03, “daily_loss_pct”:0.10},
{“min”: 5000, “max”:999999, “lots”:1.00, “risk_pct”:0.02, “daily_loss_pct”:0.08},
]

# Scalp vs swing trade parameters

TRADE_PARAMS = {
“scalp”: {“tp_atr”:1.5, “sl_atr”:0.7, “trail_start”:1.0, “trail_step”:0.3, “max_hold_hours”:4},
“swing”: {“tp_atr”:2.5, “sl_atr”:1.0, “trail_start”:1.5, “trail_step”:0.5, “max_hold_hours”:999},
}

MIN_SCORE       = 85
MAX_OPEN_TRADES = 5
STATE_FILE      = “state.json”

logging.basicConfig(level=logging.INFO, format=”%(asctime)s [%(levelname)s] %(message)s”)
log = logging.getLogger(“APEX”)

# STATE

def load_state():
try:
with open(STATE_FILE) as f:
return json.load(f)
except:
return {“open_trades”:{}, “daily_loss”:0.0, “daily_date”:””,
“total”:0, “wins”:0, “losses”:0, “journal”:[], “consecutive_losses”:0}

def save_state(s):
with open(STATE_FILE,“w”) as f:
json.dump(s, f, indent=2, default=str)

# OANDA

def og(path):
r = requests.get(f”{BASE_URL}{path}”, headers=HEADERS, timeout=15)
r.raise_for_status()
return r.json()

def op(path, body):
r = requests.post(f”{BASE_URL}{path}”, headers=HEADERS, json=body, timeout=15)
r.raise_for_status()
return r.json()

def oput(path, body):
r = requests.put(f”{BASE_URL}{path}”, headers=HEADERS, json=body, timeout=15)
r.raise_for_status()
return r.json()

def get_account():
return og(f”/accounts/{ACCOUNT_ID}/summary”)[“account”]

def get_open_trades_api():
return og(f”/accounts/{ACCOUNT_ID}/openTrades”)[“trades”]

def get_candles(inst, gran, count=100):
return og(f”/instruments/{inst}/candlescount={count}&granularity={gran}&price=M”)[“candles”]

def get_price(inst):
d = og(f”/accounts/{ACCOUNT_ID}/pricinginstruments={inst}”)[“prices”][0]
return float(d[“bids”][0][“price”]), float(d[“asks”][0][“price”])

def place_order(inst, direction, lots, tp, sl):
units = lots * 10000
if direction == “SHORT”:
units = -units
# Adjust units for metals/indices
if “XAU” in inst:  units = lots * 100
if “XAG” in inst:  units = lots * 5000
if “BCO” in inst or “WTICO” in inst: units = lots * 1000
if “SPX” in inst or “NAS” in inst or “US30” in inst: units = lots * 10
if direction == “SHORT”: units = -abs(units)
body = {“order”: {
“type”:“MARKET”, “instrument”:inst,
“units”: str(int(units)),
“takeProfitOnFill”: {“price”: f”{tp:.5f}”},
“stopLossOnFill”:   {“price”: f”{sl:.5f}”},
“timeInForce”:“FOK”, “positionFill”:“DEFAULT”
}}
return op(f”/accounts/{ACCOUNT_ID}/orders”, body)

def close_trade(tid):
return oput(f”/accounts/{ACCOUNT_ID}/trades/{tid}/close”, {})

def set_sl(tid, sl):
return oput(f”/accounts/{ACCOUNT_ID}/trades/{tid}/orders”,
{“stopLoss”:{“price”:f”{sl:.5f}”,“timeInForce”:“GTC”}})

# TECHNICALS

def extract(candles):
c = [x for x in candles if x[“complete”]]
return ([float(x[“mid”][“c”]) for x in c],
[float(x[“mid”][“h”]) for x in c],
[float(x[“mid”][“l”]) for x in c],
[float(x[“volume”])   for x in c])

def rsi(closes, p=14):
if len(closes) < p+1: return 50.0
g=l=0
for i in range(len(closes)-p, len(closes)):
d = closes[i]-closes[i-1]
if d>0: g+=d
else:   l+=abs(d)
ag,al = g/p, l/p
return 100.0 if al==0 else 100-(100/(1+ag/al))

def ema(closes, p):
k=2/(p+1); e=closes[0]
for c in closes[1:]: e=c*k+e*(1-k)
return e

def stoch(closes, highs, lows, kp=14, dp=3):
if len(closes) < kp+dp: return 50,50,50,50
ks=[]
for i in range(kp-1, len(closes)):
h=max(highs[i-kp+1:i+1]); l=min(lows[i-kp+1:i+1])
ks.append(50.0 if h==l else ((closes[i]-l)/(h-l))*100)
if len(ks)<dp: return ks[-1],ks[-1],ks[-1],ks[-1]
k=ks[-1]; d=sum(ks[-dp:])/dp
kp2=ks[-2] if len(ks)>=2 else k
dp2=sum(ks[-dp-1:-1])/dp if len(ks)>dp else d
return k,d,kp2,dp2

def atr(candles, p=14):
c=[x for x in candles if x[“complete”]]
if len(c)<p: return 0.0
trs=[]
for i in range(1,len(c)):
h=float(c[i][“mid”][“h”]); l=float(c[i][“mid”][“l”]); pc=float(c[i-1][“mid”][“c”])
trs.append(max(h-l,abs(h-pc),abs(l-pc)))
return sum(trs[-p:])/p

def vol_ratio(vols):
if len(vols)<5: return 1.0
avg=sum(vols[-20:-1])/max(1,len(vols[-20:-1]))
return vols[-1]/avg if avg>0 else 1.0

def score(RSI, VR, e20d, e50d, chg, sk, sd):
s=50
if RSI<30 or RSI>70: s+=20
elif RSI<40 or RSI>60: s+=8
if VR>2.5: s+=15
elif VR>1.4: s+=8
if (e20d>0 and e50d>0) or (e20d<0 and e50d<0): s+=10
if abs(chg)>1: s+=10
elif abs(chg)>0.4: s+=5
if sk<20 or sk>80: s+=10
return min(99,max(1,round(s)))

def direction(RSI, e20d, e50d, chg, sk, sd, sk_prev, sd_prev):
bull_ema = e20d>0 and e50d>0
bear_ema = e20d<0 and e50d<0
stoch_bull = sk_prev<sd_prev and sk>sd and sk<30
stoch_bear = sk_prev>sd_prev and sk<sd and sk>70
bp = (1 if RSI<35 else 0)+(1 if bull_ema else 0)+(1 if chg>0 else 0)+(1 if stoch_bull else 0)
brp= (1 if RSI>65 else 0)+(1 if bear_ema else 0)+(1 if chg<0 else 0)+(1 if stoch_bear else 0)
if bp>=3 and stoch_bull: return “LONG”
if brp>=3 and stoch_bear: return “SHORT”
return “NONE”

# REVERSAL CHECK (3 of 5)

def reversal(dir_, RSI, e20d, candles, closes, vols, sk, sd, sk_prev, sd_prev):
hits=0
if dir_==“LONG”:
if RSI>70: hits+=1; log.info(“Rev 1: RSI OB”)
if e20d<0: hits+=1; log.info(“Rev 2: Price below EMA20”)
else:
if RSI<30: hits+=1; log.info(“Rev 1: RSI OS”)
if e20d>0: hits+=1; log.info(“Rev 2: Price above EMA20”)
c=[x for x in candles if x[“complete”]]
if len(c)>=2:
po=float(c[-2][“mid”][“o”]); pc_=float(c[-2][“mid”][“c”])
lo=float(c[-1][“mid”][“o”]); lc=float(c[-1][“mid”][“c”])
if dir_==“LONG” and lc<lo and abs(lc-lo)>abs(pc_-po): hits+=1; log.info(“Rev 3: Bear engulf”)
elif dir_==“SHORT” and lc>lo and abs(lc-lo)>abs(pc_-po): hits+=1; log.info(“Rev 3: Bull engulf”)
vr=vol_ratio(vols)
if vr>2.0 and len(c)>0:
lc_=float(c[-1][“mid”][“c”]); lo_=float(c[-1][“mid”][“o”])
if dir_==“LONG” and lc_<lo_: hits+=1; log.info(f”Rev 4: Vol spike bear {vr:.1f}x”)
elif dir_==“SHORT” and lc_>lo_: hits+=1; log.info(f”Rev 4: Vol spike bull {vr:.1f}x”)
if dir_==“LONG” and sk_prev>sd_prev and sk<sd and sk>70: hits+=1; log.info(“Rev 5: Stoch bear cross”)
elif dir_==“SHORT” and sk_prev<sd_prev and sk>sd and sk<30: hits+=1; log.info(“Rev 5: Stoch bull cross”)
log.info(f”Reversal conditions: {hits}/5”)
return hits>=3

# RISK

def get_tier(bal):
for t in RISK_TIERS:
if t[“min”]<=bal<t[“max”]: return t
return RISK_TIERS[-1]

def daily_reset(state):
today=datetime.now(timezone.utc).strftime(”%Y-%m-%d”)
if state[“daily_date”]!=today:
state[“daily_loss”]=0.0; state[“daily_date”]=today
state[“consecutive_losses”]=0
log.info(f”New trading day: {today}”)
return state

def can_trade(state, bal):
tier=get_tier(bal)
limit=bal*tier[“daily_loss_pct”]
if state[“daily_loss”]>=limit:
log.warning(f”Daily loss limit: {state[‘daily_loss’]:.2f}/{limit:.2f}  paused”)
return False
if state[“consecutive_losses”]>=3:
log.warning(“3 consecutive losses  pausing 4 hours”)
return False
return True

def bad_time():
now=datetime.now(timezone.utc)
if now.weekday()==0 and now.hour<10: return True   # Mon pre-10am
if now.weekday()==4 and now.hour>=15: return True  # Fri post-3pm
return False

# MANAGE OPEN TRADES

def manage_trades(state):
if not state[“open_trades”]: return state
open_api={t[“id”]:t for t in get_open_trades_api()}

```
for tid, info in list(state["open_trades"].items()):
    # Closed externally (hit TP or SL)
    if tid not in open_api:
        pl = float(info.get("unrealized_pl",0))
        result="WIN" if pl>=0 else "LOSS"
        if result=="WIN": state["wins"]+=1; state["consecutive_losses"]=0
        else: state["losses"]+=1; state["daily_loss"]+=abs(pl); state["consecutive_losses"]+=1
        state["journal"].append({
            "id":tid,"instrument":info["instrument"],"direction":info["direction"],
            "entry":info["entry"],"result":result,"pl":round(pl,2),
            "trade_type":info.get("trade_type","swing"),
            "closed_at":datetime.now(timezone.utc).isoformat()
        })
        del state["open_trades"][tid]
        log.info(f"Trade {tid} closed  {result} PL:{pl:.2f}")
        continue

    # Update unrealized PL
    try:
        bid,ask = get_price(info["instrument"])
        mid=(bid+ask)/2
        dir_=info["direction"]
        entry=info["entry"]
        atr_v=info["atr"]
        params=TRADE_PARAMS[info.get("trade_type","swing")]

        profit_atr = ((mid-entry)/atr_v if dir_=="LONG" else (entry-mid)/atr_v) if atr_v>0 else 0
        info["unrealized_pl"] = round((mid-entry if dir_=="LONG" else entry-mid)*info.get("units",1), 4)

        # Breakeven
        if profit_atr>=1.0 and not info.get("be_set"):
            try: set_sl(tid, entry); info["current_sl"]=entry; info["be_set"]=True; log.info(f"{tid} breakeven set")
            except: pass

        # Trailing stop
        if profit_atr>=params["trail_start"]:
            new_sl = (mid-atr_v*params["trail_step"] if dir_=="LONG" else mid+atr_v*params["trail_step"])
            cur_sl = info.get("current_sl",0)
            if (dir_=="LONG" and new_sl>cur_sl) or (dir_=="SHORT" and new_sl<cur_sl):
                try: set_sl(tid, new_sl); info["current_sl"]=new_sl; log.info(f"{tid} trail  {new_sl:.5f}")
                except: pass

        # Scalp max hold time
        if info.get("trade_type")=="scalp":
            opened=datetime.fromisoformat(info["opened_at"])
            age_h=(datetime.now(timezone.utc)-opened).total_seconds()/3600
            if age_h>params["max_hold_hours"]:
                try: close_trade(tid); log.info(f"{tid} scalp max hold hit  closed"); continue
                except: pass

        # Reversal check on H4
        candles=get_candles(info["instrument"],"H4",60)
        closes,highs,lows,vols=extract(candles)
        if len(closes)>20:
            RSI=rsi(closes)
            e20=ema(closes,min(20,len(closes))); e20d=((mid-e20)/e20)*100
            e50=ema(closes,min(50,len(closes))); e50d=((mid-e50)/e50)*100
            sk,sd,skp,sdp=stoch(closes,highs,lows)
            if reversal(dir_,RSI,e20d,candles,closes,vols,sk,sd,skp,sdp):
                try: close_trade(tid); log.info(f"{tid} early exit  reversal confirmed"); continue
                except: pass

    except Exception as e:
        log.warning(f"Error managing trade {tid}: {e}")

save_state(state)
return state
```

# SCAN & ENTER

def scan_and_trade(state):
try:
acc=get_account()
bal=float(acc[“balance”])
tier=get_tier(bal)
lots=tier[“lots”]
open_count=len(state[“open_trades”])
open_instruments={v[“instrument”] for v in state[“open_trades”].values()}

```
    log.info(f"Balance: {bal:.2f} | Tier: {lots} lots | Open: {open_count}/{MAX_OPEN_TRADES}")

    if open_count>=MAX_OPEN_TRADES:
        log.info("Max open trades reached  skipping scan")
        return state

    if not can_trade(state, bal):
        return state

    if bad_time():
        log.info("Bad trading time  skipping")
        return state

    for inst_id, inst_info in INSTRUMENTS.items():
        if inst_id in open_instruments:
            continue
        if open_count>=MAX_OPEN_TRADES:
            break

        scalp_allowed = inst_info["scalp"]
        timeframes = TIMEFRAMES["swing_scalp"] if scalp_allowed else TIMEFRAMES["swing_only"]

        for tf in timeframes:
            try:
                candles=get_candles(inst_id, tf, 100)
                if len(candles)<30: continue
                closes,highs,lows,vols=extract(candles)
                if len(closes)<20: continue

                bid,ask=get_price(inst_id)
                mid=(bid+ask)/2

                RSI=rsi(closes)
                e20=ema(closes,min(20,len(closes)))
                e50=ema(closes,min(50,len(closes)))
                e20d=((mid-e20)/e20)*100
                e50d=((mid-e50)/e50)*100
                VR=vol_ratio(vols)
                chg=(closes[0]-closes[-1])/closes[-1]*100 if closes[0]>0 else 0
                sk,sd,skp,sdp=stoch(closes,highs,lows)
                ATR=atr(candles)
                S=score(RSI,VR,e20d,e50d,chg,sk,sd)
                DIR=direction(RSI,e20d,e50d,chg,sk,sd,skp,sdp)

                log.info(f"{inst_id} {tf} | Score:{S} Dir:{DIR} RSI:{RSI:.0f} Stoch:{sk:.0f}/{sd:.0f}")

                if S<MIN_SCORE or DIR=="NONE":
                    continue

                # Determine scalp or swing
                is_scalp = scalp_allowed and tf=="H1"
                trade_type = "scalp" if is_scalp else "swing"
                params = TRADE_PARAMS[trade_type]

                if ATR==0:
                    log.warning(f"{inst_id} ATR=0  skipping")
                    continue

                if DIR=="LONG":
                    tp=ask+ATR*params["tp_atr"]
                    sl=ask-ATR*params["sl_atr"]
                    entry=ask
                else:
                    tp=bid-ATR*params["tp_atr"]
                    sl=bid+ATR*params["sl_atr"]
                    entry=bid

                log.info(f" SIGNAL: {inst_id} {DIR} {trade_type.upper()} | Entry:{entry:.5f} TP:{tp:.5f} SL:{sl:.5f} | Score:{S}")

                result=place_order(inst_id, DIR, lots, tp, sl)
                filled=result.get("orderFillTransaction",{})
                trade_id=filled.get("tradeOpened",{}).get("tradeID")

                if trade_id:
                    state["open_trades"][trade_id]={
                        "instrument":inst_id,"direction":DIR,
                        "entry":entry,"tp":tp,"sl":sl,
                        "current_sl":sl,"atr":ATR,"lots":lots,
                        "trade_type":trade_type,"timeframe":tf,
                        "score":S,"be_set":False,
                        "opened_at":datetime.now(timezone.utc).isoformat(),
                        "unrealized_pl":0
                    }
                    state["total"]+=1
                    open_count+=1
                    open_instruments.add(inst_id)
                    log.info(f" Trade opened: ID {trade_id} | {inst_id} {DIR} {trade_type}")
                    save_state(state)
                    break  # One trade per instrument across timeframes

            except Exception as e:
                log.warning(f"Error scanning {inst_id} {tf}: {e}")
                continue

except Exception as e:
    log.error(f"Scan error: {e}")

return state
```

# MAIN LOOP

def main():
log.info(”=” * 50)
log.info(“APEX AUTO-TRADER STARTED”)
log.info(f”Account: {ACCOUNT_ID}”)
log.info(f”Min Score: {MIN_SCORE} | Max Trades: {MAX_OPEN_TRADES}”)
log.info(”=” * 50)

```
state=load_state()

while True:
    try:
        state=daily_reset(state)
        log.info(" Managing open trades ")
        state=manage_trades(state)
        log.info(" Scanning for new setups ")
        state=scan_and_trade(state)
        save_state(state)

        wins=state["wins"]; losses=state["losses"]
        total=wins+losses
        wr=wins/total*100 if total>0 else 0
        log.info(f"Stats: {wins}W/{losses}L ({wr:.0f}%) | Daily loss: {state['daily_loss']:.2f}")
        log.info(f"Sleeping 4 hours until next scan...")
        time.sleep(4*60*60)

    except KeyboardInterrupt:
        log.info("Bot stopped by user")
        save_state(state)
        break
    except Exception as e:
        log.error(f"Main loop error: {e}")
        time.sleep(60)
```

if **name**==”**main**”:
main()
