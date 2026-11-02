"""Fake Fyers model that returns data in the documented Fyers v3 response formats.

Prices can be moved with set_spot() (tests) or drift randomly on every quote call (walk=True, demo).
"""
import datetime as dt
import hashlib
import math
import random
import threading
import time

from fyers_client import IST, today_ist

PREFIX = "NSE:NIFTY26O28"   # fake weekly-style option symbol prefix
INDEX = "NSE:NIFTY50-INDEX"


def _epoch(d, h, m):
    return int(dt.datetime(d.year, d.month, d.day, h, m, tzinfo=IST).timestamp())


class FakeFyers:
    """trend: 'up', 'down' or 'flat'."""

    def __init__(self, trend="up", spot_open=25000.0, fail=None, walk=False, seed=1):
        self.trend = trend
        self.spot_open = spot_open
        self.fail = fail
        self.walk = walk
        self.rng = random.Random(seed)
        self.calls = []
        self._intraday = self._make_intraday()
        self._stocks = {}

    # ---- price helpers
    def _make_intraday(self):
        t = today_ist()
        drift = {"up": 4.0, "down": -4.0, "flat": 0.0}[self.trend]
        candles, price = [], self.spot_open
        for i in range(40):  # 40 x 5 min = 09:15 .. 12:30
            mins = 15 + i * 5
            o = price
            wobble = 6 * math.sin(i / 2.0) if self.trend == "flat" else 3 * math.sin(i)
            c = o + drift + wobble
            h, l = max(o, c) + 5, min(o, c) - 5
            candles.append([_epoch(t, 9 + mins // 60, mins % 60), round(o, 2), round(h, 2),
                            round(l, 2), round(c, 2), 0])   # index has no volume
            price = c
        return candles

    @property
    def spot(self):
        return self._intraday[-1][4]

    def set_spot(self, value):
        c = self._intraday[-1]
        c[4] = round(value, 2)
        c[2] = max(c[2], c[4])
        c[3] = min(c[3], c[4])

    def _step(self):
        if self.walk:
            drift = {"up": 0.6, "down": -0.6, "flat": 0.0}[self.trend]
            self.set_spot(self.spot + self.rng.gauss(drift, 4))
            for st in self._stocks.values():
                st["ltp"] = round(st["ltp"] * (1 + self.rng.gauss(drift / 25000, 0.0006)), 2)
                st["vol"] += self.rng.randint(500, 20000)

    # ---- fake Nifty 50 stocks
    def stock(self, sym):
        st = self._stocks.get(sym)
        if st is None:
            h = int(hashlib.md5(sym.encode()).hexdigest(), 16)
            prev = 100 + h % 4900
            bias = {"up": 0.6, "down": -0.6, "flat": 0.0}[self.trend]
            chp = bias + ((h >> 8) % 400 - 200) / 100          # -2% .. +2% around the trend
            ltp = round(prev * (1 + chp / 100), 2)
            vwap = round((prev + ltp) / 2 + ((h >> 16) % 10 - 5) * prev / 10000, 2)
            buy = 50000 + (h >> 20) % 400000
            sell = 50000 + (h >> 30) % 400000
            st = {"prev": float(prev), "ltp": ltp, "open": float(prev), "vwap": vwap,
                  "vol": 100000 + (h >> 40) % 9000000, "buy": buy, "sell": sell}
            self._stocks[sym] = st
        return st

    def stock_tick(self, sym):
        """Tick in the Fyers WebSocket SymbolUpdate format."""
        if sym == INDEX:
            s = self.spot
            return {"symbol": sym, "ltp": s, "prev_close_price": 24950.0, "ch": round(s - 24950, 2),
                    "chp": round((s - 24950) / 249.5, 2), "high_price": max(c[2] for c in self._intraday),
                    "low_price": min(c[3] for c in self._intraday), "open_price": self.spot_open, "type": "if"}
        if sym == "NSE:INDIAVIX-INDEX":
            return {"symbol": sym, "ltp": 12.4, "prev_close_price": 12.6, "ch": -0.2, "chp": -1.59, "type": "if"}
        if sym.startswith(PREFIX):
            strike, typ = float(sym[len(PREFIX):-2]), sym[-2:]
            p = self.option_price(strike, typ)
            return {"symbol": sym, "ltp": p, "bid_price": round(p - 0.5, 2), "ask_price": round(p + 0.5, 2),
                    "type": "sf"}
        st = self.stock(sym)
        ch = round(st["ltp"] - st["prev"], 2)
        return {"symbol": sym, "ltp": st["ltp"], "prev_close_price": st["prev"], "ch": ch,
                "chp": round(ch / st["prev"] * 100, 2), "open_price": st["open"],
                "high_price": max(st["ltp"], st["open"]) * 1.004, "low_price": min(st["ltp"], st["open"]) * 0.996,
                "avg_trade_price": st["vwap"], "vol_traded_today": st["vol"],
                "tot_buy_qty": st["buy"], "tot_sell_qty": st["sell"],
                "bid_price": round(st["ltp"] - 0.05, 2), "ask_price": round(st["ltp"] + 0.05, 2), "type": "sf"}

    def depth(self, data):
        self.calls.append(("depth", data))
        sym = data["symbol"]
        t = self.stock_tick(sym)
        ltp = t["ltp"]
        tick = 0.05
        bids = [{"price": round(ltp - tick * (i + 1), 2), "volume": 100 * (i + 3) + int(ltp) % 97, "ord": i + 2}
                for i in range(5)]
        asks = [{"price": round(ltp + tick * (i + 1), 2), "volume": 100 * (i + 2) + int(ltp) % 89, "ord": i + 1}
                for i in range(5)]
        return {"s": "ok", "code": 200, "d": {sym: {
            "totalbuyqty": t.get("tot_buy_qty", sum(b["volume"] for b in bids) * 40),
            "totalsellqty": t.get("tot_sell_qty", sum(a["volume"] for a in asks) * 40),
            "bids": bids, "ask": asks, "o": t.get("open_price"), "h": t.get("high_price"),
            "l": t.get("low_price"), "c": t.get("prev_close_price"), "chp": t.get("chp"), "ch": t.get("ch"),
            "ltq": 25, "ltp": ltp, "v": t.get("vol_traded_today"), "atp": t.get("avg_trade_price"),
            "lower_ckt": round(ltp * 0.9, 2), "upper_ckt": round(ltp * 1.1, 2)}}}

    def option_price(self, strike, typ, spot=None):
        s = self.spot if spot is None else spot
        dist = strike - s
        tv = max(5.0, 120 * math.exp(-abs(dist) / 250))
        intrinsic = max(0, s - strike) if typ == "CE" else max(0, strike - s)
        return round(intrinsic + tv, 2)

    def _err(self, name):
        if self.fail == name:
            return {"s": "error", "code": -16, "message": "Could not authenticate the user"}
        return None

    # ---- Fyers API surface
    def quotes(self, data):
        self.calls.append(("quotes", data))
        if e := self._err("quotes"):
            return e
        self._step()
        s = self.spot
        out = []
        for sym in data["symbols"].split(","):
            if sym == INDEX:
                v = {"ch": round(s - 24950, 2), "chp": round((s - 24950) / 249.5, 2), "lp": s,
                     "open_price": self.spot_open, "high_price": max(c[2] for c in self._intraday),
                     "low_price": min(c[3] for c in self._intraday), "prev_close_price": 24950.0,
                     "symbol": sym, "short_name": "NIFTY50-INDEX", "exchange": "NSE"}
            elif sym.startswith(PREFIX):
                strike, typ = float(sym[len(PREFIX):-2]), sym[-2:]
                p = self.option_price(strike, typ)
                v = {"lp": p, "bid": round(p - 0.5, 2), "ask": round(p + 0.5, 2), "symbol": sym}
            elif sym.endswith("-EQ") or sym == "NSE:INDIAVIX-INDEX":
                t = self.stock_tick(sym)
                v = {"lp": t["ltp"], "ch": t["ch"], "chp": t["chp"], "prev_close_price": t["prev_close_price"],
                     "open_price": t.get("open_price"), "high_price": t.get("high_price"),
                     "low_price": t.get("low_price"), "volume": t.get("vol_traded_today"),
                     "atp": t.get("avg_trade_price"), "bid": t.get("bid_price"), "ask": t.get("ask_price"),
                     "symbol": sym}
            else:
                out.append({"n": sym, "s": "error", "v": {"errmsg": "invalid symbol"}})
                continue
            out.append({"n": sym, "s": "ok", "v": v})
        return {"s": "ok", "code": 200, "d": out}

    def history(self, data):
        self.calls.append(("history", data))
        if e := self._err("history"):
            return e
        if data["resolution"] in ("D", "1D"):
            t = today_ist()
            out = []
            for k in range(10, 0, -1):
                d = t - dt.timedelta(days=k)
                if d.weekday() >= 5:
                    continue
                out.append([_epoch(d, 9, 15), 24900.0, 25040.0, 24880.0, 24950.0, 0])
            out.append([_epoch(t, 9, 15), self.spot_open, 25100.0, 24950.0, self.spot, 0])  # today partial
            return {"s": "ok", "code": 200, "candles": out}
        return {"s": "ok", "code": 200, "candles": [list(c) for c in self._intraday]}

    def optionchain(self, data):
        self.calls.append(("optionchain", data))
        if e := self._err("optionchain"):
            return e
        s = self.spot
        atm = round(s / 50) * 50
        n = int(data["strikecount"])
        exp_ts = data.get("timestamp") or "1761645600"
        rows = [{"symbol": INDEX, "ltp": s, "ltpch": round(s - 24950, 2),
                 "ltpchp": 0.3, "option_type": "", "strike_price": -1, "fyToken": "101000000026000"}]
        call_oi_total = put_oi_total = 0
        for k in range(-n, n + 1):
            strike = atm + 50 * k
            dist = strike - s
            # OI walls: bullish -> puts heavy below; bearish -> calls heavy above
            ce_oi = int(400000 * math.exp(-abs(strike - (atm + 300)) / 200)) + 50000
            pe_oi = int(400000 * math.exp(-abs(strike - (atm - 200)) / 200)) + 50000
            if self.trend == "up":
                pe_oi = int(pe_oi * 1.6)
            elif self.trend == "down":
                ce_oi = int(ce_oi * 1.6)
            call_oi_total += ce_oi
            put_oi_total += pe_oi
            for typ, oi, delta in (("CE", ce_oi, 0.5 - dist / 800), ("PE", pe_oi, -(0.5 + dist / 800))):
                delta = max(-0.99, min(0.99, delta))
                prem = self.option_price(strike, typ)
                rows.append({
                    "symbol": f"{PREFIX}{int(strike)}{typ}", "fyToken": f"1011{int(strike)}{typ}",
                    "strike_price": strike, "option_type": typ, "ltp": prem,
                    "bid": round(prem - 0.5, 2), "ask": round(prem + 0.5, 2), "ltpch": 1.0, "ltpchp": 1.0,
                    "oi": oi, "oich": oi // 10, "oichp": 10.0, "prev_oi": oi - oi // 10, "volume": oi * 3,
                    "greeks": {"delta": round(delta, 2), "gamma": 0.001, "theta": -12.0, "vega": 9.0, "iv": 12.5},
                })
        return {"s": "ok", "code": 200, "message": "", "data": {
            "callOi": call_oi_total, "putOi": put_oi_total,
            "expiryData": [{"date": "28-10-2026", "expiry": exp_ts}, {"date": "04-11-2026", "expiry": "1762250400"}],
            "indiavixData": {"symbol": "NSE:INDIAVIX-INDEX", "ltp": 12.4, "ltpch": -0.2, "ltpchp": -1.6},
            "optionsChain": rows}}


class FakeSocket:
    """Stands in for FyersDataSocket: pushes SymbolUpdate-style ticks for subscribed symbols."""

    def __init__(self, feed, fake, interval=1.0):
        self.feed, self.fake, self.interval = feed, fake, interval
        self.subs = set()
        self.running = False

    def connect(self):
        self.running = True
        self.feed._on_open()
        threading.Thread(target=self._pump, daemon=True).start()

    def subscribe(self, symbols, data_type="SymbolUpdate"):
        self.subs.update(symbols)
        self.push_once()

    def keep_running(self):
        pass

    def close_connection(self):
        self.running = False

    def push_once(self):
        for sym in list(self.subs):
            try:
                self.feed.on_tick(self.fake.stock_tick(sym))
            except Exception:  # noqa: BLE001
                pass

    def _pump(self):
        while self.running:
            time.sleep(self.interval)
            self.fake._step()
            self.push_once()
