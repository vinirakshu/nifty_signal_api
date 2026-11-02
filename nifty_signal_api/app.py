"""
Nifty Signal API - REST service on top of the Fyers API v3.

Endpoints
  GET /                          -> web page (signal dashboard)
  GET /health
  GET /auth/login-url            -> Fyers login link (do this once every trading day)
  GET /auth/callback             -> Fyers redirects here; saves today's access token
  GET /api/nifty/levels          -> support / resistance / pivots / OI walls
  GET /api/nifty/signal          -> levels + bias + entry / stop-loss / targets + option instrument
        ?moneyness=ATM|ITM1|ITM2|OTM1|OTM2   (default ATM)
        ?expiry=<epoch from /api/nifty/expiries>   (default nearest expiry)
  GET /api/nifty/option-chain    -> full Nifty option chain (?strikecount=10&expiry=<epoch>)
  GET /api/nifty/expiries        -> available expiries
  GET /api/strategies            -> available strategies and their rules

Paper trading (virtual money, nothing is sent to the exchange)
  GET  /api/paper/summary                 -> account, open positions, history, stats by strategy
  POST /api/paper/trades                  -> {"strategy","moneyness","expiry","lots","order_type","exit_at"}
  POST /api/paper/trades/<id>/close       -> close at live price (or cancel a pending order)
  POST /api/paper/reset                   -> {"capital": 1000000000}

Live market board
  GET  /api/market/nifty50          -> 50 stocks: LTP, change, weight, index points, VWAP, buy/sell side, movers
  GET  /api/market/options-activity -> most active Nifty options, OI change, buildup, aggressor
  GET  /api/market/depth?symbol=    -> order book (5 levels, total buy / sell qty) for a stock or option

Auto paper trading (all strategies, whole session)
  GET  /api/auto/status     -> engine state, per-strategy signal / position / today's P&L
  POST /api/auto/start      -> start the engine (also starts with the server if AUTO_TRADE_ENABLED=1)
  POST /api/auto/stop       -> stop new entries
  GET  /api/auto/log        -> event log (?after=<id>&limit=300&day=YYYY-MM-DD)

This service never places real orders.
"""
import datetime as dt
import logging
import os
from functools import wraps
from urllib.parse import urlencode

from flask import Flask, jsonify, redirect, request, send_from_directory

import threading
import time

import analysis
import config
import strategies
from fyers_client import IST, FyersClient, FyersError, today_ist
import market
from autotrader import AutoTrader, _hm
from market_feed import MarketFeed
from paper import PaperBook, PaperError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("nifty_signal_api")

DISCLAIMER = ("Rule-based technical levels for information only. Not investment advice and not a "
              "prediction. Options can lose value quickly; trade at your own risk.")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
app.json.sort_keys = False
client = FyersClient()
book = PaperBook(
    config.PAPER_DB, start_capital=config.PAPER_START_CAPITAL,
    brokerage=config.PAPER_BROKERAGE_PER_ORDER, stt_sell_pct=config.PAPER_STT_SELL_PCT,
    exchange_pct=config.PAPER_EXCHANGE_PCT, stamp_buy_pct=config.PAPER_STAMP_BUY_PCT,
    gst_pct=config.PAPER_GST_PCT, square_off=config.PAPER_SQUARE_OFF,
    ignore_market_hours=config.PAPER_IGNORE_MARKET_HOURS, log_dir=config.AUTO_LOG_DIR,
)


CONSTITUENTS = market.load_constituents()
feed = MarketFeed(
    lambda: client,
    [config.NIFTY_INDEX_SYMBOL, config.INDIA_VIX_SYMBOL] + [s["symbol"] for s in CONSTITUENTS["stocks"]],
    client_id=config.FYERS_CLIENT_ID, log_path=config.FYERS_LOG_PATH,
    use_websocket=config.FEED_WEBSOCKET, rest_seconds=config.FEED_REST_SECONDS,
)


def settings():
    return {
        "cluster_pts": config.LEVEL_CLUSTER_POINTS,
        "bias_threshold": config.BIAS_THRESHOLD,
        "pcr_bull": config.PCR_BULLISH,
        "pcr_bear": config.PCR_BEARISH,
        "min_rr": config.MIN_RISK_REWARD,
        "lot_size": config.NIFTY_LOT_SIZE,
    }


def now_ist():
    return dt.datetime.now(IST).isoformat(timespec="seconds")


def error(message, status, **extra):
    body = {"success": False, "error": message, "timestamp": now_ist()}
    body.update(extra)
    return jsonify(body), status


def api_route(fn):
    """API-key check + uniform Fyers error handling."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if config.API_KEY and request.headers.get("X-API-Key") != config.API_KEY:
            return error("Invalid or missing X-API-Key", 401)
        try:
            return fn(*args, **kwargs)
        except FyersError as e:
            if e.auth:
                return error(str(e), 401, login_url="/auth/login-url", fyers_code=e.code)
            logger.warning("Fyers error: %s (code %s)", e, e.code)
            return error(str(e), 502, fyers_code=e.code)
        except (ValueError, PaperError) as e:
            return error(str(e), 400)
        except Exception as e:  # noqa: BLE001
            logger.exception("Unhandled error")
            return error(f"Internal error: {e}", 500)
    return wrapper


def _expiry_arg():
    exp = request.args.get("expiry", "").strip()
    if exp and not exp.isdigit():
        raise ValueError("expiry must be an epoch value from /api/nifty/expiries")
    return exp


# ------------------------------------------------------------------ web page
@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


# ------------------------------------------------------------------ health & auth
@app.get("/health")
def health():
    cid = config.FYERS_CLIENT_ID
    return jsonify({"status": "ok", "fyers_configured": bool(cid and config.FYERS_SECRET_KEY),
                    "fyers_app_id": (cid[:3] + "…" + cid[-4:]) if len(cid) > 7 else (cid or None),
                    "redirect_uri": config.FYERS_REDIRECT_URI, "env_file": config.ENV_FILE,
                    "fyers_logged_in": client.is_logged_in(), "timestamp": now_ist()})


@app.get("/auth/login-url")
def login_url():
    try:
        url = client.login_url()
    except FyersError as e:
        return error(str(e), 500)
    return jsonify({"success": True, "login_url": url,
                    "message": "Open this link, log in to Fyers; you will be redirected to /auth/callback."})


@app.get("/auth/callback")
def auth_callback():
    """Fyers redirects the browser here after login; send the user back to the web page."""
    auth_code = request.args.get("auth_code")
    if not auth_code:
        msg = request.args.get("message") or f"Missing auth_code in redirect: {dict(request.args)}"
        return redirect("/?" + urlencode({"login": "failed", "msg": msg}))
    try:
        client.exchange_auth_code(auth_code, request.args.get("state"))
    except FyersError as e:
        return redirect("/?" + urlencode({"login": "failed", "msg": str(e)}))
    logger.info("Fyers login successful for %s", today_ist().isoformat())
    return redirect("/?login=success")


# ------------------------------------------------------------------ data
def _load(expiry=""):
    sym = config.NIFTY_INDEX_SYMBOL
    quote = client.quote(sym)
    daily = client.daily_candles(sym)
    intraday = client.intraday_candles(sym)
    chain = client.option_chain(sym, expiry=expiry)
    return quote, daily, intraday, chain


def build_report(strategy="confluence", moneyness="ATM", expiry=""):
    moneyness = (moneyness or "ATM").upper()
    if moneyness not in analysis.MONEYNESS_SHIFT:
        raise ValueError(f"moneyness must be one of {list(analysis.MONEYNESS_SHIFT)}")
    if strategy not in strategies.STRATEGIES:
        raise ValueError(f"strategy must be one of {list(strategies.STRATEGIES)}")
    expiry = str(expiry or "").strip()
    if expiry and not expiry.isdigit():
        raise ValueError("expiry must be an epoch value from /api/nifty/expiries")
    quote, daily, intraday, chain = _load(expiry)
    return analysis.analyse(quote, daily, intraday, chain, today_ist(), settings(),
                            moneyness=moneyness, expiry_ts=expiry or None,
                            strategy=strategy, now=dt.datetime.now(IST))


@app.get("/api/nifty/signal")
@api_route
def nifty_signal():
    report = build_report(request.args.get("strategy", "confluence"),
                          request.args.get("moneyness", "ATM"), _expiry_arg())
    return jsonify({"success": True, "timestamp": now_ist(), **report, "disclaimer": DISCLAIMER})


@app.get("/api/strategies")
@api_route
def list_strategies():
    return jsonify({"success": True, "strategies": strategies.strategy_list()})


# ------------------------------------------------------------------ paper trading
def run_paper_monitor(now=None):
    """Fetch live prices for active paper trades and apply fills / exits."""
    if not book.has_active():
        return []
    sym = config.NIFTY_INDEX_SYMBOL
    symbols = book.active_symbols()
    feed.ensure(symbols)
    # live feed first (updated by the WebSocket); REST quotes for anything missing or stale
    fresh = {s: feed.get(s, max_age=5) for s in [sym] + symbols}
    prices = {s: {"ltp": t.get("ltp"), "bid": t.get("bid_price"), "ask": t.get("ask_price")}
              for s, t in fresh.items() if t}
    missing = [s for s, t in fresh.items() if not t]
    if missing:
        quotes = client.quotes_many(missing)
        prices.update({k: {"ltp": v.get("lp"), "bid": v.get("bid"), "ask": v.get("ask")}
                       for k, v in quotes.items()})
    idx = (prices.get(sym) or {}).get("ltp")
    events = book.monitor(prices, idx, now=now or dt.datetime.now(IST))
    for e in events:
        logger.info("Paper trade: %s", e)
    return events


def _monitor_loop():
    while True:
        # every 2 s while the WebSocket feed is live (no REST cost), otherwise every PAPER_MONITOR_SECONDS
        time.sleep(2 if feed.ws_live() else config.PAPER_MONITOR_SECONDS)
        try:
            if client.is_logged_in():
                run_paper_monitor()
        except Exception as e:  # noqa: BLE001 - keep the loop alive
            logger.warning("Paper monitor: %s", e)


def start_monitor():
    t = threading.Thread(target=_monitor_loop, name="paper-monitor", daemon=True)
    t.start()
    return t


# ------------------------------------------------------------------ auto trading
def _auto_cfg():
    for sid in config.AUTO_STRATEGIES:
        if sid not in strategies.STRATEGIES:
            raise RuntimeError(f"AUTO_STRATEGIES has unknown strategy '{sid}'")
    return {
        "strategies": config.AUTO_STRATEGIES,
        "names": {k: v["name"] for k, v in strategies.STRATEGIES.items()},
        "cycle_seconds": config.AUTO_CYCLE_SECONDS,
        "entry_start": _hm(config.AUTO_ENTRY_START), "entry_end": _hm(config.AUTO_ENTRY_END),
        "square_off": config.PAPER_SQUARE_OFF,
        "risk_pct": config.AUTO_RISK_PER_TRADE_PCT, "max_capital_pct": config.AUTO_MAX_CAPITAL_PER_TRADE_PCT,
        "max_oi_pct": config.AUTO_MAX_OI_PCT, "max_lots": config.AUTO_MAX_LOTS,
        "max_trades": config.AUTO_MAX_TRADES_PER_STRATEGY, "cooldown_minutes": config.AUTO_COOLDOWN_MINUTES,
        "exit_at": config.AUTO_EXIT_AT, "moneyness": config.AUTO_MONEYNESS,
        "slippage_pct": config.AUTO_SLIPPAGE_PCT, "ignore_market_hours": config.PAPER_IGNORE_MARKET_HOURS,
    }


engine = AutoTrader(
    book,
    get_report=lambda sid: build_report(sid, config.AUTO_MONEYNESS, ""),
    run_monitor=lambda now=None: run_paper_monitor(now),
    is_logged_in=lambda: client.is_logged_in(),
    cfg=_auto_cfg(),
)


@app.get("/api/auto/status")
@api_route
def auto_status():
    return jsonify({"success": True, "timestamp": now_ist(), **engine.status()})


@app.post("/api/auto/start")
@api_route
def auto_start():
    engine.start()
    return jsonify({"success": True, **engine.status()})


@app.post("/api/auto/stop")
@api_route
def auto_stop():
    engine.stop()
    return jsonify({"success": True, **engine.status()})


@app.get("/api/auto/log")
@api_route
def auto_log():
    after = request.args.get("after", "0")
    limit = request.args.get("limit", "300")
    if not after.isdigit() or not limit.isdigit():
        raise ValueError("after and limit must be numbers")
    day = request.args.get("day") or None
    return jsonify({"success": True, "events": book.events(int(after), min(int(limit), 1000), day)})


# ------------------------------------------------------------------ market board
@app.get("/api/market/nifty50")
@api_route
def market_nifty50():
    if client.is_logged_in() and not feed.ticks and time.time() - feed.last_rest > config.FEED_REST_SECONDS:
        feed.rest_poll()          # first load before the feed loop has data
    ticks = feed.snapshot()
    idx = ticks.get(config.NIFTY_INDEX_SYMBOL) or {}
    vix = ticks.get(config.INDIA_VIX_SYMBOL) or {}
    board = market.nifty50_board(CONSTITUENTS, ticks, idx)
    return jsonify({"success": True, "timestamp": now_ist(), "feed": feed.status(),
                    "nifty": {"ltp": idx.get("ltp"), "change": idx.get("ch"), "change_pct": idx.get("chp"),
                              "prev_close": idx.get("prev_close_price"), "high": idx.get("high_price"),
                              "low": idx.get("low_price")},
                    "india_vix": {"ltp": vix.get("ltp"), "change_pct": vix.get("chp")},
                    "logged_in": client.is_logged_in(), **board,
                    "note": "Buy/sell side combines today's change, price vs VWAP and pending buy vs sell "
                            "quantity in the order book. Pending orders can be cancelled any time."})


@app.get("/api/market/options-activity")
@api_route
def market_options_activity():
    expiry = _expiry_arg()
    chain = client.option_chain(config.NIFTY_INDEX_SYMBOL, expiry=expiry)
    act = market.options_activity(chain, top_n=int(request.args.get("top", "10")))
    return jsonify({"success": True, "timestamp": now_ist(), "expiry": analysis.expiry_info(chain, expiry or None),
                    **act})


@app.get("/api/market/depth")
@api_route
def market_depth():
    import re
    sym = request.args.get("symbol", "").strip()
    if not re.fullmatch(r"(NSE|BSE):[A-Za-z0-9&\-_]+", sym):
        raise ValueError("symbol must look like NSE:SBIN-EQ or NSE:NIFTY26O2825150CE")
    d = market.parse_depth(sym, client.depth(sym))
    name = next((c["name"] for c in CONSTITUENTS["stocks"] if c["symbol"] == sym), None)
    return jsonify({"success": True, "timestamp": now_ist(), "name": name, **d})


@app.get("/api/paper/summary")
@api_route
def paper_summary():
    events = []
    if book.has_active() and client.is_logged_in():
        events = run_paper_monitor()
    return jsonify({"success": True, "timestamp": now_ist(), "events": events, **book.summary(),
                    "note": "Virtual money. Fills use live bid/ask; charges are approximate."})


@app.post("/api/paper/trades")
@api_route
def paper_place():
    body = request.get_json(silent=True) or {}
    report = build_report(body.get("strategy", "confluence"), body.get("moneyness", "ATM"),
                          body.get("expiry", ""))
    trade = book.place(report, body.get("lots", 1), body.get("order_type", "market"),
                       body.get("exit_at", "T1"), now=dt.datetime.now(IST), max_lots=config.AUTO_MAX_LOTS)
    return jsonify({"success": True, "trade": trade}), 201


@app.post("/api/paper/trades/<int:trade_id>/close")
@api_route
def paper_close(trade_id):
    t = book.get(trade_id)
    price = index = None
    if t["status"] == "OPEN":
        sym = config.NIFTY_INDEX_SYMBOL
        q = client.quotes_many([sym, t["symbol"]])
        oq = q.get(t["symbol"]) or {}
        price = oq.get("bid") or oq.get("lp")
        index = (q.get(sym) or {}).get("lp")
    return jsonify({"success": True, "trade": book.close(trade_id, price, index, now=dt.datetime.now(IST))})


@app.post("/api/paper/reset")
@api_route
def paper_reset():
    body = request.get_json(silent=True) or {}
    book.reset(body.get("capital"))
    return jsonify({"success": True, **book.summary()})


@app.get("/api/nifty/levels")
@api_route
def nifty_levels():
    r = build_report(expiry=_expiry_arg())
    return jsonify({"success": True, "timestamp": now_ist(), "underlying": r["underlying"],
                    "levels": r["levels"], "pivots": r["pivots"], "previous_day": r["previous_day"],
                    "option_chain_summary": r["option_chain_summary"], "disclaimer": DISCLAIMER})


@app.get("/api/nifty/option-chain")
@api_route
def nifty_option_chain():
    strikecount = request.args.get("strikecount", str(config.OPTION_STRIKE_COUNT))
    if not strikecount.isdigit() or not 1 <= int(strikecount) <= 50:
        raise ValueError("strikecount must be between 1 and 50")
    expiry = _expiry_arg()
    chain = client.option_chain(config.NIFTY_INDEX_SYMBOL, strikecount=int(strikecount), expiry=expiry)
    underlying, options, step = analysis.split_chain(chain)
    exp = analysis.expiry_info(chain, expiry or None)

    by_strike = {}
    for o in options:
        row = by_strike.setdefault(o["strike_price"], {"strike": o["strike_price"], "CE": None, "PE": None})
        row[o["option_type"]] = {k: o.get(k) for k in (
            "symbol", "fyToken", "ltp", "ltpch", "ltpchp", "bid", "ask", "volume",
            "oi", "oich", "oichp", "prev_oi", "greeks")}
    return jsonify({
        "success": True, "timestamp": now_ist(),
        "underlying": {"symbol": config.NIFTY_INDEX_SYMBOL, "ltp": (underlying or {}).get("ltp")},
        "expiry": exp, "strike_step": step, "lot_size": config.NIFTY_LOT_SIZE,
        "total_call_oi": chain.get("callOi"), "total_put_oi": chain.get("putOi"),
        "india_vix": (chain.get("indiavixData") or {}).get("ltp"),
        "strikes": [by_strike[k] for k in sorted(by_strike)],
    })


@app.get("/api/nifty/expiries")
@api_route
def nifty_expiries():
    chain = client.option_chain(config.NIFTY_INDEX_SYMBOL, strikecount=1)
    return jsonify({"success": True, "timestamp": now_ist(), "expiries": chain.get("expiryData", [])})


if __name__ == "__main__":
    if config.PAPER_MONITOR:
        start_monitor()
    if config.AUTO_TRADE_ENABLED:
        engine.start()
    if config.FEED_ENABLED:
        feed.start()
    app.run(host=config.HOST, port=config.PORT, debug=False, use_reloader=False)
