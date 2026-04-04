from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests, os

app = Flask(__name__)
CORS(app)

AK  = os.environ.get("OANDA_API_KEY", "bf70bcd936733bc516622f1dbdc1dacb-b9439874df7ea93be60da791a9252f15")
CAK = os.environ.get("CLAUDE_API_KEY", "")  # Set CLAUDE_API_KEY env var for AI features

@app.route("/oanda/<path:path>", methods=["GET","POST","PUT"])
def oanda(path):
    url = f"https://api-fxpractice.oanda.com/v3/{path}"
    headers = {"Authorization": f"Bearer {AK}", "Content-Type": "application/json"}
    params  = dict(request.args)
    if request.method == "GET":
        r = requests.get(url, headers=headers, params=params, timeout=15)
    elif request.method == "POST":
        r = requests.post(url, headers=headers, json=request.get_json(), timeout=15)
    else:
        r = requests.put(url, headers=headers, json=request.get_json(), timeout=15)
    return Response(r.text, status=r.status_code, mimetype="application/json")

@app.route("/claude", methods=["POST"])
def claude():
    """Proxy Claude API calls from the browser (avoids exposing key in frontend)"""
    if not CAK:
        return jsonify({"error": "CLAUDE_API_KEY not set on proxy server"}), 503
    data = request.get_json()
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": CAK, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json=data, timeout=30
    )
    return Response(r.text, status=r.status_code, mimetype="application/json")

if __name__ == "__main__":
    print("APEX Proxy V4 running on http://localhost:5000")
    print("OANDA: OK  |  Claude AI: "+("OK" if CAK else "NOT SET — add CLAUDE_API_KEY env var"))
    app.run(port=5000, debug=False)
