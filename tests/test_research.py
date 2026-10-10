import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agent import edgar, research, valuation


def steady_years(n=10, fcf=10.0, growth=0.08, shares=100.0, cash=500.0, debt=200.0):
    """A boring, compounding business: free cash flow per share grows `growth` a year."""
    out = {}
    for i in range(n):
        f = fcf * (1 + growth) ** i * shares
        out[2015 + i] = {"ocf": f + 50.0, "capex": 50.0, "shares": shares, "cash": cash, "debt": debt}
    return out


class Valuation(unittest.TestCase):
    def test_values_a_steady_compounder(self):
        v = valuation.value(steady_years())
        self.assertTrue(v["ok"])
        self.assertAlmostEqual(v["growth"], 0.08, places=3)
        self.assertEqual(v["years"][0], 2015)
        self.assertGreater(v["intrinsic"], v["base_fcf_per_share"] * 10)
        self.assertAlmostEqual(v["buy_below"], v["intrinsic"] * 0.7, places=1)
        self.assertEqual(v["net_cash_per_share"], 3.0)

    def test_growth_is_capped(self):
        self.assertEqual(valuation.value(steady_years(growth=0.40))["growth"], 0.12)

    def test_refuses_short_history(self):
        v = valuation.value(steady_years(n=5))
        self.assertFalse(v["ok"])
        self.assertIn("5 years", v["reason"])

    def test_refuses_erratic_cash_flow(self):
        years = steady_years()
        for y in (2016, 2018, 2020):
            years[y]["ocf"] = 0.0
        self.assertFalse(valuation.value(years)["ok"])

    def test_verdict_labels(self):
        v = valuation.value(steady_years())
        self.assertEqual(valuation.verdict(v["buy_below"] - 1, v)["label"], "cheap")
        self.assertEqual(valuation.verdict(v["intrinsic"] - 1, v)["label"], "fair")
        self.assertEqual(valuation.verdict(v["intrinsic"] * 2, v)["label"], "expensive")
        self.assertIsNone(valuation.verdict(10, {"ok": False}))


def fact(val, end, start=None, form="10-K", filed="2025-02-01"):
    r = {"val": val, "end": end, "form": form, "filed": filed}
    if start:
        r["start"] = start
    return r


class Edgar(unittest.TestCase):
    def test_annual_facts_keeps_full_years_and_latest_restatement(self):
        facts = {"facts": {"us-gaap": {
            "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
                fact(100, "2023-12-31", "2023-01-01", filed="2024-02-01"),
                fact(110, "2023-12-31", "2023-01-01", filed="2025-02-01"),  # restated next year
                fact(30, "2023-09-30", "2023-07-01", form="10-Q"),            # a quarter: ignored
            ]}},
            "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [fact(20, "2023-12-31", "2023-01-01")]}},
            "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": [fact(10, "2023-12-31", "2023-01-01")]}},
            "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [fact(5, "2023-12-31")]}},
        }}}
        self.assertEqual(edgar.annual_facts(facts), {2023: {"ocf": 110.0, "capex": 20.0, "shares": 10.0, "cash": 5.0}})

    def test_parse_submissions_filters_forms(self):
        sub = {"filings": {"recent": {
            "form": ["8-K", "4", "SC 13G"], "accessionNumber": ["0001-24-1", "0001-24-2", "0001-24-3"],
            "primaryDocument": ["a.htm", "xslF345X05/f4.xml", "b.htm"], "filingDate": ["2025-01-02"] * 3,
            "reportDate": ["2025-01-01", "", ""]}}}
        out = edgar.parse_submissions(sub, 320193, {"8-K", "4"})
        self.assertEqual([f["form"] for f in out], ["8-K", "4"])
        self.assertEqual(out[0]["url"], "https://www.sec.gov/Archives/edgar/data/320193/0001241/a.htm")

    def test_html_to_text(self):
        raw = "<html><style>x{}</style><p>Net&nbsp;sales <b>rose</b> 5%</p><script>bad()</script></html>"
        self.assertEqual(edgar.html_to_text(raw), "Net sales rose 5%")

    def test_parse_form4_keeps_open_market_trades_only(self):
        xml = """<ownershipDocument>
          <reportingOwner><reportingOwnerId><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
            <reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>CEO</officerTitle></reportingOwnerRelationship>
          </reportingOwner>
          <nonDerivativeTable>
            <nonDerivativeTransaction><transactionCoding><transactionCode>P</transactionCode></transactionCoding>
              <transactionAmounts><transactionShares><value>1000</value></transactionShares>
              <transactionPricePerShare><value>50.5</value></transactionPricePerShare></transactionAmounts>
            </nonDerivativeTransaction>
            <nonDerivativeTransaction><transactionCoding><transactionCode>M</transactionCode></transactionCoding>
              <transactionAmounts><transactionShares><value>9</value></transactionShares></transactionAmounts>
            </nonDerivativeTransaction>
          </nonDerivativeTable></ownershipDocument>"""
        self.assertEqual(edgar.parse_form4(xml), [
            {"owner": "Doe Jane", "title": "CEO", "code": "P", "shares": 1000.0, "price": 50.5, "side": "buy"}])

    def test_parse_13f_sums_managers_and_skips_options(self):
        xml = """<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
          <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><cusip>037833100</cusip><value>100</value>
            <shrsOrPrnAmt><sshPrnamt>10</sshPrnamt></shrsOrPrnAmt></infoTable>
          <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><cusip>037833100</cusip><value>50</value>
            <shrsOrPrnAmt><sshPrnamt>5</sshPrnamt></shrsOrPrnAmt></infoTable>
          <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><cusip>037833100</cusip><value>9</value>
            <shrsOrPrnAmt><sshPrnamt>1</sshPrnamt></shrsOrPrnAmt><putCall>Put</putCall></infoTable>
        </informationTable>"""
        self.assertEqual(edgar.parse_13f(xml)["037833100"],
                         {"name": "APPLE INC", "cusip": "037833100", "value": 150.0, "shares": 15.0})

    def test_ticker_for_name(self):
        saved = edgar._tickers
        edgar._tickers = {"AAPL": {"cik": 1, "title": "Apple Inc."}, "KO": {"cik": 2, "title": "COCA COLA CO"}}
        try:
            self.assertEqual(edgar.ticker_for_name("APPLE INC"), "AAPL")
            self.assertEqual(edgar.ticker_for_name("COCA-COLA CO"), "KO")
            self.assertIsNone(edgar.ticker_for_name("UNKNOWN WIDGETS"))
        finally:
            edgar._tickers = saved


class FakeEdgar:
    """In-memory stand-in for agent/edgar.py."""

    def __init__(self, companies, filings=None, f4=None, thirteen=None):
        self.companies = companies  # ticker -> (cik, title, years)
        self.filings, self.f4, self.thirteen = filings or {}, f4 or [], thirteen or []
        self.texts_read = []

    def ticker_map(self):
        return {t: {"cik": c, "title": n} for t, (c, n, _) in self.companies.items()}

    def cik_for(self, t):
        return self.companies.get(t, (None,))[0]

    def company_facts(self, cik):
        return cik

    def annual_facts(self, cik):
        return next(y for c, _, y in self.companies.values() if c == cik)

    def recent_filings(self, cik, forms):
        return self.filings.get(cik, [])

    def filing_text(self, f, limit):
        self.texts_read.append(f["accession"])
        return f"text of {f['form']}"

    def insider_trades(self, f):
        return self.f4

    def thirteen_f(self, cik, count):
        return self.thirteen

    def ticker_for_name(self, name):
        return {"APPLE INC": "AAPL", "COCA COLA CO": "KO"}.get(name)


class FakeAnalyst:
    def __init__(self, broken=False, buy=()):
        self.broken, self.buy, self.checks, self.debated = broken, set(buy), [], []

    def thesis_check(self, ticker, thesis, events):
        self.checks.append((ticker, events))
        return {"thesis_broken": self.broken, "severity": "broken" if self.broken else "none",
                "summary": "Guidance cut 30%." if self.broken else "Nothing new.", "evidence": "8-K item 2.02"}

    def debate(self, ticker, company, val, price):
        self.debated.append(ticker)
        return {"buy": ticker in self.buy, "reason": "Durable and cheap."}


def filing(acc, form="8-K"):
    return {"accession": acc, "form": form, "date": "2026-10-09", "doc": "x.htm", "url": "u", "cik": 1}


class Desk(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = Path(self.dir.name) / "r.json"
        self.sent = []

    def tearDown(self):
        self.dir.cleanup()

    def desk(self, ed, an, prices=None, universe=None, **cfg):
        return research.Desk(cfg, self.state, self.sent.append, ed=ed, an=an,
                             price=lambda t: (prices or {}).get(t, 100.0),
                             universe=lambda url: universe or [])

    def test_watch_baselines_then_reads_only_new_filings_and_stays_quiet(self):
        ed = FakeEdgar({"KO": (1, "Coca-Cola", {})}, filings={1: [filing("a"), filing("b", "4")]},
                       f4=[{"side": "sell", "shares": 10}])
        an = FakeAnalyst()
        d = self.desk(ed, an, watchlist={"KO": "Brand moat, pricing power."})
        d.watch()
        self.assertEqual(an.checks[-1], ("KO", []))  # first run: baseline, headlines only
        ed.filings[1] = [filing("c"), filing("a"), filing("b", "4")]
        d.watch()
        self.assertEqual([e["form"] for e in an.checks[-1][1]], ["8-K"])
        self.assertEqual(ed.texts_read, ["c"])
        self.assertEqual(self.sent, [])  # not broken: no push

    def test_watch_pushes_when_thesis_breaks(self):
        ed = FakeEdgar({"KO": (1, "Coca-Cola", {})}, filings={1: [filing("a")]})
        d = self.desk(ed, FakeAnalyst(broken=True), watchlist={"KO": "Brand moat."})
        self.assertEqual(d.watch(), ["KO"])
        self.assertTrue(self.sent[0].startswith("THESIS BROKEN: KO. Guidance cut 30%."))

    def test_berkshire_reports_new_and_added_once(self):
        cheap = steady_years()
        v = valuation.value(cheap)
        ed = FakeEdgar({"AAPL": (1, "Apple", cheap), "KO": (2, "Coca-Cola", cheap)}, thirteen=[
            {"accession": "new", "date": "2026-08-14", "period": "2026-06-30", "holdings": {
                "a": {"name": "APPLE INC", "value": 9, "shares": 120.0},
                "k": {"name": "COCA COLA CO", "value": 5, "shares": 50.0},
                "z": {"name": "MYSTERY CORP", "value": 1, "shares": 1.0}}},
            {"accession": "old", "date": "2026-05-15", "period": "2026-03-31", "holdings": {
                "a": {"name": "APPLE INC", "value": 8, "shares": 100.0},
                "k": {"name": "COCA COLA CO", "value": 5, "shares": 50.0}}}])
        d = self.desk(ed, FakeAnalyst(), prices={"AAPL": v["buy_below"] - 1})
        lines = d.berkshire()
        self.assertEqual(len(lines), 2)
        self.assertIn("AAPL (added +20%): STILL CHEAP", lines[0])
        self.assertIn("MYSTERY CORP (new): couldn't match a ticker", lines[1])
        self.assertIn("days old", self.sent[0])
        self.assertIsNone(d.berkshire())  # same 13F: silent
        self.assertEqual(len(self.sent), 1)

    def test_screen_sends_at_most_max_and_zero_is_ok(self):
        good, v = steady_years(), valuation.value(steady_years())
        companies = {t: (i, t, good) for i, t in enumerate(["A", "B", "C", "D", "E", "F", "G"], 1)}
        companies["BAD"] = (99, "BAD", steady_years(n=3))
        cheap_px = {t: v["buy_below"] * (0.5 + i / 100) for i, t in enumerate("ABCDEFG")}
        an = FakeAnalyst(buy="ABCDEF")
        d = self.desk(FakeEdgar(companies), an, prices=cheap_px, universe=list(companies), screen_max=5)
        picks = d.screen()
        self.assertEqual([p["ticker"] for p in picks], ["A", "B", "C", "D", "E"])  # deepest discount first, max 5
        self.assertTrue(self.sent[-1].startswith("Saturday screen: 5 to look at."))
        an2 = FakeAnalyst(buy=())
        d2 = self.desk(FakeEdgar(companies), an2, prices=cheap_px, universe=list(companies), screen_debate=3)
        self.assertEqual(d2.screen(), [])
        self.assertEqual(an2.debated, ["A", "B", "C"])
        self.assertIn("nothing to buy", self.sent[-1])

    def test_due_runs_each_job_once_a_day_on_schedule(self):
        ed = FakeEdgar({"KO": (1, "Coca-Cola", {})}, filings={1: []})
        d = self.desk(ed, FakeAnalyst(), watchlist={"KO": "moat"})
        d.berkshire = lambda force=False: None
        d.screen = lambda: []
        sat_noon = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(d.due(sat_noon), ["watch", "berkshire"])  # screen is at 13:00
        self.assertEqual(d.due(sat_noon.replace(hour=14)), ["screen"])
        self.assertEqual(d.due(sat_noon.replace(hour=15)), [])
        self.assertEqual(json.loads(self.state.read_text())["last_run"]["screen"], "2026-10-10")

    def test_job_failure_alerts_and_is_not_retried_all_day(self):
        d = self.desk(FakeEdgar({}), FakeAnalyst())

        def boom(force=False):
            raise RuntimeError("SEC down")
        d.berkshire = boom
        when = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
        d.due(when)
        d.due(when.replace(hour=13))
        self.assertEqual(self.sent, ["Research berkshire failed: SEC down"])


if __name__ == "__main__":
    unittest.main()
