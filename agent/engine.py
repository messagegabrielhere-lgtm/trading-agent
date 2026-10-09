"""Portfolio simulation shared by the backtest and the paper-trading run."""
from .strategies import STRATEGIES


def new_portfolio(cash):
    return {"cash": float(cash), "positions": {}}


def equity(pf, px):
    return pf["cash"] + sum(q * px[s] for s, q in pf["positions"].items())


def rebalance(pf, targets, px, cfg):
    """Trade pf toward target weights at px with slippage. Returns the list of fills."""
    slip = cfg["slippage_bps"] / 10000
    eq = equity(pf, px)
    deltas = {}
    for s in set(pf["positions"]) | set(targets):
        tgt = targets.get(s, 0) * eq
        d = tgt - pf["positions"].get(s, 0) * px[s]
        # Skip small adjustments, but always close a position that left the targets.
        if abs(d) < cfg["min_trade_pct"] * eq and tgt > 0:
            continue
        if abs(d) > 1e-9:
            deltas[s] = d

    fills = []
    for s, d in sorted(deltas.items(), key=lambda kv: kv[1]):  # sells first, to fund buys
        held = pf["positions"].get(s, 0)
        if d < 0:
            qty = held if targets.get(s, 0) == 0 else min(held, -d / px[s])
            price = px[s] * (1 - slip)
            pf["cash"] += qty * price
            side = "sell"
        else:
            price = px[s] * (1 + slip)
            qty = min(d, pf["cash"]) / price
            pf["cash"] -= qty * price
            side = "buy"
        if qty <= 1e-9:
            continue
        left = held + (qty if side == "buy" else -qty)
        if left > 1e-9:
            pf["positions"][s] = left
        else:
            pf["positions"].pop(s, None)
        fills.append({
            "symbol": s, "side": side, "qty": round(qty, 6), "price": round(price, 4),
            "value": round(qty * price, 2), "target_weight": round(targets.get(s, 0), 4),
        })
    return fills


def step(pf, i, sig, px, last_idx, name, cfg):
    """Run one day. Returns (fills, reason, targets, rebalanced)."""
    eq = equity(pf, px)
    ctx = {
        "i": i, "sig": sig, "last_idx": last_idx, "universe": cfg["universe"],
        "params": cfg["strategies"][name], "all_params": cfg["strategies"],
        "weights": {s: q * px[s] / eq for s, q in pf["positions"].items()},
    }
    targets, reason, due = STRATEGIES[name](ctx)
    fills = rebalance(pf, targets, px, cfg) if due else []
    return fills, reason, targets, due


def warmup(cfg):
    """Days of history every strategy needs before its first decision (12-month lookbacks need 253)."""
    need = [253]
    for p in cfg["strategies"].values():
        need += [v for k, v in p.items() if k.endswith("_days") and k != "rebalance_days" and isinstance(v, int)]
    return max(need) + 1


def backtest(name, dates, adj, cfg):
    """Simulate a strategy over the full history. Returns the daily equity list."""
    pf, last_idx, curve = new_portfolio(cfg["starting_cash"]), None, []
    for i in range(warmup(cfg), len(dates)):
        px = {s: adj[s][i] for s in adj}
        _, _, _, did = step(pf, i, adj, px, last_idx, name, cfg)
        if did:
            last_idx = i
        curve.append(equity(pf, px))
    return curve


def stats(curve):
    """Summary statistics for a daily equity curve."""
    rets = [b / a - 1 for a, b in zip(curve, curve[1:])]
    mean = sum(rets) / len(rets)
    vol = (sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
    peak, max_dd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        max_dd = min(max_dd, v / peak - 1)
    year = [curve[i] / curve[i - 252] for i in range(252, len(curve))]
    return {
        "cagr": (curve[-1] / curve[0]) ** (252 / len(rets)) - 1,
        "vol": vol * 252 ** 0.5,
        "sharpe": mean / vol * 252 ** 0.5 if vol else 0,
        "max_drawdown": max_dd,
        "total_multiple": curve[-1] / curve[0],
        "best_1y_multiple": max(year) if year else None,
        "worst_1y_multiple": min(year) if year else None,
    }
