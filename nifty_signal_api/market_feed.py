"""
Live market feed.

Primary : Fyers WebSocket data socket ("SymbolUpdate"): pushes every change for the 50 stocks,
          Nifty, India VIX and any open paper-trade options. Includes total buy / sell quantity
          and VWAP. Not limited by the REST per-minute limit (free plan: up to 100 symbols).
Fallback: if the socket is not delivering, REST quotes every FEED_REST_SECONDS (2 calls for ~52 symbols).
"""
import logging
import threading
import time

logger = logging.getLogger("market_feed")

TICK_FIELDS = ("ltp", "ch", "chp", "vol_traded_today", "avg_trade_price", "tot_buy_qty", "tot_sell_qty",
               "bid_price", "ask_price", "bid_size", "ask_size", "open_price", "high_price", "low_price",
               "prev_close_price", "last_traded_qty", "last_traded_time", "exch_feed_time")


def rest_to_tick(v):
    """Map a REST quote ('v' block) to the same keys the WebSocket uses."""
    return {"ltp": v.get("lp"), "ch": v.get("ch"), "chp": v.get("chp"), "vol_traded_today": v.get("volume"),
            "avg_trade_price": v.get("atp"), "bid_price": v.get("bid"), "ask_price": v.get("ask"),
            "open_price": v.get("open_price"), "high_price": v.get("high_price"),
            "low_price": v.get("low_price"), "prev_close_price": v.get("prev_close_price"),
            "tot_buy_qty": v.get("tot_buy_qty"), "tot_sell_qty": v.get("tot_sell_qty")}


class MarketFeed:
    def __init__(self, client_fn, base_symbols, client_id="", log_path="", use_websocket=True,
                 rest_seconds=10, stale_seconds=30, socket_factory=None):
        self.client_fn = client_fn            # returns the current FyersClient
        self.base_symbols = list(dict.fromkeys(base_symbols))
        self.extra = set()
        self.client_id = client_id
        self.log_path = log_path
        self.use_websocket = use_websocket
        self.rest_seconds = rest_seconds
        self.stale_seconds = stale_seconds
        self.socket_factory = socket_factory
        self.ticks = {}
        self.lock = threading.Lock()
        self.ws = None
        self.ws_token = None
        self.ws_connected = False
        self.ws_error = None
        self.last_ws_tick = 0.0
        self.last_rest = 0.0
        self.mode = "off"
        self.tick_count = 0
        self._thread = None

    @property
    def client(self):
        return self.client_fn()

    # ------------------------------------------------------------------ public
    def start(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="market-feed", daemon=True)
            self._thread.start()

    def symbols(self):
        return self.base_symbols + sorted(self.extra - set(self.base_symbols))

    def ensure(self, symbols):
        """Add symbols (e.g. options of open paper trades) to the live feed."""
        new = [s for s in symbols if s and s not in self.extra and s not in self.base_symbols]
        if not new:
            return
        self.extra.update(new)
        if self.ws is not None and self.ws_connected:
            try:
                self.ws.subscribe(symbols=new, data_type="SymbolUpdate")
            except Exception as e:  # noqa: BLE001
                logger.warning("subscribe failed: %s", e)

    def on_tick(self, msg):
        if not isinstance(msg, dict) or not msg.get("symbol") or msg.get("ltp") is None:
            return
        tick = {k: msg[k] for k in TICK_FIELDS if k in msg}
        tick["_ts"] = time.time()
        tick["_src"] = "ws"
        with self.lock:
            cur = self.ticks.get(msg["symbol"], {})
            cur.update(tick)
            self.ticks[msg["symbol"]] = cur
            self.tick_count += 1
        self.last_ws_tick = tick["_ts"]

    def snapshot(self):
        with self.lock:
            return {k: dict(v) for k, v in self.ticks.items()}

    def get(self, symbol, max_age=None):
        with self.lock:
            t = self.ticks.get(symbol)
            if not t:
                return None
            if max_age is not None and time.time() - t.get("_ts", 0) > max_age:
                return None
            return dict(t)

    def ws_live(self):
        return self.ws_connected and time.time() - self.last_ws_tick < self.stale_seconds

    def status(self):
        now = time.time()
        newest = max((t.get("_ts", 0) for t in self.ticks.values()), default=0)
        return {
            "mode": "websocket" if self.ws_live() else ("rest" if self.mode == "rest" else self.mode),
            "websocket_connected": self.ws_connected, "websocket_error": self.ws_error,
            "symbols": len(self.symbols()), "with_data": len(self.ticks),
            "last_update_age_s": round(now - newest, 1) if newest else None,
            "ticks_received": self.tick_count,
        }

    # ------------------------------------------------------------------ internals
    def _loop(self):
        while True:
            try:
                self._step()
            except Exception as e:  # noqa: BLE001
                logger.warning("feed loop: %s", e)
            time.sleep(2)

    def _step(self):
        token = self.client.tokens.get() if hasattr(self.client, "tokens") else None
        if not token:
            self.mode = "off"
            return
        if self.use_websocket and token != self.ws_token:
            self._connect(token)
        if self.ws_live():
            self.mode = "websocket"
            return
        if time.time() - self.last_rest >= self.rest_seconds:
            self.rest_poll()

    def rest_poll(self):
        syms = self.symbols()
        got = {}
        for i in range(0, len(syms), 50):
            got.update(self.client.quotes_many(syms[i:i + 50]))
        now = time.time()
        with self.lock:
            for s, v in got.items():
                t = rest_to_tick(v)
                t = {k: val for k, val in t.items() if val is not None}
                t["_ts"], t["_src"] = now, "rest"
                cur = self.ticks.get(s, {})
                cur.update(t)
                self.ticks[s] = cur
        self.last_rest = now
        self.mode = "rest"
        return len(got)

    def _connect(self, token):
        self._close()
        self.ws_token = token
        self.ws_error = None
        try:
            if self.socket_factory:
                self.ws = self.socket_factory(self)
            else:
                from fyers_apiv3.FyersWebsocket import data_ws
                self.ws = data_ws.FyersDataSocket(
                    access_token=f"{self.client_id}:{token}", log_path=self.log_path or "",
                    litemode=False, write_to_file=True, reconnect=True,
                    on_connect=self._on_open, on_message=self.on_tick,
                    on_error=self._on_error, on_close=self._on_close)
            self.ws.connect()
        except Exception as e:  # noqa: BLE001
            self.ws_error = str(e)
            self.ws_connected = False
            logger.warning("websocket connect failed: %s", e)

    def _on_open(self):
        self.ws_connected = True
        try:
            self.ws.subscribe(symbols=self.symbols(), data_type="SymbolUpdate")
            self.ws.keep_running()
        except Exception as e:  # noqa: BLE001
            self.ws_error = str(e)

    def _on_error(self, msg):
        self.ws_error = str(msg)[:300]
        logger.warning("websocket error: %s", msg)

    def _on_close(self, msg):
        self.ws_connected = False

    def _close(self):
        if self.ws is not None:
            try:
                self.ws.close_connection()
            except Exception:  # noqa: BLE001
                pass
        self.ws, self.ws_connected = None, False
