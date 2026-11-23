# poker-quant-bot (Kalshi) — handoff v2

> supersedes `HANDOFF.md` dated 2026-06-14. that doc's central premise is **overturned** — see "corrections" below.
> all code described here is applied to disk. nothing is pending.

**path:** `C:\Users\pikal\Downloads\claude project` (the parent dir; `options-quant-bot` is a separate project inside it)
**repo:** https://github.com/Vitaliy-Pikalo/kalshi-trading-bot

---

## THE VERDICT

**`crypto_vol_v1` has no demonstrable edge in any market segment.**

384 configs swept per segment, ranked on a training period, scored on a held-out period:

| segment | train | holdout | degradation |
|---|---|---|---|
| between (range) | +11.11% (n=81) | **−2.66%** (n=121) | −13.77% |
| greater (daily) | +15.07% (n=217) | **−4.98%** (n=234) | −20.06% |
| both | +13.13% (n=359) | **−3.90%** (n=356) | −17.03% |

**not one config in any segment survived.** and the best-on-train config was `min_price=70, max_price=80` in **all 45 top rows across all three segments** — i.e. `range_v3`, the preset that had been running live. the tool rediscovered it from the training half and falsified it on the holdout half. that band always looks best in-sample (75% win rate → low in-sample variance) and it is where the fee eats everything.

precision: standard error at these n is roughly ±5–9pp, so the holdout does **not** prove these configs lose money. it proves they aren't +11% to +15%. the honest statement is *no edge is demonstrable, and in-sample rankings are actively misleading.*

### the series-level trap, and why it is a trap

holdout broke out by series and ETH looked good while BTC looked bad. it does not replicate:

| series | all-time net (rescore_fees) | holdout net (replay) | sign flip |
|---|---|---|---|
| KXBTC | +4.23% | −8.61% | **yes** |
| KXETH | −0.40% | −24.92% | no |
| KXBTCD | **+6.74%** | −7.38% | **yes** |
| KXETHD | **−26.68%** | +6.40% | **yes** |

**3 of 4 series flipped sign between periods.** series-level performance is not a property of the market, it is noise. this also retroactively explains `KXBTCD +6.74%` — the number that made daily-greater look worth reviving. do not chase it.

---

## corrections to handoff v1

| v1 claimed | reality |
|---|---|
| "[0.40,0.80) underconfidence is alpha LEFT ON THE TABLE" (+21 to +28pp) | **artifact of a partially-backfilled `settled_outcome` column.** with complete data [0.60,0.80) reads 71.6% → 71.9%. the model is *over*confident at the top, not under |
| v2 +24.4% ROI, v3 −24.4%, v4 +0.1% | all **gross of fees**. `paper_sim.py` modelled none. v4 net = **−2.63%** (taker) / −1.33% (maker) |
| "edge lives in the 10-30c cheap tail" (+50.7%, n=19) | **−80.3% net** in v4 (n=18). the tail thesis is falsified |
| `range_v3`: "ONLY 70-80c is profitable (+5% ROI)" | selected on n=115 **gross**. net of fees that band earns **+0.01%** — it is the fee, not an edge |
| watchdog keeps the daemon alive | **it killed and restarted a healthy daemon every 5 minutes for weeks** |

---

## what was built this session

| file | purpose |
|---|---|
| `src/fees.py` | Kalshi fee: `roundup(0.07·C·P·(1−P))`, maker = 25%. the round-up dominates single-contract orders |
| `src/rescore_fees.py` | re-score any fill range net of fees. read-only |
| `src/phase2/derive_outcomes_from_fills.py` | outcome from `fills.realized_pnl`. zero API calls |
| `src/phase2/resolve_from_spot.py` | **resolve every BTC/ETH market from free Coinbase 1m candles.** refuses to call within 5bp of a strike. self-validates against known outcomes |
| `src/phase2/calibration_audit.py` | calibration under 4 sampling schemes + by time-to-close |
| `src/phase2/replay_gates.py` | **offline gate sweep with time-based holdout + MinBTL trial counter** |

**`resolve_from_spot.py` is the unlock.** it took ground truth from ~658 markets to **269,806**, validated at **100.0000% agreement on 8,689 overlapping markets, zero mismatches**. that converted a two-month experiment into a 20-second one.

### code changes
- `paper_sim.py`: `fee_aware` gate (edge must exceed fee/stake + margin, derived from the fee formula, not fitted); **every skip path now counted** — five were silent `continue`s
- `daemon.py`: `range_v4` preset; expanded TRADE log line
- `_watchdog.ps1`: **counts root processes only** (see below)
- `_daemon_stop.bat` / `_kill_python.bat`: `exit` → `exit /b` (bare `exit` killed the parent terminal)

---

## the watchdog bug (biggest operational finding)

one launch of `_daemon_run.bat` creates **two** `python.exe` processes with identical command lines and identical creation timestamps — a venv launcher plus the real interpreter. `Get-DaemonProcs` counted both, so `Count -gt 1` was **always true**:

```
13:01:28  2 daemon processes running -> duplicates, killing all and starting one
13:01:34  daemon launched
13:07:15  2 daemon processes running -> duplicates, killing all and starting one
```

**the daemon never survived one 5-minute tick.** trade cycle is 300s, `DISCOVER` eats ~60s of it. that is the real reason fills accumulated at ~6/day, and likely the cause of the 28-day July gap.

fixed by filtering to processes whose parent is *not* also a daemon. genuine duplicates (two independent launches → two roots) are still caught. **verified live:** watchdog log went quiet, and the daemon ran 17:12 → 17:23+ with two TRADE cycles from a single start.

---

## calibration: the real shape

scheme 4 — prediction nearest each actual fill, n=355:

| bucket | n | share of fills | predicted | actual | gap |
|---|---|---|---|---|---|
| [0.20, 0.40) | 196 | 55% | 26.5% | **27.0%** | **+0.6pp** |
| [0.95, 1.01) | 115 | 32% | 97.6% | **79.1%** | **−18.5pp** |

the model is **well calibrated in its main working range** and **badly overconfident at the top**. those map onto sides: [0.20,0.40) → buys NO (profitable side); [0.95,1.01) → buys YES (losing side).

sizing consequence: at a 75c price the bot computes `edge = 0.976 − 0.75 = 0.226`. true edge is `0.791 − 0.75 = 0.041`. **it believes its edge is 5.5× larger than it is** — and `range_v2` raised `max_credible_edge` 0.15 → 0.40 specifically to let those through.

`iso_v1_between.pkl` maps 50% → 97%, pushing probabilities *up* in the bucket already 18.5pp too high. **do not re-enable calibration until refit on the corrected dataset.**

---

## status map

| item | state |
|---|---|
| fee model | ✅ built, gated at entry (not yet in `cost_usd` — needs a schema migration) |
| outcomes ground truth | ✅ 269,806 markets, validated 100% |
| offline gate replay + holdout + trial counter | ✅ built |
| watchdog kill loop | ✅ fixed, verified live |
| skip-path observability | ✅ every path counted |
| gate tuning on `crypto_vol_v1` | ❌ **CLOSED — no config survives holdout in any segment** |
| `KXBTCD` daily-greater | ❌ **CLOSED.** −4.98% holdout; its +6.74% all-time flipped sign |
| isotonic refit on corrected data | ❌ not done — but pointless while no gate has edge |
| tennis Elo baseline | ❓ **never evaluated.** registered in `BASELINES`, ~18,000 tennis markets in db |
| fills clustered near daemon restarts? | ❓ open — the daemon only ever traded in its first 60s |

---

## current task / step / phase

**phase 3.0 — gate tuning is closed. the open questions are all about untested segments and the model itself.**

`range_v4` is live (v6 cutoff = **545**). but it is placing **zero fills**, and the log says why:

```
TRADE: seen=12 preds=12 fills=0 skip(min_edge=5, strike_type=7, dedup=0, ...)
```

7 of 12 candidates are the wrong `strike_type`; the other 5 fail the 5% edge floor. **`range_v4` may never trade**, and given the replay says no gate config has edge, letting it run to accumulate a sample is probably not worth doing.

---

## what still needs to be done

**the trial budget is spent.** 384 trials over 42 days (0.12 years). MinBTL at Sharpe 1 for 384 trials is **11.9 years**. further sweeping of the same data finds noise faster — new hypotheses need genuinely new data or forward testing.

ordered by expected value:

1. **replay the tennis baseline.** never evaluated, ~18,000 markets, and **uncorrelated with crypto** — so it adds genuinely independent sample rather than reslicing the same 42 days. requires resolving tennis markets (no spot price; needs match results or the Kalshi API for recent ones).
2. **replay `KXBTCD` daily-greater.** one flag in `replay_gates.py`. +6.74% net all-time on n=95 and it was suspended on the same class of reasoning that produced `range_v3`.
3. **check fill-time clustering.** the daemon only traded in its first ~60s post-restart for weeks. if opportunity composition differs right after `DISCOVER`, the historical fills aren't a random sample. cheap to check against `replay_dataset.csv`.
4. **fix the model, not the gates.** −18.5pp overconfidence at [0.95,1.01) is a model defect. no threshold fixes it.
5. **fees into `cost_usd`** (schema migration) so `diagnose_v1` reports net without `rescore_fees`.

---

## next step

paste into a fresh session:

```
read the handoff at kalshi-handoff-v2.md. gate tuning on crypto_vol_v1 is CLOSED
-- 384 configs x 3 segments (between / greater / both), every one negative on
holdout, and 3 of 4 series flipped sign between periods. do not sweep gates
again, and do not chase a series-level or price-bucket result on this data.

next: the tennis Elo baseline. it is registered in BASELINES, there are ~18,000
tennis markets in the db, and it has NEVER been evaluated. it is also the only
thing in this project uncorrelated with the BTC/ETH price path, so it adds
genuinely independent sample rather than reslicing the same 42 days.

step 1 only: tennis markets cannot be resolved from spot price. work out how to
populate markets.settled_outcome for KXATP*/KXWTA* -- match results from a free
source, or the Kalshi API for recent ones (it 404s on markets older than about a
month). report the options with row counts before writing any code.
```

---

## workflow rules

1. one step at a time; paste output and verify before the next.
2. manual intervention (auth, sudo, Task Scheduler, vendor SDK) → hand back with instructions.
3. **no invented numbers.** name the gap and its cause.
4. **pre-register every prediction.** every real finding this session came from predicting the number first. a fix that doesn't move the number it should move is mis-wired.
5. acknowledge and correct mistakes explicitly. this doc corrects five v1 claims and two of mine.
6. **gross-of-fees numbers are not results.** `rescore_fees.py` exists for this.
7. **read the HOLDOUT column.** train ROI on a 384-trial sweep is a ranking of noise.
8. **single daemon only** — but verify with root-process counting, not raw process count.
9. lead with the verdict; tables over prose; metric units.
