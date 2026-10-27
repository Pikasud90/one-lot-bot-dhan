# Plan Review — Brutal Critique of the v1 Blueprint

**Reviewer:** Claude (Opus 5) · **Date:** 2026-08-20
**Subject:** "Semi-Automated Trade Assistant for Bank Nifty Options" (v1 blueprint, Gemini 3.1)
**Verdict:** **DO NOT BUILD AS SPECIFIED.** The intent is sound and the module decomposition is
reasonable, but the v1 blueprint contains **9 defects that lose money silently**, **6 that
guarantee the system fires zero signals**, **5 statistical look-ahead leaks**, and **an
infrastructure stack that cannot start on the target machine**. Every one is fixed below.

---

## 0. What you actually want (intent, restated)

Stripping the prose, the requirements are:

| # | Requirement | v1 status |
|---|---|---|
| R1 | Machine does 100% of the math; human is a Yes/No trigger | Intent correct |
| R2 | **Zero** automated order placement — signal-only, always | Correct, preserved |
| R3 | Runs standalone on **macOS and Windows**, no babysitting | **BROKEN** (Docker) |
| R4 | FOSS only; sole proprietary input = Dhan client-id + access-token | Correct |
| R5 | Signals must be **net-of-everything** profitable, not gross | **BROKEN** (cost model wrong/incomplete) |
| R6 | No silent failure — if data is stale/dead, you must know | Partial |
| R7 | Survives crashes, network drops, restarts with a live position | **MISSING ENTIRELY** |
| R8 | Full documentation, local + GitHub | Not addressed |

The unstated but load-bearing requirement, which v1 misses completely:

> **R9 — Falsifiability.** A strategy that only ever runs live, forward, once, cannot be
> validated, tuned, or trusted. Without a replay/backtest harness and a paper-trading mode,
> you are not building a trading assistant, you are building a random number generator with
> a nice UI. **This is the single biggest gap in v1.**

---

## A. FATAL — defects that lose real money

### A1. The cost model is missing ~40% of actual costs
v1 says: *"Net Cost = Premium + (Rs 40 Brokerage) + (0.1% STT on Exit) + (Exchange Txn Fees)"*

Actually charged on an NSE index-option round trip, **all of which v1 omits**:

| Charge | Rate | Side | In v1? |
|---|---|---|---|
| Brokerage | Rs 20 / executed order (Dhan F&O) | both | Yes (Rs 40) |
| STT | 0.10% of premium turnover | **sell only** | Yes |
| Exchange txn charge (NSE options) | 0.03503% of premium turnover | both | Vague |
| SEBI turnover fee | Rs 10 per crore (0.0001%) | both | **NO** |
| NSE IPFT | Rs 50 per crore premium (0.0005%) | both | **NO** |
| Stamp duty | 0.003% of premium turnover | **buy only** | **NO** |
| **GST @ 18%** on (brokerage + txn + SEBI + IPFT) | 18% | both | **NO** |
| Bid-ask spread crossed twice | ~2x half-spread | both | **NO** |
| Slippage beyond the quote | config, in ticks | both | **NO** (see A2) |

GST alone on a 2-lot trade is a non-trivial fixed drag, and **spread cost dwarfs every line
item above it**. On a Rs 300 ITM BankNifty option with a Rs 4 spread and 35 qty/lot,
crossing the spread twice costs `4 x 35 = Rs 140 per lot`. v1's entire modelled cost was
Rs 40 + STT. It is under-counting friction by roughly **3-4x**.

> **FIX:** Full statutory cost model in `src/costs.py`, every rate in `config/costs.yaml`
> with an `effective_from` date and a mandatory pre-live reconciliation against a real Dhan
> contract note. R/R computed on **net rupees**, not gross premium. Alert displays the
> **breakeven premium**, not just target/SL.

### A2. Slippage is checked but never *priced*
v1 has a 3% slippage *gate* post-click, but the R/R that qualified the signal assumed you
buy at LTP and sell at LTP. You don't. You **buy at the ask** and **sell at the bid**.

> **FIX:** `entry_effective = ask + slippage_ticks x 0.05`, `exit_effective = bid - slippage_ticks x 0.05`.
> R/R is computed on effective prices. Gate compares live **ask** to signalled ask, and the
> tolerance is `max(pct_tol, k x spread)` — a 3% tolerance on a Rs 300 option is Rs 9, which is
> narrower than the natural spread on some ITM strikes, so a pure-% gate rejects valid fills.

### A3. Delta 0.60-0.70 with no mandatory square-off
The plan targets ITM options and never once says "close the position before expiry."
Three separate costs follow from that omission:

1. **Exercise STT.** An ITM index option left to expire is charged STT at **0.125% of
   intrinsic settlement value**, versus **0.10% of premium** if you sell it — about **25%
   more** on a comparable base. (Correction: an earlier draft of this review called it an
   order of magnitude. It is not. It is real but modest, and it is the smallest of the three
   costs here.)
2. **Forfeited time value.** Exercise pays intrinsic only. Any remaining extrinsic value is
   simply given up.
3. **Loss of price control** — the far bigger one. Settlement uses the exchange's weighted
   average over the closing window, so you do not choose your exit at all.

On top of which: an overnight long option position is exposed to gap risk that no stop-loss
can protect against, since the stop cannot trigger while the market is closed.

> **FIX:** Hard rule — mandatory intraday square-off. On expiry day, no new entries after
> `no_new_entry_after`, and a **blocking** EXIT alert escalating every 60s from
> `mandatory_squareoff_time`. The engine refuses to enter a contract expiring today unless
> explicitly enabled.

### A4. There is no position sizing, despite a module named "Execution Sizer"
v1's "Friction Sizer" only **filters** on R/R. It never answers *how many lots*. So the
human guesses — which is exactly the human math the system exists to eliminate. Worse,
without sizing there is no risk budget, so a single bad trade is unbounded.

> **FIX:** `lots = floor(capital x risk_pct / net_risk_per_lot)`, clamped by
> `max_lots`, `max_premium_deployment_pct`, and NSE freeze quantity. **If `lots == 0`, the
> signal is suppressed** — you cannot take the trade within your risk budget, so it isn't a
> trade.

### A5. No kill switches of any kind
No daily loss limit. No max trades/day. No consecutive-loss breaker. No max concurrent
positions. On a bad day the system will happily fire signal after signal into a trend that
is eating you.

> **FIX:** `RiskGovernor` with hard-stop states: `daily_loss_limit`, `max_trades_per_day`,
> `max_consecutive_losses`, `max_open_positions = 1`, plus a manual **PANIC HALT** button on
> the dashboard. Entering `HALTED` requires explicit operator reset; it survives restart.

### A6. R/R >= 2.5 is mathematically inconsistent with a trailing stop
You cannot filter on a fixed reward:risk ratio and then manage the exit with a trailing stop
that has no target. The 2.5 number is decorative — nothing in the Guardian ever tries to
reach it.

> **FIX:** Define R explicitly. `R = entry_eff - initial_SL_eff`. `T1 = entry + 2.5R` is a real,
> quoted level. The trailing stop **activates only after `T1_partial` (1R) is touched** and
> never trails below the initial SL. Exit reasons become a closed set:
> `{HARD_SL, TRAIL_SL, TARGET, TIME_STOP, EOD, DIVERGENCE, REGIME_FLIP, MANUAL}` — every one
> logged and P&L-attributed.

### A7. No time-stop — theta bleeds you while "nothing happens"
An option position that goes sideways is not flat, it is losing. v1 has no exit for "thesis
didn't play out."

> **FIX:** `time_stop_minutes` — if neither 1R nor SL is hit within N minutes, exit at market.

### A8. `abs(delta)` — PE deltas are negative
`0.60 <= Delta <= 0.70` matches **zero** put contracts. Every PE delta is in `[-1, 0]`. As
written, the system is CE-only and will silently never signal a put.

> **FIX:** `0.60 <= abs(delta) <= 0.70`, with a unit test asserting a PE contract passes.

### A9. Greeks are trusted blindly from the broker
If Dhan returns a stale, null, or wrong greek, v1 sizes and filters off garbage with no
detection.

> **FIX:** Local **Black-76** implementation (options on index track the **future**, not
> spot — pricing off spot with an assumed rate is a real mispricing). Broker greeks are used
> when present *and* cross-checked against local values; divergence beyond tolerance flags
> the contract as untrustworthy and it is skipped. If broker greeks are absent, local values
> are used with IV solved from the mid price.

---

## B. STRUCTURAL — the system as specified fires zero signals

These are not opinions; they are arithmetic.

### B1. Bid-ask spread <= Rs 1.50 on ITM monthly BankNifty
Monthly-expiry, 0.65-delta BankNifty options routinely quote Rs 3-15 wide outside the most
liquid strikes. A flat Rs 1.50 filter rejects essentially the whole eligible universe.

> **FIX:** Relative liquidity gate: `spread <= max(abs_floor, spread_pct x mid)` plus minimum
> OI and minimum volume. Tuned against **recorded chain data**, not a guess.

### B2. R/R 2.5 with a 1.5 x ATR x delta stop needs a ~225-point index move
Worked example: index ATR(5m) = 60 pts, delta = 0.65 -> stop distance =
`1.5 x 60 x 0.65 = 58.5` premium points. Target at 2.5R = **146 premium points** =
`146 / 0.65 = ~225 index points`, in one leg, before the trailing stop or time stop fires.
That is a large intraday move. Combined with B1, expect **months between signals**.

> **FIX:** Both numbers become config, calibrated by the **backtest harness** before going
> live, plus a scaled partial-exit ladder so the full 2.5R isn't required for the trade to be
> a winner. The dashboard shows a **"why no signal"** gate-rejection histogram so a
> zero-signal week is diagnosable instead of mysterious.

### B3. VWAP on the Bank Nifty **index** is undefined — the index has no volume
This is the most serious analytical error in v1. VWAP is *volume*-weighted. The NIFTY BANK
index is a computed number; it does not trade and has no volume. Dhan will return `volume=0`
for index candles. v1's "institutional anchor" is `0/0`.

The identical flaw kills the divergence rule: *"tick-volume drops by 20%"* — **on the
index, there is no tick volume.**

> **FIX:** Run VWAP and all volume analytics on the **current-month BankNifty futures**,
> which do trade and do have volume, and which is what institutions actually anchor to. The
> price test becomes futures-vs-futures-VWAP. Volume divergence uses futures volume, with
> the traded option's own volume as a secondary confirm.

### B4. Cold start — every indicator is garbage until ~10:30 AM
EMA(21), RSI(14), ATR, ADX(14) on 5-minute bars need 20-30 closed bars. Sessions start at
09:15. v1 has no warm-up, so the system either emits nonsense for the first 100 minutes or
sits idle through the most tradeable part of the day.

> **FIX:** On startup, pull N prior sessions of 5-min candles from Dhan's historical API and
> seed the indicator buffers. Signals are **blocked** until `warmup_complete` is true, and
> the dashboard shows warm-up status explicitly.

### B5. IV Percentile needs a year of IV history you do not have on day 1
IVP is by definition a rank over ~252 sessions. On day 1 you have zero. v1 would compute
IVP over a 1-element series and get 0 or 100 — silently passing or blocking every trade.

> **FIX:** ATM IV is recorded daily from our own chain snapshots and accumulates. Until
> `min_history_days` is met, the IV gate runs in an explicit mode:
> `strict` (block all signals — safest), `degraded` (rank over available history, flagged
> loudly on the alert), or `off`. Never silently wrong. India VIX is offered as an optional
> bootstrap proxy **clearly labelled as a proxy** (it prices Nifty, not Bank Nifty).

### B6. Theta gate is specified in units the strategy doesn't use
*"Projected overnight Theta decay < 10% of premium"* — but v1 never says whether this is an
intraday or positional system. If intraday, overnight theta is irrelevant. If positional,
almost nothing else in the plan (5-min bars, tick trailing) makes sense.

> **FIX:** Declare it: **intraday-only, mandatory EOD square-off** is the default profile.
> The theta gate becomes `theta_per_hour x expected_hold_hours <= max_theta_burn_pct x premium`.
> A `positional` profile exists in config but ships disabled.

---

## C. STATISTICAL LEAKS — the model lies to you about its own edge

### C1. HMM Viterbi decoding over the full series is look-ahead bias
`model.predict(X)` runs Viterbi, which finds the most likely state sequence **given the
entire observation sequence including the future**. Using that to label "today's regime"
means today's label depends on tomorrow's data. Backtests will look brilliant and live
performance will not match. This is the classic HMM trading bug.

> **FIX:** Forward-filtered state probability only — `P(state_t | observations up to t)`.
> Fit on data through T-1, infer for T. Enforced by a unit test that asserts the inferred
> label for a given day is unchanged when future data is appended.

### C2. hmmlearn state indices are arbitrary and unstable across refits
State `0` is not "bull". It is whatever EM happened to converge to, and it can permute
between daily refits. v1 assumes stable semantic labels. In production, your "bull" filter
silently becomes your "bear" filter after an overnight refit.

> **FIX:** Deterministic relabelling every fit — sort states by mean log-return
> (highest=BULL, lowest=BEAR, remaining/highest-variance=CHOP). Persist the fitted model +
> label map with a hash; refit at most once daily, pre-market. Log the transition matrix.

### C3. A **daily** regime cannot gate a **5-minute** entry
Daily HMM states persist for days-to-weeks. Using one to authorise a 5-minute breakout means
the filter is effectively constant all day — it is not filtering, it is just occasionally
switching the system off entirely.

> **FIX:** Two-tier. Daily HMM = *directional bias* (which side is permitted). A separate
> **intraday chop filter** (5-min ADX / ATR-percentile / EMA-slope) supplies the actual
> per-bar gate. Confidence floor: if `max P(state) < 0.60`, treat as CHOP.

### C4. Repainting — signals computed on the forming candle
v1 says "evaluates every 5 minutes" without saying *when*. If it reads the in-progress bar,
every signal repaints and the backtest is fiction.

> **FIX:** Evaluate strictly on **closed** bars, at `bar_close + settle_delay`. The candle
> builder marks bars `closed=True` only after the boundary passes. Enforced by test.

### C5. No holiday / session calendar, no timezone discipline
`datetime.now()` on a machine set to anything but IST silently shifts every candle boundary.
NSE holidays, half-days, and the muhurat session will produce phantom bars and warm-up
corruption.

> **FIX:** All timestamps tz-aware `Asia/Kolkata`, from exchange time where available. A
> maintained `config/nse_holidays.yaml`, plus a startup assertion that the machine clock is
> within tolerance of a reference. Session windows are config, not constants.

---

## D. ARCHITECTURE — it will not run, and when it runs it will break

### D1. Docker/TimescaleDB is not present on the target machine
Verified on this Mac: `docker: command not found`. Requiring Docker Desktop breaks the
"standalone, runs on Mac and Windows" requirement outright — on Windows it additionally
needs WSL2, and Docker Desktop is not free for larger organisations, breaking FOSS-only.
TimescaleDB is enormous overkill for one user recording one instrument's chain.

> **FIX:** **SQLite (WAL mode) is the default and requires zero installation on either OS.**
> Postgres/TimescaleDB remains fully supported as an opt-in via `DB_URL` — the DAL is
> backend-agnostic and creates hypertables only when Timescale is detected. `docker-compose.yml`
> ships for users who want it; nothing depends on it.

### D2. Streamlit cannot host the engine — this is a guaranteed production failure
Streamlit re-executes the entire script top-to-bottom on **every interaction and every
auto-refresh**. Background threads started at module scope get **duplicated on every
rerun**. Within an hour you would have dozens of polling threads racing the same rate limit
(-> `RL001` lockout) and mutating conflicting state. Button clicks during an auto-refresh
cycle are dropped — meaning **your "Trade Taken" click can silently vanish**.

> **FIX:** Hard process split.
> - **`engine`** — a single long-lived process (FastAPI + background workers). Owns all
>   state, all threads, all API quota. Exposes REST + WebSocket/SSE.
> - **`dashboard`** — a thin client that only reads state and POSTs actions. It holds no
>   strategy state and starts no threads.
> A single-instance lock file prevents two engines ever racing your rate limit.

### D3. Per-thread `sleep(5)` is not rate limiting
v1 has three independent consumers of the same quota: the 5s chain poller, the 1/sec REST
failover, and historical/warm-up fetches. When the WebSocket drops — precisely the moment
failover fires — they collide and you get rate-limited exactly when you least want to be.

> **FIX:** One **central token-bucket limiter** with per-endpoint buckets, shared by every
> caller, plus jittered exponential backoff and explicit `RL001`/429 handling that degrades
> gracefully instead of hammering.

### D4. A 3-second tick heartbeat will false-positive constantly
Market-data ticks arrive **on trades**. A quiet strike legitimately goes 10+ seconds without
one. v1 would flap into REST failover repeatedly, burning quota for no reason.

> **FIX:** Separate the two signals: **socket liveness** = protocol ping/pong + connection
> state (fast, ~5s); **data staleness** = per-instrument age with a longer threshold, scaled
> by that instrument's typical inter-trade time. Failover requires socket death, not silence.
> A three-state health model (`HEALTHY / DEGRADED / DEAD`) is surfaced on the dashboard, and
> **signals are suppressed unless HEALTHY**.

### D5. No crash recovery — a restart with an open position loses the position
Nothing in v1 is persisted. Power cut at 11:00 with a live trade = the Guardian is gone, your
trailing stop is gone, and the system restarts thinking you are flat.

> **FIX:** Explicit persisted state machine —
> `IDLE -> SIGNAL_PENDING -> {TAKEN|SKIPPED|EXPIRED} -> POSITION_OPEN -> EXIT_PENDING -> CLOSED`
> — written to DB on every transition, idempotently. On boot the engine **reconstructs open
> positions and resumes the Guardian**, and refuses to emit new signals until the operator
> confirms the recovered state.

### D6. No instrument master, so nothing can be subscribed
The WebSocket needs numeric `securityId`s. v1 never fetches Dhan's scrip master, so
"subscribe to the chosen contract's token" has no token to use. **Lot size and expiry dates
must come from here too — never hardcoded.** BankNifty's lot size and expiry weekday have
both been revised by regulators in recent years; any hardcoded value is a live landmine.

> **FIX:** Daily scrip-master download + cache, expiry list pulled from the API, lot size
> read from the master with a config override and a **startup assertion** that logs loudly if
> they disagree.

### D7. Telegram is in the blueprint and in no other part of the plan
Section 4 has you clicking "Trade Taken" in Telegram; Step 6 has the buttons in Streamlit.
Two sources of truth for the most safety-critical action in the system.

> **FIX:** The **engine is the only source of truth**. Dashboard and Telegram are both thin
> clients hitting the same idempotent API with a `signal_id`. Double-acknowledgement is a
> no-op, not a double trade.

### D8. `plyer` is a weak choice for the one thing that must never fail
On macOS `plyer` shells out to `osascript` (no sound, no actions, silently no-ops when
Terminal lacks notification permission); on Windows it is unreliable on modern builds.

> **FIX:** A `Notifier` abstraction with per-OS backends (osascript/terminal-notifier;
> windows-toasts/plyer; notify-send), **always-on in-dashboard visual + audible alert** as the
> guaranteed channel, and optional Telegram for when you are away from the machine. Failure of
> any one channel is logged and does not suppress the others.

---

## E. SECURITY & OPERATIONS

- **E1.** Dashboard on `0.0.0.0:8501` with no auth exposes your trading state to the LAN.
  -> Bind `127.0.0.1` by default; a shared-secret header for the engine API; explicit opt-in
  for any non-loopback bind, with a startup warning.
- **E2.** Access tokens will end up in logs via exception dumps and request logging.
  -> A logging filter that redacts token-shaped strings globally, plus a test asserting a
  known token never appears in log output.
- **E3.** Dhan access tokens **expire**. v1 has no handling; you would find out mid-session
  via a wall of 401s. -> Detect 401/token-expiry, transition to `DEGRADED`, fire a loud
  distinct alert, and show a token-age countdown on the dashboard.
- **E4.** No log rotation -> tick logs fill the disk. -> Rotating handlers + DB retention job.
- **E5.** The project path contains spaces (`Web Application 6 - Bank Nifty`). Unquoted
  variables in `.sh`/`.bat` will break. -> Every path quoted; CI tests a spaced path.
- **E6.** No line-ending policy -> `.sh` gets CRLF on a Windows checkout and fails to execute.
  -> `.gitattributes` pinning LF for `*.sh`, CRLF for `*.bat`.
- **E7.** Dhan's **Data APIs (live feed + option chain) are a paid subscription** separate
  from the free trading API. -> Documented as a prerequisite, with a preflight check that
  reports exactly which entitlement is missing rather than failing obscurely.

---

## F. What v1 got right (keep, unchanged)

1. **Signal-only, human-in-the-loop.** No auto-execution. Correct and non-negotiable.
2. **Separating fast entry logic from heavy hold logic.** Genuinely good design — cheap gates
   for entry latency, expensive analytics only once capital is at risk.
3. **Local in-memory cache in front of the API.** Right call for latency and quota.
4. **Explicit regime filter to avoid theta decay in chop.** Right instinct, wrong implementation.
5. **Modular file layout.** Broadly kept, with the engine/UI split added.
6. **Recording everything to a time-series store for later analysis.** Essential — and now
   actually used, by the replay harness.

---

## G. Scorecard

| Area | v1 | v2 (this plan) |
|---|---|---|
| Cost accuracy | ~40% of real friction modelled | Full statutory + spread + slippage |
| Signal viability | Filters reject ~everything | Calibrated against recorded data |
| Look-ahead leakage | 4 distinct leaks | Closed, test-enforced |
| Crash safety | None | Persisted state machine + recovery |
| Runs on this Mac | **No** (Docker absent) | Yes, zero external deps |
| Falsifiable | No | Replay + backtest + paper mode |
| Kill switches | None | Six, incl. manual panic halt |
| Notification reliability | Single fragile channel | 3 channels, independent failure |

**Bottom line:** v1 is a good sketch of the *idea* and an unsafe specification for the
*system*. v2 keeps the idea entirely and rebuilds the specification.
