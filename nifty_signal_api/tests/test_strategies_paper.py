import datetime as dt
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.environ.setdefault("FYERS_LOG_PATH", "")
os.environ.setdefault("PAPER_DB", ":memory:")

import app as app_module  # noqa: E402
import strategies  # noqa: E402
from fake_fyers import FakeFyers  # noqa: E402
from fyers_client import IST, FyersClient  # noqa: E402
from paper import PaperBook  # noqa: E402


class MemTokens:
    def get(self):
        return "TEST"

    def save(self, t):
        pass


@pytest.fixture
def env(monkeypatch, tmp_path):
    def _make(trend="up", capital=100000, ignore_hours=True):
        fake = FakeFyers(trend=trend)
        c = FyersClient(token_store=MemTokens(), model_factory=lambda tok: fake)
        b = PaperBook(str(tmp_path / "paper.db"), start_capital=capital, ignore_market_hours=ignore_hours)
        monkeypatch.setattr(app_module, "client", c)
        monkeypatch.setattr(app_module, "book", b)
        return app_module.app.test_client(), fake, c, b
    return _make


def move(fake, client, price):
    fake.set_spot(price)
    client.cache.clear()


# ------------------------------------------------------------------ strategies
def test_strategy_list(env):
    tc, *_ = env()
    d = tc.get("/api/strategies").json
    ids = [s["id"] for s in d["strategies"]]
    assert ids == ["confluence", "orb", "ema_vwap", "oi_bounce"]
    assert all(s["rules"] and s["best_for"] for s in d["strategies"])


@pytest.mark.parametrize("sid", ["confluence", "orb", "ema_vwap", "oi_bounce"])
def test_every_strategy_returns_a_report(env, sid):
    tc, *_ = env("up")
    r = tc.get(f"/api/nifty/signal?strategy={sid}")
    assert r.status_code == 200, r.json
    d = r.json
    assert d["strategy"]["id"] == sid and d["strategy"]["reasons"]
    if d["trade"]["action"].startswith("BUY"):
        ix = d["trade"]["index"]
        if d["trade"]["action"] == "BUY CE":
            assert ix["stoploss"] < ix["entry"] < ix["target1"]
        else:
            assert ix["stoploss"] > ix["entry"] > ix["target1"]


def test_unknown_strategy_400(env):
    tc, *_ = env()
    assert tc.get("/api/nifty/signal?strategy=magic").status_code == 400


def test_ema_vwap_bullish_in_uptrend(env):
    tc, *_ = env("up")
    assert tc.get("/api/nifty/signal?strategy=ema_vwap").json["strategy"]["direction"] == "BULLISH"


def _ctx(spot, intra=None, oi=None, candles=10):
    return {"spot": spot, "intra": intra or {}, "oi": oi or {}, "intraday": [[0, 0, 0, 0, 0, 0]] * candles,
            "bias": {"direction": "NEUTRAL", "reasons": [], "score": 0, "max_score": 4, "threshold": 2}}


def test_orb_rules():
    intra = {"opening_range_high": 25050, "opening_range_low": 25000, "atr": 15}
    inside = strategies.run("orb", _ctx(25020, intra))
    assert inside["direction"] == "NEUTRAL"
    up = strategies.run("orb", _ctx(25060, intra))
    assert up["direction"] == "BULLISH"
    o = up["overrides"]
    assert o["stoploss"] == 25025 and o["target1"] == 25100 and o["target2"] == 25150
    down = strategies.run("orb", _ctx(24990, intra))
    assert down["direction"] == "BEARISH" and down["overrides"]["target1"] == 24950
    late = strategies.run("orb", _ctx(25120, intra))
    assert late["direction"] == "NEUTRAL"
    early = strategies.run("orb", _ctx(25060, intra, candles=2))
    assert early["direction"] == "NEUTRAL"


def test_oi_bounce_rules():
    oi = {"put_oi_walls": [{"strike": 25000}], "call_oi_walls": [{"strike": 25300}]}
    intra = {"atr": 15}
    near_put = strategies.run("oi_bounce", _ctx(25012, intra, oi))
    assert near_put["direction"] == "BULLISH" and near_put["overrides"]["stoploss"] < 25000
    near_call = strategies.run("oi_bounce", _ctx(25290, intra, oi))
    assert near_call["direction"] == "BEARISH" and near_call["overrides"]["stoploss"] > 25300
    assert strategies.run("oi_bounce", _ctx(25150, intra, oi))["direction"] == "NEUTRAL"
    assert strategies.run("oi_bounce", _ctx(24950, intra, oi))["direction"] == "NEUTRAL"


# ------------------------------------------------------------------ paper trading
def place(tc, **body):
    body.setdefault("strategy", "confluence")
    return tc.post("/api/paper/trades", json=body)


def test_market_trade_hits_target(env):
    tc, fake, c, b = env("up")
    r = place(tc, lots=2, exit_at="T1")
    assert r.status_code == 201, r.json
    t = r.json["trade"]
    assert t["status"] == "OPEN" and t["qty"] == 130 and t["option_type"] == "CE"
    s = tc.get("/api/paper/summary").json
    assert s["account"]["cash"] < 100000 and len(s["active"]) == 1

    move(fake, c, t["t1_index"] + 2)
    s = tc.get("/api/paper/summary").json
    closed = s["history"][0]
    assert closed["status"] == "CLOSED" and closed["exit_reason"] == "Target 1 hit"
    assert closed["net_pnl"] > 0 and closed["charges"] > 0
    assert s["stats"]["trades"] == 1 and s["stats"]["wins"] == 1
    assert s["by_strategy"][0]["strategy"] == "confluence"
    assert s["account"]["equity"] == pytest.approx(100000 + closed["net_pnl"], abs=0.05)


def test_market_trade_hits_stoploss(env):
    tc, fake, c, b = env("up")
    t = place(tc, lots=1).json["trade"]
    move(fake, c, t["sl_index"] - 2)
    closed = tc.get("/api/paper/summary").json["history"][0]
    assert closed["exit_reason"] == "Stop-loss hit" and closed["net_pnl"] < 0


def test_bearish_put_trade_target2(env):
    tc, fake, c, b = env("down")
    t = place(tc, lots=1, exit_at="T2").json["trade"]
    assert t["option_type"] == "PE"
    move(fake, c, t["t1_index"] - 1)          # T1 reached but we wait for T2
    assert tc.get("/api/paper/summary").json["active"]
    move(fake, c, t["t2_index"] - 1)
    closed = tc.get("/api/paper/summary").json["history"][0]
    assert closed["exit_reason"] == "Target 2 hit" and closed["net_pnl"] > 0


def test_trigger_order_fills_then_manual_close(env):
    tc, fake, c, b = env("up")
    t = place(tc, lots=1, order_type="trigger").json["trade"]
    assert t["status"] == "PENDING"
    move(fake, c, t["trigger_index"] + 1)
    act = tc.get("/api/paper/summary").json["active"][0]
    assert act["status"] == "OPEN" and act["entry_price"] > 0
    r = tc.post(f"/api/paper/trades/{t['id']}/close")
    assert r.json["trade"]["status"] == "CLOSED" and r.json["trade"]["exit_reason"] == "Closed manually"
    assert tc.post(f"/api/paper/trades/{t['id']}/close").status_code == 400   # already closed


def test_cancel_pending(env):
    tc, fake, c, b = env("up")
    t = place(tc, order_type="trigger").json["trade"]
    r = tc.post(f"/api/paper/trades/{t['id']}/close").json
    assert r["trade"]["status"] == "CANCELLED"


def test_no_signal_cannot_paper_trade(env):
    tc, *_ = env("flat")
    r = place(tc)
    assert r.status_code == 400 and "No trade signal" in r.json["error"]


def test_not_enough_cash(env):
    tc, *_ = env("up", capital=1000)
    r = place(tc, lots=1)
    assert r.status_code == 400 and "Not enough virtual cash" in r.json["error"]


def test_validation(env):
    tc, *_ = env("up")
    assert place(tc, lots=0).status_code == 400
    assert place(tc, order_type="limit").status_code == 400
    assert place(tc, exit_at="T3").status_code == 400


def test_reset(env):
    tc, *_ = env("up")
    place(tc, lots=1)
    d = tc.post("/api/paper/reset", json={"capital": 50000}).json
    assert d["account"]["cash"] == 50000 and not d["active"] and not d["history"]


def test_square_off_and_market_hours(tmp_path):
    b = PaperBook(str(tmp_path / "p.db"), ignore_market_hours=False)
    report = {"underlying": {"ltp": 25000},
              "strategy": {"id": "confluence", "name": "Confluence"},
              "trade": {"action": "BUY CE",
                        "index": {"entry": 25000, "stoploss": 24950, "target1": 25080, "target2": 25150},
                        "option": {"symbol": "X", "option_type": "CE", "strike": 25000, "lot_size": 65,
                                   "ltp": 100, "ask": 100.5, "bid": 99.5, "expiry_date": "28-10-2026"}}}
    day = dt.date(2026, 10, 20)   # a Tuesday
    with pytest.raises(Exception):
        b.place(report, 1, now=dt.datetime(2026, 10, 20, 8, 0, tzinfo=IST))      # before open
    t = b.place(report, 1, now=dt.datetime.combine(day, dt.time(10, 0), IST))
    ev = b.monitor({"X": {"ltp": 110, "bid": 109.5, "ask": 110.5}}, 25010,
                   now=dt.datetime.combine(day, dt.time(15, 16), IST))
    assert ev and b.get(t["id"])["exit_reason"] == "Auto square-off 15:15"
    assert b.get(t["id"])["exit_price"] == 109.5


def test_charges_reasonable(tmp_path):
    b = PaperBook(str(tmp_path / "c.db"))
    ch = b.charges(10000, 11000)
    assert 40 < ch < 80   # 2 x Rs20 brokerage + STT + exchange + GST + stamp
