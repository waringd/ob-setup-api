#!/usr/bin/env python3
"""
Order Block Setup API
=====================
Lightweight Flask API deployed to Render.com (free tier).
Receives setup data from v3_live_runner and model_b_live,
serves it to Pine Script via request.json().

DEPLOY TO RENDER:
  1. Create a free account at render.com
  2. New → Web Service → connect your GitHub repo (or paste this file)
  3. Build command:  pip install flask gunicorn
  4. Start command:  gunicorn setup_api:app
  5. Copy the public URL (e.g. https://ob-setups.onrender.com)
  6. Paste that URL into:
       - setup_api_client.py  (API_URL)
       - The Pine Script       (API_URL input)

ENDPOINTS:
  POST /setup          — add a new setup (called by Python scripts)
  POST /close          — mark a setup closed when TP/SL hit
  GET  /setups/<symbol> — Pine Script polls this per bar
  GET  /health         — uptime check
"""

from flask import Flask, request, jsonify
from datetime import datetime, timezone
from collections import defaultdict
import os

app = Flask(__name__)

# ── In-memory store ──────────────────────────────────────────────
# { symbol: [ setup_dict, ... ] }  — newest first, capped at MAX_PER_SYMBOL
MAX_PER_SYMBOL = 10
store = defaultdict(list)

# ── Auth ─────────────────────────────────────────────────────────
# Set API_KEY as an environment variable in Render dashboard.
# All POST requests must include header:  X-API-Key: <your_key>
# GET requests (Pine Script) are public — Pine Script can't send headers easily.
API_KEY = os.environ.get('API_KEY', 'changeme')


def check_auth():
    return request.headers.get('X-API-Key') == API_KEY


# ── Routes ───────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    total = sum(len(v) for v in store.values())
    return jsonify({
        'status': 'ok',
        'time':   datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
        'pairs':  len(store),
        'setups': total,
    })


@app.route('/setup', methods=['POST'])
def add_setup():
    """
    Called by Python script when a limit order is placed.
    Expected JSON body:
    {
        "symbol":    "GBPJPY",
        "direction": "SHORT",         # SHORT or LONG
        "timeframe": "H1",            # H1 or M15
        "entry":     156.234,
        "sl":        156.890,
        "tp":        154.500,
        "ob_high":   156.780,
        "ob_low":    156.120,
        "rr":        2.34,
        "ticket":    123456789,
        "source":    "v3"             # v3 or modelb
    }
    """
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
        'status':    'active',        # active | closed_tp | closed_sl | expired
        'time':      datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
        'time_unix': int(datetime.now(timezone.utc).timestamp()),
    }

    # Prepend (newest first) and cap at MAX_PER_SYMBOL
    store[symbol].insert(0, setup)
    store[symbol] = store[symbol][:MAX_PER_SYMBOL]

    print(f"[SETUP] {symbol} {setup['direction']} | entry={setup['entry']} | "
          f"ticket={setup['ticket']} | source={setup['source']}")
    return jsonify({'status': 'ok', 'symbol': symbol, 'total': len(store[symbol])})


@app.route('/close', methods=['POST'])
def close_setup():
    """
    Called by Python script when TP or SL is hit.
    Expected JSON:
    {
        "ticket": 123456789,
        "outcome": "TP"    # TP or SL
    }
    """
    if not check_auth():
        return jsonify({'error': 'Unauthorised'}), 401

    data    = request.get_json(force=True)
    ticket  = int(data.get('ticket', 0))
    outcome = data.get('outcome', 'SL')

    for symbol_setups in store.values():
        for s in symbol_setups:
            if s['ticket'] == ticket:
                s['status'] = f'closed_{outcome.lower()}'
                print(f"[CLOSE] ticket={ticket} outcome={outcome}")
                return jsonify({'status': 'ok', 'ticket': ticket, 'outcome': outcome})

    return jsonify({'status': 'not_found', 'ticket': ticket}), 404


@app.route('/setups/<symbol>', methods=['GET'])
def get_setups(symbol):
    """
    Polled by Pine Script every bar.
    Returns the last MAX_PER_SYMBOL setups for the symbol.
    Pine Script uses request.json() to call this.
    Symbol is case-insensitive.
    """
    symbol  = symbol.upper()
    setups  = store.get(symbol, [])
    return jsonify({
        'symbol': symbol,
        'count':  len(setups),
        'setups': setups,
    })


@app.route('/all', methods=['GET'])
def get_all():
    """Returns all stored setups — useful for debugging."""
    if not check_auth():
        return jsonify({'error': 'Unauthorised'}), 401
    return jsonify({s: store[s] for s in store})


# ── Entry point ──────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
