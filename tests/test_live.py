import json
import tempfile
import unittest
from pathlib import Path

from agent import live

CFG = dict(live.DEFAULTS, crypto=["BTC/USD", "ETH/USD"], stocks=["TQQQ"])


def rising(n=60, start=100.0, step=0.002):
    out, p = [], start
    for _ in range(n):
        p *= 1 + step
        out.append(p)
    return out


class Signals(unittest.TestCase):
    def test_uptrend_buys(self):
        sig = live.signal(rising(), CFG)
        self.assertTrue(sig["uptrend"])
        self.assertTrue(sig["buy"])

    def test_downtrend_does_not_buy(self):
        sig = live.signal(list(reversed(rising())), CFG)
        self.assertFalse(sig["uptrend"])
        self.assertFalse(sig["buy"])

    def test_no_chasing_a_spike(self):
        closes = rising() + [rising()[-1] * 1.12]
        self.assertFalse(live.signal(closes, CFG)["buy"])

    def test_too_little_data(self):
        self.assertIsNone(live.signal([1.0] * 10, CFG))


class Exits(unittest.TestCase):
    up = {"uptrend": True, "ema_fast": 2, "ema_slow": 1}

    def test_stop_loss(self):
        self.assertEqual(live.exit_decision(100, 93.9, 100, False, self.up, CFG)[0], "all")

    def test_trailing_stop(self):
        # up 5% at the high, now 4% off it
        self.assertEqual(live.exit_decision(100, 100.8, 105, False, self.up, CFG)[0], "all")

    def test_no_trail_before_it_starts(self):
        self.assertIsNone(live.exit_decision(100, 98, 101, False, self.up, CFG)[0])

    def test_take_half_once(self):
        self.assertEqual(live.exit_decision(100, 110, 110, False, self.up, CFG)[0], "half")
        self.assertIsNone(live.exit_decision(100, 110, 110, True, self.up, CFG)[0])

    def test_trend_break(self):
        down = {"uptrend": False, "ema_fast": 1, "ema_slow": 2}
        self.assertEqual(live.exit_decision(100, 101, 101, False, down, CFG)[0], "all")


class FakeAlpaca:
    """In-memory stand-in for the Alpaca client."""

    def __init__(self, equity=20.0, cash=20.0, positions=None, closes=None, quotes=None, open_=False):
        self.eq, self.cash, self.pos = equity, cash, positions or []
        self._closes, self._quotes, self.open = closes or {}, quotes or {}, open_
        self.orders = []

    def account(self):
        return {"equity": str(self.eq), "cash": str(self.cash)}

    def positions(self):
        return self.pos

    def market_open(self):
        return self.open

    def quotes(self, crypto, stocks):
        return {k: v for k, v in self._quotes.items() if k in crypto + stocks}

    def closes(self, crypto, stocks):
        return self._closes

    def buy(self, sym, usd, crypto):
        self.orders.append(("buy", sym, usd))

    def sell(self, sym, fraction):
        self.orders.append(("sell", sym, fraction))


def q(mid, spread=0.001):
    return {"bid": mid * (1 - spread / 2), "ask": mid * (1 + spread / 2), "mid": mid, "spread": spread}


class Loop(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = Path(self.dir.name) / "s.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_buys_best_uptrend_and_skips_wide_spread(self):
        up, steeper = rising(), rising(step=0.003)
        api = FakeAlpaca(closes={"BTC/USD": up, "ETH/USD": steeper},
                         quotes={"BTC/USD": q(up[-1]), "ETH/USD": q(steeper[-1], spread=0.02)})
        live.Trader(api, CFG, self.state).tick()
        self.assertEqual(api.orders, [("buy", "BTC/USD", 10.0)])
        self.assertIn("BTCUSD", json.loads(self.state.read_text())["positions"])

    def test_stop_loss_sells(self):
        down = list(reversed(rising()))
        api = FakeAlpaca(positions=[{"symbol": "BTCUSD", "asset_class": "crypto", "qty": "0.0002",
                                     "avg_entry_price": "100000", "current_price": "93000"}],
                         closes={"BTC/USD": down}, quotes={"BTC/USD": q(93000)})
        live.Trader(api, CFG, self.state).tick()
        self.assertEqual(api.orders, [("sell", "BTC/USD", 1)])

    def test_halt_sells_everything_and_stays_halted(self):
        self.state.write_text(json.dumps({"peak_equity": 40.0}))
        pos = [{"symbol": "ETHUSD", "asset_class": "crypto", "qty": "0.01",
                "avg_entry_price": "2500", "current_price": "2400"}]
        api = FakeAlpaca(equity=29.0, cash=5.0, positions=pos,
                         closes={"BTC/USD": rising()}, quotes={"BTC/USD": q(rising()[-1])})
        t = live.Trader(api, CFG, self.state)
        t.tick()
        self.assertEqual(api.orders, [("sell", "ETHUSD", 1)])
        api.orders, api.pos = [], []
        t.tick()
        self.assertEqual(api.orders, [])
        self.assertTrue(json.loads(self.state.read_text())["halted"])

    def test_claude_risk_off_blocks_buys(self):
        up = rising()
        api = FakeAlpaca(closes={"BTC/USD": up}, quotes={"BTC/USD": q(up[-1])})
        brain = lambda summary: {"risk": "off", "allow": [], "note": "choppy"}  # noqa: E731
        live.Trader(api, CFG, self.state, brain).tick()
        self.assertEqual(api.orders, [])

    def test_stocks_ignored_when_market_closed(self):
        up = rising()
        api = FakeAlpaca(closes={"TQQQ": up}, quotes={"TQQQ": q(up[-1])}, open_=False)
        live.Trader(api, CFG, self.state).tick()
        self.assertEqual(api.orders, [])


if __name__ == "__main__":
    unittest.main()
