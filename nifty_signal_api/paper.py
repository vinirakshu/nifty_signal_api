"""
Paper trading book (virtual money) stored in SQLite.

- Orders: "market" (fill now at the option's ask) or "trigger" (wait until Nifty crosses the entry level).
- Exits are decided on the Nifty index levels from the signal (stop-loss / target 1 or 2),
  filled at the option's live bid. Open positions are squared off at PAPER_SQUARE_OFF (15:15).
- Charges are an approximation of Indian option-buying costs and are configurable.
"""
import datetime as dt
import os
import sqlite3
import threading
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    starting_capital REAL NOT NULL,
    cash REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT, strategy TEXT, strategy_name TEXT,
    symbol TEXT, option_type TEXT, strike REAL, expiry_date TEXT,
    lots INTEGER, lot_size INTEGER, qty INTEGER,
    order_type TEXT, exit_at TEXT, status TEXT,
    created_at TEXT, entry_time TEXT, entry_price REAL, entry_index REAL,
    trigger_index REAL, sl_index REAL, t1_index REAL, t2_index REAL,
    last_price REAL, last_index REAL, updated_at TEXT,
    exit_time TEXT, exit_price REAL, exit_index REAL, exit_reason TEXT,
    charges REAL, gross_pnl REAL, net_pnl REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, day TEXT, level TEXT, strategy TEXT, message TEXT
);
"""

# columns added after the first version - created on startup if missing
EXTRA_TRADE_COLUMNS = {"source": "TEXT", "slippage_pct": "REAL", "size_note": "TEXT"}


def _tick(x):
    return round(max(0.05, round(x / 0.05) * 0.05), 2)


def _inr(v, d=2):
    """Indian digit grouping: 12,34,567.89"""
    neg, v = v < 0, abs(float(v))
    whole, frac = f"{v:.{d}f}".split(".") if d else (f"{v:.0f}", "")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return ("-" if neg else "") + whole + (f".{frac}" if d else "")


def _amt(v):
    """Rupee amount in lakh / crore for log lines."""
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e7:
        return f"{sign}₹{a / 1e7:.2f} Cr"
    if a >= 1e5:
        return f"{sign}₹{a / 1e5:.2f} L"
    return f"{sign}₹{_inr(a)}"

ACTIVE = ("PENDING", "OPEN")


class PaperError(Exception):
    pass


def _ts(now):
    return now.isoformat(timespec="seconds")


class PaperBook:
    def __init__(self, path, start_capital=100000.0, brokerage=20.0, stt_sell_pct=0.1,
                 exchange_pct=0.03503, stamp_buy_pct=0.003, gst_pct=18.0, square_off="15:15",
                 ignore_market_hours=False, log_dir=None):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(trades)")}
        for col, typ in EXTRA_TRADE_COLUMNS.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
        self.db.commit()
        self.log_dir = log_dir
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        self.start_capital = float(start_capital)
        self.brokerage, self.stt_sell_pct = float(brokerage), float(stt_sell_pct)
        self.exchange_pct, self.stamp_buy_pct, self.gst_pct = float(exchange_pct), float(stamp_buy_pct), float(gst_pct)
        h, m = (int(x) for x in square_off.split(":"))
        self.square_off = dt.time(h, m)
        self.ignore_market_hours = ignore_market_hours
        with self.lock:
            acc = self.db.execute("SELECT * FROM account WHERE id=1").fetchone()
            if not acc:
                self._reset(self.start_capital)
            elif acc["starting_capital"] != self.start_capital and \
                    not self.db.execute("SELECT 1 FROM trades LIMIT 1").fetchone():
                self._reset(self.start_capital)   # capital setting changed and no trades yet

    # ------------------------------------------------------------------ event log
    def log(self, message, level="info", strategy=None, now=None):
        """Store an event for the on-screen log (and the daily log file)."""
        now = now or dt.datetime.now(IST)
        with self.lock:
            self.db.execute("INSERT INTO events (ts, day, level, strategy, message) VALUES (?, ?, ?, ?, ?)",
                            (_ts(now), now.date().isoformat(), level, strategy, message))
            self.db.commit()
        if self.log_dir:
            try:
                with open(os.path.join(self.log_dir, f"auto_{now.date().isoformat()}.log"), "a",
                          encoding="utf-8") as fh:
                    fh.write(f"{now.strftime('%H:%M:%S')}\t{level.upper()}\t{strategy or '-'}\t{message}\n")
            except OSError:
                pass

    def has_event(self, day, prefix):
        with self.lock:
            return bool(self.db.execute("SELECT 1 FROM events WHERE day=? AND message LIKE ? LIMIT 1",
                                        (day, prefix + "%")).fetchone())

    def events(self, after_id=0, limit=200, day=None):
        q, args = "SELECT * FROM events WHERE id > ?", [int(after_id)]
        if day:
            q += " AND day = ?"
            args.append(day)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self.lock:
            return [dict(r) for r in self.db.execute(q, args)]

    # ------------------------------------------------------------------ per-strategy queries
    def active_for_strategy(self, strategy):
        with self.lock:
            r = self.db.execute(f"SELECT * FROM trades WHERE strategy=? AND status IN {ACTIVE} "
                                "ORDER BY id DESC LIMIT 1", (strategy,)).fetchone()
            return self._view(r) if r else None

    def trades_today(self, strategy, day):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) c FROM trades WHERE strategy=? AND trade_date=? "
                                   "AND status != 'CANCELLED'", (strategy, day)).fetchone()["c"]

    def last_exit_time(self, strategy, day):
        with self.lock:
            r = self.db.execute("SELECT MAX(exit_time) t FROM trades WHERE strategy=? AND trade_date=? "
                                "AND status='CLOSED'", (strategy, day)).fetchone()
            return dt.datetime.fromisoformat(r["t"]) if r and r["t"] else None

    def today_by_strategy(self, day):
        with self.lock:
            rows = [dict(r) for r in self.db.execute("SELECT * FROM trades WHERE trade_date=?", (day,))]
        out = {}
        for t in rows:
            o = out.setdefault(t["strategy"], {"trades": 0, "closed": 0, "wins": 0, "net_pnl": 0.0,
                                               "open_pnl": 0.0})
            if t["status"] == "CANCELLED":
                continue
            o["trades"] += 1
            if t["status"] == "CLOSED":
                o["closed"] += 1
                o["net_pnl"] += t["net_pnl"] or 0
                o["wins"] += 1 if (t["net_pnl"] or 0) > 0 else 0
            elif t["status"] == "OPEN" and t["entry_price"] is not None and t["last_price"] is not None:
                o["open_pnl"] += (t["last_price"] - t["entry_price"]) * t["qty"]
        for o in out.values():
            o["net_pnl"], o["open_pnl"] = round(o["net_pnl"], 2), round(o["open_pnl"], 2)
        return out

    # ------------------------------------------------------------------ helpers
    def _reset(self, capital):
        self.db.execute("DELETE FROM trades")
        self.db.execute("DELETE FROM account")
        self.db.execute("INSERT INTO account (id, starting_capital, cash, created_at) VALUES (1, ?, ?, ?)",
                        (capital, capital, _ts(dt.datetime.now(IST))))
        self.db.commit()

    def reset(self, capital=None):
        capital = float(capital or self.start_capital)
        if capital <= 0:
            raise PaperError("Capital must be positive")
        with self.lock:
            self._reset(capital)
        self.log(f"Paper account reset to {_amt(capital)}")

    def charges(self, buy_value, sell_value):
        """Approximate charges for one round trip (buy + sell) of an option."""
        brokerage = 2 * self.brokerage
        exchange = (buy_value + sell_value) * self.exchange_pct / 100
        stt = sell_value * self.stt_sell_pct / 100
        stamp = buy_value * self.stamp_buy_pct / 100
        gst = (brokerage + exchange) * self.gst_pct / 100
        return round(brokerage + exchange + stt + stamp + gst, 2)

    def _cash(self):
        return self.db.execute("SELECT cash FROM account WHERE id=1").fetchone()["cash"]

    def _add_cash(self, delta):
        self.db.execute("UPDATE account SET cash = cash + ? WHERE id=1", (delta,))

    def market_open(self, now):
        if self.ignore_market_hours:
            return True
        return now.weekday() < 5 and dt.time(9, 15) <= now.time() <= dt.time(15, 30)

    def has_active(self):
        with self.lock:
            return bool(self.db.execute(
                f"SELECT 1 FROM trades WHERE status IN {ACTIVE} LIMIT 1").fetchone())

    def active_symbols(self):
        with self.lock:
            return [r["symbol"] for r in self.db.execute(
                f"SELECT DISTINCT symbol FROM trades WHERE status IN {ACTIVE}")]

    # ------------------------------------------------------------------ orders
    def place(self, report, lots, order_type="market", exit_at="T1", now=None, source="manual",
              slippage_pct=0.0, size_note=None, max_lots=50):
        now = now or dt.datetime.now(IST)
        trade, strat = report.get("trade") or {}, report.get("strategy") or {}
        if not trade.get("action", "").startswith("BUY") or not trade.get("option") or not trade.get("index"):
            raise PaperError("No trade signal right now for this strategy - nothing to paper trade.")
        if order_type not in ("market", "trigger"):
            raise PaperError("order_type must be 'market' or 'trigger'")
        if exit_at not in ("T1", "T2"):
            raise PaperError("exit_at must be 'T1' or 'T2'")
        lots = int(lots)
        if not 1 <= lots <= max_lots:
            raise PaperError(f"lots must be between 1 and {max_lots}")
        if not self.ignore_market_hours and (not self.market_open(now) or now.time() >= self.square_off):
            raise PaperError("Paper trades can be placed only during market hours (9:15 to 15:15 IST).")

        opt, idx = trade["option"], trade["index"]
        qty = lots * int(opt["lot_size"])
        spot = report["underlying"]["ltp"]
        price = opt.get("ask") or opt.get("ltp")
        if not price or price <= 0:
            raise PaperError("Option price not available")
        slippage_pct = float(slippage_pct or 0)
        price = _tick(price * (1 + slippage_pct / 100))

        row = {
            "trade_date": now.date().isoformat(), "strategy": strat.get("id"), "strategy_name": strat.get("name"),
            "symbol": opt["symbol"], "option_type": opt["option_type"], "strike": opt["strike"],
            "expiry_date": opt.get("expiry_date"), "lots": lots, "lot_size": opt["lot_size"], "qty": qty,
            "order_type": order_type, "exit_at": exit_at, "created_at": _ts(now),
            "trigger_index": idx["entry"], "sl_index": idx["stoploss"],
            "t1_index": idx["target1"], "t2_index": idx["target2"],
            "last_price": opt.get("ltp"), "last_index": spot, "updated_at": _ts(now),
            "source": source, "slippage_pct": slippage_pct, "size_note": size_note,
        }
        with self.lock:
            if order_type == "market":
                cost = price * qty
                if cost + self.charges(cost, cost) > self._cash():
                    raise PaperError(f"Not enough virtual cash: need about {_amt(cost)}, have {_amt(self._cash())}")
                row.update(status="OPEN", entry_time=_ts(now), entry_price=price, entry_index=spot)
                self._add_cash(-cost)
            else:
                row.update(status="PENDING")
            cols = ", ".join(row)
            cur = self.db.execute(f"INSERT INTO trades ({cols}) VALUES ({', '.join('?' * len(row))})",
                                  list(row.values()))
            self.db.commit()
            t = self.get(cur.lastrowid)
        tgt = t["t1_index"] if exit_at == "T1" else t["t2_index"]
        who = "AUTO" if source == "auto" else "Manual"
        if t["status"] == "OPEN":
            self.log(f"{who} ENTRY #{t['id']}: {trade['action']} {t['symbol']} · {lots} lots ({qty} qty) "
                     f"@ ₹{_inr(price)} = {_amt(price * qty)} · Nifty {_inr(spot)} · "
                     f"SL {_inr(t['sl_index'])} / target {_inr(tgt)}" + (f" · {size_note}" if size_note else ""),
                     "entry", t["strategy"], now)
        else:
            self.log(f"{who} ORDER #{t['id']}: {trade['action']} {t['symbol']} · {lots} lots when Nifty reaches "
                     f"{_inr(t['trigger_index'])}", "order", t["strategy"], now)
        return t

    def get(self, trade_id):
        r = self.db.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if not r:
            raise PaperError(f"Trade {trade_id} not found")
        return self._view(r)

    def _close(self, r, price, index, reason, now, apply_slippage=True):
        slip = (r["slippage_pct"] or 0) if "slippage_pct" in r.keys() else 0
        if apply_slippage and slip:
            price = _tick(price * (1 - slip / 100))
        buy_value = r["entry_price"] * r["qty"]
        sell_value = price * r["qty"]
        charges = self.charges(buy_value, sell_value)
        gross = sell_value - buy_value
        self.db.execute(
            "UPDATE trades SET status='CLOSED', exit_time=?, exit_price=?, exit_index=?, exit_reason=?, "
            "charges=?, gross_pnl=?, net_pnl=?, last_price=?, last_index=?, updated_at=? WHERE id=?",
            (_ts(now), price, index, reason, charges, round(gross, 2), round(gross - charges, 2),
             price, index, _ts(now), r["id"]))
        self._add_cash(sell_value - charges)
        net = gross - charges
        self.log(f"EXIT #{r['id']}: {r['symbol']} · {r['qty']} qty @ ₹{_inr(price)} · {reason} · "
                 f"Nifty {_inr(index) if index else '-'} · net P&L {'+' if net >= 0 else '-'}{_amt(abs(net))} "
                 f"(charges {_amt(charges)})", "win" if net > 0 else "loss", r["strategy"], now)

    def close(self, trade_id, price=None, index=None, reason="Closed manually", now=None):
        now = now or dt.datetime.now(IST)
        with self.lock:
            r = self.db.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
            if not r:
                raise PaperError(f"Trade {trade_id} not found")
            if r["status"] == "PENDING":
                self.db.execute("UPDATE trades SET status='CANCELLED', exit_reason=?, exit_time=?, updated_at=? "
                                "WHERE id=?", ("Cancelled manually", _ts(now), _ts(now), trade_id))
            elif r["status"] == "OPEN":
                self._close(r, price if price is not None else r["last_price"],
                            index if index is not None else r["last_index"], reason, now,
                            apply_slippage=price is not None)
            else:
                raise PaperError(f"Trade {trade_id} is already {r['status'].lower()}")
            self.db.commit()
            return self.get(trade_id)

    # ------------------------------------------------------------------ monitor
    def monitor(self, prices, index_ltp, now=None):
        """
        prices: {symbol: {"ltp":..., "bid":..., "ask":...}}; index_ltp: current Nifty.
        Fills pending orders, exits on stop-loss / target, squares off at the cut-off time.
        Returns a list of event strings.
        """
        now = now or dt.datetime.now(IST)
        today = now.date().isoformat()
        events = []
        with self.lock:
            rows = self.db.execute(f"SELECT * FROM trades WHERE status IN {ACTIVE}").fetchall()
            for r in rows:
                q = prices.get(r["symbol"]) or {}
                ltp = q.get("ltp") or r["last_price"]
                bid = q.get("bid") or ltp
                ask = q.get("ask") or ltp
                bull = r["option_type"] == "CE"
                stale = r["trade_date"] < today
                cutoff = (not self.ignore_market_hours) and now.time() >= self.square_off

                if r["status"] == "PENDING":
                    if stale or cutoff:
                        self.db.execute("UPDATE trades SET status='CANCELLED', exit_reason=?, exit_time=?, "
                                        "updated_at=? WHERE id=?",
                                        ("Expired - entry level not reached", _ts(now), _ts(now), r["id"]))
                        events.append(f"#{r['id']} cancelled (not triggered)")
                        self.log(f"CANCELLED #{r['id']}: entry level not reached", "info", r["strategy"], now)
                        continue
                    if index_ltp is None:
                        continue
                    hit = index_ltp >= r["trigger_index"] if bull else index_ltp <= r["trigger_index"]
                    if hit:
                        ask = _tick(ask * (1 + (r["slippage_pct"] or 0) / 100))
                        cost = ask * r["qty"]
                        if cost + self.charges(cost, cost) > self._cash():
                            self.db.execute("UPDATE trades SET status='CANCELLED', exit_reason=?, exit_time=?, "
                                            "updated_at=? WHERE id=?",
                                            ("Not enough virtual cash at trigger", _ts(now), _ts(now), r["id"]))
                            events.append(f"#{r['id']} cancelled (cash)")
                            continue
                        self.db.execute("UPDATE trades SET status='OPEN', entry_time=?, entry_price=?, "
                                        "entry_index=?, last_price=?, last_index=?, updated_at=? WHERE id=?",
                                        (_ts(now), ask, index_ltp, ltp, index_ltp, _ts(now), r["id"]))
                        self._add_cash(-cost)
                        events.append(f"#{r['id']} filled at {ask}")
                        self.log(f"FILLED #{r['id']}: {r['symbol']} · {r['qty']} qty @ ₹{_inr(ask)} "
                                 f"(Nifty reached {_inr(index_ltp)})", "entry", r["strategy"], now)
                    else:
                        self.db.execute("UPDATE trades SET last_price=?, last_index=?, updated_at=? WHERE id=?",
                                        (ltp, index_ltp, _ts(now), r["id"]))
                    continue

                # OPEN
                if stale:
                    self._close(r, r["last_price"], r["last_index"],
                                "Closed next session at last known price", now, apply_slippage=False)
                    events.append(f"#{r['id']} closed (stale)")
                    continue
                if cutoff:
                    self._close(r, bid, index_ltp or r["last_index"],
                                f"Auto square-off {self.square_off.strftime('%H:%M')}", now)
                    events.append(f"#{r['id']} squared off")
                    continue
                if index_ltp is None:
                    continue
                target = r["t1_index"] if r["exit_at"] == "T1" else r["t2_index"]
                sl_hit = index_ltp <= r["sl_index"] if bull else index_ltp >= r["sl_index"]
                tg_hit = index_ltp >= target if bull else index_ltp <= target
                if sl_hit:
                    self._close(r, bid, index_ltp, "Stop-loss hit", now)
                    events.append(f"#{r['id']} stop-loss")
                elif tg_hit:
                    self._close(r, bid, index_ltp, f"Target {r['exit_at'][1]} hit", now)
                    events.append(f"#{r['id']} target")
                else:
                    self.db.execute("UPDATE trades SET last_price=?, last_index=?, updated_at=? WHERE id=?",
                                    (ltp, index_ltp, _ts(now), r["id"]))
            self.db.commit()
        return events

    # ------------------------------------------------------------------ reporting
    @staticmethod
    def _view(r):
        d = dict(r)
        if d["status"] == "OPEN" and d["entry_price"] is not None and d["last_price"] is not None:
            d["unrealised_pnl"] = round((d["last_price"] - d["entry_price"]) * d["qty"], 2)
            d["unrealised_pct"] = round((d["last_price"] / d["entry_price"] - 1) * 100, 2) if d["entry_price"] else None
        return d

    def summary(self, history_limit=30):
        with self.lock:
            acc = dict(self.db.execute("SELECT * FROM account WHERE id=1").fetchone())
            active = [self._view(r) for r in self.db.execute(
                f"SELECT * FROM trades WHERE status IN {ACTIVE} ORDER BY id DESC")]
            history = [self._view(r) for r in self.db.execute(
                "SELECT * FROM trades WHERE status IN ('CLOSED','CANCELLED') ORDER BY id DESC LIMIT ?",
                (history_limit,))]
            closed = [dict(r) for r in self.db.execute("SELECT * FROM trades WHERE status='CLOSED'")]

        open_value = sum((t["last_price"] or 0) * t["qty"] for t in active if t["status"] == "OPEN")
        unreal = sum(t.get("unrealised_pnl") or 0 for t in active)
        realised = sum(t["net_pnl"] for t in closed)

        def stats(rows):
            wins = [t for t in rows if t["net_pnl"] > 0]
            n = len(rows)
            return {
                "trades": n, "wins": len(wins), "losses": n - len(wins),
                "win_rate": round(len(wins) / n * 100, 1) if n else None,
                "net_pnl": round(sum(t["net_pnl"] for t in rows), 2),
                "avg_pnl": round(sum(t["net_pnl"] for t in rows) / n, 2) if n else None,
                "best": max((t["net_pnl"] for t in rows), default=None),
                "worst": min((t["net_pnl"] for t in rows), default=None),
                "charges": round(sum(t["charges"] or 0 for t in rows), 2),
            }

        by_strategy = {}
        for t in closed:
            by_strategy.setdefault(t["strategy"], {"name": t["strategy_name"], "rows": []})["rows"].append(t)

        equity = acc["cash"] + open_value
        return {
            "account": {
                "starting_capital": acc["starting_capital"], "cash": round(acc["cash"], 2),
                "open_value": round(open_value, 2), "equity": round(equity, 2),
                "realised_pnl": round(realised, 2), "unrealised_pnl": round(unreal, 2),
                "total_return_pct": round((equity / acc["starting_capital"] - 1) * 100, 2),
                "since": acc["created_at"],
            },
            "active": active,
            "history": history,
            "stats": stats(closed),
            "by_strategy": [{"strategy": k, "name": v["name"], **stats(v["rows"])} for k, v in by_strategy.items()],
        }
