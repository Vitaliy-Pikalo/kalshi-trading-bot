# poker-quant-bot

a kalshi prediction market trading bot that prices events using ML and sizes positions with poker bankroll math (kelly criterion, fractional sizing, variance-aware).

**status:** phase 0 — setup
**bankroll:** $100 paper / $0 live
**platform:** kalshi (CFTC-regulated, US legal)

## the thesis

prediction markets like kalshi are inefficient because they're young, liquidity is uneven, and most participants aren't pricing rigorously. classic poker math — EV, kelly criterion, variance-aware bankroll mgmt — applies directly. this project tests that thesis with a small live bankroll after honest backtesting.

## architecture

```
src/
  config.py        # loads env vars via pydantic-settings
  kalshi/          # api client (auth, market data, orders)
  ingest/          # market polling -> sqlite snapshots
  models/          # ml edge models (logistic, xgb)
  strategy/        # ev calc, kelly sizing, risk limits
  backtest/        # historical replay + metrics
  live/            # production trading loop
data/              # sqlite db, raw + processed data
notebooks/         # exploration, backtest analysis
tests/             # pytest unit tests
```

## quickstart

```powershell
# 1. clone + enter
git clone <repo> poker-quant-bot
cd poker-quant-bot

# 2. python env
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 3. configure
copy .env.example .env
# edit .env with your kalshi key_id + private key path

# 4. verify api works
python -m src.kalshi.smoke_test

# 5. start ingestion (writes snapshots every 60s)
python -m src.ingest.run
```

## phases

| phase | weeks | deliverable |
|---|---|---|
| 0 | 0-1 | platform + skeleton (this) |
| 1 | 1-2 | data ingestion + paper trade |
| 2 | 3-6 | ml edge model + backtest |
| 3 | 7-9 | kelly sizing + risk mgmt + dashboard |
| 4 | 10-12 | go live + writeup |

see `poker-quant-project-brief.md` for full plan.

## risk rules (hardcoded)

- max 1% bankroll per single position
- quarter-kelly sizing (k=0.25)
- min edge 3% to enter
- 5% daily loss → halt for the day
- 20% drawdown → full stop, post-mortem

## license

MIT (planned, after phase 4 cleanup)
