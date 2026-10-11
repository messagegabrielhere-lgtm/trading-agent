import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from agent import research, signals


def staircase(cycles=11, base=100.0, follow=0.01, end_on_breakout=True):
    """Flat 20-day bases, each ended by a high-volume breakout and `follow` per day of follow-through."""
    bars, day, px = [], date(2025, 1, 1), base

    def add(close, vol, hi=None, lo=None):
        nonlocal day
        bars.append((day.isoformat(), close, hi or close * 1.005, lo or close * 0.995, vol))
        day += timedelta(days=1)

    for c in range(cycles):
        for _ in range(20):
            add(px, 1e6)
        if c == cycles - 1 and end_on_breakout:
            add(px * 1.03, 3e6)
            break
        px *= 1.03
        add(px, 3e6)
        for _ in range(5):
            px *= 1 + follow
            add(px, 1e6)
    return bars


class Detect(unittest.TestCase):
    def test_breakout_on_volume_fires_and_quiet_day_does_not(self):
        bars = staircase()
        self.assertIn("breakout", signals.events_at(bars, len(bars) - 1))
        self.assertEqual(signals.events_at(bars, len(bars) - 2), [])

    def test_backtest_learns_from_this_tickers_history(self):
        good = signals.backtest(staircase(), "breakout", 5)
        self.assertGreaterEqual(good["n"], 5)
        self.assertEqual(good["hit_rate"], 1.0)
        bad = signals.backtest(staircase(follow=-0.01), "breakout", 5)
        self.assertLess(bad["avg"], 0)


class Scan(unittest.TestCase):
    def test_survivor_gets_a_two_to_one_plan(self):
        surv, _ = signals.scan(staircase())
        kinds = {s["kind"] for s in surv}
        self.assertIn("breakout", kinds)
        p = next(s for s in surv if s["kind"] == "breakout")["plan"]
        self.assertAlmostEqual(p["target"] - p["entry"], 2 * (p["entry"] - p["stop"]), places=1)

    def test_failed_history_thin_volume_and_weak_relative_strength_are_rejected(self):
        surv, rej = signals.scan(staircase(follow=-0.01))
        self.assertNotIn("breakout", {s["kind"] for s in surv})
        self.assertTrue(any("history says no" in why for _, why in rej))
        _, rej = signals.scan(staircase(), {"min_dollar_volume": 1e12})
        self.assertTrue(all(why.startswith("thin") for _, why in rej))
        surv, rej = signals.scan(staircase(), spy_ret_20d=0.50)
        self.assertEqual(surv, [])
        self.assertTrue(any("weaker than the S&P" in why for _, why in rej))

    def test_short_history_is_skipped(self):
        self.assertEqual(signals.scan(staircase()[:60])[0], [])


def flat_market(n=260):
    return [((date(2025, 1, 1) + timedelta(days=i)).isoformat(), 100.0, 100.5, 99.5, 1e6) for i in range(n)]


class FakeAn:
    def __init__(self, approve=True):
        self.approve, self.calls = approve, []

    def validate_signal(self, ticker, signal, filings):
        self.calls.append(ticker)
        return {"approve": self.approve, "confidence": "high", "catalyst": "beat and raise",
                "risk": "market", "reason": "Raised guidance."}


class NoSec:
    def cik_for(self, t):
        return None


class DeskScan(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.sent = []

    def tearDown(self):
        self.dir.cleanup()

    def desk(self, data, an, **cfg):
        cfg = {"scan_universe": list(data), "scan_max": 2, "scan_validate": 3, **cfg}
        return research.Desk(cfg, Path(self.dir.name) / "s.json", self.sent.append, ed=NoSec(), an=an,
                             history=lambda t: data[t])

    def test_funnel_sends_validated_setups_once_and_scores_them_later(self):
        flat = flat_market()
        data = {"SPY": flat, "AAA": staircase(), "BBB": staircase(), "CCC": staircase(), "DDD": flat}
        an = FakeAn()
        d = self.desk(data, an)
        sent = d.scan()
        self.assertEqual(len(sent), 2)                     # scan_max
        self.assertEqual(len(an.calls), 2)                 # Claude only sees survivors, stops at the max
        self.assertTrue(self.sent[-1].startswith("Scan: 2 setup(s)."))
        self.assertIn("stop", self.sent[-1])
        # Same bars again: nothing changed, nothing new to validate.
        self.assertEqual(d.scan(), [])
        self.assertIn("nothing survived", self.sent[-1])
        # Five rising days later the earlier signals get scored.
        for t in ("AAA", "BBB"):
            last = data[t][-1]
            data[t] = data[t] + [((date.fromisoformat(last[0]) + timedelta(days=i)).isoformat(),
                                  last[1] * 1.01 ** i, last[1] * 1.01 ** i, last[1] * 1.01 ** i * 0.999, 1e6)
                                 for i in range(1, 7)]
        d.scan()
        rec = d.track_record()
        self.assertEqual(rec["scored"], 2)
        self.assertEqual(rec["wins"], 2)

    def test_claude_veto_means_no_message_but_a_funnel_report(self):
        data = {"SPY": flat_market(), "AAA": staircase()}
        self.assertEqual(self.desk(data, FakeAn(approve=False)).scan(), [])
        self.assertIn("1 checked by Claude, 0 sent", self.sent[-1])


if __name__ == "__main__":
    unittest.main()
