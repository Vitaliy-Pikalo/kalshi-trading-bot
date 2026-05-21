# kalshi trading bot — the story

a plain-language writeup of what this project is, what i built, what i learned, and where it's going. written for anyone — no quant background required.

---

## tl;dr

i built a trading bot that bets on prediction markets (specifically kalshi, where you can buy contracts that pay out based on real-world events like "will bitcoin close above $77,000 on may 21?"). it's been running 24/7 against the live kalshi API in paper-trading mode for about a week.

the bot's job: find markets where the price doesn't match what the actual probability should be, then place bets sized using poker bankroll math.

three baselines locked so far:
- **v1** (n=51 fills): +14.3% return on investment (ROI)
- **v2** (n=104 fills): +24.4% ROI — looked great
- **v3** (n=22 fills, controlled experiment): **-24.4% ROI**

the v3 result is the punchline. it killed the v2 hypothesis. the "edge" i thought i'd found was variance from a small sample. the next phase is fixing the underlying model so the bot can find real edge instead of noise.

if you're a recruiter or someone evaluating this as a quant signal: the negative result is more important than the positive ones. that's the whole point.

---

## what is this thing

### prediction markets in 90 seconds

kalshi is a regulated US prediction market. you can buy contracts that pay $1 if some event happens and $0 if it doesn't. the market price (between $0.01 and $0.99) is basically the market's estimate of the probability.

example: "will bitcoin be between $77,000 and $77,500 at 6pm EDT on may 21?" might trade at 22 cents. that means the market thinks there's a 22% chance.

if you think the real probability is higher than 22%, you buy YES. if you think it's lower, you sell YES (or buy NO). over many bets, if you're systematically more accurate than the market price, you make money.

### the strategy in one sentence

look at every bitcoin and ethereum price-range market kalshi offers, compute what the real probability should be using a math model of how crypto prices move, find markets where the real probability disagrees with the market price by enough to be worth betting, and size each bet so a bad streak doesn't blow up the bankroll.

### why bother

three reasons, in order of weight:
1. **quant internship signal.** i'm a student. employers want to see "can this person build a real thing end-to-end, instrument it, run a controlled experiment, and read the results honestly." a paper trading bot with versioned baselines and a calibration analysis hits all of those.
2. **passion.** i like poker, statistics, and prediction markets. this is the cross-section.
3. **small real-money side project.** if the bot eventually proves it has edge, it'll go live with $100 of real bankroll. not life-changing money — proof of concept.

---

## how the bot works, in english

four moving parts:

### 1. data ingestion (the eyes)

every 60 seconds, a python process called the "poller" hits kalshi's API, asks for the current state of every market i care about (bitcoin range markets, ethereum range markets, tennis match markets, and some daily greater-than markets), and writes the bid/ask/volume to a sqlite database.

after a week of running, the database has:
- **270,000+ market snapshots** (and growing by ~30k/day)
- **13,000+ predictions** (model output for each snapshot)
- **157 paper-trade fills**

sqlite is overkill simple. that's the point. fast, durable, easy to query, doesn't need a server.

### 2. the model (the brain)

for crypto range markets, the model uses **log-normal volatility pricing**. plain version:

given the current bitcoin spot price and how much it's been moving lately (its volatility), assume bitcoin's future price follows a log-normal distribution. then compute the probability of bitcoin landing between any two prices by integrating that distribution. compare to the market price. if they disagree by more than a threshold, that's an "edge".

example:
- bitcoin spot = $77,200
- recent volatility = X
- market: "between $77,000 and $77,500 at 6pm" trading at 22c (= 22% implied)
- model says: 35% probability
- edge = 35% - 22% = 13 percentage points → place YES bet

for tennis, the model uses **surface-adjusted Elo** — same Elo rating system used in chess, but with separate ratings per court surface (hard / clay / grass) because players have very different skill profiles on different surfaces.

both models output a single number per market per snapshot: the predicted probability.

### 3. position sizing (the wallet)

even when you have edge, betting too much will blow you up on a bad streak. this is where poker math comes in.

**fractional kelly sizing.** the kelly criterion is the mathematically optimal bet size to maximize long-run growth, but it's volatile — losing streaks hurt badly. fractional kelly (i use k=0.25, so "quarter kelly") trades some long-run growth for way less variance.

formula in plain words: bet a fraction of your bankroll proportional to your edge divided by the odds against you, times 0.25.

i also cap any single bet at **1% of bankroll** as a hard ceiling. so even if the model says "this is the trade of the year, bet huge", the bot won't.

### 4. the daemon (the heartbeat)

all of the above runs as a single long-running python process called the daemon. every cycle, it:
1. polls fresh market data
2. runs the model on every active market
3. writes predictions to the database
4. checks the dedup table (don't bet the same market twice within 4 hours)
5. for each market with sufficient edge: places a paper-trade fill, records it
6. periodically settles closed markets, calculating realized PnL

it runs 24/7. logs every cycle. survives restarts because the database is the source of truth.

---

## the scientific journey

this is the part that matters. i ran three versions, each time getting cleaner data, and the conclusions changed each time.

### v1 — first real run (n=51)

| metric | value |
|---|---|
| ROI | **+14.3%** |
| total realized | +$6.00 |
| win rate | 29.4% |
| cost | $42 |

the first long-run sample. discovered three bugs in the process:
- **bug a:** the settler (the thing that finalizes PnL after markets close) was running too early and reporting "checked=0". false alarm — just a timing issue, not a real bug.
- **bug b:** the calibration analyzer was joining every fill to every prediction for that ticker (a cartesian product), inflating the bucket counts by ~18x. fixed by joining each fill to its nearest prediction by timestamp.
- **bug c:** "database is locked" errors when two processes touched the database at the same time. fixed by enabling sqlite's WAL (write-ahead logging) mode + a 5-second busy timeout.

v1 was positive but the win rate (29.4%) was suspicious. the wins were big enough to overcome the losses, which is the structure of a "lottery ticket" strategy — most bets lose small, a few win big.

### v2 — bigger sample (n=104)

with bugs fixed, i ran the daemon overnight on the conservative preset (min_edge 3%, min_price 5c, max_price 95c, no strike-type filter).

| metric | v1 | **v2** |
|---|---|---|
| total fills | 51 | 104 |
| ROI | +14.3% | **+24.4%** |
| total realized | +$6.00 | **+$20.82** |
| win rate | 29.4% | 60.6% |

ROI went UP with more data. that's encouraging. but the win rate jumping from 29% to 61% was suspicious — that's a different distribution. why?

i broke v2 down by market type. this is where the story gets interesting:

| dimension | v1 | v2 | what it means |
|---|---|---|---|
| **between (range markets)** | +126% / n=17 | **+100% / n=24** | the alpha is here |
| greater (daily threshold) | -37% / n=34 | +2% / n=80 | "fixed" only because of deep ITM layups that win 100% but pay tiny ROI. no real edge. |
| YES side | +65% | +49% | positive |
| NO side | -69% | -6% | recovered via layups, not alpha |
| 10-30c price bucket | +36% | **+50.7% / n=19** | cheap range tails — the engine of the +100% between number |

the read: the edge wasn't in daily-threshold markets. it was in cheap YES bets on "between" (range) markets — the lottery tickets.

i also looked at calibration (does the model's predicted probability actually match what happens in reality):

| probability bucket | what model predicted | what actually happened | n |
|---|---|---|---|
| 0-20% | 10% | 8.7% | 23 |
| **20-40%** | **30%** | **15.8%** | **19** |
| 40-60% | 50% | 63.6% | 11 |
| 60-80% | 70% | 100% | 13 |
| 80-95% | 88% | 100% | 21 |
| 95-100% | 98% | 100% | 17 |

the 20-40% bucket is overconfident: model says 30%, reality is 16%. that's the bucket where most range-tail bets live. the model is systematically overpricing range probabilities.

### v3 — controlled experiment (n=22)

session 4 decision: commit to the path. the bot is a range-tail specialist. tightened the gates to focus on the alpha:

- **min_edge** raised from 3% → 5%
- **max_credible_edge** introduced at 15% (gate the leaky 20-40% bucket)
- **min_price** raised from 5c → 10c (the 0-10c bucket was -1.4%)
- **allowed_strike_types** = ["between"] only (no daily-threshold markets)

a clean experiment: if v2's +100% between-ROI was real edge, v3 should reproduce it on the same market type with even tighter filters.

session 5 hit a snag: 30 of the first 31 fills under the new preset were actually "greater" strike type — looked like the strike-type filter was broken. investigation showed it wasn't. those 30 fills were placed by a previous daemon run before the range_v1 daemon started. fix: established a "v3 cutoff" at fill_id ≥ 135. any fill before that gets excluded from v3 analysis. patched the diagnostic tool to support that filter.

now to the result. v3 with n=22 settled fills, all confirmed "between" markets:

| metric | v2 between-only | **v3** | delta |
|---|---|---|---|
| n | 24 | 22 | comparable |
| ROI | +100% | **-24.4%** | **-124 percentage points** |
| total PnL | ~$+24 | **-$4.20** | -$28 |
| win rate | (high) | 40.9% | — |
| 10-30c bucket ROI | +50.7% | **-50.6%** | **sign-flipped** |
| 20-40% calibration | 15.8% / 30% | **12.5% / 30%** | leak unchanged |

the +100% was variance. plain and simple.

the math: at average entry price ~22c, breakeven win rate is 22%. v3's 10-30c bucket won 1 out of 9 (11.1%) — half the breakeven rate. that's not "fat-tail edge that needs more samples to see clearly". that's the model systematically overpricing range-tail YES probabilities. the calibration data confirms it: in the 20-40% bucket where these bets live, the model says 30% but reality is 12.5%.

v2 got lucky on n=24. v3 got the real distribution at n=22 — one nice $3.16 win on a yes@21c, but offset by ten yes@18-24c bets going to zero.

### what this means

three things:

1. **the v1 and v2 ROI numbers were not edge.** they were noise. the bot won money for a week, but not for the reasons the analysis claimed. honest read: i don't have an edge yet.

2. **the bottleneck is calibration, not raw model fit.** the log-normal vol model is reasonable for capturing general crypto price dynamics, but it's systematically miscalibrated in the 20-40% bucket. fixing that requires a post-hoc calibration step (isotonic regression — a non-parametric way to remap raw model probabilities to actual frequencies).

3. **the right experiment was the negative result.** v3 was designed to be the cleanest possible test of the v2 "between edge" hypothesis. it returned a clear answer. that's how this is supposed to work.

---

## what i learned

### the lesson worth its weight in tuition

**a positive ROI on a small sample is not evidence of edge.** it's hypothesis-generating, not hypothesis-confirming. you only know if the edge is real after you've run a controlled experiment with a pre-registered hypothesis and the result has held.

v2 was the small-sample positive. v3 was the controlled experiment. v3 said no.

### infrastructure lessons

- **sqlite WAL mode** is essential the moment you have a single writer and any concurrent reader (analyzer, jupyter notebook, vscode sql extension). default sqlite locking will give you mysterious "database is locked" errors.
- **always join fills to predictions by nearest timestamp**, not by full ticker match. the cartesian-product bug inflated my calibration buckets 18-fold before i caught it.
- **log every cycle and dump skip counters.** the only reason i could prove the strike-type filter was working in session 5 was that the daemon logged `skip(dedup=1, edge=0, price=0, strike_type=2)` every cycle. without that, i'd have wasted days chasing a phantom filter bug.
- **versioned baselines.** the fact that v1, v2, v3 are all reproducible from saved diagnostic outputs is what let me make the v3 vs. v2 comparison clean. if i'd been overwriting numbers in a spreadsheet, the lesson would be lost.

### what i'd do differently

- start with calibration first, not models first. i spent weeks tuning the vol model when the calibration leak was the dominant issue.
- pre-register the hypothesis BEFORE running the experiment. write down "v3 will be considered confirmation of edge if between-ROI > +50% on n ≥ 20." then read the result against that. v3 hit n=22, ROI is -24.4%, hypothesis rejected.
- get the dataset structured for ML training from day one. v2 happened, then i had to retrofit features for ML. wasted time.

---

## what's next

**phase 2: build a real edge model.**

three pieces:

1. **feature engineering.** the vol model uses one signal (recent volatility) and one assumption (log-normal). a real model needs more: order book imbalance, spot price velocity vs. realized vol, market-implied vol vs. realized vol, time-to-expiry effects, day-of-week, intraday seasonality, kalshi order flow imbalance. the data is all there — 270k snapshots.
2. **xgboost training with time-series cross-validation.** train a gradient-boosted decision tree on past snapshots → did the market settle YES? predict probability. critical detail: **time-series CV** means no future data leaks into training. each fold trains on data before a cutoff and tests on data after. this is the difference between a real backtest and a fake one.
3. **isotonic calibration.** after training the raw model, run a post-hoc calibration step that remaps raw probabilities to empirical frequencies. this is the explicit fix for the 20-40% leak. it's a non-parametric step — no new parameters to overfit — and it specifically solves the "model says 30%, reality is 13%" problem.

then re-run the v3 experiment with the new model. if range-tail bets get repriced more aggressively (cheaper YES, more expensive NO), the bot will simply not place those losing bets.

**phase 3: portfolio kelly.** when phase 2 is solid, add portfolio-level kelly that accounts for correlation between positions (bitcoin range markets within an hour of each other are highly correlated — they should not be sized independently).

**phase 4: go live.** $100 real money. write up the methodology. ship.

---

## the honest pitch

this is what a quant internship application should look like, except in story form:

- **i built a production-quality data pipeline** (RSA-authenticated kalshi API client, sqlite event store with WAL pragmas, 60-second polling daemon, settler, analyzer).
- **i implemented two pricing models from scratch** (log-normal vol pricing for crypto ranges, surface-adjusted Elo for tennis).
- **i implemented poker bankroll math** (fractional kelly sizing with hard caps, dedup, edge floors and credibility ceilings).
- **i ran a versioned series of controlled experiments** (v1 baseline → v2 bigger sample → v3 hypothesis test) with full per-dimension diagnostics on every run.
- **i caught and fixed three real bugs** that would have invalidated the analysis (settler timing, cartesian-product calibration join, sqlite locking).
- **i got a clean negative result** that disproved my own hypothesis on a 124-percentage-point ROI swing.
- **i'm pivoting to ML + isotonic calibration** based on what the data actually said, not what i hoped it would say.

zero of those things look impressive in isolation. together they're "this person ran a real experiment, instrumented it correctly, and read the results honestly."

---

## one-line elevator pitch

"i built a kalshi prediction market trading bot from scratch in python — log-normal volatility pricing on crypto range markets, surface-adjusted Elo for tennis, fractional kelly position sizing, running 24/7 against live kalshi data; v1 and v2 baselines were promising but a controlled v3 experiment proved the apparent edge was variance, and i'm pivoting to ML + isotonic calibration to fix the underlying calibration leak."
