import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.environ.setdefault("FYERS_LOG_PATH", "")
os.environ.setdefault("PAPER_DB", ":memory:")
os.environ.setdefault("AUTO_LOG_DIR", "")

import app as app_module  # noqa: E402
import market  # noqa: E402
from fake_fyers import INDEX, FakeFyers, FakeSocket  # noqa: E402
from fyers_client import FyersClient  # noqa: E402
from market_feed import MarketFeed  # noqa: E402
from paper import PaperBook  # noqa: E402

CONS = market.load_constituents()
SYMS = [INDEX, "NSE:INDIAVIX-INDEX"] + [s["symbol"] for s in CONS["stocks"]]


class Tok:
    def get(self):
        return "T"

    def save(self, t):
        pass


def make(trend="up"):
    fake = FakeFyers(trend=trend)
    client = FyersClient(token_store=Tok(), model_factory=lambda t: fake)
    return fake, client


# ------------------------------------------------------------------ pure analytics
def test_constituents_file():
    assert len(CONS["stocks"]) == 50
    assert abs(sum(s["weight"] for s in CONS["stocks"]) - 100) < 0.1
    assert all(s["symbol"].startswith("NSE:") and s["symbol"].endswith("-EQ") for s in CONS["stocks"])


def test_stock_side_rules():
    assert market.stock_side(1.0, 105, 100, 300, 100)[0] == "Strong buying"
    assert market.stock_side(-1.0, 95, 100, 100, 300)[0] == "Strong selling"
    assert market.stock_side(0.0, 100, 100, 100, 100)[0] == "Neutral"
    label, score, why = market.stock_side(0.5, 99, 100, None, None)   # REST mode: no order-book qty
    assert score == 0 and "up today" in why and "below VWAP" in why


def test_board_contributions_and_weights():
    fake, _ = make("up")
    ticks = {s: fake.stock_tick(s) for s in SYMS}
    b = market.nifty50_board(CONS, ticks, ticks[INDEX])
    assert len(b["stocks"]) == 50 and b["breadth"]["with_data"] == 50
    assert abs(sum(r["live_weight"] for r in b["stocks"]) - 100) < 0.01
    hdfc = next(r for r in b["stocks"] if r["ticker"] == "HDFCBANK")
    assert hdfc["index_points"] == pytest.approx(24950 * 0.1054 * hdfc["change_pct"] / 100, abs=0.02)
    g = [x["change_pct"] for x in b["gainers"]]
    assert g == sorted(g, reverse=True) and all(x > 0 for x in g)
    assert all(x["change_pct"] < 0 for x in b["losers"])
    assert b["breadth"]["advances"] + b["breadth"]["declines"] + b["breadth"]["unchanged"] == 50
    assert all(x["index_points"] > 0 for x in b["top_contributors"])


def test_board_without_data():
    b = market.nifty50_board(CONS, {}, None)
    assert b["breadth"]["with_data"] == 0 and b["gainers"] == [] and b["stocks"][0]["ltp"] is None


def test_option_buildup_and_aggressor():
    assert market.option_buildup(5, 1000) == ("Long buildup", "buy")
    assert market.option_buildup(-5, 1000) == ("Short buildup", "sell")
    assert market.option_buildup(5, -1000) == ("Short covering", "buy")
    assert market.option_buildup(-5, -1000) == ("Long unwinding", "sell")
    assert market.aggressor(10.0, 9.0, 10.0) == "At ask (buyers)"
    assert market.aggressor(9.0, 9.0, 10.0) == "At bid (sellers)"
    assert market.aggressor(9.5, 9.0, 10.0) == "Mid"


def test_options_activity():
    fake, client = make("up")
    chain = client.option_chain(INDEX)
    a = market.options_activity(chain, top_n=5)
    vols = [o["volume"] for o in a["most_active"]]
    assert len(vols) == 5 and vols == sorted(vols, reverse=True)
    assert all(o["type"] == "CE" for o in a["most_active_calls"])
    assert a["volume_pcr"] > 0 and a["total_options"] == 62


def test_parse_depth():
    fake, client = make()
    d = market.parse_depth("NSE:SBIN-EQ", client.depth("NSE:SBIN-EQ"))
    assert len(d["bids"]) == 5 and len(d["asks"]) == 5
    assert d["bids"][0]["price"] < d["asks"][0]["price"] and d["spread"] > 0
    assert 0 < d["buy_pct"] < 100


# ------------------------------------------------------------------ feed
def test_feed_websocket_mode():
    fake, client = make()
    feed = MarketFeed(lambda: client, SYMS, socket_factory=lambda f: FakeSocket(f, fake, interval=0.2))
    feed._step()
    assert feed.ws_connected and feed.ws_live()
    assert len(feed.ticks) == 52 and feed.status()["mode"] == "websocket"
    t = feed.get("NSE:HDFCBANK-EQ")
    assert t["tot_buy_qty"] and t["avg_trade_price"] and t["_src"] == "ws"
    feed.ensure(["NSE:NIFTY26O2825150CE"])
    assert feed.get("NSE:NIFTY26O2825150CE")["ltp"] > 0
    feed._close()


def test_feed_rest_fallback():
    fake, client = make()
    feed = MarketFeed(lambda: client, SYMS, use_websocket=False)
    feed._step()
    assert feed.status()["mode"] == "rest" and len(feed.ticks) == 52
    assert sum(1 for c in fake.calls if c[0] == "quotes") == 2          # 52 symbols -> 2 calls
    assert feed.get("NSE:SBIN-EQ")["_src"] == "rest"


def test_feed_off_without_login():
    fake, client = make()
    client.tokens = type("T", (), {"get": lambda self: None})()
    feed = MarketFeed(lambda: client, SYMS, use_websocket=False)
    feed._step()
    assert feed.status()["mode"] == "off" and not feed.ticks


# ------------------------------------------------------------------ API
@pytest.fixture
def api(monkeypatch, tmp_path):
    fake, client = make("up")
    feed = MarketFeed(lambda: client, SYMS, use_websocket=False)
    book = PaperBook(str(tmp_path / "m.db"), ignore_market_hours=True)
    monkeypatch.setattr(app_module, "client", client)
    monkeypatch.setattr(app_module, "feed", feed)
    monkeypatch.setattr(app_module, "book", book)
    return app_module.app.test_client(), fake, client, feed, book


def test_api_nifty50(api):
    tc, fake, client, feed, book = api
    d = tc.get("/api/market/nifty50").json
    assert d["success"] and len(d["stocks"]) == 50 and d["breadth"]["with_data"] == 50
    assert d["nifty"]["ltp"] and d["feed"]["mode"] == "rest"
    assert d["gainers"] or d["losers"]


def test_api_options_activity(api):
    tc, *_ = api
    d = tc.get("/api/market/options-activity?top=8").json
    assert d["success"] and len(d["most_active"]) == 8 and d["expiry"]["date"] == "28-10-2026"
    assert {"buildup", "aggressor", "oi_change"} <= set(d["most_active"][0])


def test_api_depth(api):
    tc, *_ = api
    d = tc.get("/api/market/depth?symbol=NSE:M%26M-EQ").json
    assert d["success"] and d["name"] == "Mahindra & Mahindra" and len(d["bids"]) == 5
    assert tc.get("/api/market/depth?symbol=DROP TABLE").status_code == 400


def test_paper_monitor_uses_live_feed(api):
    tc, fake, client, feed, book = api
    t = tc.post("/api/paper/trades", json={"strategy": "confluence", "lots": 1}).json["trade"]
    sock = FakeSocket(feed, fake, interval=60)
    feed.socket_factory = lambda f: sock
    feed.use_websocket = True
    feed._connect("T")
    feed.ensure([t["symbol"]])
    assert feed.get(t["symbol"], max_age=5)
    n_quotes = sum(1 for c in fake.calls if c[0] == "quotes")
    fake.set_spot(t["t1_index"] + 1)
    sock.push_once()                              # new ticks arrive over the "WebSocket"
    app_module.run_paper_monitor()
    assert book.get(t["id"])["exit_reason"] == "Target 1 hit"
    assert sum(1 for c in fake.calls if c[0] == "quotes") == n_quotes   # no REST call needed
    feed._close()
