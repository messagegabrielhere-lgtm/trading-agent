"""Daily price history from Yahoo Finance's chart endpoint (no API key)."""
import json
import time
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"
NY = ZoneInfo("America/New_York")


def fetch(sym, rng="10y"):
    """Return {date: (close, adjclose)} for one symbol."""
    req = urllib.request.Request(URL.format(sym=sym, rng=rng), headers={"User-Agent": "Mozilla/5.0"})
    last_err = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.load(r)["chart"]["result"][0]
            break
        except Exception as e:  # network / rate limit: back off and retry
            last_err = e
            time.sleep(2 * (attempt + 1))
    else:
        raise RuntimeError(f"could not fetch {sym}: {last_err}")

    closes = res["indicators"]["quote"][0]["close"]
    adjs = res["indicators"]["adjclose"][0]["adjclose"]
    out = {}
    for ts, c, a in zip(res["timestamp"], closes, adjs):
        if c is None or a is None:
            continue
        out[datetime.fromtimestamp(ts, timezone.utc).astimezone(NY).date().isoformat()] = (c, a)

    # Drop today's bar while the session is still open: it is not a close yet.
    now = datetime.now(NY)
    if (now.hour, now.minute) < (16, 15):
        out.pop(now.date().isoformat(), None)
    return out


def load(symbols, rng="10y"):
    """Fetch all symbols and align them on the dates they share.

    Returns (dates, close, adj) where close/adj map symbol -> list aligned to dates.
    """
    raw = {s: fetch(s, rng) for s in symbols}
    dates = sorted(set.intersection(*(set(v) for v in raw.values())))
    close = {s: [raw[s][d][0] for d in dates] for s in symbols}
    adj = {s: [raw[s][d][1] for d in dates] for s in symbols}
    return dates, close, adj
