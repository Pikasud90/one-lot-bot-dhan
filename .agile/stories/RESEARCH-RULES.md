# Research rules of the sprint of 24 November 2026 (US-088, US-089, US-091, US-092, US-093)

Owner, 2026-11-24: "analysing the test results of different strategies we have tested and
built over time, first fix the timeframe where you want to trade for that particular
instrument (parameters and setting of the setup strategy can be different for all the
instruments) then ... single out the periods where a strategy works a particular condition or
some setup ... analyse it thoroughly until we see a tradable alpha ... there is no need of
trading same setup on every instrument ... maintain proper setup-strategy book, noting the
best alphas we see till now."

What changed: until now one setting had to hold across instruments. The owner now allows a
different timeframe, setup and parameters per instrument. That freedom multiplies the number
of things tried, so this file fixes, BEFORE any result, how a finding is told from luck. A
study may add to these rules in its own note before it runs; it may not loosen them after.

What is already known (read, do not redo): `docs/research/STRATEGIES_TESTED_2026-11-12.md`,
`ALPHA_SEARCH_2026-11-12.md`, `MICROTREND_STUDY_2026-11-13.md`, `RATCHET_STUDY_2026-11-23.md`,
`REGIME_STUDY_2026-11-23.md`, `MLSTATES_2026-11-23.md`. Short version: no same-day rule has
shown an edge after costs; the stop, target and trail rule adds none; the direction of the
next session cannot be told from market states but the SIZE of its move can (active follows
active); 0 of 90 strategy-and-condition cells passed; the one positive result is the stock
pullback rule as a delivery trade (+1.21% a trade on the 18 desk stocks, +0.78% on 43 others).

## 1. Where and how to run
- Your scripts live in YOUR worktree (`scripts/`), your outputs in the MAIN checkout's
  git-ignored `data/research/<study>_2026-11-24/`.
- Run from the MAIN checkout (the data is there), like this, never otherwise:
  `cd "/Users/sudhanshushekhar/Downloads/ONE LOT BOT - DHAN" && PYTHONPATH=src:scripts:.
  nice -n 10 .venv/bin/python -W ignore "<your worktree>/scripts/<script>.py" <stage>`
  The harness is the main checkout's `scripts/mtf_bruteforce.py` (import it as `h`); guard with
  `assert Path(h.__file__).resolve().parents[1] == Path.cwd().resolve()`. Load a module of your
  worktree by FILE PATH (`importlib.util.spec_from_file_location`); never put your worktree's
  `src` on the path (its schema could create tables in the live store).
- The stores are READ-ONLY for you: `data/onelot.db` (15 GB; SQLite URI `mode=ro`, or the
  harness's own readers) and `data/expired_options.db` (real option bars). Never write to
  them, never run `python -m onelot backfill`, never call port 8787 (the owner's live desk).
- Never open `.env` or `data/api_token.txt`, never print a settings object. Only US-093 may
  ask Dhan for data, and only through the app's own adapter as its story says.
- The owner's desk shares this machine (8 cores). Use `nice -n 10` and the worker limit your
  story gives. Cache intermediate results on disk so a stage can be re-run cheaply.
- Adani Green is excluded everywhere. MIDCPNIFTY is a training instrument: report it apart,
  never as a tradable finding.

## 2. Periods
TUNE: from the first bar to 2024-11-07. CHOOSE: 2024-11-08 to 2025-11-07. TEST: 2025-11-08 to
2026-11-07. Three numbers in a row are always TUNE / CHOOSE / TEST. Bars after 2026-11-07 are
not used for selection (the live record of the desk starts there). TEST has been looked at in
earlier studies for the default settings; say so where it matters.

## 3. What a trade is, and its costs
- Index options (NIFTY, BANKNIFTY, FINNIFTY, SENSEX): a long signal buys a call, a short
  signal buys a put, ONE lot, same day only, never sold short. Result in % of the entry
  premium by the harness's model (`h.pnl`), and a real-premium reading: real one-minute option
  bars from `data/expired_options.db` where the strike traded (see `src/onelot/lab/
  real_options.py` and `scripts/ratchet_study.py real`), otherwise "model less 1.0 point of
  premium". A claim that an option setup works MUST rest on the real-premium reading.
- Stocks, same day: ONE share, long or short, the product's intraday equity charges and
  slippage; state the round-trip cost in basis points you end up with.
- Stocks, delivery (held for days): about Rs 10,000 a trade (owner's decision of 24 Nov),
  long only, the cost model of the pullback study (it includes the depository charge of
  Rs 14.75 on each sale).
- Entry at the open of the minute after the signal bar closes (delivery: the next session's
  open unless the story says otherwise). One open trade per cell at a time.

## 4. Numbers to report for a cell
Trades, entry days, % a trade (t), winners %, payoff (average win over average loss), and the
% a trade for TUNE / CHOOSE / TEST. The t treats all trades of one entry day as ONE draw
(delivery trades: one entry week as one draw). Never a t over single trades.

## 5. Telling a finding from luck (the selection rule, the same in every study)
- A CELL is one instrument (or one declared pool) x one setup (strategy and preset) x one
  timeframe x one condition value (or "all"). It is EXAMINED when TUNE has at least 60 trades
  on at least 30 entry days. Count every examined cell: the count is part of the result.
- DISCOVERED: TUNE mean above zero with t of 2.5 or more AND of a size worth trading: index
  options at least +1.0% of premium a trade by the model; same-day stocks at least +0.10% of
  price; delivery at least +0.50% of the amount.
- CONFIRMED: also CHOOSE mean above zero with t of 1.0 or more on at least 20 trades.
- PASSED: also TEST mean above zero on at least 20 trades and, for index options, above zero
  on the real-premium reading over the whole span.
- Against luck: report how many cells were examined, discovered, confirmed and passed, beside
  what luck alone gives: by arithmetic (examined x 0.6% x 16% x 50%) and, for cells with a
  condition, by a placebo: shuffle the condition labels over the DAYS inside each instrument
  and calendar year (whole days keep their trades), run the same selection, 20 shuffles, mean
  counts. If the real counts are no higher than luck's, say exactly that.
- Neighbours: for each PASSED cell show TUNE at the adjacent timeframes, the other presets,
  and the other instruments of its kind. A lone island among negatives is suspect: say so.
- One condition at a time in the selection. Pairs of conditions only afterwards, for a PASSED
  cell, labelled exploratory.
- Status for the setup book: CANDIDATE = passed AND the count of passes is clearly above
  luck's (the next step is a paper test, never real money on this alone). WATCH = passed but
  the count is no better than luck, or positive in all three periods with a t of 2 or more
  over the whole span but one threshold missed. AVOID = TUNE t of -2.5 or less and negative in
  CHOOSE and TEST (a condition in which NOT to take the setup). REJECTED = anything else that
  the note names, with the number that ruled it out.

## 6. No look-ahead
A condition uses only data that existed before the entry: daily values from the previous
session, intraday values from bars that had closed. A level computed on a bar holds from the
next bar. For each new signal function prove it: cut the data after a signal bar and show the
signal is unchanged (a truncation test on at least 50 random signals). If a result looks too
good, look for a bug first (a condition that sees today's close, an entry before its bar
closed, one trade counted in two cells, a survivor-only universe).

## 7. Rows for the setup book
End your note with a section "Rows for the setup book": one table, these columns, one row per
CANDIDATE, WATCH and AVOID cell and per REJECTED headline idea:

| Instrument | Kind | Setup and preset | Timeframe | Condition | Exit | Trades a year |
% a trade TUNE / CHOOSE / TEST (t whole span) | Real-premium or cost note | Status | Remark |

Kind is one of: option same-day, stock same-day, stock delivery. The remark says in plain
words when it works and when it does not.

## 8. Writing
Notes go to `docs/research/<NAME>_2026-11-24.md`, at most 180 lines. Prose lines at most 100
characters (tables exempt). Plain words; no em or en dashes; ranges with "to"; a real minus
sign in figures. First the answer in five lines, then what was fixed in advance, then the
evidence, then what you could not do. Scripts are ruff-clean (`ruff format`, `ruff check`).

## 9. Honesty
Many cells are examined: never present the best of them without its CHOOSE and TEST and the
count against luck. A plain "nothing passes" is a full answer and a useful one. Backtests are
evidence, not advice to trade. Say what you doubt.
