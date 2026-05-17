# handoff doc — kalshi trading bot

**last updated:** 2026-05-16 (session 2 wrap, daemon running)
**phase:** 1 complete-ish (pipeline working end-to-end, daemon running overnight to accumulate data)
**repo:** https://github.com/Vitaliy-Pikalo/kalshi-trading-bot (public)
**local:** `C:\Users\pikal\Downloads\claude project`

---

## project context

kalshi prediction market trading bot. prices binary event markets using ML + sizes positions with poker bankroll math (fractional kelly, EV thresholds). passion project + quant internship signal + small real $ side project.

categories chosen: **crypto** (BTC/ETH daily) primary, **tennis** (ATP/WTA) secondary. running both in parallel.

**user goal:** narrow + nail one market type for predictable consistent returns. focus on ATM BTC dailies during Tokyo hours (0-8 UTC). low risk, not high frequency. paper now, real $ in phase 4.

---

## status map

| phase | name | status |
|---|---|---|
| 0 | setup + skeleton | ✅ |
| 1 | data ingestion + paper trade | ✅ pipeline done, daemon running for data |
| 2 | ml edge model + backtest | next — start by reviewing daemon results |
| 3 | poker-math layer (kelly + risk) | pending |
| 4 | go live + writeup | pending |

---

## what's running RIGHT NOW

a **daemon** at `src/run/daemon.py` is (or should be) running in the background. it:

- polls 768+ active markets every **60 sec** → snapshots table
- runs paper sim every **5 min** with conservative gates → predictions + fills
- settles closed fills every **30 min** → realized P&L
- refreshes market catalog every **6 hours**
- logs every action to `_daemon.log`

**conservative gates** (the active preset):
| gate | value | purpose |
|---|---|---|
| min_edge | 3% | don't trade noise |
| max_credible_edge | 30% | skip "too good to be true" (model bug) |
| min_price_cents | 5 | avoid 1¢-4¢ tail markets (huge round-trip) |
| max_price_cents | 95 | same on the other side |
| dedup_window | 4 hours | don't re-trade same market |
| min_volume | 1000 | market must have real activity |
| kelly_fraction | 0.25 | quarter-kelly (poker standard) |
| max_risk_per_trade | 1% of bankroll | hard cap |

---

## first 5 things to do in next session

1. **check the daemon is still alive + healthy**
   ```
   double-click _daemon_status.bat
   ```
   - look at _daemon.log tail: should see POLL/TRADE/SETTLE entries
   - look at db state: snapshots should be 10k+, predictions 2k+, fills 200+
   - look at realized_pnl: this is the headline number
2. **run analysis**
   ```
   .venv\Scripts\python.exe -m src.analyze
   ```
   - win rate, total P&L, calibration plot
3. **identify what worked**
   - which series_ticker had best return? (KXBTCD likely)
   - which strike-distance-from-spot bucket worked best?
   - what time of day did best (Tokyo 0-8 vs NY 13-21 UTC)?
4. **tune gates based on data** — narrow further to what worked
5. **decide phase 2 model** — xgboost on the features we now have, or stay with vol baseline + better tuning

---

## what's been done

### phase 0 (committed)
RSA-signed kalshi client, sqlite schema, pydantic config, smoke test, public github repo

### phase 1 (committed across 5 commits)
- expanded kalshi client (events, market detail, orderbook, bulk fetch, rate limit + retry)
- market discovery + classifier (crypto + tennis, scoped to BTC/ETH + ATP/WTA)
- coinbase spot/vol client
- snapshot poller (bulk fetch, handles `*_dollars` field names)
- crypto vol baseline (log-normal, clipped to [0.5%, 99.5%])
- tennis Elo baseline (Sackmann data, surface-adjusted, predicts P(player A wins))
- paper trade simulator with dedup + max-edge gate + price gate + volume floor
- settlement worker (turns paper fills into realized P&L)
- analyzer (win rate, sharpe, calibration)
- diagnose script (edge histogram, liquid markets sample)
- production daemon (set-and-forget, multi-stage scheduler)

### key wins
- **end-to-end pipeline functional on PROD** with real kalshi auth + real liquidity
- **field-name bug fixed** (kalshi returns `yes_bid_dollars` string not `yes_bid` int)
- **prob clipping** removes phantom +99% edges (log-normal underestimates tails)
- **dedup window** prevents duplicate fills on same market
- **5 commits pushed to github**

---

## current task / step / phase

**daemon is running overnight**. next session:
1. check what it accumulated (snapshots, fills, settled, realized P&L)
2. analyze: did the bot make or lose money? on which markets?
3. tune the strategy or pivot to ML based on findings

---

## what still needs to be done

### immediate (next session)
- [ ] verify daemon was healthy overnight (no crashes)
- [ ] settle remaining fills, get realized P&L
- [ ] analyze by market type, by time-of-day, by strike-distance
- [ ] decide: continue tuning vol baseline OR move to xgboost ML

### phase 1 polish
- [ ] datetime.utcnow() deprecation fix (replace with `datetime.now(timezone.utc)`)
- [ ] tennis surface lookup expansion (french open = clay, may 24+)
- [ ] add coinbase spot to snapshot table (or separate spot_history table) for ML features

### phase 2 (when data validates)
- [ ] feature engineering (see `src/features/crypto.py` — already scaffolded)
- [ ] xgboost training (`src/models/train.py` — needs creating)
- [ ] isotonic calibration of raw model output → real probability
- [ ] backtest harness with time-series CV (no future leakage)
- [ ] ML baseline implementing Baseline interface (loads trained model + predicts)

### phase 3 (when ML beats baseline)
- [ ] portfolio-level kelly (correlated positions)
- [ ] dynamic position sizing based on edge confidence
- [ ] drawdown circuit breaker
- [ ] streamlit dashboard

### phase 4 (when backtest sharpe > 1)
- [ ] switch is_paper=0 (real $ trading)
- [ ] start with $100 bankroll
- [ ] write public blog post
- [ ] github README polish + open-source

---

## next step (for fresh chat)

paste this into the new chat to resume context:

> i'm continuing my kalshi trading bot project. repo: https://github.com/Vitaliy-Pikalo/kalshi-trading-bot. read HANDOFF.md in the project folder. a daemon has been running overnight at `_daemon.log` — start by running `_daemon_status.bat` to see what we have, then `python -m src.analyze` for P&L. then help me decide next steps based on results.

---

## workflow rules

- one phase at a time
- one task at a time with explicit verification
- backtest before paper trade, paper trade before real $
- log everything to sqlite (markets, snapshots, predictions, fills, bankroll)
- git commit after every working feature
- max risk: 1% bankroll per position (fractional kelly k=0.25)
- never paste private keys, passwords, or KYC info into chat
- use `_*.bat` scripts for command execution (gitignored). real code in `src/`
- PROD env OK for read-only. orders require explicit user approval (phase 4)

---

## key files for next session

read in this order:
1. **HANDOFF.md** (this file)
2. **_daemon.log** (or `_daemon_cons.log`, `_daemon_bal.log` if dual run)
3. `src/run/daemon.py` (current scheduler)
4. `src/strategy/paper_sim.py` (gates + dedup logic)
5. `src/strategy/crypto_vol.py` (prediction model)
6. `src/db.py` (data model)

---

## quickstart commands

```powershell
cd C:\Users\pikal\Downloads\claude project

# verify all still works
.venv\Scripts\python.exe -m src.kalshi.smoke_test
.venv\Scripts\python.exe -m src.diagnose

# check daemon status
double-click _daemon_status.bat

# stop daemon if needed
double-click _daemon_stop.bat

# settle + analyze on demand
.venv\Scripts\python.exe -m src.ingest.settler
.venv\Scripts\python.exe -m src.analyze

# restart daemon
double-click _start_daemon.bat
```

---

## one-line elevator pitch

"i built a kalshi prediction market trading bot from scratch in python — RSA-authenticated API client, sqlite event store, log-normal volatility pricing on BTC/ETH dailies, surface-adjusted Elo for ATP/WTA matches, fractional kelly position sizing from poker bankroll theory, running 24/7 as a daemon against live kalshi prod data. paper trading currently; switching to real $ after backtest validation."
