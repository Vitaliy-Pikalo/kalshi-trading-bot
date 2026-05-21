# handoff doc — kalshi trading bot

**last updated:** 2026-05-19 (session 5 — investigation: 30 of 31 unsettled fills were pre-range_v1 contamination, NOT a filter bug. v3 cutoff established at fill_id ≥ 135. daemon restart pending.)
**phase:** 1.7 — long sample run on range_v1 preset (target n≥100 on between markets only)
**repo:** https://github.com/Vitaliy-Pikalo/kalshi-trading-bot (public)
**local:** `C:\Users\pikal\Downloads\claude project`

---

## v3 CUTOFF — read this first

**v3 = fill_id ≥ 135.** anything below 135 is pre-range_v1 (likely conservative-preset legacy) and must be excluded from range_v1 ROI calculations.

- fills 1–104: settled, included in v2 baseline (+24.4% ROI, n=104)
- fills 105–134: placed before range_v1 daemon started 2026-05-19 08:06 UTC. 29 BTC `greater` + 1 ETH `greater`. **legacy — exclude from v3.**
- fill 135: first true range_v1 fill (KXETH between @ 57c YES, placed 2026-05-19 15:44 UTC). unsettled at time of writing, closes 21:00 UTC same day.

**how to view v3-only stats:**
```powershell
.venv\Scripts\python.exe -m src.diagnose_v1 --min-fill-id 135
```

session 5 verified the range_v1 filter is **working correctly**: daemon log shows `strike_type` skip counter incrementing (6, 2, 8 skips across 3 trade cycles). only 1 fill placed under range_v1 so far, and it IS a between market. the 30 greater fills were placed by a prior daemon run (likely conservative preset) between 05-19 01:56 and 05-19 08:06 UTC.

---

## project context

kalshi prediction market trading bot. prices binary event markets using ML + sizes positions with poker bankroll math (fractional kelly, EV thresholds). passion project + quant internship signal + small real $ side project.

categories: **crypto** (BTC/ETH range markets — KXBTC/KXETH "between") primary. daily-greater (KXBTCD/KXETHD) suspended in range_v1 preset pending model rebuild.

**goal (resolved session 4):** OTM range-tail specialist on BTC/ETH between markets. accept high variance + lottery-ticket fat-tail structure as the edge. previous goal of "ATM dailies, predictable returns" was incompatible with where the alpha actually sits — bot wins on cheap YES range tails (10-30c price bucket), not ATM daily-greater. when phase 2 ML model is built it'll specifically target range-market mispricing + fix the [0.20, 0.40) overconfidence leak via isotonic recalibration. daily-greater can be revisited in a separate preset later if a working ATM model is built.

---

## status map

| phase | name | status |
|---|---|---|
| 0 | setup + skeleton | ✅ |
| 1 | data ingestion + paper trade | ✅ |
| 1.5 | v1 model verified, bugs fixed | ✅ |
| 1.6 | long daemon run, v2 baseline n=104 | ✅ |
| 1.7 | **range_v1 preset, n≥100 on between only** | ⏳ **in progress (session 4)** |
| 2 | ML edge model + isotonic calibration | next — start after range_v1 sample |
| 3 | poker-math layer (kelly + risk) | pending |
| 4 | go live + writeup | pending |

---

## v2 baseline (session 4, the latest)

run on conservative preset, ~36h of daemon time, n=104 settled fills.

| metric | v1 (n=51) | **v2 (n=104)** | delta |
|---|---|---|---|
| total fills | 51 | 134 (104 settled + 30 unsettled <1d) | +163% |
| ROI | +14.3% | **+24.4%** | +10.1pp |
| total realized | +$6.00 | **+$20.82** | +$14.82 |
| win rate | 29.4% | 60.6% | driven by ITM near-certs |
| cost | $42 | $85.18 | 2x |

### v2 by dimension (the structural read)

| dimension | v1 | v2 | conclusion |
|---|---|---|---|
| **between (range)** | +126% / n=17 | **+100% / n=24** | edge held on 1.4x sample — real |
| greater (daily) | -37% / n=34 | +2% / n=80 | "fixed" only by deep-ITM layups (70-95c bucket, 100% win rate, tiny ROI). no structural edge. |
| YES side | +65% | +49% | still positive |
| NO side | -69% | -6% | recovered via layups, not alpha |
| KXBTC (range) | +175% | +116% | n=18, holding |
| KXETHD (daily) | -83% | -27% | still bleeding |
| 10-30c price | +36% | **+50.7%** / n=19 | this is the real alpha bucket |
| 0-10c price | (mixed) | -1.4% / n=19 | cheap NO tails on dailies lose systematically |

### calibration v2

| bucket | v1 actual/exp | v2 actual/exp | n2 |
|---|---|---|---|
| 0.00–0.20 | 10%/10% ✅ | 8.7%/10% ✅ | 23 |
| **0.20–0.40** | **12%/30% 🔴** | **15.8%/30% 🔴** | 19 |
| 0.40–0.60 | 25%/50% | 63.6%/50% | 11 |
| 0.60–0.80 | 100%/70% | 100%/70% | 13 |
| 0.80–0.95 | 100%/88% | 100%/88% | 21 |
| 0.95–1.01 | 100%/98% ✅ | 100%/98% ✅ | 17 |

**leak persists:** vol model is overconfident in [0.20, 0.40) (says 30%, reality 16%) AND underconfident on 0.60+ (says 70-88%, reality 100%). classic ML calibration shape — needs isotonic in phase 2. range_v1 preset's `max_credible_edge=0.15` blocks most fills out of the leaky bucket as a stopgap.

**caveat:** ~$32 of the $20.82 PnL still comes from top 4 winners (fat-tail wins on range markets). lottery-ticket structure is real and expected on this edge — it's not a bug, it's the trade.

---

## bugs found + fixed this session

| # | bug | fix | verified |
|---|---|---|---|
| A | settler returns checked=0 on expired fills | FALSE ALARM — just ran too early, markets hadn't closed | ✅ confirmed via re-run |
| B | `src/analyze.py` cartesian Fill×Prediction join inflated buckets ~18× | replaced with nearest-prediction-by-ts logic (matches `diagnose_v1.py`) | ✅ buckets now match diagnose_v1 (20/17/4/3/6/1) |
| C | "database is locked" errors from dual daemons | sqlite WAL mode + 5s busy_timeout in `src/db.py` engine event listener | ✅ `_verify_db.bat` confirms WAL on |
| D | `datetime.utcnow()` deprecation | new `utcnow_naive()` helper in `src/utils.py`, applied to `diagnose_v1.py`. **still needs applying to settler.py + analyze.py.** | partial |

---

## new files / changes — session 4

- **`src/strategy/paper_sim.py`** — added `allowed_strike_types: list[str] | None = None` param to `paper_trade_once`. when set, predictions are still written for all markets (calibration intact) but fills are only placed for matching strike_types. new stats counter `trades_skipped_strike_type`.
- **`src/run/daemon.py`** — added `range_v1` preset alongside conservative/balanced/aggressive. configured as A+B from strategy fork: min_edge 5%, max_credible_edge 15%, min_price 10c, allowed_strike_types=["between"].
- **`_start_daemon.bat`** — switched default to `--preset range_v1`. banner updated. note to edit for conservative fallback.
- **`HANDOFF.md`** — v2 baseline + range_v1 rationale + fork D resolution.

## changes — session 3 (kept for ref)

- **`src/diagnose_v1.py`** — deep-dive analyzer
- **`src/verify_db.py`** — confirms WAL + pragmas
- **`_diagnose_v1.bat`** / **`_settle_and_diagnose.bat`** / **`_verify_db.bat`**
- **`_start_2_daemons.bat`** — warning added (don't use; will lock)
- **`src/analyze.py`** — calibration loop rewritten (bug B fix)
- **`src/db.py`** — sqlite pragma event listener (bug C fix: WAL, synchronous=NORMAL, busy_timeout=5000, FK on)
- **`src/utils.py`** — added `utcnow_naive()` helper

---

## what's running RIGHT NOW

**one daemon** (range_v1 preset) launched via updated `_start_daemon.bat`. logs to `_daemon.log`. target sample: **n≥100 settled fills on between markets only** (will take longer than v2 because gates are tighter — estimate 3-5 days).

range_v1 preset (session 4):
- min_edge **5%** (was 3%)
- max_credible_edge **15%** (was 30%) — blocks the [0.20, 0.40) overconfident bucket
- min_price **10c** (was 5c) — 0-10c bucket was -1.4% ROI
- max_price 95c
- dedup 4h, min_volume 1000
- **allowed_strike_types=["between"]** — range markets only (cuts daily-greater entirely)
- kelly_fraction 0.25, max_risk_per_trade 1%

old conservative preset still defined in `daemon.py` for A/B comparability — run with `--preset conservative` to reproduce v1/v2.

---

## current task / step / phase

**phase 1.7 — range_v1 sample run (effective n=1 as of session 5, daemon being restarted).**

**state at end of session 5:**
- range_v1 daemon ran from 05-19 08:06 to ~05-19 15:48 UTC (≈7.5h) before being killed
- placed exactly 1 fill (id=135) under range_v1 gates — closes 21:00 UTC today
- daemon was killed; will be restarted on range_v1 preset
- 30 legacy unsettled fills (105-134) will settle tonight regardless — exclude from v3

waiting for daemon to accumulate ≥100 settled fills **with fill_id ≥ 135, between markets only**. v2 had 24 between fills out of 104 total — range_v1 narrows to ~25-30% of v2's universe + tighter gates means estimate **5-7 days** (revised up from 3-5d after seeing ~1 fill / 7.5h pace).

monitor checkpoints (all on `--min-fill-id 135` view):
- n=10 (≈3 days): does between ROI still look like +50-100%? if catastrophic flip, kill + investigate.
- n=30 (≈5 days): does [0.20, 0.40) actually shrink under max_credible_edge=15%? expected bucket should drop to <3 fills.
- n=100 (≈2-3 weeks at current pace): v3 baseline. compare to v2's between-only slice (+100% / n=24).

**if pace is too slow** (still n<10 after 5 days): reconsider gates. options:
- loosen min_edge to 0.04 (was 0.03 → 0.05)
- loosen max_credible_edge to 0.20 (was 0.15) — risks reopening [0.20, 0.40) leak
- expand allowed_strike_types to include some bounded greater markets

---

## what still needs to be done

### immediate (next session, after n=10 checkpoint ≈3 days)

- [ ] re-run `_settle_and_diagnose.bat` for full picture
- [ ] **also run** `.venv\Scripts\python.exe -m src.diagnose_v1 --min-fill-id 135 > _diagnose_v3_output.txt` for v3-only stats
- [ ] confirm between-edge held under tighter gates (compare to v2's between-only +100%)
- [ ] confirm [0.20, 0.40) bucket shrunk (max_credible_edge=15% gate working)
- [ ] check skip counters in `_daemon.log`: how many predictions blocked by `strike_type` vs `edge_too_big`?
- [ ] update v3 cutoff in HANDOFF.md if daemon was restarted again (new floor = current max fill_id + 1)

### resolved this session (session 5)

- ✅ **investigated 30 of 31 unsettled fills being `greater` strike type.** verdict: not a filter bug. range_v1 filter (paper_sim.py:177-179) works correctly. those fills were placed by a prior daemon run between 05-19 01:56 and 05-19 08:06 UTC, before the range_v1 daemon started.
- ✅ **established v3 cutoff = fill_id ≥ 135.** documented at top of HANDOFF.md.
- ✅ **patched `src/diagnose_v1.py`** with `--min-fill-id N` flag for clean v3 slicing. default 0 preserves v2 behavior. v3 view: `--min-fill-id 135`.

### resolved this session (session 4)

- ✅ **fork D resolved → path 1 (embrace the edge).** goal updated: OTM range-tail specialist on BTC/ETH between markets. "predictable returns" goal abandoned as incompatible with where the alpha sits.
- ✅ **fork A+B implemented** as the `range_v1` preset (between-only + min_edge 5% + max_credible_edge 15% + min_price 10c).
- ✅ conservative preset preserved in `daemon.py` for A/B comparability.
- ✅ `src/strategy/paper_sim.py` extended with `allowed_strike_types` param. predictions still written for ALL markets (calibration stays alive), only fills are filtered.

### phase 1 polish

- [ ] finish `datetime.utcnow()` deprecation: apply `utcnow_naive()` to `src/ingest/settler.py`, `src/analyze.py`, `src/ingest/poller.py`, `src/strategy/paper_sim.py`, etc.
- [ ] tennis surface lookup expansion (french open = clay, may 24+)
- [ ] add coinbase spot to snapshot table (or separate spot_history) for ML features

### phase 2 (after sample + decision)

- [ ] feature engineering (`src/features/crypto.py` already scaffolded)
- [ ] xgboost training (`src/models/train.py` — needs creating)
- [ ] isotonic calibration of raw model → real probability (specifically to fix the [0.20, 0.40) overconfidence bucket)
- [ ] backtest harness with time-series CV (no future leakage)
- [ ] ML baseline implementing `Baseline` interface

### phase 3 (when ML beats baseline)

- [ ] portfolio-level kelly (correlated positions)
- [ ] dynamic position sizing based on edge confidence
- [ ] drawdown circuit breaker
- [ ] streamlit dashboard

### phase 4 (when backtest sharpe > 1)

- [ ] switch is_paper=0
- [ ] start with $100 bankroll
- [ ] blog post + README polish

---

## next step (for fresh chat)

paste this into the new chat to resume:

> i'm continuing my kalshi trading bot. repo: https://github.com/Vitaliy-Pikalo/kalshi-trading-bot. read HANDOFF.md in C:\Users\pikal\Downloads\claude project. daemon has been running ~[N] hours on `range_v1` preset since session 5 restart. **v3 cutoff = fill_id ≥ 135** (everything below is pre-range_v1 legacy, must be excluded). kill via `_kill_python.bat`, then run BOTH `_settle_and_diagnose.bat` (full picture) AND `.venv\Scripts\python.exe -m src.diagnose_v1 --min-fill-id 135 > _diagnose_v3_output.txt` (v3-only). compare v3 stats to v2's between-only slice (+100% ROI, n=24): does the edge hold under tightened gates? did [0.20, 0.40) bucket shrink? then decide: continue running to n=100, loosen gates if pace is too slow, start phase 2 ML build, or adjust strategy. session 5 verified the filter works; n=1 effective sample at restart. v2 numbers + range_v1 rationale in HANDOFF.md.

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
- **single daemon only** until separate-db-per-strategy is built (bug C constraint)

---

## key files for next session

1. **HANDOFF.md** (this file)
2. **`_diagnose_v1_output.txt`** / **`_settle_and_diagnose_output.txt`** (v1 true baseline data)
3. **`_daemon.log`** (current run)
4. `src/run/daemon.py` (scheduler)
5. `src/strategy/paper_sim.py` (gates + dedup)
6. `src/strategy/crypto_vol.py` (prediction model — the [0.20, 0.40) overconfidence lives here)
7. `src/db.py` (data model, now with WAL pragmas)
8. `src/diagnose_v1.py` (deep-dive analyzer)

---

## quickstart commands

```powershell
cd C:\Users\pikal\Downloads\claude project

# verify infrastructure
double-click _verify_db.bat              # confirms WAL + pragmas
.venv\Scripts\python.exe -m src.kalshi.smoke_test

# check daemon status
double-click _daemon_status.bat

# stop daemon
double-click _kill_python.bat

# settle + analyze (safe to run while daemon is up — WAL handles concurrency)
double-click _settle_and_diagnose.bat    # most useful single command

# restart daemon
double-click _start_daemon.bat           # use this one, NOT _start_2_daemons.bat
```

---

## one-line elevator pitch

"i built a kalshi prediction market trading bot from scratch in python — RSA-authenticated API client, sqlite event store, log-normal volatility pricing on BTC/ETH dailies, surface-adjusted Elo for ATP/WTA matches, fractional kelly position sizing from poker bankroll theory, running 24/7 as a daemon against live kalshi prod data. paper trading; v1 baseline shows +14.3% ROI on n=51 with edge concentrated in cheap-tail range markets. switching to real $ after ML model + 100-trade backtest validation."
