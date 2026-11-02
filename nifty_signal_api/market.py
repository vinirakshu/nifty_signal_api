"""
Market analytics for the Nifty 50 board and option activity. Pure functions (easy to test).

Stocks - buy side / sell side uses three live signals:
  1. price change today        (up = buyers won so far)
  2. LTP vs VWAP (avg price)   (above = buyers paying up, below = sellers in control)
  3. pending order book        (total buy qty vs total sell qty waiting in the order book)

Options - buildup from price change and open-interest change (standard F&O reading):
  price up   + OI up   -> Long buildup     (fresh buying)
  price down + OI up   -> Short buildup    (fresh selling / writing)
  price up   + OI down -> Short covering   (sellers exiting -> buying)
  price down + OI down -> Long unwinding   (buyers exiting -> selling)
  plus the aggressor: last trade at the ask (buyers hitting offers) or at the bid (sellers hitting bids).
"""
import json
import os

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "nifty50.json")


def load_constituents(path=DATA_FILE):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _r(x, d=2):
    return None if x is None else round(float(x), d)


# ------------------------------------------------------------------ stocks
def stock_side(chp, ltp, vwap, buy_qty, sell_qty):
    """Returns (label, score, reasons). score: -3 (strong selling) .. +3 (strong buying)."""
    score, reasons = 0, []
    if chp is not None:
        if chp > 0.05:
            score += 1; reasons.append("up today")
        elif chp < -0.05:
            score -= 1; reasons.append("down today")
    if ltp and vwap:
        if ltp > vwap * 1.0005:
            score += 1; reasons.append("above VWAP")
        elif ltp < vwap * 0.9995:
            score -= 1; reasons.append("below VWAP")
    if buy_qty and sell_qty:
        ratio = buy_qty / sell_qty
        if ratio >= 1.2:
            score += 1; reasons.append(f"buy orders {ratio:.1f}x sell orders")
        elif ratio <= 1 / 1.2:
            score -= 1; reasons.append(f"sell orders {1 / ratio:.1f}x buy orders")
    label = {3: "Strong buying", 2: "Buying", 1: "Mild buying", 0: "Neutral",
             -1: "Mild selling", -2: "Selling", -3: "Strong selling"}[score]
    return label, score, reasons


def nifty50_board(constituents, ticks, index_tick=None, top_n=5):
    """
    constituents: data from nifty50.json; ticks: {symbol: tick dict from the feed}.
    Returns the full board with live weights, index contribution, side, movers and breadth.
    """
    idx_prev = (index_tick or {}).get("prev_close_price")
    rows = []
    for c in constituents["stocks"]:
        t = ticks.get(c["symbol"]) or {}
        ltp = t.get("ltp")
        chp = t.get("chp")
        prev = t.get("prev_close_price")
        if chp is None and ltp and prev:
            chp = (ltp - prev) / prev * 100
        vwap = t.get("avg_trade_price")
        vol = t.get("vol_traded_today")
        bq, sq = t.get("tot_buy_qty"), t.get("tot_sell_qty")
        label, score, why = stock_side(chp, ltp, vwap, bq, sq)
        rows.append({
            "symbol": c["symbol"], "ticker": c["ticker"], "name": c["name"], "sector": c["sector"],
            "base_weight": c["weight"],
            "ltp": _r(ltp), "change": _r(t.get("ch") if t.get("ch") is not None else
                                         (ltp - prev if ltp and prev else None)),
            "change_pct": _r(chp), "open": _r(t.get("open_price")), "high": _r(t.get("high_price")),
            "low": _r(t.get("low_price")), "prev_close": _r(prev), "vwap": _r(vwap),
            "volume": vol, "turnover": _r(vol * vwap if vol and vwap else (vol * ltp if vol and ltp else None), 0),
            "bid": _r(t.get("bid_price")), "ask": _r(t.get("ask_price")),
            "total_buy_qty": bq, "total_sell_qty": sq,
            "buy_pct": _r(bq / (bq + sq) * 100, 1) if bq and sq else None,
            "side": label, "side_score": score, "side_reasons": why,
            "has_data": ltp is not None, "updated": t.get("_ts"),
        })

    # live weights: base weight scaled by today's move, re-normalised to 100
    scaled = [r["base_weight"] * (1 + (r["change_pct"] or 0) / 100) for r in rows]
    tot = sum(scaled) or 1
    for r, s in zip(rows, scaled):
        r["live_weight"] = _r(s / tot * 100, 3)
        r["index_points"] = _r(idx_prev * r["base_weight"] / 100 * (r["change_pct"] or 0) / 100) \
            if idx_prev else None

    live = [r for r in rows if r["has_data"]]
    adv = sum(1 for r in live if (r["change_pct"] or 0) > 0)
    dec = sum(1 for r in live if (r["change_pct"] or 0) < 0)
    by_chg = sorted(live, key=lambda r: r["change_pct"] or 0)
    by_pts = sorted([r for r in live if r["index_points"] is not None], key=lambda r: r["index_points"])
    slim = lambda r: {k: r[k] for k in ("symbol", "ticker", "name", "ltp", "change_pct", "index_points",
                                         "base_weight", "side")}
    buy_side = sum(1 for r in live if r["side_score"] > 0)
    sell_side = sum(1 for r in live if r["side_score"] < 0)
    return {
        "stocks": rows,
        "breadth": {"advances": adv, "declines": dec, "unchanged": len(live) - adv - dec,
                    "with_data": len(live), "buy_side": buy_side, "sell_side": sell_side,
                    "weighted_buy_pct": _r(sum(r["base_weight"] for r in live if r["side_score"] > 0) /
                                           (sum(r["base_weight"] for r in live) or 1) * 100, 1)},
        "gainers": [slim(r) for r in reversed(by_chg[-top_n:]) if (r["change_pct"] or 0) > 0],
        "losers": [slim(r) for r in by_chg[:top_n] if (r["change_pct"] or 0) < 0],
        "top_contributors": [slim(r) for r in reversed(by_pts[-top_n:]) if r["index_points"] > 0],
        "top_draggers": [slim(r) for r in by_pts[:top_n] if r["index_points"] < 0],
        "points_explained": _r(sum(r["index_points"] or 0 for r in live)),
        "weights_as_of": constituents.get("weights_as_of"),
    }


# ------------------------------------------------------------------ options
def option_buildup(price_change, oi_change):
    if price_change is None or oi_change is None or (price_change == 0 and oi_change == 0):
        return "No clear buildup", "neutral"
    if price_change >= 0 and oi_change > 0:
        return "Long buildup", "buy"
    if price_change < 0 and oi_change > 0:
        return "Short buildup", "sell"
    if price_change > 0 and oi_change <= 0:
        return "Short covering", "buy"
    return "Long unwinding", "sell"


def aggressor(ltp, bid, ask):
    if not ltp or not bid or not ask or ask <= bid:
        return "—"
    pos = (ltp - bid) / (ask - bid)
    if pos >= 0.8:
        return "At ask (buyers)"
    if pos <= 0.2:
        return "At bid (sellers)"
    return "Mid"


def options_activity(chain, top_n=10):
    rows = chain.get("optionsChain", []) or []
    spot = next((r.get("ltp") for r in rows if r.get("strike_price", -1) == -1 and not r.get("option_type")), None)
    opts = []
    for o in rows:
        if o.get("option_type") not in ("CE", "PE"):
            continue
        label, side = option_buildup(o.get("ltpch"), o.get("oich"))
        ltp, vol = o.get("ltp"), o.get("volume") or 0
        opts.append({
            "symbol": o.get("symbol"), "strike": o.get("strike_price"), "type": o["option_type"],
            "ltp": ltp, "change": o.get("ltpch"), "change_pct": o.get("ltpchp"),
            "volume": vol, "oi": o.get("oi"), "oi_change": o.get("oich"), "oi_change_pct": o.get("oichp"),
            "bid": o.get("bid"), "ask": o.get("ask"),
            "turnover": _r(vol * ltp, 0) if ltp else None,
            "buildup": label, "side": side, "aggressor": aggressor(ltp, o.get("bid"), o.get("ask")),
            "iv": (o.get("greeks") or {}).get("iv"),
            "moneyness": None if spot is None else (
                "ATM" if abs(o["strike_price"] - spot) <= 25 else
                ("ITM" if (o["option_type"] == "CE") == (o["strike_price"] < spot) else "OTM")),
        })
    calls = [o for o in opts if o["type"] == "CE"]
    puts = [o for o in opts if o["type"] == "PE"]
    by = lambda lst, k, rev=True: sorted([o for o in lst if o[k] is not None], key=lambda o: o[k], reverse=rev)
    buildups = {}
    for o in opts:
        b = buildups.setdefault(f"{o['type']} {o['buildup']}", {"count": 0, "volume": 0})
        b["count"] += 1
        b["volume"] += o["volume"]
    return {
        "spot": spot,
        "most_active": by(opts, "volume")[:top_n],
        "most_active_calls": by(calls, "volume")[:top_n],
        "most_active_puts": by(puts, "volume")[:top_n],
        "top_oi_addition": by(opts, "oi_change")[:top_n],
        "top_oi_reduction": by(opts, "oi_change", rev=False)[:top_n],
        "call_volume": sum(o["volume"] for o in calls), "put_volume": sum(o["volume"] for o in puts),
        "volume_pcr": _r(sum(o["volume"] for o in puts) / (sum(o["volume"] for o in calls) or 1)),
        "buildup_summary": buildups,
        "total_options": len(opts),
    }


# ------------------------------------------------------------------ depth
def parse_depth(symbol, resp_data):
    """Normalise a Fyers depth response ({symbol: {...}}) into bids / asks lists."""
    d = (resp_data or {}).get(symbol) or next(iter((resp_data or {}).values()), {}) or {}
    norm = lambda lvls: [{"price": l.get("price"), "qty": l.get("volume", l.get("qty")),
                          "orders": l.get("ord", l.get("orders"))} for l in (lvls or [])]
    bids, asks = norm(d.get("bids")), norm(d.get("ask") or d.get("asks"))
    tb, ts = d.get("totalbuyqty"), d.get("totalsellqty")
    return {
        "symbol": symbol, "ltp": d.get("ltp"), "change": d.get("ch"), "change_pct": d.get("chp"),
        "open": d.get("o"), "high": d.get("h"), "low": d.get("l"), "prev_close": d.get("c"),
        "volume": d.get("v"), "vwap": d.get("atp"), "oi": d.get("oi"),
        "last_qty": d.get("ltq"), "lower_circuit": d.get("lower_ckt"), "upper_circuit": d.get("upper_ckt"),
        "total_buy_qty": tb, "total_sell_qty": ts,
        "buy_pct": _r(tb / (tb + ts) * 100, 1) if tb and ts else None,
        "bids": bids, "asks": asks,
        "spread": _r(asks[0]["price"] - bids[0]["price"]) if bids and asks and asks[0]["price"] and bids[0]["price"] else None,
    }
