"""Intrinsic value from up to 10 years of SEC filings (pure functions, unit tested).

The method is a plain owner-earnings DCF:
  1. Free cash flow per share each year = (operating cash flow - capital spending) / diluted shares.
  2. Refuse to value a business whose free cash flow is erratic: fewer than 7 years of history, or
     negative in more than 1 year in 5, means "can't value", never a guess.
  3. Start from the average of the last 3 years. Growth is the historical per-share growth, capped
     at `max_growth` and never below 0, fading in a straight line to `terminal_growth` over 10 years.
  4. Discount at `discount_rate`, add a terminal value, add net cash (cash - long-term debt).
  5. Buy below = intrinsic value x (1 - margin_of_safety).

It is a screen, not a forecast. Banks, insurers and young companies don't fit it, and it says so.

quality() is the other half of the playbook: Buffett buys wonderful businesses, not just cheap ones.
It checks the filings for the marks of a durable moat (see QUALITY_RULES) before price matters at all.
"""

DEFAULTS = {
    "discount_rate": 0.10,
    "terminal_growth": 0.03,
    "max_growth": 0.12,
    "margin_of_safety": 0.30,
    "min_years": 7,
    "min_roe": 0.15,              # median return on equity
    "max_debt_years": 4,          # long-term debt payable from this many years of free cash flow
    "max_dilution": 0.10,         # share count may rise at most this much over the history
    "max_margin_swing": 0.05,     # gross-margin standard deviation, when gross profit is reported
}

QUALITY_RULES = {
    "high_returns": "Earns at least 15% on shareholders' equity in a typical year (a moat shows up as high returns).",
    "steady_earnings": "Profitable every year, or all but one, for the last 10 years (no turnarounds).",
    "low_debt": "Could pay off long-term debt from a few years of free cash flow.",
    "no_dilution": "Share count flat or shrinking: management isn't paying itself in new stock.",
    "growing_earnings": "Earnings power today is above where it was a decade ago.",
    "pricing_power": "Gross margin holds steady through good years and bad.",
}


def _mean(xs):
    return sum(xs) / len(xs)


def history(years):
    """[(year, fcf_per_share)] for the years that have cash flow, capex and share counts."""
    out = []
    for y, f in sorted(years.items()):
        if all(k in f for k in ("ocf", "capex", "shares")) and f["shares"] > 0:
            out.append((y, (f["ocf"] - abs(f["capex"])) / f["shares"]))
    return out[-10:]


def value(years, cfg=None):
    """Value a company from annual_facts() output. Returns a dict with ok=False and a reason when it can't."""
    c = {**DEFAULTS, **(cfg or {})}
    rows = history(years)
    if len(rows) < c["min_years"]:
        return {"ok": False, "reason": f"only {len(rows)} years of cash-flow history (need {c['min_years']})"}
    fcf = [v for _, v in rows]
    negatives = sum(v <= 0 for v in fcf)
    if negatives > len(fcf) / 5:
        return {"ok": False, "reason": f"free cash flow negative in {negatives} of {len(fcf)} years"}
    base = _mean(fcf[-3:])
    if base <= 0:
        return {"ok": False, "reason": "free cash flow negative over the last 3 years"}

    start, span = _mean(fcf[:3]), len(fcf) - 3
    growth = (base / start) ** (1 / span) - 1 if start > 0 and span > 0 else c["terminal_growth"]
    growth = min(max(growth, 0.0), c["max_growth"])

    r, g_end = c["discount_rate"], c["terminal_growth"]
    cash_flow, pv = base, 0.0
    for t in range(1, 11):
        g = growth + (g_end - growth) * (t - 1) / 9  # straight-line fade to the terminal rate
        cash_flow *= 1 + g
        pv += cash_flow / (1 + r) ** t
    terminal = cash_flow * (1 + g_end) / (r - g_end) / (1 + r) ** 10

    last_year = rows[-1][0]
    latest = years.get(last_year, {})
    net_cash = (latest.get("cash", 0.0) - latest.get("debt", 0.0)) / latest["shares"]
    intrinsic = pv + terminal + net_cash
    if intrinsic <= 0:
        return {"ok": False, "reason": "debt outweighs the value of the cash flows"}
    return {
        "ok": True,
        "intrinsic": round(intrinsic, 2),
        "buy_below": round(intrinsic * (1 - c["margin_of_safety"]), 2),
        "growth": round(growth, 4),
        "base_fcf_per_share": round(base, 2),
        "net_cash_per_share": round(net_cash, 2),
        "years": [y for y, _ in rows],
        "fcf_per_share": [round(v, 2) for v in fcf],
    }


def verdict(price, val):
    """'cheap' (under buy-below), 'fair' (under intrinsic) or 'expensive', and the discount to intrinsic."""
    if not val.get("ok") or not price:
        return None
    discount = 1 - price / val["intrinsic"]
    label = "cheap" if price <= val["buy_below"] else "fair" if price <= val["intrinsic"] else "expensive"
    return {"label": label, "discount": round(discount, 4)}


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return (xs[n // 2] + xs[(n - 1) // 2]) / 2


def quality(years, cfg=None):
    """Buffett's business checklist from annual_facts() output.

    Returns {"passed", "score", "checks": [{"rule", "ok", "detail"}]}. ok is None when the filings
    can't answer the question (e.g. no gross profit line for a bank); those checks don't count.
    passed means every answerable check passed and at least 4 could be answered.
    """
    c = {**DEFAULTS, **(cfg or {})}
    ys = sorted(years)[-10:]
    get = lambda k: [(y, years[y][k]) for y in ys if k in years[y]]
    checks = []

    def add(rule, ok, detail):
        checks.append({"rule": rule, "ok": ok, "detail": detail})

    ni, eq = dict(get("net_income")), dict(get("equity"))
    roes = [ni[y] / eq[y] for y in ys if y in ni and y in eq and eq[y] > 0]
    if eq and eq[max(eq)] <= 0:
        add("high_returns", None, "negative equity (usually from buybacks); return on equity is meaningless")
    elif len(roes) >= 5:
        m = _median(roes)
        add("high_returns", m >= c["min_roe"], f"median return on equity {m:.0%}")
    else:
        add("high_returns", None, "not enough equity history")

    incomes = [v for _, v in get("net_income")]
    if len(incomes) >= c["min_years"]:
        losses = sum(v <= 0 for v in incomes)
        add("steady_earnings", losses <= 1, f"losses in {losses} of {len(incomes)} years")
    else:
        add("steady_earnings", False, f"only {len(incomes)} years of earnings")

    fcf = [years[y]["ocf"] - abs(years[y]["capex"]) for y in ys if "ocf" in years[y] and "capex" in years[y]]
    debt = years[ys[-1]].get("debt", 0.0) if ys else 0.0
    if len(fcf) >= 3:
        avg = _mean(fcf[-3:])
        if debt <= 0:
            add("low_debt", True, "no long-term debt")
        elif avg <= 0:
            add("low_debt", False, "debt with no free cash flow to repay it")
        else:
            add("low_debt", debt / avg <= c["max_debt_years"], f"debt equals {debt / avg:.1f} years of free cash flow")
    else:
        add("low_debt", None, "not enough cash-flow history")

    shares = [v for _, v in get("shares") if v > 0]
    if len(shares) >= 5:
        change = shares[-1] / shares[0] - 1
        add("no_dilution", change <= c["max_dilution"], f"share count {change:+.0%} over {len(shares)} years")
    else:
        add("no_dilution", None, "not enough share-count history")

    if len(incomes) >= 6 and _mean(incomes[:3]) > 0:
        growth = _mean(incomes[-3:]) / _mean(incomes[:3]) - 1
        add("growing_earnings", growth > 0, f"earnings {growth:+.0%} vs a decade ago (3-year averages)")
    else:
        add("growing_earnings", None, "not enough earnings history")

    margins = [years[y]["gross_profit"] / years[y]["revenue"] for y in ys
               if years[y].get("revenue", 0) > 0 and "gross_profit" in years[y]]
    if len(margins) >= 5:
        mu = _mean(margins)
        sd = (_mean([(m - mu) ** 2 for m in margins])) ** 0.5
        add("pricing_power", sd <= c["max_margin_swing"], f"gross margin {mu:.0%} ± {sd:.1%}")
    else:
        add("pricing_power", None, "gross margin not reported")

    answered = [x for x in checks if x["ok"] is not None]
    good = sum(x["ok"] for x in answered)
    return {"passed": len(answered) >= 4 and good == len(answered),
            "score": f"{good}/{len(answered)}", "checks": checks}
