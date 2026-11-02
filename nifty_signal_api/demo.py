"""
Demo mode: runs the API, web page and paper trading with simulated market data.
No Fyers login needed, market hours are ignored, and prices drift every few seconds.

    python demo.py            # bullish demo
    python demo.py down       # bearish demo
    python demo.py flat       # no-trade demo
Then open http://127.0.0.1:5110
"""
import os
import sys

os.environ.setdefault("PAPER_IGNORE_MARKET_HOURS", "1")
os.environ.setdefault("PAPER_DB", "paper_trades_demo.db")
os.environ.setdefault("PAPER_MONITOR_SECONDS", "5")
os.environ.setdefault("AUTO_CYCLE_SECONDS", "5")
os.environ.setdefault("AUTO_COOLDOWN_MINUTES", "1")
os.environ.setdefault("AUTO_LOG_DIR", "")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests"))

import app as app_module  # noqa: E402
import config  # noqa: E402
from fake_fyers import FakeFyers, FakeSocket  # noqa: E402
from fyers_client import FyersClient  # noqa: E402


class _DemoTokens:
    def get(self):
        return "DEMO"

    def save(self, token):
        pass


if __name__ == "__main__":
    trend = sys.argv[1] if len(sys.argv) > 1 else "up"
    fake = FakeFyers(trend=trend, walk=True)
    app_module.client = FyersClient(token_store=_DemoTokens(), model_factory=lambda t: fake)
    app_module.start_monitor()
    app_module.engine.start()
    app_module.feed.socket_factory = lambda f: FakeSocket(f, fake, interval=1.0)
    app_module.feed.start()
    print(f"DEMO MODE ({trend}) - simulated data, not real prices. Open http://127.0.0.1:{config.PORT}")
    app_module.app.run(host="127.0.0.1", port=config.PORT, debug=False, use_reloader=False)
