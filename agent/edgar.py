"""SEC EDGAR client for agent/research.py (standard library only).

Everything comes from the SEC's free public APIs:
  * company_tickers.json      ticker -> CIK
  * submissions/CIK*.json     each company's recent filings (10-K, 10-Q, 8-K, Form 4, 13F-HR)
  * companyfacts/CIK*.json    every XBRL number the company has reported, by fiscal year
  * Archives/...              the filing documents themselves

The SEC asks every client to send a User-Agent naming the person and an email address, and to stay
under 10 requests a second. Set SEC_USER_AGENT, e.g. "Jane Doe jane@example.com".
"""
import html
import json
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date

WWW = "https://www.sec.gov"
DATA = "https://data.sec.gov"
BERKSHIRE_CIK = 1067983

_last_request = 0.0


def _get(url):
    global _last_request
    agent = os.environ.get("SEC_USER_AGENT")
    if not agent:
        raise RuntimeError("SEC_USER_AGENT is not set (the SEC requires a name and email, e.g. 'Jane Doe jane@example.com')")
    req = urllib.request.Request(url, headers={"User-Agent": agent, "Accept-Encoding": "identity"})
    for attempt in range(4):
        wait = 0.12 - (time.monotonic() - _last_request)  # about 8 requests a second at most
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code not in (403, 429, 500, 502, 503) or attempt == 3:
                raise RuntimeError(f"GET {url} -> HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 3:
                raise RuntimeError(f"GET {url} -> {e}") from None
        time.sleep(2 ** attempt)


def get_json(url):
    body = _get(url)
    return None if body is None else json.loads(body)


def get_text(url):
    body = _get(url)
    return None if body is None else body.decode("utf-8", errors="replace")


# ---------- companies and filings ----------

_tickers = None


def ticker_map():
    """{"AAPL": {"cik": 320193, "title": "Apple Inc."}, ...}"""
    global _tickers
    if _tickers is None:
        raw = get_json(f"{WWW}/files/company_tickers.json") or {}
        _tickers = {r["ticker"].upper(): {"cik": int(r["cik_str"]), "title": r["title"]} for r in raw.values()}
    return _tickers


def cik_for(ticker):
    hit = ticker_map().get(ticker.upper().replace(".", "-"))
    return hit["cik"] if hit else None


def filing_url(cik, accession, doc):
    return f"{WWW}/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{doc}"


def recent_filings(cik, forms=None):
    """Recent filings, newest first: [{form, date, accession, doc, url, period}]."""
    sub = get_json(f"{DATA}/submissions/CIK{int(cik):010d}.json") or {}
    return parse_submissions(sub, cik, forms)


def parse_submissions(sub, cik, forms=None):
    r = sub.get("filings", {}).get("recent", {})
    out = []
    for i, form in enumerate(r.get("form", [])):
        if forms and form not in forms:
            continue
        acc, doc = r["accessionNumber"][i], r["primaryDocument"][i]
        out.append({"form": form, "date": r["filingDate"][i], "accession": acc, "doc": doc, "cik": int(cik),
                    "period": (r.get("reportDate") or [""] * (i + 1))[i],
                    "url": filing_url(cik, acc, doc)})
    return out


def html_to_text(raw, limit=None):
    """Plain text from a filing's HTML (or XBRL-inline HTML)."""
    raw = re.sub(r"(?is)<(script|style|ix:header)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", html.unescape(raw)).strip()
    return text[:limit] if limit else text


def filing_text(f, limit=30000):
    raw = get_text(f["url"])
    return html_to_text(raw, limit) if raw else ""


# ---------- insider trades (Form 4) ----------

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _find(node, path):
    """Namespace-agnostic find along a slash path; returns the element's text or None."""
    for part in path.split("/"):
        node = next((c for c in (node if node is not None else []) if _local(c.tag) == part), None)
    return node.text.strip() if node is not None and node.text else None


def parse_form4(xml_text):
    """Open-market trades in a Form 4: [{owner, title, code, shares, price, side}] (P buy, S sell only)."""
    root = ET.fromstring(xml_text)
    owner = _find(root, "reportingOwner/reportingOwnerId/rptOwnerName") or "?"
    rel = next((c for c in root.iter() if _local(c.tag) == "reportingOwnerRelationship"), None)
    title = (_find(rel, "officerTitle") if rel is not None else None) or (
        "director" if rel is not None and _find(rel, "isDirector") in ("1", "true") else "")
    trades = []
    for t in (c for c in root.iter() if _local(c.tag) == "nonDerivativeTransaction"):
        code = _find(t, "transactionCoding/transactionCode")
        if code not in ("P", "S"):  # skip grants, option exercises, tax withholding, gifts
            continue
        shares = float(_find(t, "transactionAmounts/transactionShares/value") or 0)
        price = float(_find(t, "transactionAmounts/transactionPricePerShare/value") or 0)
        trades.append({"owner": owner, "title": title, "code": code, "shares": shares, "price": price,
                       "side": "buy" if code == "P" else "sell"})
    return trades


def insider_trades(f):
    """Parse the raw XML behind a Form 4 (primaryDocument usually points at an XSL-rendered copy)."""
    xml_text = get_text(filing_url(f["cik"], f["accession"], f["doc"].split("/")[-1]))
    return parse_form4(xml_text) if xml_text else []


# ---------- financial history (XBRL company facts) ----------

CONCEPTS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "net_income": ["NetIncomeLoss"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "shares": ["WeightedAverageNumberOfDilutedSharesOutstanding", "WeightedAverageNumberOfSharesOutstandingBasic"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "debt": ["LongTermDebt", "LongTermDebtNoncurrent"],
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "gross_profit": ["GrossProfit"],
}
FLOWS = {"revenue", "net_income", "ocf", "capex", "shares", "gross_profit"}  # reported over a year, not at a date


def company_facts(cik):
    return get_json(f"{DATA}/api/xbrl/companyfacts/CIK{int(cik):010d}.json")


def annual_facts(facts):
    """{fiscal_year_end_year: {"revenue": .., "ocf": .., ...}} from a companyfacts document.

    Uses 10-K figures only. Flow items must cover about a year (no quarters), and for each period
    end the value from the latest filing wins, so restatements replace the originals.
    """
    gaap = (facts or {}).get("facts", {}).get("us-gaap", {})
    years = {}
    for key, names in CONCEPTS.items():
        for name in names:
            units = gaap.get(name, {}).get("units", {})
            rows = units.get("shares" if key == "shares" else "USD", [])
            picked = {}
            for r in rows:
                if r.get("form") not in ("10-K", "10-K/A") or "end" not in r:
                    continue
                if key in FLOWS:
                    if "start" not in r:
                        continue
                    days = (date.fromisoformat(r["end"]) - date.fromisoformat(r["start"])).days
                    if not 340 <= days <= 390:
                        continue
                prev = picked.get(r["end"])
                if prev is None or r.get("filed", "") >= prev.get("filed", ""):
                    picked[r["end"]] = r
            for end, r in picked.items():
                year = int(end[:4])
                years.setdefault(year, {}).setdefault(key, float(r["val"]))  # first concept found wins
            if picked:
                break
    return dict(sorted(years.items()))


# ---------- 13F holdings ----------

def parse_13f(xml_text):
    """{cusip: {"name", "cusip", "value", "shares"}} from a 13F information table (puts/calls skipped)."""
    root = ET.fromstring(xml_text)
    out = {}
    for row in (c for c in root.iter() if _local(c.tag) == "infoTable"):
        if _find(row, "putCall"):
            continue
        cusip = _find(row, "cusip")
        name = _find(row, "nameOfIssuer") or ""
        value = float(_find(row, "value") or 0)
        shares = float(_find(row, "shrsOrPrnAmt/sshPrnamt") or 0)
        hit = out.setdefault(cusip, {"name": name, "cusip": cusip, "value": 0.0, "shares": 0.0})
        hit["value"] += value  # Berkshire splits big holdings across several managers; add them up
        hit["shares"] += shares
    return out


def thirteen_f(cik=BERKSHIRE_CIK, count=2):
    """The latest `count` 13F-HR filings, newest first, each with its parsed holdings."""
    out = []
    for f in recent_filings(cik, {"13F-HR"})[:count]:
        index = get_json(filing_url(cik, f["accession"], "index.json")) or {}
        names = [i["name"] for i in index.get("directory", {}).get("item", [])]
        table = next((n for n in names if n.lower().endswith(".xml") and "primary_doc" not in n.lower()), None)
        if not table:
            continue
        xml_text = get_text(filing_url(cik, f["accession"], table))
        out.append({**f, "holdings": parse_13f(xml_text) if xml_text else {}})
    return out


def _norm(name):
    name = re.sub(r"[^A-Z0-9 ]", " ", name.upper())
    drop = {"INC", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "PLC", "THE", "NEW", "CL", "CLASS",
            "A", "B", "COM", "HLDGS", "HOLDINGS", "GROUP", "SA", "NV", "DEL"}
    return " ".join(w for w in name.split() if w not in drop)


def ticker_for_name(name):
    """Best-effort ticker for a 13F issuer name, e.g. 'APPLE INC' -> 'AAPL'. None if unsure."""
    want = _norm(name)
    if not want:
        return None
    hits = [t for t, v in ticker_map().items() if _norm(v["title"]) == want]
    return min(hits, key=len) if hits else None
