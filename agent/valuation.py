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
"""

DEFAULTS = {
    "discount_rate": 0.10,
    "terminal_growth": 0.03,
    "max_growth": 0.12,
    "margin_of_safety": 0.30,
    "min_years": 7,
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
