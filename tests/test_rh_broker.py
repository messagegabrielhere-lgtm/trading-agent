import json
import tempfile
import unittest
from pathlib import Path

from agent import live
from agent.rh_broker import RobinhoodCrypto, round_down


def q(bid, ask):
    mid = (bid + ask) / 2
    return {"bid": bid, "ask": ask, "mid": mid, "spread": (ask - bid) / mid}


class DryRunBook(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.book = Path(self.dir.name) / "book.json"
        self.rh = RobinhoodCrypto(None, None, self.book, budget_usd=20, live=False)

    def tearDown(self):
        self.dir.cleanup()

    def test_buy_then_sell_tracks_cash_and_pnl(self):
        self.rh._last_quotes = {"SOL/USD": q(99, 101)}
        self.rh.buy("SOL/USD", 10, True)
        self.assertAlmostEqual(float(self.rh.account()["cash"]), 10.0)
        [p] = self.rh.positions()
        self.assertEqual(p["symbol"], "SOLUSD")
        self.assertAlmostEqual(float(p["avg_entry_price"]), 101)

        self.rh._last_quotes = {"SOL/USD": q(110, 112)}
        self.rh.sell("SOLUSD", 0.5)
        self.rh.sell("SOL/USD", 1)
        self.assertEqual(self.rh.positions(), [])
        acct = self.rh.account()
        self.assertAlmostEqual(float(acct["equity"]), 20 + 10 / 101 * 9, places=6)
        self.assertEqual(json.loads(self.book.read_text())["coins"], {})

    def test_never_sells_coins_it_did_not_buy(self):
        self.assertIsNone(self.rh.sell("BTC/USD", 1))

    def test_book_survives_restart(self):
        self.rh._last_quotes = {"ETH/USD": q(2000, 2040)}
        self.rh.buy("ETH/USD", 5, True)
        again = RobinhoodCrypto(None, None, self.book, budget_usd=20, live=False)
        self.assertEqual(len(again.positions()), 1)

    def test_round_down(self):
        self.assertEqual(round_down(0.123456789, "0.00001"), "0.12345")
        self.assertEqual(round_down(12.9, "1"), "12")


class Config(unittest.TestCase):
    def test_robinhood_overrides_apply_only_to_robinhood(self):
        rh, alp = live.load_config("robinhood"), live.load_config("alpaca")
        self.assertGreater(rh["max_spread"], alp["max_spread"])
        self.assertNotIn("robinhood", alp)


if __name__ == "__main__":
    unittest.main()


class EndToEndDryRun(unittest.TestCase):
    """One Trader tick through the Robinhood adapter, with the public price feeds faked."""

    def test_buys_an_uptrend_with_robinhood_settings(self):
        from unittest import mock
        from agent import rh_broker

        closes = [100 * 1.004 ** i for i in range(80)]  # steady climb, about 6% over 4 hours

        def fake_http(req, tries=4):
            url = req.full_url
            if "/candles" in url:
                if "SOL-USD" not in url:
                    return []
                return [[1_700_000_000 + 900 * i, c, c, c, c, 1] for i, c in enumerate(closes)][::-1]
            if "/ticker" in url:
                return {"price": str(closes[-1])}
            raise AssertionError(url)

        with tempfile.TemporaryDirectory() as d, mock.patch.object(rh_broker, "http_json", fake_http):
            cfg = live.load_config("robinhood")
            cfg["crypto"] = ["SOL/USD", "BTC/USD"]
            rh = RobinhoodCrypto(None, None, Path(d) / "book.json", budget_usd=20, live=False)
            live.Trader(rh, cfg, Path(d) / "state.json").tick()
            [p] = rh.positions()
            self.assertEqual(p["symbol"], "SOLUSD")
            self.assertAlmostEqual(float(rh.account()["cash"]), 10.0, places=2)
