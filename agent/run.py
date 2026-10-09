"""Daily run: refresh the backtest, advance the paper portfolio, write dashboard data.

    python -m agent.run

Paper trading only. Nothing here connects to a brokerage or places an order.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from . import data, engine, strategies

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data"
STATE = OUT / "state.json"


def write(path, obj):
    path.write_text(json.dumps(obj, indent=1) + "\n")


def run_backtests(dates, adj, cfg):
    start = engine.warmup(cfg)
    bench = adj[cfg["benchmark"]][start:]
    curves = {name: engine.backtest(name, dates, adj, cfg) for name in cfg["strategies"]}
    curves[cfg["benchmark"]] = [cfg["starting_cash"] * p / bench[0] for p in bench]
    half = len(bench) // 2  # judge each half on its own, so one lucky stretch cannot carry a strategy

    d = dates[start:]
    keep = sorted(set(range(0, len(d), 5)) | {len(d) - 1})  # weekly points keep the file small
    return {
        "from": d[0], "to": d[-1],
        "benchmark": cfg["benchmark"],
        "dates": [d[k] for k in keep],
        "curves": {n: [round(c[k], 2) for k in keep] for n, c in curves.items()},
        "stats": {n: engine.stats(c) for n, c in curves.items()},
        "halves": {"split": d[half],
                   "first": {n: engine.stats(c[:half + 1]) for n, c in curves.items()},
                   "second": {n: engine.stats(c[half:]) for n, c in curves.items()}},
    }


def run_paper(dates, close, adj, cfg):
    name, bench = cfg["strategy"], cfg["benchmark"]
    if STATE.exists():
        st = json.loads(STATE.read_text())
        first = dates.index(st["last_date"]) + 1
        if "targets" not in st:  # older state files: take the targets from the last rebalance
            done = [d for d in st["decisions"] if d["action"] == "rebalance"]
            st["targets"] = done[-1]["targets"] if done else {}
    else:
        st = {
            "start_date": dates[-1], "start_cash": cfg["starting_cash"],
            "benchmark_start": close[bench][-1],
            "cash": float(cfg["starting_cash"]), "positions": {}, "last_rebalance": None,
            "history": [], "trades": [], "decisions": [], "tickets": None,
        }
        first = len(dates) - 1

    pf = {"cash": st["cash"], "positions": st["positions"]}
    for i in range(first, len(dates)):
        px = {s: close[s][i] for s in close}
        last = st["last_rebalance"]
        last_idx = dates.index(last) if last in dates else None
        fills, reason, targets, did = engine.step(pf, i, adj, px, last_idx, name, cfg)
        eq = engine.equity(pf, px)
        if did:
            st["last_rebalance"] = dates[i]
            st["targets"] = {s: round(w, 4) for s, w in targets.items()}  # what the broker step mirrors
            for f in fills:
                st["trades"].append({"date": dates[i], **f})
            if fills:
                st["tickets"] = {
                    "date": dates[i],
                    "items": [{
                        "symbol": f["symbol"], "side": f["side"],
                        "target_weight": f["target_weight"],
                        "paper_qty": f["qty"], "reference_close": round(px[f["symbol"]], 2),
                    } for f in fills],
                }
        st["decisions"].append({
            "date": dates[i], "action": "rebalance" if fills else "hold", "reason": reason,
            "targets": {s: round(w, 4) for s, w in targets.items()},
        })
        st["history"].append({
            "date": dates[i], "equity": round(eq, 2),
            "benchmark": round(st["start_cash"] * px[bench] / st["benchmark_start"], 2),
        })
        st["last_date"] = dates[i]

    px = {s: close[s][-1] for s in close}
    eq = engine.equity(pf, px)
    st.update({
        "cash": round(pf["cash"], 2),
        "positions": {s: round(q, 6) for s, q in pf["positions"].items()},
        "holdings": [{
            "symbol": s, "qty": round(q, 4), "price": round(px[s], 2),
            "value": round(q * px[s], 2), "weight": round(q * px[s] / eq, 4),
        } for s, q in sorted(pf["positions"].items())],
        "decisions": st["decisions"][-250:],
        "strategy": name, "benchmark": bench,
        "prices": {s: round(close[s][-1], 4) for s in close},  # last closes, for whole-share orders
        "goal_multiple": cfg["goal_multiple"], "goal_days": cfg["goal_days"],
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return st


def main():
    cfg = json.loads((ROOT / "config.json").read_text())
    dates, close, adj = data.load(sorted(strategies.symbols_of(cfg)))

    OUT.mkdir(parents=True, exist_ok=True)
    write(OUT / "backtest.json", run_backtests(dates, adj, cfg))
    st = run_paper(dates, close, adj, cfg)
    write(STATE, st)
    print(f"{st['last_date']}: paper equity {st['history'][-1]['equity']:.2f}, "
          f"{st['decisions'][-1]['action']} - {st['decisions'][-1]['reason']}")


if __name__ == "__main__":
    main()
