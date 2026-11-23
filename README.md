# kalshi-trading-bot

a 24/7 market-data pipeline and paper-trading harness for [kalshi](https://kalshi.com) prediction markets, built to answer one question honestly: **is there a tradeable edge here after fees?**

**answer: no, not with these models.** the conclusion holds up on data the model never saw (holdout) and in live forward tests. the infrastructure that produced that answer is the point of this repo.

| | |
|---|---|
| runtime | may 17 → nov 22 2026, 24/7 on a home pc (windows, sqlite in WAL mode) |
| data | **25.7m order-book snapshots**, 325k markets (272.7k resolved), 373k model predictions |
| paper trades | 649 crypto fills + 240 logged tennis bets, all settled |
| status | paper trading paused, live trading disarmed, pipeline still recording |

---

## results

every number below is **net of kalshi fees** unless marked gross. fee = `roundup(0.07 · C · P · (1−P))` per order (maker = 25% of taker), see `src/fees.py`.

| strategy | sample | result | verdict |
|---|---|---|---|
| crypto range markets, log-normal vol model (`crypto_vol_v1`) | 649 fills, $498 staked | gross +$4.57, **net −$11.22** (t = −0.44) | ❌ no edge |
| gate tuning on the same model | 384 configs × 3 market segments | **0 configs positive on holdout** | ❌ the best in-sample config was always the 70–80c band, where the fee eats the edge |
| `KXBTCD` daily above/below | n = 95 | +6.74% all-time → **−4.98% holdout** | ❌ sign flip |
| tennis favourite-longshot, pre-match | 166 settled bets, $444 | **−6.69%**; hit rate 0.831 (95% CI 0.767–0.881) vs 0.94 predicted | ❌ falsified |
| tennis, in-play | 74 settled bets, $182 | **−4.64%** vs +5.94% predicted | ❌ falsified |

### what went wrong, specifically

- **fees decide everything at this edge size.** the model's mean predicted probability was 0.420 vs market-implied 0.414 across 373k predictions. a 0.6pp average disagreement can't pay a fee that rounds up to a whole cent per order.
- **overconfidence at the tails.** in the [0.95, 1.01) bucket the model was 18.5pp too high. at a 75c price it computed edge 0.226; true edge was 0.041, so it thought its edge was **5.5× larger than it really was**. a threshold can't fix this; it's a defect in the model.
- **early results were small-sample noise.** v1 (+14%, n=51) and v2 (+24%, n=104) were both gross of fees and later reversed. v3 (−24%, n=22) was the first warning sign.
- **trying 384 configs on 42 days of data overfits.** by the minimum-backtest-length bound (MinBTL), 384 trials at sharpe 1 need ~11.9 years of data. after that, tuning the same data only finds more noise. new hypotheses need new data.

---

## what's worth looking at

| component | file | what it does |
|---|---|---|
| poller + catalog | `src/ingest/poller.py`, `discover.py` | polls up to 1,500 active markets every cycle into sqlite (~20k snapshots/hr) |
| settler | `src/ingest/settler.py` | resolves fills and markets against the kalshi api, writes realized pnl |
| daemon | `src/run/daemon.py` | single process: poll → predict → paper-trade → settle → discover. counts every reason a trade is skipped (5 of these used to be silent `continue`s) |
| fee model | `src/fees.py`, `src/rescore_fees.py` | exact kalshi fee schedule, and re-scores any fill range net of fees |
| holdout replay | `src/phase2/replay_gates.py` | offline gate replay with a train/holdout split and a trial counter, so the number of configs tried is always reported |
| calibration audit | `src/phase2/calibration_audit.py`, `fit_isotonic.py` | reliability by probability bucket; isotonic recalibration |
| tennis forward test | `src/phase2/tennis_bet_log.py`, `tennis_inplay_log.py`, `tennis_autotrade.py` | logs every bet the signal would place before the result is known, then scores them. live orders need two switches (`--live` flag + an arm file) and a `HALT` kill file |
| models | `src/strategy/crypto_vol.py`, `tennis_elo.py` | log-normal range pricing from coinbase spot + realized vol; surface-adjusted Elo |

### engineering problems solved along the way

- `database is locked` with concurrent writers → WAL mode + busy timeout
- a calibration join that matched each fill to every prediction for that market inflated the bucket counts 18×. fixed by joining each fill to its nearest prediction in time
- a watchdog that **killed and restarted a healthy daemon every 5 minutes for weeks**, so the daemon only ever traded in its first 60 seconds after a restart → fixed by counting root processes instead of all processes
- outcome labels backfilled for 269.8k markets and validated 100% against settlements before any calibration was refit

---

## running it

```powershell
py -3.12 -m venv .venv; .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env          # kalshi key id + private key path
python -m src.kalshi.smoke_test
python -m src.run.daemon --preset range_v4
```

`data/NO_PAPER_TRADE` present → the daemon records data but places no paper trades.

## what i'd do differently

1. **put fees in from day one.** every early "edge" was a gross-of-fees number.
2. **pre-register the test before looking at the data.** decide the sample size and the pass/fail line first. the tennis test was designed this way, which is why it gave a clean answer.
3. **count your trials.** a replay tool that doesn't report how many configs it tried will mislead you.
4. **fix the model before tuning thresholds.** threshold tuning can't fix a model that's miscalibrated at the tails.

deeper writeups: `PROJECT_STORY.md` (plain-language narrative), `HANDOFF.md` (technical log), `docs/KALSHI-HARNESS-ASSESSMENT.md`.
