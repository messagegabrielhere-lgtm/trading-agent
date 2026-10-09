"""Polymarket US: "favorite harvester".

    python -m agent.polymarket

The evidence: in a study of ~586 million Polymarket purchases (Nov 2022 to Mar 2026), contracts bought
at 90 cents or more returned a small positive amount before fees (about +0.3% to +0.8%), while
contracts bought under 10 cents lost money (about -6% to -19%). The effect held in crypto and politics
markets and did NOT hold in sports, where longshots did fine. A separate out-of-sample test on ~1,100
markets found positive but statistically unproven results after costs. So the edge, if real, is thin.

What this does with that:
  * Looks only at events that end within `max_days_to_end` days, so money turns over quickly.
  * Skips sports and anything thin (`min_liquidity`).
  * Buys YES only when the market already prices it as a heavy favorite (bid between `min_price` and
    `max_price`), with maker-only limit orders posted at or just above the bid. Maker orders earn
    Polymarket's maker rebate instead of paying the taker fee, which would eat most of the edge.
  * Holds to resolution. One position per event, a hard dollar cap per market and in total.

The risk: a favorite that loses costs roughly 90 cents per contract, and wipes out the profit of about
ten winners. Spreading across many unrelated events is what makes this work, if it works at all.

Modes (config.json -> polymarket.mode):
  dry_run  (default) reads public market data and logs the orders it would place. No keys needed.
  live     places real orders with real money. Needs POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY.
"""
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data" / "polymarket.json"

DEFAULTS = {
    "mode": "dry_run",
    "min_price": 0.90,
    "max_price": 0.97,          # above this the upside is too small to cover a single loss
    "max_days_to_end": 14,
    "min_hours_to_end": 2,      # avoid the last-minute scramble
    "min_liquidity": 2000,
    "skip_tags": ["sports", "nfl", "nba", "mlb", "nhl", "soccer", "football", "basketball",
                  "baseball", "hockey", "tennis", "golf", "mma", "ufc", "boxing", "f1", "cricket"],
    "max_per_market_usd": 25,
    "max_total_usd": 200,
    "max_positions": 10,
    "max_per_series": 1,        # related events (same data release, same race) move together
    "max_events_scanned": 300,
    "max_price_checks": 150,    # order-book lookups per run
    "pause_seconds": 0.25,      # between lookups; the public API is rate limited
}


class Throttled(Exception):
    pass


def paced(fn, what, cfg):
    """Call fn with spacing; on a rate limit wait and retry twice, then give up for this run."""
    from polymarket_us.errors import RateLimitError
    for wait in (0, 20, 60):
        if wait:
            time.sleep(wait)
        time.sleep(cfg["pause_seconds"])
        try:
            return fn()
        except RateLimitError:
            continue
    raise Throttled(what)


def now():
    return datetime.now(timezone.utc)


def iso(t):
    return t.isoformat(timespec="seconds").replace("+00:00", "Z")


def money(a):
    return float(a["value"]) if a else None


def load_report():
    if OUT.exists():
        rep = json.loads(OUT.read_text())
    else:
        rep = {}
    rep.setdefault("orders", [])
    rep.setdefault("log", [])
    return rep


def save_report(rep):
    rep["orders"] = rep["orders"][-300:]
    rep["log"] = rep["log"][-200:]
    rep["updated"] = iso(now())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rep, indent=1) + "\n")


def log(rep, text):
    print(text)
    rep["log"].append({"time": iso(now()), "text": text})


def make_client(live):
    try:
        from polymarket_us import PolymarketUS
    except ImportError:
        sys.exit("The Polymarket US SDK is missing. Install it with: pip install polymarket-us")
    kw = {}
    if os.environ.get("POLYMARKET_GATEWAY_URL"):  # tests only
        kw["gateway_base_url"] = os.environ["POLYMARKET_GATEWAY_URL"]
        kw["api_base_url"] = os.environ["POLYMARKET_API_URL"]
    if live:
        return PolymarketUS(key_id=os.environ["POLYMARKET_KEY_ID"],
                            secret_key=os.environ["POLYMARKET_SECRET_KEY"], max_retries=0, **kw)
    return PolymarketUS(max_retries=0, **kw)


def candidate_events(client, cfg):
    t = now()
    params = {
        "active": True, "closed": False, "limit": 100,
        "endDateMin": iso(t + timedelta(hours=cfg["min_hours_to_end"])),
        "endDateMax": iso(t + timedelta(days=cfg["max_days_to_end"])),
        "liquidityMin": cfg["min_liquidity"],
    }
    skip = {s.lower() for s in cfg["skip_tags"]}
    out, offset = [], 0
    while offset < cfg["max_events_scanned"]:
        page = paced(lambda: client.events.list({**params, "offset": offset}),
                     "event list", cfg).get("events", [])
        if not page:
            break
        for ev in page:
            tags = {(tg.get("slug") or "").lower() for tg in ev.get("tags", [])} | \
                   {(tg.get("label") or "").lower() for tg in ev.get("tags", [])}
            if tags & skip:
                continue
            out.append(ev)
        offset += len(page)
        if len(page) < params["limit"]:
            break
    return out


def series_key(ev):
    """Group related events (e.g. every market on one CPI release) so they count as one bet."""
    # The slug's first word is the topic code (e.g. "cpic" for every CPI market). Series names are
    # finer-grained (headline vs core CPI) and would let one data release count twice.
    return ev["slug"].split("-")[0]


def pick_price(bid, ask, cfg):
    """Post at the bid, or one cent better when the spread allows, never crossing the ask."""
    if bid is None:
        return None
    px = bid
    if ask is not None and ask - bid >= 0.02:
        px = round(bid + 0.01, 2)
    if not (cfg["min_price"] <= px <= cfg["max_price"]):
        return None
    return px


def score_dry_runs(client, rep):
    """Settle simulated orders whose event has ended, so dry runs build a track record.

    Assumes every simulated maker order filled, which flatters the result a little: in live
    trading some bids are never hit.
    """
    t, hour_ago, tries = iso(now()), iso(now() - timedelta(hours=1)), 0
    for o in rep["orders"]:
        if o.get("mode") != "dry_run" or "result" in o or o["time"] > hour_ago:
            continue
        if o.get("ends") and o["ends"] > t:
            continue
        if tries >= 40:
            break
        tries += 1
        try:
            time.sleep(0.25)
            st = client.markets.settlement(o["slug"])
        except Exception:
            continue  # not settled yet
        val = st.get("settlement")
        if val is None:
            continue
        o["result"] = "won" if float(val) >= 0.5 else "lost"
        o["pnl"] = round(o["qty"] * (float(val) - o["price"]), 2)
    done = [o for o in rep["orders"] if o.get("mode") == "dry_run" and "result" in o]
    rep["dry_run_record"] = {
        "settled": len(done), "won": sum(o["result"] == "won" for o in done),
        "pnl_usd": round(sum(o["pnl"] for o in done), 2),
        "staked_usd": round(sum(o["cost"] for o in done), 2),
    }


def holdings(client, live, rep):
    """Slugs we hold or have open orders on, and the dollars tied up in them."""
    if not live:  # the simulated book: dry-run orders whose event has not ended yet
        t = iso(now())
        held = {o["slug"]: {"qty": o["qty"], "cost": o["cost"], "event_title": o.get("event"),
                            "series": o.get("series")}
                for o in rep["orders"] if o.get("mode") == "dry_run" and "result" not in o
                and (o.get("ends") or "9999") > t}
        return held, {}, sum(h["cost"] for h in held.values())
    pos = client.portfolio.positions().get("positions", {}) or {}
    held = {}
    for key, p in pos.items():
        q = float(p.get("netPositionDecimal") or p.get("netPosition") or 0)
        if q and not p.get("expired"):
            slug = (p.get("marketMetadata") or {}).get("slug") or key
            ev_slug = (p.get("marketMetadata") or {}).get("eventSlug") or slug
            held[slug] = {"qty": q, "cost": money(p.get("cost")) or 0.0,
                          "event": ev_slug, "series": ev_slug.split("-")[0]}
    open_orders = {}
    for o in client.orders.list().get("orders", []) or []:
        slug = o.get("marketSlug") or (o.get("marketMetadata") or {}).get("slug")
        left = float(o.get("leavesQuantity") or o.get("quantity") or 0)
        open_orders[slug] = open_orders.get(slug, 0.0) + left * (money(o.get("price")) or 0)
    used = sum(h["cost"] for h in held.values()) + sum(open_orders.values())
    return held, open_orders, used


def main():
    cfg = {**DEFAULTS, **json.loads((ROOT / "config.json").read_text()).get("polymarket", {})}
    live = cfg["mode"] == "live"
    rep = load_report()
    rep["mode"] = cfg["mode"]
    rep["settings"] = {k: cfg[k] for k in ("min_price", "max_price", "max_days_to_end",
                                           "max_per_market_usd", "max_total_usd", "max_positions")}

    if live and not (os.environ.get("POLYMARKET_KEY_ID") and os.environ.get("POLYMARKET_SECRET_KEY")):
        log(rep, "Mode is live but POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY are not set. Nothing placed.")
        save_report(rep)
        return

    client = make_client(live)
    try:
        if not live:
            score_dry_runs(client, rep)
        held, open_orders, used = holdings(client, live, rep)
        held_events = {h["event"] for h in held.values() if h.get("event")}
        held_titles = {h["event_title"] for h in held.values() if h.get("event_title")}
        busy = set(held) | set(open_orders)
        if not live:  # never simulate the same market twice
            busy |= {o["slug"] for o in rep["orders"] if o.get("mode") == "dry_run"}
        room = cfg["max_total_usd"] - used
        slots = cfg["max_positions"] - len(busy)

        events = candidate_events(client, cfg)
        picks, checks, throttled = [], 0, None
        for ev in events:
            if throttled or checks >= cfg["max_price_checks"]:
                break
            if ev["slug"] in held_events or ev.get("title") in held_titles:
                continue  # one position per event
            best = None
            for m in ev.get("markets", []):
                if not m.get("active", True) or m.get("closed") or m["slug"] in busy:
                    continue
                if checks >= cfg["max_price_checks"]:
                    break
                checks += 1
                try:
                    bbo = paced(lambda: client.markets.bbo(m["slug"]), "price check", cfg).get("marketData", {})
                except Throttled as t:
                    throttled = str(t)
                    break
                bid, ask = money(bbo.get("bestBid")), money(bbo.get("bestAsk"))
                px = pick_price(bid, ask, cfg)
                if px is not None and (best is None or px > best["price"]):
                    best = {"slug": m["slug"], "title": m.get("title") or ev.get("title"),
                            "event": ev["slug"], "event_title": ev.get("title"),
                            "ends": ev.get("endTime") or ev.get("endDate") or ev.get("closeTime"),
                            "series": series_key(ev), "bid": bid, "ask": ask, "price": px}
            if best:
                picks.append(best)

        # Soonest-ending first: capital comes back faster.
        picks.sort(key=lambda p: p["ends"] or "9999")
        series_count = {}
        for h in held.values():
            if h.get("series"):
                series_count[h["series"]] = series_count.get(h["series"], 0) + 1
        diverse = []
        for p in picks:
            if series_count.get(p["series"], 0) < cfg["max_per_series"]:
                series_count[p["series"]] = series_count.get(p["series"], 0) + 1
                diverse.append(p)
        picks = diverse
        rep["candidates"] = picks[:50]
        placed = 0
        for p in picks:
            if slots <= 0 or room < 1:
                break
            spend = min(cfg["max_per_market_usd"], room)
            qty = math.floor(spend / p["price"])
            if qty < 1:
                continue
            cost = round(qty * p["price"], 2)
            order = {
                "marketSlug": p["slug"], "intent": "ORDER_INTENT_BUY_LONG",
                "type": "ORDER_TYPE_LIMIT", "price": {"value": f"{p['price']:.2f}", "currency": "USD"},
                "quantity": qty, "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
                "participateDontInitiate": True,
                "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            }
            entry = {"time": iso(now()), "slug": p["slug"], "title": p["title"], "event": p["event_title"],
                     "ends": p["ends"], "series": p["series"], "price": p["price"], "qty": qty, "cost": cost,
                     "max_profit": round(qty * (1 - p["price"]), 2), "mode": cfg["mode"]}
            if live:
                try:
                    res = client.orders.create(order)
                    entry["id"], entry["status"] = res.get("id"), "placed"
                except Exception as e:  # one bad market should not stop the run
                    entry["status"] = f"rejected: {str(e)[:160]}"
            else:
                entry["status"] = "dry run"
            rep["orders"].append(entry)
            verb = "Placed" if live else "Would place"
            log(rep, f"{verb} BUY YES {qty} x {p['slug']} at ${p['price']:.2f} (${cost:.2f}, "
                     f"pays ${qty:.0f} if it resolves YES; ends {p['ends']}). {entry['status']}.")
            if not entry["status"].startswith("rejected"):
                placed += 1
                slots -= 1
                room -= cost

        rep["exposure"] = {"used_usd": round(used, 2), "limit_usd": cfg["max_total_usd"],
                           "positions": len(held), "open_orders": len(open_orders)}
        rep["positions"] = [{"slug": s, **h} for s, h in sorted(held.items())]
        note = f" Stopped early: Polymarket rate-limited the {throttled}." if throttled else ""
        log(rep, f"Scanned {len(events)} events ending within {cfg['max_days_to_end']} days "
                 f"({checks} price checks); {len(picks)} qualify; "
                 f"{placed} order(s) {'placed' if live else 'simulated'}.{note}")
    except Throttled as t:
        log(rep, f"Polymarket rate-limited the {t} even after waiting. Nothing placed this run.")
        save_report(rep)
        return
    except Exception as e:
        msg = str(e)
        if "<html" in msg.lower():
            msg = f"HTTP {getattr(getattr(e, 'response', None), 'status_code', '?')} (an HTML error page)"
        log(rep, f"Stopped: {type(e).__name__}: {msg[:200]}")
        save_report(rep)
        sys.exit(1)
    finally:
        try:
            client.close()
        except Exception:
            pass
    save_report(rep)


if __name__ == "__main__":
    main()
