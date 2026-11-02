import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.environ.setdefault("FYERS_LOG_PATH", "")
os.environ.setdefault("PAPER_DB", ":memory:")

import analysis  # noqa: E402
import app as app_module  # noqa: E402
from fake_fyers import FakeFyers  # noqa: E402
from fyers_client import FyersClient  # noqa: E402


class MemTokens:
    def __init__(self, token="TEST"):
        self.token = token

    def get(self):
        return self.token

    def save(self, t):
        self.token = t


def make_client(trend="up", token="TEST", fail=None):
    fake = FakeFyers(trend=trend, fail=fail)
    c = FyersClient(token_store=MemTokens(token), model_factory=lambda tok: fake)
    return c, fake


@pytest.fixture
def api(monkeypatch):
    def _make(trend="up", token="TEST", fail=None):
        c, fake = make_client(trend, token, fail)
        monkeypatch.setattr(app_module, "client", c)
        return app_module.app.test_client(), fake
    return _make


# ------------------------------------------------------------------ unit tests
def test_pivots_known_values():
    p = analysis.pivots(25040, 24880, 24950)
    assert p["P"] == 24956.67
    assert p["R1"] == 25033.33 and p["S1"] == 24873.33
    assert p["R2"] == 25116.67 and p["S2"] == 24796.67
    assert p["CPR_TC"] >= p["CPR_BC"]


def test_ema_and_atr():
    assert analysis.ema([1, 2, 3], 5) is None
    assert round(analysis.ema(list(range(1, 22)), 9), 4) > 0
    candles = [[0, 10, 12, 9, 11, 0], [0, 11, 13, 10, 12, 0]]
    assert analysis.atr(candles) == 3


def test_twap_used_when_no_volume():
    v, kind = analysis.average_price([[0, 1, 2, 0, 1, 0], [0, 1, 4, 2, 3, 0]])
    assert kind == "TWAP" and v == 2.0


# ------------------------------------------------------------------ API tests
def test_bullish_signal_buys_call(api):
    tc, fake = api("up")
    r = tc.get("/api/nifty/signal")
    assert r.status_code == 200, r.json
    d = r.json
    assert d["bias"]["direction"] == "BULLISH"
    t = d["trade"]
    assert t["action"] in ("BUY CE", "NO TRADE")
    idx = t["index"]
    assert idx["stoploss"] < idx["entry"] < idx["target1"] <= idx["target2"]
    opt = t["option"]
    assert opt["option_type"] == "CE" and opt["symbol"].endswith("CE")
    assert opt["strike"] == opt["atm_strike"]
    assert opt["est_stoploss_premium"] < opt["est_entry_premium"] < opt["est_target1_premium"]
    assert opt["lot_size"] == 65
    assert d["levels"]["immediate_support"]["level"] < d["underlying"]["ltp"]
    assert d["levels"]["immediate_resistance"]["level"] > d["underlying"]["ltp"]


def test_bearish_signal_buys_put_itm(api):
    tc, _ = api("down")
    r = tc.get("/api/nifty/signal?moneyness=ITM1")
    d = r.json
    assert r.status_code == 200, d
    assert d["bias"]["direction"] == "BEARISH"
    idx, opt = d["trade"]["index"], d["trade"]["option"]
    assert idx["stoploss"] > idx["entry"] > idx["target1"] >= idx["target2"]
    assert opt["option_type"] == "PE" and opt["strike"] == opt["atm_strike"] + 50   # ITM put = higher strike
    assert opt["est_stoploss_premium"] < opt["est_entry_premium"] < opt["est_target1_premium"]


def test_flat_market_no_trade(api):
    tc, _ = api("flat")
    d = tc.get("/api/nifty/signal").json
    assert d["trade"]["action"] == "NO TRADE"


def test_levels_and_chain_and_expiries(api):
    tc, _ = api("up")
    lv = tc.get("/api/nifty/levels").json
    assert lv["success"] and lv["pivots"]["P"] and len(lv["levels"]["support"]) >= 1
    ch = tc.get("/api/nifty/option-chain?strikecount=5").json
    assert ch["success"] and len(ch["strikes"]) == 11 and ch["strike_step"] == 50
    assert ch["strikes"][0]["CE"]["symbol"] and ch["strikes"][0]["PE"]["symbol"]
    ex = tc.get("/api/nifty/expiries").json
    assert ex["expiries"][0]["date"] == "28-10-2026"


def test_specific_expiry_passed_through(api):
    tc, fake = api("up")
    d = tc.get("/api/nifty/signal?expiry=1762250400").json
    assert d["trade"]["option"]["expiry_epoch"] == "1762250400"
    assert ("optionchain", {"symbol": "NSE:NIFTY50-INDEX", "strikecount": 15,
                            "timestamp": "1762250400", "greeks": "1"}) in fake.calls


def test_validation_errors(api):
    tc, _ = api("up")
    assert tc.get("/api/nifty/signal?moneyness=XYZ").status_code == 400
    assert tc.get("/api/nifty/signal?expiry=abc").status_code == 400
    assert tc.get("/api/nifty/option-chain?strikecount=999").status_code == 400


def test_not_logged_in_returns_401(api):
    tc, _ = api("up", token=None)
    r = tc.get("/api/nifty/signal")
    assert r.status_code == 401 and r.json["login_url"] == "/auth/login-url"


def test_fyers_token_rejected_returns_401(api):
    tc, _ = api("up", fail="quotes")
    r = tc.get("/api/nifty/signal")
    assert r.status_code == 401 and r.json["fyers_code"] == -16


def test_api_key_enforced(api, monkeypatch):
    tc, _ = api("up")
    monkeypatch.setattr(app_module.config, "API_KEY", "secret")
    assert tc.get("/api/nifty/signal").status_code == 401
    assert tc.get("/api/nifty/signal", headers={"X-API-Key": "secret"}).status_code == 200


def test_cache_limits_fyers_calls(api):
    tc, fake = api("up")
    tc.get("/api/nifty/signal")
    n = len(fake.calls)
    tc.get("/api/nifty/signal")
    assert len(fake.calls) == n   # second call served from cache


def test_web_page_served(api):
    tc, _ = api("up")
    r = tc.get("/")
    assert r.status_code == 200 and b"Nifty Signal" in r.data


def test_callback_redirects_to_page(api, monkeypatch):
    tc, _ = api("up")
    r = tc.get("/auth/callback")
    assert r.status_code == 302 and "login=failed" in r.headers["Location"]

    def ok(code, state=None):
        return "TOKEN"
    monkeypatch.setattr(app_module.client, "exchange_auth_code", ok)
    r = tc.get("/auth/callback?auth_code=abc&state=xyz")
    assert r.status_code == 302 and r.headers["Location"].endswith("/?login=success")


def test_print_sample(api):
    tc, _ = api("up")
    d = tc.get("/api/nifty/signal").json
    with open(os.path.join(ROOT, "sample_response.json"), "w") as fh:
        json.dump(d, fh, indent=2)
