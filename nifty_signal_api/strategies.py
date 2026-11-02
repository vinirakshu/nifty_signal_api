"""
Selectable intraday strategies. Each strategy looks at the same market context and returns:
  direction : BULLISH / BEARISH / NEUTRAL
  reasons   : list of plain-English reasons
  overrides : optional entry / stop-loss / targets (otherwise the level-based plan is used)
"""
import datetime as dt

from analysis import r2


def _ctx_atr(ctx):
    return (ctx["intra"] or {}).get("atr") or max(ctx["spot"] * 0.001, 10)


# ------------------------------------------------------------------ 1. confluence
def confluence(ctx):
    b = ctx["bias"]
    return {"direction": b["direction"], "reasons": list(b["reasons"]),
            "status": f"Score {b['score']} of ±{b['max_score']} (needs ±{b['threshold']})"}


# ------------------------------------------------------------------ 2. opening range breakout
def orb(ctx):
    intra, spot, candles = ctx["intra"] or {}, ctx["spot"], ctx["intraday"]
    if len(candles) < 4:
        return {"direction": "NEUTRAL", "reasons": ["Opening range (9:15–9:30) is still forming."],
                "status": "Waiting for 9:30"}
    hi, lo = intra["opening_range_high"], intra["opening_range_low"]
    rng = hi - lo
    reasons = [f"Opening range 9:15–9:30: {r2(lo)} – {r2(hi)} ({r2(rng)} pts)"]
    if lo <= spot <= hi:
        reasons.append(f"Price is inside the range. CE above {r2(hi)}, PE below {r2(lo)}.")
        return {"direction": "NEUTRAL", "reasons": reasons, "status": "Waiting for breakout"}

    bull = spot > hi
    sign = 1 if bull else -1
    edge = hi if bull else lo
    t1, t2 = edge + sign * rng, edge + sign * 2 * rng
    if (spot - t1) * sign >= 0:
        reasons.append(f"Breakout already reached target 1 ({r2(t1)}) - too late to enter.")
        return {"direction": "NEUTRAL", "reasons": reasons, "status": "Breakout move done"}

    mid = (hi + lo) / 2
    reasons.append(f"Price broke {'above' if bull else 'below'} the opening range "
                   f"{'high' if bull else 'low'} ({r2(edge)}).")
    return {
        "direction": "BULLISH" if bull else "BEARISH", "reasons": reasons,
        "status": f"{'Upside' if bull else 'Downside'} breakout",
        "overrides": {
            "entry": spot, "entry_rule": f"Breakout {'above' if bull else 'below'} {r2(edge)} - enter now",
            "stoploss": mid, "stoploss_basis": "Middle of opening range",
            "target1": t1, "target1_basis": "1 x opening range",
            "target2": t2, "target2_basis": "2 x opening range",
        },
    }


# ------------------------------------------------------------------ 3. EMA + VWAP trend
def ema_vwap(ctx):
    intra, spot = ctx["intra"] or {}, ctx["spot"]
    e9, e21, avg = intra.get("ema9"), intra.get("ema21"), intra.get("avg_price")
    name = intra.get("avg_price_type", "VWAP")
    if not (e9 and e21 and avg):
        return {"direction": "NEUTRAL", "reasons": ["Not enough 5-minute candles yet (needs ~21)."],
                "status": "Warming up"}
    reasons = [f"EMA9 {e9} vs EMA21 {e21}", f"Price {r2(spot)} vs {name} {avg}"]
    if e9 > e21 and spot > avg and spot > e9:
        reasons.append("Fast EMA above slow EMA and price above both average lines - uptrend.")
        return {"direction": "BULLISH", "reasons": reasons, "status": "Uptrend"}
    if e9 < e21 and spot < avg and spot < e9:
        reasons.append("Fast EMA below slow EMA and price below both average lines - downtrend.")
        return {"direction": "BEARISH", "reasons": reasons, "status": "Downtrend"}
    reasons.append("EMAs and price disagree - no clean trend.")
    return {"direction": "NEUTRAL", "reasons": reasons, "status": "No clean trend"}


# ------------------------------------------------------------------ 4. OI wall bounce
def oi_bounce(ctx):
    oi, spot = ctx["oi"] or {}, ctx["spot"]
    pw = (oi.get("put_oi_walls") or [{}])[0].get("strike")
    cw = (oi.get("call_oi_walls") or [{}])[0].get("strike")
    if not pw or not cw:
        return {"direction": "NEUTRAL", "reasons": ["Option chain OI not available."], "status": "No OI data"}
    a = _ctx_atr(ctx)
    near = max(0.6 * a, spot * 0.0012)
    buf = max(0.5 * a, 10)
    mid = (pw + cw) / 2
    reasons = [f"Biggest put OI (support) at {pw}, biggest call OI (resistance) at {cw}"]

    if 0 <= spot - pw <= near:
        reasons.append(f"Price is just above the put wall ({r2(spot - pw)} pts) - expecting a bounce up.")
        return {"direction": "BULLISH", "reasons": reasons, "status": "Bounce off put wall",
                "overrides": {"entry": spot, "entry_rule": f"Near put wall {pw} - enter now",
                              "stoploss": pw - buf, "stoploss_basis": f"Below put wall {pw}",
                              "target1": mid, "target1_basis": "Midway between walls",
                              "target2": cw - buf, "target2_basis": f"Just below call wall {cw}"}}
    if 0 <= cw - spot <= near:
        reasons.append(f"Price is just below the call wall ({r2(cw - spot)} pts) - expecting a fall.")
        return {"direction": "BEARISH", "reasons": reasons, "status": "Rejection at call wall",
                "overrides": {"entry": spot, "entry_rule": f"Near call wall {cw} - enter now",
                              "stoploss": cw + buf, "stoploss_basis": f"Above call wall {cw}",
                              "target1": mid, "target1_basis": "Midway between walls",
                              "target2": pw + buf, "target2_basis": f"Just above put wall {pw}"}}
    if spot < pw or spot > cw:
        reasons.append("Price has broken through an OI wall - bounce setup is not valid.")
        return {"direction": "NEUTRAL", "reasons": reasons, "status": "Wall broken"}
    reasons.append(f"Price is between the walls. CE near {pw}, PE near {cw} (within {r2(near)} pts).")
    return {"direction": "NEUTRAL", "reasons": reasons, "status": "Mid-range"}


# ------------------------------------------------------------------ registry
STRATEGIES = {
    "confluence": {
        "fn": confluence, "name": "Confluence (default)", "style": "Trend",
        "summary": "Trades only when several indicators agree on the direction.",
        "rules": ["Score +1/-1 each: price vs day average, EMA 9 vs 21, price vs CPR, PCR",
                  "Score +2 or more: buy CE. -2 or less: buy PE",
                  "Stop-loss and targets at the nearest support / resistance levels"],
        "best_for": "Trending days. Fewer but cleaner signals.",
    },
    "orb": {
        "fn": orb, "name": "Opening Range Breakout", "style": "Breakout",
        "summary": "Uses the 9:15–9:30 high and low. Trades the break of that range.",
        "rules": ["Mark the high and low of the first 15 minutes",
                  "Break above the high: buy CE. Break below the low: buy PE",
                  "Stop-loss at the middle of the range; targets 1x and 2x the range"],
        "best_for": "Gap or news days with a strong first move. Fails on choppy days.",
    },
    "ema_vwap": {
        "fn": ema_vwap, "name": "EMA + VWAP Trend", "style": "Trend following",
        "summary": "Follows the trend when moving averages and day average line up.",
        "rules": ["EMA 9 above EMA 21 and price above both the day average and EMA 9: buy CE",
                  "EMA 9 below EMA 21 and price below both: buy PE",
                  "Stop-loss and targets at the nearest support / resistance levels"],
        "best_for": "Steady trending sessions. Gives late or false signals in sideways markets.",
    },
    "oi_bounce": {
        "fn": oi_bounce, "name": "OI Wall Bounce", "style": "Range / reversal",
        "summary": "Buys near the biggest put OI (support) and sells near the biggest call OI (resistance).",
        "rules": ["Price just above the biggest put OI strike: buy CE",
                  "Price just below the biggest call OI strike: buy PE",
                  "Stop-loss just beyond the wall; targets midway and at the opposite wall"],
        "best_for": "Range-bound days, especially near expiry. Fails when a wall breaks.",
    },
}


def strategy_list():
    return [{"id": k, **{f: v[f] for f in ("name", "style", "summary", "rules", "best_for")}}
            for k, v in STRATEGIES.items()]


def run(name, ctx):
    if name not in STRATEGIES:
        raise ValueError(f"strategy must be one of {list(STRATEGIES)}")
    out = STRATEGIES[name]["fn"](ctx)
    out.setdefault("overrides", None)
    out["id"], out["name"] = name, STRATEGIES[name]["name"]
    return out
