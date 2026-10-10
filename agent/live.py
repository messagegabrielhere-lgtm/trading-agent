"""Always-on momentum trader for an Alpaca account (crypto 24/7, stocks during market hours).

    python -m agent.live            # runs until stopped
    python -m agent.live --once     # one tick, then exit (for testing)

Two layers:
  * This loop, in plain code. Every `tick_seconds` it reads quotes and positions and enforces the
    exits (stop-loss, trailing stop, partial take-profit, trend break, account halt). Every
    `signal_seconds` it recomputes the 15-minute trend signals and looks for entries.
  * agent/brain.py, once an hour when ANTHROPIC_API_KEY is set: Claude reads a market summary and
    returns risk on/off and which symbols may be bought. It can veto entries; it never places orders.

Safety rails:
  * Paper or dry run by default. Real money needs ALPACA_MODE=live or RH_LIVE=1, set on purpose.
  * Cash only (no margin, no shorting). Buys are sized from cash, never buying power.
  * At most `max_positions` positions; a symbol is skipped when its spread is too wide.
  * Account halt: if equity falls `halt_drawdown` below its peak, sell everything and stop buying
    until you set "halted": false in the state file. A daily loss of `daily_loss_pause` pauses
    new buys until the next UTC day.
  * Every order and halt is printed and, when NTFY_TOPIC is set, pushed to your phone via ntfy.sh.

Brokers (BROKER=alpaca, the default, or BROKER=robinhood):
  * Alpaca: crypto and stocks. Paper by default; live needs ALPACA_MODE=live.
  * Robinhood: crypto only, through Robinhood's official Crypto Trading API (agent/rh_broker.py).
    Dry run by default; real orders need RH_LIVE=1. Trades only its own `budget_usd`.

Environment:
  BROKER                             "alpaca" (default) or "robinhood"
  ALPACA_KEY_ID, ALPACA_SECRET_KEY   Alpaca API keys (paper or live, matching ALPACA_MODE)
  ALPACA_MODE                        "paper" (default) or "live"
  RH_API_KEY, RH_PRIVATE_KEY         Robinhood crypto API key and base64 Ed25519 private key
  RH_LIVE                            "1" to place real Robinhood orders
  ANTHROPIC_API_KEY                  optional, turns on the hourly Claude review
  NTFY_TOPIC                         optional, ntfy.sh topic for phone alerts
  LIVE_STATE                         optional, path of the state file (default ./live_state.json)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRADE_URLS = {"paper": "https://paper-api.alpaca.markets", "live": "https://api.alpaca.markets"}
DATA_URL = "https://data.alpaca.markets"

DEFAULTS = {
    "crypto": ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD", "AVAX/USD", "LINK/USD", "XRP/USD"],
    "stocks": ["TQQQ", "SOXL", "TNA", "QQQ", "SPY"],
    "tick_seconds": 15,
    "signal_seconds": 60,
    "max_positions": 2,
    "position_pct": 0.5,         # share of equity per position
    "min_order_usd": 1.0,
    "max_spread": 0.005,         # skip a symbol whose bid/ask spread is wider than 0.5%
    "min_momentum": 0.005,       # 4-hour return needed to buy
    "max_chase": 0.08,           # skip a symbol up more than 8% in the last hour
    "stop_loss": 0.06,           # sell if 6% below entry
    "trail_start": 0.02,         # start trailing once 2% up
    "trail_pct": 0.04,           # then sell 4% below the high since entry
    "take_half_at": 0.10,        # sell half at +10%
    "halt_drawdown": 0.25,       # sell everything and stop at 25% below peak equity
    "daily_loss_pause": 0.10,    # no new buys after a 10% loss on the day
    "cooldown_minutes": 30,      # don't rebuy a symbol this soon after selling it
    "brain_minutes": 60,
    "budget_usd": 20.0,          # BROKER=robinhood only: the most the bot may have in play
}


def now():
    return datetime.now(timezone.utc)


def stamp():
    return now().isoformat(timespec="seconds")


# ---------- indicators and decisions (pure functions, unit tested) ----------

def ema(values, n):
    k, out = 2 / (n + 1), None
    for v in values:
        out = v if out is None else v * k + out * (1 - k)
    return out


def signal(closes, cfg):
    """Trend signal from 15-minute closes. Returns a dict, or None if there is too little data."""
    if len(closes) < 40:
        return None
    fast, slow = ema(closes, 9), ema(closes, 21)
    slow_prev = ema(closes[:-4], 21)
    mom_4h = closes[-1] / closes[-17] - 1
    chg_1h = closes[-1] / closes[-5] - 1
    uptrend = fast > slow and closes[-1] > slow and slow > slow_prev
    return {
        "price": closes[-1], "ema_fast": fast, "ema_slow": slow, "mom_4h": mom_4h, "chg_1h": chg_1h,
        "uptrend": uptrend,
        "buy": uptrend and mom_4h >= cfg["min_momentum"] and chg_1h <= cfg["max_chase"],
    }


def exit_decision(entry, price, high, took_half, sig, cfg):
    """What to do with an open position. Returns (action, reason) with action in
    None, "half", "all"."""
    gain = price / entry - 1
    if gain <= -cfg["stop_loss"]:
        return "all", f"stop-loss ({gain:+.1%})"
    if high / entry - 1 >= cfg["trail_start"] and price <= high * (1 - cfg["trail_pct"]):
        return "all", f"trailing stop ({price / high - 1:+.1%} from high, {gain:+.1%} overall)"
    if sig is not None and not sig["uptrend"] and sig["ema_fast"] < sig["ema_slow"]:
        return "all", f"trend broke ({gain:+.1%})"
    if not took_half and gain >= cfg["take_half_at"]:
        return "half", f"take profit ({gain:+.1%})"
    return None, ""


def pick_entries(signals, quotes, held, allowed, slots, cfg):
    """Symbols to buy, best first."""
    cands = []
    for sym, sig in signals.items():
        q = quotes.get(sym)
        if not sig or not sig["buy"] or sym in held or sym not in allowed or not q:
            continue
        if q["spread"] > cfg["max_spread"]:
            continue
        cands.append((sig["mom_4h"], sym))
    return [s for _, s in sorted(cands, reverse=True)[:max(slots, 0)]]


def norm(sym):
    """Alpaca reports crypto positions without the slash: BTC/USD -> BTCUSD."""
    return sym.replace("/", "")


# ---------- Alpaca ----------

class Alpaca:
    def __init__(self, key, secret, mode):
        if mode not in TRADE_URLS:
            sys.exit(f"ALPACA_MODE must be paper or live, not {mode!r}")
        self.mode, self.trade = mode, TRADE_URLS[mode]
        self.headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                        "Content-Type": "application/json", "User-Agent": "trading-agent-live/1.0"}

    def call(self, method, url, body=None, ok404=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=self.headers)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="replace")[:300]
                if e.code == 404 and ok404:
                    return None
                if e.code == 429 or e.code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {msg}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == 3:
                    raise RuntimeError(f"{method} {url} -> {e}") from None
                time.sleep(2 ** attempt)
        raise RuntimeError(f"{method} {url} kept failing")

    def t(self, method, path, body=None, ok404=False):
        return self.call(method, self.trade + path, body, ok404)

    def d(self, path, **params):
        return self.call("GET", f"{DATA_URL}{path}?{urllib.parse.urlencode(params)}")

    def account(self):
        return self.t("GET", "/v2/account")

    def positions(self):
        return self.t("GET", "/v2/positions") or []

    def market_open(self):
        return bool(self.t("GET", "/v2/clock")["is_open"])

    def quotes(self, crypto, stocks):
        out = {}
        if crypto:
            r = self.d("/v1beta3/crypto/us/latest/quotes", symbols=",".join(crypto))
            out.update(r.get("quotes", {}))
        if stocks:
            r = self.d("/v2/stocks/quotes/latest", symbols=",".join(stocks), feed="iex")
            out.update(r.get("quotes", {}))
        res = {}
        for sym, q in out.items():
            bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2
                res[sym] = {"bid": bid, "ask": ask, "mid": mid, "spread": (ask - bid) / mid}
        return res

    def closes(self, crypto, stocks, hours=36):
        start = (now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        bars = {}
        for path, syms, extra in (("/v1beta3/crypto/us/bars", crypto, {}),
                                  ("/v2/stocks/bars", stocks, {"feed": "iex"})):
            if not syms:
                continue
            token = None
            while True:
                params = {"symbols": ",".join(syms), "timeframe": "15Min", "start": start,
                          "limit": 10000, **extra}
                if token:
                    params["page_token"] = token
                r = self.d(path, **params)
                for sym, rows in (r.get("bars") or {}).items():
                    bars.setdefault(sym, []).extend(float(b["c"]) for b in rows)
                token = r.get("next_page_token")
                if not token:
                    break
        return bars

    def buy(self, sym, usd, crypto):
        return self.t("POST", "/v2/orders", {
            "symbol": sym, "notional": f"{usd:.2f}", "side": "buy", "type": "market",
            "time_in_force": "gtc" if crypto else "day",
            "client_order_id": f"live-{norm(sym)}-{uuid.uuid4().hex[:10]}"})

    def sell(self, sym, fraction):
        q = "" if fraction >= 1 else f"?percentage={fraction * 100:.0f}"
        return self.t("DELETE", f"/v2/positions/{norm(sym)}{q}", ok404=True)


# ---------- state and alerts ----------

def load_state(path):
    try:
        s = json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("halted", False)
    s.setdefault("peak_equity", None)
    s.setdefault("day", None)
    s.setdefault("day_start_equity", None)
    s.setdefault("positions", {})     # symbol -> {"high": float, "took_half": bool}
    s.setdefault("cooldown", {})      # symbol -> ISO time of last sell
    s.setdefault("brain", {"risk": "on", "allow": None, "note": "", "time": None})
    return s


def save_state(path, s):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(s, indent=1))
    tmp.replace(path)


def alert(text):
    print(f"{stamp()} {text}", flush=True)
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=text.encode(), method="POST",
                                     headers={"Title": "Trading agent"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:  # an alert failure must never stop trading
        print(f"{stamp()} ntfy failed: {e}", flush=True)


def log(text):
    print(f"{stamp()} {text}", flush=True)


# ---------- the loop ----------

class Trader:
    def __init__(self, api, cfg, state_path, brain=None):
        self.api, self.cfg, self.path, self.brain = api, cfg, state_path, brain
        self.s = load_state(state_path)
        self.signals, self.last_signal, self.last_brain = {}, 0.0, 0.0

    def universe(self, stocks_open):
        return self.cfg["crypto"], (self.cfg["stocks"] if stocks_open else [])

    def tick(self):
        cfg, s = self.cfg, self.s
        acct = self.api.account()
        if acct.get("trading_blocked") or acct.get("account_blocked"):
            log("Alpaca reports the account is blocked. Nothing placed.")
            return
        equity, cash = float(acct["equity"]), float(acct["cash"])
        today = now().date().isoformat()
        if s["day"] != today:
            s["day"], s["day_start_equity"] = today, equity
        s["peak_equity"] = max(equity, s["peak_equity"] or equity)

        positions = {p["symbol"]: p for p in self.api.positions()}
        stocks_open = self.api.market_open()
        crypto, stocks = self.universe(stocks_open)
        quotes = self.api.quotes(crypto, stocks)

        if time.time() - self.last_signal >= cfg["signal_seconds"]:
            closes = self.api.closes(crypto, stocks)
            self.signals = {sym: signal(closes.get(sym, []), cfg) for sym in crypto + stocks}
            self.last_signal = time.time()

        # Account halt.
        dd = equity / s["peak_equity"] - 1
        if s["halted"] or dd <= -cfg["halt_drawdown"]:
            if not s["halted"]:
                for sym in positions:
                    self.api.sell(sym, 1)
                s["halted"] = True
                alert(f"HALTED: equity ${equity:,.2f} is {-dd:.0%} below the ${s['peak_equity']:,.2f} peak. "
                      f"Sold everything. Set \"halted\": false in {self.path} to resume.")
            self._save()
            return

        # Exits, every tick.
        by_norm = {norm(sym): sym for sym in crypto + self.cfg["stocks"]}
        for psym, p in positions.items():
            sym = by_norm.get(psym, psym)
            is_crypto = p.get("asset_class") == "crypto"
            if not is_crypto and not stocks_open:
                continue  # stock orders only fill in market hours
            q = quotes.get(sym)
            price = q["bid"] if q else float(p["current_price"])
            entry = float(p["avg_entry_price"])
            track = s["positions"].setdefault(psym, {"high": price, "took_half": False})
            track["high"] = max(track["high"], price)
            action, why = exit_decision(entry, price, track["high"], track["took_half"],
                                        self.signals.get(sym), cfg)
            if not action:
                continue
            self.api.sell(sym, 0.5 if action == "half" else 1)
            pnl = (price - entry) * float(p["qty"]) * (0.5 if action == "half" else 1)
            alert(f"SELL {'half ' if action == 'half' else ''}{sym} @ {price:,.4g}: {why}, P&L ${pnl:+,.2f}")
            if action == "half":
                track["took_half"] = True
            else:
                s["positions"].pop(psym, None)
                s["cooldown"][psym] = stamp()
        for psym in list(s["positions"]):
            if psym not in positions:
                s["positions"].pop(psym)  # closed elsewhere (or by a fill we didn't see)

        # Hourly Claude review.
        if self.brain and time.time() - self.last_brain >= cfg["brain_minutes"] * 60:
            self.last_brain = time.time()
            try:
                verdict = self.brain(self.summary(equity, cash, positions, quotes))
                s["brain"] = {**verdict, "time": stamp()}
                log(f"Claude review: risk {verdict['risk']}, allow {verdict['allow']}. {verdict['note']}")
            except Exception as e:
                log(f"Claude review failed, keeping the last verdict: {e}")

        # Entries.
        if s["day_start_equity"] and equity / s["day_start_equity"] - 1 <= -cfg["daily_loss_pause"]:
            self._save()
            return
        if s["brain"].get("risk") == "off":
            self._save()
            return
        allowed = set(crypto + stocks)
        if s["brain"].get("allow") is not None:
            allowed &= set(s["brain"]["allow"])
        cutoff = now() - timedelta(minutes=cfg["cooldown_minutes"])
        allowed = {a for a in allowed
                   if datetime.fromisoformat(s["cooldown"].get(norm(a), "2000-01-01T00:00:00+00:00")) < cutoff}
        slots = cfg["max_positions"] - len(positions)
        for sym in pick_entries(self.signals, quotes, {by_norm.get(p, p) for p in positions},
                                allowed, slots, cfg):
            usd = round(min(equity * cfg["position_pct"], cash * 0.98), 2)
            if usd < cfg["min_order_usd"]:
                break
            try:
                self.api.buy(sym, usd, "/" in sym)
            except RuntimeError as e:
                log(f"Buy {sym} failed: {e}")
                continue
            cash -= usd
            sig = self.signals[sym]
            s["positions"][norm(sym)] = {"high": quotes[sym]["ask"], "took_half": False}
            alert(f"BUY ${usd:,.2f} {sym} @ {quotes[sym]['ask']:,.4g}: uptrend, 4h {sig['mom_4h']:+.1%}")
        self._save()

    def summary(self, equity, cash, positions, quotes):
        rows = []
        for sym, sig in self.signals.items():
            if sig:
                rows.append({"symbol": sym, "price": round(sig["price"], 6), "mom_4h": round(sig["mom_4h"], 4),
                             "chg_1h": round(sig["chg_1h"], 4), "uptrend": sig["uptrend"],
                             "spread": round(quotes[sym]["spread"], 4) if sym in quotes else None})
        held = [{"symbol": k, "qty": v["qty"], "entry": v["avg_entry_price"],
                 "unrealized_plpc": v.get("unrealized_plpc")} for k, v in positions.items()]
        return {"time": stamp(), "equity": equity, "cash": cash, "peak_equity": self.s["peak_equity"],
                "positions": held, "symbols": rows}

    def _save(self):
        save_state(self.path, self.s)


def make_broker(cfg, state_path):
    """The broker named by BROKER (alpaca or robinhood) and a label for the start-up alert."""
    broker = os.environ.get("BROKER", "alpaca")
    if broker == "robinhood":
        from .rh_broker import RobinhoodCrypto
        key, priv = os.environ.get("RH_API_KEY"), os.environ.get("RH_PRIVATE_KEY")
        live = os.environ.get("RH_LIVE") == "1"
        if live and not (key and priv):
            sys.exit("RH_LIVE=1 needs RH_API_KEY and RH_PRIVATE_KEY.")
        api = RobinhoodCrypto(key, priv, str(state_path) + ".rh_book.json", cfg["budget_usd"], live)
        return api, f"Robinhood crypto {'LIVE' if live else 'DRY RUN'}, budget ${cfg['budget_usd']:,.2f}"
    if broker != "alpaca":
        sys.exit(f"BROKER must be alpaca or robinhood, not {broker!r}")
    key, secret = os.environ.get("ALPACA_KEY_ID"), os.environ.get("ALPACA_SECRET_KEY")
    if not (key and secret):
        sys.exit("Set ALPACA_KEY_ID and ALPACA_SECRET_KEY.")
    mode = os.environ.get("ALPACA_MODE", "paper")
    return Alpaca(key, secret, mode), f"Alpaca {mode.upper()}"


def load_config(broker):
    live_cfg = json.loads((ROOT / "config.json").read_text()).get("live", {})
    overrides = live_cfg.pop("robinhood", {})
    cfg = {**DEFAULTS, **live_cfg}
    if broker == "robinhood":
        cfg.update(overrides)
    return cfg


def main():
    cfg = load_config(os.environ.get("BROKER", "alpaca"))
    state_path = os.environ.get("LIVE_STATE", "live_state.json")
    api, label = make_broker(cfg, state_path)
    brain = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        from .brain import review
        brain = lambda summary: review(summary, cfg["crypto"] + cfg["stocks"])  # noqa: E731
    trader = Trader(api, cfg, state_path, brain)
    alert(f"Trading agent started: {label}, Claude review {'on' if brain else 'off'}.")
    once = "--once" in sys.argv
    while True:
        try:
            trader.tick()
        except Exception as e:  # keep running through network blips; the next tick retries
            log(f"Tick failed: {e}")
        if once:
            break
        time.sleep(cfg["tick_seconds"])


if __name__ == "__main__":
    main()
