# handoff doc — kalshi trading bot

**last updated:** 2026-05-16 (session 2 wrap)
**phase:** 1 partially complete (pipeline working end-to-end on prod)
**repo:** https://github.com/Vitaliy-Pikalo/kalshi-trading-bot (public)
**local:** `C:\Users\pikal\Downloads\claude project`

---

## project context

kalshi prediction market trading bot. prices binary event markets using ML + sizes positions with poker bankroll math (fractional kelly, EV thresholds). passion project + quant internship signal + small real $ side bet.

three-way thesis:
1. **passion** — poker math (EV, kelly, variance) applied to a live game
2. **quant signal** — same work SIG / Susquehanna / Jane Street do in event markets
3. **real $** — kalshi is CFTC-regulated US legal. $100 paper through phase 3, $100 live in phase 4

categories chosen: **crypto** (BTC/ETH daily + 15m) + **tennis** (ATP/WTA matches). running both in parallel.

---

## status map

| phase | name | weeks | status |
|---|---|---|---|
| 0 | setup + skeleton | 0-1 | ✅ done |
| 1 | data ingestion + paper trade | 1-2 | 🚧 ~80% done |
| 2 | ml edge model + backtest | 3-6 | pending |
| 3 | poker-math layer (kelly + risk) | 7-9 | pending |
| 4 | go live + writeup | 10-12 | pending |

---

## what's been done

### phase 0 (shipped to github)
- kalshi RSA-signed REST client, sqlite schema (5 tables), pydantic config, smoke test
- git repo, github public repo, first commit pushed

### phase 1 (huge progress this session)
- **expanded kalshi client** — added list_events, get_market, get_event, list_series, get_orderbook, iter_markets (pagination), self-throttling + 429 retry
- **probe scripts** — `src/kalshi/probe_categories.py` (lists all 10,361 series by category), `src/kalshi/probe_market.py` (deep-dives a single market)
- **market discovery + classifier** — `src/ingest/discover.py` walks series, classifies crypto/tennis/other, upserts to db
- **coinbase spot client** — `src/spot/coinbase.py` pulls live BTC/ETH prices + historical candles + 30d realized vol (no auth)
- **snapshot poller** — `src/ingest/poller.py` polls bid/ask/volume every 60s for active markets (closing in 48h), bulk fetch via `tickers=` param
- **crypto vol baseline** — `src/strategy/crypto_vol.py` log-normal pricing model, parses ticker for strike+direction, computes P(yes) for any BTC/ETH binary
- **paper trade simulator** — `src/strategy/paper_sim.py` runs baseline → writes prediction + paper fill if |edge| > threshold + has liquidity, uses fractional Kelly sizing
- **db wipe + inspect** — `src/db_wipe.py`, `src/db_inspect.py`, `src/diagnose.py` for ops
- **utils** — `src/utils.py` idempotent utf-8 stdout setup
- **migrated to PROD env** — kalshi auth works on prod, real liquidity confirmed

### key wins
- **end-to-end pipeline functional on prod:** 1547 markets discovered, 768 actively snapshotted, 498 predictions, 157 paper trades fired in single cycle
- **real liquidity confirmed:** BTC15M markets have volumes >300k contracts, BTCD strikes near spot trade actively
- **kalshi field name fix** — discovered kalshi returns `yes_bid_dollars` (string) not `yes_bid` (int cents). poller now handles both formats

### code stats
- ~1,425 lines added this session
- 13 new files
- 3 commits pushed: `bb65ed6`, `4ad4766`, `732a280`

---

## current task / step / phase

**phase 1 ~80% complete.** all infrastructure built and verified against live prod data. remaining:

1. **settlement tracking** — when market settles, fetch outcome from kalshi + compute realized P&L on paper fills. without this, paper trades are meaningless
2. **tennis Elo baseline** (tasks 17 + 18) — fetch JeffSackmann ATP/WTA data, compute surface-adjusted Elo ratings, implement Baseline interface for tennis matches
3. **phase 1 checkpoint** (task 16) — run poller + paper sim continuously for 24-48h, accumulate 100+ paper trades with settled outcomes, compute realized edge vs predicted edge

---

## what still needs to be done

### phase 1 remaining
- [ ] settlement worker — runs hourly, finds fills where market closed, fetches result, updates Fill.realized_pnl
- [ ] tennis data client — pull JeffSackmann/tennis_atp historical match data
- [ ] tennis Elo baseline — surface-adjusted, accounts for elo decay
- [ ] phase 1 checkpoint — run 24h+, verify >100 settled paper trades, edge analysis
- [ ] honest model critique — current model has 0/1 saturation issue (log-normal assumes 0% tail prob). real BTC has fat tails. edge=0.99 readings are illusory
- [ ] fix deprecation warnings — `datetime.utcnow()` is deprecated in Python 3.14, switch to `datetime.now(timezone.utc)`

### phase 2 (when phase 1 verified)
- xgboost edge model with proper time-series CV
- features: lagged spot, vol regime, time-to-expiry, market microstructure (spread, depth)
- calibration: isotonic regression to map raw model output to actual probability
- backtest harness with realistic slippage + fees

---

## next step (for fresh chat)

1. read this HANDOFF.md + the project brief
2. **first task**: implement settlement tracking
   - new module `src/ingest/settler.py`
   - finds fills with NULL realized_pnl whose market.close_ts < now
   - fetches `get_market(ticker)` to read `result` field
   - computes P&L: if won → contracts * (100 - price_cents) cents, if lost → -contracts * price_cents
   - updates Fill.realized_pnl in cents
3. then start tennis baseline (task 17 → 18)
4. then run 24h soak test for phase 1 checkpoint

---

## workflow rules

- one phase at a time. don't skip ahead to phase 2 until phase 1 settlement + realized P&L is working
- one task at a time with explicit verification
- backtest before paper trade, paper trade before real $
- log everything to sqlite (markets, snapshots, predictions, fills, bankroll)
- git commit after every working feature, push to github
- max risk: 1% bankroll per position (fractional kelly k=0.25)
- never paste private keys, passwords, or KYC info into chat
- use `_*.bat` scripts for command execution (gitignored). real code goes in `src/`
- prod env is OK for read-only operations. orders require explicit user approval

---

## quickstart for next session

```powershell
# verify env works (from project root)
cd C:\Users\pikal\Downloads\claude project

# verify pipeline still runs
.venv\Scripts\python.exe -m src.kalshi.smoke_test
.venv\Scripts\python.exe -m src.ingest.poller --once
.venv\Scripts\python.exe -m src.strategy.paper_sim --once
.venv\Scripts\python.exe -m src.diagnose
```

if `.env` got reset: KALSHI_ENV=prod, KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH=`C:\Users\pikal\Downloads\bot.txt` (or wherever you moved it)

---

## key files for next session to read

1. `HANDOFF.md` (this file)
2. `README.md`
3. `src/config.py` — pydantic settings + risk defaults
4. `src/db.py` — sqlite schema
5. `src/kalshi/client.py` — RSA-signed API, rate limiting, all endpoint wrappers
6. `src/ingest/discover.py` — series-scoped market discovery + classifier
7. `src/ingest/poller.py` — bulk snapshot polling with field-name handling
8. `src/strategy/base.py` — Baseline ABC + Prediction dataclass
9. `src/strategy/crypto_vol.py` — log-normal vol baseline (parses ticker for strike)
10. `src/strategy/paper_sim.py` — fractional Kelly paper trader
11. `src/diagnose.py` — edge distribution + sample liquid markets

---

## one-line elevator pitch (current)

"i built a kalshi prediction market bot that pulls real-time crypto + tennis market data, prices each binary using log-normal vol models, sizes positions with fractional kelly criterion from poker bankroll theory, and runs a paper trader against live prod data. currently logging 157 paper trades per polling cycle with proper edge filtering, working toward settlement tracking + realized P&L for phase 1 checkpoint."
