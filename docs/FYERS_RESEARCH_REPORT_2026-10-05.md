# Strategy Research Report

Generated 2026-10-05 18:49 IST by `onelot research`.

## Data and method

Universe: 15 NSE large caps (config/universe.yaml). Data source: **yahoo**. Yahoo intraday history is capped (5m/15m about the last 60 days, 60m about 2 years), so 5m/15m results cover roughly 40 sessions and are a preliminary screen; the 60m runs span two years. Re-run this gate on Fyers 1-minute history from 2017 (`onelot backfill --provider fyers` then `onelot research --timeframe 5m,15m,60m`) before trusting any verdict.

* **Edge mode**: every signal is taken with a fixed risk of 1% of Rs 5,00,000 (Rs 5,000 = 1R); costs (brokerage, STT, exchange, SEBI, stamp, GST) and 0.03% slippage per side are included.
* **Walk-forward**: parameters are optimised on an in-sample window and scored only on the following, unseen out-of-sample (OOS) window; windows roll forward. **Only OOS numbers decide the verdict.**
* **Robust selection**: the best of the top-5 trials after averaging with jittered neighbours.
* **Gates (PASS)**: OOS trades >= 30, OOS profit factor >= 1.2, OOS expectancy >= 0.05R, still profitable with 2x costs, profitable parameter neighbourhood. WATCH = positive OOS but not yet robust; FAIL = no out-of-sample edge.

## Verdict summary

| Strategy | TF | Verdict | OOS trades | OOS win % | OOS PF | OOS exp (R) | Default OOS PF | 2x cost PF | Jitter exp (R) | Portfolio return % | Portfolio max DD % |
|---|---|---|---|---|---|---|---|---|---|---|---|
| vwap_pullback | 5m | **FAIL** | 82 | 32.9 | 0.71 | -0.147 | 0.66 | 0.50 | -0.198 | -24.78 | -24.78 |
| orb | 5m | **WATCH** | 34 | 41.2 | 1.19 | +0.129 | 0.54 | 0.59 | -0.330 | -6.98 | -13.34 |
| supertrend_adx | 5m | **FAIL** | 28 | 32.1 | 0.42 | -0.246 | - | 1.04 | -0.083 | +4.75 | -2.32 |
| squeeze | 5m | **FAIL** | 28 | 42.9 | 0.83 | -0.059 | 0.89 | 1.04 | +0.062 | +2.30 | -3.57 |
| mean_reversion | 5m | **FAIL** | 270 | 36.7 | 0.25 | -0.228 | 0.26 | 0.16 | -0.280 | -61.60 | -61.60 |
| gap_fade | 5m | **WATCH** | 23 | 52.2 | 1.95 | +0.257 | 2.60 | 2.04 | +0.293 | +7.60 | -2.71 |
| benchmark_random | 5m | **FAIL** | 121 | 40.5 | 0.68 | -0.140 | 0.54 | 0.52 | -0.193 | -15.81 | -16.74 |
| vwap_pullback | 15m | **FAIL** | 22 | 31.8 | 0.34 | -0.293 | 0.30 | 0.42 | -0.260 | -6.33 | -8.73 |
| orb | 15m | **FAIL** | 79 | 30.4 | 0.81 | -0.149 | 0.85 | 0.87 | -0.320 | -18.11 | -18.11 |
| supertrend_adx | 15m | **FAIL** | 28 | 32.1 | 0.52 | -0.246 | - | 0.61 | -0.070 | -8.66 | -11.88 |
| squeeze | 15m | **FAIL** | 3 | 33.3 | 0.04 | -0.296 | - | 0.13 | -0.662 | -3.51 | -3.51 |
| mean_reversion | 15m | **FAIL** | 201 | 45.3 | 0.37 | -0.223 | 0.40 | 0.25 | -0.224 | -53.69 | -53.75 |
| gap_fade | 15m | **WATCH** | 24 | 41.7 | 1.04 | +0.013 | 2.01 | 1.56 | +0.202 | +4.98 | -2.16 |
| benchmark_random | 15m | **FAIL** | 30 | 33.3 | 0.40 | -0.349 | 0.35 | 0.86 | -0.062 | +12.56 | -2.31 |
| vwap_pullback | 60m | **FAIL** | 87 | 33.3 | 0.32 | -0.252 | 0.41 | 0.35 | -0.318 | -23.88 | -24.50 |
| orb | 60m | **FAIL** | 973 | 39.7 | 0.66 | -0.181 | 0.70 | 0.63 | -0.164 | -60.01 | -62.03 |
| supertrend_adx | 60m | **FAIL** | 117 | 40.2 | 0.57 | -0.116 | - | 0.55 | -0.075 | -4.70 | -6.89 |
| squeeze | 60m | **FAIL** | 11 | 45.5 | 0.52 | -0.116 | - | 1.18 | +0.011 | +1.06 | -1.24 |
| mean_reversion | 60m | **FAIL** | 765 | 48.1 | 0.69 | -0.080 | 0.62 | 0.57 | -1.000 | -61.84 | -62.20 |
| gap_fade | 60m | **FAIL** | 149 | 46.3 | 0.89 | -0.036 | 1.03 | 0.88 | -0.017 | -2.43 | -9.01 |
| benchmark_random | 60m | **FAIL** | 741 | 44.4 | 0.78 | -0.083 | 0.51 | 0.66 | -0.141 | -67.05 | -68.84 |

## Details

### vwap_pullback on 5m - FAIL

Sessions: 40 (2026-08-07..2026-10-05).

Why not PASS: OOS profit factor 0.71 < 1.2; OOS expectancy -0.147R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 296 | 34.1 | 0.49 | -0.300 | 104/192 | -443,680 |
| Walk-forward OOS | 82 | 32.9 | 0.71 | -0.147 | 34/48 | -60,492 |
| Default params on the same OOS windows | 139 | 40.3 | 0.66 | -0.185 | 52/87 | -128,513 |
| Final params, full period (in-sample, optimistic) | 162 | 32.1 | 0.62 | -0.180 | 70/92 | -145,494 |
| Final params, costs x2 | 162 | 30.9 | 0.50 | -0.259 | 70/92 | -209,881 |
| Final params, slippage x2 | 162 | 29.0 | 0.45 | -0.311 | 70/92 | -252,056 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-08-12..2026-09-08 | 2026-09-09..2026-09-17 | 0.54 | 15 | 0.38 | -0.417 | 0.71 |
| 2 | 2026-08-20..2026-09-17 | 2026-09-18..2026-09-25 | 0.55 | 38 | 1.10 | +0.032 | 0.80 |
| 3 | 2026-08-28..2026-09-25 | 2026-09-28..2026-10-05 | 0.80 | 29 | 0.63 | -0.243 | 0.51 |

Monte Carlo (OOS trades resampled, 1% risk): median max DD -15.9%, 95th pct -25.9%, probability of a losing run 90%.

Final parameters: `{"zone_atr": 0.6, "adx_min": 25, "rvol_min": 1.0, "min_strength": 65, "target_r": 3.0, "trail_atr_mult": 2.5, "min_risk_pct": 0.5}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| 50-65 | 6 | 0.0 | -0.895 | 0.00 |
| 65-80 | 47 | 38.3 | -0.157 | 0.67 |
| 80+ | 29 | 31.0 | +0.024 | 1.05 |

### orb on 5m - WATCH

Sessions: 40 (2026-08-07..2026-10-05).

Why not PASS: OOS profit factor 1.19 < 1.2; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 88 | 31.8 | 0.39 | -0.469 | 42/46 | -205,998 |
| Walk-forward OOS | 34 | 41.2 | 1.19 | +0.129 | 14/20 | 21,688 |
| Default params on the same OOS windows | 50 | 40.0 | 0.54 | -0.312 | 20/30 | -77,877 |
| Final params, full period (in-sample, optimistic) | 34 | 29.4 | 0.67 | -0.239 | 12/22 | -40,798 |
| Final params, costs x2 | 34 | 29.4 | 0.59 | -0.318 | 12/22 | -54,199 |
| Final params, slippage x2 | 34 | 26.5 | 0.54 | -0.373 | 12/22 | -63,550 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-08-12..2026-09-08 | 2026-09-09..2026-09-17 | 0.58 | 12 | 0.85 | -0.100 | 0.54 |
| 2 | 2026-08-20..2026-09-17 | 2026-09-18..2026-09-25 | 0.72 | 11 | 1.15 | +0.096 | 0.53 |
| 3 | 2026-08-28..2026-09-25 | 2026-09-28..2026-10-05 | 0.73 | 11 | 1.62 | +0.412 | 0.57 |

Monte Carlo (OOS trades resampled, 1% risk): median max DD -6.8%, 95th pct -13.9%, probability of a losing run 32%.

Final parameters: `{"or_minutes": 30, "direction_mode": "breakout", "stop_mode": "datr", "stop_datr": 0.2, "or_rvol_min": 1.5, "target_r": 0.0, "trail_atr_mult": 0.0, "min_risk_pct": 0.5}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 4 | 25.0 | -0.538 | 0.39 |
| 50-65 | 9 | 22.2 | -0.376 | 0.51 |
| 65-80 | 15 | 53.3 | +0.510 | 1.93 |
| 80+ | 6 | 50.0 | +0.379 | 1.63 |

### supertrend_adx on 5m - FAIL

Sessions: 40 (2026-08-07..2026-10-05).

Why not PASS: only 28 out-of-sample trades (< 30); OOS profit factor 0.42 < 1.2; OOS expectancy -0.246R < 0.05R; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 4 | 75.0 | 2.17 | +0.296 | 1/3 | 5,899 |
| Walk-forward OOS | 28 | 32.1 | 0.42 | -0.246 | 13/15 | -34,428 |
| Default params on the same OOS windows | 1 | 100.0 | - | +0.442 | 1/0 | 2,208 |
| Final params, full period (in-sample, optimistic) | 67 | 46.3 | 1.29 | +0.066 | 27/40 | 22,082 |
| Final params, costs x2 | 67 | 43.3 | 1.04 | +0.009 | 27/40 | 3,117 |
| Final params, slippage x2 | 67 | 38.8 | 0.95 | -0.013 | 27/40 | -4,462 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-08-12..2026-09-08 | 2026-09-09..2026-09-17 | 0.85 | 8 | 0.38 | -0.191 | inf |
| 2 | 2026-08-20..2026-09-17 | 2026-09-18..2026-09-25 | 1.52 | 4 | 0.25 | -0.374 | - |
| 3 | 2026-08-28..2026-09-25 | 2026-09-28..2026-10-05 | 0.87 | 16 | 0.48 | -0.241 | - |

Monte Carlo (OOS trades resampled, 1% risk): median max DD -7.8%, 95th pct -12.1%, probability of a losing run 97%.

Final parameters: `{"st_period": 14, "st_mult": 3.0, "adx_min": 25, "flip_window": 6, "max_vwap_dist_atr": 3.0, "target_r": 0.0, "trail_atr_mult": 1.5, "min_risk_pct": 0.75}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| 50-65 | 9 | 22.2 | -0.421 | 0.18 |
| 65-80 | 14 | 42.9 | -0.054 | 0.84 |
| 80+ | 5 | 20.0 | -0.466 | 0.05 |

### squeeze on 5m - FAIL

Sessions: 40 (2026-08-07..2026-10-05).

Why not PASS: only 28 out-of-sample trades (< 30); OOS profit factor 0.83 < 1.2; OOS expectancy -0.059R < 0.05R.

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 10 | 40.0 | 0.58 | -0.259 | 4/6 | -12,938 |
| Walk-forward OOS | 28 | 42.9 | 0.83 | -0.059 | 10/18 | -8,351 |
| Default params on the same OOS windows | 5 | 60.0 | 0.89 | -0.054 | 0/5 | -1,339 |
| Final params, full period (in-sample, optimistic) | 33 | 57.6 | 1.27 | +0.096 | 10/23 | 15,793 |
| Final params, costs x2 | 33 | 57.6 | 1.04 | +0.015 | 10/23 | 2,555 |
| Final params, slippage x2 | 27 | 59.3 | 1.08 | +0.024 | 9/18 | 3,164 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-08-12..2026-09-08 | 2026-09-09..2026-09-17 | 1.75 | 8 | 0.54 | -0.164 | 0.00 |
| 2 | 2026-08-20..2026-09-17 | 2026-09-18..2026-09-25 | 1.13 | 9 | 1.11 | +0.040 | 0.23 |
| 3 | 2026-08-28..2026-09-25 | 2026-09-28..2026-10-05 | 0.96 | 11 | 0.82 | -0.065 | inf |

Monte Carlo (OOS trades resampled, 1% risk): median max DD -4.8%, 95th pct -9.3%, probability of a losing run 67%.

Final parameters: `{"min_squeeze_bars": 6, "kc_mult": 1.25, "dc_len": 10, "rvol_min": 0.8, "target_r": 1.5, "trail_atr_mult": 1.5, "min_risk_pct": 0.5}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 1 | 0.0 | -0.101 | 0.00 |
| 50-65 | 6 | 50.0 | -0.270 | 0.35 |
| 65-80 | 14 | 28.6 | -0.164 | 0.55 |
| 80+ | 7 | 71.4 | +0.336 | 2.07 |

### mean_reversion on 5m - FAIL

Sessions: 40 (2026-08-07..2026-10-05).

Why not PASS: OOS profit factor 0.25 < 1.2; OOS expectancy -0.228R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 409 | 26.9 | 0.18 | -0.260 | 144/265 | -530,816 |
| Walk-forward OOS | 270 | 36.7 | 0.25 | -0.228 | 112/158 | -306,893 |
| Default params on the same OOS windows | 514 | 37.5 | 0.26 | -0.241 | 200/314 | -620,019 |
| Final params, full period (in-sample, optimistic) | 437 | 36.4 | 0.29 | -0.232 | 156/281 | -506,386 |
| Final params, costs x2 | 301 | 23.6 | 0.16 | -0.341 | 102/199 | -512,687 |
| Final params, slippage x2 | 253 | 17.0 | 0.11 | -0.405 | 89/164 | -512,045 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-08-12..2026-09-08 | 2026-09-09..2026-09-17 | 0.24 | 96 | 0.30 | -0.168 | 0.25 |
| 2 | 2026-08-20..2026-09-17 | 2026-09-18..2026-09-25 | 0.27 | 96 | 0.35 | -0.158 | 0.35 |
| 3 | 2026-08-28..2026-09-25 | 2026-09-28..2026-10-05 | 0.29 | 78 | 0.16 | -0.386 | 0.20 |

Monte Carlo (OOS trades resampled, 1% risk): median max DD -46.3%, 95th pct -53.3%, probability of a losing run 100%.

Final parameters: `{"entry_level": 5, "exit_level": 70, "trend_slow": 30, "stop_atr": 1.5, "max_bars": 12, "min_risk_pct": 0.35}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 21 | 38.1 | -0.196 | 0.36 |
| 50-65 | 98 | 31.6 | -0.261 | 0.20 |
| 65-80 | 99 | 36.4 | -0.201 | 0.29 |
| 80+ | 52 | 46.2 | -0.229 | 0.24 |

### gap_fade on 5m - WATCH

Sessions: 40 (2026-08-07..2026-10-05).

Why not PASS: only 23 out-of-sample trades (< 30).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 30 | 60.0 | 2.23 | +0.293 | 0/30 | 43,880 |
| Walk-forward OOS | 23 | 52.2 | 1.95 | +0.257 | 0/23 | 29,514 |
| Default params on the same OOS windows | 17 | 64.7 | 2.60 | +0.336 | 0/17 | 28,489 |
| Final params, full period (in-sample, optimistic) | 30 | 60.0 | 2.23 | +0.293 | 0/30 | 43,880 |
| Final params, costs x2 | 30 | 60.0 | 2.04 | +0.261 | 0/30 | 39,004 |
| Final params, slippage x2 | 30 | 60.0 | 1.99 | +0.248 | 0/30 | 37,134 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-08-12..2026-09-08 | 2026-09-09..2026-09-17 | 1.42 | 6 | 5.81 | +0.880 | inf |
| 2 | 2026-08-20..2026-09-17 | 2026-09-18..2026-09-25 | 2.82 | 3 | 0.00 | -0.480 | 0.00 |
| 3 | 2026-08-28..2026-09-25 | 2026-09-28..2026-10-05 | 0.89 | 14 | 1.56 | +0.148 | 2.35 |

Monte Carlo (OOS trades resampled, 1% risk): median max DD -2.4%, 95th pct -5.3%, probability of a losing run 12%.

Final parameters: `{"min_rel_gap": null, "min_gap": null, "confirm": null, "stop_datr": null, "target_r": null, "entry_bar": null}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 3 | 0.0 | -0.489 | 0.00 |
| 50-65 | 6 | 50.0 | +0.002 | 1.00 |
| 65-80 | 9 | 66.7 | +0.507 | 2.61 |
| 80+ | 5 | 60.0 | +0.561 | 10.50 |

### benchmark_random on 5m - FAIL

Sessions: 40 (2026-08-07..2026-10-05).

Why not PASS: OOS profit factor 0.68 < 1.2; OOS expectancy -0.140R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 310 | 38.1 | 0.49 | -0.324 | 152/158 | -501,250 |
| Walk-forward OOS | 121 | 40.5 | 0.68 | -0.140 | 72/49 | -84,454 |
| Default params on the same OOS windows | 242 | 35.5 | 0.54 | -0.313 | 113/129 | -378,591 |
| Final params, full period (in-sample, optimistic) | 388 | 41.0 | 0.64 | -0.127 | 182/206 | -245,095 |
| Final params, costs x2 | 388 | 37.9 | 0.52 | -0.183 | 182/206 | -354,897 |
| Final params, slippage x2 | 388 | 36.1 | 0.48 | -0.208 | 183/205 | -401,765 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-08-12..2026-09-08 | 2026-09-09..2026-09-17 | 0.74 | 39 | 0.41 | -0.247 | 0.35 |
| 2 | 2026-08-20..2026-09-17 | 2026-09-18..2026-09-25 | 0.87 | 32 | 0.84 | -0.048 | 0.63 |
| 3 | 2026-08-28..2026-09-25 | 2026-09-28..2026-10-05 | 0.74 | 50 | 0.78 | -0.116 | 0.72 |

Monte Carlo (OOS trades resampled, 1% risk): median max DD -19.2%, 95th pct -29.8%, probability of a losing run 97%.

Final parameters: `{"entry_prob": 0.02, "target_r": 3.0, "trail_atr_mult": 1.5, "min_risk_pct": 0.75}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 121 | 40.5 | -0.140 | 0.68 |

### vwap_pullback on 15m - FAIL

Sessions: 40 (2026-08-07..2026-10-05).

Why not PASS: only 22 out-of-sample trades (< 30); OOS profit factor 0.34 < 1.2; OOS expectancy -0.293R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 58 | 31.0 | 0.34 | -0.385 | 19/39 | -111,391 |
| Walk-forward OOS | 22 | 31.8 | 0.34 | -0.293 | 7/15 | -32,200 |
| Default params on the same OOS windows | 34 | 26.5 | 0.30 | -0.442 | 12/22 | -75,121 |
| Final params, full period (in-sample, optimistic) | 33 | 27.3 | 0.50 | -0.195 | 10/23 | -32,137 |
| Final params, costs x2 | 33 | 27.3 | 0.42 | -0.252 | 10/23 | -41,404 |
| Final params, slippage x2 | 33 | 27.3 | 0.39 | -0.270 | 10/23 | -44,464 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-08-12..2026-09-08 | 2026-09-09..2026-09-17 | 1.01 | 8 | 0.32 | -0.318 | 0.17 |
| 2 | 2026-08-20..2026-09-17 | 2026-09-18..2026-09-25 | 0.70 | 11 | 0.36 | -0.231 | 0.43 |
| 3 | 2026-08-28..2026-09-25 | 2026-09-28..2026-10-05 | 0.43 | 3 | 0.34 | -0.453 | 0.21 |

Monte Carlo (OOS trades resampled, 1% risk): median max DD -7.0%, 95th pct -10.8%, probability of a losing run 99%.

Final parameters: `{"zone_atr": 0.4, "adx_min": 15, "rvol_min": 1.25, "min_strength": 65, "target_r": 0.0, "trail_atr_mult": 1.5, "min_risk_pct": 0.75}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| 50-65 | 2 | 50.0 | +0.173 | 1.50 |
| 65-80 | 11 | 45.5 | -0.157 | 0.55 |
| 80+ | 9 | 11.1 | -0.563 | 0.04 |

### orb on 15m - FAIL

Sessions: 40 (2026-08-07..2026-10-05).

Why not PASS: OOS profit factor 0.81 < 1.2; OOS expectancy -0.149R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 94 | 34.0 | 0.50 | -0.313 | 35/59 | -147,043 |
| Walk-forward OOS | 79 | 30.4 | 0.81 | -0.149 | 30/49 | -58,953 |
| Default params on the same OOS windows | 54 | 46.3 | 0.85 | -0.081 | 21/33 | -21,787 |
| Final params, full period (in-sample, optimistic) | 30 | 23.3 | 0.99 | -0.012 | 11/19 | -1,822 |
| Final params, costs x2 | 30 | 20.0 | 0.87 | -0.139 | 11/19 | -20,911 |
| Final params, slippage x2 | 30 | 20.0 | 0.87 | -0.134 | 11/19 | -20,042 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-08-12..2026-09-08 | 2026-09-09..2026-09-17 | 0.72 | 52 | 0.90 | -0.076 | 0.60 |
| 2 | 2026-08-20..2026-09-17 | 2026-09-18..2026-09-25 | 0.59 | 6 | 0.00 | -1.140 | 1.27 |
| 3 | 2026-08-28..2026-09-25 | 2026-09-28..2026-10-05 | 0.73 | 21 | 0.92 | -0.048 | 0.91 |

Monte Carlo (OOS trades resampled, 1% risk): median max DD -19.7%, 95th pct -31.6%, probability of a losing run 83%.

Final parameters: `{"or_minutes": 30, "direction_mode": "first_candle", "stop_mode": "datr", "stop_datr": 0.1, "or_rvol_min": 2.0, "target_r": 0.0, "trail_atr_mult": 0.0, "min_risk_pct": 0.3}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 16 | 31.2 | -0.066 | 0.92 |
| 50-65 | 30 | 30.0 | -0.156 | 0.79 |
| 65-80 | 28 | 32.1 | -0.140 | 0.81 |
| 80+ | 5 | 20.0 | -0.426 | 0.53 |

### supertrend_adx on 15m - FAIL

Sessions: 40 (2026-08-07..2026-10-05).

Why not PASS: only 28 out-of-sample trades (< 30); OOS profit factor 0.52 < 1.2; OOS expectancy -0.246R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 28 | 32.1 | 0.52 | -0.246 | 12/16 | -34,423 |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 83 | 43.4 | 0.71 | -0.128 | 38/45 | -52,866 |
| Final params, costs x2 | 83 | 39.8 | 0.61 | -0.187 | 38/45 | -77,110 |
| Final params, slippage x2 | 70 | 42.9 | 0.61 | -0.185 | 32/38 | -64,473 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-08-12..2026-09-08 | 2026-09-09..2026-09-17 | 0.83 | 8 | 0.46 | -0.283 | - |
| 2 | 2026-08-20..2026-09-17 | 2026-09-18..2026-09-25 | 0.88 | 10 | 0.37 | -0.310 | - |
| 3 | 2026-08-28..2026-09-25 | 2026-09-28..2026-10-05 | 0.84 | 10 | 0.70 | -0.153 | - |

Monte Carlo (OOS trades resampled, 1% risk): median max DD -8.3%, 95th pct -13.8%, probability of a losing run 94%.

Final parameters: `{"st_period": 14, "st_mult": 2.0, "adx_min": 20, "flip_window": 6, "max_vwap_dist_atr": 3.0, "target_r": 3.0, "trail_atr_mult": 1.5, "min_risk_pct": 0.5}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 2 | 50.0 | +0.712 | 3.77 |
| 50-65 | 14 | 35.7 | -0.234 | 0.50 |
| 65-80 | 10 | 30.0 | -0.378 | 0.36 |
| 80+ | 2 | 0.0 | -0.630 | 0.00 |

### squeeze on 15m - FAIL

Sessions: 40 (2026-08-07..2026-10-05).

Why not PASS: only 3 out-of-sample trades (< 30); OOS profit factor 0.04 < 1.2; OOS expectancy -0.296R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 3 | 33.3 | 0.04 | -0.296 | 0/3 | -4,436 |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 11 | 27.3 | 0.19 | -0.323 | 4/7 | -17,719 |
| Final params, costs x2 | 11 | 27.3 | 0.13 | -0.379 | 4/7 | -20,802 |
| Final params, slippage x2 | 9 | 22.2 | 0.04 | -0.469 | 4/5 | -21,054 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-08-12..2026-09-08 | 2026-09-09..2026-09-17 | 0.52 | 1 | 0.00 | -0.709 | - |
| 2 | 2026-08-20..2026-09-17 | 2026-09-18..2026-09-25 | 0.47 | 1 | 0.00 | -0.219 | - |
| 3 | 2026-08-28..2026-09-25 | 2026-09-28..2026-10-05 | 0.65 | 1 | inf | +0.040 | - |

Final parameters: `{"min_squeeze_bars": 4, "kc_mult": 2.0, "dc_len": 10, "rvol_min": 0.8, "target_r": 2.0, "trail_atr_mult": 0.0, "min_risk_pct": 0.75}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| 65-80 | 2 | 50.0 | -0.089 | 0.18 |
| 80+ | 1 | 0.0 | -0.709 | 0.00 |

### mean_reversion on 15m - FAIL

Sessions: 40 (2026-08-07..2026-10-05).

Why not PASS: OOS profit factor 0.37 < 1.2; OOS expectancy -0.223R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 405 | 43.2 | 0.36 | -0.242 | 118/287 | -488,610 |
| Walk-forward OOS | 201 | 45.3 | 0.37 | -0.223 | 66/135 | -224,090 |
| Default params on the same OOS windows | 187 | 43.3 | 0.40 | -0.201 | 60/127 | -187,611 |
| Final params, full period (in-sample, optimistic) | 468 | 45.7 | 0.40 | -0.218 | 144/324 | -509,375 |
| Final params, costs x2 | 317 | 37.9 | 0.25 | -0.320 | 84/233 | -507,047 |
| Final params, slippage x2 | 246 | 30.1 | 0.16 | -0.412 | 77/169 | -505,641 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-08-12..2026-09-08 | 2026-09-09..2026-09-17 | 0.38 | 63 | 0.50 | -0.148 | 0.62 |
| 2 | 2026-08-20..2026-09-17 | 2026-09-18..2026-09-25 | 0.42 | 70 | 0.41 | -0.189 | 0.41 |
| 3 | 2026-08-28..2026-09-25 | 2026-09-28..2026-10-05 | 0.48 | 68 | 0.26 | -0.328 | 0.24 |

Monte Carlo (OOS trades resampled, 1% risk): median max DD -37.1%, 95th pct -45.2%, probability of a losing run 100%.

Final parameters: `{"entry_level": 15, "exit_level": 70, "trend_slow": 30, "stop_atr": 1.5, "max_bars": 24, "min_risk_pct": 0.5}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 4 | 50.0 | +0.022 | 1.35 |
| 50-65 | 51 | 41.2 | -0.291 | 0.29 |
| 65-80 | 87 | 48.3 | -0.196 | 0.43 |
| 80+ | 59 | 44.1 | -0.222 | 0.35 |

### gap_fade on 15m - WATCH

Sessions: 40 (2026-08-07..2026-10-05).

Why not PASS: only 24 out-of-sample trades (< 30); OOS profit factor 1.04 < 1.2; OOS expectancy +0.013R < 0.05R.

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 30 | 53.3 | 1.70 | +0.202 | 0/30 | 30,250 |
| Walk-forward OOS | 24 | 41.7 | 1.04 | +0.013 | 0/24 | 1,503 |
| Default params on the same OOS windows | 17 | 64.7 | 2.01 | +0.261 | 0/17 | 22,083 |
| Final params, full period (in-sample, optimistic) | 30 | 53.3 | 1.70 | +0.202 | 0/30 | 30,250 |
| Final params, costs x2 | 30 | 53.3 | 1.56 | +0.170 | 0/30 | 25,402 |
| Final params, slippage x2 | 30 | 53.3 | 1.53 | +0.159 | 0/30 | 23,842 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-08-12..2026-09-08 | 2026-09-09..2026-09-17 | 1.33 | 7 | 1.95 | +0.328 | inf |
| 2 | 2026-08-20..2026-09-17 | 2026-09-18..2026-09-25 | 2.73 | 3 | 0.00 | -0.697 | 0.00 |
| 3 | 2026-08-28..2026-09-25 | 2026-09-28..2026-10-05 | 1.00 | 14 | 1.03 | +0.007 | 1.97 |

Monte Carlo (OOS trades resampled, 1% risk): median max DD -3.7%, 95th pct -7.6%, probability of a losing run 49%.

Final parameters: `{"min_rel_gap": null, "min_gap": null, "confirm": null, "stop_datr": null, "target_r": null, "entry_bar": null}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 3 | 0.0 | -0.505 | 0.00 |
| 50-65 | 3 | 66.7 | +0.215 | 11.41 |
| 65-80 | 12 | 58.3 | +0.133 | 1.37 |
| 80+ | 6 | 16.7 | -0.071 | 0.79 |

### benchmark_random on 15m - FAIL

Sessions: 40 (2026-08-07..2026-10-05).

Why not PASS: OOS profit factor 0.40 < 1.2; OOS expectancy -0.349R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 174 | 32.8 | 0.65 | -0.206 | 84/90 | -178,817 |
| Walk-forward OOS | 30 | 33.3 | 0.40 | -0.349 | 15/15 | -52,391 |
| Default params on the same OOS windows | 30 | 26.7 | 0.35 | -0.485 | 15/15 | -72,786 |
| Final params, full period (in-sample, optimistic) | 73 | 35.6 | 1.01 | +0.007 | 35/38 | 2,417 |
| Final params, costs x2 | 73 | 32.9 | 0.86 | -0.071 | 35/38 | -26,201 |
| Final params, slippage x2 | 73 | 31.5 | 0.81 | -0.099 | 35/38 | -36,226 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2026-08-12..2026-09-08 | 2026-09-09..2026-09-17 | 0.59 | 15 | 0.49 | -0.234 | 0.35 |
| 2 | 2026-08-20..2026-09-17 | 2026-09-18..2026-09-25 | 0.45 | 15 | 0.35 | -0.465 | 0.34 |
| 3 | 2026-08-28..2026-09-25 | 2026-09-28..2026-10-05 | 1.07 | 0 | - | - | - |

Monte Carlo (OOS trades resampled, 1% risk): median max DD -11.3%, 95th pct -17.2%, probability of a losing run 98%.

Final parameters: `{"entry_prob": 0.01, "target_r": 0.0, "trail_atr_mult": 1.5, "min_risk_pct": 0.5}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 30 | 33.3 | -0.349 | 0.40 |

### vwap_pullback on 60m - FAIL

Sessions: 493 (2024-10-07..2026-10-05).

Why not PASS: OOS profit factor 0.32 < 1.2; OOS expectancy -0.252R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 84 | 35.7 | 0.42 | -0.244 | 32/52 | -102,125 |
| Walk-forward OOS | 87 | 33.3 | 0.32 | -0.252 | 38/49 | -109,519 |
| Default params on the same OOS windows | 56 | 33.9 | 0.41 | -0.262 | 21/35 | -73,347 |
| Final params, full period (in-sample, optimistic) | 145 | 34.5 | 0.45 | -0.185 | 60/85 | -133,917 |
| Final params, costs x2 | 145 | 31.0 | 0.35 | -0.244 | 60/85 | -176,862 |
| Final params, slippage x2 | 145 | 27.6 | 0.32 | -0.261 | 60/85 | -188,717 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2024-10-10..2025-04-03 | 2025-04-04..2025-06-04 | 0.51 | 2 | 0.00 | -0.324 | 0.00 |
| 2 | 2024-12-10..2025-06-04 | 2025-06-05..2025-07-30 | 0.72 | 19 | 0.21 | -0.345 | 0.22 |
| 3 | 2025-02-04..2025-07-30 | 2025-07-31..2025-09-26 | 0.60 | 14 | 0.44 | -0.114 | 2.10 |
| 4 | 2025-04-04..2025-09-26 | 2025-09-29..2025-11-26 | 0.64 | 2 | 0.85 | -0.039 | 0.85 |
| 5 | 2025-06-05..2025-11-26 | 2025-11-27..2026-01-23 | 0.58 | 11 | 0.13 | -0.458 | 0.13 |
| 6 | 2025-07-31..2026-01-23 | 2026-01-27..2026-03-24 | 0.52 | 9 | 0.00 | -0.660 | 0.00 |
| 7 | 2025-09-29..2026-03-24 | 2026-03-25..2026-05-26 | 0.26 | 11 | 1.57 | +0.068 | 0.37 |
| 8 | 2025-11-27..2026-05-26 | 2026-05-27..2026-07-23 | 0.47 | 8 | 0.28 | -0.347 | 0.70 |
| 9 | 2026-01-27..2026-07-23 | 2026-07-24..2026-09-18 | 0.63 | 8 | 1.33 | +0.077 | 1.20 |
| 10 | 2026-03-25..2026-09-18 | 2026-09-21..2026-10-05 | 0.99 | 3 | 0.45 | -0.217 | inf |

Monte Carlo (OOS trades resampled, 1% risk): median max DD -20.4%, 95th pct -26.7%, probability of a losing run 100%.

Final parameters: `{"zone_atr": 0.4, "adx_min": 20, "max_pierce_atr": 0.4, "target_r": 1.5, "trail_atr_mult": 2.5, "min_risk_pct": 0.6}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 1 | 100.0 | +1.466 | inf |
| 50-65 | 23 | 47.8 | -0.068 | 0.71 |
| 65-80 | 47 | 27.7 | -0.361 | 0.20 |
| 80+ | 16 | 25.0 | -0.303 | 0.15 |

### orb on 60m - FAIL

Sessions: 493 (2024-10-07..2026-10-05).

Why not PASS: OOS profit factor 0.66 < 1.2; OOS expectancy -0.181R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 404 | 37.4 | 0.64 | -0.251 | 211/193 | -506,812 |
| Walk-forward OOS | 973 | 39.7 | 0.66 | -0.181 | 448/525 | -878,550 |
| Default params on the same OOS windows | 734 | 40.1 | 0.70 | -0.197 | 374/360 | -720,769 |
| Final params, full period (in-sample, optimistic) | 975 | 44.8 | 0.80 | -0.080 | 490/485 | -387,690 |
| Final params, costs x2 | 613 | 41.1 | 0.63 | -0.164 | 332/281 | -502,119 |
| Final params, slippage x2 | 561 | 40.6 | 0.60 | -0.181 | 297/264 | -506,160 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2024-10-10..2025-04-03 | 2025-04-04..2025-06-04 | 1.03 | 28 | 1.04 | +0.016 | 0.87 |
| 2 | 2024-12-10..2025-06-04 | 2025-06-05..2025-07-30 | 1.18 | 150 | 0.85 | -0.069 | 0.58 |
| 3 | 2025-02-04..2025-07-30 | 2025-07-31..2025-09-26 | 0.88 | 94 | 0.46 | -0.307 | 0.73 |
| 4 | 2025-04-04..2025-09-26 | 2025-09-29..2025-11-26 | 0.87 | 73 | 0.61 | -0.180 | 0.53 |
| 5 | 2025-06-05..2025-11-26 | 2025-11-27..2026-01-23 | 0.75 | 311 | 0.51 | -0.332 | 0.63 |
| 6 | 2025-07-31..2026-01-23 | 2026-01-27..2026-03-24 | 0.78 | 68 | 1.03 | +0.011 | 0.72 |
| 7 | 2025-09-29..2026-03-24 | 2026-03-25..2026-05-26 | 0.80 | 60 | 0.61 | -0.209 | 0.75 |
| 8 | 2025-11-27..2026-05-26 | 2026-05-27..2026-07-23 | 0.82 | 82 | 0.80 | -0.097 | 0.69 |
| 9 | 2026-01-27..2026-07-23 | 2026-07-24..2026-09-18 | 0.81 | 79 | 1.00 | -0.001 | 0.78 |
| 10 | 2026-03-25..2026-09-18 | 2026-09-21..2026-10-05 | 0.80 | 28 | 0.92 | -0.028 | 1.00 |

Monte Carlo (OOS trades resampled, 1% risk): median max DD -84.4%, 95th pct -90.8%, probability of a losing run 100%.

Final parameters: `{"direction_mode": "breakout", "stop_datr": 0.35, "or_rvol_min": 0.0, "target_r": 1.5, "trail_atr_mult": 2.0, "min_risk_pct": 0.6}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 243 | 36.2 | -0.221 | 0.64 |
| 50-65 | 323 | 42.7 | -0.137 | 0.73 |
| 65-80 | 261 | 43.7 | -0.128 | 0.72 |
| 80+ | 146 | 31.5 | -0.304 | 0.45 |

### supertrend_adx on 60m - FAIL

Sessions: 493 (2024-10-07..2026-10-05).

Why not PASS: OOS profit factor 0.57 < 1.2; OOS expectancy -0.116R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 0 | - | - | - | None/None | - |
| Walk-forward OOS | 117 | 40.2 | 0.57 | -0.116 | 70/47 | -67,654 |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 69 | 39.1 | 0.67 | -0.068 | 37/32 | -23,398 |
| Final params, costs x2 | 69 | 39.1 | 0.55 | -0.103 | 37/32 | -35,494 |
| Final params, slippage x2 | 59 | 40.7 | 0.56 | -0.095 | 30/29 | -28,026 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2024-10-10..2025-04-03 | 2025-04-04..2025-06-04 | 1.23 | 15 | 0.52 | -0.081 | - |
| 2 | 2024-12-10..2025-06-04 | 2025-06-05..2025-07-30 | 0.80 | 19 | 0.19 | -0.335 | - |
| 3 | 2025-02-04..2025-07-30 | 2025-07-31..2025-09-26 | 0.49 | 17 | 1.13 | +0.031 | - |
| 4 | 2025-04-04..2025-09-26 | 2025-09-29..2025-11-26 | 0.96 | 9 | 0.96 | -0.013 | - |
| 5 | 2025-06-05..2025-11-26 | 2025-11-27..2026-01-23 | 0.86 | 6 | 1.71 | +0.071 | - |
| 6 | 2025-07-31..2026-01-23 | 2026-01-27..2026-03-24 | 1.80 | 7 | 0.23 | -0.173 | - |
| 7 | 2025-09-29..2026-03-24 | 2026-03-25..2026-05-26 | 0.93 | 10 | 0.30 | -0.232 | - |
| 8 | 2025-11-27..2026-05-26 | 2026-05-27..2026-07-23 | 1.08 | 18 | 0.23 | -0.296 | - |
| 9 | 2026-01-27..2026-07-23 | 2026-07-24..2026-09-18 | 0.74 | 14 | 1.29 | +0.044 | - |
| 10 | 2026-03-25..2026-09-18 | 2026-09-21..2026-10-05 | 0.56 | 2 | inf | +0.698 | - |

Monte Carlo (OOS trades resampled, 1% risk): median max DD -14.3%, 95th pct -21.7%, probability of a losing run 99%.

Final parameters: `{"st_period": 7, "st_mult": 2.0, "adx_min": 15, "flip_window": 2, "max_vwap_dist_atr": 2.0, "target_r": 0.0, "trail_atr_mult": 1.5}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 5 | 20.0 | -0.158 | 0.39 |
| 50-65 | 38 | 39.5 | -0.130 | 0.47 |
| 65-80 | 56 | 42.9 | -0.101 | 0.64 |
| 80+ | 18 | 38.9 | -0.121 | 0.60 |

### squeeze on 60m - FAIL

Sessions: 493 (2024-10-07..2026-10-05).

Why not PASS: only 11 out-of-sample trades (< 30); OOS profit factor 0.52 < 1.2; OOS expectancy -0.116R < 0.05R.

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 1 | 100.0 | - | +0.672 | 0/1 | 3,359 |
| Walk-forward OOS | 11 | 45.5 | 0.52 | -0.116 | 6/5 | -6,388 |
| Default params on the same OOS windows | 0 | - | - | - | None/None | - |
| Final params, full period (in-sample, optimistic) | 15 | 66.7 | 1.45 | +0.072 | 6/9 | 5,372 |
| Final params, costs x2 | 15 | 66.7 | 1.18 | +0.031 | 6/9 | 2,330 |
| Final params, slippage x2 | 13 | 69.2 | 1.16 | +0.027 | 5/8 | 1,744 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2024-10-10..2025-04-03 | 2025-04-04..2025-06-04 | 4.97 | 2 | 0.62 | -0.053 | - |
| 2 | 2024-12-10..2025-06-04 | 2025-06-05..2025-07-30 | 5.20 | 2 | 0.70 | -0.065 | - |
| 3 | 2025-02-04..2025-07-30 | 2025-07-31..2025-09-26 | 1.82 | 0 | - | - | - |
| 4 | 2025-04-04..2025-09-26 | 2025-09-29..2025-11-26 | 2.28 | 2 | 1.46 | +0.077 | - |
| 5 | 2025-06-05..2025-11-26 | 2025-11-27..2026-01-23 | 2.51 | 1 | 0.00 | -0.382 | - |
| 6 | 2025-07-31..2026-01-23 | 2026-01-27..2026-03-24 | 2.24 | 2 | 2.14 | +0.080 | - |
| 7 | 2025-09-29..2026-03-24 | 2026-03-25..2026-05-26 | 0.62 | 1 | inf | +0.126 | - |
| 8 | 2025-11-27..2026-05-26 | 2026-05-27..2026-07-23 | 0.24 | 0 | - | - | - |
| 9 | 2026-01-27..2026-07-23 | 2026-07-24..2026-09-18 | 3.03 | 1 | 0.00 | -1.105 | - |
| 10 | 2026-03-25..2026-09-18 | 2026-09-21..2026-10-05 | 0.11 | 0 | - | - | - |

Monte Carlo (OOS trades resampled, 1% risk): median max DD -1.9%, 95th pct -3.9%, probability of a losing run 82%.

Final parameters: `{"min_squeeze_bars": 3, "release_window": 1, "dc_len": 20, "kc_mult": 1.5, "rvol_min": 1.1, "target_r": 2.0}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| 50-65 | 3 | 33.3 | -0.117 | 0.33 |
| 65-80 | 6 | 33.3 | -0.226 | 0.37 |
| 80+ | 2 | 100.0 | +0.212 | inf |

### mean_reversion on 60m - FAIL

Sessions: 493 (2024-10-07..2026-10-05).

Why not PASS: OOS profit factor 0.69 < 1.2; OOS expectancy -0.080R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 983 | 49.0 | 0.65 | -0.103 | 533/450 | -506,040 |
| Walk-forward OOS | 765 | 48.1 | 0.69 | -0.080 | 442/323 | -306,678 |
| Default params on the same OOS windows | 1175 | 47.6 | 0.62 | -0.109 | 642/533 | -639,502 |
| Final params, full period (in-sample, optimistic) | 1421 | 47.7 | 0.69 | -0.071 | 745/676 | -502,608 |
| Final params, costs x2 | 946 | 44.4 | 0.57 | -0.106 | 508/438 | -500,228 |
| Final params, slippage x2 | 897 | 44.3 | 0.55 | -0.113 | 483/414 | -502,779 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2024-10-10..2025-04-03 | 2025-04-04..2025-06-04 | 0.68 | 152 | 0.86 | -0.035 | 0.86 |
| 2 | 2024-12-10..2025-06-04 | 2025-06-05..2025-07-30 | 0.75 | 146 | 0.51 | -0.090 | 0.48 |
| 3 | 2025-02-04..2025-07-30 | 2025-07-31..2025-09-26 | 0.65 | 67 | 0.92 | -0.013 | 0.49 |
| 4 | 2025-04-04..2025-09-26 | 2025-09-29..2025-11-26 | 0.73 | 75 | 0.51 | -0.132 | 0.44 |
| 5 | 2025-06-05..2025-11-26 | 2025-11-27..2026-01-23 | 0.71 | 68 | 0.60 | -0.177 | 0.86 |
| 6 | 2025-07-31..2026-01-23 | 2026-01-27..2026-03-24 | 0.68 | 66 | 0.99 | -0.001 | 0.70 |
| 7 | 2025-09-29..2026-03-24 | 2026-03-25..2026-05-26 | 0.80 | 62 | 0.58 | -0.090 | 0.82 |
| 8 | 2025-11-27..2026-05-26 | 2026-05-27..2026-07-23 | 0.89 | 77 | 0.47 | -0.169 | 0.33 |
| 9 | 2026-01-27..2026-07-23 | 2026-07-24..2026-09-18 | 0.76 | 47 | 0.93 | -0.022 | 0.79 |
| 10 | 2026-03-25..2026-09-18 | 2026-09-21..2026-10-05 | 0.70 | 5 | 0.74 | -0.108 | 0.95 |

Monte Carlo (OOS trades resampled, 1% risk): median max DD -48.0%, 95th pct -59.5%, probability of a losing run 100%.

Final parameters: `{"entry_level": 10, "exit_level": 70, "trend_slow": 30, "stop_atr": 2.0, "max_bars": 5}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 38 | 50.0 | -0.079 | 0.68 |
| 50-65 | 263 | 49.4 | -0.060 | 0.76 |
| 65-80 | 275 | 49.5 | -0.066 | 0.73 |
| 80+ | 189 | 43.9 | -0.131 | 0.55 |

### gap_fade on 60m - FAIL

Sessions: 493 (2024-10-07..2026-10-05).

Why not PASS: OOS profit factor 0.89 < 1.2; OOS expectancy -0.036R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 294 | 48.6 | 0.97 | -0.008 | 0/294 | -11,318 |
| Walk-forward OOS | 149 | 46.3 | 0.89 | -0.036 | 0/149 | -27,049 |
| Default params on the same OOS windows | 212 | 50.0 | 1.03 | +0.008 | 0/212 | 8,590 |
| Final params, full period (in-sample, optimistic) | 94 | 50.0 | 0.98 | -0.009 | 0/94 | -4,095 |
| Final params, costs x2 | 94 | 47.9 | 0.88 | -0.050 | 0/94 | -23,137 |
| Final params, slippage x2 | 94 | 47.9 | 0.85 | -0.060 | 0/94 | -28,129 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2024-10-10..2025-04-03 | 2025-04-04..2025-06-04 | 0.80 | 13 | 0.66 | -0.067 | 0.35 |
| 2 | 2024-12-10..2025-06-04 | 2025-06-05..2025-07-30 | 0.76 | 1 | 0.00 | -0.068 | 2.40 |
| 3 | 2025-02-04..2025-07-30 | 2025-07-31..2025-09-26 | 0.81 | 17 | 0.81 | -0.038 | 0.70 |
| 4 | 2025-04-04..2025-09-26 | 2025-09-29..2025-11-26 | 0.74 | 9 | 1.10 | +0.034 | 1.47 |
| 5 | 2025-06-05..2025-11-26 | 2025-11-27..2026-01-23 | 1.68 | 8 | 0.12 | -0.329 | 0.26 |
| 6 | 2025-07-31..2026-01-23 | 2026-01-27..2026-03-24 | 1.31 | 15 | 0.52 | -0.166 | 0.62 |
| 7 | 2025-09-29..2026-03-24 | 2026-03-25..2026-05-26 | 1.14 | 16 | 1.88 | +0.227 | 1.35 |
| 8 | 2025-11-27..2026-05-26 | 2026-05-27..2026-07-23 | 0.97 | 11 | 2.36 | +0.292 | 1.46 |
| 9 | 2026-01-27..2026-07-23 | 2026-07-24..2026-09-18 | 1.52 | 48 | 0.69 | -0.140 | 1.35 |
| 10 | 2026-03-25..2026-09-18 | 2026-09-21..2026-10-05 | 1.40 | 11 | 1.24 | +0.074 | 1.24 |

Monte Carlo (OOS trades resampled, 1% risk): median max DD -11.8%, 95th pct -21.5%, probability of a losing run 75%.

Final parameters: `{"min_rel_gap": 1.0, "min_gap": 0.5, "confirm": "red_bar", "stop_datr": 0.5, "target_r": 0.0}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 10 | 50.0 | -0.086 | 0.80 |
| 50-65 | 65 | 44.6 | -0.034 | 0.90 |
| 65-80 | 66 | 43.9 | -0.067 | 0.79 |
| 80+ | 8 | 75.0 | +0.257 | 2.83 |

### benchmark_random on 60m - FAIL

Sessions: 493 (2024-10-07..2026-10-05).

Why not PASS: OOS profit factor 0.78 < 1.2; OOS expectancy -0.083R < 0.05R; loses money when costs are doubled; parameter neighbourhood is not profitable (fragile optimum).

| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |
|---|---|---|---|---|---|---|
| Default params (full period) | 568 | 37.5 | 0.63 | -0.162 | 305/263 | -460,402 |
| Walk-forward OOS | 741 | 44.4 | 0.78 | -0.083 | 256/485 | -305,789 |
| Default params on the same OOS windows | 362 | 35.9 | 0.51 | -0.231 | 73/289 | -418,103 |
| Final params, full period (in-sample, optimistic) | 809 | 39.8 | 0.70 | -0.125 | 418/391 | -505,815 |
| Final params, costs x2 | 635 | 38.1 | 0.66 | -0.158 | 330/305 | -500,058 |
| Final params, slippage x2 | 556 | 36.2 | 0.61 | -0.184 | 286/270 | -511,572 |

Walk-forward folds:

| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |
|---|---|---|---|---|---|---|---|
| 1 | 2024-10-10..2025-04-03 | 2025-04-04..2025-06-04 | 0.97 | 45 | 0.69 | -0.098 | 0.69 |
| 2 | 2024-12-10..2025-06-04 | 2025-06-05..2025-07-30 | 0.71 | 45 | 0.98 | -0.008 | 0.98 |
| 3 | 2025-02-04..2025-07-30 | 2025-07-31..2025-09-26 | 0.89 | 45 | 0.35 | -0.327 | 0.35 |
| 4 | 2025-04-04..2025-09-26 | 2025-09-29..2025-11-26 | 0.87 | 15 | 2.58 | +0.294 | 2.58 |
| 5 | 2025-06-05..2025-11-26 | 2025-11-27..2026-01-23 | 0.84 | 45 | 0.51 | -0.271 | 0.51 |
| 6 | 2025-07-31..2026-01-23 | 2026-01-27..2026-03-24 | 0.52 | 153 | 1.07 | +0.023 | 0.57 |
| 7 | 2025-09-29..2026-03-24 | 2026-03-25..2026-05-26 | 0.87 | 30 | 0.05 | -0.717 | 0.05 |
| 8 | 2025-11-27..2026-05-26 | 2026-05-27..2026-07-23 | 1.03 | 211 | 0.92 | -0.025 | 0.19 |
| 9 | 2026-01-27..2026-07-23 | 2026-07-24..2026-09-18 | 0.66 | 140 | 0.86 | -0.053 | 0.88 |
| 10 | 2026-03-25..2026-09-18 | 2026-09-21..2026-10-05 | 0.97 | 12 | 0.49 | -0.274 | 0.49 |

Monte Carlo (OOS trades resampled, 1% risk): median max DD -50.7%, 95th pct -65.6%, probability of a losing run 100%.

Final parameters: `{"entry_prob": 0.1, "target_r": 0.0, "trail_atr_mult": 2.5, "min_risk_pct": 0.6}`

Strength-score calibration (OOS):

| Strength | Trades | Win % | Avg R | PF |
|---|---|---|---|---|
| <50 | 741 | 44.4 | -0.083 | 0.78 |

## Long-horizon validation: Relative Gap Fade on daily bars (2015-2026)

The gap-fade idea was found by exploring daily bars from 2015-2021 only; 2022-2026 was held out. Daily simulation: short at the open when the stock gaps >= 0.75% more than NIFTY (and >= 0.3% itself), stop 0.75 x ATR(14) above the open (a touch of the day's high counts as stopped), exit at the close, 0.105% round-trip friction deducted.

| Period | Trades | Net % / trade | Win % | PF | Exp (R) | t-stat |
|---|---|---|---|---|---|---|
| 2015-21 exploration | 1316 | +0.290 | 56.3 | 1.39 | +0.131 | +4.7 |
| 2022-26 holdout | 685 | +0.139 | 54.5 | 1.23 | +0.087 | +2.3 |

Net % per trade by year: 2015: +0.41, 2016: +0.24, 2017: +0.23, 2018: +0.42, 2019: -0.11, 2020: +0.34, 2021: +0.38, 2022: +0.44, 2023: +0.24, 2024: +0.07, 2025: +0.04, 2026: -0.02

Capturability (60-minute bars, last 2 years, 321 signal days): of the open-to-close fade, +0.084% happens between the pre-open auction price and the first continuous trade (not capturable from an alert), while +0.128% happens from the first trade to the 15:15 square-off (capturable). Expect the live edge to be the smaller, post-open number minus costs.

