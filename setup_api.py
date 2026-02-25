#!/usr/bin/env python3
"""
Order Block Setup API
=====================
Lightweight Flask API deployed to Render.com (free tier).
Receives setup data from v3_live_runner and model_b_live,
serves it to Pine Script via request.seed() (GitHub CSV).

CSV files pushed to GitHub:
  data/<SYMBOL>_prices.csv  — entry, ob_high, ob_low, sl, tp (mapped to open/high/low/close/volume)
  data/<SYMBOL>_meta.csv    — direction(1/-1), status(1/2/3), rr, source(1/2), timeframe(1/2)
                              also mapped to open/high/low/close/volume
"""

from flask import Flask, request, jsonify
from datetime import datetime, timezone
from collections import defaultdict
import os, base64, json
import urllib.request, urllib.error

app = Flask(__name__)

MAX_PER_SYMBOL = 10
store = defaultdict(list)

API_KEY       = os.environ.get('API_KEY', 'changeme')
GITHUB_TOKEN  = os.environ.get('GITHUB_TOKEN', '')
GITHUB_REPO   = os.environ.get('GITHUB_REPO', 'waringd/ob-setup-api')
GITHUB_BRANCH = 'main'


def check_auth():
    return request.headers.get('X-API-Key') == API_KEY


def github_put(path, content_str, commit_msg):
    """Create or update a file in the GitHub repo."""
    if not GITHUB_TOKEN:
        print("[GITHUB] No token, skipping")
        return

    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Content-Type":  "application/json",
        "Accept":        "application/vnd.github.v3+json",
    }

    # Get existing SHA if file exists
    sha = None
    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req) as resp:
            sha = json.loads(resp.read()).get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"[GITHUB] Check error {e.code}")

    payload = {
        "message": commit_msg,
        "content": base64.b64encode(content_str.encode()).decode(),
        "branch":  GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    try:
        data = json.dumps(payload).encode()
        req  = urllib.request.Request(api_url, data=data, headers=headers, method="PUT")
        with urllib.request.urlopen(req) as resp:
            print(f"[GITHUB] Pushed {path}")
    except urllib.error.HTTPError as e:
        print(f"[GITHUB] Push failed {e.code}: {e.read()}")


def push_csv_to_github(symbol):
    """
    Push two CSV files for this symbol.

    _prices.csv columns mapped to OHLCV:
      open=entry, high=ob_high, low=ob_low, close=sl, volume=tp

    _meta.csv columns mapped to OHLCV:
      open=direction (1=LONG, -1=SHORT)
      high=status    (1=active, 2=closed_tp, 3=closed_sl)
      low=rr
      close=source   (1=v3, 2=modelb)
      volume=timeframe (60=H1, 15=M15)

    Rows are ordered oldest first (Pine reads newest bar = last row).
    Row 0 in Pine (offset [0]) = most recent setup = last row in CSV.
    """
    setups = store.get(symbol, [])
    if not setups:
        return

    # store is newest-first, reverse for CSV (oldest first)
    ordered = list(reversed(setups))

    # Prices CSV
    price_lines = ["time,open,high,low,close,volume"]
    meta_lines  = ["time,open,high,low,close,volume"]

    for i, s in enumerate(ordered):
        t = s['time_unix']

        dir_val = 1 if s['direction'] == 'LONG' else -1
        status_map = {'active': 1, 'closed_tp': 2, 'closed_sl': 3}
        status_val = status_map.get(s['status'], 1)
        src_val = 1 if s['source'] == 'v3' else 2
        tf_val  = 60 if s['timeframe'] == 'H1' else 15

        price_lines.append(f"{t},{s['entry']},{s['ob_high']},{s['ob_low']},{s['sl']},{s['tp']}")
        meta_lines.append(f"{t},{dir_val},{status_val},{s['rr']},{src_val},{tf_val}")

    github_put(f"data/{symbol}_prices.csv", "\n".join(price_lines), f"update {symbol} prices")
    github_put(f"data/{symbol}_meta.csv",   "\n".join(meta_lines),  f"update {symbol} meta")


# ── Routes ───────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    total = sum(len(v) for v in store.values())
    return jsonify({'status': 'ok', 'time': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'), 'pairs': len(store), 'setups': total})


@app.route('/setup', methods=['POST'])
def add_setup():
    if not check_auth():
        return jsonify({'error': 'Unauthorised'}), 401

    data = request.get_json(force=True)
    required = ['symbol', 'direction', 'entry', 'sl', 'tp', 'ob_high', 'ob_low']
    for field in required:
        if field not in data:
            return jsonify({'error': f'Missing field: {field}'}), 400

    symbol = data['symbol'].upper()
    setup  = {
        'symbol':    symbol,
        'direction': data['direction'],
        'timeframe': data.get('timeframe', 'H1'),
        'entry':     float(data['entry']),
        'sl':        float(data['sl']),
        'tp':        float(data['tp']),
        'ob_high':   float(data['ob_high']),
        'ob_low':    float(data['ob_low']),
        'rr':        float(data.get('rr', 0)),
        'ticket':    int(data.get('ticket', 0)),
        'source':    data.get('source', 'unknown'),
        'status':    'active',
        'time':      datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
        'time_unix': int(datetime.now(timezone.utc).timestamp()),
    }

    store[symbol].insert(0, setup)
    store[symbol] = store[symbol][:MAX_PER_SYMBOL]
    push_csv_to_github(symbol)

    print(f"[SETUP] {symbol} {setup['direction']} | entry={setup['entry']} | ticket={setup['ticket']}")
    return jsonify({'status': 'ok', 'symbol': symbol, 'total': len(store[symbol])})


@app.route('/close', methods=['POST'])
def close_setup():
    if not check_auth():
        return jsonify({'error': 'Unauthorised'}), 401

    data    = request.get_json(force=True)
    ticket  = int(data.get('ticket', 0))
    outcome = data.get('outcome', 'SL')

    for symbol, symbol_setups in store.items():
        for s in symbol_setups:
            if s['ticket'] == ticket:
                s['status'] = f'closed_{outcome.lower()}'
                push_csv_to_github(symbol)
                print(f"[CLOSE] ticket={ticket} outcome={outcome}")
                return jsonify({'status': 'ok', 'ticket': ticket, 'outcome': outcome})

    return jsonify({'status': 'not_found', 'ticket': ticket}), 404


@app.route('/setups/<symbol>', methods=['GET'])
def get_setups(symbol):
    symbol = symbol.upper()
    setups = store.get(symbol, [])
    return jsonify({'symbol': symbol, 'count': len(setups), 'setups': setups})


@app.route('/all', methods=['GET'])
def get_all():
    if not check_auth():
        return jsonify({'error': 'Unauthorised'}), 401
    return jsonify({s: store[s] for s in store})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
