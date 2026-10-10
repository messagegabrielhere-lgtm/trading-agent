"""Overnight research desk: SEC filings, insider trades, valuations, Berkshire's 13F, a weekly screen.

    python -m agent.research watch           # check every watchlist thesis now
    python -m agent.research value KO        # value one company from its SEC filings
    python -m agent.research berkshire       # latest Berkshire 13F: are its buys still cheap?
    python -m agent.research screen          # value the S&P 500 and debate the cheapest
    python -m agent.research due             # run whatever the schedule says is due (what the bot does)

Jobs (times in UTC; agent/live.py runs them in a background thread when SEC_USER_AGENT is set):
  watch      daily at watch_hour_utc. For each stock in research.watchlist, reads new 10-K, 10-Q
             and 8-K text and Form 4 insider trades, and Claude checks the last few days of
             headlines. It pushes to your phone only when Claude says your thesis is broken.
  berkshire  daily at berkshire_hour_utc, but it only speaks when Berkshire files a new 13F
             (about 45 days after each quarter ends). For each new or enlarged position: the
             price today against the buy-below price, and how many days old the trade is.
  screen     Saturdays at screen_hour_utc. Values every S&P 500 company from 10 years of filings,
             takes the cheapest few, lets the bear attack each one, and sends at most screen_max
             names that Warren still wants to buy. Zero is a normal answer.

Nothing here places orders. It writes to your phone through the same ntfy topic as the trader.

Environment:
  SEC_USER_AGENT      required by the SEC: your name and email, e.g. "Jane Doe jane@example.com"
  ANTHROPIC_API_KEY   required for watch and for the bear/Warren debate
  RESEARCH_STATE      optional, state file path (default ./research_state.json)
"""
import csv
import io
import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

from . import analyst, edgar, valuation

ROOT = Path(__file__).resolve().parent.parent
SP500_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"

DEFAULTS = {
    "watchlist": {},              # {"TICKER": "why you own it, in a sentence or three"}
    "watch_hour_utc": 10,         # 6am New York: before you wake up
    "berkshire_hour_utc": 11,
    "screen_weekday": 5,          # Saturday (Monday is 0)
    "screen_hour_utc": 13,
    "screen_max": 5,              # names sent at most
    "screen_debate": 8,           # cheapest names the bear and Warren look at
    "require_quality": True,      # screen only businesses that pass valuation.quality()
    "universe_url": SP500_URL,
    "valuation": {},              # overrides for agent/valuation.py DEFAULTS
}
FORMS = {"10-K", "10-Q", "8-K", "4"}
EXCERPT = {"8-K": 8000, "10-Q": 15000, "10-K": 25000}


def utcnow():
    return datetime.now(timezone.utc)


def yahoo_price(ticker):
    """Latest close (or last trade) from Yahoo's chart endpoint."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker.replace('.', '-')}?range=5d&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        meta = json.load(r)["chart"]["result"][0]["meta"]
    return float(meta["regularMarketPrice"])


def sp500(url=SP500_URL):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        rows = csv.DictReader(io.StringIO(r.read().decode()))
        return [row["Symbol"].strip().upper() for row in rows if row.get("Symbol")]


def load_config():
    cfg = json.loads((ROOT / "config.json").read_text()).get("research", {})
    return {**DEFAULTS, **cfg}


class Desk:
    """The research jobs. Network and Claude calls go through attributes so tests can swap them."""

    def __init__(self, cfg, state_path, alert, ed=edgar, an=analyst, price=yahoo_price, universe=sp500):
        self.cfg, self.path, self.alert = {**DEFAULTS, **cfg}, Path(state_path), alert
        self.ed, self.an, self.price, self.universe = ed, an, price, universe
        try:
            self.s = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.s = {}
        for key in ("seen", "last_run", "theses"):
            self.s.setdefault(key, {})

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, indent=1))
        tmp.replace(self.path)

    # ---------- valuation ----------

    def value(self, ticker):
        """{"ticker", "company", "price", "valuation", "quality", "verdict"} for one stock."""
        cik = self.ed.cik_for(ticker)
        if not cik:
            return {"ticker": ticker, "valuation": {"ok": False, "reason": "not found in SEC ticker list"}}
        company = self.ed.ticker_map().get(ticker.upper(), {}).get("title", ticker)
        years = self.ed.annual_facts(self.ed.company_facts(cik))
        val = valuation.value(years, self.cfg["valuation"])
        price = self.price(ticker) if val["ok"] else None
        return {"ticker": ticker, "company": company, "price": price, "valuation": val,
                "quality": valuation.quality(years, self.cfg["valuation"]),
                "verdict": valuation.verdict(price, val)}

    # ---------- watch: thesis checks ----------

    def watch(self):
        """Check every watchlist thesis. Pushes only when Claude says a thesis is broken."""
        broken = []
        for ticker, thesis in self.cfg["watchlist"].items():
            cik = self.ed.cik_for(ticker)
            if not cik:
                self.alert(f"Research: {ticker} isn't in the SEC's ticker list, so it can't be watched.")
                continue
            filings = self.ed.recent_filings(cik, FORMS)[:40]
            seen = set(self.s["seen"].get(ticker, []))
            first_run = ticker not in self.s["seen"]
            new = [] if first_run else [f for f in filings if f["accession"] not in seen]
            events = []
            for f in new[:12]:
                if f["form"] == "4":
                    trades = self.ed.insider_trades(f)
                    if trades:
                        events.append({"form": "4", "filed": f["date"], "trades": trades})
                else:
                    events.append({"form": f["form"], "filed": f["date"],
                                   "excerpt": self.ed.filing_text(f, EXCERPT.get(f["form"], 8000))})
            # Headlines alone can break a thesis, so Claude looks every day, filings or not.
            verdict = self.an.thesis_check(ticker, thesis, events)
            self.s["seen"][ticker] = sorted(seen | {f["accession"] for f in filings})[-200:]
            self.s["theses"][ticker] = {"checked": utcnow().isoformat(timespec="seconds"), **verdict}
            self._save()
            if verdict["thesis_broken"]:
                broken.append(ticker)
                self.alert(f"THESIS BROKEN: {ticker}. {verdict['summary']} Evidence: {verdict['evidence']}"[:1500])
        return broken

    # ---------- berkshire: 13F ----------

    def berkshire(self, force=False):
        """Report Berkshire's new and enlarged positions once per new 13F. Returns the lines sent."""
        filings = self.ed.thirteen_f(edgar.BERKSHIRE_CIK, 2)
        if not filings:
            return None
        latest = filings[0]
        if latest["accession"] == self.s.get("berkshire_seen") and not force:
            return None
        before = filings[1]["holdings"] if len(filings) > 1 else {}
        quarter_end = date.fromisoformat(latest["period"]) if latest.get("period") else None
        age = (utcnow().date() - quarter_end).days if quarter_end else None
        lines = []
        for cusip, h in sorted(latest["holdings"].items(), key=lambda kv: -kv[1]["value"]):
            old = before.get(cusip)
            if old and h["shares"] <= old["shares"] * 1.001:
                continue
            kind = "new" if not old else f"added {h['shares'] / old['shares'] - 1:+.0%}"
            ticker = self.ed.ticker_for_name(h["name"])
            if not ticker:
                lines.append(f"{h['name']} ({kind}): couldn't match a ticker")
                continue
            r = self.value(ticker)
            v, vd = r["valuation"], r.get("verdict")
            if not v["ok"]:
                lines.append(f"{ticker} ({kind}): can't value, {v['reason']}")
            elif vd["label"] == "cheap":
                q = r["quality"]
                lines.append(f"{ticker} ({kind}): STILL CHEAP at ${r['price']:.2f}, buy below ${v['buy_below']:.2f}, "
                             f"quality {q['score']}{'' if q['passed'] else ' (fails the checklist)'}")
            else:
                lines.append(f"{ticker} ({kind}): too late, ${r['price']:.2f} is above the buy-below ${v['buy_below']:.2f}")
        self.s["berkshire_seen"] = latest["accession"]
        self._save()
        head = (f"Berkshire 13F filed {latest['date']} for the quarter ending {latest.get('period') or '?'}"
                + (f": these trades are up to {age} days old." if age is not None else "."))
        self.alert(head + "\n" + ("\n".join(lines[:12]) if lines else "No new or enlarged positions."))
        return lines

    # ---------- screen: weekly S&P 500 ----------

    def screen(self):
        """Value the universe, debate the cheapest, push at most screen_max names. Returns the picks."""
        tickers = self.universe(self.cfg["universe_url"])
        cheap, valued, weak = [], 0, 0
        for t in tickers:
            try:
                r = self.value(t)
            except Exception as e:  # one bad filing must not sink the whole screen
                print(f"screen: {t} failed: {e}", flush=True)
                continue
            if r["valuation"].get("ok"):
                valued += 1
                if r["verdict"] and r["verdict"]["label"] == "cheap":
                    if self.cfg["require_quality"] and not r["quality"]["passed"]:
                        weak += 1  # cheap but not a wonderful business: a value trap until proven otherwise
                    else:
                        cheap.append(r)
        cheap.sort(key=lambda r: -r["verdict"]["discount"])
        picks = []
        for r in cheap[: self.cfg["screen_debate"]]:
            d = self.an.debate(r["ticker"], r["company"], {**r["valuation"], "quality": r["quality"]}, r["price"])
            if d["buy"]:
                picks.append({**r, "reason": d["reason"]})
                if len(picks) >= self.cfg["screen_max"]:
                    break
        self.s["last_screen"] = {"date": utcnow().date().isoformat(), "universe": len(tickers), "valued": valued,
                                 "cheap": [r["ticker"] for r in cheap], "cheap_but_weak": weak, "picks": [p["ticker"] for p in picks]}
        self._save()
        if picks:
            lines = [f"{p['ticker']} ${p['price']:.2f} (buy below ${p['valuation']['buy_below']:.2f}, "
                     f"{p['verdict']['discount']:.0%} under value): {p['reason']}" for p in picks]
            self.alert(f"Saturday screen: {len(picks)} to look at.\n" + "\n".join(lines))
        else:
            self.alert(f"Saturday screen: nothing to buy. {valued} of {len(tickers)} companies could be valued, "
                       f"{len(cheap) + weak} were under their buy-below price ({weak} failed the quality checklist), "
                       f"none survived the bear and Warren.")
        return picks

    # ---------- schedule ----------

    def due(self, now=None):
        """Run each job whose time has come today and hasn't run yet today."""
        now = now or utcnow()
        jobs = [("watch", self.cfg["watch_hour_utc"], None, self.watch),
                ("berkshire", self.cfg["berkshire_hour_utc"], None, self.berkshire),
                ("screen", self.cfg["screen_hour_utc"], self.cfg["screen_weekday"], self.screen)]
        ran = []
        for name, hour, weekday, job in jobs:
            if now.hour < hour or (weekday is not None and now.weekday() != weekday):
                continue
            if self.s["last_run"].get(name) == now.date().isoformat():
                continue
            if name == "watch" and not self.cfg["watchlist"]:
                continue
            self.s["last_run"][name] = now.date().isoformat()  # mark first: a crash shouldn't retry all day
            self._save()
            try:
                job()
                ran.append(name)
            except Exception as e:
                self.alert(f"Research {name} failed: {str(e)[:300]}")
        return ran


def loop(cfg, state_path, alert, every=600):
    """What agent/live.py runs in a background thread."""
    desk = Desk(cfg, state_path, alert)
    while True:
        try:
            desk.due()
        except Exception as e:
            alert(f"Research loop error: {str(e)[:300]}")
        time.sleep(every)


def main(argv):
    from .live import alert
    cfg = load_config()
    desk = Desk(cfg, os.environ.get("RESEARCH_STATE", "research_state.json"), alert)
    cmd = argv[0] if argv else "due"
    if cmd == "value":
        for t in argv[1:]:
            print(json.dumps(desk.value(t.upper()), indent=1))
    elif cmd == "watch":
        desk.watch()
    elif cmd == "berkshire":
        desk.berkshire(force=True)
    elif cmd == "screen":
        desk.screen()
    elif cmd == "due":
        print("ran:", desk.due())
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
