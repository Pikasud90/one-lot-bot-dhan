# 10 — Review of the Fyers trade bot (what was taken, what was left, and why)

The `TRADE BOT - FYERS` project (Oct 2026, ~10 k lines, 54 tests) was a semi-automated
equity trade assistant on the Fyers v3 API: a 15-stock universe, six intraday strategies
plus a random benchmark, a vectorised backtester with a portfolio layer, a random-search
optimizer with walk-forward, a research gate with PASS/WATCH/FAIL verdicts, a Streamlit
dashboard, and a live engine (WebSocket + REST failover + Guardian). It never ran live
because a Fyers account could not be opened.

## What it did well

1. **Research discipline.** Walk-forward only, plateau-robust parameter selection (best of
   top-5 after jittering neighbours), cost ×2 / slippage ×2 stress, a random-entry control,
   Monte-Carlo drawdown, and a gate that refuses to PASS anything fragile. This is the
   single most valuable thing in the repository and it is now the Lab's research gate.
2. **Honest results.** On ~40 sessions of Yahoo 5-minute data (the only data it had), every
   classic setup FAILED out of sample — VWAP pullback, Supertrend/ADX/MACD, squeeze, RSI-2 —
   and the random control lost −0.14 R per trade, which is exactly the friction. Only the
   relative gap fade (stocks gapping ≥ 0.75 % above NIFTY's gap drift down into the close)
   showed a durable effect on 2015–21 data *and* an untouched 2022–26 holdout, and even that
   decayed in 2024–26 and was rated WATCH. The report is kept verbatim as
   `docs/FYERS_RESEARCH_REPORT_2026-10-05.md`.
3. **Indicators validated against TA-Lib**, correct Wilder seeding, session-anchored VWAP that
   is NaN (not 0/0) on volume-free series, time-of-day relative volume, no-look-ahead tests
   for every strategy.
4. **A clean strategy contract**: pydantic parameters with `opt=[lo, hi, step]` so the UI and
   the optimizer read the same metadata; features computed once and cached; a shared exit
   model (friction floor, target R, ATR trail, partials, time stop, square-off).
5. **A realistic simulator**: next-open fills, gap-through-stop handling, same-bar ambiguity
   resolved on 1-minute bars, trailing effective from the next bar.

## What was wrong or did not fit

1. **Fixed-fractional sizing and Kelly presets.** Correct for a stock book, irrelevant under
   the one-lot rule. The portfolio layer (capital, heat, leverage, daily-loss % lock) was
   removed; the Lab's execution models size nothing.
2. **Fyers-specific everything** (symbol master, TOTP login, WebSocket, error codes). Removed.
3. **Streamlit + PostgreSQL/TimescaleDB + Docker.** The same dependency mistake the Bank Nifty
   build had already corrected: not installable on the target Mac without Docker, re-runs
   the script on every click, duplicates threads. Replaced by the one-process FastAPI app and
   SQLite.
4. **Stock-only universe.** Indices and FX have no volume; several strategies hard-filtered on
   relative volume and would have produced zero signals on them. Fixed in `strategies/base.py`
   (volume filters are explicitly neutral when the series has no volume) and tested.
5. **Research spaces tuned to 5 m/15 m/60 m stocks only.** Re-expressed per strategy and
   applied per instrument; timeframe classes decide the walk-forward window sizes.
6. **Sharpe from short windows** was reported without a warning; the Lab now notes it under
   20 trading days.
7. **The `square_off` identifier** collided with the Bank Nifty project's no-orders scanner;
   renamed `exit_time` rather than weakening the invariant.

## Verdict

The research machinery was good and is now the spine of the Lab. The strategies were
ordinary and, on the data available, did not work — which the project itself reported
honestly. Nothing in it should be traded on the strength of its backtests; everything in it
should be re-run on five years of Dhan 1-minute data before any verdict is believed.
