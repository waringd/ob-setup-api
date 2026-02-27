#!/usr/bin/env python3
"""
Order Block Setup API — v2 (Persistent)
========================================
Deployed to Render.com (free tier).
Receives setup data from MT5 scripts, serves it to the web dashboard.

PERSISTENCE:
  On every setup/close event, CSV files are pushed to GitHub.
  On cold start, the API reads those CSVs back from GitHub to restore state.
  This means Render free-tier cold restarts don't lose data.

CSV files pushed to GitHub (data/<SYMBOL>_prices.csv, data/<SYMBOL>_meta.csv):
  See push_csv_to_github() for column mappings.

ENDPOINTS:
  POST /setup          — receive a new OB setup from MT5
  POST /close          — mark a setup as closed (TP or SL hit)
  GET  /setups/<SYM>   — all setups for a symbol
  GET  /all            — all stored setups (requires API key)
  GET  /history        — all trades with full detail for dashboard history page
  GET  /health         — health check
"""

from flask import Flask, request, jsonify
from datetime import datetime, timezone
from collections import defaultdict
import os, base64, json, csv, io
import urllib.request, urllib.error

app = Flask(__name__)


@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-API-Key'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response


@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def handle_options(path):
    """Handle CORS preflight requests."""
    return '', 204


MAX_PER_SYMBOL = 50          # raised from 10 — we want history
store = defaultdict(list)    # symbol -> [setup_dict, ...]  (newest first)

API_KEY       = os.environ.get('API_KEY', 'changeme')
GITHUB_TOKEN  = os.environ.get('GITHUB_TOKEN', '')
GITHUB_REPO   = os.environ.get('GITHUB_REPO', 'waringd/seed_waringd_obsetupapi')
GITHUB_BRANCH = 'main'

# Source label map — add new strategies here
SOURCE_LABELS = {
    'v3': 1, 'modelb': 2,
    # future: 'v4': 3, 'scalper': 4, etc.
}
SOURCE_LABELS_REV = {v: k for k, v in SOURCE_LABELS.items()}

TIMEFRAME_MAP = {'H1': 60, 'M15': 15, 'H4': 240, 'M5': 5, 'D1': 1440}
TIMEFRAME_MAP_REV = {v: k for k, v in TIMEFRAME_MAP.items()}

STATUS_MAP     = {'active': 1, 'closed_tp': 2, 'closed_sl': 3}
STATUS_MAP_REV = {v: k for k, v in STATUS_MAP.items()}


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
            data = json.loads(resp.read())
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


def github_list_data_files():
    """List all files in data/ directory of the GitHub repo."""
    if not GITHUB_TOKEN:
        return []
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/data?ref={GITHUB_BRANCH}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req) as resp:
            files = json.loads(resp.read())
            return [f['name'] for f in files if f['type'] == 'file']
    except Exception as e:
        print(f"[GITHUB] List data/ failed: {e}")
        return []


# ── Persistence: restore from GitHub on startup ──────────────────

def restore_from_github():
    """Read all CSV pairs from GitHub and rebuild the in-memory store."""
    print("[RESTORE] Loading setups from GitHub CSVs...")
    files = github_list_data_files()
    
    # Find all symbols that have both _prices.csv and _meta.csv
    symbols = set()
    for f in files:
        if f.endswith('_prices.csv'):
            sym = f.replace('_prices.csv', '')
            if f"{sym}_meta.csv" in files:
                symbols.add(sym)
    
    total = 0
    for symbol in sorted(symbols):
        prices_csv = github_get_file(f"data/{symbol}_prices.csv")
        meta_csv   = github_get_file(f"data/{symbol}_meta.csv")
        if not prices_csv or not meta_csv:
            continue
        
        try:
            prices_rows = list(csv.DictReader(io.StringIO(prices_csv)))
            meta_rows   = list(csv.DictReader(io.StringIO(meta_csv)))
            
            if len(prices_rows) != len(meta_rows):
                print(f"[RESTORE] {symbol}: row count mismatch, skipping")
                continue
            
            setups = []
            for p, m in zip(prices_rows, meta_rows):
                src_val = int(float(m['close']))
                tf_val  = int(float(m['volume']))
                status_val = int(float(m['high']))
                dir_val = int(float(m['open']))
                
                setup = {
                    'symbol':     symbol,
                    'direction':  'LONG' if dir_val == 1 else 'SHORT',
                    'timeframe':  TIMEFRAME_MAP_REV.get(tf_val, f'{tf_val}m'),
                    'entry':      float(p['open']),
                    'sl':         float(p['close']),
                    'tp':         float(p['volume']),
                    'ob_high':    float(p['high']),
                    'ob_low':     float(p['low']),
                    'rr':         float(m['low']),
                    'ticket':     0,
                    'source':     SOURCE_LABELS_REV.get(src_val, f'source_{src_val}'),
                    'status':     STATUS_MAP_REV.get(status_val, 'active'),
                    'time_unix':  int(float(p['time'])),
                    'time':       datetime.fromtimestamp(
                                      int(float(p['time'])), tz=timezone.utc
                                  ).strftime('%Y-%m-%d %H:%M UTC'),
                    'close_time': '',
                }
                setups.append(setup)
            
            # CSVs are oldest-first, store is newest-first
            setups.reverse()
            store[symbol] = setups
            total += len(setups)
            print(f"[RESTORE] {symbol}: {len(setups)} setups")
        except Exception as e:
            print(f"[RESTORE] {symbol} failed: {e}")
    
    print(f"[RESTORE] Done — {total} setups across {len(store)} pairs")


# ── CSV push ─────────────────────────────────────────────────────

def push_csv_to_github(symbol):
    """Push price + meta CSVs for this symbol."""
    setups = store.get(symbol, [])
    if not setups:
        return

    # store is newest-first, CSV needs oldest-first
    ordered = list(reversed(setups))

    price_lines = ["time,open,high,low,close,volume"]
    meta_lines  = ["time,open,high,low,close,volume"]

    for s in ordered:
        t = s['time_unix']
        dir_val    = 1 if s['direction'] == 'LONG' else -1
        status_val = STATUS_MAP.get(s['status'], 1)
        src_val    = SOURCE_LABELS.get(s['source'], 0)
        tf_val     = TIMEFRAME_MAP.get(s['timeframe'], 60)

        price_lines.append(f"{t},{s['entry']},{s['ob_high']},{s['ob_low']},{s['sl']},{s['tp']}")
        meta_lines.append(f"{t},{dir_val},{status_val},{s['rr']},{src_val},{tf_val}")

    github_put(f"data/{symbol}_prices.csv", "\n".join(price_lines), f"update {symbol} prices")
    github_put(f"data/{symbol}_meta.csv",   "\n".join(meta_lines),  f"update {symbol} meta")


# ── Routes ───────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    total = sum(len(v) for v in store.values())
    return jsonify({
        'status': 'ok',
        'time': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
        'pairs': len(store),
        'setups': total,
    })


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
    now_ts = datetime.now(timezone.utc)
    
    setup = {
        'symbol':     symbol,
        'direction':  data['direction'],
        'timeframe':  data.get('timeframe', 'H1'),
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

    store[symbol].insert(0, setup)
    store[symbol] = store[symbol][:MAX_PER_SYMBOL]
    push_csv_to_github(symbol)

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

    for symbol, symbol_setups in store.items():
        for s in symbol_setups:
            if s['ticket'] == ticket:
                s['status']     = f'closed_{outcome.lower()}'
                s['close_time'] = now_ts.strftime('%Y-%m-%d %H:%M UTC')
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


@app.route('/history', methods=['GET'])
def get_history():
    """
    Returns ALL trades (active + closed) as a flat list for the history dashboard.
    Optional query params: source, timeframe, status, symbol
    """
    if not check_auth():
        return jsonify({'error': 'Unauthorised'}), 401

    # Collect all setups into flat list
    all_trades = []
    for symbol, setups in store.items():
        for s in setups:
            all_trades.append(s)

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

    # Sort by time descending (newest first)
    all_trades.sort(key=lambda t: t.get('time_unix', 0), reverse=True)

    # Summary stats
    closed = [t for t in all_trades if t['status'].startswith('closed')]
    wins   = [t for t in closed if t['status'] == 'closed_tp']
    losses = [t for t in closed if t['status'] == 'closed_sl']

    stats = {
        'total_trades':  len(all_trades),
        'active':        len([t for t in all_trades if t['status'] == 'active']),
        'closed':        len(closed),
        'wins':          len(wins),
        'losses':        len(losses),
        'win_rate':      round(len(wins) / len(closed) * 100, 1) if closed else 0,
        'avg_rr':        round(sum(t['rr'] for t in all_trades) / len(all_trades), 2) if all_trades else 0,
    }

    return jsonify({'trades': all_trades, 'stats': stats})


# ── Startup ──────────────────────────────────────────────────────

with app.app_context():
    restore_from_github()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
