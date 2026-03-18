#!/usr/bin/env python3
"""
Order Block Setup API — v3 (JSON Persistence)
===============================================
Deployed to Render.com (free tier).
Receives setup data from MT5 scripts, serves it to the web dashboard.

PERSISTENCE:
  All trades stored in a single JSON file: data/trades.json in GitHub.
  On cold start the API reads that one file to restore state.
  On every write event (new setup or close) the file is pushed back.
  Drawings remain as per-symbol JSON files in drawings/ — unchanged.

  No encoding maps, no CSV column hacks, no schema to maintain.
  New strategies and timeframes just work — no code changes needed.

ENDPOINTS:
  POST /setup                   — receive a new setup from MT5
  POST /close                   — mark a setup as closed (TP or SL hit)
  GET  /setups/<SYM>            — all setups for a symbol
  GET  /all                     — all stored setups (requires API key)
  GET  /history                 — all trades with stats for history dashboard
  GET  /health                  — health check
  POST /drawings/<SYM>          — save a drawing
  GET  /drawings/<SYM>          — get all drawings for a symbol
  DELETE /drawings/<SYM>/<ID>   — delete a drawing
  POST /drawings/<SYM>/clear    — clear all drawings for a symbol
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timezone
from collections import defaultdict
import os, base64, json
import urllib.request, urllib.error

app = Flask(__name__)
CORS(app)


MAX_PER_SYMBOL = 50          # max trades kept per symbol in memory
store    = defaultdict(list) # symbol -> [setup_dict, ...]  (newest first)
drawings = defaultdict(list) # symbol -> [drawing_dict, ...]

API_KEY       = os.environ.get('API_KEY', 'changeme')
GITHUB_TOKEN  = os.environ.get('GITHUB_TOKEN', '')
GITHUB_REPO   = os.environ.get('GITHUB_REPO', 'waringd/seed_waringd_obsetupapi')
GITHUB_BRANCH = 'main'
TRADES_FILE   = 'data/trades.json'


def check_auth():
    return request.headers.get('X-API-Key') == API_KEY


# ── GitHub helpers ───────────────────────────────────────────────

def github_get_file(path):
    """Read a file from the GitHub repo. Returns content string or None."""
    if not GITHUB_TOKEN:
        return None
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}?ref={GITHUB_BRANCH}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req) as resp:
            data    = json.loads(resp.read())
            content = base64.b64decode(data['content']).decode('utf-8')
            return content
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"[GITHUB] Read error {e.code} for {path}")
        return None
    except Exception as e:
        print(f"[GITHUB] Read exception for {path}: {e}")
        return None


def github_put(path, content_str, commit_msg):
    """Create or update a file in the GitHub repo."""
    if not GITHUB_TOKEN:
        print("[GITHUB] No token, skipping push")
        return

    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Content-Type":  "application/json",
        "Accept":        "application/vnd.github.v3+json",
    }

    # Get current SHA so we can update rather than create
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


# ── Persistence: single JSON file ────────────────────────────────

def restore_from_github():
    """Read data/trades.json from GitHub and rebuild the in-memory store."""
    print("[RESTORE] Loading trades from GitHub...")
    content = github_get_file(TRADES_FILE)
    if not content:
        print("[RESTORE] No trades.json found — starting fresh")
        return

    try:
        trades = json.loads(content)
        for trade in trades:
            symbol = trade.get('symbol', '').upper()
            if symbol:
                store[symbol].append(trade)

        # Each symbol list should be newest-first
        for symbol in store:
            store[symbol].sort(key=lambda t: t.get('time_unix', 0), reverse=True)
            store[symbol] = store[symbol][:MAX_PER_SYMBOL]

        total = sum(len(v) for v in store.values())
        print(f"[RESTORE] Done — {total} trades across {len(store)} pairs")
    except Exception as e:
        print(f"[RESTORE] Failed to parse trades.json: {e}")


def push_trades_to_github():
    """Push all trades as a single flat JSON list to GitHub.
    Candle data is stripped before persisting to avoid bloating the file."""
    all_trades = []
    for symbol_trades in store.values():
        all_trades.extend(symbol_trades)

    # Save oldest-first so the file is human-readable chronologically
    all_trades.sort(key=lambda t: t.get('time_unix', 0))
    content = json.dumps(all_trades, indent=2)
    github_put(TRADES_FILE, content, "update trades")


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
    if not check_auth():
        return jsonify({'error': 'Unauthorised'}), 401

    data     = request.get_json(force=True)
    required = ['symbol', 'direction', 'entry', 'sl', 'tp', 'ob_high', 'ob_low']
    for field in required:
        if field not in data:
            return jsonify({'error': f'Missing field: {field}'}), 400

    symbol = data['symbol'].upper()
    now_ts = datetime.now(timezone.utc)

    setup = {
        'symbol':     symbol,
        'direction':  data['direction'],
        'timeframe':  data.get('timeframe', 'H2'),
        'entry':      float(data['entry']),
        'sl':         float(data['sl']),
        'tp':         float(data['tp']),
        'ob_high':    float(data['ob_high']),
        'ob_low':     float(data['ob_low']),
        'rr':         round(float(data.get('rr', 0)), 2),
        'ticket':     int(data.get('ticket', 0)),
        'source':     data.get('source', 'unknown'),
        'status':     'active',
        'time':       now_ts.strftime('%Y-%m-%d %H:%M UTC'),
        'time_unix':  int(now_ts.timestamp()),
        'close_time': '',
    }

    # Candle data: keep in memory for dashboard charts but exclude from
    # GitHub persistence to avoid bloating trades.json
    candles = data.get('candles')
    if candles and isinstance(candles, list):
        setup['candles'] = candles

    store[symbol].insert(0, setup)
    store[symbol] = store[symbol][:MAX_PER_SYMBOL]
    push_trades_to_github()

    print(f"[SETUP] {symbol} {setup['direction']} | entry={setup['entry']} | "
          f"ticket={setup['ticket']} | source={setup['source']}")
    return jsonify({'status': 'ok', 'symbol': symbol, 'total': len(store[symbol])})


@app.route('/close', methods=['POST'])
def close_setup():
    if not check_auth():
        return jsonify({'error': 'Unauthorised'}), 401

    data    = request.get_json(force=True)
    ticket  = int(data.get('ticket', 0))
    outcome = data.get('outcome', 'SL')
    now_ts  = datetime.now(timezone.utc)

    for symbol_setups in store.values():
        for s in symbol_setups:
            if s['ticket'] == ticket:
                s['status']     = f'closed_{outcome.lower()}'
                s['close_time'] = now_ts.strftime('%Y-%m-%d %H:%M UTC')
                push_trades_to_github()
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


@app.route('/history', methods=['GET'])
def get_history():
    """
    Returns all trades (active + closed) as a flat list for the history dashboard.
    Optional query params: source, timeframe, status, symbol
    """
    if not check_auth():
        return jsonify({'error': 'Unauthorised'}), 401

    all_trades = []
    for symbol_trades in store.values():
        all_trades.extend(symbol_trades)

    # Optional filters
    source = request.args.get('source')
    if source:
        all_trades = [t for t in all_trades if t['source'] == source]

    timeframe = request.args.get('timeframe')
    if timeframe:
        all_trades = [t for t in all_trades if t['timeframe'] == timeframe.upper()]

    status = request.args.get('status')
    if status:
        if status == 'active':
            all_trades = [t for t in all_trades if t['status'] == 'active']
        elif status == 'closed':
            all_trades = [t for t in all_trades if t['status'].startswith('closed')]

    symbol_filter = request.args.get('symbol')
    if symbol_filter:
        all_trades = [t for t in all_trades if t['symbol'] == symbol_filter.upper()]

    # Sort newest first
    all_trades.sort(key=lambda t: t.get('time_unix', 0), reverse=True)

    # Summary stats
    closed = [t for t in all_trades if t['status'].startswith('closed')]
    wins   = [t for t in closed if t['status'] == 'closed_tp']
    losses = [t for t in closed if t['status'] == 'closed_sl']

    stats = {
        'total_trades': len(all_trades),
        'active':       len([t for t in all_trades if t['status'] == 'active']),
        'closed':       len(closed),
        'wins':         len(wins),
        'losses':       len(losses),
        'win_rate':     round(len(wins) / len(closed) * 100, 1) if closed else 0,
        'avg_rr':       round(sum(t['rr'] for t in all_trades) / len(all_trades), 2) if all_trades else 0,
    }

    return jsonify({'trades': all_trades, 'stats': stats})


@app.route('/backfill-candles', methods=['POST'])
def backfill_candles():
    """Patch candle data onto an existing setup by ticket number.
    Used by the one-off backfill script — does not create new setups."""
    if not check_auth():
        return jsonify({'error': 'Unauthorised'}), 401

    data    = request.get_json(force=True)
    ticket  = int(data.get('ticket', 0))
    candles = data.get('candles')

    if not ticket or not candles:
        return jsonify({'error': 'ticket and candles required'}), 400

    for symbol_setups in store.values():
        for s in symbol_setups:
            if s['ticket'] == ticket:
                s['candles'] = candles
                push_trades_to_github()
                print(f"[BACKFILL] ticket={ticket} — {len(candles)} candles added")
                return jsonify({'status': 'ok', 'ticket': ticket})

    return jsonify({'status': 'not_found', 'ticket': ticket}), 404


# ── Drawings ─────────────────────────────────────────────────────

def push_drawings_to_github(symbol):
    """Persist drawings for a symbol as JSON to GitHub."""
    d       = drawings.get(symbol, [])
    content = json.dumps(d, indent=2)
    github_put(f"drawings/{symbol}.json", content, f"update {symbol} drawings")


def restore_drawings_from_github():
    """Load all drawing files from GitHub on startup."""
    print("[RESTORE] Loading drawings from GitHub...")
    if not GITHUB_TOKEN:
        print("[RESTORE] No token, skipping drawings")
        return

    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/drawings?ref={GITHUB_BRANCH}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req) as resp:
            files = json.loads(resp.read())
    except Exception as e:
        print(f"[RESTORE] No drawings/ folder yet: {e}")
        return

    total = 0
    for f in files:
        if not f['name'].endswith('.json'):
            continue
        symbol  = f['name'].replace('.json', '')
        content = github_get_file(f"drawings/{f['name']}")
        if content:
            try:
                drawing_list     = json.loads(content)
                drawings[symbol] = drawing_list
                total           += len(drawing_list)
                print(f"[RESTORE] {symbol}: {len(drawing_list)} drawings")
            except Exception as e:
                print(f"[RESTORE] {symbol} drawings failed: {e}")

    print(f"[RESTORE] Drawings done — {total} across {len(drawings)} symbols")


@app.route('/drawings/<symbol>', methods=['GET'])
def get_drawings(symbol):
    if not check_auth():
        return jsonify({'error': 'Unauthorised'}), 401
    symbol = symbol.upper()
    return jsonify({'symbol': symbol, 'drawings': drawings.get(symbol, [])})


@app.route('/drawings/<symbol>', methods=['POST'])
def save_drawing(symbol):
    if not check_auth():
        return jsonify({'error': 'Unauthorised'}), 401

    symbol = symbol.upper()
    data   = request.get_json(force=True)

    drawing = {
        'id':        data.get('id', ''),
        'type':      data.get('type', 'hline'),
        'color':     data.get('color', '#ffffff'),
        'lineWidth': data.get('lineWidth', 1),
        'points':    data.get('points', []),
        'created':   datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
    }

    drawings[symbol].append(drawing)
    push_drawings_to_github(symbol)
    print(f"[DRAWING] Added {drawing['type']} on {symbol}")
    return jsonify({'status': 'ok', 'id': drawing['id']})


@app.route('/drawings/<symbol>/<drawing_id>', methods=['DELETE'])
def delete_drawing(symbol, drawing_id):
    if not check_auth():
        return jsonify({'error': 'Unauthorised'}), 401

    symbol = symbol.upper()
    before = len(drawings.get(symbol, []))
    drawings[symbol] = [d for d in drawings.get(symbol, []) if d.get('id') != drawing_id]
    after  = len(drawings[symbol])

    if before != after:
        push_drawings_to_github(symbol)
        print(f"[DRAWING] Deleted {drawing_id} from {symbol}")
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'not_found'}), 404


@app.route('/drawings/<symbol>/clear', methods=['POST'])
def clear_drawings(symbol):
    if not check_auth():
        return jsonify({'error': 'Unauthorised'}), 401

    symbol           = symbol.upper()
    drawings[symbol] = []
    push_drawings_to_github(symbol)
    print(f"[DRAWING] Cleared all drawings for {symbol}")
    return jsonify({'status': 'ok'})


# ── Startup ──────────────────────────────────────────────────────

with app.app_context():
    restore_from_github()
    restore_drawings_from_github()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
