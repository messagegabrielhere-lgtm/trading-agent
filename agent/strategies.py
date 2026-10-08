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
