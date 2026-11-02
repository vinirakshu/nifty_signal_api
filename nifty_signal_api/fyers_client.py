"""
Fyers API v3 wrapper: login (daily auth code -> access token), token storage,
and cached market-data calls (quotes, candles, option chain).
"""
import datetime as dt
import json
import logging
import os
import secrets
import threading
import time
from zoneinfo import ZoneInfo

from fyers_apiv3 import fyersModel

import config

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger(__name__)

# Fyers error codes that mean the access token is missing / invalid / expired
AUTH_ERROR_CODES = {-8, -15, -16, -17, -300, 401}


class FyersError(Exception):
    def __init__(self, message, code=None, auth=False):
        super().__init__(message)
        self.code = code
        self.auth = auth


def today_ist():
    return dt.datetime.now(IST).date()


# ------------------------------------------------------------------ token store
class TokenStore:
    """Fyers access tokens are valid for one trading day; keep today's on disk."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._token = None
        self._date = None
        self._load()

    def _load(self):
        if config.FYERS_ACCESS_TOKEN:
            self._token, self._date = config.FYERS_ACCESS_TOKEN, today_ist().isoformat()
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            self._token, self._date = data.get("access_token"), data.get("date")
        except (OSError, ValueError):
            pass

    def get(self):
        with self._lock:
            if self._token and self._date == today_ist().isoformat():
                return self._token
            return None

    def save(self, token):
        with self._lock:
            self._token, self._date = token, today_ist().isoformat()
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump({"access_token": token, "date": self._date}, fh)


# ------------------------------------------------------------------ tiny TTL cache
class TTLCache:
    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    def get_or_set(self, key, ttl, fn):
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        value = fn()
        with self._lock:
            self._data[key] = (time.monotonic(), value)
        return value

    def clear(self):
        with self._lock:
            self._data.clear()


# ------------------------------------------------------------------ client
class FyersClient:
    def __init__(self, token_store=None, model_factory=None):
        self.tokens = token_store or TokenStore(config.TOKEN_FILE)
        self.cache = TTLCache()
        self._states = set()
        self._model = None
        self._model_token = None
        self._model_factory = model_factory or self._default_model
        if config.FYERS_LOG_PATH:
            os.makedirs(config.FYERS_LOG_PATH, exist_ok=True)

    # ---------- login
    def _session(self, state=None):
        return fyersModel.SessionModel(
            client_id=config.FYERS_CLIENT_ID,
            secret_key=config.FYERS_SECRET_KEY,
            redirect_uri=config.FYERS_REDIRECT_URI,
            response_type="code",
            grant_type="authorization_code",
            state=state,
        )

    def login_url(self):
        if not config.FYERS_CLIENT_ID or not config.FYERS_SECRET_KEY:
            raise FyersError(
                f"Fyers App ID / Secret ID not set. Run 'python setup_env.py' in the project folder "
                f"(or create {config.ENV_FILE} with FYERS_CLIENT_ID and FYERS_SECRET_KEY), then restart the server.")
        state = secrets.token_urlsafe(16)
        self._states.add(state)
        return self._session(state).generate_authcode()

    def exchange_auth_code(self, auth_code, state=None):
        if self._states and state not in self._states:
            raise FyersError("Invalid or expired login state; start again from /auth/login-url")
        self._states.discard(state)
        session = self._session(state)
        session.set_token(auth_code)
        resp = session.generate_token()
        token = resp.get("access_token") if isinstance(resp, dict) else None
        if not token:
            raise FyersError(f"Token generation failed: {resp}", code=resp.get("code") if isinstance(resp, dict) else None)
        self.tokens.save(token)
        self.cache.clear()
        return token

    def is_logged_in(self):
        return self.tokens.get() is not None

    # ---------- model
    @staticmethod
    def _default_model(token):
        return fyersModel.FyersModel(
            client_id=config.FYERS_CLIENT_ID, token=token, is_async=False,
            log_path=config.FYERS_LOG_PATH,
        )

    def _fyers(self):
        token = self.tokens.get()
        if not token:
            raise FyersError("Not logged in to Fyers today. Open /auth/login-url", auth=True)
        if self._model is None or self._model_token != token:
            self._model = self._model_factory(token)
            self._model_token = token
        return self._model

    @staticmethod
    def _check(resp, what):
        if not isinstance(resp, dict):
            raise FyersError(f"{what}: unexpected response {resp!r}")
        if resp.get("s") == "ok":
            return resp
        code = resp.get("code")
        msg = resp.get("message") or resp.get("msg") or str(resp)
        auth = code in AUTH_ERROR_CODES or "token" in str(msg).lower() or "authenticate" in str(msg).lower()
        raise FyersError(f"{what} failed: {msg}", code=code, auth=auth)

    # ---------- market data
    def quote(self, symbol):
        def fetch():
            resp = self._check(self._fyers().quotes({"symbols": symbol}), "quotes")
            for item in resp.get("d", []):
                if item.get("s") == "ok" or item.get("v"):
                    return item.get("v", {})
            raise FyersError(f"quotes: no data for {symbol}")
        return self.cache.get_or_set(("quote", symbol), config.CACHE_TTL_QUOTE, fetch)

    def quotes_many(self, symbols):
        """Returns {symbol: quote_values} for up to 50 symbols in one call."""
        symbols = sorted(set(symbols))
        if not symbols:
            return {}

        def fetch():
            resp = self._check(self._fyers().quotes({"symbols": ",".join(symbols)}), "quotes")
            out = {}
            for item in resp.get("d", []):
                v = item.get("v") or {}
                name = item.get("n") or v.get("symbol")
                if name and v:
                    out[name] = v
            return out
        return self.cache.get_or_set(("quotes", tuple(symbols)), config.CACHE_TTL_QUOTE, fetch)

    def depth(self, symbol):
        """Market depth (order book, 5 levels) for one symbol. Returns the 'd' block."""
        def fetch():
            resp = self._check(self._fyers().depth({"symbol": symbol, "ohlcv_flag": "1"}), "depth")
            return resp.get("d", {}) or {}
        return self.cache.get_or_set(("depth", symbol), config.CACHE_TTL_DEPTH, fetch)

    def candles(self, symbol, resolution, date_from, date_to):
        """Returns list of [epoch, open, high, low, close, volume]."""
        def fetch():
            resp = self._check(self._fyers().history({
                "symbol": symbol,
                "resolution": resolution,
                "date_format": "1",
                "range_from": date_from.isoformat(),
                "range_to": date_to.isoformat(),
                "cont_flag": "1",
            }), "history")
            return resp.get("candles", []) or []
        ttl = config.CACHE_TTL_DAILY if resolution in ("D", "1D") else config.CACHE_TTL_INTRADAY
        return self.cache.get_or_set(("candles", symbol, resolution, date_from, date_to), ttl, fetch)

    def daily_candles(self, symbol, days=15):
        t = today_ist()
        return self.candles(symbol, "D", t - dt.timedelta(days=days), t)

    def intraday_candles(self, symbol, resolution=None):
        t = today_ist()
        return self.candles(symbol, resolution or config.CANDLE_RESOLUTION, t, t)

    def option_chain(self, symbol, strikecount=None, expiry=""):
        strikecount = strikecount or config.OPTION_STRIKE_COUNT

        def fetch():
            resp = self._check(self._fyers().optionchain({
                "symbol": symbol,
                "strikecount": int(strikecount),
                "timestamp": str(expiry or ""),
                "greeks": "1",
            }), "optionchain")
            return resp.get("data", {})
        return self.cache.get_or_set(("chain", symbol, strikecount, str(expiry or "")),
                                     config.CACHE_TTL_CHAIN, fetch)
