# Testing

```bash
pytest                      # everything
pytest -m "not slow"        # fast subset, seconds
pytest tests/test_costs.py  # one module
```

**No test requires broker credentials.** Live connectivity is a separate opt-in
check: `python -m onelot.preflight`.

## What the suite proves

### Safety invariants (`test_invariants.py`)
Enforced by scanning the source tree, not by convention:

- **I1** No order-placement call, no order endpoint, no broker write method
  exists anywhere in `src/`. This is what makes the system safe to run
  unattended.
- **I6** A known token never appears in log output — including inside a
  rendered traceback, which is how tokens usually leak.
- Defaults ship safe: `PAPER` mode, capital unset, loopback bind, unverified
  costs flagged, chain poll interval that cannot breach `RL001`.

### Look-ahead leakage (`test_regime.py`)
The important one. `test_filtered_label_is_unchanged_when_future_data_is_appended`
infers a regime at time *t*, appends 100 future observations, re-infers for the
same instant, and asserts the answer did not move.

That test would be worthless if nothing could fail it, so the suite also
demonstrates the leak it is guarding against: sweeping 75 (seed, cut, horizon)
configurations, Viterbi decoding **rewrites past state labels in ~19% of them**.
`predict_proba` (forward-backward smoothing) is shown to leak too.

### Cost model (`test_costs.py`)
Every line pinned against figures computed by hand in
`docs/03_COSTS_AND_SIZING.md`. GST is asserted to exclude STT and stamp duty.
Breakeven is verified exact to one tick, and by a property test across the
premium range.

**If a statutory rate changes, these tests should fail.** That is the point.
Update the worked document first, then the tests.

### Repainting (`test_market.py`)
A forming bar is never exposed as closed; a closed bar's values cannot be
altered afterwards; bars align to the session open (09:15), not the hour.

### Idempotency (`test_trade.py`, `test_store.py`)
Double-clicks, three clients racing, and conflicting actions all resolve to
**exactly one** position. The compare-and-set is at the database level.

### Crash recovery (`test_trade.py`)
Open a position, dispose the database, reopen it, and assert the Guardian
resumes with the correct entry, stop and trail — and that the risk governor
knows a position is live.

### Kill switches (`test_governor.py`)
All seven, including that a halt **survives a restart** and needs a deliberate
reset.

## Regression tests worth knowing about

Three bugs were found by running the system rather than by reading it. Each has
a named test now:

1. **`TestRegimeFlipTypeSafety`** — the Guardian compared a regime label
   (`BULL`/`BEAR`/`CHOP`) against an option side (`CE`/`PE`). Those domains
   never intersect, so the condition was always true and **every position
   exited after one minute**. The original unit test passed it, because the
   test repeated the same confusion. The replay harness caught it.

2. **`TestBarClosePathsBothFire`** — bars close by two paths (tick-driven and
   clock-driven) and the engine consumed only one. In a live feed the tick path
   always wins, so the engine evaluated **zero bars while reporting perfect
   health**. Only booting the real engine surfaced it.

3. **`TestCumulativeDrawdown`** — the daily loss limit does not bound total
   loss. A run of days each inside the daily limit still adds up, and nothing
   objected. A cumulative drawdown limit was added.

4. **`TestStandingHaltsOutliveTheSession`** — halt state was keyed by trade
   date, so a new day loaded no row, started ACTIVE, and the cumulative
   drawdown halt **silently cleared itself overnight**. A cumulative limit that
   resets daily is not a cumulative limit. Found by noticing the same halt
   message repeating once per session in a replay log.

5. **`TestOneLot` / `test_undeclared_capital_does_not_suppress_signals`** —
   when capital became optional, a leftover capital gate would have blocked
   every signal on the shipped config.

## What the suite cannot prove

- **That the strategy makes money.** The replay harness runs on a synthetic
  tape with no exploitable structure; a trend system correctly loses the
  friction there. It validates the machinery and the cost model, nothing more.
- **That the Dhan adapter's live paths work.** No credentials exist in CI, so
  request/response shapes are unverified against the real API. Run
  `python -m onelot.preflight` before relying on it.
- **That your statutory rates are current.** Only a contract note can tell you.
- **A meaningful Sharpe from a short replay.** The metrics deliberately refuse
  to report one below 20 trading days.
- **That macOS actually displayed a toast.** `osascript` returns success even
  when the banner is suppressed by a missing permission.

## Continuous integration

GitHub Actions runs the suite on **ubuntu, macOS and Windows** across Python
3.10–3.12, including a checkout path containing spaces — this project's own
directory has them, and an unquoted path is a classic cross-platform break.
