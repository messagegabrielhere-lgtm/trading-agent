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
- `.github/workflows/trade.yml` — runs the broker step every 15 minutes while the market is open, and right after any change to the broker code or config.

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
3. It checks the account every 15 minutes while the market is open (9:30 to 16:00 ET). To run it
   immediately: *Actions → Trade paper account → Run workflow*.

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

## Polymarket and Robinhood crypto (real-money platforms, dry run by default)

Both run every 30 minutes, all week, from `.github/workflows/markets.yml` and start in **dry-run mode**: they read
live public prices and log the trades they *would* make, without any keys and without moving money.
Results show on the dashboard.

**Polymarket favorite harvester** (`agent/polymarket.py`). Research on ~586 million Polymarket
purchases found contracts bought at 90¢ or more earned a small positive return before fees, while
contracts under 10¢ lost money; sports markets were the exception. An out-of-sample test on ~1,100
markets found positive but statistically unproven results after costs. The bot buys YES on heavy
favorites (bid 90–97¢) in non-sports events ending within 14 days, using maker-only limit orders so
it earns the maker rebate instead of paying the taker fee, one position per event, and holds to
resolution. A loss costs about ten wins, so it relies on spreading across many unrelated events.
Dry runs settle their simulated bets, so the dashboard builds a track record before you go live.

**Robinhood crypto trend** (`agent/crypto.py`). Holds BTC and ETH while each is above its 200-day
average, cash otherwise, within a fixed `budget_usd`. It only sells coins it bought itself, so your
own crypto in the same account is untouched. Each run backtests the rule against buy-and-hold.

Limits live in `config.json` under `polymarket` and `crypto` (per-market, total, per-order and
budget caps). To go live:

1. Polymarket: create an API key at polymarket.us/developer. Add secrets `POLYMARKET_KEY_ID` and
   `POLYMARKET_SECRET_KEY`, then set `"mode": "live"` under `polymarket`.
2. Robinhood: generate an Ed25519 key pair (`python3 -c "import base64,nacl.signing as s;k=s.SigningKey.generate();print('private:',base64.b64encode(bytes(k)).decode());print('public:',base64.b64encode(bytes(k.verify_key)).decode())"`),
   paste the public key at robinhood.com/account/crypto → Add key, then add secrets `RH_API_KEY` and
   `RH_PRIVATE_KEY`, and set `"mode": "live"` under `crypto`.

This repository is public, so anyone can read the dashboard and the Actions logs. Consider that
before switching a real-money bot to live.

## Active strategy: 2x Nasdaq trend (chosen 2026-10-09)

`leveraged_trend` holds QLD (2x the Nasdaq-100) while QQQ is above its 200-day average, with a 2%
band so it does not flip-flop on small crossings, and holds IEF (7-10 year Treasuries) otherwise.
In the 2017-2026 backtest it turned $10,000 into about $70,500 (24% a year) against $34,900 for
holding SPY, and beat SPY in both halves of the period. It also fell 54.5% at its worst (the 2020
crash moved faster than the 200-day average could react), which is why it failed the drawdown
limit set before the test. It runs in the Alpaca paper account to see how that behaves live.
Caveat: 2017-2026 was a strong decade for the Nasdaq; a 2x fund in 2000-2002 or 2008 would have
been far worse, and the trend filter only partly protects against that.

## Always-on trading (Alpaca)

`agent/live.py` is a separate bot that runs nonstop on a small server instead of on a GitHub Actions
schedule. It trades crypto around the clock and a few stocks and leveraged ETFs while the market is
open, in an Alpaca **paper** account by default.

**How it decides**

- Every 15 seconds it checks quotes and positions and enforces the exits:
  - **Stop-loss:** sells if a position is 6% below entry.
  - **Trailing stop:** once a position is 2% up, sells if it falls 4% from its high.
  - **Take profit:** sells half at +10%.
  - **Trend break:** sells if the 15-minute trend turns down.
- Every minute it recomputes 15-minute trend signals. It buys a symbol only when all of these hold:
  - the trend is up (9-bar average above 21-bar average, and the 21-bar average is rising);
  - it rose at least 0.5% over the last 4 hours;
  - it rose no more than 8% in the last hour, so it isn't chasing a spike;
  - its bid/ask spread is under 0.5%.
- **Positions and sizing:** at most 2 positions, each sized at half of equity, paid from cash only. No margin, no shorting.
- **Claude review:** once an hour, if `ANTHROPIC_API_KEY` is set, Claude (`claude-opus-5-5`, `agent/brain.py`) reads a market summary and returns risk on/off plus which symbols may be bought. It can only block buys; it never places orders.
- **Brakes:**
  - At 25% below peak equity, it sells everything and stops until you set `"halted": false` in the state file.
  - After a 10% loss in a day, it pauses new buys until the next UTC day.
- **Alerts:** every buy, sell and halt is printed and, with `NTFY_TOPIC` set, pushed to your phone.
  A "Still running" summary (equity, cash, positions) goes out every 6 hours.
  To get them, install the ntfy app and subscribe to that topic. Pick a long, random topic name.

All thresholds live under `live` in `config.json`.

**Run it locally**

```bash
ALPACA_KEY_ID=... ALPACA_SECRET_KEY=... python3 -m agent.live --once   # one check, then exit
python3 -m unittest discover -s tests -t .                              # tests
```

**Deploy on Fly.io** (about $2-5 a month)

```bash
fly launch --no-deploy --copy-config      # pick a unique app name when asked
fly volumes create agent_data --size 1
fly secrets set ALPACA_KEY_ID=... ALPACA_SECRET_KEY=... ANTHROPIC_API_KEY=... NTFY_TOPIC=...
fly deploy
fly logs                                  # watch it trade
```

The same `Dockerfile` runs on Railway, Render, or any VPS:

```bash
docker run -d --restart always -v agent_data:/data --env-file .env trading-agent
```

**Going live with real money**

1. Run on paper for a few weeks and read the logs.
2. Generate **live** API keys in the Alpaca dashboard.
3. Set `ALPACA_MODE=live` along with the live keys.

Without `ALPACA_MODE=live` it never touches a live account. Alpaca charges about 0.15-0.25% per
crypto trade, so each round trip costs roughly 0.3-0.5%. Nothing here is investment advice.

### Using Robinhood instead (`BROKER=robinhood`, crypto only)

The same bot can trade crypto through Robinhood's official Crypto Trading API (`agent/rh_broker.py`).
Robinhood has no official API for stocks, so this mode trades crypto only, around the clock.

- **It trades only its own budget.** `budget_usd` is set to $50 in config.json. The bot keeps a book of the coins it bought and only ever
  sells those, so crypto you hold yourself in the same Robinhood account is never touched.
- **Price history comes from Coinbase.** Robinhood's API has no candles, so the trend signals use Coinbase's public 15-minute candles. Quotes,
  spreads and orders come from Robinhood.
- **Robinhood's spread is wide**, often 1-2% between buy and sell. The Robinhood settings under
  `live.robinhood` in `config.json` require a bigger move before buying (1.5% over 4 hours) and
  use wider stops (8% stop-loss, 5% trailing).
- **Dry run by default.** It logs the trades it would make. Add `RH_LIVE=1` for real orders.

Set up:

1. Make a key pair:
   `python3 -c "import base64,nacl.signing as s;k=s.SigningKey.generate();print('private:',base64.b64encode(bytes(k)).decode());print('public:',base64.b64encode(bytes(k.verify_key)).decode())"`
2. At robinhood.com/account/crypto, add an API key with the **public** key. Robinhood shows
   which crypto account the key trades; the API works only for crypto.
3. Deploy as above with these secrets:
   `fly secrets set BROKER=robinhood RH_API_KEY=... RH_PRIVATE_KEY=... RH_ACCOUNT=... NTFY_TOPIC=... ANTHROPIC_API_KEY=...`
   `RH_ACCOUNT` is the crypto account number the bot may trade. On every check it asks Robinhood which
   account the key belongs to, and places nothing if that isn't `RH_ACCOUNT`.
4. Watch `fly logs` in dry run. On start it reports which crypto account the key belongs to
   ("Robinhood key belongs to crypto account ending ...").
5. Set `RH_ACCOUNT` to that account number and `RH_LIVE=1` to trade for real.
