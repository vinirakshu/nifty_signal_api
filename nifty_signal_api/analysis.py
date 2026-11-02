"""
Rule-based Nifty analysis. Pure functions, no API calls, so it is easy to test.

Levels   : classic pivots + CPR from the previous day, previous day high/low,
           today's high/low, opening range, and option-chain OI walls.
Bias     : score from price vs VWAP/TWAP, EMA 9/21 (5-min), price vs CPR, and PCR.
Trade    : breakout entry, stop-loss at the nearest support/resistance (ATR-capped),
           targets at the next levels, option premium estimated with delta.

These are mechanical levels, not predictions.
"""
import datetime as dt
import math
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def r2(x):
    return None if x is None else round(float(x), 2)


def tick(x, size=0.05):
    """Round an option premium to the 0.05 tick, never below one tick."""
    return round(max(size, round(x / size) * size), 2)


# ------------------------------------------------------------------ candles
def candle_date(c):
    return dt.datetime.fromtimestamp(c[0], IST).date()


def previous_day(daily_candles, today):
    past = [c for c in daily_candles if candle_date(c) < today]
    if not past:
        return None
    c = past[-1]
    return {"date": candle_date(c).isoformat(), "open": c[1], "high": c[2], "low": c[3], "close": c[4]}


def pivots(high, low, close):
    p = (high + low + close) / 3
    bc = (high + low) / 2
    tc = 2 * p - bc
    tc, bc = max(tc, bc), min(tc, bc)
    return {
        "P": r2(p),
        "R1": r2(2 * p - low), "R2": r2(p + (high - low)), "R3": r2(high + 2 * (p - low)),
        "S1": r2(2 * p - high), "S2": r2(p - (high - low)), "S3": r2(low - 2 * (high - p)),
        "CPR_TC": r2(tc), "CPR_BC": r2(bc), "CPR_width": r2(tc - bc),
    }


def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def atr(candles, period=14):
    if len(candles) < 2:
        return None
    trs = []
    for prev, cur in zip(candles, candles[1:]):
        h, l, pc = cur[2], cur[3], prev[4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    trs = trs[-period:]
    return sum(trs) / len(trs)


def average_price(candles):
    """VWAP if the symbol has volume, otherwise TWAP (index symbols carry no volume)."""
    if not candles:
        return None, None
    vol = sum(c[5] for c in candles)
    if vol > 0:
        return sum(((c[2] + c[3] + c[4]) / 3) * c[5] for c in candles) / vol, "VWAP"
    return sum((c[2] + c[3] + c[4]) / 3 for c in candles) / len(candles), "TWAP"


def intraday_stats(candles, opening_range_candles=3):
    if not candles:
        return {}
    closes = [c[4] for c in candles]
    avg, avg_type = average_price(candles)
    orc = candles[:opening_range_candles]
    return {
        "day_high": r2(max(c[2] for c in candles)),
        "day_low": r2(min(c[3] for c in candles)),
        "opening_range_high": r2(max(c[2] for c in orc)),
        "opening_range_low": r2(min(c[3] for c in orc)),
        "avg_price": r2(avg), "avg_price_type": avg_type,
        "ema9": r2(ema(closes, 9)), "ema21": r2(ema(closes, 21)),
        "atr": r2(atr(candles)),
        "candles": len(candles),
    }


# ------------------------------------------------------------------ option chain
def split_chain(chain):
    """Returns (underlying_row, options list, strike_step)."""
    rows = chain.get("optionsChain", []) or []
    underlying = next((r for r in rows if r.get("strike_price", -1) == -1 and not r.get("option_type")), None)
    options = [r for r in rows if r.get("option_type") in ("CE", "PE")]
    strikes = sorted({o["strike_price"] for o in options})
    diffs = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
    step = min(diffs) if diffs else 50
    return underlying, options, step


def oi_analysis(chain, spot):
    _, options, step = split_chain(chain)
    ce = {o["strike_price"]: o for o in options if o["option_type"] == "CE"}
    pe = {o["strike_price"]: o for o in options if o["option_type"] == "PE"}
    if not ce or not pe:
        return {}

    call_oi = chain.get("callOi") or sum(o.get("oi", 0) for o in ce.values())
    put_oi = chain.get("putOi") or sum(o.get("oi", 0) for o in pe.values())

    def top(book, side_filter, n=2):
        rows = [o for k, o in book.items() if side_filter(k)] or list(book.values())
        rows.sort(key=lambda o: o.get("oi", 0), reverse=True)
        return [{"strike": o["strike_price"], "oi": o.get("oi", 0), "oi_change": o.get("oich", 0)} for o in rows[:n]]

    strikes = sorted(set(ce) | set(pe))

    def pain(k):
        return sum(ce[s].get("oi", 0) * max(0, k - s) for s in ce) + \
               sum(pe[s].get("oi", 0) * max(0, s - k) for s in pe)
    max_pain = min(strikes, key=pain)

    return {
        "pcr": r2(put_oi / call_oi) if call_oi else None,
        "total_call_oi": call_oi, "total_put_oi": put_oi,
        "call_oi_walls": top(ce, lambda k: k >= spot),   # resistance
        "put_oi_walls": top(pe, lambda k: k <= spot),    # support
        "max_pain": max_pain,
        "strike_step": step,
        "note": "OI walls and max pain use only the strikes fetched (OPTION_STRIKE_COUNT each side of ATM).",
    }


# ------------------------------------------------------------------ levels
def build_levels(spot, piv, prev, intra, oi, cluster_pts):
    cands = []

    def add(v, src):
        if v is not None:
            cands.append((float(v), src))

    if piv:
        for k in ("R3", "R2", "R1", "P", "S1", "S2", "S3", "CPR_TC", "CPR_BC"):
            add(piv[k], k)
    if prev:
        add(prev["high"], "PDH")
        add(prev["low"], "PDL")
    if intra:
        add(intra.get("day_high"), "Day high")
        add(intra.get("day_low"), "Day low")
        add(intra.get("opening_range_high"), "ORB high")
        add(intra.get("opening_range_low"), "ORB low")
    for w in (oi or {}).get("call_oi_walls", []):
        add(w["strike"], f"Call OI wall {w['strike']}")
    for w in (oi or {}).get("put_oi_walls", []):
        add(w["strike"], f"Put OI wall {w['strike']}")

    cands.sort()
    clusters = []
    for v, s in cands:
        if clusters and v - clusters[-1]["_max"] <= cluster_pts:
            c = clusters[-1]
            c["_vals"].append(v)
            c["_max"] = v
            c["sources"].append(s)
        else:
            clusters.append({"_vals": [v], "_max": v, "sources": [s]})

    levels = []
    for c in clusters:
        lvl = sum(c["_vals"]) / len(c["_vals"])
        levels.append({"level": r2(lvl), "sources": sorted(set(c["sources"])), "strength": len(c["sources"])})

    resistance = sorted([l for l in levels if l["level"] > spot], key=lambda l: l["level"])
    support = sorted([l for l in levels if l["level"] < spot], key=lambda l: -l["level"])
    return support, resistance


# ------------------------------------------------------------------ bias
def market_bias(spot, piv, intra, oi, threshold, pcr_bull, pcr_bear):
    score, reasons = 0, []

    avg = intra.get("avg_price") if intra else None
    if avg:
        name = intra.get("avg_price_type", "VWAP")
        if spot > avg:
            score += 1; reasons.append(f"Price above {name} ({avg})")
        elif spot < avg:
            score -= 1; reasons.append(f"Price below {name} ({avg})")

    e9, e21 = (intra or {}).get("ema9"), (intra or {}).get("ema21")
    if e9 and e21:
        if e9 > e21:
            score += 1; reasons.append(f"EMA9 {e9} above EMA21 {e21} (uptrend)")
        elif e9 < e21:
            score -= 1; reasons.append(f"EMA9 {e9} below EMA21 {e21} (downtrend)")

    if piv:
        if spot > piv["CPR_TC"]:
            score += 1; reasons.append(f"Price above CPR top ({piv['CPR_TC']})")
        elif spot < piv["CPR_BC"]:
            score -= 1; reasons.append(f"Price below CPR bottom ({piv['CPR_BC']})")
        else:
            reasons.append("Price inside CPR (sideways zone)")

    pcr = (oi or {}).get("pcr")
    if pcr is not None:
        if pcr >= pcr_bull:
            score += 1; reasons.append(f"PCR {pcr} - heavy put writing (bullish)")
        elif pcr <= pcr_bear:
            score -= 1; reasons.append(f"PCR {pcr} - heavy call writing (bearish)")
        else:
            reasons.append(f"PCR {pcr} - neutral")

    direction = "BULLISH" if score >= threshold else "BEARISH" if score <= -threshold else "NEUTRAL"
    return {"direction": direction, "score": score, "max_score": 4, "threshold": threshold, "reasons": reasons}


# ------------------------------------------------------------------ trade plan
MONEYNESS_SHIFT = {"ITM2": -2, "ITM1": -1, "ATM": 0, "OTM1": 1, "OTM2": 2}
DEFAULT_DELTA = {"ITM2": 0.7, "ITM1": 0.6, "ATM": 0.5, "OTM1": 0.4, "OTM2": 0.3}


def pick_option(chain, spot, option_type, moneyness="ATM"):
    _, options, step = split_chain(chain)
    book = {o["strike_price"]: o for o in options if o["option_type"] == option_type}
    if not book:
        return None, None
    atm = min(book, key=lambda k: abs(k - spot))
    shift = MONEYNESS_SHIFT.get(moneyness, 0)
    # CE: ITM = lower strike. PE: ITM = higher strike.
    want = atm + shift * step if option_type == "CE" else atm - shift * step
    strike = min(book, key=lambda k: abs(k - want))
    return book[strike], atm


def expiry_info(chain, expiry_ts=None):
    exp = chain.get("expiryData", []) or []
    if not exp:
        return None
    if expiry_ts:
        for e in exp:
            if str(e.get("expiry")) == str(expiry_ts):
                return e
    return exp[0]


def trade_plan(spot, direction, signal_reason, support, resistance, intra, chain, moneyness,
               min_rr, lot_size, expiry_ts=None, overrides=None, neutral_reason=None):
    a = (intra or {}).get("atr") or max(spot * 0.001, 10)   # fallback ~0.1%

    if direction == "NEUTRAL":
        return {"action": "NO TRADE", "reason": neutral_reason or "No clear setup - wait.",
                "index": None, "option": None}

    bull = direction == "BULLISH"
    option_type = "CE" if bull else "PE"
    sign = 1 if bull else -1

    if overrides:
        entry = overrides["entry"]
        entry_rule = overrides.get("entry_rule") or f"Enter {'above' if bull else 'below'} {r2(entry)}"
        sl, sl_src = overrides["stoploss"], overrides.get("stoploss_basis", "strategy")
        t1, t1_src = overrides["target1"], overrides.get("target1_basis", "strategy")
        t2, t2_src = overrides["target2"], overrides.get("target2_basis", "strategy")
    else:
        # entry: break of recent 3-candle high (bull) / low (bear)
        recent = (intra or {}).get("_recent") or []
        if recent:
            trigger = max(c[2] for c in recent) if bull else min(c[3] for c in recent)
            entry = max(spot, trigger) if bull else min(spot, trigger)
        else:
            entry = spot
        entry_rule = f"Enter when Nifty trades {'above' if bull else 'below'} {r2(entry)}"

        ahead = resistance if bull else support          # targets
        behind = support if bull else resistance         # stop-loss side

        # stop-loss: nearest level behind entry at least 0.5 ATR away, capped at 3 ATR
        sl, sl_src = None, None
        for l in behind:
            d = (entry - l["level"]) * sign
            if d >= 0.5 * a:
                if d <= 3 * a:
                    sl, sl_src = l["level"], ", ".join(l["sources"])
                break
        if sl is None:
            sl, sl_src = entry - sign * 1.5 * a, "1.5 x ATR"

        # targets: next levels ahead at least 0.5 ATR beyond entry
        tgts = [l for l in ahead if (l["level"] - entry) * sign >= 0.5 * a]
        t1, t1_src = (tgts[0]["level"], ", ".join(tgts[0]["sources"])) if tgts else (entry + sign * 2 * a, "2 x ATR")
        if len(tgts) > 1:
            t2, t2_src = tgts[1]["level"], ", ".join(tgts[1]["sources"])
        else:
            t2, t2_src = t1 + sign * 1.5 * a, "T1 + 1.5 x ATR"

    risk = abs(entry - sl)
    rr1 = abs(t1 - entry) / risk if risk else None
    rr2 = abs(t2 - entry) / risk if risk else None

    index = {
        "entry": r2(entry), "entry_rule": entry_rule,
        "stoploss": r2(sl), "stoploss_basis": sl_src,
        "target1": r2(t1), "target1_basis": t1_src,
        "target2": r2(t2), "target2_basis": t2_src,
        "risk_points": r2(risk), "risk_reward_t1": r2(rr1), "risk_reward_t2": r2(rr2),
        "atr": r2(a),
    }

    opt, atm = pick_option(chain, spot, option_type, moneyness)
    option = None
    if opt:
        greeks = opt.get("greeks") or {}
        d = abs(greeks.get("delta") or 0)
        delta_src = "fyers"
        if not 0.05 <= d <= 0.99:
            d, delta_src = DEFAULT_DELTA.get(moneyness, 0.5), "assumed"
        ltp = opt.get("ltp", 0)

        def prem(index_level):
            move = (index_level - spot) * (1 if option_type == "CE" else -1)
            return tick(ltp + d * move)

        exp = expiry_info(chain, expiry_ts)
        p_entry = prem(entry)
        option = {
            "symbol": opt.get("symbol"),
            "fyToken": opt.get("fyToken"),
            "underlying": "NIFTY",
            "option_type": option_type,
            "strike": opt.get("strike_price"),
            "atm_strike": atm,
            "moneyness": moneyness,
            "expiry_date": exp.get("date") if exp else None,
            "expiry_epoch": exp.get("expiry") if exp else None,
            "lot_size": lot_size,
            "ltp": opt.get("ltp"), "bid": opt.get("bid"), "ask": opt.get("ask"),
            "change": opt.get("ltpch"), "change_pct": opt.get("ltpchp"),
            "volume": opt.get("volume"), "oi": opt.get("oi"), "oi_change": opt.get("oich"),
            "greeks": greeks or None, "delta_used": round(d, 2), "delta_source": delta_src,
            "est_entry_premium": p_entry,
            "est_stoploss_premium": prem(sl),
            "est_target1_premium": prem(t1),
            "est_target2_premium": prem(t2),
            "est_capital_per_lot": round(p_entry * lot_size, 2),
            "est_risk_per_lot": round(max(0.0, p_entry - prem(sl)) * lot_size, 2),
            "premium_note": "Estimated from delta only; ignores gamma, theta (time decay) and IV changes.",
        }

    action, reason = f"BUY {option_type}", signal_reason
    if rr1 is not None and rr1 < min_rr:
        action, reason = "NO TRADE", f"{signal_reason}, but risk-reward to T1 is {r2(rr1)} (< {min_rr})"

    return {"action": action, "reason": reason, "index": index, "option": option}


# ------------------------------------------------------------------ full report
def analyse(spot_quote, daily, intraday, chain, today, settings, moneyness="ATM", expiry_ts=None,
            strategy="confluence", now=None):
    import strategies  # local import avoids a circular import
    underlying, _, _ = split_chain(chain)
    spot = spot_quote.get("lp") or (underlying or {}).get("ltp")
    if intraday and not spot:
        spot = intraday[-1][4]
    if not spot:
        raise ValueError("No Nifty price available")

    prev = previous_day(daily, today)
    piv = pivots(prev["high"], prev["low"], prev["close"]) if prev else None
    intra = intraday_stats(intraday)
    oi = oi_analysis(chain, spot)
    support, resistance = build_levels(spot, piv, prev, intra, oi, settings["cluster_pts"])
    bias = market_bias(spot, piv, intra, oi, settings["bias_threshold"],
                       settings["pcr_bull"], settings["pcr_bear"])

    ctx = {"spot": spot, "piv": piv, "prev": prev, "intra": intra, "oi": oi, "bias": bias,
           "support": support, "resistance": resistance, "intraday": intraday,
           "now": now or dt.datetime.now(IST), "settings": settings}
    strat = strategies.run(strategy, ctx)

    intra_for_plan = dict(intra)
    intra_for_plan["_recent"] = intraday[-4:-1] if len(intraday) >= 4 else intraday[-3:]
    sig_reason = f"{strat['name']}: {strat['direction'].lower()} - {strat['status']}"
    plan = trade_plan(spot, strat["direction"], sig_reason, support, resistance, intra_for_plan,
                      chain, moneyness, settings["min_rr"], settings["lot_size"], expiry_ts,
                      overrides=strat["overrides"],
                      neutral_reason=f"{strat['name']}: {strat['status']}. " + (strat["reasons"][-1] if strat["reasons"] else ""))

    vix = chain.get("indiavixData") or {}
    return {
        "underlying": {
            "symbol": spot_quote.get("symbol") or "NSE:NIFTY50-INDEX",
            "ltp": r2(spot),
            "change": r2(spot_quote.get("ch") if spot_quote.get("ch") is not None else (underlying or {}).get("ltpch")),
            "change_pct": r2(spot_quote.get("chp") if spot_quote.get("chp") is not None else (underlying or {}).get("ltpchp")),
            "open": r2(spot_quote.get("open_price")), "high": r2(spot_quote.get("high_price")),
            "low": r2(spot_quote.get("low_price")), "prev_close": r2(spot_quote.get("prev_close_price")),
            "india_vix": r2(vix.get("ltp")),
        },
        "intraday": {k: v for k, v in intra.items() if not k.startswith("_")},
        "previous_day": prev,
        "pivots": piv,
        "option_chain_summary": oi,
        "levels": {
            "immediate_support": support[0] if support else None,
            "immediate_resistance": resistance[0] if resistance else None,
            "support": support[:3],
            "resistance": resistance[:3],
        },
        "bias": bias,
        "strategy": {"id": strat["id"], "name": strat["name"], "direction": strat["direction"],
                     "status": strat["status"], "reasons": strat["reasons"]},
        "trade": plan,
    }
