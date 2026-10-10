"""Robinhood crypto as a broker for agent/live.py (BROKER=robinhood).

Uses Robinhood's official Crypto Trading API (crypto only; Robinhood has no official stock API).
That API has no price history, so the 15-minute trend signals come from Coinbase's public candles,
while quotes, spreads and orders come from Robinhood.

The bot trades its own budget only:
  * It keeps a book of the coins it bought (with entry prices) and only ever sells those, so coins
    you hold yourself in the same Robinhood crypto account are never touched.
  * Its equity is `budget_usd` plus realized P&L plus the value of its open coins, so your own
    deposits and withdrawals don't move its halt line.

Dry run by default: orders are simulated at the current bid/ask and logged. Set RH_LIVE=1 to place
real orders.

Environment: RH_API_KEY, RH_PRIVATE_KEY (base64 Ed25519 key; see README), RH_LIVE=1 for real orders.
"""
import base64
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

RH_URL = "https://trading.robinhood.com"
CANDLES_URL = "https://api.exchange.coinbase.com/products/{}/candles?granularity=900"


def pair(sym):
    """BTC/USD -> BTC-USD"""
    return sym.replace("/", "-")


def round_down(qty, step):
    step = float(step)
    decimals = max(0, -int(f"{step:e}".split("e")[1]))
    return f"{int(qty / step + 1e-9) * step:.{decimals}f}"


def http_json(req, tries=4):
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:300]
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"{req.get_method()} {req.full_url} -> HTTP {e.code}: {msg}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == tries - 1:
                raise RuntimeError(f"{req.get_method()} {req.full_url} -> {e}") from None
            time.sleep(2 ** attempt)
    raise RuntimeError(f"{req.get_method()} {req.full_url} kept failing")


class RobinhoodCrypto:
    def __init__(self, api_key, private_key_b64, book_path, budget_usd, live=False, expected_account=None):
        self.key, self.live, self.budget = api_key, live, float(budget_usd)
        self.expected_account = expected_account  # live orders only go to this crypto account number
        self.signer = None
        if private_key_b64:
            from nacl.signing import SigningKey
            self.signer = SigningKey(base64.b64decode(private_key_b64)[:32])
        self.book_path = Path(book_path)
        try:
            self.book = json.loads(self.book_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.book = {}
        self.book.setdefault("coins", {})       # "BTC/USD" -> {"qty": float, "entry": float}
        self.book.setdefault("realized", 0.0)
        self._pairs = {}
        self._last_quotes = {}

    # --- transport ---

    def call(self, method, path, body=None):
        text = json.dumps(body) if body is not None else ""
        ts = str(int(time.time()))
        sig = self.signer.sign(f"{self.key}{ts}{path}{method}{text}".encode()).signature
        req = urllib.request.Request(RH_URL + path, data=text.encode() if body is not None else None,
                                     method=method, headers={
            "x-api-key": self.key, "x-timestamp": ts, "x-signature": base64.b64encode(sig).decode(),
            "Content-Type": "application/json; charset=utf-8"})
        return http_json(req)

    def _save(self):
        tmp = Path(str(self.book_path) + ".tmp")
        tmp.write_text(json.dumps(self.book, indent=1))
        tmp.replace(self.book_path)

    # --- the interface agent/live.py uses ---

    def open_value(self):
        return sum(c["qty"] * self._last_quotes.get(s, {}).get("bid", c["entry"])
                   for s, c in self.book["coins"].items())

    def cost_basis(self):
        return sum(c["qty"] * c["entry"] for c in self.book["coins"].values())

    def whoami(self):
        """The crypto account this API key belongs to."""
        return self.call("GET", "/api/v1/crypto/trading/accounts/")

    def account(self):
        bot_cash = self.budget + self.book["realized"] - self.cost_basis()
        cash = bot_cash
        if self.live:
            acct = self.call("GET", "/api/v1/crypto/trading/accounts/")
            if str(acct.get("account_number")) != str(self.expected_account):
                raise RuntimeError(
                    f"this API key trades crypto account ending {str(acct.get('account_number'))[-4:]}, "
                    f"not the one in RH_ACCOUNT (ending {str(self.expected_account)[-4:]}). Nothing placed.")
            if acct.get("status") != "active":
                return {"equity": "0", "cash": "0", "trading_blocked": True}
            cash = min(bot_cash, float(acct["buying_power"]))
        return {"equity": str(bot_cash + self.open_value()), "cash": str(max(cash, 0.0))}

    def positions(self):
        out = []
        for sym, c in self.book["coins"].items():
            if c["qty"] <= 0:
                continue
            price = self._last_quotes.get(sym, {}).get("bid", c["entry"])
            out.append({"symbol": sym.replace("/", ""), "asset_class": "crypto", "qty": str(c["qty"]),
                        "avg_entry_price": str(c["entry"]), "current_price": str(price)})
        return out

    def market_open(self):
        return False  # crypto only: the stock list is never traded through Robinhood

    def quotes(self, crypto, stocks):
        if not crypto:
            return {}
        qs = "&".join(f"symbol={pair(s)}" for s in crypto)
        if self.signer:
            rows = self.call("GET", f"/api/v1/crypto/marketdata/best_bid_ask/?{qs}")["results"]
        else:  # no keys: dry run on Coinbase's public ticker so the bot can be tried without an account
            rows = [self._public_quote(s) for s in crypto]
        out = {}
        for r in rows:
            sym = r["symbol"].replace("-", "/")
            bid = float(r["bid_inclusive_of_sell_spread"])
            ask = float(r["ask_inclusive_of_buy_spread"])
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2
                out[sym] = {"bid": bid, "ask": ask, "mid": mid, "spread": (ask - bid) / mid}
        self._last_quotes = out
        return out

    def _public_quote(self, sym):
        t = http_json(urllib.request.Request(
            f"https://api.exchange.coinbase.com/products/{pair(sym)}/ticker",
            headers={"User-Agent": "trading-agent-live/1.0"}))
        # Pad Coinbase's spread to roughly what Robinhood charges, so dry runs aren't optimistic.
        price = float(t["price"])
        return {"symbol": pair(sym), "bid_inclusive_of_sell_spread": price * 0.99,
                "ask_inclusive_of_buy_spread": price * 1.01}

    def closes(self, crypto, stocks, hours=36):
        out = {}
        for sym in crypto:
            rows = http_json(urllib.request.Request(CANDLES_URL.format(pair(sym)),
                                                    headers={"User-Agent": "trading-agent-live/1.0"}))
            rows = sorted(rows or [], key=lambda r: r[0])[-(hours * 4):]  # [time, low, high, open, close, vol]
            out[sym] = [float(r[4]) for r in rows]
        return out

    def _pair_info(self, sym):
        if sym not in self._pairs:
            self._pairs[sym] = self.call(
                "GET", f"/api/v1/crypto/trading/trading_pairs/?symbol={pair(sym)}")["results"][0]
        return self._pairs[sym]

    def _market(self, sym, side, qty):
        """Place a market order and wait for it. Returns (filled_qty, avg_price)."""
        o = self.call("POST", "/api/v1/crypto/trading/orders/", {
            "client_order_id": str(uuid.uuid4()), "side": side, "type": "market",
            "symbol": pair(sym), "market_order_config": {"asset_quantity": qty}})
        for _ in range(20):
            o = self.call("GET", f"/api/v1/crypto/trading/orders/{o['id']}/")
            if o.get("state") in ("filled", "canceled", "failed"):
                break
            time.sleep(1.5)
        filled = float(o.get("filled_asset_quantity") or 0)
        return filled, float(o.get("average_price") or 0)

    def buy(self, sym, usd, crypto=True):
        ask = self._last_quotes[sym]["ask"]
        if self.live:
            info = self._pair_info(sym)
            qty = round_down(usd / ask, info["asset_increment"])
            if float(qty) < float(info.get("min_order_size", 0)) or float(qty) <= 0:
                raise RuntimeError(f"${usd:.2f} of {sym} is under Robinhood's minimum order size")
            filled, price = self._market(sym, "buy", qty)
            if not filled:
                raise RuntimeError(f"buy of {sym} did not fill")
        else:
            filled, price = usd / ask, ask
        c = self.book["coins"].setdefault(sym, {"qty": 0.0, "entry": price})
        c["entry"] = (c["qty"] * c["entry"] + filled * price) / (c["qty"] + filled)
        c["qty"] += filled
        self._save()

    def sell(self, sym, fraction):
        sym = sym if "/" in sym else sym[:-3] + "/" + sym[-3:]   # BTCUSD -> BTC/USD
        c = self.book["coins"].get(sym)
        if not c or c["qty"] <= 0:
            return None
        want = c["qty"] if fraction >= 1 else c["qty"] * fraction
        if self.live:
            info = self._pair_info(sym)
            held = self.call("GET", f"/api/v1/crypto/trading/holdings/?asset_code={sym.split('/')[0]}")["results"]
            avail = float(held[0]["quantity_available_for_trading"]) if held else 0.0
            qty = round_down(min(want, avail), info["asset_increment"])
            if float(qty) <= 0:
                raise RuntimeError(f"nothing available to sell for {sym}")
            filled, price = self._market(sym, "sell", qty)
        else:
            filled, price = want, self._last_quotes.get(sym, {}).get("bid", c["entry"])
        self.book["realized"] += filled * (price - c["entry"])
        c["qty"] -= filled
        if c["qty"] * max(price, c["entry"]) < 0.01:  # sold out (leftover dust is unsellable)
            self.book["coins"].pop(sym)
        self._save()
        return {"filled": filled, "price": price}
