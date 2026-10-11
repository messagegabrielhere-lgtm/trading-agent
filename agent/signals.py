"""Price/volume signal detection and validation for the research desk's scan (pure functions, unit tested).

The scan runs as a pipeline where each stage is cheaper than the next and most names stop early:
  1. collect   a year of daily bars per ticker (one HTTP call, no AI)
  2. detect    what changed on the latest bar: a breakout, a breakdown, a trend turn, a volume spike
  3. validate  arithmetic first: has this exact signal paid on this ticker over the past year
               (event study: forward return after every earlier time it fired), is the stock liquid,
               is it not already extended. Only survivors reach Claude (agent/analyst.py).
  4. update    research.py records what it surfaced and later scores it, so the desk knows its own
               hit rate instead of guessing.

A bar is (date, close, high, low, volume), oldest first.
"""

DEFAULTS = {
    "horizon": 5,             # trading days a signal is meant to play out over
    "min_events": 5,          # earlier occurrences needed before a backtest counts
    "min_hit_rate": 0.55,     # share of earlier occurrences that moved the right way
    "min_dollar_volume": 20e6,
    "max_day_move": 0.08,     # don't chase anything already up (or down) this much today
    "stop_atr": 1.5,          # stop distance in ATRs; the target is twice that (2:1 reward to risk)
}

BULLISH = {"breakout", "trend_turn_up", "volume_surge_up"}
BEARISH = {"breakdown", "trend_turn_down", "volume_surge_down"}


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def sma(values, n, end=None):
    end = len(values) if end is None else end
    return _mean(values[end - n:end]) if end >= n else None


def atr(bars, n=14, end=None):
    end = len(bars) if end is None else end
    if end < n + 1:
        return None
    trs = []
    for i in range(end - n, end):
        _, c, h, l, _ = bars[i]
        prev = bars[i - 1][1]
        trs.append(max(h - l, abs(h - prev), abs(l - prev)))
    return _mean(trs)


def rsi(closes, n=14, end=None):
    end = len(closes) if end is None else end
    if end < n + 1:
        return None
    gains = losses = 0.0
    for i in range(end - n, end):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def events_at(bars, i):
    """Signals that fire on bar i (needs 51 bars of history before it)."""
    if i < 51:
        return []
    closes = [b[1] for b in bars]
    vols = [b[4] for b in bars]
    c, prev = closes[i], closes[i - 1]
    avg_vol = _mean(vols[i - 20:i])
    hi20 = max(b[2] for b in bars[i - 20:i])
    lo20 = min(b[3] for b in bars[i - 20:i])
    s50, s50_prev = sma(closes, 50, i + 1), sma(closes, 50, i)
    out = []
    if c > hi20 and vols[i] >= 1.5 * avg_vol:
        out.append("breakout")
    if c < lo20 and vols[i] >= 1.5 * avg_vol:
        out.append("breakdown")
    if prev <= s50_prev and c > s50:
        out.append("trend_turn_up")
    if prev >= s50_prev and c < s50:
        out.append("trend_turn_down")
    if vols[i] >= 2.5 * avg_vol:
        out.append("volume_surge_up" if c > prev else "volume_surge_down")
    return out


def backtest(bars, kind, horizon):
    """Every earlier time `kind` fired: how often price went the signal's way over `horizon` days."""
    sign = 1 if kind in BULLISH else -1
    rets = []
    for i in range(51, len(bars) - horizon - 1):  # the last bars have no full forward window yet
        if kind in events_at(bars, i):
            rets.append(sign * (bars[i + horizon][1] / bars[i][1] - 1))
    if not rets:
        return {"n": 0, "hit_rate": 0.0, "avg": 0.0}
    return {"n": len(rets), "hit_rate": round(sum(r > 0 for r in rets) / len(rets), 3),
            "avg": round(_mean(rets), 4)}


def snapshot(bars):
    """Features of the latest bar, for the plan and for Claude."""
    closes = [b[1] for b in bars]
    c = closes[-1]
    return {
        "close": c,
        "day_move": round(c / closes[-2] - 1, 4),
        "ret_5d": round(c / closes[-6] - 1, 4),
        "ret_20d": round(c / closes[-21] - 1, 4),
        "sma20": round(sma(closes, 20), 4),
        "sma50": round(sma(closes, 50), 4),
        "rsi14": round(rsi(closes), 1),
        "atr14": round(atr(bars), 4),
        "dollar_volume": round(_mean([b[1] * b[4] for b in bars[-20:]])),
    }


def scan(bars, cfg=None, spy_ret_20d=None):
    """Candidates on the latest bar that survive the arithmetic. Each comes with a trade plan.

    Returns [{"kind", "direction", "backtest", "features", "plan"}] plus rejected kinds with reasons,
    as (survivors, rejected)."""
    c = {**DEFAULTS, **(cfg or {})}
    if len(bars) < 80:
        return [], [("all", f"only {len(bars)} bars of history")]
    f = snapshot(bars)
    survivors, rejected = [], []
    for kind in events_at(bars, len(bars) - 1):
        bull = kind in BULLISH
        bt = backtest(bars, kind, c["horizon"])
        why = None
        if f["dollar_volume"] < c["min_dollar_volume"]:
            why = f"thin: ${f['dollar_volume']:,.0f}/day"
        elif abs(f["day_move"]) > c["max_day_move"]:
            why = f"already moved {f['day_move']:+.1%} today"
        elif bt["n"] < c["min_events"]:
            why = f"only {bt['n']} earlier {kind} signals to learn from"
        elif bt["hit_rate"] < c["min_hit_rate"] or bt["avg"] <= 0:
            why = f"history says no: {bt['hit_rate']:.0%} hit, {bt['avg']:+.1%} avg over {bt['n']}"
        elif spy_ret_20d is not None and bull and f["ret_20d"] < spy_ret_20d:
            why = "weaker than the S&P 500 over 20 days"
        elif spy_ret_20d is not None and not bull and f["ret_20d"] > spy_ret_20d:
            why = "stronger than the S&P 500 over 20 days"
        if why:
            rejected.append((kind, why))
            continue
        risk = c["stop_atr"] * f["atr14"]
        stop = f["close"] - risk if bull else f["close"] + risk
        target = f["close"] + 2 * risk if bull else f["close"] - 2 * risk
        survivors.append({
            "kind": kind, "direction": "long" if bull else "put", "backtest": bt, "features": f,
            "plan": {"entry": round(f["close"], 2), "stop": round(stop, 2), "target": round(target, 2),
                     "reward_risk": 2.0, "horizon_days": c["horizon"]},
        })
    return survivors, rejected


def score(sig):
    """Rank survivors: historical edge first, weighted by how often it fired."""
    bt = sig["backtest"]
    return bt["avg"] * bt["hit_rate"] * min(bt["n"], 20) ** 0.5
