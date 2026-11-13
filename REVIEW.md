# One Lot Desk: end-to-end review and critique

Date: 11 November 2026. Reviewer: Claude (Fable 5), working in the owner's session.

Reviewed state: branch `sprint-02-owner-feedback` at commit `09dd210`, plus the uncommitted work of
11 November (16 modified files, 26 new files). Line numbers refer to that working tree. They will
drift as the code changes, so every finding also names the function or route it is about.

---

## 0. How to use this file

This file is the single starting point for whoever updates the app next (the owner named Opus). It
does three jobs:

1. It records what the strategy research of 11 November found, with the numbers.
2. It lists what is wrong, misleading or awkward in the app, with the place in the code and a fix.
3. It puts the work in an order, as stories that continue the existing backlog (`.agile/board.md`).

| If you want | Read |
|---|---|
| The verdict and the top findings in two pages | Section 1 |
| What changed in the code on 11 November | Section 2 |
| The backtest evidence and what to do about the strategies | Section 3 |
| Defects by area: desk, paper book, Lab, data, API, legacy engine, operations | Sections 4 to 10 |
| The user interface: layout, ergonomics, screen by screen | Section 11 |
| Code structure and missing tests | Sections 12 and 13 |
| The ordered work plan with acceptance criteria | Section 14 |
| Decisions only the owner can make | Section 15 |
| Research outputs, measurements, sources and the index of findings | Appendices A to D |

The file is long, so read it in pieces. Each part starts at the line given here (as
written on 11 November 2026; search for the heading if the file has changed since).

| Part | Starts at line |
|---|---|
| 0. How to use this file | 11 |
| 1. The short version | 113 |
| 2. What was built on 11 November 2026 | 179 |
| 3. Strategy and profitability | 253 |
| 4. The desk and its alerts | 502 |
| 5. The paper book and trade management | 662 |
| 6. The Lab | 740 |
| 7. Data | 836 |
| 8. API and security | 924 |
| 9. The legacy Bank Nifty option engine | 1017 |
| 10. Operations and documents | 1087 |
| 11. The user interface | 1204 |
| 12. Code structure | 1733 |
| 13. Tests | 1912 |
| 14. The work plan | 2039 |
| 15. Decisions only the owner can make | 2813 |
| Appendix A. Research outputs of 11 November 2026 | 3075 |
| Appendix B. Interface and API measurements | 3109 |
| Appendix C. Sources | 3212 |
| Appendix D. Index of findings | 3289 |

**Severity.** P0: the app shows or uses something wrong today in a way that can mislead a trading
decision. P1: must be fixed before the paper test can be trusted or the app can run unattended.
P2: should be fixed; robustness, quality, ergonomics. P3: polish.

**Evidence.** "Verified" means I read the code at the line given, or saw the behaviour on the
running desk on 11 November. "Measured" means the number comes from a script whose output is listed
in Appendix A, or from a measurement of the running desk that is recorded in Appendix B. Anything
I could not check is marked "to verify".

**Finding IDs.** S strategy, F desk and alerts, P paper book and trade management, L Lab, D data,
A API and security, G legacy engine, O operations and documents, U user interface, C code,
T tests. US-nnn is a story of the work plan (section 14). Q-1 to Q-14 are the questions for the
owner (section 15).

### 0.1 Rules every change must keep

These are the owner's standing rules. A change that breaks one is wrong even if its tests pass.

1. **The system never places, modifies or cancels an order.** `tests/test_invariants.py` scans
   `src/onelot/**/*.py` and fails on identifiers such as `place_order`, `modify_order`,
   `cancel_order`, `square_off`, `exit_position`, `kill_switch_order`. Use `exit_time` and
   `SESSION_END` for the end-of-session rule.
2. **Credentials live only in `.env`** (or are entered in the Settings tab by the owner). Never
   print, log or commit a token. The API session token is in `data/api_token.txt`.
3. **One unit per trade**: one lot of an index option, one share of a stock. No position sizing.
4. **Indices: buy options only** (calls and puts). Never sell futures or options. Stocks: buy or
   sell one share.
5. **Alert rules**: every alert opens a paper trade; an alert has exactly one exit; the alert's
   prices are the record; no risk governor blocks an alert or a paper trade.
6. **The option alerts stay in the paper test** until each slot has 30 closed trades (owner,
   8 November 2026). Nothing in this file asks to change that. Section 3 is the evidence that
   review should weigh.
7. **One setting across instruments.** Adani Green is excluded from research. Verdicts are stated
   honestly, with costs.
8. **Interface**: the bright Day theme and large type stay. The dark 15-pixel look was rejected.
9. **Git**: commit and push only when the owner asks. `main` stays untouched. Before any push,
   scan the history for secrets and print locations only.
10. **The gate stays green**: `bash scripts/gate.sh` (ruff format, ruff check, the mypy ratchet,
    `pytest -m "not live"`).

### 0.2 Commands

```bash
cd "/Users/sudhanshushekhar/Downloads/ONE LOT BOT - DHAN"
bash scripts/gate.sh                                   # full gate, about 9 minutes
.venv/bin/python -W ignore -m pytest -o addopts="" -q -p no:cacheprovider tests/desk   # one folder
.venv/bin/python -m onelot run --port 8787             # the desk; scripts/start_mac.sh fails (O-10)
PYTHONPATH=src:. .venv/bin/python -W ignore scripts/mtf_stage2.py     # a research script
sqlite3 "file:data/onelot.db?mode=ro" ".timeout 5000" "<sql>"         # read the store safely
```

Bar timestamps in the database are UTC. Indian Standard Time is UTC plus 5 hours 30 minutes.

---

## 1. The short version

### 1.1 Verdict on the strategies

This is evidence from backtests and one day of paper trades. It is not investment advice.

- **No setting on the existing scoreboard is profitable after costs in all three test periods:
  0 of 5,752.** The periods are TUNE (8 Nov 2021 to 7 Nov 2024), CHOOSE (to 7 Nov 2025) and TEST
  (to 7 Nov 2026).
- **The owner's multi-timeframe Supertrend rule** (Supertrend with period 14 and multiplier 1.7 on
  1-minute, 5-minute, 15-minute, 30-minute, 60-minute and daily bars; trade only when all agree;
  the Supertrend line is the stop and the trail) was built and tested on about 15,000 variants per
  instrument group.
  - **Stocks, one share:** no intraday variant is positive in all three periods. The agreement of
    timeframes does carry information, but the gross edge is 0.01% to 0.06% a trade against a
    round-trip cost of about 0.166%. Carried long trades look better until the depository charge
    of ₹14.75 per delivery sale is counted; at one share it turns +0.28% a trade into −1.64%.
  - **Index options, one lot:** the same-day version loses on modelled premiums. The carried
    version (hold calls for days behind the 30-minute line) shows +4.1% of premium a trade on
    modelled premiums. On the real premiums that could be found (41% of the trades) it shows
    **−9.1%**. The priced subset leans toward losing trades, and the large winners that carry the
    modelled result could not be priced, so the claim is unverified, not disproved.
- **The index slot that is running in the paper test** (`supertrend_vol`, 30-minute) shows +2.9%
  a trade on modelled premiums with its own settings (485 trades, t = 2.4). On the real premiums
  that could be found it is at zero over five years and loses in both later periods: +1.9% /
  −1.7% / −3.1%, and −0.1% overall (265 of 488 trades priced, the study of 8 November). Rebuilt
  under the owner's logic (Supertrend 14 and 1.7, the 30-minute line as the stop) it shows +0.9%
  modelled and **−2.1% a trade on real premiums** (544 priced trades, t = −2.3).
- **Live, 11 November:** that slot raised two alerts at 09:45 (NIFTY and SENSEX calls). Both paper
  trades were stopped out, for −₹20,579 net in one day at one lot each.
- **What earns in the tests is exposure to a trend held across days**, not the entry filters. The
  desk cannot hold a paper trade overnight: the backlog's US-027 covers stocks only, and
  section 14 adds US-041 for index options. So the trail the owner asked for is implemented and
  correct, but inside a single session it rarely matters: 75% to 95% of trades end at the
  session close before the line is reached.

### 1.2 The findings that matter most

| # | Finding | Severity | Section |
|---|---|---|---|
| 1 | No tested setting has a verified edge after costs; on real premiums the running index slot is at zero over five years and loses in both later periods | P0 | 3 |
| 2 | The session calendar is out of date since the Closing Auction Session began on 3 August 2026: F&O stocks stop continuous trading at 15:15, the app still assumes 15:30 and exits at 15:15 | P0 | 7 (D-1) |
| 3 | One lot of an index option risks ₹10,000 to ₹12,500 per alert; two correlated indices alert together; the screen shows a ₹6,000 daily loss limit that gates nothing | P1 | 3 (S-4, S-5), 9 (G-1) |
| 4 | If the desk stops polling, nothing on screen says so; the header keeps showing "live" | P1 | 4 (F-2) |
| 5 | Overnight carry does not exist, so the owner's "long horizon" idea cannot be paper-tested | P1 | 3 (S-2) |
| 6 | Option backtests use modelled premiums by default and flatter the results; real premiums cover under 60% of trades | P1 | 3 (S-3), 6 (L-3) |
| 7 | The desk picks monthly SENSEX and FINNIFTY contracts whose measured half-spread is 1% to 24% of premium | P1 | 3 (S-6) |
| 8 | The store is not kept up to date: only a manual backfill writes bars, and 5 of 6 backfill jobs ended with errors that the screen reduces to a count | P1 | 7 (D-2, D-3) |
| 9 | Any caller can fetch the API token; every read route is open | P1 | 8 (A-1) |
| 10 | The Lab cannot drive the new strategy: its text settings are free-text boxes, sweeps skip it, and Optimize would tune the two values the owner fixed and ignores costs | P1 | 6 (L-1, L-2) |
| 11 | Four separate implementations of stop, trail and exit logic are kept in step only by tests | P1 | 5 (P-1) |
| 12 | The interface buries what the owner needs in the session (is the desk alive, what is open, where is the stop) under reference tables; times mix UTC and IST | P1 | 11 |

### 1.3 What to do first

1. **Restart the desk** so it loads the code and configuration of 11 November (see section 2.4),
   and renew the Dhan token before the next session. The token shown in the header runs out at
   06:29 on 12 November.
2. Fix the session calendar (D-1) and add the stall banner (F-2). Both are small, and both affect
   whether the screen can be believed.
3. Show the truth about open paper trades and risk (U-14, U-15), and remove the legacy risk block.
4. Decide the questions in section 15. Several stories depend on the answers.
5. Then follow the phases in section 14.

---

## 2. What was built on 11 November 2026

Nothing is committed. The 26 new files are staged with `git add` (so the repository hygiene test
passes); the 16 modified files are unstaged. The last commit is still `09dd210`.

### 2.1 The strategy and the trail

| Piece | Where | What it does |
|---|---|---|
| Trend ladder | `src/onelot/indicators/mtf.py` | Supertrend(14, 1.7) on 1m, 5m, 15m, 30m, 60m and 1d bars built from the same one-minute history, with no look-ahead: a slower bar counts only after it has closed |
| Strategy `supertrend_mtf` | `src/onelot/strategies/supertrend_mtf.py`, `mtf_support.py` | Entry on the decision timeframe when the chosen set of timeframes agrees (`gate`: `all`, `slower`, `faster`, `daily`, `trend`); optional checks on the trend timeframe (ADX, RSI, relative volume); entry only within `flip_window` bars of a fresh turn; one trade per trend |
| Line trail | `src/onelot/lab/simulator.py`, `src/onelot/paper/manage.py`, `book.py`, `position.py` | A strategy may publish `long_trail` and `short_trail`. After each closed bar the stop moves to that level if it is better, and never moves back. The new stop is in force from the next bar. The paper book records each move as a `STOP_MOVED` event |
| Trail on a chosen timeframe | `trail_tf`, `stop_buffer_atr` | The stop can follow the line of a slower timeframe (for example the 60-minute line), optionally a number of ATRs behind it |
| Same option for the two older strategies | `supertrend_vol.py`, `supertrend_adx.py` | `line_trail` (off by default, so the running paper slots are unchanged) |
| Desk wiring | `src/onelot/desk/tracker.py`, `desk.py` | Every instrument card carries the six-timeframe reading (`trend`); each slot reports its trail (`trail_view`); the desk passes the level to the paper book through `PaperBook.follow_trail` |
| Clean history at load | `src/onelot/data/hygiene.py`, `src/onelot/desk/history.py` | When bars are read: drops exchange mock-trading sessions and a flat Muhurat session, adjusts two demergers the vendor left unadjusted (ITC, Tata Motors), and rebuilds daily bars from the cleaned one-minute bars. The database itself is unchanged (see D-4) |
| Interface | `src/onelot/ui/app.js`, `styles.css` | A "Micro trend" strip on each card, a table in the open card and focus panel, and a Trail column |

### 2.2 The new paper slot

`config/desk.yaml` now has a second index slot beside the existing one:

```yaml
- name: supertrend_mtf
  timeframe: 5m
  params: {allow_long: true, allow_short: false, st_period: 14, st_mult: 1.7, gate: all,
           trend_tf: 30m, flip_window: 4, trend_adx_min: 25, trend_rsi_min: 60,
           entry_end: "13:00", trail_tf: 60m, stop_buffer_atr: 2.0, max_trades_per_day: 1}
```

It is the owner's rule in the form that did best in the same-day tests: buy a call when all six
timeframes point up, within four bars of a fresh 30-minute turn, with ADX of at least 25 and RSI of
at least 60, before 13:00, once a day. The trade ends when a 60-minute bar closes below the
60-minute Supertrend line. A resting stop stands two ATRs under that line as a backstop and moves
up at each 60-minute close. Through the Lab engine it shows 202 trades in five years, +2.6% of
premium a trade on modelled premiums (t = 1.8), and 95% of exits at the session close. On real
premiums the closest research structure shows +0.7% (t = 0.3) on 110 priced trades. **It is in
the paper test as the owner's rule, not as a proven edge.** The plain rule (all six agree, any
time of day) lost after costs over five years, so it is shown on every card but not alerted.

### 2.3 How it was checked

- 369 new tests in 13 files. The full gate passed: ruff format and check clean, mypy ratchet at
  195 errors (none above the baseline), **7,754 tests passed, 1 skipped**, in 8 minutes 45 seconds.
- A replay of two windows (3 to 25 August and 27 October to 6 November 2026, five indices) through
  the desk's tracker and the paper book's fill and manage functions reproduced every Lab trade:
  10 of 10 for the slot, 57 of 57 for the owner's plain rule, with 52 and 245 stop moves.
- The research harness was validated against the product code: its charges equal `CostModel.leg`
  to 1e-12, and 144 random structures (135,055 trades) equal a plain loop.

### 2.4 What is not done

| Gap | Why it matters |
|---|---|
| No end-to-end test of the line trail through `Desk.poll()` with a real `PaperBook` | The replay above skips `Desk.poll`, the `PaperBook` class, option pricing and the one-live-idea-per-instrument rule (T-1) |
| The running desk still has the old code | It was started at 06:42 on 11 November. Use Restart, not "Reload desk config": the old process does not know `supertrend_mtf` and would only log "unknown strategy" (F-4). Until the restart every card shows "Micro trend: no reading yet" |
| Overnight carry | New story US-041 for index options (the backlog's US-027 covers stocks only); see S-2 |
| The Lab form for `supertrend_mtf` | See L-1 |

To restart, from the project folder: stop the running process, wait until it has gone (about
ten seconds; `data/engine.pid` disappears), then start the app the way the running desk was
started.

```bash
bash scripts/stop_mac.sh
nohup .venv/bin/python -m onelot run --port 8787 > logs/app.out 2>&1 &
```

A start that comes too early ends with "another engine is already running" in
`logs/app.out` and harms nothing: run the second command again. Do not use
`bash scripts/start_mac.sh`: it stops with an error before the app starts (O-10).

---

## 3. Strategy and profitability

All results are after the modelled costs, per trade, in percent of the entry price (for an option,
percent of the premium paid). "t" is the t-statistic of the mean; values near 2 or above are
unlikely to be chance for a single pre-chosen test, but every structure here is the best of
thousands, so even t = 2.3 is weak. The full method and tables are in
`docs/research/SUPERTREND_MTF_2026-11-11.md`; the raw outputs are listed in Appendix A.

### 3.1 The existing scoreboard: 5,752 settings, none positive in all three periods

| Group | Strategy, timeframe | Settings | Positive in TUNE / CHOOSE / TEST | Median % per trade, TUNE / CHOOSE / TEST |
|---|---|---|---|---|
| Index options | `supertrend_vol` 30m | 68 | 60% / 69% / 6% | +0.33 / +1.17 / −4.10 |
| Index options | `cpr_breakout` 60m | 69 | 51% / 0% / 48% | +0.07 / −3.52 / −0.06 |
| Index options | `supertrend_vol` 60m | 67 | 25% / 25% / 34% | −0.91 / −1.66 / −3.10 |
| Index options | every other family | | | −1 to −5 |
| Stocks | `supertrend_vol` 30m | 70 | 41% / 0% / 9% | −0.06 / −0.14 / −0.07 |
| Stocks | `supertrend_adx` 30m | 30 | 17% / 0% / 3% | −0.07 / −0.20 / −0.11 |
| Stocks | `gap_fade` 15m | 61 | 16% / 2% / 0% | −0.07 / −0.13 / −0.12 |
| Stocks | every other family | | | −0.05 to −0.27 |

- **Direction.** Index options: calls only are positive in 17% / 17% / 2% of settings, puts only
  in 9% / 9% / 19%. Stocks: short only 9% / 2% / 3%, long only 3% / 1% / 0%.
- **Timeframe.** No timeframe is consistently better. Index options: 15m 6% / 9% / 10%, 30m
  10% / 18% / 4%, 60m 16% / 6% / 17%.
- **Instrument.** No instrument is consistently favourable. An index is profitable in 12% to 29%
  of settings in a given period, a stock in 6% to 31%.
- **Supertrend multiplier on index options.** 2.0 is the only value with positive medians in TUNE
  and CHOOSE (+0.45, +1.12) and it loses 2.39% in TEST. The owner's 1.7 with period 14, tested
  separately on the running slot, gives +1.31% over five years and −2.53% in TEST.

### 3.2 The three paper slots, in depth (2,500 backtests)

| Slot | Trades in five years | Result | What decides it |
|---|---|---|---|
| Index options, `supertrend_vol` 30m, calls | 485 | +2.94% a trade on modelled premiums (t = 2.4); TEST year +0.50% (t = 0.2) | The day's direction: +9.3% on days NIFTY closed up, −12.4% on down days. 75% of trades end at the session close. Every exit by stop or by flip is a loss |
| Stocks, `supertrend_vol` 60m, short | 129 | Net ₹296 in five years at one share | Gross +0.22% to +0.25% a trade against about 0.16% cost. CHOOSE year negative |
| Stocks, `supertrend_adx` 30m, short | 175 | Net ₹274 in five years at one share | Same. About two trades a month across 18 stocks |

### 3.3 The owner's multi-timeframe rule

Stage 1 tested 5,760 structures per instrument (which timeframes must agree, which line is the
stop, exit on touch or on a close, same day or carried). Stage 2 tested 14,976, adding the
scoreboard strategies' own filters under the owner's logic.

**Stocks, one share**

| Test | Result per trade |
|---|---|
| No signal at all: buy at 09:30, sell at the close | −0.17% (the cost of a round trip is about 0.166%) |
| Owner's rule, same day, best line and exit (short, 15m line, exit on touch) | −0.13% (gross +0.04%) |
| Owner's rule with the 1-minute line as the stop | Stopped after about 11 minutes; 13% to 15% winners |
| All 3,450 same-day structures of stage 1 | None positive in TUNE; median gross +0.015% |
| `supertrend_vol` short 60m under the owner's logic | −0.05 / −0.07 / −0.05 (1,243 trades, gross +0.11%) |
| `supertrend_adx` short 30m under the owner's logic | +0.04 / −0.17 / +0.04 (404 trades, 0.00% overall) |
| All 6,654 same-day structures of stage 2 | None positive in all three periods |
| Best carried long (60m line, held 2.7 sessions) | +0.28% (t = 1.4), ₹1,895 in five years; **with the ₹14.75 depository charge per sale: −1.64% (t = −7.4)** |

More agreeing timeframes do lift the gross result (from +0.012% with one timeframe to +0.021% with
six). The effect is real and about ten times too small to pay the costs at one share.

**Index options, one lot, modelled premiums**

| Test | TUNE / CHOOSE / TEST | All five years |
|---|---|---|
| No signal: buy a call at 09:30, sell at the close | −1.19 / −0.98 / −1.82 | |
| No signal: buy a call, hold five sessions | +0.83 / +1.37 / −18.19 | |
| Owner's rule, same day, best line (60m, exit on close) | TEST −1.2 | −0.39% |
| The running slot's structure under the owner's logic | +1.45 / +0.81 / −0.69 | +0.88% (948 trades, t = 1.0) |
| `carry30`: owner's rule, calls carried behind the 30m line | +3.61 / +11.76 / −2.16 | **+4.10%** (1,041 trades, t = 2.3, 36.6% winners) |
| `intra_volc`: fresh turn plus filters, same day | +2.69 / +5.16 / +2.93 | +3.07% (204 trades, t = 2.2) |

Passing TUNE and CHOOSE predicted nothing about TEST: of 2,511 structures that passed, 10% were
positive in TEST; of the 9,956 that did not pass, 36% were. The index itself drifted +1.3%, +8.0%,
+25.4%, +8.3% and +0.3% in the five years; the carried-call results follow that drift.

### 3.4 How fragile is the best modelled result (`carry30`)?

| Variation | % per trade |
|---|---|
| Base | +4.10 |
| Costs × 1.5 / × 2 | +3.91 / +3.73 |
| Entry one minute late | +3.87 |
| Alert on the next minute | +2.30 |
| A resting stop instead of an exit on the close | +2.08 |
| Yesterday's VIX for the premium instead of the same day's | +2.91 |
| Supertrend (10, 1.7) / (21, 1.7) / (14, 1.4) / (14, 2.0) / (14, 2.5) | +3.83 / +4.36 / +2.73 / +4.81 / +1.74 |

- By year: +4.96, +3.29, +2.85, +11.76, **−2.16**.
- One trade: median −7.5%, worst −64.8%, best +405.5%. Winners average +38.7%, losers −15.9%.
- **The result is a few very large winners.** The best 1% of trades (10 trades) make ₹695,222 of
  the ₹982,412 total. Without the best 5% of trades the mean is **−3.15%**.
- Trades closed the same day average −16.6% (243 trades); held one session −6.3% (428); held two
  to three sessions +17.0% (315); held four or more sessions +102.6% (55).
- Deepest fall ₹358,903 at one lot (October 2025 to October 2026); 379 days below an earlier high;
  29 of 59 months positive; 20 losing trades in a row at worst.

### 3.5 Real premiums

Real premiums are the stored Dhan prices of expired contracts (`src/onelot/lab/real_options.py`).
A trade is "priced" when the contract traded within three minutes of the entry and its strike was
stored (at the money plus or minus ten strikes).

| Structure | Trades | Priced | Model, all trades | Model, priced trades | **Real, priced trades** |
|---|---|---|---|---|---|
| `carry30` | 1,041 | 423 (41%) | +4.10 | −8.23 | **−9.14** (t = −8.0) |
| `carry60` | 715 | 281 (39%) | +5.77 | −9.80 | **−8.86** (t = −6.6) |
| `intra_volc` | 204 | 110 (54%) | +3.07 | +2.56 | **+0.74** (t = 0.3) |
| `slot_vol`: the running slot's structure under the owner's logic | 948 | 544 (57%) | +0.88 | −1.75 | **−2.09** (t = −2.3) |
| The running slot with its own settings (study of 8 November) | 488 | 265 (54%) | +2.85 | +0.78 | **−0.14** (t = −0.1) |

- On the same trades the model and the real prices differ by −1.8 to +0.9 points, so the model is
  not wildly off trade by trade.
- The problem is coverage. The trades that could not be priced are the winners: for `carry30`,
  the 65 trades whose exit strike had moved beyond the stored range have a modelled result of
  +61.6%, and the 137 trades with an unknown expiry at entry +17.9%. The fat tail that carries the
  modelled result cannot be checked with the stored data.
- `slot_vol` on real premiums, by index: NIFTY −0.4%, BANKNIFTY −2.6%, FINNIFTY −1.8%,
  MIDCPNIFTY −4.0%, SENSEX −4.5%. TEST year −3.8% (t = −2.4). Real winners 37.5%. `slot_vol` is
  the running slot's entry rule with Supertrend 14 and 1.7 and the 30-minute line as the stop.
- The running slot with its own settings (Supertrend 14 and 2.0, a 2.5R target, a 1.5 ATR trail,
  a 12-bar time stop) was priced on 8 November (`docs/research/REAL_OPTIONS_2026-11-08.md`,
  on the bars as stored then): real +1.92% / −1.67% / −3.06% in TUNE / CHOOSE / TEST, 43.4% real
  winners. On NIFTY and BANKNIFTY, where 91% of its trades are priced, +0.94% (t = 0.7); on the
  other three indices, where 30% are priced, −2.28% (t = −1.0). The owner saw this result on
  8 November and kept the slot in the paper test (rule 6).

### 3.6 The product strategy through the Lab engine

The same rule run through `supertrend_mtf` and the Lab simulator (the code the desk uses):

| Variant | Trades | % per trade | t |
|---|---|---|---|
| Owner's rule, calls, same day, 30m line, exit on touch | 2,023 | −0.36 | −0.6 |
| Owner's rule, calls, same day, 60m line | 1,987 | −0.24 | −0.4 |
| Owner's rule, puts, same day | 1,291 | −1.19 | −1.1 |
| The paper slot (fresh turn, ADX and RSI, 60m line with 2 ATR) | 202 | +2.64 | 1.8 |
| Owner's rule, stocks, long / short, same day | 5,884 / 4,909 | −0.14 / −0.15 | −8.1 / −6.1 |

### 3.7 Live evidence, 11 November 2026

The running index slot raised two alerts at 09:45:35. Both were marked low confidence (44), and
the alert text itself quoted a negative backtest for the instrument (NIFTY: 267 trades, −0.085R,
profit factor 0.72; SENSEX: 143 trades, −0.070R, 0.78).

| Paper trade | Entry | Exit | Result |
|---|---|---|---|
| SENSEX 82000 CE, 26 Nov, 20 units | ₹1,476.35 at 09:45 | ₹969.35 at 15:01, stop | −34.3% of premium, net −₹10,228 |
| NIFTY 25250 CE, 23 Nov, 65 units | ₹425.10 at 09:45 | ₹267.15 at 15:04, stop | −37.2% of premium, net −₹10,351 |

One minute after the alert the premiums were already 1.5% and 1.7% lower; five minutes after,
1.4% and 3.1% lower. Two trades are no sample. They do show the size of one bad day at one lot:
−₹20,579.

### 3.8 What the evidence says

1. **At one share, costs decide the stock result.** The signals contain a little information, far
   less than a round trip costs, and the fixed depository charge rules out holding overnight.
2. **For index options, the direction of the day and week decides the result.** Entry filters add
   little; the ADX family hurts. Long calls gain when the index drifts up and lose otherwise. The
   TEST year, with +0.3% drift, is negative for almost every long-call structure.
3. **The only structure with a meaningful modelled result is carried for days**, and its profit
   is a handful of very large winners that the stored real premiums cannot confirm.
4. **Selection did not generalise.** A structure that passed two periods was less likely, not
   more likely, to be positive in the third.
5. **The trail is implemented as asked and is correct in the tests.** It will matter only when a
   trade can live longer than a session.

### 3.9 Recommendations

**S-1 (P0). Say on every alert what its own evidence is.** Today's alerts were raised by a slot
whose backtest for that instrument was negative, and said so only in small print. Show it as the
headline: "Paper-test alert. Backtest for this slot on real premiums: −0.1% a trade over five
years, −3.1% in the last year." See F-3 for the mechanism.

**S-2 (P1). Build overnight carry for index-option paper trades (US-041) before judging the
trail.** The owner's goal is to "capture the long horizon profits". Same day, the line is seldom
reached. Scope it narrowly first: long calls and puts only, the existing line trail, an exit on
the trail timeframe's close, a gap rule at the open (exit at the open if the open is beyond the
stop), and a contract that is named at the alert with enough days to its expiry, is never
changed, and is held to that expiry at the latest. Stocks stay same-day (the depository charge).
Whether the desk carries at all is the owner's decision (Q-6). Done when a paper trade opened on
one day can end on a later day by `TRAIL_STOP`, the Lab and the paper book agree on a replay,
and the alert still has exactly one exit.

**S-3 (P1). Make real premiums the default for option backtests, and store enough to price the
winners.** Extend `scripts/expired_options_fetch.py` so that a carried trade can be followed: the
entry strike must stay stored for the life of the trade even when the index moves more than ten
strikes away, and the expiry must be known at entry. Report coverage next to every option result
("423 of 1,041 trades priced"). Until coverage is above about 90%, label any option result on
modelled premiums as "modelled, unverified".

**S-4 (P1). State and budget the rupee risk of one lot.** The stop is set on the index, about
1.2% away, which is about a third of the premium of the monthly call of about 0.65 delta that
the alert names: ₹12,300 to ₹12,500 estimated at the alert, ₹10,200 to ₹10,350 realised. Rule 5
forbids blocking an alert, so do not block: a setting (`risk_budget_inr`) puts the estimated
loss at the stop beside a budget and marks the alert "over budget". Letting the same setting
choose a contract that fits the budget is the owner's decision (Q-7). Test the choice of strike
and expiry in the Lab on real premiums first, since a cheaper contract also responds less.

**S-5 (P1). Treat correlated indices as one idea.** NIFTY and SENSEX alerted in the same second
and lost together. Group the indices (NIFTY with SENSEX; BANKNIFTY with FINNIFTY; MIDCPNIFTY
alone, to be confirmed from the stored returns), allow one live idea per group, and prefer the
member with the tighter measured spread. Count sessions, not trades, in every statistic. This
changes which alerts are raised, so it needs the owner's decision (Q-8).

**S-6 (P1). Check liquidity before naming a contract.** The Live Desk's own measurement (1,960
quotes over 30 days) shows a median half-spread of 24.2% of premium for monthly SENSEX puts, 2.3%
for SENSEX calls (9.9% between 09:15 and 10:00), 1.2% to 1.7% for FINNIFTY, against 0.17% for
NIFTY calls and 0.18% for BANKNIFTY calls. The Lab assumes 0.6% plus two ticks for all of them.
First check that the sampler in `src/onelot/desk/fillcost.py` is not reading stale or one-sided
quotes. Then add a spread check at the alert: when the half-spread is above a limit, name the more
liquid contract (`option_expiry: nearest` where a weekly exists) or mark the alert "wide spread".
Feed the measured spread per index into the Lab's cost model.

**S-7 (P1). Decide now what the 30-trade review can and cannot conclude (extends US-015).** The
per-trade spread of the index slot is about 27% of premium. With 30 trades the 95% interval on the
mean is about ±10% of premium, so 30 trades cannot confirm or refute an edge of +3%; that would
take about 340 trades, roughly three and a half years at this slot's rate, and trades on the same
day in several indices are not independent. Use the 30-trade review for what it can show: fills
against alert prices, slippage against the model, the exit mix, the adverse move in the minutes
after an alert. Write down in advance the result that would end a slot (Q-5).

**S-8 (P1). Stocks at one share.** Five years of the two stock slots net under ₹300 each. The
scoreboard rows for stocks will read ₹0 to ₹50 a trade whatever happens. This is the owner's call
(Q-10): keep them as practice alerts with that stated on the card, or pause them.

**S-9 (P2). Do not alert on delayed data.** On 5 and 6 November stock alerts were raised on Yahoo
one-minute data that was about 15 minutes late (the card was amber, but the alert went out and a
paper trade opened). An alert valid for 10 minutes (`signal_valid_minutes`) on data 15 minutes old
is already expired. A slot's `fresh` flag is judged at the time of the data, not the wall clock.
Judge it against the wall clock and hold alerts while the delay exceeds the validity window.
Holding an alert back is the owner's decision (Q-4).

**S-10 (P2). Details of the new slot.** It takes one entry per trend of the entry timeframe, so a
signal blocked downstream (for example by the other index slot holding the instrument) is lost for
that whole trend. The two index slots compete for each index: whichever alerts first silences the
other. Whether that is wanted is the owner's decision (Q-9); if not, let the blocked slot re-arm.

**S-11 (P2). Every report shows the no-signal benchmark and the number of settings tried.** A
strategy that makes +0.9% where "buy a call at 09:30" makes −1.2% is different from one that makes
+0.9% where the benchmark makes +0.8%. The research scripts print both; the Lab does not (L-5).

**S-12 (P2). Protect the only clean test data.** All three periods have now been used to choose
structures. Bars from 8 November 2026 onward are the only untouched sample. Record every future
tuning run (date, settings tried, data end date) so that the paper test stays a real test.

---

## 4. The desk and its alerts

The desk (`src/onelot/desk/`) polls bars for 23 instruments, runs each instrument's strategy slots,
raises alerts and hands each alert to the paper book. Its core loop is sound: it survived the whole
session of 11 November, raised two alerts and followed both paper trades to their exits. The
findings below are about what it tells the owner and what it forgets.

**F-1 (P1). "Reload desk config" forgets today's trade counts and cooldowns.**
- Where: `Desk.reload`, `src/onelot/desk/desk.py:335`. `_restore_slot_counters` (`desk.py:395`)
  is called only at start (`desk.py:257`).
- What happens: a reload builds new trackers and copies only their bars and daily history. Every
  new slot starts with `signals_today = 0` and no cooldown. All four paper slots are limited to
  one trade a day, so a reload during the session lets a slot alert a second time that day.
- Fix: call `self._restore_slot_counters(now_ist())` inside `reload`, after `_build_trackers()`.
  The alert archive it reads is already in memory.
- Done when: a test raises an alert, reloads, feeds a second qualifying bar and gets no second
  alert; a cooldown in force before the reload is still in force after it.

**F-2 (P1). Nothing on screen says the desk has stopped polling.**
- Where: `Desk._loop` (`desk.py:455`) catches every exception from `poll()` and only logs it. The
  snapshot is rebuilt at the end of a successful poll (`desk.py:2108`), so after a failure the
  last good snapshot stays on screen. In the interface the footer shows "live" whenever any
  stream frame arrives (`connect`, `app.js:897`), and the legacy engine sends a frame every
  second. The header badge "desk dhan-ws live" means "the data source is not delayed"
  (`delayNote`, `app.js:150`). The only sign of age is "updated 15:40" inside the `desk-source`
  line, which sits 2,194 pixels down the Live Desk page and is never compared with the clock.
- What happens: if `poll()` fails on every cycle (a schema error, a full disk, a bug in a
  strategy), prices, cards and open trades freeze, no alert is raised, and every indicator on
  screen still reads normal.
- Fix: the desk records `last_poll_ok_at`, `last_poll_error` (type and message, no secrets),
  `consecutive_poll_errors` and `poll_seconds`, and puts them in the snapshot and in
  `/api/health`. The interface computes the age against the wall clock once a second. While the
  market is open and the age is above twice the poll interval plus 10 seconds (the limit of
  U-3), it shows a red banner ("The desk has not updated for 4 minutes. Everything on this page
  is stale.") and dims the cards. The third failure in a row raises one CRITICAL notification
  (Telegram included), and recovery raises one.
- Done when: a test makes `poll` raise three times and finds the fields set and exactly one
  notification; a test of the interface helper shows the banner for a stale payload in session
  and not after the close.

**F-3 (P0). The confidence label and the "backtest" line of an alert are not evidence about the
slot that raised it.**
- Where: `Desk._backtest_evidence` (`desk.py:611`), `confidence` and `strategy_scoreboard`
  (`src/onelot/desk/audit.py:145`, `:183`), called from `_raise_signal` (`desk.py:668`).
- What happens:
  1. The backtest line is the Lab leaderboard row for (strategy, instrument, timeframe). The
     parameters are not part of the key. The row is whatever was swept last, which need not be
     what `config/desk.yaml` runs.
  2. With no backtest and no track record each part scores a neutral 50, and 35% of the score is
     the strategy's own signal strength. Measured: strength 90, no backtest, no track record,
     regime fits gives **67, labelled "high"**. An alert can be called high confidence with no
     evidence at all.
  3. The track-record part is `hit rate × 0.6 + 50 + average R × 50`. Measured: a record of 50%
     winners and 0.00R scores **80**; a losing record of 30% winners and −0.20R still scores
     **58**, above neutral. Any strategy with five archived alerts is lifted by this part.
  4. The track record is keyed by strategy name alone (`self.scoreboard.get(slot.cfg.name)`), so
     one strategy's alerts on every timeframe and market are pooled. It is built from the alert
     archive's outcome estimates, not from the paper book. The Home scoreboard uses the paper
     book. The two can disagree about the same alerts.
  5. The new slot `supertrend_mtf` has no leaderboard row (L-1). It reports no strength on
     purpose, so its alerts will read about 50, "medium", with "no saved backtest".
- Fix: drop the blended score. Give each slot one evidence record tied to its exact settings:
  a `params_hash` over (strategy, timeframe, market, parameters), computed when the desk starts
  or reloads. The record holds the backtest of exactly those settings (trades, result per trade,
  the three periods, real-premium result and coverage for options, the date computed) and the
  paper-book record of that slot and hash (live-grade trades only). The alert shows both as
  plain facts ("Backtest, these settings: 485 trades, −0.1% a trade on real premiums, 54% priced.
  Paper test: 2 trades, 0 won.") or "No backtest for these settings". If a single label is
  wanted, derive it from evidence only: "untested", "negative", "unproven", "supported".
- Done when: changing one parameter of a slot makes its next alert say "No backtest for these
  settings"; no code path can label an alert without evidence as high confidence; tests cover
  the four labels.

**F-4 (P1). Configuration mistakes are logged, not shown.**
- Where: `InstrumentTracker.__init__`, `src/onelot/desk/tracker.py:137` to `:149`.
- What happens: an unknown strategy name is skipped with a log line. Parameters that fail
  validation skip the slot with a log line. A misspelt parameter is neither: the parameter
  models accept unknown keys and drop them (pydantic's default). Measured:
  `STMtfParams(st_mul=9.9)` is accepted and runs with the default `st_mult`. After any of these
  the reload button answers "Desk reloaded: 23 instruments".
- Fix: forbid unknown keys in every `StrategyParams` model (`model_config =
  ConfigDict(extra="forbid")`), and make `load_desk_config` validate every slot before the desk
  uses the file. `reload` returns a list of problems and keeps the running configuration when
  the new one is invalid. The interface shows the list. At start an invalid file stops the
  desk with the same list on screen.
- Done when: a misspelt key, an unknown strategy and an out-of-range value each produce a named
  problem in the API answer and on screen, and the desk keeps running on the old configuration.

**F-5 (P1). Paper trades made on delayed or mixed data are counted with the live ones.**
- Measured: 99 of the 147 paper trades in the book are graded `delayed`: 65 from 29 and 30
  October carry no recorded source, 14 came from Yahoo data about 15 minutes late, and 20 came
  from the Dhan socket and were regraded because the feed mixed bar resolutions. 48 are `live`.
- Where: the Home scoreboard sums every grade (`PaperLedger(book.engine).by_slot()` without
  `grade`, `src/onelot/api/server.py:210`; the ledger already accepts `grade=`).
- Fix: with S-9, raise no alert while the data is older than the alert's validity window. The
  scoreboard and every statistic count live-grade trades; a note gives the number of delayed
  trades left out. The paper-test progress count ("7 of 30") uses the same filter.
- Done when: the scoreboard row of a slot changes when a delayed trade is added only in the
  "left out" note.

**F-6 (P2). The desk reads the wall clock directly in many places.**
- Where: 14 calls of `now_ist()` in `desk.py`; `pd.Timestamp.now(tz=IST) - pd.Timedelta(days=70)`
  in `InstrumentTracker.update_bars` (`tracker.py:185`); `pd.Timestamp.now` in
  `src/onelot/desk/data.py:74`.
- What happens: a replay of past sessions through the real desk needs patches around the clock,
  which is why the end-to-end test of the line trail is still missing (T-1). The 70-day cut also
  silently drops history in a replay of older dates.
- Fix: one injected clock (`self.clock()`) passed down to trackers and the data source; `poll`
  takes `now` as an argument.
- Done when: a recorded session can be replayed through `Desk.poll(now)` with no monkeypatching.

**F-7 (P2). Work done inside the desk lock grows with the watch list.**
- Measured: reading the six-timeframe trend costs 30 to 80 ms per instrument each time a new
  bar arrives, and the daily history 0.10 to 0.15 s per instrument when it is loaded. Both run
  under the desk lock, so with 23 instruments a poll holds the lock for one to two seconds a
  minute and requests that need the lock wait.
- Related: the memo that skips recomputation (`mtf_support`'s fingerprint) looks at the ends of
  the frame; a bar revised in the middle of the frame is not noticed.
- Fix: recompute a timeframe's line only when one of its bars closes (the 60-minute line changes
  once an hour); compute outside the lock and swap the result in; add a revision counter from
  the data layer to the fingerprint.
- Done when: the poll time and the time the lock is held are in `/api/health`, and a test with
  100 instruments keeps the lock under 200 ms.

**F-8 (P2). One instrument, two kinds of bars.**
- What happens: the trend ladder and `supertrend_mtf` read cleaned bars (`data/hygiene.py`). The
  two older slots read the polled bars as they come. In the Lab every strategy gets cleaned bars
  (`src/onelot/lab/runner.py:144`). So on a day the cleaning rules touch, the desk and the Lab
  can disagree for the older slots.
- Also: a slot with `gate: all` stays silent when the daily history is missing or shorter than
  the Supertrend needs, and on the two-minute failsafe feed (no one-minute bars). The card says
  "no reading yet" without the reason.
- Fix: clean once per instrument in the tracker and give every slot the same frame. Each
  timeframe of the ladder reports why it has no reading ("daily: 9 of 15 bars", "1m: failsafe
  feed has no 1-minute bars").

**F-9 (P2). Fields and labels left from the risk governor.** Every alert carries
`"risk_allowed": True` and a `risk_reason` (`desk.py:726`), and the snapshot carries
`risk = self.governor.snapshot()` (`desk.py:2100`). Nothing gates on them (rule 5). They feed the
"Risk governor" block on every card (G-1). Remove them with G-1.

**F-10 (P2). Wrong or noisy messages at the start of the day.** From `logs/engine.log` of
11 November: rate-limit back-off warnings every minute from 09:03 to 09:16; three CRITICAL
"Index feed disagreement" notifications before the open ("INDEX DIVERGENCE 2.00% / 1.31%, limit
0.50%"), raised by the legacy engine comparing two feeds while one still showed yesterday's
close (G-4); a Yahoo failure for USD/INR at 09:38 that leaves a context tile empty. Fix: compare
feeds only when both prices are from the current session; batch the start-up history requests
under the rate limit; show "not available" with the reason on the tile.

**F-11 (P2). The strategy label promises a volume check that an index cannot have.** The cards
of index slots read "Supertrend (tuned) + volume + momentum". An index has no volume, so the
check is skipped (`vol_ok = (rv >= p.rvol_min) | rv.isna()`, `supertrend_vol.py:92`; the desk
and the Lab agree on this). Say so on the card: "volume: not available for an index, skipped".

**F-12 (P3). The snapshot version moves without a new snapshot.** `self.version += 1` appears
in seven places in `desk.py`; only one of them rebuilds the snapshot. Tie the version to the
snapshot so "version changed" always means "content changed".

---

## 5. The paper book and trade management

The paper book (`src/onelot/paper/`) is the most careful part of the code: integer paise, an event
per state change, restart recovery, a ledger. The findings are about duplication and about what a
paper trade can and cannot do.

**P-1 (P1). Stop, trail and exit logic exists four times.**
- Where: `src/onelot/lab/simulator.py` (backtests), `src/onelot/paper/manage.py` (paper trades),
  `Desk._manage_positions` in `desk.py:1065` (positions the owner marked as taken), and
  `src/onelot/trade/guardian.py` (the legacy engine).
- What is shared already: `src/onelot/desk/rules.py` holds the small arithmetic and the
  vocabulary (`ratchet_stop`, `r_multiple`, `session_due`, `exit_priority`, `EXIT_PRECEDENCE`),
  and the paper book has a bar-by-bar step function (`paper.manage.manage_step`). The walk over
  the bars is written separately in each of the four.
- What happens: every rule must be written and tested once per copy. The line trail of
  11 November needed changes in three of them and about 1,800 lines of tests in five files to
  hold them together. Known differences today: a line-trail `STOP_MOVED` event is stamped with a
  bar's open time and an ATR-trail one with a bar's close time; the session-end rule is set in
  three places (`desk.py:649`, `:1126`, `:1606`).
- Fix, step 1 (cheap, do it first): put the parity replay in the gate (T-2), so that a
  difference between the Lab and the paper book fails a test on the day it is introduced.
- Fix, step 2: move `manage_step` to a module that neither `desk` nor `paper` owns (today the
  desk may not import the paper package). The Lab simulator calls it once per bar; the desk's
  taken positions call it with a one-price bar, which keeps their "advise from the live price"
  behaviour. No caller keeps stop, target or trail arithmetic of its own. Measure the Lab's
  speed before and after; a sweep must not become slower by more than a tenth. This absorbs
  story US-025. The legacy guardian goes with G-2.
- Done when: the parity replay (`scripts/mtf_desk_replay.py`) runs in the gate on a small
  stored fixture and the three callers import the one function.

**P-2 (P1). A paper trade cannot live past the session.** Every paper trade ends at `exit_time`
with `SESSION_END`. See S-2 for the story. Backlog story US-027 covers carried stock longs only;
after the depository-charge result of section 3.3 it should be put on hold, and carry for index
options needs a new story (US-041 in section 14).

**P-3 (P0, with D-1). Stock paper trades are closed at a time when the stock no longer trades
continuously.** Measured: 20 stock paper trades in the book closed with `SESSION_END` at 15:15
IST between 29 October and 6 November. Since 3 August 2026 the last one-minute bar of an F&O
stock ends at 15:14 and the closing auction runs after it. The booked exit price is therefore
not a price the owner could have traded at that time. Fix with D-1: the stock exit moves before
the end of continuous trading, or becomes an explicit "exit in the closing auction" rule priced
at the auction close.

**P-4 (P2). Details of the line trail in the paper book.**
- The desk tells the book only the latest level (`_tell_paper_trail`, `desk.py:1782`). If the
  desk was down for some bars and `stop_buffer_atr` is above zero, a higher level reached in the
  gap can be missed, and the paper stop then stands lower than the Lab's would. Fix: after a
  restart, rebuild the ratchet from the closed bars since the trade's `last_bar_ts`.
- With a buffer the reported level can step back while the stop in force never does. The card
  should show the stop in force, and the line as a second number.
- Stamp every `STOP_MOVED` with the close time of the bar that caused it.

**P-5 (P1). Three track records for the same alerts.** The alert archive's outcome estimates
(`audit.strategy_scoreboard`), the paper ledger (Home scoreboard) and the Lab leaderboard each
answer "how has this strategy done". Keep one: the paper ledger by slot and settings, live
grade. The archive keeps the owner's own actions (taken, skipped, expired) and nothing else.
Stories US-011 and US-016 already ask for the analytics and the full ledger view; the trade list
is cut at 200 rows today.

**P-6 (P1). A paper trade does not record the settings that produced it.** The `plan` stored
with a trade holds the alert, the levels and the management rules. It does not hold the
strategy's parameters. The scoreboard groups by (strategy, timeframe, market), so a change of
parameters continues the same row and the same count toward 30 trades. Fix: store `params_hash`
and the parameters with each trade (and each alert); a new hash starts a new scoreboard row and
a new count. Needed by F-3.

**P-7 (P2). Money as floating point outside the paper book.** The manual journal, the risk
governor and the alert archive's `pnl_est` use floats (story US-010). The paper book is correct.

**P-8 (P2). At the current rate the 30-trade review is far away.** Measured from the book: the
index slot `supertrend_vol` 30m has 2 closed trades; the two stock slots have none since
8 November. At the backtest rates the four slots reach 30 closed trades in about 4 months
(index `supertrend_vol`), 9 months (`supertrend_mtf`), 10 months (stock `supertrend_adx`) and
14 months (stock `supertrend_vol`). Show the count and the expected date on the scoreboard, and
see S-7 for what the review can conclude.

---

## 6. The Lab

The Lab (`src/onelot/lab/`) runs backtests, sweeps and an optimiser, and stores their results.

**L-1 (P1). The Lab cannot drive `supertrend_mtf`.**
- What happens: the parameter form is built from each field's type. The text settings `gate`,
  `trend_tf`, `trail_tf` and `entry_end` appear as free-text boxes; a typing mistake is a
  validation error after the run starts. `trend_tf` and `trail_tf` accept `60m` and refuse `1h`
  and `2h`, which the timeframe selector offers elsewhere. The strategy is absent from `SPACES`
  (`src/onelot/lab/research.py:43`, used at `:470`), so sweeps skip it and the leaderboard never
  has a row for it (F-3).
- Fix: `param_docs` (`src/onelot/strategies/base.py:168`) reports the allowed values of a
  `Literal` field, and the form renders a select. One table of timeframe names and aliases is
  shared by the Lab, the desk and the strategies. `supertrend_mtf` gets a `SPACES` entry made of
  the structures section 3 found worth watching, with `st_period` and `st_mult` fixed.
- Done when: the strategy can be run, swept and compared from the Lab without typing a setting
  name or value.

**L-2 (P1). Optimize ignores the costs set on the form and would tune the owner's fixed
values.** `/api/lab/optimize` (`src/onelot/api/server.py:507`) does not pass the form's
slippage, cost stress or sentiment gate to the runs, so an optimised result is not comparable
with a single run of the same settings. Its search space comes from the numeric ranges of the
parameter model, so for `supertrend_mtf` it would vary `st_period` and `st_mult` (the owner fixed
14 and 1.7) and cannot vary a text setting. Trials are clamped to 5 to 300. Fix: the optimiser
takes the same cost inputs as a single run; a parameter can be marked `locked` in the model and
is then shown but never varied; categorical settings are searchable.

**L-3 (P1). Option backtests use modelled premiums unless real ones are asked for.** See S-3 for
the evidence. In the code: the model prices the option from the index and India VIX of the same
day (a small look-ahead: section 3.4 shows +4.10% falling to +2.91% with the previous day's VIX);
time decay runs on calendar time; story US-031 (the underlying is slipped before the option is
priced) is still open; the Lab's help text still says "until the Dhan option-chain history is
wired", which is no longer true. Fix: real premiums first, the model only for trades that cannot
be priced, the share of each printed on the result, the previous day's VIX in the model, and
US-031.

**L-4 (P1). The cost table is unverified and misses two charges.**
- `config/costs.yaml` says `verified_against_contract_note: false` (the app shows a permanent
  warning for it; story US-021).
- The Live Desk's panel "Charges on your own fills" compares the table with the broker's own
  trade book: delivery buys were billed 0.116% against 0.154% in the table, delivery sells
  0.101% against 0.139%, and one intraday buy 0.152% against 0.042%.
- The table charges brokerage on delivery trades (`equity_delivery`: the lower of ₹20 and
  0.03%). Dhan's published delivery brokerage is zero (to verify on a contract note).
- There is no depository charge: ₹12.50 plus GST, ₹14.75, per stock per day sold from the demat
  account. At one share this one charge decides whether a carried stock trade can pay (section
  3.3).
- The Lab's slippage is 0.03% a side for stocks (measured half-spread 0.005%) and 0.6% plus two
  ticks for every option (measured 0.17% for NIFTY calls to 24% for SENSEX puts, S-6).
- Fix: complete US-021 against one real contract note for each product; add the depository
  charge as a per-sale fixed cost on delivery sells; read slippage per instrument from the
  measured table, with the model value as a floor.

**L-5 (P2). A Lab verdict is given without the things that make it honest.** `verdictOf`
(`src/onelot/ui/app.js:1100`) grades a run on trade count, profit factor and expectancy. It does
not show the three periods, the no-signal benchmark, the t-statistic, the number of settings
tried before this one, or (for options) the priced share. Add them to the run report and to the
verdict rule: no "POSITIVE" unless every period is positive after costs and the result beats
the benchmark. See S-11 and S-12.

**L-6 (P2). There is no path from a Lab result to the desk.** "Apply best params to the form"
(`app.js:1170`) fills the Lab form. Moving a setting to `config/desk.yaml` is done by hand, and
the evidence does not travel with it. Add "Send to paper test": it writes the slot (strategy,
timeframe, market, parameters) into the desk configuration after showing the difference, stores
the evidence record under the slot's `params_hash` (F-3), and asks for a reload.

**L-7 (P2). The form turns a zero into the default.** `labBody` (`app.js:1040`) reads
`parseFloat(value) || 0.03` for slippage and `|| 1` for the cost multiplier, so a run asked for
with zero slippage runs with 0.03% and reports it as asked. Use an explicit empty check.

**L-8 (P2). Payloads and polling.** Measured: `/api/lab/matrix` returns 1.83 MB,
`/api/lab/leaderboard` 212 KB in 1.45 s, `/api/lab/runs` 117 KB; the page asks for job status
every 2 seconds (`startPoll`, `app.js:1067`). Page the lists on the server, send the matrix as
compact arrays, and report job progress over the existing stream.

**L-9 (P2). Lab jobs run inside the live desk's process.** `src/onelot/lab/jobs.py:17` uses a
thread pool. A sweep during market hours competes with the desk loop for the same interpreter
and the same database file (to verify by timing a poll during a sweep). A restart marks running
jobs as failed (`recover_interrupted`, `jobs.py:95`) and they must be started again by hand. Run
jobs in a separate worker process with its own database connection, and make a sweep resumable
from its finished runs.

**L-10 (P2). The research of 11 November lives in eight scripts of about 4,000 lines.**
`scripts/mtf_bruteforce.py` (1,275 lines), `mtf_stage2.py`, `mtf_robust.py`,
`mtf_real_premiums.py`, `mtf_desk_replay.py`, `mtf_lab_check.py`, `deep_backtest_candidates.py`
and `bruteforce_analysis.py` share a fast harness that was checked against the product code
(section 2.3) but has no tests of its own. Move the harness into `src/onelot/lab/` as a tested
module (structure grid, period split, benchmark, t-statistic, real-premium join) so that the
next study is a configuration, and keep the scripts as thin callers.

**L-11 (P3). Small things in the Lab.** `available_resolutions` runs a `GROUP BY` over the bars
table on every call; cache it with the coverage table. The Lab's instrument and strategy selects
are empty until their requests return, with no loading state (U-22).

---

## 7. Data

The store is one SQLite file of about 12 GB (`data/onelot.db`) with one-minute and daily bars for
five indices and the stocks, expired option prices, and every table of the app.

**D-1 (P0). The session calendar is out of date.**
- Measured in the store: every F&O stock has 375 one-minute bars a day up to 31 July 2026 and
  360 a day, ending with the 15:14 bar, from 3 August 2026. The daily close (set in the closing
  auction) now differs from the last one-minute close by 0.33% on average (1.96% at most),
  against 0.13% before.
- Cause: the Closing Auction Session. From 3 August 2026 continuous trading in F&O stocks ends
  at 15:15, the closing price is found in an auction after it, and derivatives trade to 15:40.
  The pre-open session changed on 7 September 2026. The exact times must be checked against the
  exchange circulars before coding (sources in Appendix C).
- Where the old times live: `EQUITY_SESSION` in `src/onelot/data/universe.py:27` (close 15:30,
  exit 15:15, last entry 14:45); `config/universe.yaml:58`; the `exit_time` defaults of "15:15"
  in `desk.py:649`, `:1126`, `:1606` and `tracker.py:144`; `/api/calendar`
  (`src/onelot/api/server.py:914`); the header clock (`tickClock`, `app.js:66`, which counts
  minutes 555 to 930); the Lab simulator, which ends a stock trade "at the end of the day" on
  the last bar it finds (`src/onelot/lab/simulator.py`).
- What happens today: a stock alert can be raised up to 14:45 and its paper trade is "exited" at
  15:15, the first minute in which the stock no longer trades continuously (P-3). Backtests
  before and after 3 August end the day at different times without saying so. The header shows
  the market open until 15:30 for every instrument.
- Fix: one dated session calendar, per instrument class: for each date range, the pre-open, the
  continuous session, the closing auction and the derivative session. Everything above reads it.
  Stock slots exit at the last continuous minute minus a margin (for example 15:10), or by an
  explicit auction rule priced at the daily close. Index option slots follow the derivative
  session. The calendar is data (`config/sessions.yaml`), with a test that compares it with the
  first and last stored bar of each instrument on a sample of dates.
- Done when: that test passes on the store, no time of day is written in code, and a stock paper
  trade can no longer close at a minute with no continuous trading.

**D-2 (P1). The store is not kept up to date.** Stored bars end on 6 November 2026. The only
writer of stored bars is the manual backfill (`save_bars`, `src/onelot/data/backfill.py:116`).
The desk fetches its own 25 days in memory each morning and stores none of it. So every backtest
is stale by the time since the last manual backfill, and the days the paper test trades are not
in the store to check it against. Fix: an end-of-day job (after the close, and at start for any
missing days) appends the day's one-minute and daily bars from Dhan for every instrument and
writes a gap report. The Data tab shows "store complete to <date>" per instrument.

**D-3 (P1). Backfill errors are reduced to a number.** Five of the six stored backfill jobs
ended `DONE_WITH_ERRORS` ("4136608 rows; 3 errors", "1851223 rows; 12 errors", and so on). The
screen shows the count. Which instrument and which dates failed is not kept. Story US-019
(resumable backfill) should store one row per failed request (instrument, range, error) and
offer "retry the failures".

**D-4 (P1). Known defects in the stored data are repaired only when bars are read.**
`src/onelot/data/hygiene.py` drops exchange mock-trading sessions and a flat Muhurat session,
rescales two demergers the vendor left unadjusted (ITC on 6 January 2025, Tata Motors on
14 October 2025), and rebuilds daily bars where the daily and one-minute series disagree. The
database is unchanged, so every reader that does not call the module sees the raw defects: the
Data tab's quality report, the exploratory charts, the correlation view, and the older slots on
the desk (F-8). The list of dates is in code. Fix: a table of corporate actions and excluded
sessions (data, with source and date added); adjusted bars are either written once with an
audit row, or served through one read function that every reader must use. The quality report
flags an unexplained overnight jump as a possible corporate action for the owner to confirm.

**D-5 (P2). One series can hold bars from two vendors.** Daily bars from Yahoo and one-minute
bars from Dhan disagree on some days; `load_bars(..., "1d")` lets the stored daily series win
when it has more rows; a series that has only daily bars skips the junk-session filter. Story
US-029 (fallback bars never pollute a Dhan series) covers the desk side. Store the vendor with
every bar range and never mix vendors inside one indicator window.

**D-6 (P2). Slow reads on the Data tab.** `/api/data/quality` takes 33 seconds on the live
database with no progress shown, and it competes with the desk for the file. The coverage table
has 155 rows with no paging. Compute the quality report in the end-of-day job and serve the
stored result.

**D-7 (P2). A time-zone trap in the store.** `src/onelot/data/store.py:49` concatenates frames
whose indexes can carry two different time-zone implementations (pytz and zoneinfo), which
raises in some pandas versions. Normalise to UTC on write and convert once on read.

**D-8 (P2). One 12 GB file holds everything, with no migrations and no backup.** A damaged file
loses the bars, the paper book and the journal together. Story US-009 covers migrations and
backups. Consider two files: market data (large, can be rebuilt) and the book (small, cannot).

**D-9 (P2). The holiday file has no exchange list for 2027.** `config/nse_holidays.yaml` holds
only the fixed-date national holidays for 2027, with a note to re-check in December. The app
warns at start that the calendar is unverified (`src/onelot/clock.py:75`). Add the 2027 circular
before 1 January and make the start-up warning name the first date not covered.

**D-10 (P1). Expired option prices are stored only near the money.** At the money plus or minus
ten strikes, by the day's level. A carried trade whose index moves away loses its price series
(section 3.5). See S-3.

---

## 8. API and security

The app runs on the owner's laptop and listens on the loopback address by default. The `--lan`
flag makes it listen on every network interface so that a phone can open it. What can leak or be
changed: the owner's broker account data (balances, holdings, ledger, trade book), the Dhan
credentials (reads show them masked, but they can be overwritten), and the desk's own state. The
app cannot place an order (rule 1), which limits the worst case: nobody can trade through it.

The API has 65 routes in one function (`create_app`, `src/onelot/api/server.py:221`). Measured:
all 39 GET routes are open; the 25 routes that change something require the token; one POST is
open.

**A-1 (P1). Anyone who can reach the port can fetch the API token, and every read is open.**
- Where: `GET /api/session` returns `{"token": ...}` with no check (`server.py:909`). The page
  itself carries the token (`html.replace("__API_TOKEN__", api_token)`, `server.py:987`). Both
  are open GET routes.
- What happens: the token is meant to protect the routes that change something. Since any
  caller can read it first, it protects nothing. With `--lan`, anyone on the same network can
  read the account data and the paper book, and can then post new broker credentials, reload
  the desk or mark alerts as taken. The start-up warning says "mutations still need the API
  token" (`src/onelot/__main__.py:178`), which is true in form only.
- On loopback the risk is smaller but not nil: there is no check of the `Host` header, so a web
  page open in the owner's browser can reach the app by DNS rebinding and read the token the
  same way.
- Fix: story US-005 as written (passcode login off loopback, an HttpOnly SameSite=Strict
  cookie, secret-writing routes on loopback only), plus four points it does not yet have:
  1. Remove `/api/session`. The page gets a session cookie when it loads and the script never
     holds a token.
  2. Refuse any request whose `Host` header is not on an allow-list (`localhost`, `127.0.0.1`,
     `[::1]`, and a LAN name the owner configures).
  3. Until the story is done, `--lan` refuses to start.
  4. Count the routes in the story again: 39 open GET routes today, not 37.
- Done when: a test walks every registered route and, for a server bound off loopback, gets 401
  without a session on all but the page, the static files, the login and one liveness probe;
  no response body contains the token; a request with a foreign `Host` gets 400.

**A-2 (P2). A GET can make the app call the broker.** `GET /api/account?refresh=1`
(`server.py:434`) refreshes the account from Dhan. A read must have no side effect: any page
can fire a GET at a local address. Make the refresh a POST behind the session.

**A-3 (P2). An open POST writes whatever it is sent into the event log.** `POST
/api/dhan/postback` (`server.py:969`) stores the JSON body of any caller as an event row, with
no size limit. Its own docstring says nothing should ever arrive, since the app places no
orders. Remove the route.

**A-4 (P2). Details of the token check.** `require_token` (`server.py:230`) accepts the token
in the query string (`?token=`), where it ends up in browser history and logs; it compares with
`!=` instead of a constant-time comparison; there is no CORS policy and no security header.
With A-1 these go away with the token. If a token stays for scripts, accept it in a header
only and compare with `hmac.compare_digest`.

**A-5 (P2). Text from outside reaches the page as HTML.**
- Where: `esc` (`src/onelot/ui/app.js:14`) escapes `&`, `<`, `>` and `"`, and not the single
  quote. The script has 98 `innerHTML` writes. `loadSettings` (`app.js:1455`) puts the masked
  credentials into the page through `kv()` without `esc`. Text that comes from outside the app
  (news headlines, broker error messages, instrument names) is drawn the same way.
- Fix: `esc` covers the single quote; one `html` template helper escapes every value unless it
  is marked as trusted markup; the server sends a Content-Security-Policy header
  (`default-src 'self'`, no inline script), which also forces the token out of the page (A-1).
- Done when: a node test feeds `<img src=x onerror=...>` and a quote through every renderer
  that takes outside text and finds it as text; the page loads clean under the policy.

**A-6 (P2). Bad input gives a 500.** `dt.datetime.fromisoformat(body.ts)` in the routes that
add a news item and a sentiment score (`server.py:730`, `:784`) is not guarded, so a malformed
time answers "Internal Server Error". Declare the field as a datetime in the request model so
that the framework answers 422 with the reason.

**A-7 (P2). Two read routes do their filtering after the limit.** `GET /api/news`
(`server.py:708`) takes the newest N rows and then filters by symbol in Python, so a symbol
with few items can come back empty although older items exist. `/api/sentiment/latest` reads
2,000 rows to answer with a handful. Filter in SQL and add the index.

**A-8 (P2). The stream sends the legacy engine's state every second and replays old
notifications.** Measured on `/api/stream`: the first frame is 81,705 bytes (65,807 of them
the desk); afterwards about 3.5 KB of legacy state every second; the desk part is sent only
when its version changes (which is right). The first frame also carries the last eight
notifications, and the page shows them again as toasts on every load (U-1). Fix: typed events
(`desk`, `paper`, `notification`, `heartbeat`); each notification has an id and the page
remembers the last id it showed; the heartbeat carries the server time and the desk's last
good poll (F-2); the legacy state leaves the stream (G-2).

**A-9 (P2). The API has no declared responses.** The 65 routes return plain dictionaries
(`response_model` is used nowhere), so nothing checks that a field the page reads is still
sent. The trend strip shows this today: with the old process running the page finds no `trend`
field and prints "no reading yet". Declare response models for `/api/desk`, `/api/paper` and
the stream events, and add a test that the fields `app.js` reads exist in them.

**A-10 (P3). Small things.** The static route has no media type for `.html`, `.ico` or `.map`
files, and `favicon.ico` answers 404. `/api/docs` is open; put it behind the session off
loopback. The static route's check against paths outside the interface folder is correct.

---

## 9. The legacy Bank Nifty option engine

The app began as a Bank Nifty option-signal engine. The desk was added beside it and the engine
was "folded underneath". It still runs.

**G-1 (P1). Every card and the header show a risk governor that governs nothing.**
- Where: the "Risk governor" blocks in `app.js:793` and `:922`; `risk =
  self.governor.snapshot()` in `desk.py:2100`; the header's risk badge.
- What happens: on 11 November every card read "Trades (all) 0/2 · Open (all) 0/1 · Realised
  ₹0", and the limits listed a daily loss of ₹6,000. On that day two paper trades were open and
  lost ₹20,579. The numbers describe the legacy engine's own trades, of which there are none.
  By rule 5 no governor gates the desk's alerts, so the block can only mislead.
- Fix: remove the block, the badge and the `risk_allowed` fields (F-9). Put the paper book's
  own figures in their place (U-15): open paper trades, the loss if every open stop is hit,
  today's realised result.
- Done when: the words "Risk governor" appear nowhere on the Home and Live Desk tabs, and the
  figures shown equal the paper ledger's in a test.

**G-2 (P1). Two applications share one class and one process.**
- Where: `src/onelot/engine.py` (1,263 lines). The class `Engine` is the application host
  (broker and credentials, token renewal, notifications, the paper book, starting the desk) and
  also the legacy engine (`_evaluate`, `_emit_signal`, `_guardian_loop`, `_manage`,
  `acknowledge`, `close_position`, `panic_halt`).
- Measured on 11 November: the legacy engine evaluated 68 times and raised no signal; it sent
  about 3.5 KB of state to every open page each second; it raised three CRITICAL notifications
  before the open (G-4); it set the health state to DEGRADED at the close; and it owns four of
  the seven header badges.
- Code that only the legacy engine uses (found by imports): `src/onelot/strategy/` (1,226
  lines), `src/onelot/trade/` (1,093), `src/onelot/risk/` (352), `src/onelot/replay/` (502,
  imported by nothing else in `src/onelot`), and about half of `engine.py`. Shared and to be
  kept: `market/` (the desk's data source uses it), `analytics/` (option pricing for the paper
  book and the Lab), `store/` (the database layer). `cost_preview.py` imports `strategy/` and
  must be checked.
- Fix: this is the owner's decision (Q-3). Recommended: split `Engine` into an
  application host and a legacy engine, switch the legacy engine off by default
  (`legacy_engine.enabled: false`), and remove its section, its badges and its stream state
  from the page when it is off. Delete the code in a later story once a release has run
  without it; a git tag keeps it reachable.
- Done when: with the switch off, no thread of the legacy engine starts, the page has no
  "Bank Nifty option engine" section, the gate passes, and the desk and the paper book behave
  the same in the parity replay.

**G-3 (P1). "Halt" does not halt the desk.** The Halt button asks "Halt trading? No new signals
will fire until you clear it." (`app.js:903`) and calls `/api/risk/halt` (`server.py:324`),
which sets the legacy engine's halt. The desk never reads it (the word "halt" does not occur
in `desk.py`), so its alerts continue. Fix: remove Halt and Reset with G-2. If the owner wants
a switch of their own (Q-13), make it "Pause alerts": the desk keeps tracking and
showing, raises no alert until resumed, shows a banner while paused, and keeps the state
across a restart. A pause by the owner is not a risk governor, but it does stop alerts, so the
owner should confirm it fits rule 5.

**G-4 (P2). False alarms from the legacy checks.** Three CRITICAL "Index feed disagreement"
notifications before the open on 11 November (the two feeds were compared while one still
held the previous close: 2.00% and 1.31% against a limit of 0.50%). "Health HEALTHY ->
DEGRADED: market is closed" at 15:30 every day: a closed market is not a degraded system.
`consecutive_api_errors: 3` in `/api/health` at the end of the day with no explanation on
screen. If the engine stays, compare feeds only inside the session and give "closed" its own
state.

**G-5 (P2). The legacy panels are rebuilt every second.** `renderSignal` (`app.js:486`) and
the legacy positions table are rebuilt on every stream frame, which wipes anything typed into
their exit-price boxes. The desk's own rendering is already throttled and compares rows
before touching them. Goes away with G-2; otherwise render them the same way.

**G-6 (P3). Old names.** `scripts/start_mac.sh` still introduces itself as "Bank Nifty Trade
Assistant"; the session's own working folder and several documents (`docs/00` to `docs/13`)
describe the engine. See O-6.

---

## 10. Operations and documents

**O-1 (P1). The app runs in a terminal window, with nothing to restart it.**
- Where: the desk is started by hand in a terminal, with `nohup` and the command of section
  2.4 (`scripts/start_mac.sh` would end with `exec python -m onelot`, but it fails, see
  O-10). A single-instance lock exists (`SingleInstanceLock`, `engine.py:80`) and the engine
  log rotates. Nothing starts the app at login, restarts it after a crash, or tells the owner
  that it stopped.
- What happens: the paper test needs the desk to run every session for months (P-8). A closed
  lid, a crash or a forgotten start loses a day of alerts without a trace, and the scoreboard
  cannot tell "no signal" from "not running".
- Fix: story US-006. On macOS a launchd agent with `KeepAlive`, started at login; the desk
  records each session it covered (first and last good poll) so that the scoreboard can show
  "sessions covered: 18 of 21"; a notification on every start that follows an unclean stop.
- Done when: killing the process brings it back within 30 seconds with open paper trades
  recovered, and a day the desk did not run is visible on the scoreboard.

**O-2 (P1). The Dhan token runs out before the session and the app says so only in a badge.**
- What happens: the token lasts about a day. The one in use runs out at 06:29 on 12 November,
  before the 09:15 open. The header shows "token Nh left" and turns amber under two hours.
  After that the desk falls back to delayed data (S-9). Self-renewal exists in the code
  (`_maybe_auto_renew`, `engine.py:568`) and needs a TOTP secret in `.env`, which is not set.
- Fix: a check at start and at 08:45 on every trading day: if the token ends before the end of
  today's session, raise a red banner and one notification with the time in IST ("The Dhan
  token ends at 06:29, before today's session. Renew it in Settings."). Whether to store the
  TOTP secret for self-renewal is the owner's decision (Q-12).
- Done when: a test with a token that ends at 06:29 produces the banner and exactly one
  notification, and none when the token covers the session.

**O-3 (P1). An alert reaches the owner only at the laptop.** An alert is valid for ten minutes.
The channels today are the open page (a toast and a beep) and a macOS notification. Telegram is
built (`src/onelot/notify/telegram*.py`, 1,876 lines) and not configured: `.env` has no bot
token and the outbox table is empty. Until it is set up (the owner creates the bot with
@BotFather and puts the token in `.env`), the stall, token and alert messages of this review
have nowhere to go when the owner is away. Stories US-024 and US-013 cover the settings and
the buttons.

**O-4 (P1). The gate runs only by hand, and builds are not reproducible.** There is no
`.github/workflows` folder; `scripts/gate.sh` says "CI will run the same script (US-008)".
`requirements.txt` gives version ranges (`pandas>=2.2,<3`) and there is no lock file, so the
same commit can install different libraries on another day. Fix with US-008: a lock file, and
a workflow that runs the gate on every push. The gate needs no secret.

**O-5 (P1). The work of 11 November exists only in one working folder.** 26 new files are
staged and 16 changed files are unstaged, about 10,900 lines, with no commit. The research
outputs that back section 3 are under `data/research/`, which git ignores. Commit when the
owner asks (rule 9). Suggested commits, in order: (1) data hygiene and the trend ladder with
their tests; (2) the strategy and the line trail in the Lab; (3) the paper book and desk
wiring; (4) the interface; (5) the research scripts and `docs/research/`; (6) this review. The
written results are in `docs/research/SUPERTREND_MTF_2026-11-11.md`, which is not yet added
to git, and neither is this file.

**O-6 (P2). Documents describe an earlier app.** `README.md` says "10 stocks" (there are 18),
"15 instruments, 45 strategy slots" (23 and 46), that the Live Desk "runs on Yahoo bars", and
twice that option backtests are synthetic "until Dhan expired-options history is wired" (it
is: `src/onelot/lab/real_options.py`). The Lab's help text says the same. Fix with US-022:
one operator runbook (start, stop, restart against reload, token renewal, backfill, what each
banner means, what to do after a crash); a test that compares the counts in the README with
the configuration.

**O-7 (P2). The app cannot tell the owner how it is doing.** There is no figure for the poll
duration, the time the desk lock is held, the feed's delay, the database size or the last
backup. The server writes no access log (`access_log=False`, `__main__.py:185`). The code has
145 `except Exception` handlers (24 in `desk.py`, 20 in `engine.py`, 17 in `paper/book.py`),
most of which log and continue. Fix with US-007: every handler that swallows an error counts
it under a name; a "System" panel shows the counts and the figures above (U-3).

**O-8 (P2). Start-up is noisy and close to the broker's rate limit.** The log of 11 November
has a rate-limit back-off warning every minute from 09:03 to 09:16 while 23 instruments
loaded 25 days of history each. Load the history from the store (D-2) and ask the broker only
for the missing days.

**O-9 (P3). Old log files are never removed.** `logs/` holds ten files from different stages
of the project (the largest 10.5 MB). Keep the rotating engine log and the current output
file; remove the rest at start when older than 30 days.

**O-10 (P1). The macOS start script cannot start the app.**
- Where: `scripts/start_mac.sh` ends with `exec "$VENV/bin/python" -m onelot --port "$PORT"`
  and names no command. `main()` (`src/onelot/__main__.py:477`) puts `run` in front only when
  every argument starts with a dash, and the port number does not. The script has been like
  this since the first commit of the unified project (27 October 2026). The Windows launchers
  (`scripts/start_win.bat`, `scripts/start_win.ps1`) pass `run` and are not affected.
- What happens: every call of the script, with or without `--mock` or `--lan`, ends with
  `argument cmd: invalid choice: '8787'` right after it prints "starting the engine". This
  was checked against the parser alone, so nothing was started. `README.md` (lines 61 to 63),
  `QUICKSTART.md` and `docs/04_OPERATIONS.md` give this script as the way to start the app.
  The running desk was started with the direct command of section 2.4, which works. The same
  rule breaks the two top-level options when no command is given: `--log-level DEBUG` reaches
  `cmd_run` without the options of `run` and fails on `args.lan` (read from the code, not
  run), and `--log-level=DEBUG` is refused by the parser.
- Fix: with US-006, which builds on the start command. Pass `run` in the script. In `main()`,
  treat a command line that names no command as `run` whatever its options are, and take the
  command names from the parser instead of the hand-kept list, which already lacks `sweep`
  and `leaderboard`.
- Done when: a test takes the arguments after `-m onelot` from each of the three launchers,
  with and without `--mock` and `--lan`, parses them with `build_parser()` and gets the `run`
  command and the port; `python -m onelot --port 8787` and
  `python -m onelot --log-level DEBUG` both start the desk.

**O-11 (P1). A token renewed in Settings does not reach the desk's bar source.**
- Where: a save in Settings runs `Engine._apply_credentials` (`engine.py:409`), which builds a
  new account adapter and gives the engine's own broker the new token. The desk's bar source
  (`DhanBars`, `desk/data.py:132`) keeps the adapter object it was built with, and
  `Desk._maybe_switch_source` (`desk/desk.py:560`) builds a source only when there is none.
- What happens: seen live on 12 November 2026. The owner renewed the token at 06:34. The
  socket feed and the option chain recovered at once. The desk's bar requests kept failing
  with HTTP 401 (06:40, 06:45, 06:50, 07:19), the desk showed delayed data, and only the
  restart at 07:30 cleared it. A renewal during a session would leave the desk on delayed
  bars until someone restarts it, with a valid token in `.env`.
- Fix: with US-040. The bar source asks for the current adapter at each fetch (a getter, as
  the option-chain reads already do), or `_apply_credentials` tells the desk to rebuild its
  source.
- Done when: a test builds the desk with one fake adapter, saves new credentials, and sees
  the next bar fetch go through the new adapter; `source.last_error` is empty after it.

---

## 11. The user interface

The interface is one page with six tabs: Home, Live Desk, Backtest Lab, Data, News & Sentiment and
Settings. It is `src/onelot/ui/index.html` (443 lines), `app.js` (1,527 lines), `charts.js` (128)
and `styles.css` (432), with no build step and no framework. The fonts and the chart library are
served by the app itself, so the page works without the internet. That simplicity should stay.

What works and must be kept (rule 8):

- The bright Day theme with a Night toggle, the 17 px base size and the house style.
- Numbers in a monospaced face with Indian digit grouping.
- The desk's throttled drawing, which compares a card with what is on screen before touching it
  (`renderDesk`, `app.js:820`).
- The paper view switch (Today, Last session, All), the one control on the page built as a proper
  keyboard widget (`showPaperView`, `app.js:743`).
- Choices remembered in local storage: theme, tab, open cards, paper view.

How it was examined: the source files were read in full. Each tab was loaded in a headless browser
at 1440×900 (laptop) and 390×844 (phone) against the running desk, and positions, heights and font
sizes were measured by script (Appendix B). The Settings tab was read in source only, and the
account panels were looked at for layout only, because they hold the owner's own data.

The main finding: the page is organised around what the app knows, not around what the owner must
see during a session. In a session the owner has four questions:

1. Is the desk alive and on live data?
2. Is there an alert to act on, and for how long?
3. What is open, where is its stop, and how is it doing?
4. How is the paper test going?

Today the first answer is spread over seven badges, the footer and a line 2.4 screens down the
Live Desk. The second sits in the last 102 px of the first screen on a laptop and 2.5 screens down
on a phone. The third is not on screen at all for paper trades. The fourth is a table without the
count that matters. Section 11.1 gives the layout to build. The findings after it give the
evidence and the detail.

### 11.1 The layout to build

1. **One header row**: brand, the six tabs, one status pill, the clock with "IST", the theme
   button. At most 72 px high at 1440 px wide and 112 px on a phone (today 213 and 476). The line
   "signal-only · places no orders · you execute" moves to the footer.
2. **One status pill** in place of the seven badges (U-3). A click opens a "System" panel.
3. **Banners under the header, only when something needs the owner**: connection lost, desk
   stalled, token ends before the session does, data delayed, configuration error (F-4), alerts
   paused. One line each, with the IST time and what to do about it.
4. **Home is the session screen**, in the owner's order (My portfolio, Alerts, Monitoring beside
   the Strategy scoreboard, Archive, alert feed, then ledger, journal, profile). Three changes
   keep that order: "My portfolio" becomes one compact row with "show all" (an owner decision,
   Q-14); a pending alert is shown open, with its buttons; "Monitoring" lists every open
   paper trade as well as the alerts the owner took, with stop, last price, unrealised result,
   stop moves and the risk line (U-14, U-15).
5. **Live Desk is the instruments screen**: pending alerts, open trades (the same component as on
   Home), Indices beside Stocks, Instrument focus, today's closed paper trades. "Market context"
   becomes a one-line strip at the top. "Fill costs" moves to a "Costs" sub-tab. The legacy
   section goes with G-2.
6. **One formatter for every time on the page**, and every time says IST (U-5).
7. **Nothing below 13 px.** The owner's scale stays: 17 px text, 14.5 px tables, 26 px tiles.
8. **Phone**: one column in the same order, cards in place of wide tables for alerts and open
   trades, tap targets at least 44 px.

### 11.2 Header, status and time

**U-1 (P1). Old notifications are replayed as toasts and beeps every time the page loads.**
- Where: `renderFeed` (`app.js:580`) and the first frame of the stream. The first frame carries
  the last eight notifications. A `SIGNAL` beeps and raises a toast (`app.js:589`); an `EXIT` or a
  `CRITICAL` raises a toast (`app.js:590`).
- Measured: seven toasts on every load of the page on 11 November. By the code the same happens
  after every reconnect (the page reconnects 3 seconds after a stream error, `app.js:899`), and
  the feed lists then hold the same lines twice.
- What happens: the owner hears the alert sound for an alert that is hours old, and for six
  seconds the toasts cover the header badges (U-27). A sound that fires for old news stops being
  trusted for new news.
- Fix: every notification gets an id and a time (A-8). The page keeps the highest id it has
  announced, in local storage. Anything at or below that id goes into the feed silently. The feed
  is keyed by id, so a reconnect cannot double it. Nothing older than 60 seconds beeps.
- Done when: a node test feeds the same first frame twice and counts each toast and each beep
  once; a notification stamped five minutes ago appears in the feed with no sound.

**U-2 (P1). The header takes a quarter of the screen, and most of its badges describe the legacy
engine.**
- Where: `index.html:52` to `:75`; `.top` wraps (`styles.css:51`, `flex-wrap:wrap`);
  `renderBadges` (`app.js:151`).
- Measured: the header is 213 px high at 1440×900 (24% of the screen) and 476 px at 390×844
  (56%). With one start-up warning under it, the first content of Home starts at y = 336 px on
  the laptop and at y = 633 px on the phone (75% of the first screen).
- What happens: seven badges are shown, in this order: mode, feed, desk source, health, risk,
  "entries closed", token. Four of them (feed, health, risk, "entries closed") describe the
  legacy Bank Nifty engine, and the mode badge is read from its status as well. Only "desk" and
  "token" are about the desk the owner uses. A green "HEALTHY" says nothing about whether the
  desk is polling (F-2), and "entries closed" is the legacy engine's rule, not the desk's.
- Fix: the one-row header of section 11.1. The badges become the status pill (U-3). Start-up
  warnings move into the System panel unless one needs action today.
- Done when: a browser test (T-3) measures a header of at most 72 px at 1440×900 and at most
  112 px at 390×844, with every tab reachable.

**U-3 (P1). One status pill and a "System" panel.**
- Where: new; replaces `renderBadges` (`app.js:151`), the `desk-source` line and the footer's
  connection word.
- The pill shows exactly one state, the first of these that is true:
  1. DISCONNECTED: no stream frame for 5 seconds.
  2. STALLED: the market is open and the desk's last good poll is older than twice the poll
     interval plus 10 seconds (F-2).
  3. TOKEN: the Dhan token has ended, or ends before today's session does (O-2).
  4. DELAYED: some instruments are on delayed data; the pill names how many. Amber.
  5. PAUSED: alerts are paused, if the owner accepts G-3 (Q-13).
  6. LIVE: "LIVE · 4 s", the age of the last good poll.
  7. CLOSED: "CLOSED · opens Thu 09:15 IST".
- The age counts up in the page from the server's heartbeat (A-8), so a server that goes silent
  turns the pill red by itself. Every state has a word as well as a colour (U-27).
- The panel behind the pill holds what the owner needs once a day, not once a second: the last
  good poll and how long it took; source, delay and socket state of each feed; the token's end
  in IST and the time left; the start time and code version of the process; sessions covered
  this month (O-1); swallowed errors by name (O-7); database size and last backup (D-8); the
  start-up warnings.
- Why the code version: on 11 November the process started at 06:42 ran the old code all day
  after the new code was written, and nothing on screen said so (section 2.4).
- Done when: a test with a fake clock produces each of the seven states from a payload, in that
  order of priority; stopping the server turns the pill to DISCONNECTED within 5 seconds.

**U-4 (P1). The connection state sits in the footer, and drawing errors are swallowed.**
- Where: `connect` (`app.js:897` to `:901`); `#conn` in the footer (`index.html:436`).
- What happens: when the stream drops, the word "reconnecting…" appears at the bottom of a page
  that is 4.9 to 6 screens long, while every price and card above it stays as it was. The handler
  is `try { applyStream(JSON.parse(e.data)); } catch (err) {}`: an error in any drawing function
  is discarded, the rest of that frame is not drawn, and the footer still says "live" because
  the word is written before the drawing starts.
- Fix: the connection state goes into the pill (U-3). A banner says "Connection to the desk lost
  at 10:42:07 IST. The figures below are from that time." and a `stale` class greys every live
  figure. Each renderer runs in its own `try`, so one broken card cannot stop the others. A
  drawing error shows one banner per distinct error, is written to the console, and is posted
  once to a small client-error route so that it reaches the log (O-7).
- Done when: a node test with one card that throws finds the other cards drawn and the banner
  shown; the banner appears within 5 seconds of stopping the server.

**U-5 (P1). Times: UTC and IST are mixed, and almost no time names its zone.**
- Where: `tsf` (`app.js:18`), `hhmm` (`app.js:19`), and a third cut of the string at
  `app.js:696`. All three cut characters out of the text the server sent.
- What happens: the server sends IST everywhere except `/api/sentiment` and
  `/api/sentiment/latest`, which send UTC. On the News tab a score made at 15:18 IST is printed
  as 09:48, beside the market pulse, whose time is IST. Only the body of an alert names the zone.
- Fix: one serializer on the server, always `+05:30`. One module in the page with `fmtTime`,
  `fmtDateTime` and `fmtAge`, built on `Intl.DateTimeFormat` for `Asia/Kolkata` (the clock
  already does this, `app.js:64`). Column headings say "Time (IST)". Delete `tsf` and `hhmm`.
- Done when: a node test run with `TZ=America/New_York` and with `TZ=UTC` prints the same IST
  text for one instant given once as `+00:00` and once as `+05:30`; the contract test (T-6)
  finds no time without `+05:30` in any API answer; `slice(11, 16)` appears nowhere in the page
  script.

**U-6 (P2). The clock redraws on every animation frame and works out the session in the
browser.**
- Where: `tickClock` (`app.js:66` to `:81`).
- What happens: a `requestAnimationFrame` loop (`app.js:79`, `:81`) writes the time with
  milliseconds about 60 times a second, all day. The words under the clock ("market open",
  "market closed") come from the laptop's clock and the fixed minutes 555 and 930
  (`app.js:75`, `:76`). Holidays and weekends come from the calendar that is loaded once when
  the page opens (`app.js:124`), so by the code a page left open overnight keeps yesterday's
  answer. Special sessions and the hours of D-1 (15:15 for F&O stocks, 15:40 for equity
  derivatives) are not known to it. "Market closed" does not say when the market opens next.
- Fix: one tick a second and no milliseconds. The session words, the next open and the next
  close come from the server's heartbeat, which reads the session calendar (D-1). The alert
  countdown (`app.js:874`) uses the same server-corrected time, so a laptop clock that is a
  minute wrong cannot shorten or lengthen an alert.
- Done when: the page script has no `requestAnimationFrame` clock loop; tests cover a holiday
  and a special session; the numbers 555 and 930 are gone from the page (C-4).

### 11.3 Home and the Live Desk

**U-7 (P1). On the Live Desk the instruments start 2.7 screens down.**
- Where: `index.html:129` to `:181`. The order is: Open positions, pending alert cards, Paper
  trades, Fill costs, Market context, Indices beside Stocks, Instrument focus, the legacy engine.
- Measured at 1440×900: the "Fill costs" card starts at y = 676 px and is 1,481 px high
  (1.6 screens). The freshness line (`#desk-source`) is at y = 2,194 px. The first instrument
  card is at y = 2,428 px. The page is 5,430 px long (6.0 screens). On the phone the first
  instrument card is at y = 3,460 px (4.1 screens down).
- What happens: the tab is called Live Desk, and what is live on it (the watch list with its
  states and the micro-trend strip) is below a reference table that changes once in five
  minutes.
- Fix: the order of section 11.1, point 5. The freshness line goes into the pill (U-3).
- Done when: at 1440×900, with no pending alert and no open trade, the first instrument card
  starts inside the first screen (y below 600 px), measured by the browser test (T-3).

**U-8 (P1). The alert card does not put first what the owner needs in ten minutes.**
- Where: `alertRow` (`app.js:312`) on Home and `signalCard` (`app.js:599`) on the Live Desk: two
  renderers for one alert.
- What happens:
  1. On Home the buttons "Trade taken" and "Skipped" are inside the body (`app.js:331`, `:332`),
     and the body is hidden until the head is clicked (`.alert-body{display:none}`,
     `styles.css:399`; the `open` class is set only for a row the owner opened, `app.js:333`).
     The alert is valid for ten minutes.
  2. Under 900 px the stylesheet removes the strategy and the countdown from the head
     (`styles.css:432`). On a phone a pending alert on Home shows neither.
  3. The head shows the outlay, not the loss at the stop. "Risk ₹ (incl. charges)" is in the
     body (S-4).
  4. The confidence badge and the "backtest" line are not evidence about the slot (F-3, S-1).
  5. Delayed data is a few words in the dim line under the name (`srcTag`, `app.js:295`).
  6. Nothing says whether the contract can be traded at the alert's price (S-6).
  7. The buttons stay active while the request runs (`wireAlerts`, `app.js:405` to `:410`), and
     "Skipped" gives no feedback.
- Fix: one component for both tabs. A pending alert is always open. Line 1: instrument, side,
  contract, and a large countdown with "valid until 10:55 IST". Line 2: entry, stop, "loss at
  the stop ₹…, including charges", outlay. Line 3: the slot's own record (S-1), or "no evidence
  yet". Then spread and depth with a plain verdict (S-6). A red band across the card when the
  data is delayed. Buttons at least 44 px high, disabled while a request is in flight, with a
  toast for both answers. Keys `T` and `S` act on the alert that has the focus (US-017).
- Done when: node tests cover the states pending, expired, taken, skipped and delayed; at 390 px
  wide the countdown and both buttons are visible without a click (T-3); a second click during
  a slow request sends nothing.

**U-9 (P2). The confidence tooltip is raw JSON.** `confBadge` (`app.js:296`) puts
`JSON.stringify(c.parts)` into the `title` attribute. The four parts (`signal_strength`,
`backtest_evidence`, `live_record`, `regime_fit`) are already drawn in the open row
(`app.js:325` to `:327`). Remove the `title`; let a click on the badge open the parts. This goes
away with F-3 if the blended score is dropped.

**U-10 (P1). Home starts with the broker account, and a pending alert appears at the bottom of
the first screen.**
- This order is the owner's own decision of 7 November 2026, so this finding proposes and does
  not decide (Q-14).
- Where: `index.html:87` to `:126`.
- Measured at 1440×900: the header ends at y = 229, a start-up warning ends at y = 316, the
  "My portfolio" tiles run from y = 392 to 646, and a pending alert starts at y = 798, the last
  102 px of the first screen. At 390×844 the tiles run from y = 762 to 1,901 and a pending alert
  is at y = 2,150, which is 2.5 screens down.
- What happens: the account changes when the owner trades; an alert changes by the second and
  is gone in ten minutes. The slow block is above the fast one. Broker money and paper trades
  also share one page with nothing that tells them apart at a glance.
- Proposal that keeps the order: "My portfolio" becomes one compact row (the same tiles in one
  line) with "show all". Every block carries a small tag, "Dhan account" or "paper". If the
  owner agrees, the broker blocks lower on the page (ledger, trade book, profile, positions,
  holdings) move to an "Account" tab.
- Done when (if accepted): a pending alert starts above y = 300 px at 1440×900 and above
  y = 500 px at 390×844.

**U-11 (P1). The scoreboard does not show how far the paper test is.**
- Where: `drawScoreboard` (`app.js:376` to `:403`). Columns: Strategy, Trades, Win %, Net ₹,
  Avg / trade, Profit factor, Avg R, Open, No fill.
- Missing: the count against 30 and the month it is expected to be reached (P-8); the sessions
  the desk covered (O-1); live and delayed trades apart (F-5); any caution about small samples.
  A win rate of "100%" from two trades is printed like one from two hundred.
- Fix: "closed 9 of 30" with a bar. Live and delayed counts apart, and the figures from live
  trades only. Win rate and profit factor are printed grey with "too few" until a slot has 10
  closed trades. Sessions covered ("18 of 21"). One line above the table: "Review at 30 closed
  trades per slot. Until then these figures are a record, not a verdict." All figures come from
  the one ledger of P-5.
- Done when: a slot with 9 closed trades prints "9 of 30"; the totals equal the paper ledger's.

**U-12 (P2). Instrument cards explain themselves only in tooltips, and open only with a
mouse.** The chips on a card carry their reason only in `title` (`accRow`, `app.js:784`), which
a phone never shows. The head of a card is a `div` with a click handler (`app.js:808`,
`:869`). The same pattern is used for alert rows (`app.js:412`), the Lab's category chips
(`app.js:966`) and the rows of the runs table (`app.js:1090`). Fix: heads become
`<button aria-expanded>`; the state word is printed on the chip and the reason in the open card.
Done when: every card opens with Tab and Enter, and a node test finds no click handler on a
`div`, `span` or `tr` without a role.

**U-13 (P2). The micro-trend strip of 11 November can be misread as a signal.**
- Where: `trendPips` (`app.js:775`), `trendBlock` (`app.js:777`), `slotTrail` (`app.js:779`),
  `slotTrailNote` (`app.js:781`); styles at `styles.css:273` to `:284`.
- What happens: the strip shows six pips ("▲ 5m" and so on) and a count ("all 6 up",
  "3 up · 2 down · 1 no reading"). It does not mark which timeframes the running slot uses
  (entry on 5m, trend on 30m, trail on 60m), and it does not say whether the slot's gate is
  open: ADX, RSI, the flip window and the last entry time are not in it. "All 6 up" can
  therefore be read as "buy", when it is only a description. With the old process still
  running, every card says "no reading yet", the same words as the true state at the start of
  a day (A-9). The note "CE stop level now …" does not say that the level is an index level and
  not a premium. The "since" times go through `hhmm` (U-5).
- Fix: mark the slot's own timeframes on the pips. Replace the count with a verdict tied to the
  gate: "gate open for CE", or "gate shut: 30m down". Print "not sent by this server version"
  when the field is missing. Add "index level" to the note.
- Done when: node tests cover the gate-open, gate-shut, no-reading and field-missing cases.

**U-14 (P1). An open paper trade shows no stop, no current price, no result and no stop moves.**
- Where: `paperRow` (`app.js:655`) and `paperTable` (`app.js:667`), with the columns Status,
  Instrument, Side, Grade, Entry / unit, Exit / unit, Gross (trade), Net (trade), Close reason,
  Time. The payload is built by `trade_view` (`src/onelot/paper/view.py:37`).
- What happens: the API already sends `mark` and `unrealised` for an open trade and the page
  does not print them. The stop, the trail level and the stop moves are not in the payload,
  although the book records every move as a `STOP_MOVED` event (P-4). `positionsTable`
  (`app.js:634`) shows Stop, R now and Unrealised only for alerts the owner marked as taken.
  Home has no list of open paper trades: "Monitoring" lists taken alerts only, and the paper
  table is loaded only while the Live Desk tab is open (`loadPaper`, `app.js:711`). The line
  trail built on 11 November is the owner's main request, and the screen cannot show it moving.
- Fix: `trade_view` adds for an open trade: `stop`, `initial_stop`, `stop_basis` ("ATR trail",
  "60m Supertrend line"), `stop_moves`, `last_stop_move_at`, `risk_now`, `locked_in`, and the
  underlying's level where the stop is defined on the index. `mark` gets its time and its basis
  ("premium at the 10:45 bar close"), because an option's mark is the premium stored at the last
  managed bar and not a live quote. One "Open trades" table, on Home inside "Monitoring" and on
  the Live Desk, with the columns Instrument, Side, Entry, Last, Stop (with its basis), Distance
  to stop (%), Unrealised ₹, Result if stopped ₹, Stop moves, Age, and a "taken" mark. A row
  opens to the list of stop moves and a small chart of price and stop.
- Done when: a test moves the line three times and the row shows the third level, "3 moves" and
  the right "result if stopped"; an open trade whose mark is missing prints "no price yet" and
  never 0.

**U-15 (P1). Show the real risk in place of the "Risk governor" block.**
- Where: the "Risk governor" block on every instrument card (`app.js:793` to `:796`: Desk state,
  Trades today, Open n/max, Realised today, Daily loss limit, Consecutive losses), the same
  block again in Instrument focus (`app.js:922`), the line `#desk-risk-line` (`app.js:830`,
  "N open · each closes at its exit alert's price"), and "Risk (absolute rupees, one unit)" on
  Settings (`app.js:1468`), which prints the legacy engine's limits.
- What happens: the governor gates nothing on the desk (G-1, rule 5), so limits are shown that
  do not exist, and the figure that does exist is shown nowhere: how much is lost if every open
  stop is hit.
- Fix: one risk line on Home and on the Live Desk, taken from the paper book: "Open paper
  trades: 3. Loss if every stop is hit: ₹18,400, including charges. Today, closed: +₹2,150."
  The same three figures for the alerts the owner took, on a second line. If the owner sets a
  budget per lot (S-4), the line shows it as a comparison, never as a gate. Remove the block
  from the cards and from Instrument focus.
- Done when: the words "Risk governor" appear nowhere on Home or the Live Desk, and the "loss
  if every stop is hit" equals the sum over the open trades in a test with three trades.

**U-16 (P2). Details of the paper trades table.** The list is cut at the 200 newest trades
(`RECENT_LIMIT`, `src/onelot/paper/book.py:109`; the page says so, `paperPanel`, `app.js:669`),
and the payload is 66 KB on every load. A row carries `alert_id` and has no link to its alert,
and an alert has no link to its trade. The grade (live or delayed) is a column with no
explanation, and the table has no filter by grade, slot or instrument and no sorting. Fix: pages
of 50 from the server with the filters as query parameters (US-016); each row links to its
alert and back; a legend for the grade. Keep one thing as it is: when the request fails,
`loadPaper` (`app.js:711`) leaves the last table on screen and turns the line above it into a
warning that says why. That is the pattern every loader on the page should follow (U-22, U-28).

**U-17 (P2). Reference panels sit in the middle of the Live Desk.** "Fill costs"
(`loadFillCosts`, `app.js:684` to `:706`) is refreshed at most once in five minutes and is
1,481 px high on a laptop and 1,645 px on a phone. It holds a paragraph, a table by instrument,
the last ten alerts with the fill against the alert price, and the owner's own fills and
charges. "Market context" (`app.js:850` to `:852`) is a row of tiles 110 px high. Both are
reference, not live. Fix: "Fill costs" moves to a "Costs" sub-tab, which is also where the
verified charge table of L-4 belongs; "Market context" becomes one line at the top of the Live
Desk; the owner's own fills go with the account blocks (U-10).

**U-18 (P2). Instrument focus repeats the card and sends the owner elsewhere to act.**
`renderFocus` (`app.js:907` to `:940`) draws the same blocks as the card above it
(`app.js:919` to `:927`), with a second "Risk governor" block (`app.js:922`), and says
"Pending alert: … act on it from Home" (`app.js:928`). It starts at y = 4,485 px on a laptop.
Its chart loads 400 bars (`app.js:933`) with a timeframe list of its own (C-3). Fix: the focus
view opens beside the watch list when a card is chosen (under it on a phone). It holds what the
card cannot: the chart with the entry, the stop line and each stop move drawn on it, the slot's
settings, and the alert component of U-8 with its buttons.

**U-19 (P2). The legacy engine section.** `index.html:166` to `:181` is a collapsed card,
"Bank Nifty option engine", with its own signal, market, regime and "Risk governor" panels, the
"Panic halt" and "Clear halt" buttons (`:172`), its own positions, journal and alert feed. It
is rebuilt every second while closed (G-5). It goes when the owner decides G-2. Until then,
draw it only while it is open, and label the two buttons "legacy engine only" (G-3).

### 11.4 Type and phone

**U-20 (P1). A third of the text on the main screens is smaller than 13 px, against the owner's
large-type rule.**
- Where: `styles.css`. Its own first comment calls the type scale "deliberately large", for a
  page that "is read at arm's length all session".
- Measured: the file has 68 `font-size` declarations, all in px. 24 are below 13 px and 34 are
  at 13 px or below. Text elements drawn under 13 px: Home 139 of 504 (28%), Live Desk 216 of
  725 (30%), Lab 66 of 112 (59%), Data 27 of 1,174, News 33 of 774.
- Examples: table headings at 11.5 px in capitals (`styles.css:170`); tile captions at 11.5 px
  (`:119`); the unit under a signal number at 11 px (`:189`); the time in an alert head at 11 px
  in the dim colour (`:385`); the second line of an alert head at 12 px (`:387`); parameter
  descriptions in the Lab at 11.5 px in the dim colour (`:165`); the brand line at 12 px
  (`:65`); an inline `font-size:12px` in `app.js:340`.
- Colour, computed: the dim colour (`--dim`, #7a879e) has a contrast of 3.63:1 on a white card
  and 3.21:1 on the page background. In the Night theme it is 4.09:1 on a card. The usual
  minimum for small text (WCAG AA) is 4.5:1. The muted colour (7.07:1) and the text colour
  (18.5:1) are fine.
- What happens: the smallest and faintest text is where the units, the times and the reasons
  are: column headings, the time of an alert, what a parameter means.
- Fix: a named scale as custom properties (for example 13, 14.5, 17, 21 and 26 px), used by
  every rule, in `rem` so that the browser's own text-size setting works. Nothing below 13 px.
  Darken `--dim` to at least 4.5:1 on both backgrounds (#5f6c85 gives 5.29:1 and 4.67:1 in Day;
  #8493ae gives 4.98:1 in Night). No font size in the page script (C-11). The base size of
  17 px and the 26 px tiles do not change.
- Done when: a test reads `styles.css` and finds no size below 13 px; the browser test finds no
  text element drawn below 13 px on any tab; a test computes each text colour against its
  background at 4.5:1 or better in both themes.

**U-21 (P2). The phone layout is the laptop layout, stacked.**
- Measured at 390×844: the header is 476 px (56% of the screen). The page is wider than the
  screen: the layout viewport grows to 413 px on Home, Data and News and to 520 px on the Lab,
  so the page scrolls sideways. Page lengths: Home 8,345 px (9.9 screens), Live Desk 7,717 (9.1),
  Lab 1,582 (1.9), Data 3,933 (4.7), News 6,021 (7.1). Elements wider than the screen: Home 5,
  Live Desk 4, News 1. Controls under 32 px high: Home 4 of 15, Lab 1, Data 2, News 2. The
  account tiles alone are 1,139 px high.
- Where: six media queries, each for one component (`styles.css:197`, `:216`, `:239`, `:338`,
  `:416`, `:432`). None deals with the header, the tiles or the tables.
- Why it matters: once alerts reach the owner by Telegram (O-3), the phone is where the owner
  will open the desk, with ten minutes to act.
- Fix: point 8 of section 11.1. The tabs become one strip that scrolls sideways inside the
  header. Tiles go two to a row. Under 700 px the alert and open-trade tables become cards.
  Every other table scrolls inside its own box, so the page itself never scrolls sideways.
  Controls are at least 44 px high. Story US-017 already asks for this; add the measurements
  as its acceptance criteria.
- Done when: the browser test at 390×844 finds `document.documentElement.scrollWidth` equal to
  390 on every tab, a header of at most 112 px, and the countdown and buttons of a pending
  alert inside the first screen.

### 11.5 The other tabs

**U-22 (P2). The Lab's form is empty until three requests have all answered, with no loading
state.**
- Where: `loadLab` (`app.js:954` to `:962`) waits for `/api/lab/strategies` (64 KB),
  `/api/lab/instruments` and `/api/lab/focus` together before it fills any select. The tab is
  opened without a `catch` (`showTab`, `app.js:132`).
- What happens: until all three answer, the selects are empty and nothing says "loading". If
  one fails, the other two are thrown away, a toast names the route for six seconds, and the
  form stays empty with no way to try again except reloading the page. `loadNews` has the same
  shape with five requests (U-25). The three run buttons discard errors
  (`.catch(() => {})`, `app.js:1064` to `:1066`).
- Fix: each part is filled when its own request answers. A select that is waiting is disabled
  and says "Loading…". A failure leaves a line with the reason and a "Retry" button in that
  part (the `loadPaper` pattern, U-16).
- Done when: a node test in which `/api/lab/focus` fails finds the other two selects filled and
  one retry line.

**U-23 (P2). Lab results and tables.**
- Parameter labels print the raw key (`stop_atr_min`), with the range and the description in
  11.5 px dim type (`app.js:1023`). Print the parameter's title, with the key beside it.
- The verdict of a run is worked out in the page (`verdictOf`, `app.js:1100` to `:1105`: under
  20 trades "TOO FEW TRADES"; profit factor of 1.2 or more and expectancy above 0.05R
  "POSITIVE"; profit factor above 1 "MARGINAL"; otherwise "NEGATIVE"). A verdict in the page
  script cannot be tested with the Lab and knows nothing of periods, premiums or the number of
  settings tried. Move it to the server with L-5 and S-11.
- The hint under the form says the premiums are "Black-76 synthetic" (`app.js:987`) and stays
  the same whatever the run used (L-3). The result must say which premiums it used and how many
  trades were priced.
- Rows of the runs table are clickable `tr` elements (`app.js:1090`; U-12). A filter that can
  never be false is left in the code (`x.kind !== "sweep" || true`, `app.js:1082`). The runs
  list is 280 px high (`.runs`), about seven rows.
- The leaderboard has five selects and a number box in one row (`index.html:234` to `:246`)
  and loads 212 KB in 1.45 s; the matrix is 1.83 MB; the runs list is 117 KB every 2 seconds
  while a job runs (`app.js:1067`; L-8).
- 59% of the Lab's text is under 13 px (U-20), and its help text describes an earlier app (O-6).

**U-24 (P2). The Data tab.**
- The tab is 7,536 px long at 1440 px wide (8.4 screens), and it does not start with the one
  fact the owner needs: how fresh the store is (D-2). Put a summary first: "Bars up to 11 Nov
  15:29 IST for 21 of 23 instruments. 2 are behind."
- "Quality report (5m)" (`app.js:1390` to `:1394`) waits for `/api/data/quality`, measured at
  32.9 seconds on the live database, with no progress, no disabled state and no cancel. A second
  click starts a second read (D-6). Make it a job like the backfill, keep the last result with
  its time, and disable the button while it runs.
- A backfill that calls the broker for hours starts behind a browser `confirm()`
  (`app.js:1383`), and its errors are reduced to a count (D-3). The confirmation should say how
  many requests and how long, and a job row should open to its list of errors.
- While a job runs, `loadData` (`app.js:1332`) redraws the whole tab every 2.5 seconds.
- The correlation table is coloured with fixed `rgba` values (`app.js:1371`) that ignore the
  theme. Times go through `tsf` (U-5).

**U-25 (P2). The News tab.**
- The tab is 4,895 px long (5.4 screens). `loadNews` (`app.js:1405` to `:1434`) waits for five
  requests together, one of them with `limit=2000`, and draws nothing if one fails (U-22).
- Scores are printed in UTC beside the pulse in IST (U-5).
- "Refresh" on the market pulse is a GET with a side effect (`app.js:1435`,
  `/api/sentiment/pulse?refresh=1`), the pattern of A-2. Make it a POST.
- The card "API for your scorer" prints curl commands with `x-onelot-token: $TOKEN`
  (`app.js:1422` to `:1433`). When A-1 is fixed these must name a separate scorer key with the
  right to post scores and nothing else.
- Order on the tab: pulse, score history, latest per scope, manual post, import, recent scores,
  news feed, the API card (`index.html:339` to `:387`). No strategy on the desk reads a
  sentiment score today (to verify). If so, say that on the tab in one line, so that the owner
  does not take the pulse for an input to the alerts.

**U-26 (P2). Settings.** Read in source only.
- The credential form is right in the main points: the secret fields are of type `password`
  with `autocomplete="off"` (`index.html:393` to `:417`) and are cleared after saving
  (`app.js:1487`). The status rows are written through `kv()` without `esc` (`app.js:1458` to
  `:1465`; A-5).
- The configuration is shown as raw JSON in `<pre>` blocks (`app.js:1466`) under headings that
  belong to the legacy engine: "Live direction source (Bank Nifty engine)", "Risk (absolute
  rupees, one unit)", "Backtest defaults", "Data corpus" (`app.js:1467`, `:1468`). The note
  says "edit config/config.yaml". What the desk actually runs, `config/desk.yaml`, appears only
  as a list of slot names with a "✎" mark and no parameter values (`app.js:1475`).
- "Reload desk config" (`app.js:1518`) sits beside "Poll now" with no warning that a reload
  forgets today's counts and cooldowns (F-1), and it cannot show a configuration problem (F-4).
- "Send a test notification" shows raw JSON in a toast (`app.js:1520`).
- The end of the token is shown here and in a header badge only (O-2).
- Fix: the tab leads with a "Desk" card: every slot with its settings, its `params_hash` (F-3),
  the time the file was loaded and any problem found in it. Then Credentials and token, with
  the end time in IST and the time left. Then Notifications, with the test result in words.
  The legacy headings go with G-2. Reload asks for confirmation in the page, stating what it
  resets.

### 11.6 Accessibility and small things

**U-27 (P2). The page is built for a mouse and for good eyes.**
- Measured in the source: `index.html` has 32 buttons, 23 selects and 32 inputs, with 5 `aria-`
  attributes, 4 `role` attributes and 3 `tabindex`, all but one of them in the paper view
  switch (`index.html:137` to `:140`). `app.js` has one `keydown` listener (`app.js:758`, the
  same switch) and no `aria-live` region.
- Focus: inputs have `outline:none` (`styles.css:134`) and get a ring of their own (`:135`).
  The only `:focus-visible` rule is for the paper view buttons (`:311`). Tabs, buttons and card
  heads have no focus style of their own.
- Keyboard: card heads, alert rows, Lab chips and run rows are `div`, `span` or `tr` elements
  with click handlers (U-12). The clock is a `div` that opens the calendar on a click
  (`index.html:70`).
- Announcements: a toast is a plain `div` that is removed after 6 seconds (`app.js:30` to
  `:33`), fixed at the top right (`styles.css:210`), where it covers the header badges. A new
  alert is never announced to a screen reader, and a toast that was missed is gone.
- Dialogs: destructive or costly actions use the browser's `confirm()` (`app.js:462`, `:903`,
  `:904`, `:1383`), which cannot say enough and cannot be styled or tested.
- Colour: the trend pips and the outcomes carry a glyph or a word as well as a colour, which is
  right. The confidence badge and the health dot (`.hs`, `styles.css:287`) rely on colour alone.
- Motion and theme: there is no `prefers-reduced-motion` rule and no `prefers-color-scheme`
  rule. The Day default is the owner's choice and stays.
- Fix: real `<button>` elements with `aria-expanded`; one visible `:focus-visible` style for
  every control; an `aria-live="polite"` region for toasts and an `assertive` one for a new
  alert; toasts placed where they cover nothing, with the alert toasts staying until dismissed;
  one in-page confirmation dialog; keys for the session actions (US-017); a reduced-motion rule
  for the animations. Story US-018 covers this; add the checks below to it.
- Done when: the browser test walks Home and the Live Desk with the Tab key alone, opens a card
  and answers an alert; an automated accessibility check (axe) reports no serious finding on
  the six tabs.

**U-28 (P3). Small things.**
- `favicon.ico` answers 404 (A-10).
- Every failed API call raises a toast with the route (`app.js:43`), so one dead route polled
  every 2 seconds fills the corner with toasts. Show one message per route until it recovers.
- On a 401 the page fetches `/api/session` again (`app.js:41`). This goes with A-1.
- `beep` (`app.js:474` to `:485`) swallows its errors. Browsers block sound until the page has
  been clicked, and nothing on screen says whether the alert sound can play. Show a small
  "sound on / sound blocked: click to enable" mark in the header.
- Empty states use the mascot's voice ("Flat. Moo is napping until you take a signal."). Keep
  the style, but say first what the state is: "No open trades."
- The legacy card's summary reads "▾  show" from the stylesheet (`styles.css:318`), text that a
  screen reader and a translator cannot see.
- 31 inline `style="…"` attributes in `app.js` and 7 in `index.html` (C-11).
- The footer disclaimer and the connection word share one line (`index.html:435`, `:436`); the
  connection word moves to the pill (U-3).

---

## 12. Code structure

`src/onelot` holds 35,618 lines of Python in 1,527 functions; the interface is another 2,530
lines. The newer code is in good order: typed dataclasses, integer paise in the paper book, pure
functions for fills and trade management, a test for almost every rule. The structural problems
are few, and most have one cause: a fact that is written down in several places. They are the
root of several defects in the sections above, so fix the structure together with the defect it
causes, in the same story.

**C-1 (P2). The page script is one file that builds the page by joining strings.**
- Where: `src/onelot/ui/app.js`.
- Measured: 1,527 lines. The longest line has 1,375 characters and 129 lines have more than 200.
  85 function declarations and 85 top-level `const` and `let` names share one global scope.
  HTML is written through `innerHTML` in 98 places, and each value is escaped by hand (`esc(`
  occurs 306 times). There are no modules and no strict mode.
- What happens: one missed `esc()` lets outside text into the page as HTML (A-5 lists the
  misses). A line of 1,375 characters cannot be reviewed in a diff. Nineteen test files read or
  run the script; those that run it in node can reach a function only by loading all of it.
- Fix: ES modules served as plain static files (`<script type="module">`), with no build step
  and no framework: `time.js` (U-5), `api.js`, `stream.js`, `status.js` (U-3), `dom.js`,
  `alerts.js`, `trades.js` (the open-trades component that Home and the Live Desk share),
  `desk.js`, `lab.js`, `data.js`, `news.js`, `settings.js`. `dom.js` holds one `html` tagged
  template that escapes every value unless it is marked as trusted markup, so a missed escape
  becomes impossible, not just unlikely. Lines are wrapped at 100 characters.
- Order: do this first in the interface phase (section 14), before the layout work of section
  11, so that each screen is rewritten once. It is story US-018.
- Done when: no file in `ui/` has more than 400 lines or a line over 160 characters; a test
  finds `innerHTML` only in `dom.js`; the node tests pass against the modules.

**C-2 (P2). Three classes and one function hold too much.**
- Measured: `Desk` (`desk/desk.py:177`) has 1,981 lines and 70 methods. `PaperBook`
  (`paper/book.py:231`) has 1,533 lines and 68 methods. `Engine` (`engine.py:74`) has 1,190
  lines and 44 methods. `create_app` (`api/server.py:221`) is one function of 791 lines that
  defines all 65 routes as closures. 18 functions are longer than 100 lines, among them
  `_raise_signal` (`desk/desk.py:630`, 158 lines) and `_evaluate_slot` (`desk/tracker.py:339`,
  144 lines).
- What happens: `Desk` polls, fetches bars, evaluates slots, raises alerts, chooses and prices
  option contracts, keeps the archive, feeds the paper book and builds the payload for the
  page, all under one lock (F-7). None of these can be tested without building the whole desk,
  which is why the end-to-end test is still missing (T-1). The routes cannot be read or tested
  one area at a time, and none declares what it returns (A-9).
- Fix, in steps that each keep the gate green:
  1. `create_app` becomes one router per area (desk, paper, lab, data, account, sentiment,
     settings, stream), each with its response models (A-9) and the token check as a
     dependency (A-1).
  2. From `Desk`, move out the alert builder (contract choice, prices, the evidence line of
     F-3) and the snapshot builder (built outside the lock, F-7). `Desk` keeps the poll loop,
     the lock and the day's state.
  3. From `PaperBook`, move the SQL into a small repository. The rules are already pure
     functions (`paper/manage.py`, `paper/fill.py`) and the views are in `paper/view.py`.
  4. `Engine` splits with G-2.
- Done when: a test over the syntax tree finds no class over 600 lines and no function over
  100, with an allow-list that may only shrink (the pattern of the mypy ratchet).

**C-3 (P2). The list of timeframes is written in at least eight places.**
- Where: `indicators/mtf.py:29` (`LADDER_TIMEFRAMES`: 1m, 5m, 15m, 30m, 60m, 1d);
  `lab/sweep.py:27` (`FOCUS_TIMEFRAMES`: 5m, 15m, 30m, 60m); `data/store.py:267`
  (`LAB_TIMEFRAMES`: 5m to 1d); `data/resample.py:29` (`RESOLUTION_ORDER`, from 1m upward);
  `desk/history.py:30` (`FINE_RESOLUTION`); the `Literal` types of `trend_tf` and `trail_tf`
  (`strategies/supertrend_mtf.py:72`, `:109`); three selects in `index.html` (`focus-tf`: 2m to
  60m; `eda-res`: 5m, 2m, 15m, 60m; `cov-tf`: 1m to 1d); `app.js:951` (`TF_CANDIDATES`, 15
  values).
- What happens: the lists already differ, so a timeframe can be offered on one screen and
  refused on another, and adding one means finding every list.
- Fix: one module (`onelot/timeframes.py`) with the canonical list, the minutes of each, and
  named subsets (ladder, lab, focus, chart). The page gets the lists from one route and fills
  its selects from it.
- Done when: a test finds no timeframe string such as `"15m"` in a list or tuple literal
  outside that module and the tests.

**C-4 (P1). The hours of the session are written in at least fifteen files.**
- Measured: the strings "09:15", "09:30", "14:45", "15:10" to "15:15", "15:30" and "15:40"
  occur 24 times in 11 source files (`data/universe.py` 6; `desk/desk.py`, `desk/tracker.py`,
  `strategies/base.py` and `lab/runner.py` 3 each; six more files once each). The same hours
  appear as numbers: `SESSION_CLOSE_IST = dt.time(15, 30)` (`paper/fill.py:48`),
  `DEFAULT_EXIT_TIME = dt.time(15, 15)` (`paper/position.py:28`), `MINUTES_PER_SESSION = 375`
  (`broker/mock.py:41`), the minutes 555 and 930 (`app.js:75`, `:76`) and 375 for a day
  (`app.js:952`). The IST zone is defined three times (`clock.py:20`, `store/schema.py:33`,
  `indicators/mtf.py:37`). The holiday file is read by three different pieces of code
  (`clock.py:85`, `engine.py:83`, `api/server.py:918`).
- What happens: this is why D-1 exists. The exchange changed the session on 3 August 2026, and
  the change would have had to be made in fifteen files. It was made in none.
- Fix: the dated session calendar of D-1 (`config/sessions.yaml`) behind one module. Everything
  else asks it: `session_for(instrument, date)` gives the open, the last continuous minute,
  the close and the derivative close; `exit_time`, `entry_end` and the others become offsets
  from those or explicit settings of a slot in `config/desk.yaml`. The page gets the session
  from the server (U-6). One definition of the IST zone, in `clock.py`.
- Done when: a test finds no `HH:MM` string and no `dt.time(` literal for a session hour in
  `src/onelot` outside the calendar module; the numbers 555, 930 and 375 are gone from the page
  and from the mock broker.

**C-5 (P2). The clock is read directly in many places.** `datetime.now(`, `time.time()` and
`utcnow(` are called 58 times in 17 files: `lab/jobs.py` (13 times), `api/server.py` (9),
`broker/dhan.py` (5), `lab/research.py`, `data/db.py` and `broker/mock.py` (4 each) and eleven
more files. `desk.py` calls `now_ist()` 14 times (F-6). Code that reads the clock itself cannot
be replayed or tested at a chosen time, which is one reason the parity replay had to go around
`Desk.poll` (T-1, T-2). Fix: one `Clock` object that is passed in: the desk, the paper book, the
jobs and the API take it as an argument, with the real clock as the default. `freezegun` is
installed, but a clock that is passed in is simpler and faster. Done when: `datetime.now(` and
`time.time()` occur only in `clock.py`; measuring how long something took uses `time.monotonic`.

**C-6 (P2). Two database layers, a third connection, and no migrations.**
- Where: all 29 tables are declared in `store/schema.py` (653 lines), and the one SQLAlchemy
  engine is made in `store/db.py:44`. `data/db.py` (125 lines) reaches the same file through
  `get_engine()` with its own helpers, and `data/store.py` (357 lines) and `store/repo.py`
  (496 lines) both read and write through them. `data/expired_options.py:219` opens a raw
  `sqlite3.connect` with its own `CREATE TABLE`.
- Measured: there is no migration code anywhere in `src/onelot`: no `user_version`, no schema
  version table, no `ALTER TABLE`.
- What happens: a new column (the `params_hash` of P-6, the data-quality mark of F-5, the stop
  fields of U-14) cannot reach the owner's 12 GB file except by hand. Three ways in mean three
  sets of time-out and locking behaviour on one WAL file (US-026).
- Fix: story US-009 first, before any story that changes a table: a `schema_version` table,
  numbered migration scripts, a backup of the book tables before each migration. One module
  that opens connections. The expired-option tables are declared in `store/schema.py`. With
  D-8, consider two files: market data, which can be rebuilt, and the book, which cannot.
- Done when: a test builds a database at the oldest supported version, runs the migrations and
  compares the result with a freshly created schema; `sqlite3.connect` occurs in one module.

**C-7 (P2). Configuration has six files, six places that parse them and no single check.**
- Where: `config/` holds `config.yaml`, `costs.yaml`, `desk.yaml`, `universe.yaml`,
  `nse_holidays.yaml` and `nse_holidays_2026_fyers_ref.yaml`. Beside them are `.env` and the
  `app_settings` table. `yaml.safe_load` is called in six places (`config.py:86` and `:556`,
  `clock.py:85`, `settings.py:44`, `desk/config.py:112`, `api/server.py:919`).
  `desk/config.py` loads `desk.yaml` into typed objects, while `lab/sweep.py:31` reads the
  same file raw. `costs.yaml` is loaded in eight places (`__main__.py:444`,
  `cost_preview.py:35`, `desk/desk.py:287`, `desk/economics.py:73`, `engine.py:82`,
  `lab/runner.py:305`, `paper/factory.py:38`, `replay/replay.py:143`).
- What happens: a mistake in a file is found by whichever reader meets it first, and logged
  (F-4). `config.yaml` still carries the legacy engine's risk and backtest settings, which the
  Settings tab shows as if they applied (U-26).
- Fix: one `load_all()` that reads every file into typed models once at start, returns a list
  of problems with file and key, and gives the same object to the desk, the Lab and the API.
  `python -m onelot check-config` prints the problems and exits non-zero. The Settings tab
  shows them (F-4, U-26).
- Done when: `yaml.safe_load` occurs in one module; a test feeds a broken copy of each file and
  finds the problem reported with its file and key.

**C-8 (P2). 145 handlers catch every exception.** `except Exception` occurs 145 times in 32
files: `desk/desk.py` 24, `engine.py` 20, `paper/book.py` 17, `market/feed.py` 12,
`broker/dhan.py` 8, `lab/jobs.py` 7. Most log and continue. That is right for a poll loop that
must survive one bad instrument, but it also hides programming errors: the desk keeps running
with one slot silently dead (F-2, O-7). Fix: catch the errors that are expected (network,
broker, data) by name. Keep one outer handler per poll, per instrument, per job and per route
group for the rest; it counts the error by type and place and the System panel shows the
count (U-3). Done when: a test over the syntax tree finds that every remaining
`except Exception` calls the shared counter, and only the outer handlers remain.

**C-9 (P2). Names that differ by one letter, and code that only the legacy engine uses.**
`strategy/` (the legacy engine's selector, 1,226 lines) sits beside `strategies/` (the desk's
strategies, 3,140 lines); `store/` beside `data/store.py`, `data/db.py` and `data/schema.py`;
`analytics/indicators.py` beside `indicators/core.py`. The packages `strategy/`, `trade/`
(1,093 lines), `risk/` (352) and `replay/` (502), with about half of `engine.py` (1,263), serve
only the legacy engine: about 3,800 lines, or 11% of the source. Those four packages hold 33 of
the 195 type errors of C-10, and `engine.py` another 27. Fix with G-2: when the owner has
decided (Q-3), move them to `onelot/legacy/` (or delete them, keeping the tag in git), rename
`store/` to `db/`, and merge the two indicator modules. `cost_preview.py` imports `strategy/`
and must be re-pointed first.

**C-10 (P3). Type checking and lint are looser than they look.** The mypy ratchet holds 195
errors in 62 source files (`engine.py` 27, `strategy/alpha.py` 23, `lab/simulator.py` 19,
`desk/audit.py` 10, `desk/desk.py` 10). mypy runs with default strictness, on `src` only, for
Python 3.12, while `pyproject.toml` says `requires-python = ">=3.10"` and ruff targets
`py310`. Ruff's line-length rule (E501) is switched off, and no security rules are selected.
Fix: bring the count down file by file, starting with the simulator and the desk (the legacy
files go with G-2); set one Python version (3.12) in all three places; add ruff's `S` rules
for `src/onelot/api` and `broker`; type the test helpers that the stories below add.

**C-11 (P3). Small things.**
- 31 inline `style="…"` attributes in `app.js` and 7 in `index.html`, some with a font size
  (U-20). Move them to classes.
- A filter that is always true (`x.kind !== "sweep" || true`, `app.js:1082`).
- The project description in `pyproject.toml` still reads "live Bank Nifty option signals,
  one-unit backtest lab across options/futures/stocks/currency" (O-6).
- `yfinance>=0.2.40` has no upper bound in `requirements.txt`; every other package has one.
- Eight research scripts of about 4,000 lines live in `scripts/` without tests (L-10).

---

## 13. Tests

The suite is large and it is honest about rules: 209 test files, 4,303 test functions, 7,754
cases after parametrisation, all green in 8 minutes 45 seconds. Half the files are acceptance
tests named after their story (`tests/acceptance`, 103 files); the rest follow the packages
(`tests/paper` 33, `tests/desk` 23, `tests/lab` 16, `tests/notify` 10, top level 24).
`hypothesis` is used in 35 files, and 13 files run the page script in node.

What the suite cannot see is the joins between the parts and the page as a browser draws it.
Every defect in sections 4 to 11 that reached the owner's screen passed the gate. The eight
findings below are the tests that would have caught them.

**T-1 (P1). No test runs an alert from `Desk.poll()` into a real `PaperBook` and out again.**
- Where: `tests/desk/test_desk_line_trail.py` (35 tests) drives the desk with a recorder in
  place of the paper book. `tests/paper/test_book_line_trail.py` (25 tests) drives the book
  with alerts made by hand. The parity replay goes around `Desk.poll` because the desk reads
  the wall clock (F-6, C-5), and it leaves out option premiums and the rule of one live idea
  per instrument.
- What is untested: the path the owner depends on. A closed bar arrives; a slot raises an
  alert; the desk chooses and prices the contract; the book opens the paper trade at the next
  minute; the line moves; the desk passes the level; the book moves the stop and records
  `STOP_MOVED`; the trade ends by `TRAIL_STOP`, `STOP` or `SESSION_END`; the scoreboard, the
  archive and `/api/paper` all show the same result (P-5).
- Fix: one harness, `tests/e2e/`, that builds a real `Desk` and a real `PaperBook` on a
  temporary database, with a passed-in clock (C-5), a feed that plays stored one-minute bars,
  and a fixed option chain. Scenarios, each a few simulated hours:
  1. a long call that trails and ends by `TRAIL_STOP`;
  2. a trade that reaches `SESSION_END`;
  3. a gap through the stop at the open of a bar;
  4. two slots wanting the same index (S-10);
  5. a restart in the middle of an open trade: the stop and its moves survive;
  6. "Reload desk config" in the middle of the session (F-1);
  7. a feed that goes delayed during a trade (F-5, S-9);
  8. a stock trade on a day with the 15:15 close (D-1, P-3).
- Done when: the eight scenarios run in the gate in under 60 seconds together, and each
  asserts the alert, the events of the paper trade and the three views of its result.

**T-2 (P1). The proof that the desk and the Lab agree is a script that was run once.**
- Where: `scripts/mtf_desk_replay.py` (421 lines). On 11 November it matched the Lab trade for
  trade on two windows (10 of 10 and 57 of 57, section 2.3). It reads the owner's 12 GB store
  and a pickle under `data/research/`, so it cannot run in the gate or on another machine.
- What happens: the stop, trail and exit rules exist four times (P-1). The next change to one
  of them can break the agreement, and nothing will say so until someone runs the script.
- Fix: commit a small fixture (about ten sessions of one-minute bars for two indices and two
  stocks, a few megabytes as compressed CSV or parquet, cut from the two windows). A test runs
  the Lab and the desk replay on it for each line-trailing slot and for one plain slot, and
  compares the trades field by field: entry time and price, every stop move, exit time, price
  and reason. With T-1's harness the replay can go through `Desk.poll` itself.
- Done when: the test is in the gate, and changing one comparison in `paper/manage.py` or in
  `lab/simulator.py` makes it fail (check this once by hand and note it in the story).

**T-3 (P1). Nothing tests the page in a browser.**
- Where: the 13 node tests call functions of `app.js` against a hand-made DOM. They cannot see
  layout, size, focus, the stream or what happens on load. Playwright is not installed.
- What that let through: a header of 213 px (U-2), seven old toasts and a beep on every load
  (U-1), the instruments 2.7 screens down (U-7), sideways scrolling on a phone (U-21), text
  under 13 px (U-20), a footer that says "live" beside frozen prices (U-4).
- Fix: Playwright with Chromium as a development dependency, and `tests/browser/` marked
  `browser`. The app is started on a temporary database with the mock broker and a fixed clock.
  Each test loads a tab at 1440×900 and at 390×844 and measures what section 11 states as
  "Done when": header height, the position of the first alert and the first instrument card,
  `scrollWidth`, the smallest font size, toasts on load, the status pill in each of its seven
  states, the Tab-key walk, an axe scan. A screenshot of each tab is kept as a build artefact,
  not compared pixel by pixel.
- Never run it against the owner's desk or with the owner's `.env`: the test settings must
  point at the temporary database, and the test fails at start if a real client id is set.
- Done when: `pytest -m browser` passes locally in under two minutes and runs in CI (O-4).

**T-4 (P2). The gate does not measure coverage.** `pytest-cov` and `coverage` are installed and
unused. Add branch coverage to the gate as a report first (`--cov=onelot --cov-branch`), then a
floor that may only rise, per package: start with `paper/`, `desk/` and `indicators/`, where a
missed branch is a wrong trade. The legacy packages are left out until G-2 is decided. Done
when: the gate prints the table and fails when a package falls below its recorded floor.

**T-5 (P1). No test ties the session calendar to the data, and time-zone tests are thin.**
- What is missing: D-1 was found by counting bars in the store, not by a test. Nothing checks
  that the hours in the code match the first and last stored bar of an instrument; nothing
  runs a session with an early close, a special session or a holiday that falls on a weekday;
  nothing runs the page or the server in a time zone other than IST (U-5, U-6, D-7).
- Fix:
  1. A fixture of the first and last bar time per instrument class for a sample of dates on
     both sides of 3 August 2026 and of 7 September 2026, cut from the store, and a test of
     the session calendar against it.
  2. Tests of `session_for()` for a holiday, a Saturday special session, a Muhurat session
     and the last day of a contract.
  3. The server tests run once more with `TZ=UTC` and once with `TZ=America/New_York`; the
     node time tests do the same (U-5).
  4. A test that every bar frame leaving the store has one time-zone type (D-7).
- Done when: these are in the gate, and moving a stock's close back to 15:30 in
  `config/sessions.yaml` makes the first test fail.

**T-6 (P1). The API has no contract test.**
- Measured: `create_app` defines 65 routes. All 39 GET routes are open; 25 others need the
  token; one POST is open (`/api/dhan/postback`, `api/server.py:969`). No route declares a
  response model. Ten test files use the test client and four send the token header. No test
  walks the routes.
- Fix: one test that walks `app.routes` and asserts, for every route: it is in a written
  table with its access class (public, read, write); a call without the right credential is
  refused (A-1); a GET changes nothing (A-2: the test compares a hash of the database before
  and after); the answer matches the declared model (A-9); every time in the answer carries
  `+05:30` (U-5); bad input gives 422, not 500 (A-6). A route that is added without a row in
  the table fails the test.
- Done when: the test is in the gate and the table has 65 rows or the new count.

**T-7 (P2). The trail is tested by example, not by property.** None of the 13 test files of
11 November uses `hypothesis`, though the suite already does elsewhere. The rules to state as
properties, for random bars and random line levels, in the Lab simulator, in `manage_step`
and in the book alike:
- the stop of a long trade never moves down and that of a short trade never moves up;
- a stop moved after bar N is first in force on bar N+1;
- a trade has exactly one exit, and its exit price is never better than the bar allowed;
- net result = gross result − charges, to the paisa, and the charges equal `CostModel.leg`;
- the same bars give the same trades whether they arrive one by one or all at once.
Done when: the five properties run in the gate with at least 200 examples each.

**T-8 (P2). The gate takes 8 minutes 45 seconds, and what is slow is not known.**
`pytest-xdist` is not installed, the `slow` marker is used five times, and nobody has listed
the slowest tests (`pytest --durations=25`; to verify where the time goes). A gate of nine
minutes is run less often than one of two. Fix: measure first; mark what is slow; give the
tests a database per worker and run them in parallel; keep a quick gate (`scripts/gate.sh
--quick`, without `slow` and `browser`) for use while working and the full gate before a
commit and in CI. The eight research scripts (about 4,000 lines) have no tests at all; their
shared engine should move into the package with tests when L-10 is done. Done when: the quick
gate runs in under three minutes and the full gate in under six on the owner's laptop.

---

## 14. The work plan

The plan has seven phases. Phase 0 needs no code. Phases 1 to 3 are in order of need: first the
screen must tell the truth, then the paper test must be worth trusting, then the app must run
without someone watching it. Phases 4 to 6 (interface, Lab, clean-up) can be reordered by the
owner.

Stories continue the backlog in `.agile/board.md`, whose highest number is US-033. A backlog
story is reused where it fits, with the findings it must now cover. New stories are US-034 to
US-066. Points use the board's scale (1, 2, 3, 5, 8). Every finding in this file is covered by a
story or by an owner decision; Appendix D gives the story for each finding.

| Phase | Goal | New stories | Backlog stories | Points |
|---|---|---|---|---|
| 0 | Today: restart, token, decisions, commit | 0 | 0 | 0 |
| 1 | The screen tells the truth | 9 | 1 | 37 |
| 2 | The paper test can be trusted | 8 | 5 | 61 |
| 3 | The app runs unattended | 3 | 10 | 49 |
| 4 | The interface | 4 | 3 | 36 |
| 5 | The Lab and research | 5 | 1 | 31 |
| 6 | Clean-up | 4 | 1 | 26 |
| Total | | 33 | 21 | 240 |

The board holds 27 open stories of 108 points. Twenty-one of them are placed above (89 points).
The other six are in section 14.9 with the reason.

### 14.1 How to work a story

1. Read the findings named in the story's "Covers" line. Their "Where", "Fix" and "Done when"
   lines are part of the story. The story text adds only scope and order.
2. Enter the story on the board the way the earlier ones were entered (a file in
   `.agile/stories/` and a row in `.agile/board.md`), with the "Accept" lines as its acceptance
   criteria.
3. Write the failing tests first, then the code, then run `bash scripts/gate.sh`. A story is done
   when its acceptance tests and the full gate are green.
4. Keep the ten rules of section 0.1. Three are easy to break by accident:
   - Rule 1: the invariants test forbids a list of identifiers. Name a session end `exit_time`
     or `SESSION_END`.
   - Rule 5: nothing in this plan may stop an alert or a paper trade unless section 15 puts the
     question to the owner and the owner says yes. The default is a label, never a gate.
   - Rule 8: no change makes any text smaller.
5. Never run a test or a script against the owner's running desk, against `data/onelot.db` in
   write mode, or with the owner's `.env`. Tests use a temporary database and the mock broker.
   Read the store with `mode=ro` (section 0.2).
6. Do not change the parameters of a running paper slot while doing other work. After US-037 a
   changed parameter starts a new scoreboard row and a new count toward 30 trades.
7. Line numbers in this file are those of 11 November. Find the function by its name.
8. New page code goes into small new files of pure functions with node tests (the first modules
   of C-1), not into `app.js`. That keeps the rewrite of Phase 4 small.
9. After a story that changes the desk, the paper book or the page, tell the owner to restart
   the desk (section 2.4). "Reload desk config" does not load new code.
10. Commit and push only when the owner asks (rule 9).

### 14.2 Phase 0: today, no code

1. **Restart the desk** with the commands of section 2.4. Then check three things that could not
   be checked on 11 November, because the old process was left running (to verify):
   - each instrument card shows six "Micro trend" readings once its daily history has loaded;
   - the log has no "unknown strategy" line, and the desk lists the `supertrend_mtf` slot on the
     five indices;
   - an open paper trade of that slot shows a value in the Trail column.
2. **Renew the Dhan token** in Settings before the open on every trading day until US-040 is
   built. The token in use ends at 06:29 on 12 November.
3. **Answer the questions of section 15.** A story that waits for an answer says "Needs the
   owner's decision".
4. **Commit the work of 11 November** when the owner is ready (Q-1; O-5 lists six commits). Until
   then the only copy is the working folder.

### 14.3 Phase 1: the screen tells the truth

Order: US-034, US-035, US-009, US-036, US-037, US-038, US-039, US-040, US-042, US-043. If the
owner agrees to switch the legacy engine off (G-2, Q-3), do steps 1 and 2 of US-063 before US-038:
four of the seven badges, the false alarms and the legacy state in the stream then need no work.

**US-034 (5 points, new). The session calendar is data, and every time of day comes from it.**
- Covers: D-1, P-3, C-4, U-6, D-9, T-5.
- Build:
  1. Read the exchange circulars named in Appendix C. Write `config/sessions.yaml`: for each
     instrument class and date range, the pre-open, the continuous session, the closing auction
     and the derivative session. Add the 2027 holidays when the exchange publishes them; the
     start-up warning names the first date that is not covered.
  2. `session_for(instrument, date)` in the calendar module is the only source of a session
     time. It replaces `EQUITY_SESSION`, the `exit_time` defaults, the hours in `/api/calendar`
     and the Lab's end-of-day rule (D-1 and C-4 list the places).
  3. Stock slots end at the last continuous minute minus a margin (15:10, unless the owner
     chooses the auction rule, Q-2). Index option slots follow the derivative session. The 20 stock
     paper trades already booked at 15:15 stay as booked; list them in the story notes so that
     the 30-trade review can set them aside (S-7).
  4. The server tells the page the session state, the next open and the next close
     (`/api/calendar` now, the heartbeat after US-038). The header clock ticks once a second
     and reads them.
- Accept: the "Done when" of D-1, C-4 and U-6; the tests of T-5; in a replay of a day after
  3 August 2026 a stock paper trade ends before 15:15 with `SESSION_END`; every backtest report
  states the session end it used.

**US-035 (3 points, new). The desk says when it has stopped polling.**
- Covers: F-2.
- Build: `last_poll_ok_at`, `last_poll_error`, `consecutive_poll_errors` and `poll_seconds` in
  the snapshot and in `/api/health`. The page works out the age once a second on a timer of its
  own, so a server that has died is caught as well. While the market is open and the age is
  above the limit of U-3 (twice the poll interval plus 10 seconds), a red banner is shown and
  the cards are dimmed. The third failure in a row raises one CRITICAL notification, and the
  recovery raises one.
- Accept: the "Done when" of F-2.

**US-009 (5 points, backlog). Schema migrations and backups.**
- Covers: D-8, C-6.
- Moves from "Should" to "Must" and comes before the first story that changes a stored record
  (US-036, US-037, US-046).
- Add to the story: a `schema_version` table and numbered migrations; a backup of the book
  tables before each migration; the expired-option tables declared in `store/schema.py`;
  `sqlite3.connect` called in one module; a daily backup of the small tables that cannot be
  rebuilt (paper book, alerts, journal, settings); a note on splitting the file in two (D-8).
- Accept: the "Done when" of C-6; a test restores the backup into an empty file and finds the
  paper trades equal.

**US-036 (5 points, new). Open paper trades and the real risk are on screen.**
- Covers: U-14, U-15, G-1, F-9.
- Build:
  1. `trade_view` adds the fields of U-14 for an open trade, from the position and the book's
     `STOP_MOVED` events.
  2. One "Open trades" table, used inside "Monitoring" on Home and on the Live Desk. Home loads
     the paper trades too.
  3. The risk line of U-15, from the paper book.
  4. Remove the "Risk governor" block from the cards and from Instrument focus, the risk badge,
     and the fields `risk_allowed`, `risk_reason` and `risk` from the alert and the snapshot.
     The Settings card "Risk (absolute rupees, one unit)" is labelled "legacy engine only"
     until US-063.
- Accept: the "Done when" of U-14, U-15 and G-1.

**US-037 (5 points, new). Every alert states the evidence for its own slot and settings.**
- Covers: S-1, F-3, P-6, U-9, F-11.
- Build:
  1. A `params_hash` over (strategy, timeframe, market, parameters) for each slot, computed at
     start and at reload, stored with every alert and every paper trade together with the
     parameters. A new hash starts a new scoreboard row.
  2. One evidence record per hash: the backtest of exactly those settings (trades, result per
     trade, the three periods, the real-premium result and its coverage for options, the date)
     and the paper record of that slot and hash, live grade only. A command
     (`python -m onelot slot-evidence`) runs the Lab for each slot of `config/desk.yaml` and
     stores the record. The desk never computes it during the session.
  3. Drop the blended confidence score and its JSON tooltip. If one label is kept, it comes
     from the evidence only: "untested", "negative", "unproven", "supported".
  4. The alert shows the record as its headline (S-1). A pending alert on Home is drawn open,
     with its buttons (the first step of U-8).
  5. The card of an index slot says "volume: not available for an index, skipped" (F-11).
- Accept: the "Done when" of F-3; changing one parameter of a slot starts a new scoreboard row
  with a count of 0; an alert of a slot with no record reads "No backtest for these settings".

**US-038 (5 points, new). One header row, one status pill, typed stream events.**
- Covers: U-2, U-3, U-4, A-8, F-12, G-4.
- Needs: US-035.
- Build:
  1. The stream sends typed events: `desk`, `paper`, `notification`, `heartbeat`. The
     heartbeat goes out every second with the server time, the last good poll and the session
     state. The desk's version changes only when its snapshot does (F-12).
  2. The pill with its seven states in the order of U-3, and the "System" panel behind it.
  3. The header becomes one row (section 11.1). Start-up warnings move into the panel.
  4. The connection banner, the `stale` class, one `try` per renderer and the client-error
     route of U-4.
  5. A closed market is a state of its own, not "DEGRADED" (G-4).
- Accept: the "Done when" of U-2, U-3 and U-4; after the first frame the stream carries no
  legacy state unless the legacy section is open.

**US-039 (2 points, new). Notifications carry an id and a time and are never replayed.**
- Covers: U-1.
- Build: as U-1. The id comes from the server (A-8); the page keeps the highest id it has
  announced.
- Accept: the "Done when" of U-1.

**US-040 (2 points, new). The end of the token is warned before the session.**
- Covers: O-2, O-11 (a renewed token must reach the desk's bar source; see the finding).
- Build: a check at start and once before the pre-open of every trading day (08:45 as in O-2;
  the time comes from the calendar of US-034 and a setting, not from a literal). If the token
  ends before the end of today's session: a red banner and one notification with the time in
  IST. The message never contains the token.
- Accept: the "Done when" of O-2.

**US-042 (3 points, new). One time format from the server to the page.**
- Covers: U-5, D-7.
- Build: one serializer on the server, always `+05:30`; `/api/sentiment` and
  `/api/sentiment/latest` use it. One file in the page with `fmtTime`, `fmtDateTime` and
  `fmtAge`; `tsf`, `hhmm` and the string cut at `app.js:696` are deleted. Column headings say
  "Time (IST)". The store normalises to UTC on write and converts once on read (D-7).
- Accept: the "Done when" of U-5; a bar frame leaving the store has one time-zone type.

**US-043 (2 points, new). Small fixes that should not wait.**
- Covers: F-1, A-2, A-3, L-7, A-1.
- Build: `reload` restores today's trade counts and cooldowns (F-1). The account refresh
  becomes a POST behind the token (A-2). The open route `/api/dhan/postback` is removed (A-3).
  The Lab form sends a zero as a zero (L-7). Until US-005 is done, a start with `--lan` is
  refused with a message that says why (A-1, point 3).
- Accept: the "Done when" of F-1; a GET of `/api/account` with `refresh=1` makes no broker call
  (the mock broker counts calls); a POST to the old postback path is refused; a node test
  types 0 into a numeric field of the Lab form and finds 0 in the request; a start with
  `--lan` ends with the message and opens no port.

### 14.4 Phase 2: the paper test can be trusted

Order: US-044, US-045, US-046, US-011, US-015, US-041, US-047 with US-031, US-048, US-049,
US-050, US-021, US-010. The first two are the base for the rest: a harness that runs the real
desk into the real paper book, and one copy of the stop and trail rules under a parity test.

**US-044 (5 points, new). An end-to-end harness: stored bars through `Desk.poll` into a real
paper book.**
- Covers: T-1, F-6, C-5.
- Build:
  1. One clock that is passed in. The desk, the trackers, the data source and the paper book
     take it, with the real clock as the default, and `Desk.poll(now)` takes the time as an
     argument (F-6). The 70-day cut in `InstrumentTracker.update_bars` counts back from that
     clock. The other files of C-5 (jobs, API, brokers) follow in US-064.
  2. `tests/e2e/`: a real `Desk` and a real `PaperBook` on a temporary database, a feed that
     plays stored one-minute bars, a fixed option chain, and the eight scenarios of T-1.
     Scenario 7 (a feed that goes delayed) is completed by US-046.
- Accept: the "Done when" of T-1 and F-6.

**US-045 (8 points, new). One implementation of stop, trail and exit, guarded by the parity
replay.**
- Covers: P-1, P-4, T-2, T-7.
- Needs: US-044.
- Build, in this order:
  1. The parity test of T-2 on a committed fixture, in the gate, before any rule is touched.
     Check once by hand that changing one comparison makes it fail, and note it in the story.
  2. The five properties of T-7.
  3. `manage_step` moves to a module that neither `desk` nor `paper` owns. The Lab simulator
     and the desk's taken positions call it (P-1, step 2). Time a Lab sweep before and after:
     it must not be slower by more than a tenth.
  4. The three points of P-4: after a restart the ratchet is rebuilt from the closed bars
     since the trade's `last_bar_ts`; the card shows the stop in force and the line as two
     numbers; every `STOP_MOVED` carries the close time of the bar that caused it.
- This absorbs backlog story US-025, with its fix to the costing of short positions.
- Accept: the "Done when" of P-1, T-2 and T-7; a test stops the desk for five bars in the
  middle of a trailing trade with `stop_buffer_atr` above zero, starts it again and finds the
  paper stop equal to the Lab's; every `STOP_MOVED` time is the close time of a bar.

**US-046 (3 points, new). Statistics count live-grade trades; an alert is not raised on data
older than its own validity.**
- Covers: F-5, S-9.
- Needs: the owner's decision Q-4 for the second half, because it holds alerts back.
- Build:
  1. The scoreboard, the progress count and every statistic use `grade="live"`, with a note of
     the number of delayed trades left out.
  2. A slot's `fresh` flag is judged against the clock, not against the time of the data.
  3. If the owner agrees (Q-4): while the data is older than `signal_valid_minutes` the slot
     raises no alert, and its card says "held: data N minutes old". If not: the alert is
     raised with the mark "delayed data", and its paper trade is graded `delayed` as today.
- Accept: the "Done when" of F-5; scenario 7 of T-1 asserts the behaviour the owner chose.

**US-011 (5 points, backlog). Paper analytics: evidence per strategy, instrument and
confidence.**
- Covers: P-5, U-11, P-8.
- Add to the story: one track record (P-5), which is the paper ledger by slot and
  `params_hash`, live grade. The alert archive keeps the owner's own actions (taken, skipped,
  expired) and loses its outcome estimates; the page no longer reads
  `audit.strategy_scoreboard`. The scoreboard shows the count toward 30 closed trades and the
  expected date (U-11, P-8). "By confidence" in the title becomes "by evidence label" after
  US-037.
- Accept: the "Done when" of U-11; for a closed trade the Home scoreboard, `/api/paper` and
  the archive show the same result (asserted in the harness of US-044).

**US-015 (5 points, backlog). Pre-registered promotion gate: live-grade vs experimental.**
- Covers: S-7, S-12.
- Needs: the owner's decision Q-5 (the result that would end a slot).
- Add to the story:
  1. A written review plan for each running slot, made before its 30th trade: what the review
     measures (fills against alert prices, slippage against the model, the exit mix, the
     adverse move in the minutes after an alert), what it cannot conclude (an edge of a few
     per cent: S-7 gives the numbers), and the result that would end the slot.
  2. A research log. Every tuning run records its date, the settings tried, how many they
     were, and the last date of the data used.
  3. Bars from 8 November 2026 onward are held out. The research engine stops a tuning run
     whose data goes past 7 November 2026 unless a flag is given, and logs the flag.
- Accept: a plan file exists for each running slot; a tuning run on held-out data without the
  flag stops with a message; the log gains one row per run.

**US-041 (8 points, new). Overnight carry for index-option paper trades.**
- Covers: S-2, P-2.
- Needs: the owner's decision Q-6; US-034, US-044, US-045.
- Scope: index option slots only, long calls and puts only, switched on per slot by a setting
  (the default stays same-day). Stocks stay same-day (the depository charge, section 3.3).
- Build:
  1. The Lab first. The simulator carries a trade past the session with the existing line
     trail on the closes of the trail timeframe, and a gap rule at the open: if the open is
     beyond the stop, the trade ends at the open. Run the slot's exact settings on real
     premiums and report the carried result beside the same-day result, with coverage.
  2. The contract. It is named at the alert and must have enough days left to its expiry (a
     slot setting; today `option_roll_days: 3` only skips a monthly contract that is about
     to expire). The paper trade never changes contract. A trade still open at the expiry
     ends at the end of that session, which is its one exit.
  3. The paper book. A carrying position survives the night and a restart, gets no
     `SESSION_END` before its expiry, is marked overnight at its last price with the time of
     that price, and meets the gap rule on the first bar of the next session.
  4. The page. The open-trades table shows "carried, day N" and the days left to expiry. The
     alert says that the trade may be held overnight and states the estimated loss at the
     stop and through a gap of a stated size. These are labels (rule 5).
  5. Notifications for a carried trade: each stop move and the exit, as for a same-day trade.
- Accept: the "Done when" of S-2; the harness gains a scenario over three sessions with a gap
  through the stop; no carried trade is open after the expiry of its contract.

**US-047 (5 points, new). Real premiums by default, with coverage on every option result.**
- Covers: S-3, L-3, D-10.
- With: backlog story US-031 (3 points), "Lab option runs do not slip the underlying before
  pricing the option".
- Build:
  1. Find out from the vendor's documents how far from the money an expired contract can be
     fetched (to verify; Appendix C). Store expired premiums so that one contract (expiry,
     strike, side) can be followed for the whole life of a trade, also when the index moves
     more than ten strikes away (D-10). The expiry is recorded at entry.
  2. The Lab prices an option trade from stored premiums first and from the model only where
     no price exists. Every option result states the share ("423 of 1,041 trades priced").
     Below 90% the result is labelled "modelled, unverified".
  3. The model uses the India VIX of the previous day. The help text that says "until the
     Dhan option-chain history is wired" is corrected. US-031 is done here.
- Accept: an option run in the Lab shows the coverage line; on a fixture of stored premiums
  the Lab uses the stored price, not the model's; changing today's VIX in a fixture does not
  change today's modelled premium.

**US-048 (5 points, new). A spread check at the alert; measured spreads in the Lab.**
- Covers: S-6.
- Build:
  1. Check the sampler in `src/onelot/desk/fillcost.py`: a quote that is old or has an empty
     side is dropped and counted, not averaged.
  2. At the alert the desk reads the quote of the contract it names. When the half-spread is
     above a limit (a setting, in per cent of premium), the alert is marked "wide spread"
     with the measured figure. This is a label.
  3. If the owner agrees (Q-7): the desk names the more liquid contract where one exists
     (`option_expiry: nearest` where a weekly exists).
  4. The Lab's cost model reads the measured half-spread per index and side, with the model
     value as a floor (L-4).
- Accept: an alert on a contract with a wide quote in the mock chain carries the mark and the
  figure; the Lab report states the slippage it used for each index.

**US-049 (5 points, new). The rupee risk of an alert against a budget; correlated indices as
one idea.**
- Covers: S-4, S-5, S-10.
- Needs: the owner's decisions Q-7, Q-8 and Q-9.
- Build:
  1. A setting `risk_budget_inr`, empty by default. With a budget set, an option alert shows
     its estimated loss at the stop for one lot beside the budget, and the mark "within
     budget" or "over budget". Nothing is blocked (rule 5).
  2. If the owner agrees (Q-7): the desk names the contract whose estimated loss fits the
     budget. Before that is switched on, test the choice of strike and expiry in the Lab on
     real premiums (US-047), because a cheaper contract also responds less.
  3. Groups of correlated indices as data in `config/desk.yaml` (NIFTY with SENSEX;
     BANKNIFTY with FINNIFTY; MIDCPNIFTY alone). Confirm the groups from the stored daily
     returns and put the correlations in the story notes. If the owner agrees (Q-8): one live
     idea per group, the member with the tighter measured spread first (US-048). If not: both
     alerts are raised and the later one says "same idea as NIFTY, open since 09:45".
  4. Every statistic counts sessions as well as trades (S-5).
  5. A signal that is not raised because another slot holds the instrument (S-10) is shown
     on the card with its time and the reason. By the owner's answer (Q-9) the slot re-arms
     on the same trend, or the signal stays lost.
- Accept: scenario 4 of T-1 asserts the behaviour the owner chose; an alert with a budget set
  shows the mark; the scoreboard has a column of sessions.
- Backlog story US-014 (alert confluence and dedup) can be closed into this one.

**US-050 (1 point, new). Stock slots: a practice label, or a pause.**
- Covers: S-8.
- Needs: the owner's decision Q-10.
- Build, by the owner's answer: the stock cards and alerts say "practice: one share cannot
  cover its charges" with the five-year figure of section 3.2; or the two stock slots are
  switched off in `config/desk.yaml` and their scoreboard rows stay with the note "paused on"
  and the date.
- Accept: the text is on the card, or the slots are absent from the desk's list after a
  restart and the rows are still on the scoreboard.

**US-021 (3 points, backlog). Verify the charge table against a real contract note.**
- Covers: L-4.
- Needs: the owner, with one contract note for each product (intraday equity, delivery
  equity, index options). The note stays on the owner's machine; only the rates go into
  `config/costs.yaml`.
- Add to the story: the delivery brokerage (Dhan publishes zero; to verify on the note); the
  depository charge as a fixed cost per stock per day on delivery sells; slippage per
  instrument from the measured table with the model value as a floor;
  `verified_against_contract_note: true` only after the comparison passes.
- Accept: a test computes the charges of the trades on the note to the paisa; the permanent
  warning is gone; a carried stock sale in the Lab shows the depository charge on a line of
  its own.

**US-010 (5 points, backlog). Integer-paise money in the manual journal and risk governor.**
- Covers: P-7.
- Change the scope: the journal moves to integer paise. The archive's `pnl_est` goes away
  with US-011. The risk governor's part is dropped if the owner switches the legacy engine
  off (Q-3, US-063); otherwise it stays in this story.
- Accept: no float holds rupees in the journal, checked by a test of the kind the paper book
  already has.

### 14.5 Phase 3: the app runs unattended

Order: US-006, US-024, US-013, US-008, US-005, US-051, US-019, US-052 with US-029, US-053,
US-007, US-026, US-033. The paper test needs months of sessions (P-8), so the first three
stories are about the desk staying up and the owner being reached.

**US-006 (3 points, backlog). Supervised service with crash recovery.**
- Covers: O-1, O-10.
- Add to the story: first repair the start command (O-10), because the agent calls it. Then a
  launchd agent with `KeepAlive`, started at login, installed by a script the owner runs (it
  holds no secret; the app reads `.env` as today). The desk records each session it covered,
  with the first and the last good poll. The scoreboard shows "sessions covered: 18 of 21".
  A start that follows an unclean stop raises one notification.
- Accept: the "Done when" of O-1 and of O-10.

**US-024 (3 points, backlog). Telegram settings, test message, status and quiet hours.**
- Covers: O-3.
- Needs: the owner (Q-11). The owner creates the bot with @BotFather and puts its token in
  `.env`. The token is never pasted into a chat, a log or the repository.
- Add to the story: the messages this plan creates go out by the same route: the stall and
  the recovery (US-035), the token warning (US-040), each stop move and exit of a paper
  trade, the start after an unclean stop (US-006). An alert message carries the evidence
  headline of US-037 and the time at which the alert expires.
- Accept: with a mock transport a test sees one message for an alert, one for its exit and
  one for a stall; the Settings tab says "connected" or gives the reason in words (U-26).

**US-013 (5 points, backlog). Telegram commands and inline Taken/Skip buttons.**
- Covers: O-3.
- Needs: US-024.
- Note: "Taken" and "Skip" record the owner's own action in the archive. No button and no
  command reaches a broker call (rule 1).
- Accept: as the story says; a test asserts that the handlers call no broker method.

**US-008 (3 points, backlog). Reproducible build and CI.**
- Covers: O-4.
- Add to the story: a lock file with exact versions and hashes; a workflow that runs
  `scripts/gate.sh` on every push and pull request (the gate needs no secret). The browser
  tests (US-018) and the coverage report (US-065) join the workflow when they exist.
- Accept: a fresh clone installs from the lock file and the gate passes in the workflow.

**US-005 (5 points, backlog). Reads require authentication whenever the app is reachable
off-loopback.**
- Covers: A-1, A-4, A-10.
- Add to the story: the four points of A-1 (no `/api/session`, a session cookie, no token in
  the script; a `Host` allow-list; `--lan` refused until the story is done; 39 open GET
  routes, not 37). From A-4: no token in a query string; a constant-time comparison if a
  token stays for scripts; a CORS policy and security headers. From A-10: media types for
  `.html`, `.ico` and `.map`; a favicon; `/api/docs` behind the session off loopback.
- Accept: the "Done when" of A-1; no test and no page request carries the token in a URL.

**US-051 (5 points, new). An end-of-day store update with a gap report.**
- Covers: D-2, D-6, O-8, L-11, F-10.
- Build:
  1. A job after the close (the session end comes from US-034), and at start for any day
     that is missing, appends the day's one-minute and daily bars from Dhan for every
     instrument. It writes a gap report: for each instrument, the sessions missing or short.
     It never runs while the market is open.
  2. The same job computes the quality report and the coverage table and stores them.
     `/api/data/quality` serves the stored result with its time (D-6), and
     `available_resolutions` reads the stored coverage (L-11).
  3. At start the desk loads its history from the store and asks the broker only for the
     missing days, in batches under the rate limit (O-8, F-10).
  4. The other messages of F-10: two feeds are compared only when both prices are from the
     current session (or the check goes with the legacy engine, US-063); a context tile with
     no data says "not available" and why.
  5. The Data tab shows "store complete to" and the date for each instrument.
- Accept: after a simulated session on the mock broker the store holds that day's bars and
  the gap report is empty; `/api/data/quality` answers in under one second; a start with a
  complete store makes no history request; the log of a normal start has no rate-limit
  warning.

**US-019 (5 points, backlog). Resumable five-year Dhan backfill.**
- Covers: D-3.
- Add to the story: one stored row per failed request (instrument, range, error); the job
  row opens to the list; an action "retry the failures".
- Accept: a backfill on the mock broker with three failing requests ends with three rows,
  and the retry clears them.

**US-052 (5 points, new). Corporate actions and excluded sessions are data, and every reader
gets the same bars.**
- Covers: D-4, D-5, F-8.
- With: backlog story US-029 (3 points), "Fallback bars never pollute a Dhan series; alerts
  report the resolution that served them".
- Build:
  1. A table of corporate actions and excluded sessions, each row with its source and the
     date it was added. The two demergers and the sessions now listed in `data/hygiene.py`
     move into it (through a migration, US-009).
  2. One read function that every reader of bars uses: the trackers, the Lab, the quality
     report, the charts and the correlation view. It returns the cleaned frame.
  3. The tracker cleans once per instrument and gives every slot the same frame (F-8). Each
     timeframe of the ladder says why it has no reading ("daily: 9 of 15 bars", "1m: failsafe
     feed has no 1-minute bars").
  4. The vendor is stored with every bar range, and one indicator window never mixes vendors
     (D-5). US-029 is done here.
  5. The quality report flags an unexplained overnight jump as a possible corporate action
     for the owner to confirm.
- Accept: on a fixture of ITC around 6 January 2025 the desk and the Lab give the same bars
  to every slot; a test over the syntax tree finds the bars tables read in one module only; a
  card with no reading names the reason.

**US-053 (5 points, new). Configuration is loaded once, checked, and shown.**
- Covers: F-4, C-7, U-26.
- Build:
  1. `load_all()` as in C-7, and `python -m onelot check-config`.
  2. Unknown keys are refused in every `StrategyParams` model, and every slot is validated
     before the desk uses the file (F-4). `reload` returns the list of problems and keeps the
     running configuration when the new one is invalid.
  3. The Settings tab as in U-26: a "Desk" card first (every slot with its settings, its
     `params_hash`, the time the file was loaded and any problem), then credentials and
     token, then notifications with the test result in words. Reload asks for confirmation
     in the page and says what it resets.
- Before unknown keys are refused, run `check-config` on the owner's files and correct what
  it finds, so that the first restart after this story does not stop the desk.
- Accept: the "Done when" of F-4 and C-7; the Settings tab shows every parameter value of
  every slot.

**US-007 (3 points, backlog). Observability: metrics, health and a system status panel.**
- Covers: O-7, O-9, C-8.
- Add to the story: the figures of O-7 (poll duration, time the lock is held, the feed's
  delay, database size, last backup, sessions covered) in the "System" panel of US-038; a
  named counter in every handler that swallows an error (C-8), shown in the same panel; at
  start, old log files are removed when older than 30 days, keeping the rotating engine log
  and the current output file (O-9).
- Accept: the "Done when" of C-8; the panel shows each figure with its time.

**US-026 (3 points, backlog). Desk poll never blocks on notifications; SQLite contention
bounded.**
- Covers: F-7.
- Add to the story: a timeframe's line is recomputed only when one of its bars closes; the
  work is done outside the lock and swapped in; the fingerprint includes a revision counter
  from the data layer.
- Accept: the "Done when" of F-7.

**US-033 (1 point, backlog). Dhan feed shuts down cleanly.**
- Covers: no finding of this review. It is placed here because a supervised app (US-006)
  stops and starts more often.
- Accept: as the story says.

### 14.6 Phase 4: the interface

Order: US-018 first, so that each screen is rewritten once (C-1). Then US-054, US-055, US-017,
US-016, US-056, US-057. Home keeps the owner's order unless the owner answers Q-14.

**US-018 (8 points, backlog). Frontend modules, accessibility and UI smoke tests.**
- Covers: C-1, A-5, U-27, T-3, U-12.
- Build, in this order:
  1. The browser tests of T-3 against the page as it is. They record today's measurements;
     each "Done when" number of section 11 becomes an assertion when its story lands.
  2. The modules of C-1, with no build step and no framework. The small files written in
     Phase 1 already have this shape. `dom.js` holds the one `html` template that escapes
     every value; `innerHTML` is used nowhere else (A-5).
  3. A content security policy that the page passes (A-5).
  4. The points of U-27: real buttons with `aria-expanded`, one visible focus style, the two
     live regions, toasts that cover nothing, one in-page confirmation dialog, a
     reduced-motion rule. Cards open with Tab and Enter and explain themselves without a
     tooltip (U-12).
- Accept: the "Done when" of C-1, A-5, U-27, T-3 and U-12.

**US-054 (8 points, new). Home and the Live Desk as session screens.**
- Covers: U-7, U-8, U-10, U-13, U-17, U-18.
- Needs: US-018; the owner's decision Q-14 for the part that changes Home.
- Build:
  1. The alert component of U-8, one for Home, the Live Desk and Instrument focus.
  2. The Live Desk in the order of section 11.1: pending alerts, open trades, one line of
     market context, the watch list. "Fill costs" moves to a "Costs" sub-tab; the owner's
     own fills go with the account blocks (U-17).
  3. Instrument focus opens beside the watch list (under it on a phone) and holds what the
     card cannot: the chart with the entry, the stop and each stop move, the slot's
     settings, and the alert with its buttons (U-18).
  4. The micro-trend strip as in U-13.
  5. Home, only as far as the owner agrees (Q-14). Without an answer Home keeps its order
     and gets the new components in their present places.
- Accept: the "Done when" of U-7, U-8 and U-13, and of U-10 if the owner accepts it.

**US-055 (2 points, new). The type scale and the dim colour.**
- Covers: U-20.
- Build: as U-20. No size goes down (rule 8): every size below 13 px goes up to 13 px or
  more, and the base of 17 px and the 26 px tiles stay.
- Accept: the "Done when" of U-20.

**US-017 (5 points, backlog). Mobile-responsive PWA, keyboard shortcuts and notification
sound.**
- Covers: U-21, U-28.
- Add to the story: the measurements of U-21 as acceptance criteria; from U-28, one message
  per failing route until it recovers, the mark "sound on / sound blocked: click to enable",
  and empty states that name the state first.
- Accept: the "Done when" of U-21; a node test polls a failing route ten times and finds one
  message.

**US-016 (5 points, backlog). Paper book view: positions, ledger and outcome of every alert.**
- Covers: U-16.
- Add to the story: pages of 50 from the server with filters by grade, slot and instrument
  as query parameters; a link from each trade to its alert and back; a legend for the
  grade. Keep the behaviour of `loadPaper` on a failed request, and give it to every loader.
- Accept: `/api/paper` sends one page of 50 trades, not the newest 200; a trade older than
  the newest 200 can be reached from the page; a node test follows the link both ways.

**US-056 (3 points, new). The Data and News tabs.**
- Covers: U-24, U-25, A-7.
- Needs: US-051 (the stored quality report and the freshness of the store).
- Build: the Data tab starts with the freshness summary of U-24; the quality report is a job
  with its last result and time; a backfill is confirmed in the page with the number of
  requests and the expected time; a running job redraws its own row only; the correlation
  table uses theme colours. The News tab draws each block when its own request answers; the
  pulse refresh is a POST; the scorer card names a separate key that may post scores and
  nothing else (after US-005); one line says whether any strategy on the desk reads a
  sentiment score (to verify in the code first). `/api/news` and `/api/sentiment/latest`
  filter in SQL with an index (A-7).
- Accept: `/api/news` with a symbol returns that symbol's older items when the newest N hold
  none; the browser test finds the freshness summary inside the first screen of the Data
  tab; a GET of the pulse makes no outside request.

**US-057 (5 points, new). API routers, an access table and a contract test.**
- Covers: C-2, A-6, A-9, T-6, L-8.
- Needs: US-005 for the access classes.
- Build: step 1 of C-2 (`create_app` becomes one router per area); response models for
  `/api/desk`, `/api/paper` and the stream events, then for the rest (A-9); request models
  that give 422 on bad input (A-6); the contract test of T-6 with its written table; the
  Lab lists paged on the server, the matrix as compact arrays, and job progress over the
  stream in place of the two-second poll (L-8).
- Accept: the "Done when" of T-6; `/api/lab/matrix` is under half of today's 1.83 MB for the
  same content; a test finds every field that the page reads in the declared models.

### 14.7 Phase 5: the Lab and research

Order: US-058, US-059, US-060, US-061, US-062, US-020. The Lab is where the next strategy idea
will be judged, so the aim of this phase is that a Lab result says honestly what it is worth
and can reach the paper test with its evidence attached.

**US-058 (5 points, new). The Lab drives `supertrend_mtf`; Optimize keeps the costs and the
fixed values.**
- Covers: L-1, L-2, U-22, C-3.
- Build:
  1. One module holds the timeframe names and their aliases, for the Lab, the desk, the
     strategies and the page (C-3). `60m` and `1h` mean the same wherever a timeframe is set.
  2. `param_docs` reports the allowed values of a `Literal` field, and the form draws a
     select (L-1). A parameter can be marked `locked` in its model: it is shown and never
     varied. For `supertrend_mtf`, `st_period` 14 and `st_mult` 1.7 are locked (the owner's
     values).
  3. `supertrend_mtf` gets a `SPACES` entry made of the structures that section 3 found
     worth watching.
  4. Optimize takes the same slippage, cost stress and sentiment gate as a single run, and
     can search a categorical setting (L-2).
  5. Each part of the Lab form fills when its own request answers, with "Loading…" while it
     waits and a retry line when it fails (U-22).
- Accept: the "Done when" of L-1, U-22 and C-3; a single run with the best settings of an
  optimisation gives the result the optimiser reported.

**US-059 (5 points, new). Honest verdicts, made on the server.**
- Covers: L-5, S-11, U-23.
- Needs: US-015 (the research log), US-047 (the priced share).
- Build:
  1. The run report gains the three periods, the no-signal benchmark for the same
     instrument and window, the t-statistic, the number of settings tried in the study the
     run belongs to, and for options the priced share.
  2. One tested function on the server gives the verdict, and `verdictOf` leaves the page.
     "POSITIVE" is given only when every period is positive after costs and the result
     beats the benchmark.
  3. The rest of U-23: parameter titles with the key beside them; the hint about premiums
     says what the run used; the filter that is always true is removed.
- Accept: a run that is positive overall and negative in one period is not "POSITIVE"; the
  report of an option run names its premiums and the priced share; a node test finds no
  verdict rule in the page.

**US-060 (3 points, new). "Send to paper test".**
- Covers: L-6.
- Needs: US-037 (the evidence record and `params_hash`), US-053 (checked configuration).
- Build: as L-6. The page shows the difference to `config/desk.yaml`, and the slot is
  written only after the owner confirms. The writer keeps the comments of the file. The
  evidence record is stored under the slot's hash. A running slot is never changed in
  place: other settings make a new slot row with a count of 0.
- Accept: a test sends a Lab result and finds the slot in the file with the same
  parameters, the comments of the file intact, the evidence record under the hash, and the
  rows of the other slots unchanged on the scoreboard.

**US-061 (5 points, new). The research engine moves into the package.**
- Covers: L-10.
- Build: a tested module in `src/onelot/lab/` with the structure grid, the period split,
  the benchmark, the t-statistic and the real-premium join. The eight scripts become thin
  callers, and `scripts/expired_options_fetch.py` moves the same way. The two checks of
  section 2.3 become tests: the charges equal `CostModel.leg`, and random structures give
  the same trades as a plain loop.
- Accept: no script holds a rule of its own; the two checks run in the gate; a study is
  started from a configuration file and writes its row in the research log (US-015).

**US-062 (5 points, new). Lab jobs run in a worker process.**
- Covers: L-9.
- Build: first time a desk poll during a sweep and record the figure (to verify the cost).
  Then a worker process with its own database connection, started, watched and stopped by
  the app. A sweep resumes from its finished runs after a restart. Job progress goes over
  the stream (US-057).
- Accept: a poll during a sweep is not slower than a poll without one by more than a
  tenth; a worker killed in the middle of a sweep finishes it after a restart without
  repeating a finished run.

**US-020 (8 points, backlog). Matrix sweep at scale with pre-registered gate.**
- Covers: no finding of its own.
- Needs: US-015, US-047, US-059, US-061, US-062.
- Add to the story: a sweep states its hypothesis and the number of settings before it
  runs (the research log), reports the benchmark and the three periods, and uses real
  premiums for options. Section 3 is the starting point: about 15,000 variants for each
  instrument group were tried and none was positive after costs in all three periods. A
  new sweep says what it tests that those did not.
- Accept: as the story says, with the report fields of US-059.

### 14.8 Phase 6: clean-up

Order: US-063 (earlier if the owner says yes to Q-3), US-064, US-065, US-066, US-022. US-065
can be done at any point.

**US-063 (5 points, new). The legacy engine can be switched off.**
- Covers: G-2, G-3, G-4, G-5, U-19.
- Needs: the owner's decisions Q-3 (off or on) and Q-13 (a "Pause alerts" switch).
- Build:
  1. `Engine` is split into an application host and the legacy engine (G-2).
  2. A setting `legacy_engine.enabled`. When it is false no legacy thread starts, the page
     has no "Bank Nifty option engine" section, no legacy badges and no Halt or Reset
     button, and the stream carries no legacy state.
  3. If the owner wants it (Q-13): "Pause alerts" as in G-3, kept across a restart, with a
     banner while it is on.
  4. If the owner keeps the engine on: its panels are drawn only while they are open and in
     the way the desk's are (G-5, U-19); two feeds are compared only inside the session, and
     a closed market is a state of its own (G-4); its two buttons say "legacy engine only"
     (G-3).
- Accept: the "Done when" of G-2; the invariants test passes (rule 1).

**US-064 (8 points, new). `Desk` and `PaperBook` keep only their own work.**
- Covers: C-2, C-5.
- Needs: US-044 and US-045. The harness and the parity test are what make this safe.
- Build:
  1. The test of C-2 with its allow-list, first.
  2. Out of `Desk`: the alert builder (contract choice, prices, the evidence line) and the
     snapshot builder, which then works outside the lock (F-7). `Desk` keeps the poll loop,
     the lock and the day's state.
  3. Out of `PaperBook`: the SQL, into a small repository.
  4. The clock of US-044 reaches the other files of C-5 (jobs, API, brokers).
- Accept: the "Done when" of C-2 and C-5; the scenarios of US-044 and the parity test pass
  without a change to their assertions.

**US-065 (5 points, new). The gate: speed, coverage, types.**
- Covers: T-4, T-8, C-10, C-11.
- Build: list the slowest tests first, then give each worker its own database and run in
  parallel, and add `scripts/gate.sh --quick` (T-8). Branch coverage as a report, then a
  floor for each package that may only rise (T-4). One Python version in the three places,
  ruff's `S` rules for `api` and `broker`, and fewer type errors in `lab/simulator.py` and
  `desk/` (C-10). The small things of C-11.
- Accept: the "Done when" of T-4 and T-8; the mypy baseline is below 195 and the ratchet
  holds the new number.

**US-066 (5 points, new). Legacy code out; names cleaned.**
- Covers: C-9, G-6.
- Needs: US-063, and one release that has run with the engine switched off.
- Build: re-point `cost_preview.py`; move `strategy/`, `trade/`, `risk/`, `replay/` and
  the legacy half of `engine.py` to `onelot/legacy/`, or delete them and keep a git tag;
  remove the Settings card of legacy limits and the legacy part of `config/config.yaml`;
  rename `store/` to `db/`; merge the two indicator modules; correct the old names in
  `scripts/start_mac.sh` and `pyproject.toml`.
- Accept: no import of the four packages is left outside `onelot/legacy/`; the gate
  passes; the mypy baseline falls by the errors of the files that left.

**US-022 (3 points, backlog). Operator runbook, release notes and 1.0 cut.**
- Covers: O-6, G-6.
- Add to the story: one runbook (start, stop, restart against reload, token renewal,
  backfill, what each banner and each state of the status pill means, what to do after a
  crash); the README and the Lab's help text corrected; a test that compares the counts in
  the README with the configuration; `docs/00` to `docs/13` marked as describing the
  earlier engine.
- Accept: the README test passes; every command in the runbook has been run once in a
  temporary folder.

### 14.9 Backlog stories that are not placed

| Story | Points | What to do with it |
|---|---|---|
| US-025 Desk manual positions adopt the shared rules | 3 | Absorbed by US-045. Close it with that story. |
| US-027 Swing carry rules for stocks | 3 | On hold. At one share a carried stock trade loses to the depository charge (section 3.3). Reopen if US-021 shows a different charge. |
| US-014 Alert confluence and dedup | 5 | The case of 11 November is covered by US-049. Close into it, or keep for stocks. |
| US-012 Paper vs taken reconciliation and slippage report | 3 | Keep. The review plan of US-015 uses the same figures, so do it before the first 30-trade review. |
| SP-001 Spike: review fnolab.in | 2 | Unchanged. |
| SP-002 Spike: Data API features | 3 | Unchanged. |

The plan leaves three things out on purpose: any change that lets the app send an order
(rule 1), any change to the settings of a running paper slot, and a merge into `main`, which
waits for the owner.

---

## 15. Decisions only the owner can make

Nothing in this section was decided when it was written on 11 November. The answers received
since then come first; the questions below them are kept as written.

**Answers received on 12 November 2026.**
- **Q-1, answered.** The work of 11 November is committed and pushed (`c47c9fc`). Standing
  instruction: when the weekly usage limit is near, commit and push everything verified.
- **Q-6, answered: no carry.** Options are intraday only. Whatever happens, every option
  trade ends the same day, calls or puts. US-041 is not to be built.
- **Q-2 and Q-10 change.** A stock trade may be delivery and may be held for as many
  sessions as its strategy asks; it need not be intraday. The paper book must then carry
  stock trades (US-027), and a delivery trade at one share pays a fixed ₹14.75 a sale.
- **The owner's first need** is a strategy with a real edge for delivery stocks and for
  intraday options. The search of 12 November is in
  `docs/research/ALPHA_SEARCH_2026-11-12.md`: no intraday option rule was found, and one
  stock rule (a pullback in an uptrend) is the candidate for the paper test.
- **Built on 12 November (the board's story 067).** The pullback rule runs in the paper
  test as delivery stock trades of about ₹10,000 (the number of shares nearest that
  amount, by the owner's decision): a rule read on daily closes, paper trades carried over
  sessions and restarts, and every other slot paused with the new slot key
  `enabled: false`. Its contract and design are the two files numbered 067 in
  `.agile/stories/`; the operator notes are in `docs/04_OPERATIONS.md`. It replaces the
  backlog's story on stock carry (section 14.9). Every strategy tested so far, with the
  figures that ruled it out, is listed in `docs/research/STRATEGIES_TESTED_2026-11-12.md`.
- **Final decisions of 13 November 2026, built the same day.** The desk now runs two rules
  and nothing else. Index options: Supertrend(14, 1.7) on 5m, 10m, 15m, 30m, 60m and 4h
  with the 3m trend as the trigger; a call when all seven point up, a put when all point
  down; out when 3 of the 7 have reversed, or at the session's exit. Its backtest is in
  `docs/research/MICROTREND_2026-11-13.md` (−0.24% of premium a trade: not an edge; the
  owner chose it as the one way to follow options, and its alerts say what the backtest
  found). Stocks: the pullback rule only. Every other slot is deleted from
  `config/desk.yaml`. A new tab, Machine Learning Lab (`src/onelot/mllab/`), shows five
  statistical forecasts of the next candle's close for every instrument on 15m, 30m, 60m,
  4h and 1d; it raises no alert. Finding O-11 is fixed: a renewed token now reaches the
  desk's bar source without a restart. The store is topped up for all 23 instruments.

Each question gives the options, what each one changes and
a recommendation with its reason. A story that depends on an answer names the question in its
"Needs" line. Until the owner answers, the plan keeps today's behaviour or adds a label, and it
never stops an alert (rule 5). The table at the end says what is built without an answer.

**Q-1. Commit and push the work of 11 November?**
- Why: O-5. 26 new files are staged and 16 changed files are unstaged, about 10,900 lines, on
  the branch `sprint-02-owner-feedback`. Nothing is committed and nothing is pushed. The
  working folder is the only copy.
- Options:
  - (a) commit now, in the six commits that O-5 lists, and push the branch;
  - (b) commit now, and push after the restart check of section 14.2;
  - (c) wait for the restart check, then commit and push.
- Recommendation: (b). The gate is green on this code, a commit can still be amended, and a
  folder that is not committed cannot be recovered.
- In every case: the history is scanned for secrets before a push and only locations are
  printed, and `main` stays untouched until the owner asks for a merge (rule 9).

**Q-2. When does a stock paper trade end?**
- Why: D-1, P-3. Since 3 August 2026 the F&O stocks trade continuously to 15:15 and the close
  is set in an auction after it (times to verify against the circulars, Appendix C). The desk
  still ends a stock paper trade at 15:15, a minute with no continuous trading.
- Options:
  - (a) 15:10, inside the continuous session with a margin. The exit is a one-minute price
    that the Lab and the paper book both have.
  - (b) an auction rule: the trade is held into the closing auction and booked at the daily
    close. That price differs from the last one-minute close by 0.33% on average (1.96% at
    most), and whether an intraday position may stay open into the auction depends on the
    broker's cut-off (to verify, Appendix C).
- Recommendation: (a). It is a price that could have been had, and it is the same rule in the
  Lab and on the desk.
- Waits: US-034, which builds (a) unless the owner chooses (b).

**Q-3. Switch the legacy Bank Nifty option engine off?**
- Why: G-2. On 11 November it evaluated 68 times and raised no signal. It sends about 3.5 KB
  of state to every open page each second, raised three CRITICAL notifications before the
  open, owns four of the seven header badges, and has about 3,800 lines of code that nothing
  else uses.
- Options:
  - (a) off by default behind a switch (US-063); the code is moved or deleted one release
    later, with a git tag (US-066);
  - (b) keep it on; its panels, messages and buttons are then made honest (US-063, step 4);
  - (c) delete it now.
- Recommendation: (a). The desk and the paper book are the product, and the switch keeps the
  change reversible for one release.
- Waits: US-063 and US-066. An early yes also makes US-038 smaller and takes the risk
  governor out of US-010.

**Q-4. Hold alerts while the data is late?**
- Why: S-9, F-5. On 5 and 6 November stock alerts went out on data about 15 minutes old. An
  alert is valid for 10 minutes, so these had expired before they were shown. 99 of the 147
  paper trades in the book are graded `delayed`.
- Options:
  - (a) hold: while the data is older than `signal_valid_minutes` a slot raises no alert, and
    its card says "held: data N minutes old";
  - (b) raise the alert with the mark "delayed data"; its paper trade is graded `delayed` and
    is left out of the statistics.
- Rule 5: holding is a check on the data, not a limit on risk, but it does stop an alert.
- Recommendation: (a). An alert that cannot be acted on, and a paper trade at a price that
  was no longer on offer, teach nothing.
- Waits: step 3 of US-046.

**Q-5. What result ends a paper slot?**
- Why: S-7, P-8. The owner's decision of 8 November stands: the option alerts stay until each
  slot has 30 closed trades (rule 6). At the backtest rates that is about 4, 9, 10 and 14
  months away for the four slots. Thirty trades give the mean to about ±10% of premium, so
  they cannot confirm or refute an edge of a few per cent.
- Options, which can be combined:
  - (a) a fill rule: the slot ends if the measured slippage against the alert price is above
    a stated multiple of the Lab's assumption, because no backtest of the slot then holds;
  - (b) a result rule: the slot ends if its mean after 30 trades is below a stated loss per
    trade;
  - (c) no rule: at 30 trades the slot runs on to 60 and is reviewed again.
- Recommendation: (a) and (b), with the owner's own numbers, written into the plan of US-015
  before the trades are counted. A rule that is written after the result is not a test.
- Waits: US-015.

**Q-6. Overnight carry for index-option paper trades?**
- Why: S-2, P-2. The owner's aim is to "capture the long horizon profits". Same-day, 75% to
  95% of trades end at the session close before the line is reached. The carried version
  showed +4.1% of premium a trade on modelled premiums and −9.1% on real premiums, with only
  41% of its trades priced (section 3.5): unverified, not disproved.
- What changes: a carried option can open beyond its stop after a gap, and the loss is then
  larger than the estimate at the alert. This is paper only (rule 1).
- Options:
  - (a) build US-041 in full; carry is a setting of a slot, and the default stays same-day;
  - (b) build the Lab step of US-041 first, and decide on the paper book after its report on
    real premiums;
  - (c) no carry.
- Recommendation: (b). The aim cannot be tested same-day. The only real-premium evidence so
  far is against carry, on thin coverage, so the Lab report should come before the paper
  book changes, and be repeated when US-047 has raised the priced share.
- Waits: steps 2 to 5 of US-041.

**Q-7. May the desk name a different contract?**
- Why: S-4, S-6. Today every index alert names the monthly contract at a delta of about 0.65
  (`option_expiry: monthly`), which is the contract the Lab models. Two measurements speak
  against one rule for all five indices. The estimated loss at the stop for one lot was
  ₹12,300 to ₹12,500 on 11 November (₹10,200 to ₹10,350 realised). The median half-spread is
  0.17% of premium for NIFTY calls, 2.3% for SENSEX calls and 24.2% for monthly SENSEX puts.
- Options:
  - (a) labels only: "over budget" and "wide spread" with the figures; the contract rule
    stays;
  - (b) for the spread: the desk names the nearest weekly where one exists (US-048, step 3);
  - (c) for the budget: the desk names the contract whose estimated loss fits
    `risk_budget_inr` (US-049, step 2).
- Recommendation: (a) now. A different contract is a different strategy with its own
  scoreboard row (US-037), so (b) and (c) should wait for a Lab test of that contract on real
  premiums (US-047).
- Waits: step 3 of US-048 and step 2 of US-049.

**Q-8. Are correlated indices one idea?**
- Why: S-5. On 11 November NIFTY and SENSEX alerted in the same second, at 09:45, and both
  were stopped out: −₹20,579 net for the two lots.
- Options:
  - (a) one live idea per group (NIFTY with SENSEX; BANKNIFTY with FINNIFTY; MIDCPNIFTY
    alone; the groups to be confirmed from the stored returns). The member with the tighter
    measured spread alerts, and the other card says "same idea as NIFTY, open since 09:45".
  - (b) both alerts are raised, and the later one carries that line.
- In both cases every statistic counts sessions as well as trades.
- Rule 5: (a) stops an alert.
- Recommendation: (a). Two alerts on one move are one decision, and counting them as two
  trades makes the paper record look larger than it is.
- Waits: step 3 of US-049.

**Q-9. Should a blocked index slot re-arm?**
- Why: S-10. Two slots now watch each index, and an index holds one live idea at a time. The
  slot that alerts first silences the other. `supertrend_mtf` takes one entry per trend, so
  its blocked signal is lost for that whole trend. The Lab tests each slot alone and does not
  see the block.
- Options:
  - (a) re-arm: when the index is free again and the trend still holds, the blocked slot may
    alert once;
  - (b) the signal stays lost, and the card shows it with its time and the reason.
- Recommendation: (b). A re-armed entry is later, and at another price, than the entry the
  Lab tested. With (b) the card explains why the paper record has fewer trades than the
  backtest.
- Waits: step 5 of US-049.

**Q-10. Stocks at one share: a practice label, or a pause?**
- Why: S-8. Over five years each of the two stock slots nets under ₹300. The gross edge is
  0.01% to 0.06% a trade against a round-trip cost of about 0.166%, so a scoreboard row for
  stocks reads ₹0 to ₹50 a trade whatever happens.
- Options:
  - (a) keep the alerts as practice, and say so on the cards and the alerts;
  - (b) pause the two stock slots; their scoreboard rows stay, with "paused on" and the date;
  - (c) leave them as they are.
- Recommendation: (b). The eighteen stock cards are 2,024 px of the Live Desk against 514 px
  for the five indices (Appendix B), and their record cannot show an edge at one share. A
  pause is one change in `config/desk.yaml` and is undone the same way.
- Waits: US-050.

**Q-11. Telegram?**
- Why: O-3. An alert is valid for ten minutes, and today it reaches the owner only at the
  laptop. The Telegram code is built and not configured.
- The owner's part: create a bot with @BotFather and put its token and the chat id in `.env`
  (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`). The token is never pasted into a chat, a log or
  a tracked file (rule 2).
- Options: (a) yes, US-024 and then US-013; (b) no, alerts stay on the page and in the macOS
  notification.
- Recommendation: (a). The stall banner, the token warning and every alert of this plan need
  a way to reach a phone.
- Waits: US-024 and US-013.

**Q-12. Store the login secrets for token self-renewal?**
- Why: O-2. The Dhan token lasts about a day and is renewed by hand. Self-renewal is in the
  code (`_maybe_auto_renew`, `engine.py:568`). It needs the account PIN and the TOTP secret
  in `.env` (`DHAN_PIN`, `DHAN_TOTP_SECRET`). With the client id, those are everything a
  login needs.
- Options:
  - (a) store nothing: renew by hand each morning, and US-040 warns before the session;
  - (b) store both in `.env`;
  - (c) store both in the macOS Keychain and read them only at renewal (a small new story).
- Recommendation: (a), with Telegram (Q-11) so that the warning reaches a phone. If the daily
  renewal becomes a burden, (c). Not (b): the app cannot place an order (rule 1), but a file
  that holds the whole login is not limited to the app.
- Waits: nothing.

**Q-13. A "Pause alerts" switch?**
- Why: G-3. "Halt" stops the legacy engine only; the desk never reads it. With the legacy
  engine off (Q-3), Halt and Reset go.
- Options:
  - (a) no switch;
  - (b) "Pause alerts": the desk keeps tracking and showing, raises no alert until the owner
    resumes, shows a banner while paused and keeps the state across a restart. Open paper
    trades are managed to their one exit as usual.
- Rule 5: a pause by the owner is not a risk governor, but it stops alerts.
- Recommendation: (b). It does what the Halt button says and does not do.
- Waits: step 3 of US-063.

**Q-14. Home: a compact portfolio row and an "Account" tab?**
- Why: U-10. The order of Home is the owner's decision of 7 November 2026: "My portfolio",
  alerts, "Monitoring" beside the scoreboard, the archive, the alert feed, then the ledger,
  the journal, the profile and the rest. Measured: a pending alert starts at y = 798 px on a
  1440×900 screen and at y = 2,150 px on a phone, which is 2.5 screens down.
- Options:
  - (a) no change to Home; the new components take the places of the old ones;
  - (b) the same order, with "My portfolio" as one compact row and "show all", and a tag on
    every block: "Dhan account" or "paper";
  - (c) as (b), and the broker blocks lower on the page (ledger, trade book, profile,
    positions, holdings) move to an "Account" tab;
  - (d) alerts above "My portfolio".
- Recommendation: (b), and (c) as well if the owner agrees. Both keep the owner's order and
  bring a pending alert onto the first screen (the "Done when" of U-10).
- Waits: step 5 of US-054.

| Question | Without an answer | What waits |
|---|---|---|
| Q-1 | Nothing is committed or pushed | The six commits of O-5 |
| Q-2 | Stock paper trades end at 15:10 | Nothing |
| Q-3 | The switch is built and left on | The removal (US-066); US-063 builds its step 4 |
| Q-4 | Alerts go out with the mark "delayed data" | Step 3 of US-046 |
| Q-5 | The plan of US-015 lists the measures and no end rule | The end rule |
| Q-6 | The Lab step of US-041 | Steps 2 to 5 of US-041 |
| Q-7 | Labels only | Step 3 of US-048, step 2 of US-049 |
| Q-8 | Both alerts, the later one marked | The group rule of US-049 |
| Q-9 | The signal stays lost and is shown on the card | Re-arming |
| Q-10 | The stock slots stay as they are | US-050 |
| Q-11 | No Telegram | US-024, US-013 |
| Q-12 | No secret is stored | Nothing |
| Q-13 | No switch | Step 3 of US-063 |
| Q-14 | Home keeps its order and its blocks | Step 5 of US-054 |

---

## Appendix A. Research outputs of 11 November 2026

Every "Measured" number in section 3 comes from one of these files. They are in
`data/research/mtf_2026-11-11/`, a folder that git ignores, so they exist only on the owner's
machine. The scripts are in `scripts/` (staged, not committed), and each one gives its own
command in its first lines. The written summary that git can track is
`docs/research/SUPERTREND_MTF_2026-11-11.md`.

| Output | Size | Script | What it holds | Used in |
|---|---|---|---|---|
| `bruteforce_analysis.txt` | 16 KB | `bruteforce_analysis.py` | The saved results of the retune of 7 November read again: for each strategy, timeframe and group, the share of settings positive after costs in each period | 3.1 |
| `deep_backtest.txt`, `deep/` | 54 KB | `deep_backtest_candidates.py` | The three paper slots over five years: before costs, at 1.5 and 2 times the costs, on the bars as stored, and with one setting changed at a time | 3.2 |
| `mtf_stage1.txt`, `mtf_stage1.csv`, `stage1/` | 123 KB, 2.1 MB | `mtf_bruteforce.py` | Stage 1: 5,760 readings of the owner's rule for each instrument | 3.3 |
| `mtf_stage2.txt`, `mtf_stage2.csv`, `stage2/` | 74 KB, 5.4 MB | `mtf_stage2.py` | Stage 2: 14,976 structures that put the two scoreboard strategies under the owner's logic | 3.3 |
| `mtf_robust.txt`, `robust/`, `robust_run.log` | 61 KB | `mtf_robust.py` | The finalists under stress (costs, late entry, the way the line is acted on, the VIX of the day before, six Supertrend settings) and trade by trade | 3.4 |
| `mtf_real_premiums.txt`, `real/` | 11 KB | `mtf_real_premiums.py` | The same trades priced with the stored premiums of expired contracts, with the priced share | 3.5 |
| `mtf_lab_check.txt`, `lab/` | 11 KB | `mtf_lab_check.py` | The strategy `supertrend_mtf` through the Lab simulator, which is the code the desk uses | 3.6, 2.2 |
| `mtf_desk_replay.txt`, `replay/` | 2 KB | `mtf_desk_replay.py` | The replay of stored one-minute bars through the desk's tracker and the paper book's rules, beside the Lab's trades | 2.3 |
| `NOTES.md` | 19 KB | written by hand | The request, the data problems found, the test written down before stage 2 ran, and a log of the day | all |

Three things to know before a script is run again:

- The scripts read `data/onelot.db` and the expired-option store, and they write only into
  `data/research/`. Stage 1 takes about 70 seconds for 23 instruments. The longer runs keep a
  log in their own folder.
- The period split is fixed in the scripts: TUNE 8 November 2021 to 7 November 2024, CHOOSE to
  7 November 2025, TEST to 7 November 2026. Bars from 8 November 2026 onward are the held-out
  sample (S-12), and no script reads past 7 November 2026.
- The earlier studies are `docs/research/CROSS_RETUNE_2026-11-07.md` (the 5,752 settings of
  section 3.1) and `docs/research/REAL_OPTIONS_2026-11-08.md` (how a trade is priced with a
  real premium, and the fill-cost sampler).

---

## Appendix B. Interface and API measurements

Measured on 11 November 2026 on the running desk with read-only requests and a headless
browser at two sizes: 1440×900 (a laptop) and 390×844 (a phone). "y" is the distance in CSS
pixels from the top of the page to the top of the block. The Settings tab was not opened. This
appendix holds no figure from the owner's broker account: the account blocks appear by
position and height only.

### B.1 Home

| Block | y at 1440×900 | Height | y at 390×844 | Height |
|---|---|---|---|---|
| Header | 16 | 213 (24% of the screen) | 10 | 476 (56% of the screen) |
| Start-up warning bar | 245 | 71 | 502 | 111 |
| First heading of Home | 336 | | 633 | |
| "My portfolio" tiles (`#acct-tiles`) | 392 | 254 | 762 | 1,139 |
| Alerts line (`#alerts-meta`) | 660 | | 2,014 | |
| Pending alerts (`#home-signals`) | 798 | 66, with no alert pending | 2,150 | 81 |
| Scoreboard (`#home-scoreboard`) | 936 | 236 | 2,472 | 312 |
| Monitoring (`#home-positions`) | 961 | | 2,328 | |
| Archive (`#home-archive`) | 1,263 | 984 | 2,900 | 2,602 |
| Alert feed (`#home-feed`) | 2,378 | 280 | 5,633 | |
| Ledger (`#acct-ledger`) | 2,749 | 561 | 6,004 | |
| Journal (`#home-journal`) | 3,401 | 251 | 7,004 | 455 |
| Holdings (`#acct-holdings`) | 4,038 | 243 | 7,983 | |

### B.2 Live Desk

| Block | y at 1440×900 | Height | y at 390×844 | Height |
|---|---|---|---|---|
| Open positions (`#desk-positions-card`) | 330 | 97 | 627 | 156 |
| Pending alerts (`#desk-signals`) | 441 | 0, with no alert pending | not recorded | |
| Paper trades (`#desk-paper-card`) | 441 | 221 | 797 | 324 |
| Fill costs (`#desk-fillcost-card`) | 676 | 1,481 | 1,134 | 1,645 |
| Data source line (`#desk-source`) | 2,194 | | 2,859 | |
| Market context (`#desk-context`) | 2,231 | 110 | 2,930 | 384 |
| Index cards (`#desk-indices`) | 2,428 | 514 | 3,460 | 524 |
| Stock cards (`#desk-stocks`) | 2,428, beside the indices | 2,024 | 4,109 | 2,064 |
| Instrument focus (`#focus-card`) | 4,485 | 767 | 6,206 | 1,282 |

### B.3 Page heights

| Tab | 1440×900 | Screens | 390×844 | Screens |
|---|---|---|---|---|
| Home | 4,399 | 4.9 | 8,345 | 9.9 |
| Live Desk | 5,430 | 6.0 | 7,717 | 9.1 |
| Lab | 1,250 | 1.4 | 1,582 | 1.9 |
| Data | 7,536 | 8.4 | 3,933 | 4.7 |
| News | 4,895 | 5.4 | 6,021 | 7.1 |

### B.4 Type, controls and contrast

| Measure | Value |
|---|---|
| Header badges | 7 |
| Toasts shown on load | 7 |
| Text elements under 13 px, of all text elements | Home 139 of 504; Live Desk 216 of 725; Lab 66 of 112; Data 27 of 1,174; News 33 of 774 |
| `font-size` declarations in px in `styles.css` | 68; 24 below 13 px; 34 at 13 px or below |
| Layout width on the phone (the screen is 390 px) | 413 px on Home, Data and News; 520 px on the Lab |
| Elements wider than the phone screen | Home 5; Live Desk 4; News 1 |
| Controls under 32 px tall on the phone | Home 4 of 15; Lab 1; Data 2; News 2 |

| Colour | Day theme | Night theme |
|---|---|---|
| `--text` | 18.5:1 | 13.91:1 |
| `--muted` | 7.07:1 | 7.58:1 |
| `--dim` | #7a879e: 3.63:1 on white, 3.21:1 on #eef1f7 | #75849f: 4.09:1 on #1b2439 |
| `--dim`, proposed (U-20) | #5f6c85: 5.29:1 and 4.67:1 | #8493ae: 4.98:1 on the panel, 5.87:1 on the background |

The level that body text should reach is 4.5:1.

### B.5 API and stream

| Request | Measured |
|---|---|
| `/api/data/quality` | 32.9 s |
| `/api/lab/leaderboard` | 1.45 s, 212 KB |
| `/api/lab/matrix` | 0.32 s, 1.83 MB |
| `/api/lab/runs` | 117 KB |
| `/api/lab/strategies` | 64 KB |
| `/api/paper` | 66 KB |
| Stream, first frame | 81,705 bytes, of which the desk snapshot is 65,807; 8 notifications |
| Stream, each second after that | about 3.5 KB of legacy engine state |

Times arrive in two forms. `/api/desk`, `/api/paper`, `/api/desk/signals`, `/api/lab/runs`,
`/api/data/jobs`, `/api/desk/fill-costs`, `/api/health` and the pulse send Indian time
(`+05:30`). `/api/sentiment` and `/api/sentiment/latest` send UTC (`+00:00`). Bars in the
database are UTC.

### B.6 Code size

| Measure | Value |
|---|---|
| Python package `src/onelot` | 134 files, 35,618 lines, 1,527 functions; 18 functions over 100 lines, 67 over 60 |
| Largest classes | `Desk` 1,981 lines and 70 methods; `PaperBook` 1,533 and 68; `Engine` 1,190 and 44 |
| Page | `index.html` 443 lines; `app.js` 1,527; `charts.js` 128; `styles.css` 432 |
| Routes in `create_app` | 65: 39 GET routes open, 25 other routes behind the token, one POST route open |
| `except Exception` | 145 times in 32 files |
| Type errors in the mypy baseline | 195 in 62 files |
| Tests | 209 files, 4,303 test functions, 7,754 cases, 8 minutes 45 seconds |

---

## Appendix C. Sources

The pages below were read on 11 November 2026 as search summaries. The exchange circulars
themselves were not opened. Every time of day in this file that rests on them is therefore
marked "to verify", and US-034 begins by reading the circulars.

### C.1 The Closing Auction Session and the new hours (D-1, P-3, Q-2)

What the summaries said:

- The Closing Auction Session began on 3 August 2026. Its first phase covers the stocks that
  have derivative contracts.
- A stock in the auction trades continuously from 09:15 to 15:15. The auction runs from 15:15
  to 15:35 and sets the closing price, in place of the average of the last half hour.
- Equity derivatives trade from 09:15 to 15:40. Before, they stopped at 15:30.
- Cash securities that are not in the auction trade to 15:30 as before.
- The pre-open session changed on 7 September 2026. Its new times were not read.
- Brokers close intraday positions at different times: Motilal Oswal at 15:00 for equity;
  Kotak Neo at 15:05 for cash and 15:25 for F&O; Dhan's intraday auto square-off is given as
  15:10 for equity and 15:25 for F&O.

The store agrees with the first three points: every F&O stock has 375 one-minute bars a day
up to 31 July 2026 and 360 a day, ending with the 15:14 bar, from 3 August 2026 (D-1).

| Source | Link |
|---|---|
| NSE, product page | https://www.nseindia.com/static/products-services/closing-auction-session |
| NSE circular | https://nsearchives.nseindia.com/content/circulars/CMTR75401.pdf |
| NSE circular | https://nsearchives.nseindia.com/content/circulars/CMTR75479.pdf |
| NSE circular | https://nsearchives.nseindia.com/content/circulars/CMTR75366.pdf |
| Zerodha bulletin, 23 January 2026 | https://zerodha.com/marketintel/bulletin/439989/introduction-of-closing-auction-session-cas-in-the-equity-cash-segment-and-certain-modifications-in-the-pre-open-auction-session |
| Motilal Oswal | https://www.motilaloswal.com/learning-centre/2026/8/introduction-of-closing-auction-session-cas-from-03-august-2026 |
| Kotak Neo | https://www.kotakneo.com/bulletins/closing-auction-session-explained/ |
| Groww | https://groww.in/blog/nse-extends-f-and-o-trading-hours-by-10-minutes-new-timings-effective-from-august-3-2026 |
| Angel One | https://www.angelone.in/news/market-updates/nse-extends-equity-derivatives-trading-hours-till-3-40-pm-starting-august-3-2026-set-to-introduce-closing-auction-session-in-equity-cash-segment |
| Dhan support: when the auction takes effect | https://dhan.co/support/general/market-session-status-and-timing/when-does-cas-become-effective/ |
| Dhan support: which securities are covered | https://dhan.co/support/general/market-session-status-and-timing/which-securities-are-covered-by-cas/ |
| Dhan support: intraday auto square-off times | https://dhan.co/support/general/market-session-status-and-timing/what-are-the-dhan-intraday-auto-square-off-timings/ |
| Dhan blog: auto square-off times | https://dhan.co/blog/stock-market/what-are-the-latest-auto-square-off-timings/ |

### C.2 Charges (L-4, US-021)

Dhan's depository charge on a delivery sale is given as ₹12.50 plus GST, which is ₹14.75, for
each stock sold from the demat account in a day. The sources do not agree on the amount, so
it is to be confirmed on a contract note before `config/costs.yaml` is called verified.

| Source | Link |
|---|---|
| Dhan community: depository charges explained | https://community.dhan.co/t/learn-with-dhan-dp-depository-charges-explained/10082 |
| Dhan community: update on the charge | https://madefortrade.in/t/update-on-dp-transaction-charges-by-dhan/44544 |

### C.3 The broker's data API (D-10, O-2, O-8, US-047)

The DhanHQ documents are at https://dhanhq.co/docs/v2/ . The pages used are: authentication
(the life of a token), historical data, expired options data, option chain, market quote,
live market feed, rate limits, annexure and instruments.

What was checked against the live API on 8 November 2026 is written down in
`docs/research/REAL_OPTIONS_2026-11-08.md`. Two of those facts set the limits of section 3.5:
the expired-option request gives strikes relative to the money, from ten strikes below to ten
above for index options, and it accepts a window of one day. Whether a contract further from
the money can be fetched in another way is the first step of US-047 (to verify).

### C.4 Holidays (D-9)

`config/nse_holidays.yaml` has the exchange list for 2026 and only the fixed-date national
holidays for 2027. The source to use for 2027 is the exchange's own holiday circular. The
pages below are secondary and were used only to cross-check 2026.

| Source | Link |
|---|---|
| ClearTax | https://cleartax.in/s/nse-holidays-2026 |
| Bajaj Broking | https://www.bajajbroking.in/nse-holidays |
| Upstox | https://upstox.com/stocks-market/nse-holidays/ |

---

## Appendix D. Index of findings

Every finding of sections 3 to 13 in the order of the file, with the story of section 14
that covers it and, where the owner must decide first, the question of section 15. A
finding that two stories share names both, in the order of the plan.

| ID | Severity | Finding | Section | Story; owner decision |
|---|---|---|---|---|
| S-1 | P0 | Say on every alert what its own evidence is | 3 | US-037 |
| S-2 | P1 | Build overnight carry for index-option paper trades (US-041) before judging the trail | 3 | US-041; Q-6 |
| S-3 | P1 | Make real premiums the default for option backtests, and store enough to price the winners | 3 | US-047 |
| S-4 | P1 | State and budget the rupee risk of one lot | 3 | US-049; Q-7 |
| S-5 | P1 | Treat correlated indices as one idea | 3 | US-049; Q-8 |
| S-6 | P1 | Check liquidity before naming a contract | 3 | US-048; Q-7 |
| S-7 | P1 | Decide now what the 30-trade review can and cannot conclude (extends US-015) | 3 | US-015; Q-5 |
| S-8 | P1 | Stocks at one share | 3 | US-050; Q-10 |
| S-9 | P2 | Do not alert on delayed data | 3 | US-046; Q-4 |
| S-10 | P2 | Details of the new slot | 3 | US-049; Q-9 |
| S-11 | P2 | Every report shows the no-signal benchmark and the number of settings tried | 3 | US-059 |
| S-12 | P2 | Protect the only clean test data | 3 | US-015 |
| F-1 | P1 | "Reload desk config" forgets today's trade counts and cooldowns | 4 | US-043 |
| F-2 | P1 | Nothing on screen says the desk has stopped polling | 4 | US-035 |
| F-3 | P0 | The confidence label and the "backtest" line of an alert are not evidence about the slot that raised it | 4 | US-037 |
| F-4 | P1 | Configuration mistakes are logged, not shown | 4 | US-053 |
| F-5 | P1 | Paper trades made on delayed or mixed data are counted with the live ones | 4 | US-046; Q-4 |
| F-6 | P2 | The desk reads the wall clock directly in many places | 4 | US-044 |
| F-7 | P2 | Work done inside the desk lock grows with the watch list | 4 | US-026 |
| F-8 | P2 | One instrument, two kinds of bars | 4 | US-052 |
| F-9 | P2 | Fields and labels left from the risk governor | 4 | US-036 |
| F-10 | P2 | Wrong or noisy messages at the start of the day | 4 | US-051 |
| F-11 | P2 | The strategy label promises a volume check that an index cannot have | 4 | US-037 |
| F-12 | P3 | The snapshot version moves without a new snapshot | 4 | US-038 |
| P-1 | P1 | Stop, trail and exit logic exists four times | 5 | US-045 |
| P-2 | P1 | A paper trade cannot live past the session | 5 | US-041; Q-6 |
| P-3 | P0, with D-1 | Stock paper trades are closed at a time when the stock no longer trades continuously | 5 | US-034; Q-2 |
| P-4 | P2 | Details of the line trail in the paper book | 5 | US-045 |
| P-5 | P1 | Three track records for the same alerts | 5 | US-011 |
| P-6 | P1 | A paper trade does not record the settings that produced it | 5 | US-037 |
| P-7 | P2 | Money as floating point outside the paper book | 5 | US-010 |
| P-8 | P2 | At the current rate the 30-trade review is far away | 5 | US-011; Q-5 |
| L-1 | P1 | The Lab cannot drive `supertrend_mtf` | 6 | US-058 |
| L-2 | P1 | Optimize ignores the costs set on the form and would tune the owner's fixed values | 6 | US-058 |
| L-3 | P1 | Option backtests use modelled premiums unless real ones are asked for | 6 | US-047 |
| L-4 | P1 | The cost table is unverified and misses two charges | 6 | US-021 |
| L-5 | P2 | A Lab verdict is given without the things that make it honest | 6 | US-059 |
| L-6 | P2 | There is no path from a Lab result to the desk | 6 | US-060 |
| L-7 | P2 | The form turns a zero into the default | 6 | US-043 |
| L-8 | P2 | Payloads and polling | 6 | US-057 |
| L-9 | P2 | Lab jobs run inside the live desk's process | 6 | US-062 |
| L-10 | P2 | The research of 11 November lives in eight scripts of about 4,000 lines | 6 | US-061 |
| L-11 | P3 | Small things in the Lab | 6 | US-051 |
| D-1 | P0 | The session calendar is out of date | 7 | US-034; Q-2 |
| D-2 | P1 | The store is not kept up to date | 7 | US-051 |
| D-3 | P1 | Backfill errors are reduced to a number | 7 | US-019 |
| D-4 | P1 | Known defects in the stored data are repaired only when bars are read | 7 | US-052 |
| D-5 | P2 | One series can hold bars from two vendors | 7 | US-052 |
| D-6 | P2 | Slow reads on the Data tab | 7 | US-051 |
| D-7 | P2 | A time-zone trap in the store | 7 | US-042 |
| D-8 | P2 | One 12 GB file holds everything, with no migrations and no backup | 7 | US-009 |
| D-9 | P2 | The holiday file has no exchange list for 2027 | 7 | US-034 |
| D-10 | P1 | Expired option prices are stored only near the money | 7 | US-047 |
| A-1 | P1 | Anyone who can reach the port can fetch the API token, and every read is open | 8 | US-043, US-005 |
| A-2 | P2 | A GET can make the app call the broker | 8 | US-043 |
| A-3 | P2 | An open POST writes whatever it is sent into the event log | 8 | US-043 |
| A-4 | P2 | Details of the token check | 8 | US-005 |
| A-5 | P2 | Text from outside reaches the page as HTML | 8 | US-018 |
| A-6 | P2 | Bad input gives a 500 | 8 | US-057 |
| A-7 | P2 | Two read routes do their filtering after the limit | 8 | US-056 |
| A-8 | P2 | The stream sends the legacy engine's state every second and replays old notifications | 8 | US-038 |
| A-9 | P2 | The API has no declared responses | 8 | US-057 |
| A-10 | P3 | Small things | 8 | US-005 |
| G-1 | P1 | Every card and the header show a risk governor that governs nothing | 9 | US-036 |
| G-2 | P1 | Two applications share one class and one process | 9 | US-063; Q-3 |
| G-3 | P1 | "Halt" does not halt the desk | 9 | US-063; Q-13 |
| G-4 | P2 | False alarms from the legacy checks | 9 | US-038, US-063 |
| G-5 | P2 | The legacy panels are rebuilt every second | 9 | US-063 |
| G-6 | P3 | Old names | 9 | US-066, US-022 |
| O-1 | P1 | The app runs in a terminal window, with nothing to restart it | 10 | US-006 |
| O-2 | P1 | The Dhan token runs out before the session and the app says so only in a badge | 10 | US-040; Q-12 |
| O-3 | P1 | An alert reaches the owner only at the laptop | 10 | US-024, US-013; Q-11 |
| O-4 | P1 | The gate runs only by hand, and builds are not reproducible | 10 | US-008 |
| O-5 | P1 | The work of 11 November exists only in one working folder | 10 | Q-1 |
| O-6 | P2 | Documents describe an earlier app | 10 | US-022 |
| O-7 | P2 | The app cannot tell the owner how it is doing | 10 | US-007 |
| O-8 | P2 | Start-up is noisy and close to the broker's rate limit | 10 | US-051 |
| O-9 | P3 | Old log files are never removed | 10 | US-007 |
| O-10 | P1 | The macOS start script cannot start the app | 10 | US-006 |
| O-11 | P1 | A token renewed in Settings does not reach the desk's bar source | 10 | US-040 |
| U-1 | P1 | Old notifications are replayed as toasts and beeps every time the page loads | 11 | US-039 |
| U-2 | P1 | The header takes a quarter of the screen, and most of its badges describe the legacy engine | 11 | US-038 |
| U-3 | P1 | One status pill and a "System" panel | 11 | US-038 |
| U-4 | P1 | The connection state sits in the footer, and drawing errors are swallowed | 11 | US-038 |
| U-5 | P1 | Times: UTC and IST are mixed, and almost no time names its zone | 11 | US-042 |
| U-6 | P2 | The clock redraws on every animation frame and works out the session in the browser | 11 | US-034 |
| U-7 | P1 | On the Live Desk the instruments start 2.7 screens down | 11 | US-054 |
| U-8 | P1 | The alert card does not put first what the owner needs in ten minutes | 11 | US-054 |
| U-9 | P2 | The confidence tooltip is raw JSON | 11 | US-037 |
| U-10 | P1 | Home starts with the broker account, and a pending alert appears at the bottom of the first screen | 11 | US-054; Q-14 |
| U-11 | P1 | The scoreboard does not show how far the paper test is | 11 | US-011 |
| U-12 | P2 | Instrument cards explain themselves only in tooltips, and open only with a mouse | 11 | US-018 |
| U-13 | P2 | The micro-trend strip of 11 November can be misread as a signal | 11 | US-054 |
| U-14 | P1 | An open paper trade shows no stop, no current price, no result and no stop moves | 11 | US-036 |
| U-15 | P1 | Show the real risk in place of the "Risk governor" block | 11 | US-036 |
| U-16 | P2 | Details of the paper trades table | 11 | US-016 |
| U-17 | P2 | Reference panels sit in the middle of the Live Desk | 11 | US-054 |
| U-18 | P2 | Instrument focus repeats the card and sends the owner elsewhere to act | 11 | US-054 |
| U-19 | P2 | The legacy engine section | 11 | US-063 |
| U-20 | P1 | A third of the text on the main screens is smaller than 13 px, against the owner's large-type rule | 11 | US-055 |
| U-21 | P2 | The phone layout is the laptop layout, stacked | 11 | US-017 |
| U-22 | P2 | The Lab's form is empty until three requests have all answered, with no loading state | 11 | US-058 |
| U-23 | P2 | Lab results and tables | 11 | US-059 |
| U-24 | P2 | The Data tab | 11 | US-056 |
| U-25 | P2 | The News tab | 11 | US-056 |
| U-26 | P2 | Settings | 11 | US-053 |
| U-27 | P2 | The page is built for a mouse and for good eyes | 11 | US-018 |
| U-28 | P3 | Small things | 11 | US-017 |
| C-1 | P2 | The page script is one file that builds the page by joining strings | 12 | US-018 |
| C-2 | P2 | Three classes and one function hold too much | 12 | US-057, US-064 |
| C-3 | P2 | The list of timeframes is written in at least eight places | 12 | US-058 |
| C-4 | P1 | The hours of the session are written in at least fifteen files | 12 | US-034 |
| C-5 | P2 | The clock is read directly in many places | 12 | US-044, US-064 |
| C-6 | P2 | Two database layers, a third connection, and no migrations | 12 | US-009 |
| C-7 | P2 | Configuration has six files, six places that parse them and no single check | 12 | US-053 |
| C-8 | P2 | 145 handlers catch every exception | 12 | US-007 |
| C-9 | P2 | Names that differ by one letter, and code that only the legacy engine uses | 12 | US-066 |
| C-10 | P3 | Type checking and lint are looser than they look | 12 | US-065 |
| C-11 | P3 | Small things | 12 | US-065 |
| T-1 | P1 | No test runs an alert from `Desk.poll()` into a real `PaperBook` and out again | 13 | US-044 |
| T-2 | P1 | The proof that the desk and the Lab agree is a script that was run once | 13 | US-045 |
| T-3 | P1 | Nothing tests the page in a browser | 13 | US-018 |
| T-4 | P2 | The gate does not measure coverage | 13 | US-065 |
| T-5 | P1 | No test ties the session calendar to the data, and time-zone tests are thin | 13 | US-034 |
| T-6 | P1 | The API has no contract test | 13 | US-057 |
| T-7 | P2 | The trail is tested by example, not by property | 13 | US-045 |
| T-8 | P2 | The gate takes 8 minutes 45 seconds, and what is slow is not known | 13 | US-065 |

127 findings: 4 at P0, 52 at P1, 63 at P2 and 8 at P3.

---
