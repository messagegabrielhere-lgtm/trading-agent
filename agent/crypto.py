"""Robinhood crypto: trend filter on BTC and ETH.

    python -m agent.crypto

Rule: hold a coin while its daily close is above its `sma_days` moving average, sit in cash while it
is below. The budget is split equally between the coins; a coin under its average leaves its share in
cash. This is the classic time-series trend rule: it gives up some upside, and its job is to step
aside during long crashes (BTC fell about 75% in 2022). Every run also backtests the rule against
buying and holding, and writes both to the dashboard.

The bot only ever trades its own budget (`budget_usd`). It records the coins it bought and only sells
those, so crypto you hold yourself in the same Robinhood account is never touched.

Modes (config.json -> crypto.mode):
  dry_run  (default) computes targets and logs the orders it would place. No keys needed.
  live     places real market orders. Needs RH_API_KEY and RH_PRIVATE_KEY (base64 Ed25519 key).

Robinhood API: https://docs.robinhood.com/crypto/trading/
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import data, engine

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data" / "crypto.json"
RH_URL = "https://trading.robinhood.com"

DEFAULTS = {
    "mode": "dry_run",
    "coins": ["BTC", "ETH"],
    "sma_days": 200,
    "budget_usd": 500,
    "max_order_usd": 300,
    "drift_threshold": 0.10,   # rebalance a coin only when it is 10% of its target or more off
    "min_order_usd": 5,
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def base_url():
    override = os.environ.get("RH_BASE_URL")
    if not override:
        return RH_URL
    if urlparse(override).hostname in ("localhost", "127.0.0.1"):
        return override.rstrip("/")
    sys.exit(f"Refusing RH_BASE_URL={override}.")


class Robinhood:
    """Minimal signed client. Signature = Ed25519 over api_key + timestamp + path + method + body."""

    def __init__(self, api_key, private_key_b64, url):
        from nacl.signing import SigningKey
        raw = base64.b64decode(private_key_b64)
        self.key, self.url = api_key, url
        self.signer = SigningKey(raw[:32])

    def call(self, method, path, body=None):
        text = json.dumps(body) if body is not None else ""
        ts = str(int(time.time()))
        sig = self.signer.sign(f"{self.key}{ts}{path}{method}{text}".encode()).signature
        req = urllib.request.Request(self.url + path, data=text.encode() if body is not None else None,
                                     method=method, headers={
            "x-api-key": self.key, "x-timestamp": ts, "x-signature": base64.b64encode(sig).decode(),
            "Content-Type": "application/json; charset=utf-8"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="replace")[:300]
                if e.code == 429 or e.code >= 500:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {msg}") from None
        raise RuntimeError(f"{method} {path} kept failing")

    def quote(self, coin):
        r = self.call("GET", f"/api/v1/crypto/marketdata/best_bid_ask/?symbol={coin}-USD")["results"][0]
        return float(r["bid_inclusive_of_sell_spread"]), float(r["ask_inclusive_of_buy_spread"])

    def pair(self, coin):
        return self.call("GET", f"/api/v1/crypto/trading/trading_pairs/?symbol={coin}-USD")["results"][0]

    def available(self, coin):
        res = self.call("GET", f"/api/v1/crypto/trading/holdings/?asset_code={coin}")["results"]
        return float(res[0]["quantity_available_for_trading"]) if res else 0.0

    def market_order(self, coin, side, qty):
        return self.call("POST", "/api/v1/crypto/trading/orders/", {
            "client_order_id": str(uuid.uuid4()), "side": side, "type": "market",
            "symbol": f"{coin}-USD", "market_order_config": {"asset_quantity": qty}})

    def order(self, oid):
        return self.call("GET", f"/api/v1/crypto/trading/orders/{oid}/")


def signals(cfg):
    """Latest close, its moving average, and a backtest of the rule vs buy-and-hold."""
    syms = [f"{c}-USD" for c in cfg["coins"]]
    dates, close, _ = data.load(syms, rng="10y")
    n = cfg["sma_days"]
    out = {}
    for c, s in zip(cfg["coins"], syms):
        px = close[s]
        sma = sum(px[-n:]) / n
        out[c] = {"close": round(px[-1], 2), "sma": round(sma, 2), "above": px[-1] > sma}

    # Backtest: equal budget per coin, in the coin while yesterday's close > its SMA.
    k = len(syms)
    strat, hold = [1.0], [1.0]
    for i in range(n + 1, len(dates)):
        r_s = r_h = 0.0
        for s in syms:
            px = close[s]
            r = px[i] / px[i - 1] - 1
            r_h += r / k
            if px[i - 1] > sum(px[i - n:i]) / n:
                r_s += r / k
        strat.append(strat[-1] * (1 + r_s))
        hold.append(hold[-1] * (1 + r_h))
    keep = sorted(set(range(0, len(strat), 7)) | {len(strat) - 1})
    d = dates[n:]
    return out, {
        "from": d[0], "to": d[-1],
        "dates": [d[i] for i in keep],
        "trend": [round(strat[i], 4) for i in keep],
        "hold": [round(hold[i], 4) for i in keep],
        "stats": {"trend": stats365(strat), "hold": stats365(hold)},
    }


def stats365(curve):
    s = engine.stats(curve)  # engine assumes 252 trading days; crypto trades every day
    years = (len(curve) - 1) / 365
    s["cagr"] = (curve[-1] / curve[0]) ** (1 / years) - 1
    s["vol"] = s["vol"] / 252 ** 0.5 * 365 ** 0.5
    s["sharpe"] = s["sharpe"] / 252 ** 0.5 * 365 ** 0.5
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in s.items()
            if k in ("cagr", "vol", "sharpe", "max_drawdown", "total_multiple")}


def round_down(qty, step):
    step = float(step)
    return f"{(int(qty / step) * step):.{max(0, -int(f'{step:e}'.split('e')[1]))}f}"


def main():
    cfg = {**DEFAULTS, **json.loads((ROOT / "config.json").read_text()).get("crypto", {})}
    live = cfg["mode"] == "live"
    rep = json.loads(OUT.read_text()) if OUT.exists() else {}
    # Coins the bot itself holds. Dry runs keep a separate simulated book so switching to live
    # starts from zero instead of "selling" coins that were never bought.
    book = "bot_qty" if live else "sim_qty"
    rep.setdefault(book, {})
    rep.setdefault("orders", [])
    rep.setdefault("log", [])
    rep["mode"] = cfg["mode"]
    rep["settings"] = {k: cfg[k] for k in ("coins", "sma_days", "budget_usd", "max_order_usd")}

    def log(text):
        print(text)
        rep["log"].append({"time": now(), "text": text})

    def save():
        rep["orders"], rep["log"] = rep["orders"][-200:], rep["log"][-200:]
        rep["updated"] = now()
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(rep, indent=1) + "\n")

    try:
        sig, bt = signals(cfg)
    except Exception as e:
        log(f"Stopped: could not load prices ({e}).")
        save()
        sys.exit(1)
    rep["signals"], rep["backtest"] = sig, bt
    share = cfg["budget_usd"] / len(cfg["coins"])
    targets = {c: (share if sig[c]["above"] else 0.0) for c in cfg["coins"]}
    rep["targets_usd"] = targets

    api = None
    if live:
        if not (os.environ.get("RH_API_KEY") and os.environ.get("RH_PRIVATE_KEY")):
            log("Mode is live but RH_API_KEY / RH_PRIVATE_KEY are not set. Nothing placed.")
            save()
            return
        api = Robinhood(os.environ["RH_API_KEY"], os.environ["RH_PRIVATE_KEY"], base_url())

    try:
        for c in cfg["coins"]:
            s = sig[c]
            trend = f"{c} ${s['close']:,.0f} is {'above' if s['above'] else 'below'} its {cfg['sma_days']}-day average ${s['sma']:,.0f}"
            mine = float(rep[book].get(c, 0))
            bid, ask = api.quote(c) if api else (s["close"], s["close"])
            have = mine * bid
            gap = targets[c] - have
            if abs(gap) < max(cfg["min_order_usd"], cfg["drift_threshold"] * share):
                log(f"{trend}. Bot holds ${have:,.2f} vs ${targets[c]:,.2f} target. No trade.")
                continue
            side = "buy" if gap > 0 else "sell"
            usd = min(abs(gap), cfg["max_order_usd"])
            if side == "sell" and targets[c] == 0:
                usd = have  # trend broke: sell everything the bot owns
            entry = {"time": now(), "coin": c, "side": side, "usd": round(usd, 2), "mode": cfg["mode"],
                     "reason": trend}
            if not api:
                qty = usd / (ask if side == "buy" else bid)
                entry.update(qty=round(qty, 8), status="dry run")
                rep[book][c] = round(max(0.0, mine + (qty if side == "buy" else -qty)), 8)
                log(f"Would {side.upper()} ${usd:,.2f} of {c}: {trend}.")
            else:
                pair = api.pair(c)
                if side == "buy":
                    qty = round_down(usd / ask, pair["asset_increment"])
                else:
                    qty = round_down(min(usd / bid, mine, api.available(c)), pair["asset_increment"])
                if float(qty) < float(pair.get("min_order_size", 0)) or float(qty) <= 0:
                    log(f"Skipped {side} of {c}: ${usd:,.2f} is under the minimum order size.")
                    continue
                o = api.market_order(c, side, qty)
                for _ in range(10):
                    o = api.order(o["id"])
                    if o.get("state") in ("filled", "canceled", "failed"):
                        break
                    time.sleep(3)
                filled = float(o.get("filled_asset_quantity") or 0)
                if filled:
                    rep[book][c] = round(max(0.0, mine + (filled if side == "buy" else -filled)), 8)
                entry.update(qty=qty, id=o.get("id"), status=o.get("state"), filled=filled,
                             avg_price=o.get("average_price"))
                log(f"{side.upper()} {qty} {c} (about ${usd:,.2f}): {trend}. Order {o.get('state')}.")
            rep["orders"].append(entry)
    except RuntimeError as e:
        log(f"Stopped: {e}")
        save()
        sys.exit(1)
    rep["book"] = book
    rep["bot_value_usd"] = round(sum(float(q) * sig[c]["close"] for c, q in rep[book].items()
                                     if c in sig), 2)
    save()


if __name__ == "__main__":
    main()
