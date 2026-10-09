"""Mirror the strategy's target weights into an Alpaca *paper* account.

    python -m agent.broker

Runs during market hours. Reads the latest targets from docs/data/state.json, compares them with
the paper account's positions, and places market orders to close the gap. Writes the account
snapshot and every order to docs/data/broker.json for the dashboard.

Safety rails:
  * Paper only. The endpoint is fixed to Alpaca's paper API; there is no setting that points it at
    a live account. (Tests may point it at localhost and nowhere else.)
  * Needs ALPACA_KEY_ID and ALPACA_SECRET_KEY in the environment. Without them it records
    "not connected" and exits cleanly.
  * Skips when the market is closed, when execution is switched off in config.json, or when the
    account is blocked from trading.
  * Cancels leftover open orders, then works out every trade from the account's live positions,
    so rerunning it never stacks duplicate orders: a second run finds the account already in line.
  * Buys only with settled cash (never margin) and only symbols in the configured universe.
  * Drawdown halt: if account equity falls a set percentage below its high-water mark, it closes
    every position and stops trading until you clear "halted" in docs/data/broker.json.
"""
import json
import os
import sys
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
STATE = DATA / "state.json"
OUT = DATA / "broker.json"

PAPER_URL = "https://paper-api.alpaca.markets"
DEFAULTS = {
    "enabled": True,
    "drift_threshold": 0.03,     # trade a symbol only when it is this far (share of equity) off target
    "max_drawdown_halt": 0.20,   # liquidate and stop if equity falls 20% below its peak
    "allocation": 1.0,           # share of account equity the strategy may use
    "min_order_usd": 5.0,
    "fill_wait_seconds": 90,
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def base_url():
    """The paper endpoint, or a localhost override for tests. Anything else is refused."""
    override = os.environ.get("ALPACA_BASE_URL")
    if not override:
        return PAPER_URL
    host = urlparse(override).hostname
    if override.rstrip("/") == PAPER_URL or host in ("localhost", "127.0.0.1"):
        return override.rstrip("/")
    sys.exit(f"Refusing ALPACA_BASE_URL={override}: this agent only trades Alpaca paper accounts.")


class Alpaca:
    def __init__(self, key, secret, url):
        self.url, self.headers = url, {
            "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
            "Content-Type": "application/json", "User-Agent": "trading-agent/1.0",
        }

    def call(self, method, path, body=None, ok404=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=self.headers)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="replace")[:300]
                if e.code == 404 and ok404:
                    return None
                if e.code == 429 or e.code >= 500:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {msg}") from None
            except urllib.error.URLError as e:
                if attempt == 2:
                    raise RuntimeError(f"{method} {path} -> {e.reason}") from None
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"{method} {path} kept failing")


def load_report():
    if OUT.exists():
        rep = json.loads(OUT.read_text())
    else:
        rep = {"halted": False, "peak_equity": None, "orders": [], "log": []}
    rep.setdefault("orders", [])
    rep.setdefault("log", [])
    return rep


def save_report(rep):
    rep["orders"] = rep["orders"][-200:]
    rep["log"] = rep["log"][-200:]
    rep["updated"] = now()
    DATA.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rep, indent=1) + "\n")


def log(rep, text):
    print(text)
    rep["log"].append({"time": now(), "text": text})


def current_targets(state, universe):
    """Target weights from the strategy's last rebalance, validated."""
    targets = state.get("targets")
    if targets is None:  # state written before targets were recorded: use the paper holdings' weights
        targets = {h["symbol"]: h["weight"] for h in state.get("holdings", [])}
    targets = {s: float(w) for s, w in targets.items() if w > 0}
    bad = [s for s in targets if s not in universe]
    if bad:
        raise RuntimeError(f"Targets include symbols outside the universe: {bad}")
    total = sum(targets.values())
    if total > 1.0001:
        raise RuntimeError(f"Target weights sum to {total:.4f}, more than 100%")
    return targets


def wait_for(api, ids, seconds):
    """Poll until the orders are done or time runs out. Returns {id: order}."""
    done, deadline = {}, time.time() + seconds
    while ids and time.time() < deadline:
        for oid in list(ids):
            o = api.call("GET", f"/v2/orders/{oid}")
            if o["status"] in ("filled", "canceled", "expired", "rejected", "done_for_day"):
                done[oid] = o
                ids.remove(oid)
        if ids:
            time.sleep(3)
    for oid in ids:
        done[oid] = api.call("GET", f"/v2/orders/{oid}")
    return done


def record(rep, o, reason):
    rep["orders"].append({
        "time": now(), "id": o["id"], "symbol": o["symbol"], "side": o["side"],
        "qty": o.get("qty"), "notional": o.get("notional"), "status": o["status"],
        "filled_qty": o.get("filled_qty"), "filled_avg_price": o.get("filled_avg_price"),
        "reason": reason,
    })


def snapshot(api, rep):
    acct = api.call("GET", "/v2/account")
    pos = api.call("GET", "/v2/positions") or []
    eq = float(acct["equity"])
    rep["account"] = {
        "equity": round(eq, 2), "cash": round(float(acct["cash"]), 2),
        "last_equity": round(float(acct.get("last_equity") or eq), 2),
        "status": acct.get("status"),
    }
    rep["positions"] = [{
        "symbol": p["symbol"], "qty": float(p["qty"]),
        "price": round(float(p["current_price"]), 2), "value": round(float(p["market_value"]), 2),
        "weight": round(float(p["market_value"]) / eq, 4) if eq else 0,
        "unrealized_pl": round(float(p["unrealized_pl"]), 2),
        "avg_entry": round(float(p["avg_entry_price"]), 2),
    } for p in sorted(pos, key=lambda p: p["symbol"])]
    rep["peak_equity"] = round(max(eq, rep.get("peak_equity") or eq), 2)
    rep["history"] = [h for h in rep.get("history", []) if h["date"] != today()]
    rep["history"].append({"date": today(), "equity": round(eq, 2)})
    return acct, pos


def cid(symbol, side):
    return f"ta-{symbol}-{side}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"


def today():
    return datetime.now(timezone.utc).date().isoformat()


def sync(api, rep, cfg, ex):
    state = json.loads(STATE.read_text())
    from .strategies import symbols_of
    targets = current_targets(state, symbols_of(cfg))

    acct, pos = snapshot(api, rep)
    if acct.get("trading_blocked") or acct.get("account_blocked"):
        log(rep, "Alpaca reports the account is blocked from trading. Nothing placed.")
        return

    eq, peak = float(acct["equity"]), rep["peak_equity"]
    dd = eq / peak - 1 if peak else 0
    if dd <= -ex["max_drawdown_halt"]:
        api.call("DELETE", "/v2/orders")
        api.call("DELETE", "/v2/positions?cancel_orders=true")
        rep["halted"] = True
        rep["peak_equity"] = None  # the high-water mark restarts from here when you resume
        snapshot(api, rep)
        log(rep, f"Drawdown halt: equity ${eq:,.2f} is {-dd:.1%} below the ${peak:,.2f} peak "
                 f"(limit {ex['max_drawdown_halt']:.0%}). Closed every position. Trading stays off until "
                 f"you set \"halted\" to false in docs/data/broker.json.")
        return

    api.call("DELETE", "/v2/orders")  # clear anything left over from an earlier run
    budget = eq * ex["allocation"]
    held = {p["symbol"]: p for p in pos}
    prices = {s: float(p["current_price"]) for s, p in held.items()}  # buys go by dollar amount

    plan = []
    for s in sorted(set(held) | set(targets)):
        have = float(held[s]["market_value"]) if s in held else 0.0
        want = targets.get(s, 0.0) * budget
        gap = want - have
        if s not in targets:
            plan.append(("close", s, have, f"{s} is no longer a target"))
        elif s not in held:
            plan.append(("buy", s, want, f"new target at {targets[s]:.0%}"))
        elif abs(gap) / eq >= ex["drift_threshold"]:
            side = "buy" if gap > 0 else "sell"
            plan.append((side, s, abs(gap), f"{have / eq:.1%} held vs {targets[s]:.0%} target"))

    if not plan:
        log(rep, f"In line with targets ({', '.join(f'{s} {w:.0%}' for s, w in targets.items()) or 'all cash'}). "
                 f"No orders.")
        return

    # Sells first, so their proceeds fund the buys.
    sell_ids = []
    for kind, s, amt, why in plan:
        if kind == "close":
            o = api.call("DELETE", f"/v2/positions/{s}", ok404=True)
            if o:
                sell_ids.append(o["id"]); record(rep, o, why)
                log(rep, f"SELL all {s}: {why}.")
        elif kind == "sell":
            qty = min(float(held[s]["qty"]), round(amt / prices[s], 6))
            if qty * prices[s] < ex["min_order_usd"]:
                continue
            o = api.call("POST", "/v2/orders", {
                "symbol": s, "qty": f"{qty:.6f}", "side": "sell", "type": "market",
                "time_in_force": "day", "client_order_id": cid(s, "sell")})
            sell_ids.append(o["id"]); record(rep, o, why)
            log(rep, f"SELL {qty:.4f} {s} (about ${amt:,.2f}): {why}.")
    if sell_ids:
        for o in wait_for(api, sell_ids, ex["fill_wait_seconds"]).values():
            if o["status"] != "filled":
                log(rep, f"Sell of {o['symbol']} ended as {o['status']}.")

    cash = float(api.call("GET", "/v2/account")["cash"])
    buy_ids = []
    for kind, s, amt, why in plan:
        if kind != "buy":
            continue
        notional = round(min(amt, cash * 0.995), 2)  # never use margin
        if notional < ex["min_order_usd"]:
            log(rep, f"Skipped {s} buy: only ${cash:,.2f} cash left.")
            continue
        try:
            o = api.call("POST", "/v2/orders", {
                "symbol": s, "notional": f"{notional:.2f}", "side": "buy", "type": "market",
                "time_in_force": "day", "client_order_id": cid(s, "buy")})
        except RuntimeError as e:
            # Some funds (often leveraged ETFs) cannot be bought in fractions: buy whole shares.
            ref = (state.get("prices") or {}).get(s)
            if "fraction" not in str(e).lower() or not ref:
                raise
            qty = int(notional / (ref * 1.02))
            if qty < 1:
                log(rep, f"Skipped {s}: ${notional:,.2f} buys less than one whole share.")
                continue
            o = api.call("POST", "/v2/orders", {
                "symbol": s, "qty": str(qty), "side": "buy", "type": "market",
                "time_in_force": "day", "client_order_id": cid(s, "buy")})
            log(rep, f"{s} is not fractionable; bought {qty} whole shares instead.")
        cash -= notional
        buy_ids.append(o["id"]); record(rep, o, why)
        log(rep, f"BUY ${notional:,.2f} of {s}: {why}.")
    if buy_ids:
        for o in wait_for(api, buy_ids, ex["fill_wait_seconds"]).values():
            if o["status"] != "filled":
                log(rep, f"Buy of {o['symbol']} ended as {o['status']}.")

    # Refresh the stored order statuses and the account after trading.
    final = {o["id"]: o for o in api.call("GET", "/v2/orders?status=all&limit=50") or []}
    for r in rep["orders"]:
        if r["id"] in final:
            f = final[r["id"]]
            r.update(status=f["status"], filled_qty=f.get("filled_qty"), filled_avg_price=f.get("filled_avg_price"))
    snapshot(api, rep)


def main():
    cfg = json.loads((ROOT / "config.json").read_text())
    ex = {**DEFAULTS, **cfg.get("execution", {})}
    rep = load_report()
    key, secret = os.environ.get("ALPACA_KEY_ID"), os.environ.get("ALPACA_SECRET_KEY")

    if not (key and secret):
        rep["connected"] = False
        log(rep, "No Alpaca paper keys found (ALPACA_KEY_ID / ALPACA_SECRET_KEY). Nothing placed.")
        save_report(rep)
        return
    rep["connected"] = True
    api = Alpaca(key, secret, base_url())

    try:
        if not ex["enabled"]:
            log(rep, "Execution is switched off in config.json. Account snapshot only.")
            snapshot(api, rep)
        elif rep.get("halted"):
            log(rep, "Trading is halted after a drawdown. Set \"halted\" to false in docs/data/broker.json to resume.")
            snapshot(api, rep)
        elif not api.call("GET", "/v2/clock")["is_open"]:
            log(rep, "Market is closed. Account snapshot only.")
            snapshot(api, rep)
        else:
            sync(api, rep, cfg, ex)
    except RuntimeError as e:
        log(rep, f"Stopped: {e}")
        save_report(rep)
        sys.exit(1)
    save_report(rep)


if __name__ == "__main__":
    main()
