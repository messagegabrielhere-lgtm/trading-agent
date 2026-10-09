# Paper Trading Agent

A rule-based trading agent that runs on simulated money, with a GitHub Pages dashboard.

**It never touches real money.** When the strategy rebalances, it writes tickets (symbol, side,
target weight). If you add Alpaca *paper* keys, a second job places those trades automatically in an
Alpaca paper account (fake money, real market prices). The code is fixed to Alpaca's paper endpoint
and cannot reach a live account. Nothing here is investment advice.

## How it works

- `config.json` — universe, active strategy, parameters, starting cash, and the goal line.
- `agent/strategies.py` — the rules. Two examples ship: `momentum_rotation` and `target_weights`.
- `agent/engine.py` — portfolio simulation shared by the backtest and the paper run.
- `agent/run.py` — the daily run. Refreshes the backtest, advances the paper portfolio by every
  trading day since the last run, and writes `docs/data/*.json`.
- `docs/index.html` — the dashboard, served by GitHub Pages from `/docs`.
- `.github/workflows/agent.yml` — runs the agent on weekdays after the US close and commits the data.
- `agent/broker.py` — mirrors the latest targets into the Alpaca paper account during market hours.
- `.github/workflows/trade.yml` — runs the broker step on weekdays at 15:00 UTC (late morning ET).

Run it locally (Python 3.9+, no dependencies):

```bash
python3 -m agent.run
```

```bash
python3 -m http.server -d docs 8000
```

## The goal line

`goal_multiple` and `goal_days` draw a target path on the dashboard (default 10x in 365 days, about
21% compounded per month). It is a yardstick, not something the strategies are expected to reach:
the dashboard shows the best 12-month result in the backtest next to it.

## Changing the strategy

Edit the parameters in `config.json`, or add a function to `agent/strategies.py` and register it in
`STRATEGIES`. To restart the paper portfolio from scratch, delete `docs/data/state.json`.

## Limits

- Prices are daily closes from Yahoo Finance; fills are simulated at the close with slippage.
- The paper portfolio is marked at unadjusted closes, so dividends are not credited.
- Backtests use dividend-adjusted closes and ignore taxes.

## Automatic trading in an Alpaca paper account

1. Sign up at alpaca.markets and open the **Paper Trading** dashboard (it comes with $100,000 of
   fake money). Under *API Keys*, generate a key pair. Make sure the page says "Paper".
2. In this repo on GitHub: *Settings → Secrets and variables → Actions → New repository secret*.
   Add `ALPACA_KEY_ID` and `ALPACA_SECRET_KEY`.
3. Run it once now: *Actions → Trade paper account → Run workflow* (during market hours,
   9:30 to 16:00 ET). After that it runs every weekday on its own.

What it does each run: cancels leftover orders, compares the account's positions with the strategy's
targets, sells what is no longer wanted or is overweight, then buys with cash only (no margin).
Small differences under `drift_threshold` are left alone. Results land in `docs/data/broker.json`
and show on the dashboard.

Settings live under `execution` in `config.json`:

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Set to `false` to stop placing orders. The account is still reported. |
| `drift_threshold` | `0.03` | Rebalance a symbol only when it is 3% of equity or more off target. |
| `max_drawdown_halt` | `0.20` | If equity falls 20% below its peak, close everything and stop. |
| `allocation` | `1.0` | Share of account equity the strategy may use. |

After a drawdown halt, set `"halted": false` in `docs/data/broker.json` to resume.

To test locally against the paper account:

```bash
ALPACA_KEY_ID=... ALPACA_SECRET_KEY=... python3 -m agent.broker
```
