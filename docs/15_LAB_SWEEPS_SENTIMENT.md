# 15 — Lab: saved runs, sweeps, leaderboard, walk-forward, sentiment gate

## Every run is saved

Each backtest, optimisation, walk-forward and sweep is a row in `backtest_runs` (config, metrics,
artifacts, trades in `backtest_trades`). The **Runs** sub-tab lists them; the **Leaderboard** ranks
them; `python -m onelot leaderboard` prints the same table. A later conversation can read the
table instead of re-running anything.

## Sweep (brute force)

`POST /api/lab/sweep` or the **Sweep** sub-tab or

```bash
.venv/bin/python -m onelot sweep --strategies all --instruments focus --timeframes 5m,15m,30m,60m
```

runs every strategy × every desk-watchlist instrument × every timeframe with default parameters,
saves each as an ordinary run tagged with the sweep id, then ranks them. 14 strategies × 15
instruments × 4 timeframes = 840 runs ≈ 20 minutes on the Yahoo corpus. Stop it any time; what
finished stays.

## Leaderboard score

`expectancy_R × √min(trades, 100)`, minus 0.5 when the profit factor is below 1; unranked below
`min_trades` (default 10). Walk-forward rows carry only out-of-sample trades and are flagged OOS —
prefer them over in-sample rows. The latest run per (kind, strategy, instrument, timeframe,
params, sentiment mode) is kept.

## Walk-forward — what it does and why yours "failed"

The data is cut into folds: optimise on an in-sample window, run those parameters untouched on
the next out-of-sample window, slide, repeat; only the stitched OOS trades are scored, next to
the **untuned defaults on the same windows**. The old defaults (40 sessions in / 10 out) could
not fit the ~40-session Yahoo corpus, produced zero folds and still said "done". Now the windows
are **auto-sized** to give at least 3 folds (`plan_windows`), the plan is printed on the result,
and fewer than 12 sessions is a hard error. You can still force windows with "40/10" in
*Advanced*. Verdict: PASS-like needs 30+ OOS trades, PF ≥ 1.2, expectancy ≥ 0.05 R.

## Strategies added in this round (classic books, re-implemented)

| name | origin | idea |
|---|---|---|
| `cpr_breakout` | Fisher, *The Logical Trader* (2002); Ochoa, *Secrets of a Pivot Boss* (2010) | narrow prior-day Central Pivot Range + close beyond it with ADX/volume; stop on the far side |
| `nr7_breakout` | Crabel (1990) | narrowest range of N sessions → trade the first break of yesterday's high/low |
| `ema_pullback` | Raschke & Connors, *Street Smarts* (1996) "Holy Grail" | ADX strong and rising, pullback touches the EMA, enter on resumption, stop under the swing |
| `turtle_soup` | Raschke & Connors, *Street Smarts* (1996) | fade a failed N-bar breakout; stop beyond the extreme, small target, time stop |

Plus the existing ten (two operator theses, six classic intraday setups, two documented
anomalies). None is believed until it passes the walk-forward gate; the Yahoo corpus is far too
short to retune defaults honestly, so defaults are unchanged until Dhan's 5-year 1-minute
history is loaded and a sweep + walk-forward is run on it.

## Sentiment gate

Scores live in `sentiment_scores` (−1…+1; a 0…1 scale is mapped). Sources today: the **pulse**
(rule-based: breadth, index trend, VIX percentile, NIFTY PCR once the Data API is on — recorded
every 15 minutes while the market is open), manual posts, and **bulk import** of your scorer's
history (`POST /api/sentiment/import`, or the form on the News & Sentiment tab).

In the Lab, *Sentiment gate* = `follow` (block longs while bearish, shorts while bullish),
`strict` (only with sentiment) or `contrarian`. The score used for a bar is the latest one at or
before that bar's close (no look-ahead) and not older than 5 days. The run records how many
entries were blocked. Sweeps accept the same option, so "strategy × instrument × timeframe ×
sentiment" can be brute-forced once 20 years of scores are imported.

## Charts

All charts are **TradingView Lightweight Charts v5**, vendored under `src/onelot/ui/vendor/`
(Apache-2.0, 198 KB) with the fonts, so the whole app, backtests included, runs with no internet.
Candles carry entry/exit markers; click a trade in the table to zoom to it with its entry/exit
lines. Equity and drawdown share one chart in two panes.

## Matrix: three settings per strategy

`lab/presets.py` derives, from each strategy's declared search space, three fixed settings chosen
before any result is seen: **low** (first choice of every entry knob), **default**, **high** (last
choice). Exit knobs stay at the defaults. A sweep with *three settings per strategy* ticked runs
strategies × instruments × timeframes × 3 (14 × 15 × 4 × 3 = 2520 runs) — every run saved.

The **Matrix** sub-tab (`GET /api/lab/matrix?sweep_id=`) groups the results strategy → instrument
→ rows of timeframe × setting, **sorted by win rate from highest to lowest**, and marks the best
row per instrument and the best instrument per strategy by the leaderboard score. Nothing in it
sees the future: signals are closed-bar, fills are next-open, the settings are fixed a priori.
Run it on the Yahoo corpus for a pipeline check only; the real matrix needs the five-year Dhan
history (Data tab → *Dhan: 5-year 1-minute history*).
