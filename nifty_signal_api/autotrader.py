"""
All-day auto paper trader.

Once started (automatically when the server starts, if AUTO_TRADE_ENABLED=1) it:
  1. waits for the Fyers login and for the market to open (9:15 IST, Mon-Fri)
  2. every AUTO_CYCLE_SECONDS: checks open paper trades (stop-loss / target / 15:15 square-off),
     then runs every strategy on live data and opens a paper trade when a strategy gives a BUY signal
  3. stops new entries after AUTO_ENTRY_END (14:45), squares off at 15:15, and logs a day summary
     after the market closes (15:30)

Position size per trade (capital is split equally between strategies):
  lots = min( risk budget  / risk per lot,           # AUTO_RISK_PER_TRADE_PCT of the strategy's capital
              capital cap  / cost per lot,            # AUTO_MAX_CAPITAL_PER_TRADE_PCT of the strategy's capital
              liquidity cap: AUTO_MAX_OI_PCT of the option's open interest )
"""
import datetime as dt
import logging
import math
import threading
import time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger("autotrader")

MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 30)


def _hm(s):
    h, m = (int(x) for x in s.split(":"))
    return dt.time(h, m)


def _cr(v):
    """Format rupees in lakh / crore for log lines."""
    if v >= 1e7:
        return f"₹{v / 1e7:,.2f} Cr"
    if v >= 1e5:
        return f"₹{v / 1e5:,.2f} L"
    return f"₹{v:,.0f}"


class AutoTrader:
    def __init__(self, book, get_report, run_monitor, is_logged_in, cfg, now_fn=None):
        self.book = book
        self.get_report = get_report
        self.run_monitor = run_monitor
        self.is_logged_in = is_logged_in
        self.cfg = cfg
        self.now_fn = now_fn or (lambda: dt.datetime.now(IST))
        self.enabled = False
        self.state, self.message = "STOPPED", "Auto-trading is off"
        self.last_cycle = None
        self.strat = {sid: {"status": "Not checked yet", "direction": None, "action": None, "updated": None}
                      for sid in cfg["strategies"]}
        self._once_keys = set()
        self._err_last = {}
        self._thread = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ control
    def start(self, log=True):
        self.enabled = True
        if log:
            self.book.log("Auto-trading started - all strategies will be paper traded on live data.", "system")
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="auto-trader", daemon=True)
            self._thread.start()

    def stop(self):
        self.enabled = False
        self.state, self.message = "STOPPED", "Auto-trading is off"
        self.book.log("Auto-trading stopped by user. Open paper trades are still monitored for exits.", "system")

    def _loop(self):
        while True:
            try:
                self.step()
            except Exception as e:  # noqa: BLE001 - never let the engine die
                self._error("engine", f"Engine error: {e}")
            time.sleep(self.cfg["cycle_seconds"])

    # ------------------------------------------------------------------ helpers
    def _once(self, key, day):
        k = (day, key)
        if k in self._once_keys:
            return False
        self._once_keys.add(k)
        return True

    def _error(self, key, msg, strategy=None):
        now = time.monotonic()
        if now - self._err_last.get(key, -1e9) > 300:      # same error at most every 5 min
            self._err_last[key] = now
            self.book.log(msg, "error", strategy)
            logger.warning(msg)

    def _set(self, state, message):
        self.state, self.message = state, message

    def strategy_capital(self):
        cap = self.book.db.execute("SELECT starting_capital FROM account WHERE id=1").fetchone()["starting_capital"]
        return cap / max(1, len(self.cfg["strategies"]))

    # ------------------------------------------------------------------ sizing
    def size(self, report):
        c = self.cfg
        opt = report["trade"]["option"]
        lot = int(opt["lot_size"])
        price = (opt.get("ask") or opt.get("ltp") or 0) * (1 + c["slippage_pct"] / 100)
        if price <= 0:
            return 0, "no option price"
        unit_risk = (opt.get("est_entry_premium") or price) - (opt.get("est_stoploss_premium") or 0)
        if unit_risk <= 0.05:
            unit_risk = 0.3 * price
        alloc = self.strategy_capital()
        risk_budget = alloc * c["risk_pct"] / 100
        cap_budget = alloc * c["max_capital_pct"] / 100
        by_risk = math.floor(risk_budget / (unit_risk * lot))
        by_cap = math.floor(cap_budget / (price * lot))
        limits = {"risk": by_risk, "capital": by_cap}
        oi = opt.get("oi")
        if oi:
            limits["liquidity"] = math.floor(oi * c["max_oi_pct"] / 100 / lot)
        limits["max lots"] = c["max_lots"]
        lots = max(0, min(limits.values()))
        bound = min(limits, key=limits.get)
        note = (f"size: {lots} lots, limited by {bound} "
                f"(risk {_cr(risk_budget)} → {by_risk}, capital cap {_cr(cap_budget)} → {by_cap}"
                + (f", {c['max_oi_pct']}% of OI → {limits['liquidity']}" if "liquidity" in limits else "") + ")")
        return lots, note

    # ------------------------------------------------------------------ one cycle
    def step(self, now=None):
        with self._lock:
            return self._step(now or self.now_fn())

    def _step(self, now):
        c = self.cfg
        day = now.date().isoformat()
        t = now.time()
        ignore = c["ignore_market_hours"]

        if not self.enabled:
            self._set("STOPPED", "Auto-trading is off")
            return

        if not ignore and now.weekday() >= 5:
            self._set("MARKET_CLOSED", "Weekend - market closed")
            return
        if not ignore and t < MARKET_OPEN:
            self._set("BEFORE_OPEN", f"Waiting for market open at 9:15 (now {now.strftime('%H:%M')})")
            if self._once("before_open", day):
                self.book.log("Waiting for the market to open at 9:15.", "system")
            return
        if not ignore and t > MARKET_CLOSE:
            self._set("MARKET_CLOSED", "Market closed for the day")
            if self._once("summary", day) and not self.book.has_event(day, f"Day summary {day}"):
                try:
                    if self.is_logged_in():
                        self.run_monitor(now)
                except Exception as e:  # noqa: BLE001
                    self._error("monitor", f"Exit check failed: {e}")
                self.day_summary(day, now)
            return

        if not self.is_logged_in():
            self._set("WAITING_LOGIN", "Waiting for Fyers login - open the page and click 'Log in to Fyers'")
            if self._once("login", day):
                self.book.log("Fyers login needed for today. Trading starts automatically after you log in.", "warn")
            return

        if self._once("open", day):
            alloc = self.strategy_capital()
            self.book.log(f"Market session started. Strategies: {', '.join(c['strategies'])} · "
                          f"{_cr(alloc)} each · checking every {c['cycle_seconds']:.0f}s.", "system")

        try:
            self.run_monitor(now)
        except Exception as e:  # noqa: BLE001
            self._error("monitor", f"Exit check failed: {e}")

        self.last_cycle = now
        if not ignore and not (c["entry_start"] <= t <= c["entry_end"]):
            if t < c["entry_start"]:
                self._set("RUNNING", f"Watching - entries start at {c['entry_start'].strftime('%H:%M')}")
            else:
                self._set("ENTRY_CLOSED", f"No new entries after {c['entry_end'].strftime('%H:%M')}; "
                                          f"managing open trades until {c['square_off']}")
                if self._once("entry_closed", day):
                    self.book.log(f"Entry window closed at {c['entry_end'].strftime('%H:%M')}. Open trades exit at "
                                  f"stop-loss / target or {c['square_off']}.", "system")
            for sid in c["strategies"]:
                self._update_status(sid, None, now, day)
            return

        self._set("RUNNING", f"Analysing {len(c['strategies'])} strategies every {c['cycle_seconds']:.0f}s")
        for sid in c["strategies"]:
            try:
                report = self.get_report(sid)
            except Exception as e:  # noqa: BLE001
                self._error(f"report:{sid}", f"Could not analyse: {e}", sid)
                continue
            self._update_status(sid, report, now, day)
            self._maybe_enter(sid, report, now, day)

    def _update_status(self, sid, report, now, day):
        s = self.strat[sid]
        if report is None:
            s["status"] = "Entries closed"
            return
        st, tr = report["strategy"], report["trade"]
        changed = (st["status"], tr["action"]) != (s.get("logged_status"), s.get("logged_action"))
        s.update(status=st["status"], direction=st["direction"], action=tr["action"],
                 updated=now.isoformat(timespec="seconds"), reason=tr["reason"])
        last = s.get("logged_at")
        # log changes, but at most once a minute per strategy (signals can flip every cycle)
        if changed and (last is None or now - last >= dt.timedelta(seconds=60)):
            s.update(logged_status=st["status"], logged_action=tr["action"], logged_at=now)
            if tr["action"].startswith("BUY"):
                msg, level = f"Signal {tr['action']} · {st['status']}", "signal"
            else:
                extra = tr["reason"].split(", but ", 1)[1] if ", but " in tr["reason"] else \
                    (st["reasons"][-1] if st.get("reasons") else "")
                msg, level = f"No trade · {st['status']}" + (f" · {extra}" if extra else ""), "info"
            self.book.log(msg, level, sid, now)

    def _maybe_enter(self, sid, report, now, day):
        c = self.cfg
        tr = report["trade"]
        if not tr["action"].startswith("BUY") or not tr.get("option"):
            return
        if self.book.active_for_strategy(sid):
            return
        n = self.book.trades_today(sid, day)
        if n >= c["max_trades"]:
            if self._once(f"max:{sid}", day):
                self.book.log(f"Max {c['max_trades']} trades for today reached - no more entries.", "info", sid, now)
            return
        last_exit = self.book.last_exit_time(sid, day)
        if last_exit and now - last_exit < dt.timedelta(minutes=c["cooldown_minutes"]):
            until = (last_exit + dt.timedelta(minutes=c["cooldown_minutes"])).strftime("%H:%M")
            if self._once(f"cool:{sid}:{last_exit.isoformat()}", day):
                self.book.log(f"Signal ignored - cooling down after last exit until {until}.", "info", sid, now)
            return
        lots, note = self.size(report)
        if lots < 1:
            if self._once(f"small:{sid}:{tr['option']['symbol']}", day):
                self.book.log(f"Signal skipped - position size below 1 lot ({note}).", "warn", sid, now)
            return
        try:
            self.book.place(report, lots, "market", c["exit_at"], now=now, source="auto",
                            slippage_pct=c["slippage_pct"], size_note=note, max_lots=c["max_lots"])
        except Exception as e:  # noqa: BLE001
            self._error(f"place:{sid}", f"Entry failed: {e}", sid)

    # ------------------------------------------------------------------ reporting
    def day_summary(self, day, now=None):
        stats = self.book.today_by_strategy(day)
        if not stats:
            self.book.log(f"Day summary {day}: no trades today.", "summary", None, now)
            return
        total = 0.0
        for sid, o in stats.items():
            total += o["net_pnl"]
            wr = f"{o['wins'] / o['closed'] * 100:.0f}%" if o["closed"] else "-"
            self.book.log(f"Day summary: {o['trades']} trades, win rate {wr}, net P&L "
                          f"{'+' if o['net_pnl'] >= 0 else '-'}{_cr(abs(o['net_pnl']))}", "summary", sid, now)
        self.book.log(f"Day summary {day}: total net P&L {'+' if total >= 0 else '-'}{_cr(abs(total))} "
                      f"across {sum(o['trades'] for o in stats.values())} trades.", "summary", None, now)

    def status(self):
        now = self.now_fn()
        day = now.date().isoformat()
        today = self.book.today_by_strategy(day)
        names = self.cfg.get("names", {})
        rows = []
        for sid in self.cfg["strategies"]:
            s = self.strat[sid]
            act = self.book.active_for_strategy(sid)
            rows.append({
                "id": sid, "name": names.get(sid, sid), "status": s["status"], "direction": s["direction"],
                "action": s["action"], "updated": s["updated"],
                "position": None if not act else {
                    "id": act["id"], "status": act["status"], "symbol": act["symbol"], "qty": act["qty"],
                    "lots": act["lots"], "entry_price": act["entry_price"], "last_price": act["last_price"],
                    "unrealised_pnl": act.get("unrealised_pnl"), "sl_index": act["sl_index"],
                    "target_index": act["t1_index"] if act["exit_at"] == "T1" else act["t2_index"]},
                "today": today.get(sid, {"trades": 0, "closed": 0, "wins": 0, "net_pnl": 0.0, "open_pnl": 0.0}),
            })
        c = self.cfg
        return {
            "enabled": self.enabled, "state": self.state, "message": self.message,
            "last_cycle": self.last_cycle.isoformat(timespec="seconds") if self.last_cycle else None,
            "capital_per_strategy": round(self.strategy_capital(), 2),
            "settings": {
                "cycle_seconds": c["cycle_seconds"], "entry_window": f"{c['entry_start'].strftime('%H:%M')}–"
                f"{c['entry_end'].strftime('%H:%M')}", "square_off": c["square_off"],
                "risk_per_trade_pct": c["risk_pct"], "max_capital_per_trade_pct": c["max_capital_pct"],
                "max_oi_pct": c["max_oi_pct"], "max_trades_per_strategy": c["max_trades"],
                "cooldown_minutes": c["cooldown_minutes"], "exit_at": c["exit_at"],
                "moneyness": c["moneyness"], "slippage_pct": c["slippage_pct"],
            },
            "strategies": rows,
        }
