# Strategy Research Report

Generated 2026-10-27 17:14 IST by `python -m onelot research`.

## Data and method

Instruments: BANKNIFTY_OPT, BANKNIFTY_FUT, NIFTY_FUT. Data source: **yahoo**. Yahoo intraday history is capped (2m/5m/15m about the last 60 days, 60m about 2 years), so intraday results cover roughly 40 sessions and are a preliminary screen. Re-run on Dhan 1-minute history (5 years) before trusting any verdict.

* **One unit per trade**: one share (stocks) or one lot (futures / options / currency); full Indian charges and slippage included; option premiums are Black-76 synthetic (India VIX-scaled IV) until Dhan's expired-options history is wired.
* **Walk-forward**: parameters are optimised in-sample and scored only on the following, unseen out-of-sample (OOS) window; windows roll forward. **Only OOS numbers decide the verdict.**
* **Robust selection**: the best of the top-5 trials after averaging with jittered neighbours.
* **Gates (PASS)**: OOS trades >= 30, OOS profit factor >= 1.2, OOS expectancy >= 0.05R, still profitable with 2x costs, profitable parameter neighbourhood. WATCH = positive OOS but not yet robust; FAIL = no out-of-sample edge.

## Verdict summary

| Strategy | Instrument | TF | Verdict | OOS trades | OOS win % | OOS PF | OOS exp (R) | Net OOS (Rs) | 2x cost PF | Jitter exp (R) | MC DD p95 (Rs) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| ema_cross | BANKNIFTY_OPT | 5m | **FAIL** | 12 | 16.7 | 0.27 | -0.386 | -27,289 | 0.74 | -0.121 | 49,988 |
| supertrend_vol | BANKNIFTY_OPT | 5m | **FAIL** | 15 | 33.3 | 0.18 | -0.417 | -37,903 | 0.24 | -0.476 | 60,253 |
| supertrend_adx | BANKNIFTY_OPT | 5m | **FAIL** | 0 | - | - | - | - | - | -1.000 | - |
| orb | BANKNIFTY_OPT | 5m | **FAIL** | 13 | 30.8 | 0.52 | -0.364 | -22,781 | 0.55 | -0.806 | 56,979 |
| intraday_momentum | BANKNIFTY_OPT | 5m | **FAIL** | 12 | 25.0 | 0.09 | -0.524 | -17,875 | 0.19 | -0.643 | 27,371 |
| donchian_breakout | BANKNIFTY_OPT | 5m | **FAIL** | 11 | 27.3 | 0.19 | -0.380 | -18,436 | 0.15 | -0.501 | 31,065 |
| squeeze | BANKNIFTY_OPT | 5m | **FAIL** | 0 | - | - | - | - | - | -1.000 | - |
| benchmark_random | BANKNIFTY_OPT | 5m | **FAIL** | 11 | 27.3 | 0.05 | -0.651 | -33,600 | 0.64 | -0.237 | 47,193 |
| ema_cross | BANKNIFTY_OPT | 15m | **FAIL** | 4 | 50.0 | 0.48 | -0.287 | -8,375 | 0.27 | -0.367 | - |
| supertrend_vol | BANKNIFTY_OPT | 15m | **FAIL** | 9 | 22.2 | 0.29 | -0.420 | -33,991 | 0.54 | -0.380 | 64,557 |
| supertrend_adx | BANKNIFTY_OPT | 15m | **FAIL** | 0 | - | - | - | - | - | -1.000 | - |
| orb | BANKNIFTY_OPT | 15m | **FAIL** | 14 | 35.7 | 0.61 | -0.274 | -19,140 | 0.65 | -0.776 | 54,913 |
| intraday_momentum | BANKNIFTY_OPT | 15m | **FAIL** | 13 | 30.8 | 0.13 | -0.373 | -15,445 | 0.20 | -0.395 | 24,473 |
| donchian_breakout | BANKNIFTY_OPT | 15m | **FAIL** | 5 | 20.0 | 0.21 | -0.507 | -17,474 | 0.14 | -0.474 | 34,305 |
| squeeze | BANKNIFTY_OPT | 15m | **FAIL** | 0 | - | - | - | - | - | -1.000 | - |
| benchmark_random | BANKNIFTY_OPT | 15m | **FAIL** | 2 | 100.0 | - | +1.115 | 10,356 | 0.30 | -0.440 | - |
| ema_cross | BANKNIFTY_FUT | 5m | **FAIL** | 12 | 25.0 | 0.30 | -0.375 | -39,275 | 0.70 | -0.099 | 75,074 |
| supertrend_vol | BANKNIFTY_FUT | 5m | **FAIL** | 13 | 46.2 | 0.30 | -0.297 | -30,966 | 0.23 | -0.476 | 57,815 |
| supertrend_adx | BANKNIFTY_FUT | 5m | **FAIL** | 0 | - | - | - | - | - | -1.000 | - |
| orb | BANKNIFTY_FUT | 5m | **FAIL** | 12 | 25.0 | 0.45 | -0.481 | -39,355 | 0.50 | -0.822 | 86,837 |
| intraday_momentum | BANKNIFTY_FUT | 5m | **FAIL** | 12 | 25.0 | 0.15 | -0.527 | -22,330 | 0.15 | -0.703 | 37,097 |
| donchian_breakout | BANKNIFTY_FUT | 5m | **FAIL** | 11 | 27.3 | 0.26 | -0.353 | -23,524 | 0.16 | -0.496 | 43,407 |
| squeeze | BANKNIFTY_FUT | 5m | **FAIL** | 0 | - | - | - | - | - | -1.000 | - |
| benchmark_random | BANKNIFTY_FUT | 5m | **FAIL** | 11 | 27.3 | 0.09 | -0.648 | -48,017 | 0.58 | -0.226 | 69,533 |
| ema_cross | BANKNIFTY_FUT | 15m | **FAIL** | 4 | 50.0 | 0.54 | -0.277 | -11,258 | 0.27 | -0.363 | - |
| supertrend_vol | BANKNIFTY_FUT | 15m | **FAIL** | 9 | 22.2 | 0.30 | -0.410 | -51,061 | 0.51 | -0.377 | 98,737 |
| supertrend_adx | BANKNIFTY_FUT | 15m | **FAIL** | 0 | - | - | - | - | - | -1.000 | - |
| orb | BANKNIFTY_FUT | 15m | **FAIL** | 14 | 35.7 | 0.62 | -0.274 | -27,596 | 0.60 | -0.784 | 83,077 |
| intraday_momentum | BANKNIFTY_FUT | 15m | **FAIL** | 13 | 30.8 | 0.22 | -0.359 | -18,292 | 0.19 | -0.374 | 32,286 |
| donchian_breakout | BANKNIFTY_FUT | 15m | **FAIL** | 5 | 20.0 | 0.22 | -0.502 | -27,779 | 0.14 | -0.464 | 57,001 |
| squeeze | BANKNIFTY_FUT | 15m | **FAIL** | 0 | - | - | - | - | - | -1.000 | - |
| benchmark_random | BANKNIFTY_FUT | 15m | **FAIL** | 2 | 100.0 | - | +1.146 | 15,545 | 0.28 | -0.443 | - |
| ema_cross | NIFTY_FUT | 5m | **FAIL** | 11 | 54.5 | 1.81 | +0.135 | 18,927 | 0.48 | -0.375 | 29,491 |
| supertrend_vol | NIFTY_FUT | 5m | **FAIL** | 13 | 23.1 | 0.61 | -0.105 | -22,468 | 0.72 | -0.088 | 73,160 |
| supertrend_adx | NIFTY_FUT | 5m | **FAIL** | 0 | - | - | - | - | - | -1.000 | - |
| orb | NIFTY_FUT | 5m | **FAIL** | 13 | 38.5 | 0.66 | -0.244 | -19,122 | 1.22 | -0.608 | 66,514 |
| intraday_momentum | NIFTY_FUT | 5m | **FAIL** | 15 | 33.3 | 0.32 | -0.578 | -17,786 | 0.14 | -0.753 | 32,179 |
| donchian_breakout | NIFTY_FUT | 5m | **FAIL** | 13 | 23.1 | 0.42 | -0.427 | -31,021 | 0.77 | -0.138 | 66,577 |
| squeeze | NIFTY_FUT | 5m | **FAIL** | 0 | - | - | - | - | - | -1.000 | - |
| benchmark_random | NIFTY_FUT | 5m | **FAIL** | 12 | 16.7 | 0.10 | -0.783 | -56,099 | 0.38 | -0.379 | 75,929 |
| ema_cross | NIFTY_FUT | 15m | **FAIL** | 5 | 80.0 | 1.87 | +0.078 | 4,971 | 2.18 | +0.252 | 13,192 |
| supertrend_vol | NIFTY_FUT | 15m | **FAIL** | 8 | 37.5 | 0.31 | -0.317 | -36,642 | 0.29 | -0.201 | 74,841 |
| supertrend_adx | NIFTY_FUT | 15m | **FAIL** | 0 | - | - | - | - | - | -1.000 | - |
| orb | NIFTY_FUT | 15m | **FAIL** | 14 | 35.7 | 1.27 | +0.205 | 17,107 | 1.05 | -0.647 | 64,426 |
| intraday_momentum | NIFTY_FUT | 15m | **FAIL** | 12 | 25.0 | 0.30 | -0.420 | -15,445 | 0.17 | -0.336 | 30,387 |
| donchian_breakout | NIFTY_FUT | 15m | **FAIL** | 3 | 33.3 | 1.85 | +0.155 | 3,334 | 0.71 | -0.073 | - |
| squeeze | NIFTY_FUT | 15m | **FAIL** | 0 | - | - | - | - | - | -1.000 | - |
| benchmark_random | NIFTY_FUT | 15m | **FAIL** | 2 | 100.0 | - | +1.780 | 21,236 | 0.24 | -0.321 | - |

## Details

### ema_cross on BANKNIFTY_OPT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 12 out-of-sample trades (< 30); OOS profit factor 0.27 < 1.2; OOS expectancy -0.386R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 28 | 28.6 | 0.79 | -0.121 | 16/12 | -12,286 |
| Walk-forward OOS | 12 | 16.7 | 0.27 | -0.386 | 6/6 | -27,289 |
| Default params on the same OOS windows | 12 | 8.3 | 0.21 | -0.380 | 6/6 | -28,106 |
| Final params, full period (in-sample, optimistic) | 28 | 28.6 | 0.79 | -0.121 | 16/12 | -12,286 |
| Final params, costs x2 | 28 | 28.6 | 0.74 | -0.146 | 16/12 | -15,650 |
| Final params, slippage x2 | 25 | 24.0 | 0.43 | -0.301 | 14/11 | -38,766 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 1.70 | 4 | 0.00 | -0.510 | 0.00 |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 1.18 | 4 | 0.88 | -0.091 | 0.77 |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.92 | 4 | 0.00 | -0.559 | 0.00 |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 30,813, 95th pct Rs 49,988, probability of a losing run 96%.

Final parameters: `{"fast": null, "slow": null, "adx_min": null, "confirm_atr": null, "rsi_margin": null, "target_r": null, "trail_atr_mult": null}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 9 | 11.1 | -0.661 | 0.06 |
| 50-65 | 3 | 33.3 | +0.437 | 10.68 |

### supertrend_vol on BANKNIFTY_OPT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 15 out-of-sample trades (< 30); OOS profit factor 0.18 < 1.2; OOS expectancy -0.417R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 22 | 4.5 | 0.01 | -0.610 | 10/12 | -80,111 |
| Walk-forward OOS | 15 | 33.3 | 0.18 | -0.417 | 9/6 | -37,903 |
| Default params on the same OOS windows | 11 | 0.0 | 0.00 | -0.653 | 6/5 | -45,757 |
| Final params, full period (in-sample, optimistic) | 29 | 27.6 | 0.26 | -0.419 | 14/15 | -69,554 |
| Final params, costs x2 | 29 | 27.6 | 0.24 | -0.443 | 14/15 | -72,941 |
| Final params, slippage x2 | 29 | 24.1 | 0.13 | -0.563 | 14/15 | -95,389 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.82 | 5 | 0.40 | -0.165 | 0.00 |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.55 | 5 | 0.04 | -0.713 | 0.00 |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.51 | 5 | 0.24 | -0.375 | 0.00 |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 39,003, 95th pct Rs 60,253, probability of a losing run 100%.

Final parameters: `{"st_period": 14, "st_mult": 2.0, "adx_min": 12, "flip_window": 4, "rsi_long_min": 55, "target_r": 1.5, "trail_atr_mult": 1.5}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 13 | 30.8 | -0.461 | 0.17 |
| 50-65 | 2 | 50.0 | -0.136 | 0.28 |

### supertrend_adx on BANKNIFTY_OPT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 0 out-of-sample trades (< 30); OOS profit factor 0.00 < 1.2; OOS expectancy -1.000R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 0 | - | - | - | None/None | - |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 0 | - | - | - | None/None | - |
| Final params, costs x2 | 0 | - | - | - | None/None | - |
| Final params, slippage x2 | 0 | - | - | - | None/None | - |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | - | 0 | - | - | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | - | 0 | - | - | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | - | 0 | - | - | - |

Final parameters: `{"st_period": null, "st_mult": null, "adx_min": null, "flip_window": null, "max_vwap_dist_atr": null, "target_r": null, "trail_atr_mult": null}`

### orb on BANKNIFTY_OPT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 13 out-of-sample trades (< 30); OOS profit factor 0.52 < 1.2; OOS expectancy -0.364R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 13 | 30.8 | 0.52 | -0.364 | 4/9 | -22,781 |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 25 | 24.0 | 0.57 | -0.323 | 9/16 | -38,021 |
| Final params, costs x2 | 25 | 24.0 | 0.55 | -0.348 | 9/16 | -40,972 |
| Final params, slippage x2 | 27 | 22.2 | 0.44 | -0.497 | 10/17 | -63,064 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.43 | 6 | 1.14 | +0.069 | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.75 | 3 | 0.00 | -1.099 | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.65 | 4 | 0.41 | -0.464 | - |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 31,946, 95th pct Rs 56,979, probability of a losing run 88%.

Final parameters: `{"or_minutes": 15, "direction_mode": "first_candle", "stop_mode": "datr", "stop_datr": 0.1, "target_r": 2.5, "trail_atr_mult": 0.0}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 5 | 0.0 | -1.105 | 0.00 |
| 50-65 | 5 | 80.0 | +0.821 | 4.63 |
| 65-80 | 3 | 0.0 | -1.105 | 0.00 |

### intraday_momentum on BANKNIFTY_OPT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 12 out-of-sample trades (< 30); OOS profit factor 0.09 < 1.2; OOS expectancy -0.524R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 33 | 24.2 | 0.17 | -0.593 | 16/17 | -40,491 |
| Walk-forward OOS | 12 | 25.0 | 0.09 | -0.524 | 4/8 | -17,875 |
| Default params on the same OOS windows | 15 | 33.3 | 0.19 | -0.466 | 7/8 | -16,794 |
| Final params, full period (in-sample, optimistic) | 36 | 27.8 | 0.22 | -0.540 | 16/20 | -39,795 |
| Final params, costs x2 | 36 | 22.2 | 0.19 | -0.595 | 16/20 | -44,044 |
| Final params, slippage x2 | 36 | 19.4 | 0.07 | -0.754 | 16/20 | -66,296 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.23 | 5 | 0.07 | -0.697 | 0.07 |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.23 | 3 | 0.00 | -0.542 | 0.89 |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.23 | 4 | 0.16 | -0.296 | 0.13 |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 18,074, 95th pct Rs 27,371, probability of a losing run 100%.

Final parameters: `{"open_window_min": 30, "min_open_move_atr": 0.0, "stop_atr": 1.0, "require_prior_agree": false}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| 50-65 | 6 | 50.0 | -0.265 | 0.23 |
| 65-80 | 3 | 0.0 | -0.712 | 0.00 |
| 80+ | 3 | 0.0 | -0.856 | 0.00 |

### donchian_breakout on BANKNIFTY_OPT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 11 out-of-sample trades (< 30); OOS profit factor 0.19 < 1.2; OOS expectancy -0.380R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 11 | 27.3 | 0.19 | -0.380 | 3/8 | -18,436 |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 23 | 30.4 | 0.18 | -0.466 | 10/13 | -40,757 |
| Final params, costs x2 | 23 | 21.7 | 0.15 | -0.496 | 10/13 | -43,594 |
| Final params, slippage x2 | 13 | 15.4 | 0.07 | -0.674 | 3/10 | -37,675 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.11 | 5 | 0.65 | -0.114 | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.21 | 2 | 0.00 | -0.639 | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.31 | 4 | 0.00 | -0.582 | - |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 20,116, 95th pct Rs 31,065, probability of a losing run 99%.

Final parameters: `{"entry_len": 10, "exit_len": 5, "adx_min": 10, "trail_atr_mult": 2.0}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 4 | 25.0 | -0.354 | 0.00 |
| 50-65 | 5 | 40.0 | -0.182 | 0.53 |
| 65-80 | 2 | 0.0 | -0.927 | 0.00 |

### squeeze on BANKNIFTY_OPT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 0 out-of-sample trades (< 30); OOS profit factor 0.00 < 1.2; OOS expectancy -1.000R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 0 | - | - | - | None/None | - |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 0 | - | - | - | None/None | - |
| Final params, costs x2 | 0 | - | - | - | None/None | - |
| Final params, slippage x2 | 0 | - | - | - | None/None | - |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | - | 0 | - | - | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | - | 0 | - | - | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | - | 0 | - | - | - |

Final parameters: `{"min_squeeze_bars": null, "kc_mult": null, "dc_len": null, "target_r": null, "trail_atr_mult": null}`

### benchmark_random on BANKNIFTY_OPT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 11 out-of-sample trades (< 30); OOS profit factor 0.05 < 1.2; OOS expectancy -0.651R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 27 | 29.6 | 0.50 | -0.267 | 15/12 | -34,105 |
| Walk-forward OOS | 11 | 27.3 | 0.05 | -0.651 | 5/6 | -33,600 |
| Default params on the same OOS windows | 11 | 36.4 | 0.14 | -0.581 | 5/6 | -30,065 |
| Final params, full period (in-sample, optimistic) | 27 | 29.6 | 0.67 | -0.177 | 15/12 | -22,383 |
| Final params, costs x2 | 27 | 29.6 | 0.64 | -0.201 | 15/12 | -25,510 |
| Final params, slippage x2 | 27 | 25.9 | 0.49 | -0.314 | 15/12 | -39,858 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.60 | 5 | 0.05 | -0.634 | 0.13 |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.31 | 4 | 0.11 | -0.441 | 0.31 |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 1.40 | 2 | 0.00 | -1.111 | 0.00 |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 34,411, 95th pct Rs 47,193, probability of a losing run 100%.

Final parameters: `{"entry_prob": 0.02, "target_r": 2.5, "trail_atr_mult": 2.5}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 11 | 27.3 | -0.651 | 0.05 |

### ema_cross on BANKNIFTY_OPT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 4 out-of-sample trades (< 30); OOS profit factor 0.48 < 1.2; OOS expectancy -0.287R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 11 | 36.4 | 0.29 | -0.367 | 5/6 | -23,312 |
| Walk-forward OOS | 4 | 50.0 | 0.48 | -0.287 | 1/3 | -8,375 |
| Default params on the same OOS windows | 6 | 33.3 | 0.23 | -0.423 | 2/4 | -26,616 |
| Final params, full period (in-sample, optimistic) | 11 | 36.4 | 0.29 | -0.367 | 5/6 | -23,312 |
| Final params, costs x2 | 11 | 36.4 | 0.27 | -0.385 | 5/6 | -24,610 |
| Final params, slippage x2 | 11 | 27.3 | 0.18 | -0.448 | 5/6 | -31,475 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.58 | 3 | 1.24 | -0.030 | 0.41 |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.42 | 0 | - | - | 0.00 |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.60 | 1 | 0.00 | -1.061 | 0.00 |

Final parameters: `{"fast": null, "slow": null, "adx_min": null, "confirm_atr": null, "rsi_margin": null, "target_r": null, "trail_atr_mult": null}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| 50-65 | 4 | 50.0 | -0.287 | 0.48 |

### supertrend_vol on BANKNIFTY_OPT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 9 out-of-sample trades (< 30); OOS profit factor 0.29 < 1.2; OOS expectancy -0.420R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 13 | 38.5 | 0.58 | -0.124 | 3/10 | -17,608 |
| Walk-forward OOS | 9 | 22.2 | 0.29 | -0.420 | 2/7 | -33,991 |
| Default params on the same OOS windows | 6 | 50.0 | 0.56 | -0.097 | 3/3 | -12,500 |
| Final params, full period (in-sample, optimistic) | 16 | 31.2 | 0.57 | -0.206 | 4/12 | -24,840 |
| Final params, costs x2 | 16 | 31.2 | 0.54 | -0.221 | 4/12 | -26,745 |
| Final params, slippage x2 | 16 | 31.2 | 0.29 | -0.351 | 4/12 | -46,168 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.68 | 5 | 0.58 | -0.261 | 0.48 |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.57 | 2 | 0.00 | -0.521 | 0.84 |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.51 | 2 | 0.00 | -0.717 | - |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 39,987, 95th pct Rs 64,557, probability of a losing run 96%.

Final parameters: `{"st_period": 14, "st_mult": 2.0, "adx_min": 12, "flip_window": 4, "rsi_long_min": 55, "target_r": 1.5, "trail_atr_mult": 1.5}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 6 | 33.3 | -0.177 | 0.43 |
| 50-65 | 3 | 0.0 | -0.907 | 0.00 |

### supertrend_adx on BANKNIFTY_OPT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 0 out-of-sample trades (< 30); OOS profit factor 0.00 < 1.2; OOS expectancy -1.000R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 0 | - | - | - | None/None | - |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 0 | - | - | - | None/None | - |
| Final params, costs x2 | 0 | - | - | - | None/None | - |
| Final params, slippage x2 | 0 | - | - | - | None/None | - |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | - | 0 | - | - | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | - | 0 | - | - | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | - | 0 | - | - | - |

Final parameters: `{"st_period": null, "st_mult": null, "adx_min": null, "flip_window": null, "max_vwap_dist_atr": null, "target_r": null, "trail_atr_mult": null}`

### orb on BANKNIFTY_OPT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 14 out-of-sample trades (< 30); OOS profit factor 0.61 < 1.2; OOS expectancy -0.274R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 14 | 35.7 | 0.61 | -0.274 | 5/9 | -19,140 |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 24 | 29.2 | 0.68 | -0.219 | 10/14 | -24,770 |
| Final params, costs x2 | 24 | 29.2 | 0.65 | -0.244 | 10/14 | -27,622 |
| Final params, slippage x2 | 25 | 28.0 | 0.52 | -0.380 | 11/14 | -44,259 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.43 | 6 | 1.14 | +0.070 | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 1.09 | 4 | 0.29 | -0.599 | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.73 | 4 | 0.41 | -0.463 | - |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 30,678, 95th pct Rs 54,913, probability of a losing run 81%.

Final parameters: `{"or_minutes": 15, "direction_mode": "first_candle", "stop_mode": "datr", "stop_datr": 0.2, "target_r": 2.5, "trail_atr_mult": 2.0}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 6 | 16.7 | -0.704 | 0.22 |
| 50-65 | 8 | 50.0 | +0.049 | 1.13 |

### intraday_momentum on BANKNIFTY_OPT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 13 out-of-sample trades (< 30); OOS profit factor 0.13 < 1.2; OOS expectancy -0.373R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 33 | 30.3 | 0.17 | -0.410 | 16/17 | -40,910 |
| Walk-forward OOS | 13 | 30.8 | 0.13 | -0.373 | 5/8 | -15,445 |
| Default params on the same OOS windows | 15 | 33.3 | 0.16 | -0.360 | 7/8 | -19,527 |
| Final params, full period (in-sample, optimistic) | 36 | 36.1 | 0.24 | -0.260 | 16/20 | -37,376 |
| Final params, costs x2 | 36 | 27.8 | 0.20 | -0.287 | 16/20 | -41,629 |
| Final params, slippage x2 | 36 | 19.4 | 0.07 | -0.398 | 16/20 | -63,965 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.24 | 5 | 0.08 | -0.713 | 0.05 |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.19 | 4 | 0.25 | -0.135 | 0.89 |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.23 | 4 | 0.16 | -0.186 | 0.16 |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 15,892, 95th pct Rs 24,473, probability of a losing run 100%.

Final parameters: `{"open_window_min": 30, "min_open_move_atr": 0.0, "stop_atr": 1.5, "require_prior_agree": false}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 3 | 66.7 | -0.346 | 0.29 |
| 50-65 | 5 | 40.0 | -0.132 | 0.29 |
| 65-80 | 3 | 0.0 | -0.616 | 0.00 |
| 80+ | 2 | 0.0 | -0.650 | 0.00 |

### donchian_breakout on BANKNIFTY_OPT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 5 out-of-sample trades (< 30); OOS profit factor 0.21 < 1.2; OOS expectancy -0.507R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 5 | 20.0 | 0.21 | -0.507 | 1/4 | -17,474 |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 9 | 11.1 | 0.15 | -0.474 | 3/6 | -25,913 |
| Final params, costs x2 | 9 | 11.1 | 0.14 | -0.492 | 3/6 | -26,873 |
| Final params, slippage x2 | 9 | 11.1 | 0.11 | -0.561 | 3/6 | -32,101 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.00 | 3 | 0.73 | -0.140 | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.31 | 0 | - | - | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.36 | 2 | 0.00 | -1.058 | - |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 19,483, 95th pct Rs 34,305, probability of a losing run 94%.

Final parameters: `{"entry_len": 10, "exit_len": 5, "adx_min": 20, "trail_atr_mult": 0.0}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 1 | 0.0 | -0.554 | 0.00 |
| 50-65 | 3 | 33.3 | -0.307 | 0.42 |
| 65-80 | 1 | 0.0 | -1.059 | 0.00 |

### squeeze on BANKNIFTY_OPT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 0 out-of-sample trades (< 30); OOS profit factor 0.00 < 1.2; OOS expectancy -1.000R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 0 | - | - | - | None/None | - |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 0 | - | - | - | None/None | - |
| Final params, costs x2 | 0 | - | - | - | None/None | - |
| Final params, slippage x2 | 0 | - | - | - | None/None | - |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | - | 0 | - | - | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | - | 0 | - | - | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | - | 0 | - | - | - |

Final parameters: `{"min_squeeze_bars": null, "kc_mult": null, "dc_len": null, "target_r": null, "trail_atr_mult": null}`

### benchmark_random on BANKNIFTY_OPT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 2 out-of-sample trades (< 30); loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 14 | 35.7 | 0.32 | -0.440 | 5/9 | -29,214 |
| Walk-forward OOS | 2 | 100.0 | - | +1.115 | 0/2 | 10,356 |
| Default params on the same OOS windows | 2 | 100.0 | - | +1.115 | 0/2 | 10,356 |
| Final params, full period (in-sample, optimistic) | 14 | 35.7 | 0.32 | -0.440 | 5/9 | -29,214 |
| Final params, costs x2 | 14 | 35.7 | 0.30 | -0.465 | 5/9 | -30,877 |
| Final params, slippage x2 | 14 | 28.6 | 0.22 | -0.558 | 5/9 | -36,695 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.67 | 1 | inf | +0.269 | inf |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.09 | 1 | inf | +1.961 | inf |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.38 | 0 | - | - | - |

Final parameters: `{"entry_prob": null, "target_r": null, "trail_atr_mult": null}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 2 | 100.0 | +1.115 | inf |

### ema_cross on BANKNIFTY_FUT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 12 out-of-sample trades (< 30); OOS profit factor 0.30 < 1.2; OOS expectancy -0.375R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 28 | 32.1 | 0.85 | -0.099 | 16/12 | -13,075 |
| Walk-forward OOS | 12 | 25.0 | 0.30 | -0.375 | 6/6 | -39,275 |
| Default params on the same OOS windows | 12 | 16.7 | 0.24 | -0.359 | 6/6 | -39,573 |
| Final params, full period (in-sample, optimistic) | 28 | 32.1 | 0.85 | -0.099 | 16/12 | -13,075 |
| Final params, costs x2 | 28 | 32.1 | 0.70 | -0.184 | 16/12 | -28,848 |
| Final params, slippage x2 | 25 | 28.0 | 0.47 | -0.292 | 14/11 | -53,873 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 1.80 | 4 | 0.00 | -0.492 | 0.00 |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 1.22 | 4 | 0.85 | -0.106 | 0.79 |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 1.00 | 4 | 0.03 | -0.528 | 0.03 |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 44,931, 95th pct Rs 75,074, probability of a losing run 95%.

Final parameters: `{"fast": null, "slow": null, "adx_min": null, "confirm_atr": null, "rsi_margin": null, "target_r": null, "trail_atr_mult": null}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 9 | 11.1 | -0.670 | 0.07 |
| 50-65 | 3 | 66.7 | +0.509 | 38.73 |

### supertrend_vol on BANKNIFTY_FUT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 13 out-of-sample trades (< 30); OOS profit factor 0.30 < 1.2; OOS expectancy -0.297R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 22 | 18.2 | 0.02 | -0.606 | 10/12 | -118,163 |
| Walk-forward OOS | 13 | 46.2 | 0.30 | -0.297 | 9/4 | -30,966 |
| Default params on the same OOS windows | 11 | 9.1 | 0.00 | -0.643 | 6/5 | -67,412 |
| Final params, full period (in-sample, optimistic) | 29 | 27.6 | 0.28 | -0.410 | 14/15 | -102,374 |
| Final params, costs x2 | 29 | 27.6 | 0.23 | -0.490 | 14/15 | -118,699 |
| Final params, slippage x2 | 29 | 24.1 | 0.14 | -0.566 | 14/15 | -143,504 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.90 | 5 | 0.52 | -0.112 | 0.02 |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.59 | 3 | 0.07 | -0.463 | 0.00 |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.56 | 5 | 0.27 | -0.381 | 0.00 |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 34,823, 95th pct Rs 57,815, probability of a losing run 98%.

Final parameters: `{"st_period": 14, "st_mult": 2.0, "adx_min": 12, "flip_window": 4, "rsi_long_min": 55, "target_r": 1.5, "trail_atr_mult": 1.5}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 9 | 44.4 | -0.328 | 0.32 |
| 50-65 | 2 | 50.0 | -0.096 | 0.37 |
| 65-80 | 2 | 50.0 | -0.354 | 0.13 |

### supertrend_adx on BANKNIFTY_FUT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 0 out-of-sample trades (< 30); OOS profit factor 0.00 < 1.2; OOS expectancy -1.000R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 0 | - | - | - | None/None | - |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 0 | - | - | - | None/None | - |
| Final params, costs x2 | 0 | - | - | - | None/None | - |
| Final params, slippage x2 | 0 | - | - | - | None/None | - |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | - | 0 | - | - | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | - | 0 | - | - | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | - | 0 | - | - | - |

Final parameters: `{"st_period": null, "st_mult": null, "adx_min": null, "flip_window": null, "max_vwap_dist_atr": null, "target_r": null, "trail_atr_mult": null}`

### orb on BANKNIFTY_FUT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 12 out-of-sample trades (< 30); OOS profit factor 0.45 < 1.2; OOS expectancy -0.481R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 12 | 25.0 | 0.45 | -0.481 | 3/9 | -39,355 |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 25 | 28.0 | 0.57 | -0.341 | 9/16 | -57,872 |
| Final params, costs x2 | 25 | 24.0 | 0.50 | -0.423 | 9/16 | -71,941 |
| Final params, slippage x2 | 27 | 22.2 | 0.43 | -0.533 | 10/17 | -98,445 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.68 | 5 | 0.95 | -0.037 | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.76 | 3 | 0.00 | -1.165 | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.67 | 4 | 0.41 | -0.522 | - |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 54,416, 95th pct Rs 86,837, probability of a losing run 93%.

Final parameters: `{"or_minutes": 15, "direction_mode": "first_candle", "stop_mode": "datr", "stop_datr": 0.1, "target_r": 2.5, "trail_atr_mult": 0.0}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 5 | 0.0 | -1.167 | 0.00 |
| 50-65 | 4 | 75.0 | +0.893 | 4.04 |
| 65-80 | 3 | 0.0 | -1.168 | 0.00 |

### intraday_momentum on BANKNIFTY_FUT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 12 out-of-sample trades (< 30); OOS profit factor 0.15 < 1.2; OOS expectancy -0.527R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 33 | 24.2 | 0.24 | -0.633 | 16/17 | -51,465 |
| Walk-forward OOS | 12 | 25.0 | 0.15 | -0.527 | 4/8 | -22,330 |
| Default params on the same OOS windows | 15 | 33.3 | 0.28 | -0.470 | 7/8 | -19,769 |
| Final params, full period (in-sample, optimistic) | 26 | 23.1 | 0.25 | -0.672 | 12/14 | -36,007 |
| Final params, costs x2 | 26 | 19.2 | 0.15 | -0.927 | 12/14 | -50,627 |
| Final params, slippage x2 | 26 | 15.4 | 0.08 | -1.020 | 12/14 | -68,804 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.30 | 5 | 0.11 | -0.730 | 0.11 |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.30 | 3 | 0.00 | -0.570 | 1.33 |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.30 | 4 | 0.31 | -0.240 | 0.24 |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 22,992, 95th pct Rs 37,097, probability of a losing run 100%.

Final parameters: `{"open_window_min": 15, "min_open_move_atr": 1.0, "stop_atr": 0.75, "require_prior_agree": false}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| 50-65 | 6 | 50.0 | -0.205 | 0.43 |
| 65-80 | 3 | 0.0 | -0.770 | 0.00 |
| 80+ | 3 | 0.0 | -0.927 | 0.00 |

### donchian_breakout on BANKNIFTY_FUT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 11 out-of-sample trades (< 30); OOS profit factor 0.26 < 1.2; OOS expectancy -0.353R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 11 | 27.3 | 0.26 | -0.353 | 3/8 | -23,524 |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 23 | 30.4 | 0.23 | -0.460 | 10/13 | -53,947 |
| Final params, costs x2 | 23 | 21.7 | 0.16 | -0.558 | 10/13 | -66,862 |
| Final params, slippage x2 | 13 | 15.4 | 0.10 | -0.685 | 3/10 | -52,407 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.13 | 5 | 0.89 | -0.047 | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.27 | 2 | 0.00 | -0.631 | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.41 | 4 | 0.00 | -0.595 | - |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 26,694, 95th pct Rs 43,407, probability of a losing run 97%.

Final parameters: `{"entry_len": 10, "exit_len": 5, "adx_min": 10, "trail_atr_mult": 2.0}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 4 | 25.0 | -0.332 | 0.07 |
| 50-65 | 5 | 40.0 | -0.131 | 0.69 |
| 65-80 | 2 | 0.0 | -0.949 | 0.00 |

### squeeze on BANKNIFTY_FUT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 0 out-of-sample trades (< 30); OOS profit factor 0.00 < 1.2; OOS expectancy -1.000R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 0 | - | - | - | None/None | - |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 0 | - | - | - | None/None | - |
| Final params, costs x2 | 0 | - | - | - | None/None | - |
| Final params, slippage x2 | 0 | - | - | - | None/None | - |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | - | 0 | - | - | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | - | 0 | - | - | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | - | 0 | - | - | - |

Final parameters: `{"min_squeeze_bars": null, "kc_mult": null, "dc_len": null, "target_r": null, "trail_atr_mult": null}`

### benchmark_random on BANKNIFTY_FUT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 11 out-of-sample trades (< 30); OOS profit factor 0.09 < 1.2; OOS expectancy -0.648R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 27 | 37.0 | 0.53 | -0.254 | 15/12 | -47,811 |
| Walk-forward OOS | 11 | 27.3 | 0.09 | -0.648 | 5/6 | -48,017 |
| Default params on the same OOS windows | 11 | 36.4 | 0.18 | -0.580 | 5/6 | -42,897 |
| Final params, full period (in-sample, optimistic) | 27 | 37.0 | 0.69 | -0.166 | 15/12 | -31,583 |
| Final params, costs x2 | 27 | 25.9 | 0.58 | -0.249 | 15/12 | -46,835 |
| Final params, slippage x2 | 27 | 25.9 | 0.50 | -0.315 | 15/12 | -58,948 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.60 | 5 | 0.11 | -0.622 | 0.19 |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.32 | 4 | 0.15 | -0.420 | 0.37 |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 1.52 | 2 | 0.00 | -1.169 | 0.00 |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 49,579, 95th pct Rs 69,533, probability of a losing run 100%.

Final parameters: `{"entry_prob": 0.02, "target_r": 2.5, "trail_atr_mult": 2.5}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 11 | 27.3 | -0.648 | 0.09 |

### ema_cross on BANKNIFTY_FUT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 4 out-of-sample trades (< 30); OOS profit factor 0.54 < 1.2; OOS expectancy -0.277R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 11 | 36.4 | 0.33 | -0.363 | 5/6 | -33,410 |
| Walk-forward OOS | 4 | 50.0 | 0.54 | -0.277 | 1/3 | -11,258 |
| Default params on the same OOS windows | 6 | 33.3 | 0.25 | -0.406 | 2/4 | -40,273 |
| Final params, full period (in-sample, optimistic) | 11 | 36.4 | 0.33 | -0.363 | 5/6 | -33,410 |
| Final params, costs x2 | 11 | 36.4 | 0.27 | -0.420 | 5/6 | -39,612 |
| Final params, slippage x2 | 11 | 36.4 | 0.21 | -0.445 | 5/6 | -46,254 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.62 | 3 | 1.37 | -0.008 | 0.44 |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.48 | 0 | - | - | 0.00 |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.67 | 1 | 0.00 | -1.081 | 0.00 |

Final parameters: `{"fast": null, "slow": null, "adx_min": null, "confirm_atr": null, "rsi_margin": null, "target_r": null, "trail_atr_mult": null}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| 50-65 | 4 | 50.0 | -0.277 | 0.54 |

### supertrend_vol on BANKNIFTY_FUT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 9 out-of-sample trades (< 30); OOS profit factor 0.30 < 1.2; OOS expectancy -0.410R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 13 | 38.5 | 0.62 | -0.109 | 3/10 | -24,027 |
| Walk-forward OOS | 9 | 22.2 | 0.30 | -0.410 | 2/7 | -51,061 |
| Default params on the same OOS windows | 6 | 50.0 | 0.57 | -0.075 | 3/3 | -19,245 |
| Final params, full period (in-sample, optimistic) | 16 | 31.2 | 0.57 | -0.200 | 4/12 | -36,939 |
| Final params, costs x2 | 16 | 31.2 | 0.51 | -0.248 | 4/12 | -45,924 |
| Final params, slippage x2 | 16 | 31.2 | 0.31 | -0.343 | 4/12 | -69,118 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.67 | 5 | 0.58 | -0.264 | 0.49 |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.59 | 2 | 0.00 | -0.492 | 0.89 |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.55 | 2 | 0.00 | -0.693 | - |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 60,230, 95th pct Rs 98,737, probability of a losing run 96%.

Final parameters: `{"st_period": 14, "st_mult": 2.0, "adx_min": 12, "flip_window": 4, "rsi_long_min": 55, "target_r": 1.5, "trail_atr_mult": 1.5}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 6 | 33.3 | -0.148 | 0.44 |
| 50-65 | 3 | 0.0 | -0.935 | 0.00 |

### supertrend_adx on BANKNIFTY_FUT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 0 out-of-sample trades (< 30); OOS profit factor 0.00 < 1.2; OOS expectancy -1.000R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 0 | - | - | - | None/None | - |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 0 | - | - | - | None/None | - |
| Final params, costs x2 | 0 | - | - | - | None/None | - |
| Final params, slippage x2 | 0 | - | - | - | None/None | - |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | - | 0 | - | - | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | - | 0 | - | - | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | - | 0 | - | - | - |

Final parameters: `{"st_period": null, "st_mult": null, "adx_min": null, "flip_window": null, "max_vwap_dist_atr": null, "target_r": null, "trail_atr_mult": null}`

### orb on BANKNIFTY_FUT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 14 out-of-sample trades (< 30); OOS profit factor 0.62 < 1.2; OOS expectancy -0.274R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 14 | 35.7 | 0.62 | -0.274 | 5/9 | -27,596 |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 24 | 33.3 | 0.68 | -0.225 | 10/14 | -36,669 |
| Final params, costs x2 | 24 | 29.2 | 0.60 | -0.308 | 10/14 | -50,171 |
| Final params, slippage x2 | 25 | 28.0 | 0.52 | -0.404 | 11/14 | -69,015 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.45 | 6 | 1.21 | +0.124 | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 1.09 | 4 | 0.29 | -0.623 | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.74 | 4 | 0.41 | -0.522 | - |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 45,876, 95th pct Rs 83,077, probability of a losing run 80%.

Final parameters: `{"or_minutes": 15, "direction_mode": "first_candle", "stop_mode": "datr", "stop_datr": 0.2, "target_r": 2.5, "trail_atr_mult": 2.0}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 6 | 16.7 | -0.729 | 0.23 |
| 50-65 | 8 | 50.0 | +0.067 | 1.15 |

### intraday_momentum on BANKNIFTY_FUT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 13 out-of-sample trades (< 30); OOS profit factor 0.22 < 1.2; OOS expectancy -0.359R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 33 | 30.3 | 0.24 | -0.401 | 16/17 | -52,265 |
| Walk-forward OOS | 13 | 30.8 | 0.22 | -0.359 | 5/8 | -18,292 |
| Default params on the same OOS windows | 15 | 33.3 | 0.24 | -0.339 | 7/8 | -24,067 |
| Final params, full period (in-sample, optimistic) | 36 | 36.1 | 0.32 | -0.236 | 16/20 | -46,179 |
| Final params, costs x2 | 36 | 25.0 | 0.19 | -0.329 | 16/20 | -66,470 |
| Final params, slippage x2 | 36 | 22.2 | 0.11 | -0.387 | 16/20 | -88,188 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.33 | 5 | 0.13 | -0.758 | 0.08 |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.26 | 4 | 0.45 | -0.085 | 1.33 |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.29 | 4 | 0.31 | -0.134 | 0.31 |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 19,647, 95th pct Rs 32,286, probability of a losing run 99%.

Final parameters: `{"open_window_min": 30, "min_open_move_atr": 0.0, "stop_atr": 1.5, "require_prior_agree": false}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 3 | 66.7 | -0.356 | 0.53 |
| 50-65 | 5 | 40.0 | -0.062 | 0.50 |
| 65-80 | 3 | 0.0 | -0.634 | 0.00 |
| 80+ | 2 | 0.0 | -0.695 | 0.00 |

### donchian_breakout on BANKNIFTY_FUT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 5 out-of-sample trades (< 30); OOS profit factor 0.22 < 1.2; OOS expectancy -0.502R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 5 | 20.0 | 0.22 | -0.502 | 1/4 | -27,779 |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 9 | 11.1 | 0.16 | -0.464 | 3/6 | -40,041 |
| Final params, costs x2 | 9 | 11.1 | 0.14 | -0.526 | 3/6 | -45,070 |
| Final params, slippage x2 | 9 | 11.1 | 0.12 | -0.556 | 3/6 | -50,450 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.00 | 3 | 0.87 | -0.108 | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.36 | 0 | - | - | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.42 | 2 | 0.00 | -1.093 | - |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 31,663, 95th pct Rs 57,001, probability of a losing run 92%.

Final parameters: `{"entry_len": 10, "exit_len": 5, "adx_min": 20, "trail_atr_mult": 0.0}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 1 | 0.0 | -0.537 | 0.00 |
| 50-65 | 3 | 33.3 | -0.291 | 0.44 |
| 65-80 | 1 | 0.0 | -1.097 | 0.00 |

### squeeze on BANKNIFTY_FUT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 0 out-of-sample trades (< 30); OOS profit factor 0.00 < 1.2; OOS expectancy -1.000R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 0 | - | - | - | None/None | - |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 0 | - | - | - | None/None | - |
| Final params, costs x2 | 0 | - | - | - | None/None | - |
| Final params, slippage x2 | 0 | - | - | - | None/None | - |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | - | 0 | - | - | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | - | 0 | - | - | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | - | 0 | - | - | - |

Final parameters: `{"min_squeeze_bars": null, "kc_mult": null, "dc_len": null, "target_r": null, "trail_atr_mult": null}`

### benchmark_random on BANKNIFTY_FUT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 2 out-of-sample trades (< 30); loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 14 | 35.7 | 0.35 | -0.443 | 5/9 | -41,861 |
| Walk-forward OOS | 2 | 100.0 | - | +1.146 | 0/2 | 15,545 |
| Default params on the same OOS windows | 2 | 100.0 | - | +1.146 | 0/2 | 15,545 |
| Final params, full period (in-sample, optimistic) | 14 | 35.7 | 0.35 | -0.443 | 5/9 | -41,861 |
| Final params, costs x2 | 14 | 35.7 | 0.28 | -0.526 | 5/9 | -49,735 |
| Final params, slippage x2 | 14 | 28.6 | 0.24 | -0.571 | 5/9 | -54,233 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.79 | 1 | inf | +0.374 | inf |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.12 | 1 | inf | +1.917 | inf |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.41 | 0 | - | - | - |

Final parameters: `{"entry_prob": null, "target_r": null, "trail_atr_mult": null}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 2 | 100.0 | +1.146 | inf |

### ema_cross on NIFTY_FUT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 11 out-of-sample trades (< 30); loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 28 | 25.0 | 0.59 | -0.405 | 16/12 | -34,872 |
| Walk-forward OOS | 11 | 54.5 | 1.81 | +0.135 | 4/7 | 18,927 |
| Default params on the same OOS windows | 11 | 45.5 | 1.07 | -0.034 | 4/7 | 2,173 |
| Final params, full period (in-sample, optimistic) | 26 | 26.9 | 0.61 | -0.388 | 13/13 | -25,952 |
| Final params, costs x2 | 26 | 26.9 | 0.48 | -0.506 | 13/13 | -39,138 |
| Final params, slippage x2 | 22 | 22.7 | 0.32 | -0.618 | 11/11 | -53,485 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.31 | 3 | 0.31 | -0.394 | 0.00 |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.42 | 4 | inf | +0.941 | 4.34 |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 1.09 | 4 | 0.82 | -0.275 | 0.71 |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 13,365, 95th pct Rs 29,491, probability of a losing run 20%.

Final parameters: `{"fast": 9, "slow": 21, "adx_min": 25, "confirm_atr": 0.0, "rsi_margin": 0, "target_r": 0.0, "trail_atr_mult": 2.5}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 6 | 83.3 | +0.479 | 6.20 |
| 50-65 | 1 | 0.0 | -0.846 | 0.00 |
| 65-80 | 4 | 25.0 | -0.137 | 1.17 |

### supertrend_vol on NIFTY_FUT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 13 out-of-sample trades (< 30); OOS profit factor 0.61 < 1.2; OOS expectancy -0.105R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 29 | 31.0 | 0.50 | -0.237 | 14/15 | -55,245 |
| Walk-forward OOS | 13 | 23.1 | 0.61 | -0.105 | 7/6 | -22,468 |
| Default params on the same OOS windows | 16 | 31.2 | 0.43 | -0.191 | 9/7 | -41,474 |
| Final params, full period (in-sample, optimistic) | 27 | 37.0 | 0.85 | -0.064 | 11/16 | -12,978 |
| Final params, costs x2 | 27 | 33.3 | 0.72 | -0.138 | 11/16 | -26,636 |
| Final params, slippage x2 | 27 | 29.6 | 0.61 | -0.197 | 11/16 | -40,957 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.47 | 2 | 0.00 | -0.604 | 0.00 |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.84 | 4 | 1.43 | +0.300 | 1.18 |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.91 | 7 | 0.38 | -0.194 | 0.27 |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 38,008, 95th pct Rs 73,160, probability of a losing run 78%.

Final parameters: `{"st_period": 10, "st_mult": 3.0, "adx_min": 18, "flip_window": 4, "rsi_long_min": 52, "target_r": 2.5, "trail_atr_mult": 2.5}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 13 | 23.1 | -0.105 | 0.60 |

### supertrend_adx on NIFTY_FUT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 0 out-of-sample trades (< 30); OOS profit factor 0.00 < 1.2; OOS expectancy -1.000R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 0 | - | - | - | None/None | - |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 0 | - | - | - | None/None | - |
| Final params, costs x2 | 0 | - | - | - | None/None | - |
| Final params, slippage x2 | 0 | - | - | - | None/None | - |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | - | 0 | - | - | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | - | 0 | - | - | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | - | 0 | - | - | - |

Final parameters: `{"st_period": null, "st_mult": null, "adx_min": null, "flip_window": null, "max_vwap_dist_atr": null, "target_r": null, "trail_atr_mult": null}`

### orb on NIFTY_FUT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 13 out-of-sample trades (< 30); OOS profit factor 0.66 < 1.2; OOS expectancy -0.244R < 0.05R; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 13 | 38.5 | 0.66 | -0.244 | 4/9 | -19,122 |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 28 | 53.6 | 1.39 | +0.213 | 11/17 | 35,451 |
| Final params, costs x2 | 28 | 53.6 | 1.22 | +0.129 | 11/17 | 21,337 |
| Final params, slippage x2 | 28 | 42.9 | 0.86 | -0.093 | 12/16 | -15,519 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 1.11 | 6 | 1.20 | +0.123 | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 1.32 | 3 | 0.22 | -0.605 | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 1.26 | 4 | 0.41 | -0.523 | - |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 34,844, 95th pct Rs 66,514, probability of a losing run 78%.

Final parameters: `{"or_minutes": 15, "direction_mode": "first_candle", "stop_mode": "datr", "stop_datr": 0.1, "target_r": 2.5, "trail_atr_mult": 0.0}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 7 | 42.9 | -0.189 | 0.72 |
| 50-65 | 3 | 33.3 | -0.307 | 0.60 |
| 65-80 | 3 | 33.3 | -0.308 | 0.60 |

### intraday_momentum on NIFTY_FUT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 15 out-of-sample trades (< 30); OOS profit factor 0.32 < 1.2; OOS expectancy -0.578R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 30 | 13.3 | 0.11 | -0.887 | 14/16 | -53,753 |
| Walk-forward OOS | 15 | 33.3 | 0.32 | -0.578 | 7/8 | -17,786 |
| Default params on the same OOS windows | 15 | 20.0 | 0.16 | -0.740 | 8/7 | -25,222 |
| Final params, full period (in-sample, optimistic) | 31 | 25.8 | 0.24 | -0.546 | 16/15 | -39,916 |
| Final params, costs x2 | 31 | 16.1 | 0.14 | -0.740 | 16/15 | -55,576 |
| Final params, slippage x2 | 25 | 20.0 | 0.10 | -0.688 | 13/12 | -56,101 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.12 | 5 | 0.00 | -1.327 | 0.00 |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.17 | 6 | 0.50 | -0.334 | 0.19 |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.21 | 4 | 0.78 | -0.007 | 0.34 |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 19,960, 95th pct Rs 32,179, probability of a losing run 98%.

Final parameters: `{"open_window_min": 45, "min_open_move_atr": 0.5, "stop_atr": 1.5, "require_prior_agree": false}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 3 | 33.3 | -0.423 | 0.67 |
| 50-65 | 5 | 40.0 | -0.719 | 0.26 |
| 65-80 | 6 | 16.7 | -0.753 | 0.04 |
| 80+ | 1 | 100.0 | +0.712 | inf |

### donchian_breakout on NIFTY_FUT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 13 out-of-sample trades (< 30); OOS profit factor 0.42 < 1.2; OOS expectancy -0.427R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 1 | 100.0 | - | +2.499 | 1/0 | 11,114 |
| Walk-forward OOS | 13 | 23.1 | 0.42 | -0.427 | 5/8 | -31,021 |
| Default params on the same OOS windows | 1 | 100.0 | - | +2.499 | 1/0 | 11,114 |
| Final params, full period (in-sample, optimistic) | 26 | 38.5 | 0.92 | -0.066 | 9/17 | -6,541 |
| Final params, costs x2 | 26 | 34.6 | 0.77 | -0.186 | 9/17 | -19,654 |
| Final params, slippage x2 | 11 | 36.4 | 0.68 | -0.173 | 5/6 | -11,747 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 2.01 | 5 | 3.23 | +0.442 | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 2.44 | 6 | 0.00 | -0.920 | inf |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.97 | 2 | 0.00 | -1.116 | - |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 40,585, 95th pct Rs 66,577, probability of a losing run 91%.

Final parameters: `{"entry_len": 10, "exit_len": 20, "adx_min": 10, "trail_atr_mult": 3.0}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| 50-65 | 5 | 40.0 | -0.371 | 0.42 |
| 65-80 | 7 | 14.3 | -0.355 | 0.52 |
| 80+ | 1 | 0.0 | -1.207 | 0.00 |

### squeeze on NIFTY_FUT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 0 out-of-sample trades (< 30); OOS profit factor 0.00 < 1.2; OOS expectancy -1.000R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 0 | - | - | - | None/None | - |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 0 | - | - | - | None/None | - |
| Final params, costs x2 | 0 | - | - | - | None/None | - |
| Final params, slippage x2 | 0 | - | - | - | None/None | - |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | - | 0 | - | - | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | - | 0 | - | - | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | - | 0 | - | - | - |

Final parameters: `{"min_squeeze_bars": null, "kc_mult": null, "dc_len": null, "target_r": null, "trail_atr_mult": null}`

### benchmark_random on NIFTY_FUT (5m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 12 out-of-sample trades (< 30); OOS profit factor 0.10 < 1.2; OOS expectancy -0.783R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 27 | 29.6 | 0.42 | -0.356 | 15/12 | -59,143 |
| Walk-forward OOS | 12 | 16.7 | 0.10 | -0.783 | 6/6 | -56,099 |
| Default params on the same OOS windows | 12 | 33.3 | 0.14 | -0.670 | 6/6 | -48,076 |
| Final params, full period (in-sample, optimistic) | 27 | 25.9 | 0.45 | -0.341 | 15/12 | -56,723 |
| Final params, costs x2 | 27 | 22.2 | 0.38 | -0.425 | 15/12 | -70,386 |
| Final params, slippage x2 | 27 | 18.5 | 0.35 | -0.492 | 15/12 | -81,343 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.69 | 4 | 0.24 | -0.565 | 0.41 |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.36 | 5 | 0.06 | -0.878 | 0.06 |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 1.07 | 3 | 0.00 | -0.915 | 0.02 |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 57,422, 95th pct Rs 75,929, probability of a losing run 100%.

Final parameters: `{"entry_prob": 0.02, "target_r": 2.5, "trail_atr_mult": 2.5}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 12 | 16.7 | -0.783 | 0.10 |

### ema_cross on NIFTY_FUT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 5 out-of-sample trades (< 30).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 11 | 36.4 | 0.65 | -0.209 | 6/5 | -13,779 |
| Walk-forward OOS | 5 | 80.0 | 1.87 | +0.078 | 2/3 | 4,971 |
| Default params on the same OOS windows | 6 | 50.0 | 1.18 | +0.100 | 2/4 | 3,658 |
| Final params, full period (in-sample, optimistic) | 11 | 63.6 | 2.55 | +0.285 | 5/6 | 33,553 |
| Final params, costs x2 | 11 | 63.6 | 2.18 | +0.228 | 5/6 | 28,041 |
| Final params, slippage x2 | 10 | 50.0 | 1.50 | +0.070 | 4/6 | 14,320 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.00 | 4 | 1.19 | +0.016 | 0.39 |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.16 | 1 | inf | +0.329 | 4.66 |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 2.30 | 0 | - | - | - |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 5,724, 95th pct Rs 13,192, probability of a losing run 26%.

Final parameters: `{"fast": 5, "slow": 34, "adx_min": 25, "confirm_atr": 0.3, "rsi_margin": 3, "target_r": 2.5, "trail_atr_mult": 1.5}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 1 | 100.0 | +0.329 | inf |
| 50-65 | 3 | 66.7 | -0.023 | 0.94 |
| 65-80 | 1 | 100.0 | +0.131 | inf |

### supertrend_vol on NIFTY_FUT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 8 out-of-sample trades (< 30); OOS profit factor 0.31 < 1.2; OOS expectancy -0.317R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 16 | 37.5 | 0.70 | -0.070 | 7/9 | -19,641 |
| Walk-forward OOS | 8 | 37.5 | 0.31 | -0.317 | 5/3 | -36,642 |
| Default params on the same OOS windows | 9 | 33.3 | 0.48 | -0.156 | 5/4 | -23,757 |
| Final params, full period (in-sample, optimistic) | 18 | 27.8 | 0.35 | -0.288 | 8/10 | -53,113 |
| Final params, costs x2 | 18 | 16.7 | 0.29 | -0.351 | 8/10 | -62,216 |
| Final params, slippage x2 | 18 | 16.7 | 0.29 | -0.365 | 8/10 | -68,104 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.72 | 3 | 0.35 | -0.351 | 1.05 |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 1.42 | 4 | 0.49 | -0.100 | 0.49 |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.67 | 1 | 0.00 | -1.079 | 0.00 |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 44,258, 95th pct Rs 74,841, probability of a losing run 94%.

Final parameters: `{"st_period": 14, "st_mult": 2.0, "adx_min": 12, "flip_window": 4, "rsi_long_min": 55, "target_r": 1.5, "trail_atr_mult": 1.5}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 4 | 25.0 | -0.585 | 0.08 |
| 50-65 | 4 | 50.0 | -0.048 | 0.67 |

### supertrend_adx on NIFTY_FUT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 0 out-of-sample trades (< 30); OOS profit factor 0.00 < 1.2; OOS expectancy -1.000R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 0 | - | - | - | None/None | - |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 0 | - | - | - | None/None | - |
| Final params, costs x2 | 0 | - | - | - | None/None | - |
| Final params, slippage x2 | 0 | - | - | - | None/None | - |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | - | 0 | - | - | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | - | 0 | - | - | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | - | 0 | - | - | - |

Final parameters: `{"st_period": null, "st_mult": null, "adx_min": null, "flip_window": null, "max_vwap_dist_atr": null, "target_r": null, "trail_atr_mult": null}`

### orb on NIFTY_FUT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 14 out-of-sample trades (< 30); parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 14 | 35.7 | 1.27 | +0.205 | 5/9 | 17,107 |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 26 | 50.0 | 1.20 | +0.116 | 12/14 | 18,154 |
| Final params, costs x2 | 26 | 50.0 | 1.05 | +0.032 | 12/14 | 5,026 |
| Final params, slippage x2 | 25 | 48.0 | 1.08 | +0.046 | 11/14 | 7,026 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 1.34 | 5 | 1.85 | +0.598 | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 1.46 | 5 | 1.38 | +0.265 | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 2.11 | 4 | 0.59 | -0.360 | - |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 29,447, 95th pct Rs 64,426, probability of a losing run 39%.

Final parameters: `{"or_minutes": 15, "direction_mode": "first_candle", "stop_mode": "datr", "stop_datr": 0.1, "target_r": 2.5, "trail_atr_mult": 0.0}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 8 | 37.5 | +0.217 | 1.30 |
| 50-65 | 5 | 40.0 | +0.462 | 1.64 |
| 65-80 | 1 | 0.0 | -1.169 | 0.00 |

### intraday_momentum on NIFTY_FUT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 12 out-of-sample trades (< 30); OOS profit factor 0.30 < 1.2; OOS expectancy -0.420R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 28 | 21.4 | 0.19 | -0.506 | 12/16 | -43,482 |
| Walk-forward OOS | 12 | 25.0 | 0.30 | -0.420 | 6/6 | -15,445 |
| Default params on the same OOS windows | 13 | 30.8 | 0.27 | -0.432 | 6/7 | -21,002 |
| Final params, full period (in-sample, optimistic) | 36 | 25.0 | 0.29 | -0.263 | 19/17 | -38,736 |
| Final params, costs x2 | 36 | 16.7 | 0.17 | -0.375 | 19/17 | -56,926 |
| Final params, slippage x2 | 36 | 16.7 | 0.10 | -0.437 | 19/17 | -75,997 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.13 | 6 | 0.00 | -0.645 | 0.00 |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.07 | 5 | 0.63 | -0.272 | 0.66 |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.21 | 1 | inf | +0.183 | 0.36 |

Monte Carlo (OOS trades resampled, one unit): median max DD Rs 18,154, 95th pct Rs 30,387, probability of a losing run 97%.

Final parameters: `{"open_window_min": 45, "min_open_move_atr": 0.0, "stop_atr": 1.5, "require_prior_agree": false}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| 50-65 | 6 | 33.3 | -0.229 | 0.57 |
| 80+ | 6 | 16.7 | -0.612 | 0.14 |

### donchian_breakout on NIFTY_FUT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 3 out-of-sample trades (< 30); loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 3 | 33.3 | 1.85 | +0.155 | 3/0 | 3,334 |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 10 | 30.0 | 0.87 | -0.021 | 5/5 | -3,131 |
| Final params, costs x2 | 10 | 30.0 | 0.71 | -0.095 | 5/5 | -8,166 |
| Final params, slippage x2 | 7 | 28.6 | 0.50 | -0.202 | 3/4 | -11,791 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.89 | 1 | 0.00 | -0.290 | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.63 | 1 | inf | +0.880 | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.79 | 1 | 0.00 | -0.125 | - |

Final parameters: `{"entry_len": 10, "exit_len": 5, "adx_min": 20, "trail_atr_mult": 0.0}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 1 | 0.0 | -0.125 | 0.00 |
| 50-65 | 1 | 100.0 | +0.880 | inf |
| 65-80 | 1 | 0.0 | -0.290 | 0.00 |

### squeeze on NIFTY_FUT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 0 out-of-sample trades (< 30); OOS profit factor 0.00 < 1.2; OOS expectancy -1.000R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 0 | - | - | - | None/None | - |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 0 | - | - | - | None/None | - |
| Final params, costs x2 | 0 | - | - | - | None/None | - |
| Final params, slippage x2 | 0 | - | - | - | None/None | - |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | - | 0 | - | - | - |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | - | 0 | - | - | - |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | - | 0 | - | - | - |

Final parameters: `{"min_squeeze_bars": null, "kc_mult": null, "dc_len": null, "target_r": null, "trail_atr_mult": null}`

### benchmark_random on NIFTY_FUT (15m) - FAIL

Sessions: 39 (2026-08-31..2026-10-27).

Why not PASS: only 2 out-of-sample trades (< 30); loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 13 | 30.8 | 0.33 | -0.321 | 5/8 | -24,831 |
| Walk-forward OOS | 2 | 100.0 | - | +1.780 | 0/2 | 21,236 |
| Default params on the same OOS windows | 2 | 100.0 | - | +1.780 | 0/2 | 21,236 |
| Final params, full period (in-sample, optimistic) | 13 | 30.8 | 0.33 | -0.321 | 5/8 | -24,831 |
| Final params, costs x2 | 13 | 30.8 | 0.24 | -0.405 | 5/8 | -31,367 |
| Final params, slippage x2 | 13 | 30.8 | 0.18 | -0.472 | 5/8 | -36,684 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-09-03..2026-10-01 | 2026-10-05..2026-10-12 | 0.10 | 1 | inf | +1.644 | inf |
| 2 | 2026-09-11..2026-10-12 | 2026-10-13..2026-10-21 | 0.05 | 1 | inf | +1.916 | inf |
| 3 | 2026-09-22..2026-10-21 | 2026-10-22..2026-10-27 | 0.62 | 0 | - | - | - |

Final parameters: `{"entry_prob": null, "target_r": null, "trail_atr_mult": null}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 2 | 100.0 | +1.780 | inf |

