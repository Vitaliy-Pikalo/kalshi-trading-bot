# handoff doc — kalshi trading bot

**date:** 2026-05-15
**phase:** 0 complete, ready for phase 1
**repo:** https://github.com/Vitaliy-Pikalo/kalshi-trading-bot (private)
**local:** `C:\Users\pikal\Downloads\claude project`

---

## project context

building a kalshi prediction market trading bot that prices events using ML + sizes positions with poker bankroll math (kelly criterion, fractional sizing, variance-aware). goal: passion project + quant internship signal + small real $ side project.

three-way thesis:
1. **passion** — applies poker math (EV, kelly, variance) to a live game
2. **quant signal** — exactly what SIG / Susquehanna / Jane Street do in event markets
3. **real $** — kalshi is CFTC-regulated US legal, can fund with $100, scale only after backtest proves edge

bankroll target: $100 paper through phase 3, $100 live in phase 4

---

## status map

| phase | name | weeks | status |
|---|---|---|---|
| 0 | setup + skeleton | 0-1 | ✅ done |
| 1 | data ingestion + paper trade | 1-2 | ⏭ next |
| 2 | ml edge model + backtest | 3-6 | pending |
| 3 | poker-math layer (kelly + risk) | 7-9 | pending |
| 4 | go live + writeup | 10-12 | pending |

---

## what's been done

### phase 0 deliverables (all shipped)
- platform decision: kalshi (CFTC-regulated, US legal, demo env available)
- account + KYC + RSA api keys generated, private key safely off-repo at `C:\Users\pikal\Downloads\bot.txt` (move to `Documents\kalshi\` before phase 4)
- python 3.14.5 + venv (`.venv/`) + 100+ deps installed (httpx, pandas 3.0, numpy 2.4, sklearn 1.8, xgboost 3.2, kalshi-python 2.1.4, cryptography 48, streamlit, pytest, etc.)
- git repo init'd on `main` branch
- private github repo created: `Vitaliy-Pikalo/kalshi-trading-bot`
- first commit pushed: `732a280 — phase 0: project setup + kalshi api smoke test`

### code shipped
| file | purpose | lines |
|---|---|---|
| `src/config.py` | pydantic settings + risk defaults (kelly=0.25, max 1% per trade, 5% daily loss circuit breaker) | 90 |
| `src/db.py` | sqlite schema: 5 tables (markets, snapshots, predictions, fills, bankroll) + sessionmaker | 110 |
| `src/kalshi/client.py` | RSA-PSS signed REST client (handles auth headers properly) | 100 |
| `src/kalshi/smoke_test.py` | phase 0 verification — passed, pulled 5 live markets | 70 |
| `tests/test_config.py` | 3 unit tests, all passing | 30 |
| `requirements.txt` | full dep list pinned to working versions | — |
| `.env.example` | clean template (no secrets) | — |
| `.env` | gitignored, contains real kalshi key_id + key path | — |
| `.gitignore` | catches `.env`, `*.pem`, `_*.bat`, `_*.txt`, venv, db, etc. | — |
| `README.md` | project overview + quickstart | — |

### infra verified
- venv activates: `.venv\Scripts\python.exe`
- db inits: `python -m src.db init`
- config loads from .env: `python -m src.config`
- pytest passes: `python -m pytest tests/ -v`
- smoke test signs RSA + pulls live data: `python -m src.kalshi.smoke_test`
- git push to origin works

---

## current task / step / phase

**phase 1 — data ingestion + paper trade.** none of phase 1 has been started.

---

## what still needs to be done

### phase 1 deliverables
1. **market discovery** — script that pulls full kalshi market catalog (open + recently closed), stores in `markets` table. understand what categories exist (sports, weather, econ, politics, KPI events) and pick ONE narrow category for first model
2. **snapshot poller** — `src/ingest/run.py` that polls bid/ask/volume for tracked markets every 60s, writes to `snapshots` table. should run as a background process (later: VPS, for now: laptop)
3. **picked category** — recommendation: NBA player props (deep, frequent, has clean truth source via box scores). alt: NYC weather (NOAA truth source, very clean, lower volume)
4. **fair-price baseline estimator** — for chosen category, write a stupid baseline (e.g. rolling avg, simple historical hit rate). this is NOT ML yet — just a sanity check that anything beats market
5. **paper trade simulator** — for every snapshot, log "if baseline > market_implied + threshold, would have bought N contracts" to `fills` table with `is_paper=1`
6. **phase 1 checkpoint** — after 100+ paper trades, compute: win rate, average edge, P&L. if no edge in baseline, data/feature is wrong. don't move to phase 2 until baseline shows something

### blockers / decisions needed before phase 1 starts
- pick market category (NBA props vs weather vs election vs other)
- where to run the poller (laptop with task scheduler vs $5 DO vps vs github actions cron)
- if NBA: data source (basketball-reference, nba.com api, or paid like sportsdataio?)

---

## next step

**before any code:** decide market category. recommended sequence in next session:

1. read `poker-quant-project-brief.md` (in outputs folder) phase 1 section
2. answer: NBA player props or NYC weather?
3. if NBA: research free data sources (basketball-reference scraping vs nba_api python lib)
4. if weather: NOAA API access (free, no key needed)
5. write `src/ingest/discover.py` — pulls kalshi market list, filters to chosen category, writes to `markets` table
6. write `src/ingest/poller.py` — polls snapshots every 60s for active markets in chosen category
7. let it run for 24-48h to accumulate baseline data

---

## workflow rules

- one phase at a time. don't start phase 2 until phase 1 has 100+ paper trades + verified baseline edge
- backtest before paper trade, paper trade before real $
- log everything to sqlite from day 1 (markets, predictions, fills, P&L)
- git commit after every working feature
- write a markdown journal entry per phase — what worked, what didn't
- if backtest shows no edge, do NOT trade real money. journal the negative result and pivot the model
- max risk: 1% of bankroll per single position, ever (fractional Kelly with k=0.25)
- never paste private keys, passwords, or KYC info into chat. only put paths in `.env`
- use scratch `_*.bat` files for shell ops (gitignored). real tests go in `tests/`

---

## quickstart for next session

```powershell
# new chat: open the project
cd C:\Users\pikal\Downloads\claude project

# verify everything still works
.venv\Scripts\python.exe -m src.config
.venv\Scripts\python.exe -m pytest tests/ -v

# verify smoke test still works
.venv\Scripts\python.exe -m src.kalshi.smoke_test
```

if anything breaks, the issue is likely:
- `.env` got reset → repopulate with key_id + key path
- venv broke → re-run `_setup3.bat` (will rebuild)
- network issue → check kalshi status

---

## key files for next claude session to read first

1. `HANDOFF.md` (this file)
2. `README.md`
3. `src/config.py` — understand the risk defaults
4. `src/db.py` — understand the data model
5. `src/kalshi/client.py` — understand the auth + how to add new endpoints
6. `poker-quant-project-brief.md` (in `C:\Users\pikal\AppData\Roaming\Claude\local-agent-mode-sessions\...\outputs\` — copy into project folder if you want)

---

## one-line elevator pitch (current)

"i built a kalshi-authenticated trading infra in python with proper RSA signing, a sqlite event store, and pydantic-validated risk controls based on poker bankroll theory (quarter-kelly sizing, 1% per-trade cap). next: ingestion layer + first ML model on NBA player props."
