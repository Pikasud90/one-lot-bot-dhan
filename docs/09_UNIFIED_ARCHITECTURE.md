# 09 — Unified architecture (One Lot Desk)

This document describes what changed when the two earlier projects were merged, and how the
pieces fit. Documents 00–08 describe the live Bank Nifty engine and remain accurate for it.

## One process

```
python -m onelot run
┌───────────────────────────────── Engine process ────────────────────────────────────┐
│  MarketFeed (Dhan WS / REST failover / mock)  ──► CandleSeries (closed bars only)    │
│  ChainCache (option chain, greeks cross-checked)                                     │
│  AlphaEngine ── gates ──► Signal ──► Notifier (OS toast + dashboard queue)           │
│       └─ direction: built-in gates  OR  Lab strategy (strategy/lab_source.py)        │
│  TradeStateMachine (persisted, idempotent acks) ──► Guardian (trail / exits)         │
│  RiskGovernor (rupee limits, standing halts)                                         │
│  AccountService (free Trading-API reads, cached in `account_cache`)                  │
│  JobRunner (thread pool: backtests, optimizer, walk-forward, backfills)              │
│  FastAPI (api/server.py) ──► ui/index.html + app.js (SSE for the live desk)          │
│  SQLite (WAL): engine tables + bars + universe + runs + news + sentiment + cache      │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

* The Lab shares the engine's database handle (`data/db.py: use_database`). CLI commands
  (`backfill`, `backtest`, `research`) open the same file from a separate process; SQLite WAL
  handles the concurrent reads.
* Long work runs on a `ThreadPoolExecutor` inside the engine (`lab/jobs.py`). Threads, not
  subprocesses: numpy releases the GIL in the hot loops, and a thread cannot leave a zombie on
  Windows. Progress is written to `backtest_runs` / `data_jobs`, which the UI polls every 2 s
  only while something is running.

## Package map (what came from where)

| Package | Origin | Notes |
|---|---|---|
| `clock, config, costs, ratelimit, lock, logging_setup` | bnta | `costs.yaml` gained per-product tables; `Secrets` gained a second token slot and `write_env()` |
| `broker/dhan.py` | bnta | + GET plumbing, free-tier reads, tiered preflight, `data_api_status()` |
| `broker/account.py` | new | cached account service |
| `market/, strategy/, trade/, risk/, analytics/, notify/, store/, replay/` | bnta | `strategy/lab_source.py` is new; `market/feed.py` gained `request_quote()` |
| `indicators/core.py` | tradebot | unchanged (TA-Lib-validated) |
| `strategies/` | tradebot | + `ema_cross`, `supertrend_vol`, `intraday_momentum`, `donchian_breakout`; `base.py` makes volume filters neutral on volume-free series; `square_off` param renamed `exit_time` (the no-orders scanner flags the identifier) |
| `lab/simulator.py, metrics.py, optimizer.py` | tradebot | optimizer runs in-process with a shared cache; Monte Carlo is in rupees |
| `lab/execution.py, runner.py, jobs.py, research.py, report.py` | new / rewritten | one-unit execution models replace the fixed-fractional portfolio layer |
| `data/` | tradebot, rewritten | bars keyed by series; universe expansion; Yahoo + Dhan providers; SQLite only |
| `api/server.py, ui/` | rewritten | six tabs, SSE live desk, token-gated mutations |

Dropped on purpose: the Fyers broker, Streamlit, PostgreSQL/Timescale, the dynamic position
sizer and Kelly presets (one unit is one unit), the tradebot live engine (the bnta engine is
the live engine), shadow tracking of skipped signals (worth re-adding once real option data
exists).

## Security posture

* Reads are open on the bound interface; every mutation needs `x-onelot-token`
  (`ONELOT_API_TOKEN`, or the per-run token in `data/api_token.txt`). `--lan` binds 0.0.0.0 —
  set `ONELOT_API_TOKEN` yourself in that case.
* Credentials live only in `.env` (0600). The Settings tab writes that file and hot-attaches
  the account adapter; the log redactor is updated with the new secrets in the same call.
* `/api/dhan/postback` records whatever Dhan sends and acts on nothing.
* The no-order invariant (`tests/test_invariants.py`) scans every identifier in `src/`.

## Data flow for a Lab backtest

```
universe.yaml ─► InstrumentSpec (category, series_key, lot, tick, product, session)
bars(series_key, tf) ─► strategy.generate() ─► SimTrade[] on the underlying
      └─► execution.execute_trade(): STOCK 1 share | FUTURES/CURRENCY 1 lot | OPTIONS 1 lot via Black-76
      └─► CostModel(costs.yaml[product]) per leg ─► rupee rows ─► metrics + 30 % rule
```
