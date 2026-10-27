# v2 Architecture — Locked Specification

**Project:** `banknifty-trade-assistant`
**Status:** Proposed for lock-in. Supersedes v1 blueprint. Companion doc: `00_PLAN_REVIEW.md`.

> **DISCLAIMER.** This is trade-support software, not investment advice. It places no orders.
> Every signal requires a human decision and a human execution on the broker terminal. Options
> buying carries risk of total loss of premium. Nothing here is a prediction of profit, and
> the parameters shipped are *starting points requiring calibration*, not recommendations.

---

## 1. Non-negotiable invariants

These are asserted in code and enforced by tests. Violating any of them is a build failure.

- **I1.** The system contains **no order-placement code path.** No `place_order`, no
  `modify_order`, no broker write API is imported, anywhere. Enforced by a test that greps
  the source tree.
- **I2.** Every signal is evaluated on **closed** candles only.
- **I3.** Every rupee figure shown to the human is **net of all costs**.
- **I4.** No signal is emitted unless data health is `HEALTHY` and warm-up is complete.
- **I5.** All state transitions are persisted before they are acted on.
- **I6.** Secrets never enter logs, never enter the DB, never leave the machine.
- **I7.** Exactly one engine instance may run per data directory (lock file).

---

## 2. Trading profile (declared explicitly — v1 never did)

| Parameter | Value | Rationale |
|---|---|---|
| Instrument | NIFTY BANK index options, **monthly expiry** | Weeklies discontinued for BankNifty |
| Direction | Long CE / Long PE only (buying) | Defined risk = premium paid |
| Holding | **Intraday**, mandatory square-off | Removes overnight gap risk, exercise STT, and loss of exit-price control |
| Bar interval | 5-minute (signals), 1-minute (guardian), tick (trailing) | |
| Session | 09:15-15:30 IST | From holiday/session calendar, not hardcoded |
| First entry | after `warmup_complete` and `>= 09:30` | Skips opening auction noise |
| Last entry | `no_new_entry_after = 14:45` | Leaves room for the trade to work |
| Forced exit | `mandatory_squareoff = 15:15` | Ahead of closing illiquidity |
| Max concurrent positions | **1** | The human can only manage one well |
| Underlying for greeks/VWAP | **Current-month FUTURES** | Index has no volume; options price off the future |

A `positional` profile exists in config and **ships disabled**.

---

## 3. The cost model (`src/costs.py`) — full statutory math

All rates live in `config/costs.yaml` with an `effective_from` date. **Before going live you
must reconcile these against one real Dhan contract note** — the README ships a worksheet.

Given premium `P`, quantity `Q` (= lots x lot_size), side `buy|sell`:

```
turnover              = P * Q
brokerage             = min(brokerage_per_order, brokerage_cap)        # Dhan F&O: flat 20
stt                   = 0.0010    * turnover   if side == sell else 0
exchange_txn          = 0.0003503 * turnover                            # NSE options
sebi_fee              = 0.0000010 * turnover                            # Rs 10 / crore
ipft                  = 0.0000050 * turnover                            # Rs 50 / crore
stamp_duty            = 0.00003   * turnover   if side == buy  else 0
gst                   = 0.18 * (brokerage + exchange_txn + sebi_fee + ipft)
leg_cost              = brokerage + stt + exchange_txn + sebi_fee + ipft + stamp_duty + gst
```

Effective execution prices (this is where v1 leaked most):

```
tick                  = 0.05
entry_eff             = ask + slippage_ticks_entry * tick
exit_eff(level)       = level - slippage_ticks_exit * tick     # you exit into the bid
```

Net P&L and the numbers the human actually sees:

```
net_pnl(exit_eff)     = (exit_eff - entry_eff) * Q - leg_cost(entry_eff, buy) - leg_cost(exit_eff, sell)
breakeven_premium     = solve net_pnl(x) = 0                   # DISPLAYED ON EVERY ALERT
net_risk              = -net_pnl(exit_eff(initial_SL))         # > 0
net_reward            =  net_pnl(exit_eff(target))             # > 0
rr_net                = net_reward / net_risk                  # the number that gates the signal
```

**Sizing (the piece v1 was missing entirely):**

```
risk_budget           = capital * risk_pct_per_trade
lots                  = floor(risk_budget / net_risk_for_one_lot)
lots                  = min(lots, max_lots,
                            floor(capital * max_deployment_pct / (entry_eff * lot_size)),
                            floor(freeze_qty / lot_size))
if lots == 0 -> SUPPRESS SIGNAL   # cannot be taken within the risk budget
```

Unit tests pin every line against hand-computed values, including a full worked round trip.

---

## 4. Process & module architecture

Two processes. The engine owns everything; the UI owns nothing.

```
                    ┌──────────────────────────────────────────────┐
                    │  ENGINE PROCESS  (single instance, lockfile) │
  Dhan REST ──────► │                                              │
  Dhan WebSocket ─► │  ratelimit ─► broker adapter ─► market data  │
                    │                     │                        │
                    │                     ▼                        │
                    │   candles ─► indicators ─► regime (HMM)      │
                    │                     │                        │
                    │                     ▼                        │
                    │   alpha engine ─► contract select ─► costs   │
                    │                     │                        │
                    │                     ▼                        │
                    │   risk governor ─► state machine ─► store    │
                    │                     │                        │
                    │                     ├──► guardian (post-trade)│
                    │                     └──► notifier (3 channels)│
                    │                                              │
                    │  FastAPI :8787  (127.0.0.1)  REST + SSE      │
                    └───────────────┬──────────────────────────────┘
                                    │ HTTP (shared-secret header)
                    ┌───────────────┴──────────────┐
                    │  DASHBOARD PROCESS (thin)    │
                    │  reads state, POSTs actions  │
                    └──────────────────────────────┘
```

### Directory layout

```
banknifty-trade-assistant/
├── README.md  QUICKSTART.md  LICENSE  DISCLAIMER.md
├── pyproject.toml  requirements.txt  requirements-dev.txt
├── .env.example  .gitignore  .gitattributes
├── docker-compose.yml                  # OPTIONAL Postgres/Timescale. Nothing depends on it.
├── config/
│   ├── config.yaml                     # strategy + risk + session params
│   ├── costs.yaml                      # statutory rates, effective_from dated
│   └── nse_holidays.yaml
├── docs/                               # 00_PLAN_REVIEW .. 09_RUNBOOK
├── scripts/  start_mac.sh  start_win.bat  start_win.ps1  stop_mac.sh  stop_win.bat
├── src/onelot/
│   ├── config.py            # pydantic-settings; validates on load, fails fast
│   ├── logging_setup.py     # rotation + secret redaction filter
│   ├── clock.py             # tz-aware IST, session calendar, drift assertion
│   ├── ratelimit.py         # central token buckets, shared by all callers
│   ├── broker/
│   │   ├── base.py          # BrokerAdapter protocol — READ-ONLY by construction
│   │   ├── dhan.py          # dhanhq 2.x; scrip master; expiry list; chain; historical
│   │   └── mock.py          # deterministic replay/synthetic feed (no creds needed)
│   ├── market/
│   │   ├── feed.py          # WS + REST failover, 3-state health model
│   │   ├── candles.py       # closed-bar builder, warm-up seeding
│   │   └── chain.py         # 5s chain cache + snapshot recorder
│   ├── analytics/
│   │   ├── indicators.py    # EMA RSI ATR ADX VWAP  (pure functions, fully tested)
│   │   ├── blackscholes.py  # Black-76 pricing, IV solve, greeks
│   │   ├── regime.py        # HMM: forward-filtered, relabelled, persisted
│   │   └── ivp.py           # ATM IV history + IV percentile w/ sufficiency modes
│   ├── strategy/
│   │   ├── gates.py         # each gate = pure fn returning GateResult(pass, reason, value)
│   │   ├── selector.py      # contract selection from cached chain
│   │   ├── sizer.py         # net R/R + position sizing
│   │   └── alpha.py         # orchestration on closed bars
│   ├── risk/governor.py     # six kill switches, persisted HALT state
│   ├── trade/
│   │   ├── state.py         # persisted state machine
│   │   ├── guardian.py      # VWAP/ADX hold, trailing SL, divergence, time stop, EOD
│   │   └── journal.py       # net-P&L trade journal + CSV export
│   ├── notify/              # osascript | windows-toasts | notify-send | telegram | dashboard
│   ├── store/               # SQLAlchemy DAL: SQLite default, Postgres/Timescale optional
│   ├── api/server.py        # FastAPI, loopback-bound, idempotent action endpoints
│   ├── ui/                  # dashboard client
│   └── backtest/            # replay harness + paper broker + metrics
└── tests/                   # unit / property / integration / e2e-replay
```

---

## 5. Signal pipeline (corrected)

Runs at `bar_close + 2s` on every closed 5-min bar. Each gate returns a structured result;
**all gates are evaluated even after the first failure** so the rejection histogram is
complete and "why no signal?" is always answerable.

| # | Gate | Rule | v1 defect fixed |
|---|---|---|---|
| 0 | Preflight | health HEALTHY, warm-up done, session window, not HALTED, no open position | new |
| 1 | Regime bias | daily HMM filtered-state == BULL (CE) / BEAR (PE), `P >= 0.60` | C1, C2 |
| 2 | Intraday chop | 5-min ADX > threshold, ATR-percentile in band | C3 |
| 3 | Trend | close vs EMA(21) on closed bar | B4, C4 |
| 4 | Momentum | RSI(14) in `[55,65]` CE / `[35,45]` PE | — |
| 5 | Volatility | IV percentile < 45, subject to sufficiency mode | B5 |
| 6 | Contract | `0.60 <= abs(delta) <= 0.70`, relative spread, min OI/volume, greek cross-check | A8, A9, B1 |
| 7 | Theta | `theta_per_hour * expected_hold <= max_burn_pct * premium` | B6 |
| 8 | Net R/R | `rr_net >= threshold` on **effective** prices, all costs | A1, A2, A6 |
| 9 | Sizing | `lots >= 1` within risk budget, deployment cap, freeze qty | A4 |
| 10 | Risk governor | daily loss, trade count, consecutive losses, cooldown, dedup | A5 |

Passing all gates emits **one** `Signal` with a `signal_id`, a TTL, and a full audit record of
every gate value. The contract's `securityId` is subscribed on the WebSocket immediately.

**Alert payload** (all three channels, identical content):

```
BUY BANKNIFTY 25-SEP 54000 CE  x2 lots (70 qty)
Entry  ~318.45 (ask 318.00)     Breakeven  322.10      <- net of every charge
Target  392.00  (net +Rs 4,812)  Stop  289.00 (net -Rs 2,014)   R/R 2.39
Delta 0.64  IV 14.2 (IVP 31)  Theta -8.4/hr  Spread 2.10
Regime BULL 0.78 | ADX 27.4 rising | RSI 61.2 | IVP 31
Valid for 75s  ->  [Trade Taken] [Skipped]
```

---

## 6. Post-trade Guardian (corrected)

Activates only on a confirmed `TAKEN` with a **real fill price** entered by the human (not
the signalled price — you rarely get the signalled price).

- **Slippage gate.** Compare live ask to signalled ask. Reject if beyond
  `max(pct_tolerance, k x spread)`. Rejection is a *warning with override*, not a silent kill —
  if you already filled, the system must track reality, not its own opinion of reality.
- **Hold confidence** (advisory, never auto-exits): futures vs futures-VWAP; ADX(14) level and
  slope. Displayed as a HIGH/MEDIUM/LOW badge.
- **Stops.** Initial hard SL fixed at entry. Trailing activates only after +1R and **never
  moves down**: `trail = max(prev_trail, current_premium - 1.5 * ATR5_index * abs(delta))`.
- **Exit triggers**, closed set, every one logged with attribution:
  `HARD_SL | TRAIL_SL | TARGET | TIME_STOP | DIVERGENCE | REGIME_FLIP | EOD_SQUAREOFF | MANUAL`
- **Divergence** uses **futures volume** (index has none — v1's fatal B3) plus option volume.
- **EOD** escalates: advisory at 15:10, blocking repeated alert from 15:15.
- **Manual override** for network loss: hard-feed entry/LTP/target; Guardian continues on
  operator-supplied prices and marks the trade `degraded_data` in the journal.

Exit alerts require acknowledgement with an **actual exit price**, which closes the trade and
writes the net-P&L journal entry that feeds the risk governor.

---

## 7. Reliability

- **Health model:** `HEALTHY` (socket alive + data fresh) / `DEGRADED` (REST failover, or
  token expiring, or IVP insufficient) / `DEAD` (no usable data). **Signals only in HEALTHY.**
  Every transition raises a distinct notification.
- **Failover:** socket death (ping/pong + connection state, ~5s) triggers REST fallback —
  *not* tick silence, which is normal. Jittered exponential reconnect.
- **Rate limiting:** one shared token-bucket set; all callers queue through it; `RL001`/429
  backs off and reports rather than retrying blindly.
- **Crash recovery:** on boot, reload open positions and resume the Guardian; refuse new
  signals until the operator confirms recovered state.
- **Single instance:** PID lock file on the data directory.

## 8. Falsifiability (R9 — the biggest v1 gap)

- **Recorder** — every chain snapshot, candle, gate evaluation and signal is persisted.
- **Replay harness** — feeds recorded (or synthetic) history through the *identical* alpha
  and guardian code paths. Same code live and in backtest; no parallel implementation.
- **Paper mode** — full pipeline, notifications, journal, and P&L with simulated fills at
  ask/bid plus configured slippage. **`PAPER` is the default mode; `LIVE` requires an
  explicit config change and an interactive confirmation.**
- **Metrics** — win rate, expectancy net of costs, max drawdown, per-gate rejection counts,
  signal frequency, and cost-as-%-of-gross-P&L.

## 9. Build phases

| Phase | Deliverable | Gate to proceed |
|---|---|---|
| 0 | Repo, config, logging, clock/calendar, DAL, CI | tests green on macOS + Windows |
| 1 | Cost model + sizer | worked round-trip matches a hand-computed contract note |
| 2 | Indicators + Black-76 + IVP | validated against reference values |
| 3 | Broker adapter + mock + rate limiter + scrip master | mock feed drives full pipeline |
| 4 | Candles, warm-up, chain cache, health model | closed-bar and repaint tests pass |
| 5 | Regime (HMM) | look-ahead test passes: label stable when future appended |
| 6 | Gates, selector, alpha, risk governor | end-to-end replay produces auditable signals |
| 7 | State machine, guardian, journal | crash-restart test recovers an open position |
| 8 | Notifications (3 channels) | native toast verified **on this Mac** |
| 9 | API + dashboard | click-to-ack round trip, idempotency verified |
| 10 | Backtest harness + metrics | replay over recorded data produces a report |
| 11 | Start scripts, docs, runbook, GitHub push | fresh-clone bootstrap works from zero |

## 10. Testing

- Unit: indicators vs reference series; every cost line; sizing edge cases; Black-76 vs known
  values; gate purity.
- Property: costs monotonic in premium; trailing stop never decreases; sizing never exceeds
  budget.
- Invariant: **no order-placement API referenced anywhere in `src/`**; no secret in logs.
- Leakage: HMM label unchanged when future data is appended; no signal from a forming bar.
- Integration: full replay through mock broker, deterministic and seeded.
- Resilience: kill the engine mid-position, restart, assert Guardian resumes.
- Cross-platform: GitHub Actions matrix on `ubuntu` / `macos` / `windows`, including a
  **path-with-spaces** case.
- **No test requires live credentials.** Live connectivity is a separate opt-in preflight script.
