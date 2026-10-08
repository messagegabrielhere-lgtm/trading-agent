# Paper Trading Agent

A rule-based trading agent that runs on simulated money, with a GitHub Pages dashboard.

**It never places real orders and has no brokerage connection.** When the strategy rebalances, it
writes tickets (symbol, side, target weight) for a human to review and place manually. Nothing here
is investment advice.

## How it works

- `config.json` — universe, active strategy, parameters, starting cash, and the goal line.
- `agent/strategies.py` — the rules. Two examples ship: `momentum_rotation` and `target_weights`.
- `agent/engine.py` — portfolio simulation shared by the backtest and the paper run.
- `agent/run.py` — the daily run. Refreshes the backtest, advances the paper portfolio by every
  trading day since the last run, and writes `docs/data/*.json`.
- `docs/index.html` — the dashboard, served by GitHub Pages from `/docs`.
- `.github/workflows/agent.yml` — runs the agent on weekdays after the US close and commits the data.

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
