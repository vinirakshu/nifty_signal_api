"""All settings come from environment variables (or a .env file)."""
import os

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
load_dotenv(ENV_FILE)


def _f(name, default):
    return float(os.environ.get(name, default))


def _i(name, default):
    return int(os.environ.get(name, default))


# ---- Fyers app (create at https://myapi.fyers.in/dashboard) ----
FYERS_CLIENT_ID = os.environ.get("FYERS_CLIENT_ID", "").strip()          # e.g. "XB12345-100"
FYERS_SECRET_KEY = os.environ.get("FYERS_SECRET_KEY", "").strip()
FYERS_REDIRECT_URI = os.environ.get("FYERS_REDIRECT_URI", "http://127.0.0.1:5110/auth/callback")
FYERS_ACCESS_TOKEN = os.environ.get("FYERS_ACCESS_TOKEN", "")    # optional: paste today's token
TOKEN_FILE = os.environ.get("TOKEN_FILE", os.path.join(BASE_DIR, "fyers_token.json"))
FYERS_LOG_PATH = os.environ.get("FYERS_LOG_PATH", os.path.join(BASE_DIR, "logs"))

# ---- This API ----
API_KEY = os.environ.get("API_KEY", "")          # if set, /api/* needs header X-API-Key
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = _i("PORT", 5110)

# ---- Instruments ----
NIFTY_INDEX_SYMBOL = os.environ.get("NIFTY_INDEX_SYMBOL", "NSE:NIFTY50-INDEX")
NIFTY_LOT_SIZE = _i("NIFTY_LOT_SIZE", 65)        # NSE lot size from Jan 2026 contracts; verify on change

# ---- Analysis ----
CANDLE_RESOLUTION = os.environ.get("CANDLE_RESOLUTION", "5")   # intraday candle minutes
OPTION_STRIKE_COUNT = _i("OPTION_STRIKE_COUNT", 15)            # strikes each side of ATM
LEVEL_CLUSTER_POINTS = _f("LEVEL_CLUSTER_POINTS", 20)          # merge levels closer than this
MIN_RISK_REWARD = _f("MIN_RISK_REWARD", 1.0)                   # below this -> NO TRADE
BIAS_THRESHOLD = _i("BIAS_THRESHOLD", 2)                       # |score| needed for a trade
PCR_BULLISH = _f("PCR_BULLISH", 1.2)
PCR_BEARISH = _f("PCR_BEARISH", 0.8)

# ---- Cache (seconds) - keeps you inside Fyers rate limits ----
CACHE_TTL_QUOTE = _f("CACHE_TTL_QUOTE", 2)
CACHE_TTL_CHAIN = _f("CACHE_TTL_CHAIN", 5)
CACHE_TTL_INTRADAY = _f("CACHE_TTL_INTRADAY", 15)
CACHE_TTL_DAILY = _f("CACHE_TTL_DAILY", 300)
CACHE_TTL_DEPTH = _f("CACHE_TTL_DEPTH", 3)

# ---- Live market feed (Nifty 50 board, faster paper exits) ----
FEED_ENABLED = os.environ.get("FEED_ENABLED", "1") == "1"
FEED_WEBSOCKET = os.environ.get("FEED_WEBSOCKET", "1") == "1"     # Fyers WebSocket; REST polling if 0
FEED_REST_SECONDS = _f("FEED_REST_SECONDS", 10)                  # REST fallback interval
INDIA_VIX_SYMBOL = os.environ.get("INDIA_VIX_SYMBOL", "NSE:INDIAVIX-INDEX")

# ---- Paper trading (virtual money) ----
PAPER_DB = os.environ.get("PAPER_DB", os.path.join(BASE_DIR, "paper_trades.db"))
PAPER_START_CAPITAL = _f("PAPER_START_CAPITAL", 1_000_000_000)      # ₹100 crore
PAPER_SQUARE_OFF = os.environ.get("PAPER_SQUARE_OFF", "15:15")         # IST, auto-exit time
PAPER_MONITOR = os.environ.get("PAPER_MONITOR", "1") == "1"            # background SL/target checks
PAPER_MONITOR_SECONDS = _f("PAPER_MONITOR_SECONDS", 15)
PAPER_IGNORE_MARKET_HOURS = os.environ.get("PAPER_IGNORE_MARKET_HOURS", "0") == "1"   # demo only
# Approximate option-buying charges (check your broker's current rates)
PAPER_BROKERAGE_PER_ORDER = _f("PAPER_BROKERAGE_PER_ORDER", 20)
PAPER_STT_SELL_PCT = _f("PAPER_STT_SELL_PCT", 0.1)          # STT on option sell premium
PAPER_EXCHANGE_PCT = _f("PAPER_EXCHANGE_PCT", 0.03503)      # NSE option transaction charge
PAPER_STAMP_BUY_PCT = _f("PAPER_STAMP_BUY_PCT", 0.003)      # stamp duty on buy
PAPER_GST_PCT = _f("PAPER_GST_PCT", 18)                     # GST on brokerage + exchange charges

# ---- Auto paper trading (all strategies, all day) ----
AUTO_TRADE_ENABLED = os.environ.get("AUTO_TRADE_ENABLED", "1") == "1"   # start engine with the server
AUTO_STRATEGIES = [x.strip() for x in os.environ.get(
    "AUTO_STRATEGIES", "confluence,orb,ema_vwap,oi_bounce").split(",") if x.strip()]
AUTO_CYCLE_SECONDS = _f("AUTO_CYCLE_SECONDS", 30)
AUTO_ENTRY_START = os.environ.get("AUTO_ENTRY_START", "09:20")
AUTO_ENTRY_END = os.environ.get("AUTO_ENTRY_END", "14:45")
AUTO_RISK_PER_TRADE_PCT = _f("AUTO_RISK_PER_TRADE_PCT", 0.5)        # of each strategy's capital
AUTO_MAX_CAPITAL_PER_TRADE_PCT = _f("AUTO_MAX_CAPITAL_PER_TRADE_PCT", 10)
AUTO_MAX_OI_PCT = _f("AUTO_MAX_OI_PCT", 2)                          # max size as % of option OI
AUTO_MAX_LOTS = _i("AUTO_MAX_LOTS", 5000)
AUTO_MAX_TRADES_PER_STRATEGY = _i("AUTO_MAX_TRADES_PER_STRATEGY", 6)
AUTO_COOLDOWN_MINUTES = _f("AUTO_COOLDOWN_MINUTES", 10)
AUTO_EXIT_AT = os.environ.get("AUTO_EXIT_AT", "T1")
AUTO_MONEYNESS = os.environ.get("AUTO_MONEYNESS", "ATM")
AUTO_SLIPPAGE_PCT = _f("AUTO_SLIPPAGE_PCT", 0.2)                    # extra cost on each fill
AUTO_LOG_DIR = os.environ.get("AUTO_LOG_DIR", os.path.join(BASE_DIR, "logs"))
