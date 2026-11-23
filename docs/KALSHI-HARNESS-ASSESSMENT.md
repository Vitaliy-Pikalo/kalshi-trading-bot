# does the options-quant harness port to Kalshi? — assessment

**date:** 2026-08-18
**scope:** read-only inspection of `C:\Users\pikal\Downloads\claude project`. no file in that
project was modified, no daemon touched, no order placed.

---

## READ THIS FIRST

**`data/LIVE_TRADING_ARMED` contains `armed`, written 2026-08-17.** the tennis
favourite-longshot signal is armed for live execution while its forward test has
**4 settled bets.** your own handoff v3 says it needs ~200.

`data/tennis_orders.csv` shows `status=dry_run` on both rows, so no real money has moved yet.
That is the only reason this is a warning and not a loss.

**disarm it until the forward test has the n it was designed to have.**

---

## the 4 settled bets are not evidence

| | |
|---|---|
| logged | 39 |
| taken | **4** |
| settled | **4** |
| won | **4 (100%)** |
| ROI | +13.64% |
| naive t-stat | **+29.4** |

**the t-stat is an artifact and the win rate is a coin flip.**

- all four bets were bought at ~$0.87 for a $3.00 payout, so every winner returned almost
  exactly $0.36 (sd **$0.024**). the standard error is tiny because the *payoffs* are nearly
  identical, not because the *result* is certain. that is the same class of error as quoting
  `t_naive` — the variance that matters is win/lose, not payout size.
- **P(4 wins in a row | the market price is exactly right) = 0.87⁴ = 0.573.** four-for-four
  is the single most likely outcome under the null. it is not a signal, it is the base rate.
- all 4 are the same band (0.85–0.90), the same side (YES), and none is the NO-side leg that
  carried the stronger holdout result (+7.33%).

### how many bets you actually need

per bet: cost $2.64, payout $3.00, so win = +$0.36, lose = −$2.64.

| assumed true win prob | EV per bet | sd | n for t = 2 |
|---|---|---|---|
| 0.9423 (your logged `hist_rate`) | +$0.187 | 0.700 | **56** |
| 0.90 (modest edge) | +$0.060 | 0.900 | **900** |
| 0.88 (tiny edge) | −$0.000 | 0.975 | never |

**you have 4.** even on the most optimistic assumption in your own log you are 52 bets short,
and if the real edge is half what the backtest says, the requirement is an order of magnitude
larger. handoff v3's "~200 bets" was the right instinct.

---

## a concrete bug in the order path

`tennis_orders.csv`, both rows written at the same timestamp:

```
KXATPCHALLENGERMATCH-26AUG17HERDJE-HER   no    3 @ 91   band 0.05-0.10
KXATPCHALLENGERMATCH-26AUG17HERDJE-DJE   yes   3 @ 91   band 0.90-0.95
```

`HERDJE` is one match: Heredia vs Djere. **In a two-player market, "NO on Heredia" and "YES
on Djere" are the same claim.** That is $5.50 on a single outcome, booked as two independent
positions in two different bands.

consequences:

1. **position sizing is 2× what you think** on any match where both legs pass the gate
2. **the forward-test count is inflated** — those are 1 observation, not 2
3. **the band attribution is wrong** — the same wager is credited to both the 0.05–0.10 NO
   band and the 0.90–0.95 YES band, which is exactly the pair your holdout table treats as
   two separate strategies

fix: dedupe candidates by match key (`ticker.rsplit('-', 1)[0]`) before sizing, and keep the
single leg with the better EV. also add the match key as the clustering unit in every
statistic — bets on the same match are not independent draws.

---

## does the harness port? yes — and here is the mapping

| options-quant tool | Kalshi equivalent | why it transfers |
|---|---|---|
| pre-registration in the docstring | same, verbatim | it is a discipline, not code. v3 already did this for the favourite-longshot direction — **keep doing it** |
| simulated-null validation | **shuffle the outcome labels** within band and re-run the ROI | if a shuffled book shows +5% ROI, the estimator is broken. this is the single highest-value port |
| coin-flip / random control (`orb_eval`) | **bet a random side at the same prices** | measures how much of the ROI is the price structure rather than the rule. ORB's raw edge was 11.03¢ and 8.11¢ of it was the coin flip |
| **concentration test** (`vrp_eval` V6) | ROI by week / by tournament / by band | **the highest-value port.** it killed ORB (best quarter 54.6%) and cleared the VRP (15.5%). tennis will have tournament-level clustering that looks like edge |
| Driscoll-Kraay / cluster-robust SEs | **cluster by match, then by day** | two legs of one match are one draw. matches on one day share weather, surface, and scheduling |
| out-of-sample split, nothing fitted on holdout | already done in v3 | v3's train/holdout is correctly constructed. keep the gate threshold fitted on train only |
| capture / oracle ratio | ROI vs the max achievable ROI at those prices | stops a statistically-real-but-economically-tiny edge from looking tradeable |
| `t_naive` is meaningless | **applies directly, see above** | the +29.4 is precisely this failure |

**what does NOT port:** the Driscoll-Kraay *cross-sectional* term. In equities six tickers
share a market factor; tennis matches do not share a common price driver in the same way.
Cluster by match and by day; skip the panel machinery.

---

## why Kalshi is the better pond, stated honestly

the argument that survives from the options work:

- **capacity, not cleverness.** a fund with $500M cannot be bothered with a market that
  absorbs $20k. your size is an advantage here and was a liability in SPX
- **your v3 result is the right shape.** favourite-longshot bias is a documented, pre-specified
  direction, positive in both train and holdout, consistent across three months, and net of
  fees. that is a genuinely better starting position than anything the options project ever had
- **median spread measured at 1.0¢** — the blocker that killed every options idea is smaller
  here by orders of magnitude

the argument against, equally honestly:

- **+5% ROI on 3-contract bets is $0.13 per bet.** at 10 bets/day that is ~$1.30/day gross.
  the edge has to be real *and* you have to scale it before it is income
- **retention is a moving window.** ~8 weeks of trade history, per v3. the sample cannot be
  grown backwards, only forwards. that makes the forward test the only source of new evidence —
  which makes arming early even more costly, because it spends the trial budget on real money
- **the favourite-longshot bias is well documented, which means it is well known.** on Kalshi
  specifically it may persist because of who trades there; that is an assumption, not a finding

---

## recommended sequence

1. **disarm.** `data/LIVE_TRADING_ARMED` → remove or set to `disarmed`. today.
2. **fix the same-match double-count** in the order path, and add match key as the clustering
   unit everywhere.
3. **port the three controls** — shuffled-outcome null, random-side control, concentration by
   week/tournament/band. each is ~30 lines and each has already caught a real error once.
4. **run the forward test to n ≥ 200 taken bets in dry-run**, with the gate frozen. no
   parameter changes mid-test; a changed gate restarts the count.
5. **pre-register the decision rule before the test finishes**, e.g.:
   ```
   K1  ROI over >=200 settled bets is positive with match-clustered t > 2
   K2  the shuffled-outcome null returns ROI ~ 0
   K3  the random-side control returns materially less than the rule
   K4  no single week or tournament carries >40% of P&L
   K5  the NO-side band replicates, not just the YES side
   ```
6. **only then** arm, and size at a fraction of bankroll that survives a 20-bet losing streak.

at 10 taken bets/day, 200 bets is ~3 weeks. that is the actual cost of doing this properly,
and it is cheap.

---

## what I did not do

I did not modify anything in the Kalshi project, stop the daemon, disarm the flag, or place
or cancel any order. The flag is yours to change; I am only reporting that it is set and that
the evidence behind it is 4 bets.
