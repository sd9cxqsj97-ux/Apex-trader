from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests, os, csv, json as json_mod

app = Flask(__name__)
CORS(app)

AK      = os.environ.get("OANDA_API_KEY",  "bf70bcd936733bc516622f1dbdc1dacb-b9439874df7ea93be60da791a9252f15")
CAK     = os.environ.get("CLAUDE_API_KEY", "")
BOT_DIR = os.environ.get("BOT_DIR",        "/home/apexbot/apex")

# ================================================================
# OANDA PROXY
# ================================================================
@app.route("/oanda/<path:path>", methods=["GET","POST","PUT"])
def oanda(path):
    url     = "https://api-fxpractice.oanda.com/v3/" + path
    headers = {"Authorization": "Bearer " + AK, "Content-Type": "application/json"}
    params  = dict(request.args)
    try:
        if request.method == "GET":
            r = requests.get(url, headers=headers, params=params, timeout=15)
        elif request.method == "POST":
            r = requests.post(url, headers=headers, json=request.get_json(), timeout=15)
        else:
            r = requests.put(url, headers=headers, json=request.get_json(), timeout=15)
        return Response(r.text, status=r.status_code, mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ================================================================
# CLAUDE AI PROXY
# ================================================================
@app.route("/claude", methods=["POST"])
def claude():
    if not CAK:
        return jsonify({"error": "CLAUDE_API_KEY not set"}), 503
    data = request.get_json()
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": CAK, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=data, timeout=30)
        return Response(r.text, status=r.status_code, mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ================================================================
# BOT DATA ENDPOINTS — used by dashboard
# ================================================================
@app.route("/api/status")
def api_status():
    """Current bot state: balance, open trades, wins/losses"""
    try:
        with open(os.path.join(BOT_DIR, "state.json")) as f:
            return jsonify(json_mod.load(f))
    except Exception as e:
        return jsonify({"error": str(e), "trades": {}, "wins": 0, "losses": 0})

@app.route("/api/log")
def api_log():
    """Last N lines of the live bot log"""
    n = int(request.args.get("n", 60))
    try:
        with open(os.path.join(BOT_DIR, "trading_bot.log"), "r",
                  encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return jsonify({"lines": [l.rstrip() for l in lines[-n:]]})
    except Exception as e:
        return jsonify({"lines": [], "error": str(e)})

@app.route("/api/signals")
def api_signals():
    """Last N rows from signal_log.csv"""
    n = int(request.args.get("n", 200))
    try:
        rows = []
        path = os.path.join(BOT_DIR, "signal_log.csv")
        with open(path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows.append(dict(row))
        return jsonify({"signals": rows[-n:]})
    except Exception as e:
        return jsonify({"signals": [], "error": str(e)})

@app.route("/api/trades")
def api_trades():
    """All completed trades from trade_log.csv"""
    try:
        rows = []
        path = os.path.join(BOT_DIR, "trade_log.csv")
        with open(path, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows.append(dict(row))
        return jsonify({"trades": rows})
    except Exception as e:
        return jsonify({"trades": [], "error": str(e)})

@app.route("/api/binance")
def api_binance():
    """BTC price and 24h stats from Binance (no key needed)"""
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT", timeout=8)
        return Response(r.text, status=r.status_code, mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/feargreed")
def api_feargreed():
    """Crypto Fear & Greed index"""
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8)
        return Response(r.text, status=r.status_code, mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ================================================================
# HEALTH CHECK
# ================================================================
@app.route("/api/ftmo")
def api_ftmo():
    """FTMO challenge state — balance, daily loss, drawdown, days traded"""
    try:
        import json as _json
        with open(os.path.join(BOT_DIR, "state.json")) as f:
            st = _json.load(f)
        return jsonify({
            "ftmo_daily_loss":  st.get("ftmo_daily_loss", 0.0),
            "ftmo_peak_bal":    st.get("ftmo_peak_bal", 10000.0),
            "ftmo_days_traded": st.get("ftmo_days_traded", []),
            "wins":  st.get("wins", 0),
            "losses":st.get("losses", 0),
        })
    except Exception as e:
        return jsonify({"error": str(e), "ftmo_daily_loss": 0, "ftmo_peak_bal": 10000, "ftmo_days_traded": []})

@app.route("/api/ping")
def ping():
    return jsonify({
        "status":   "ok",
        "ai":       "on" if CAK else "off",
        "bot_dir":  BOT_DIR
    })

if __name__ == "__main__":
    print("APEX Proxy V5 running on http://localhost:5000")
    print("OANDA: OK  |  Claude AI: " + ("OK" if CAK else "NOT SET"))
    app.run(host="0.0.0.0", port=5000, debug=False)
