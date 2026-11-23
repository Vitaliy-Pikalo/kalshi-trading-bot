# Poller category quota + schema change — design report

> Requested by `kalshi-handoff-v3.md` → "next step": *report the schema change and the quota
> design before writing code.* No code has been changed. Nothing is applied to disk except
> this document.

---

## THE VERDICT

**The per-category quota fix, on its own, will not fix tennis polling.**

v3 diagnosed one starvation mechanism (crypto floods the 1,500-slot cap). There is a
**second, independent and probably dominant** mechanism that v3 did not connect to the
poller: the 48-hour window is applied to `close_ts`, and for tennis `close_ts` is a
settlement deadline that lags match time by up to two weeks. So the markets tennis
*does* get polled on are matches that already finished.

Both mechanisms have to be fixed together, and the second one requires a schema change.
That makes the schema change load-bearing, not incidental.

---

## correction to handoff v3

v3's next-step prompt states sub-problem (b) as:

> "`snapshots` has no bid/ask depth columns."

**Half of this is wrong.** `src/db.py:55-66` — `snapshots` already has `yes_bid`, `yes_ask`,
`no_bid`, `no_ask`, `last_price`, `volume`, `open_interest`, and `poller.py:102-119`
already populates all of them from the `*_dollars` fields.

What is genuinely missing is **quantity at those levels** (depth), which is a different
column set and is the thing the capacity question needs. Restating it correctly:

| v3 said | actual |
|---|---|
| no bid/ask columns | bid/ask exist and are populated |
| — | **no depth/size columns** — this is the real gap |
| — | **no match-time column** — this is the blocking gap, unmentioned |

---

## root cause 1 — the cap (v3's finding, confirmed)

`poller.py:54-79`, one windowed query across the union of both series sets, single
`ORDER BY close_ts LIMIT max_count`:

```python
select(Market.ticker)
  .where(Market.series_ticker.in_(series))     # CRYPTO_SERIES | TENNIS_SERIES
  .where(Market.close_ts > now)
  .where(Market.close_ts < cutoff)             # now + 48h
  .order_by(Market.close_ts)
  .limit(max_count)
```

Callers:

| caller | max_count | category |
|---|---|---|
| `daemon.py:239` | **1500** | `None` → both series sets |
| `poller.py` CLI default | 1000 | `None` unless `--category` passed |

Crypto closes ~30,014 markets per 48h (v3, 10–12 aug) vs tennis 2. Sorted by close time,
every slot is consumed before a tennis row is reached.

## root cause 2 — the window predicate (new, and it is the bigger one)

v3 established, with a counterexample:

> `KXATPMATCH-26JUN17SHESON-SON` traded **17 jun**, `close_ts` **1 jul 09:30** — two weeks later.

The poller admits a market only if `close_ts ∈ (now, now+48h]`. Combine the two facts:

- a tennis match played **today** typically has `close_ts` ~2 weeks out → **excluded**
- a tennis market with `close_ts` in the next 48h is one whose match was played ~12 days ago
  → **admitted, and worthless**

This explains v3's own table better than the cap does. Only **2** tennis markets had
`close_ts` inside the 48h window. A per-category quota of 700 slots against 2 eligible rows
still polls 2 rows. **The cap was never the binding constraint for tennis — the predicate was.**

Corollary: the ~132 live pre-match tennis markets that `tennis_live_scan.py` measured on
17 aug were reached by hitting the Kalshi API directly, not from `markets` via the poller
path. The poller has, as far as this design can tell, never captured a live pre-match
tennis book.

---

## schema change

Three columns. All additive, all nullable — no rewrite of existing rows, no data loss.

### 1. `markets.occurrence_datetime` — DateTime, indexed  *(blocking)*

```sql
ALTER TABLE markets ADD COLUMN occurrence_datetime DATETIME;
CREATE INDEX ix_markets_occurrence_datetime ON markets (occurrence_datetime);
```

The poller needs match time as a queryable column. v3 has it for all 2,724 tennis markets
but only as a **cache in `data/tennis_match_times.csv`** — a CSV cannot be used in the
`WHERE` clause the poller needs. Backfill from that CSV, then keep it populated in
`discover.py` from the Kalshi market response.

Note the other candidate anchors are all disqualified by v3: `expiration_time`,
`settlement_ts` and `close_time` leak (market accuracy 0.98–1.00), and `open_time` filters
everything out.

### 2 + 3. `snapshots.yes_bid_qty` / `yes_ask_qty` — Integer  *(capacity question)*

```sql
ALTER TABLE snapshots ADD COLUMN yes_bid_qty INTEGER;
ALTER TABLE snapshots ADD COLUMN yes_ask_qty INTEGER;
```

Spread is already measurable from the existing columns — median 1.0c, and that test passed.
**Depth is not**, and depth is what decides whether the tennis signal can hold more than
pocket money. Without these two columns the capacity ceiling stays unmeasurable.

Caveat to verify before relying on it: `list_markets` may only return top-of-book size. If
true depth needs `/markets/{ticker}/orderbook`, that is one extra call per market and must
be rate-budgeted for the tennis subset only — do not attempt it across 30,014 crypto markets.

---

## quota design

Replace the single query with per-category queries, each with its own time predicate and
its own limit, then a spillover pass so no budget is wasted.

```python
CATEGORY_SERIES = {"crypto": CRYPTO_SERIES, "tennis": TENNIS_SERIES}

# anchor column per category — tennis MUST NOT use close_ts
CATEGORY_ANCHOR = {"crypto": Market.close_ts, "tennis": Market.occurrence_datetime}

# tennis window is set by the strategy: NO dies past 3h, YES holds to 12h (v3 addendum)
CATEGORY_WINDOW_HOURS = {"crypto": 48, "tennis": 14}

DEFAULT_QUOTA = {"crypto": 800, "tennis": 700}
```

Rules:

1. **Per-category floor.** Each category runs its own query against its own anchor column.
   Crypto volume can no longer displace tennis, because they never compete in one `LIMIT`.
2. **Spillover.** Sum the shortfalls (a category with fewer eligible markets than its quota)
   and redistribute to categories that hit their cap. Tennis will typically use only
   100–400 of its 700, so crypto keeps most of its current coverage.
3. **Ordering stays `ASC` on the anchor** — soonest event first is correct for both, once
   the anchor is right.
4. **Tennis window 14h, not 48h.** `MAX_LEAD_MIN = {"no": 180, "yes": 720}` already encodes
   that the edge dies past 12h. Polling 48h of tennis spends budget on markets no strategy
   will trade. 14h gives the 12h YES window a 2h margin.
5. **Fail loud, not silent.** If `occurrence_datetime IS NULL` for a tennis market, count it
   in a `skipped_no_anchor` tally and print it. v2's lesson was five silent `continue`s
   hiding the reason for zero fills — do not add a sixth.

### total budget

800 + 700 = 1,500, unchanged from `daemon.py:239`. **This is deliberate.** Poll latency is
already ~60s of a 300s trade cycle; raising the cap risks the cycle overrun that the v2
watchdog bug was mistaken for. Reallocate, don't inflate.

Worth noting separately: crypto's 800 slots are being spent on `crypto_vol_v1`, whose gates
v2 closed and whose live `range_v4` preset places zero fills. A later step should consider
dropping crypto to a token allocation, but that is a strategy decision, not a poller fix,
and it does not belong in this change.

---

## pre-registered predictions

Per workflow rule 4 — stated before running, so a mis-wired fix is detectable.

| # | prediction | falsifies what |
|---|---|---|
| 1 | tennis tickers polled per cycle goes **2 → 100–400** | order of magnitude from the 132 live markets `tennis_live_scan` saw on 17 aug. If it stays ≤5, `occurrence_datetime` didn't backfill. If >1000, the window or series set is wrong |
| 2 | crypto tickers polled drops **1500 → ~800**, snapshot rows drop ~45% | confirms reallocation. If crypto stays at 1500, the quota isn't being applied |
| 3 | poll wall-time stays **≤ ~60s** | tennis rows replace crypto rows 1:1, so batch count is flat. A jump means depth calls were added inadvertently |
| 4 | `skipped_no_anchor` is **0** after backfill, **>2000** before it | proves the anchor backfill ran and that the tally is wired |
| 5 | median tennis spread from newly captured snapshots ≈ **1.0c**, p90 ≈ 4.0c | reproduces the 17 aug live measurement from a different code path. A large disagreement means the poller is capturing post-match or illiquid books |

Prediction 5 is the real test — it is the only one that cross-validates new plumbing against
a number measured independently.

---

## rollout

| step | action | verify |
|---|---|---|
| 0 | **stop the watchdog loop, then the daemon** | v3: `_daemon_stop.bat` alone is not enough; the watchdog relaunches within 5 min |
| 1 | 3 `ALTER TABLE` + 1 index, against a **copy** of `poker_quant.db` | `PRAGMA table_info` shows the columns |
| 2 | backfill `occurrence_datetime` from `data/tennis_match_times.csv` | 2,724 rows non-null |
| 3 | patch `get_active_tickers` + add the tally | `--once --category tennis` prints 100–400 |
| 4 | run `--once` both categories | predictions 1–4 |
| 5 | restart daemon + watchdog, let it run ~2h | prediction 5 |

Rollback: the three columns are additive and nullable, so reverting is a `git checkout` of
`poller.py`. The columns can be left in place harmlessly.

---

## risks

| risk | mitigation |
|---|---|
| `ALTER TABLE` against a live daemon holding the db | step 0 stops it first; test on a copy |
| WAL / mount weirdness during migration | run the migration **locally on Windows**, not through a mounted path — v3 already documented `mode=ro` throwing `disk I/O error` and full scans throwing `malformed` |
| `list_markets` returns no size fields | prediction 3 + the caveat above; fall back to the orderbook endpoint for tennis only |
| tennis quota is guessed, not derived | 700 is an upper bound on an unknown; spillover means over-allocating costs nothing |
| crypto coverage loss changes crypto results | crypto gates are already closed, so there is no live result to protect |
