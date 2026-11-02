import datetime as dt
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.environ.setdefault("FYERS_LOG_PATH", "")
os.environ.setdefault("PAPER_DB", ":memory:")
os.environ.setdefault("AUTO_LOG_DIR", "")

import app as app_module  # noqa: E402
from autotrader import AutoTrader  # noqa: E402
from fake_fyers import FakeFyers  # noqa: E402
from fyers_client import IST, FyersClient  # noqa: E402
from paper import PaperBook  # noqa: E402

TUE = dt.date(2026, 10, 20)


def at(h, m, day=TUE):
    return dt.datetime.combine(day, dt.time(h, m), IST)


class Tokens:
    def __init__(self, ok=True):
        self.ok = ok

    def get(self):
        return "T" if self.ok else None

    def save(self, t):
        self.ok = True


@pytest.fixture
def rig(monkeypatch, tmp_path):
    def _make(trend="up", logged_in=True, capital=1_000_000_000, **cfg_over):
        fake = FakeFyers(trend=trend)
        client = FyersClient(token_store=Tokens(logged_in), model_factory=lambda tok: fake)
        book = PaperBook(str(tmp_path / "auto.db"), start_capital=capital, ignore_market_hours=True)
        monkeypatch.setattr(app_module, "client", client)
        monkeypatch.setattr(app_module, "book", book)
        cfg = app_module._auto_cfg()
        cfg.update({"ignore_market_hours": False, "cooldown_minutes": 10, **cfg_over})
        clock = {"now": at(10, 0)}
        eng = AutoTrader(book, get_report=lambda sid: app_module.build_report(sid, "ATM", ""),
                         run_monitor=app_module.run_paper_monitor,
                         is_logged_in=client.is_logged_in, cfg=cfg, now_fn=lambda: clock["now"])
        eng.enabled = True
        monkeypatch.setattr(app_module, "engine", eng)
        return eng, fake, client, book, clock
    return _make


def msgs(book):
    return [e["message"] for e in book.events(limit=1000)]


def test_waits_before_open_and_for_login(rig):
    eng, fake, client, book, clock = rig(logged_in=False)
    eng.step(at(8, 50))
    assert eng.state == "BEFORE_OPEN" and any("Waiting for the market" in m for m in msgs(book))
    eng.step(at(9, 30))
    assert eng.state == "WAITING_LOGIN"
    assert not book.summary()["active"]
    client.tokens.save("x")
    eng.step(at(9, 31))
    assert eng.state == "RUNNING"


def test_weekend_closed(rig):
    eng, *_ = rig()
    eng.step(at(11, 0, dt.date(2026, 10, 24)))   # Saturday
    assert eng.state == "MARKET_CLOSED"


def test_auto_entries_one_per_strategy(rig):
    eng, fake, client, book, clock = rig("up")
    eng.step(at(10, 0))
    active = book.summary()["active"]
    strategies_in = {t["strategy"] for t in active}
    assert {"confluence", "ema_vwap"} <= strategies_in          # both give BUY CE in the up-trend demo
    assert all(t["source"] == "auto" and t["option_type"] == "CE" for t in active)
    assert all("limited by" in (t["size_note"] or "") for t in active)
    n = len(active)
    client.cache.clear()
    eng.step(at(10, 1))
    assert len(book.summary()["active"]) == n                   # no duplicate positions
    log = msgs(book)
    assert any("AUTO ENTRY" in m for m in log) and any("Market session started" in m for m in log)


def test_size_respects_caps(rig):
    eng, fake, client, book, clock = rig("up")
    rep = app_module.build_report("confluence", "ATM", "")
    lots, note = eng.size(rep)
    opt = rep["trade"]["option"]
    alloc = 1_000_000_000 / 4
    assert lots >= 1
    assert lots * opt["lot_size"] * opt["ask"] <= alloc * 0.10 + 1          # capital cap
    assert lots * opt["lot_size"] <= opt["oi"] * 0.02 + opt["lot_size"]    # liquidity cap


def test_exit_then_cooldown_then_reentry(rig):
    eng, fake, client, book, clock = rig("up")
    eng.step(at(10, 0))
    t = book.active_for_strategy("confluence")
    fake.set_spot(t["t1_index"] + 1)
    client.cache.clear()
    eng.step(at(10, 5))
    closed = [h for h in book.summary()["history"] if h["strategy"] == "confluence"]
    assert closed and closed[0]["exit_reason"] == "Target 1 hit"
    exit_time = dt.datetime.fromisoformat(closed[0]["exit_time"])
    # back to a level where confluence signals again
    fake._intraday = fake._make_intraday()
    client.cache.clear()
    eng.step(exit_time + dt.timedelta(minutes=3))
    assert book.active_for_strategy("confluence") is None          # cooling down
    assert any("cooling down" in m for m in msgs(book))
    client.cache.clear()
    eng.step(exit_time + dt.timedelta(minutes=11))
    assert book.active_for_strategy("confluence") is not None      # re-entered after cooldown


def test_max_trades_per_day(rig):
    eng, fake, client, book, clock = rig("up", max_trades=1, cooldown_minutes=0)
    eng.step(at(10, 0))
    t = book.active_for_strategy("confluence")
    book.close(t["id"], 1.0, None, now=at(10, 1))
    client.cache.clear()
    eng.step(at(10, 2))
    assert book.active_for_strategy("confluence") is None
    assert any("Max 1 trades" in m for m in msgs(book))


def test_no_entries_after_cutoff_and_day_summary(rig):
    eng, fake, client, book, clock = rig("up")
    eng.step(at(14, 50))
    assert eng.state == "ENTRY_CLOSED" and not book.summary()["active"]
    eng.step(at(10, 0))   # (same day) enter something so the summary has trades
    assert book.summary()["active"]
    eng.step(at(15, 35))
    eng.step(at(15, 36))
    summ = [m for m in msgs(book) if m.startswith("Day summary 2026-10-20")]
    assert len(summ) == 1 and eng.state == "MARKET_CLOSED"


def test_stopped_engine_does_nothing(rig):
    eng, fake, client, book, clock = rig("up")
    eng.enabled = False
    eng.step(at(10, 0))
    assert eng.state == "STOPPED" and not book.summary()["active"]


def test_api_endpoints(rig):
    eng, fake, client, book, clock = rig("up")
    eng.step(at(10, 0))
    tc = app_module.app.test_client()
    st = tc.get("/api/auto/status").json
    assert st["state"] == "RUNNING" and len(st["strategies"]) == 4
    conf = next(s for s in st["strategies"] if s["id"] == "confluence")
    assert conf["position"]["status"] == "OPEN" and conf["today"]["trades"] == 1
    assert st["capital_per_strategy"] == 250_000_000
    log = tc.get("/api/auto/log?after=0").json["events"]
    assert log and log[0]["id"] > log[-1]["id"]                    # newest first
    newest = log[0]["id"]
    assert tc.get(f"/api/auto/log?after={newest}").json["events"] == []
    assert tc.post("/api/auto/stop").json["enabled"] is False
    assert tc.get("/api/auto/log?after=abc").status_code == 400
