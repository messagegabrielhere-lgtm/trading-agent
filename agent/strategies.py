"""Strategies. Each takes a context dict and returns (target_weights, reason, rebalance_now).

Context keys:
  i         index of the current day
  sig       symbol -> list of adjusted closes (use sig[s][:i + 1] only)
  weights   symbol -> current portfolio weight
  last_idx  index of the last rebalance, or None
  universe  list of tradable symbols
  params    this strategy's block from config.json

Weights that sum to less than 1 leave the remainder in cash.
"""


def momentum_rotation(ctx):
    i, sig, p = ctx["i"], ctx["sig"], ctx["params"]
    look, sma_n = p["lookback_days"], p["trend_sma_days"]
    if i < max(look, sma_n):
        return {}, "Warming up: not enough history yet.", False

    scores = {}
    for s in ctx["universe"]:
        px = sig[s]
        ret = px[i] / px[i - look] - 1
        sma = sum(px[i - sma_n + 1 : i + 1]) / sma_n
        if ret > 0 and px[i] > sma:
            scores[s] = ret
    picks = sorted(scores, key=scores.get, reverse=True)[: p["top_n"]]
    targets = {s: 1 / p["top_n"] for s in picks}

    if picks:
        ranked = ", ".join(f"{s} {scores[s]:+.1%}" for s in picks)
        reason = f"Top {look}-day momentum above {sma_n}-day average: {ranked}."
        if len(picks) < p["top_n"]:
            reason += f" Only {len(picks)} of {p['top_n']} slots qualify; the rest stays in cash."
    else:
        reason = "No symbol has positive momentum above its trend average; all cash."

    last = ctx["last_idx"]
    due = last is None or i - last >= p["rebalance_days"]
    if not due:
        reason += f" Next rebalance in {p['rebalance_days'] - (i - last)} trading days."
    return targets, reason, due


def target_weights(ctx):
    p = ctx["params"]
    targets = dict(p["weights"])
    if ctx["last_idx"] is None:
        return targets, "Initial allocation to target weights.", True
    drift = max(abs(ctx["weights"].get(s, 0) - w) for s, w in targets.items())
    due = drift > p["drift_threshold"]
    reason = f"Largest drift from target is {drift:.1%} (threshold {p['drift_threshold']:.0%})."
    return targets, reason, due


STRATEGIES = {"momentum_rotation": momentum_rotation, "target_weights": target_weights}


# --- Growth candidates --------------------------------------------------------------------------
# Each is a well-known published rule, used with its usual parameters rather than tuned to this
# data, so the backtest is less likely to flatter it.

def _ret(px, i, n):
    return px[i] / px[i - n] - 1


def _vol(px, i, n):
    r = [px[k] / px[k - 1] - 1 for k in range(i - n + 1, i + 1)]
    m = sum(r) / n
    return (sum((x - m) ** 2 for x in r) / (n - 1)) ** 0.5 * 252 ** 0.5


def _monthly_due(ctx, days):
    last = ctx["last_idx"]
    return last is None or ctx["i"] - last >= days


def dual_momentum(ctx):
    """Antonacci's Global Equities Momentum: the stronger of US and international stocks over 12
    months, but only if it beat T-bills; otherwise bonds."""
    i, sig, p = ctx["i"], ctx["sig"], ctx["params"]
    look = p["lookback_days"]
    best = max(p["risk"], key=lambda s: _ret(sig[s], i, look))
    if _ret(sig[best], i, look) > _ret(sig[p["cash"]], i, look):
        t, why = {best: 1.0}, f"{best} has the best 12-month return ({_ret(sig[best], i, look):+.1%}) and beats T-bills."
    else:
        t, why = {p["safe"]: 1.0}, f"Stocks trail T-bills over 12 months; holding {p['safe']}."
    return t, why, _monthly_due(ctx, p["rebalance_days"])


def vol_target_momentum(ctx):
    """Top momentum ETFs above trend, weighted by inverse volatility, scaled so the portfolio aims
    for a fixed volatility. Holds more in calm markets and less when markets get violent."""
    i, sig, p = ctx["i"], ctx["sig"], ctx["params"]
    look, sma_n = p["lookback_days"], p["trend_sma_days"]
    ok = {s: _ret(sig[s], i, look) for s in p["symbols"]
          if _ret(sig[s], i, look) > 0 and sig[s][i] > sum(sig[s][i - sma_n + 1:i + 1]) / sma_n}
    picks = sorted(ok, key=ok.get, reverse=True)[:p["top_n"]]
    if not picks:
        return {}, "Nothing has positive momentum above trend; all cash.", _monthly_due(ctx, p["rebalance_days"])
    inv = {s: 1 / max(_vol(sig[s], i, 63), 1e-6) for s in picks}
    tot = sum(inv.values())
    w = {s: v / tot for s, v in inv.items()}
    port_vol = sum(w[s] * _vol(sig[s], i, 63) for s in picks)  # conservative: ignores diversification
    scale = min(p["max_gross"], p["target_vol"] / port_vol)
    t = {s: w[s] * scale for s in picks}
    return t, (f"Top momentum: {', '.join(picks)}; sized for {p['target_vol']:.0%} volatility "
               f"({sum(t.values()):.0%} invested)."), _monthly_due(ctx, p["rebalance_days"])


def leveraged_trend(ctx):
    """Hold a 2x Nasdaq-100 fund while the Nasdaq-100 is above its 200-day average (with a small
    band to avoid flip-flopping); move to Treasuries when it falls below. Checked daily."""
    i, sig, p = ctx["i"], ctx["sig"], ctx["params"]
    px = sig[p["signal"]]
    sma = sum(px[i - p["sma_days"] + 1:i + 1]) / p["sma_days"]
    holding_risk = ctx["weights"].get(p["risk_on"], 0) > 0.05
    if ctx["last_idx"] is None:
        on = px[i] > sma
    else:
        on = px[i] > sma * (1 - p["band"]) if holding_risk else px[i] > sma * (1 + p["band"])
    t = {p["risk_on"]: 1.0} if on else {p["risk_off"]: 1.0}
    due = ctx["last_idx"] is None or (on != holding_risk)
    why = (f"{p['signal']} {px[i]:.2f} vs 200-day average {sma:.2f}: "
           f"{'risk on, hold ' + p['risk_on'] if on else 'risk off, hold ' + p['risk_off']}.")
    return t, why, due


def canary_momentum(ctx):
    """Keller's Defensive Asset Allocation: two "canary" assets (emerging markets and bonds) act as
    an early warning. Both healthy: top offensive ETFs. One weak: half defensive. Both weak: all in
    the best defensive bond fund."""
    i, sig, p = ctx["i"], ctx["sig"], ctx["params"]

    def m(s):  # 13612W momentum: recent months weigh more
        x = sig[s]
        return 12 * _ret(x, i, 21) + 4 * _ret(x, i, 63) + 2 * _ret(x, i, 126) + _ret(x, i, 252)

    bad = sum(m(s) <= 0 for s in p["canary"])
    off_share = {0: 1.0, 1: 0.5}.get(bad, 0.0)
    t = {}
    if off_share:
        top = sorted(p["offensive"], key=m, reverse=True)[:p["top_n"]]
        for s in top:
            t[s] = t.get(s, 0) + off_share / len(top)
    if off_share < 1:
        d = max(p["defensive"], key=m)
        t[d] = t.get(d, 0) + 1 - off_share
    why = f"{bad} of {len(p['canary'])} canaries weak; {off_share:.0%} offensive: " + \
          ", ".join(f"{s} {w:.0%}" for s, w in sorted(t.items(), key=lambda kv: -kv[1]))
    return t, why, _monthly_due(ctx, p["rebalance_days"])


def blend(ctx):
    """Equal mix of other strategies, rebalanced weekly. Different rules fail at different times."""
    p, t, why = ctx["params"], {}, []
    for name in p["members"]:
        sub = dict(ctx, params=ctx["all_params"][name])
        mt, _, _ = STRATEGIES[name](sub)
        for s, w in mt.items():
            t[s] = t.get(s, 0) + w / len(p["members"])
        why.append(name.replace("_", " "))
    return t, "Blend of " + ", ".join(why) + ".", _monthly_due(ctx, p["rebalance_days"])


STRATEGIES.update({
    "dual_momentum": dual_momentum,
    "vol_target_momentum": vol_target_momentum,
    "leveraged_trend": leveraged_trend,
    "canary_momentum": canary_momentum,
    "blend": blend,
})


def symbols_of(cfg):
    """Every symbol any configured strategy can hold or reads."""
    out = set(cfg["universe"]) | {cfg["benchmark"]}
    for p in cfg["strategies"].values():
        for k, v in p.items():
            if k == "weights":
                out |= set(v)
            elif isinstance(v, list) and v and isinstance(v[0], str) and k != "members":
                out |= set(v)
            elif k in ("signal", "risk_on", "risk_off", "safe", "cash"):
                out.add(v)
    return out
