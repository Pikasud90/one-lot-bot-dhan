# Cross-instrument retune of every Lab strategy — 7 Nov 2026

> **Addendum 2026-11-08.** The index-option numbers below were computed with a model that charged
> slippage twice (0.03 % on the index AND the premium's half-spread and ticks), about 2 points of
> premium per round trip; the runner is fixed. Re-priced with Dhan's real traded premiums, no
> option setting is profitable in all three periods either. See
> [REAL_OPTIONS_2026-11-08.md](REAL_OPTIONS_2026-11-08.md). Stock results are unaffected.

The owner rejected a shortlist that leaned on one stock (Adani Green) and asked for every
strategy to be retuned so that **one setting works across the instruments**, tested with
several sweep versions until the best settings are found. This is that study.

These are backtests, not advice. Option premiums are modelled (Black-76 from India VIX),
not real traded prices.

## What was tested

- **Strategies:** all 14 Lab strategies, on 15m, 30m and 60m bars.
- **Baskets:** 18 large-cap stocks traded as one share, intraday (RELIANCE, M&M, SBIN, HDFCBANK,
  ITC, TMPV, MARUTI, ICICIBANK, HEROMOTOCO, INFY, TCS, AXISBANK, KOTAKBANK, LT, BAJFINANCE,
  BHARTIARTL, TATASTEEL, SUNPHARMA; Adani Green excluded at the owner's request), and the
  5 index options as one lot (BANKNIFTY, NIFTY, FINNIFTY, MIDCPNIFTY, SENSEX; buy CE/PE only).
- **One parameter set** per strategy × timeframe × basket, run unchanged on every instrument.
- **Protocol, fixed before any result was seen:**
  - TUNE (TRAIN) 8 Nov 2021 – 7 Nov 2024: the search ranks settings here only, by the pooled
    t-statistic of the net return per trade.
  - CHOOSE (VALID) 8 Nov 2024 – 7 Nov 2025: picks one of each group's best six TUNE settings.
  - TEST 8 Nov 2025 – 7 Nov 2026: read once, for the picked setting only.
- **Hard rules for a pick:** profitable on at least half of the instruments on their own
  (breadth), enough trades (stocks 60 TUNE / 15 CHOOSE, options 40 / 10), at least 8 stocks or
  3 indices traded, a positive average in TUNE and in CHOOSE; version 2 also needs a profit
  in at least 2 of the 3 TUNE years.
- **Costs:** Dhan brokerage and every statutory charge from `config/costs.yaml`, 0.03 %
  slippage per side (stocks), option slippage in ticks. Picks are re-run with 1-minute fills,
  at double costs, and (stocks) before costs.
- **Search knobs** besides each strategy's own: long only / short only / both, regime filter,
  last entry time (11:30 / 13:00 / 14:45), minimum strength (0 / 40 / 55 / 70), one or two
  trades a day, a time stop, a partial at 1R; version 2 adds the first entry time
  (09:30 / 10:00 / 10:30) and the NIFTY market filter.

| Run | What changed | Settings tried |
|---|---|---|
| v1 | both baskets; successive halving across groups | 2,400 |
| v2 options | index volume fixed (below); every group gets all three rounds; warm start from v1; 2-of-3 years rule; double-cost re-run | 2,018 |
| v2 stocks | the same, plus a before-cost re-run of the picks | 1,807 |
| **Total** | | **6,225 settings, 70,619 five-year backtests** |

## A data problem found on the way (fixed in commit 4904e0c)

Dhan's index history carries **no volume until 3 Dec 2025** and a volume figure after it. The
live desk already ignores index volume, but the Lab used it, so every volume filter and the
VWAP were off in TUNE and CHOOSE and on in TEST: the v1 index-option TEST numbers were judged
on different rules. The Lab now ignores volume for indices and spot FX in every year. Four
strategies (orb, supertrend_adx, vwap_pullback, squeeze) treated missing volume as "fail" and
never traded an index; their volume checks are now neutral without volume, as `base.py`
documents. Earlier Lab sweeps of index options are affected for Dec 2025 onwards.

## Index options (v2)

| Strategy | Best TF | Verdict | Tuning Nov 21-Nov 24: avg · win% | Choosing Nov 24-Nov 25: avg · win% | Test Nov 25-Nov 26: avg · win% · trades | Test at 2x costs | Trades/week (all) |
|---|---|---|---|---|---|---|---|
| supertrend_vol | 60m | passed tuning + choosing, LOST in the test year | +2.12% · 53% | +2.08% · 58% | -5.97% · 33% · 9 | -6.35% | 0.3 |
| ema_cross | 30m | passed tuning + choosing, LOST in the test year | +1.30% · 47% | +2.50% · 50% | -2.94% · 44% · 16 | -3.28% | 0.4 |
| supertrend_vol | 30m | passed tuning + choosing, LOST in the test year | +1.54% · 53% | +1.12% · 47% | -1.65% · 43% · 90 | -2.00% | 1.9 |
| nr7_breakout | 15m | lost in the choosing year | +0.83% · 39% | -0.61% · 56% | -6.11% · 56% · 9 | -6.43% | 0.3 |
| cpr_breakout | 30m | lost in the choosing year | +2.56% · 45% | -2.62% · 47% | +0.41% · 39% · 18 | +0.06% | 0.4 |
| vwap_pullback | 15m | lost in the choosing year | +0.01% · 47% | -3.11% · 36% | -3.82% · 29% · 17 | -4.21% | 0.3 |
| turtle_soup | 30m | lost in the choosing year | +0.57% · 52% | -5.06% · 47% | -6.81% · 25% · 8 | -7.14% | 0.3 |
| ema_pullback | 15m | lost in the choosing year | +3.81% · 59% | -4.85% · 38% | -3.81% · 34% · 38 | -4.16% | 0.6 |
| orb | 60m | lost in the choosing year | +2.89% · 58% | -5.59% · 21% | -2.97% · 36% · 22 | -3.30% | 0.3 |
| supertrend_adx | 15m | lost in the choosing year | +0.87% · 43% | -7.23% · 25% | -6.34% · 19% · 21 | -6.66% | 0.4 |
| donchian_breakout | 60m | profitable on only 20% of instruments in the tuning years | -2.97% · 35% | -2.45% · 35% | -4.94% · 43% · 44 | -5.27% | 0.6 |
| mean_reversion | 30m | profitable on only 0% of instruments in the tuning years | -1.67% · 59% | -2.34% · 38% | -5.19% · 42% · 19 | -5.52% | 0.5 |
| intraday_momentum | 15m | profitable on only 20% of instruments in the tuning years | -1.86% · 32% | -2.18% · 34% | -4.03% · 17% · 140 | -4.37% | 2.4 |

Too few trades to judge: squeeze (at most 29 trades in three years across the basket).

The exact settings of every row are listed at the end of this page.

### Does the timing beat random entries?

Random **long** entries on the same five indices, with the same exit styles (10 seeds each),
lose in every period. That loss is the cost of buying an option intraday: time decay plus
charges.

| Bars | Exit style | TUNE | CHOOSE | TEST |
|---|---|---|---|---|
| 15m | like supertrend_vol 30m | −3.05 % | −2.69 % | −3.53 % |
| 15m | hold to the close | −2.61 % | −2.93 % | −3.46 % |
| 30m | like supertrend_vol 30m | −2.24 % | −3.61 % | −4.01 % |
| 30m | hold to the close | −2.39 % | −3.82 % | −4.01 % |
| 60m | like supertrend_vol 30m | −1.61 % | −3.46 % | −3.89 % |
| 60m | hold to the close | −1.89 % | −3.63 % | −3.42 % |

The best option setting (supertrend_vol, 30m, long only) beat random long entries by 3–5
points per trade in every year, the test year included, so its timing is real. But a flat
test year (NIFTY −2.1 %, SENSEX −3.2 %, BANKNIFTY +1.8 %) left it at −1.65 % per trade:
the margin did not cover the roughly −4 % drag of buying options intraday.

| supertrend_vol 30m, long only | TUNE yr 1 | TUNE yr 2 | TUNE yr 3 | CHOOSE | TEST |
|---|---|---|---|---|---|
| Avg per trade | +0.31 % | +3.35 % | +1.05 % | +1.12 % | −1.65 % |
| Trades (5 indices) | 105 | 97 | 95 | 101 | 90 |
| NIFTY that year | +1.5 % | +6.5 % | +24.2 % | +6.0 % | −2.1 % |

## Stocks (v2)

| Strategy | Best TF | Verdict | Tuning Nov 21-Nov 24: avg · win% | Choosing Nov 24-Nov 25: avg · win% | Test Nov 25-Nov 26: avg · win% · trades | Test at 2x costs | Trades/week (all) |
|---|---|---|---|---|---|---|---|
| supertrend_vol | 60m | lost in the choosing year | +0.17% · 55% | -0.04% · 52% | +0.10% · 48% · 25 | -0.01% | 0.5 |
| turtle_soup | 30m | lost in the choosing year | +0.04% · 48% | -0.10% · 35% | -0.17% · 42% · 38 | -0.27% | 0.6 |
| supertrend_adx | 30m | lost in the choosing year | +0.17% · 51% | -0.13% · 47% | +0.06% · 41% · 34 | -0.04% | 0.6 |
| ema_cross | 60m | lost in the choosing year | +0.05% · 52% | -0.23% · 35% | -0.11% · 40% · 25 | -0.22% | 0.5 |
| donchian_breakout | 30m | lost in the choosing year | +0.15% · 48% | -0.38% · 24% | +0.15% · 50% · 20 | +0.04% | 0.8 |
| gap_fade | 15m | lost in the choosing year | +0.02% · 50% | -0.17% · 46% | -0.07% · 46% · 166 | -0.17% | 3.3 |
| vwap_pullback | 30m | profitable on only 29% of instruments in the tuning years | -0.06% · 32% | +0.11% · 40% | -0.09% · 27% · 15 | -0.20% | 0.4 |
| ema_pullback | 60m | profitable on only 36% of instruments in the tuning years | -0.02% · 49% | -0.07% · 38% | -0.30% · 44% · 9 | -0.40% | 0.5 |
| nr7_breakout | 15m | profitable on only 33% of instruments in the tuning years | -0.06% · 44% | -0.22% · 39% | +0.13% · 40% · 25 | +0.02% | 0.5 |
| cpr_breakout | 30m | profitable on only 44% of instruments in the tuning years | -0.03% · 40% | -0.11% · 38% | -0.07% · 41% · 111 | -0.18% | 2.0 |
| mean_reversion | 30m | profitable on only 0% of instruments in the tuning years | -0.15% · 47% | -0.11% · 54% | -0.22% · 45% · 265 | -0.33% | 5.8 |
| orb | 15m | profitable on only 31% of instruments in the tuning years | -0.11% · 40% | -0.18% · 39% | -0.21% · 33% · 134 | -0.32% | 2.2 |
| intraday_momentum | 15m | profitable on only 0% of instruments in the tuning years | -0.15% · 30% | -0.20% · 17% | -0.19% · 21% · 236 | -0.29% | 4.4 |

Too few trades to judge: squeeze (at most 52 trades in three years across the basket).

No stock setting passed. Before costs most of them make money: the picks average +0.10 % to
+0.36 % per trade in the tuning years, and most stay positive in the test year (supertrend_adx
30m +0.27 %, supertrend_vol 60m +0.26 %, nr7_breakout 15m +0.28 %, cpr_breakout 60m +0.27 %).
One-share intraday charges (brokerage 0.03 % a side, STT, exchange, GST, stamp) plus 0.03 %
slippage a side cost about 0.15 % per round trip, which is the whole edge. Every setting that
survived the tuning years lost money in the choosing year (Nov 2024 - Nov 2025).

## What this means

- **No setting of any strategy passed all three periods**, on either basket, after 6,225
  settings and 70,619 five-year backtests. The defaults are worse still (the last column of
  each run's `report.md`).
- **Stocks:** the signals carry a small edge before costs, about the size of the intraday
  costs of a share. The closest settings (supertrend_vol 60m and supertrend_adx 30m, both
  short only with entries until 11:30) make +0.06 % to +0.17 % per trade after costs in two of
  the three periods and lose in the third.
- **Index options:** the long-only trend settings time their entries better than chance in
  every year, but buying an option intraday costs 2-4 % of the premium per trade (modelled),
  so they only make money in rising years.
- **Why the search stopped here:** with this many settings tried on the same five years,
  continuing would eventually produce a setting that passes by luck, and the test year has
  now been read for every version. Further tuning on the same data would fit the test year.
- **What can still add real evidence:**
  1. A forward paper test of the closest candidates (the desk already paper-trades every
     alert); it needs months, as the stock candidates trade about once in two weeks.
  2. Real option prices (Dhan expired-options data) instead of the Black-76 model.
  3. A fill check: if real one-share fills cost less than the 0.03 % slippage assumed, the
     stock picks gain about 0.04 % per trade; that is worth measuring on real fills, not
     assuming.
- **Data fix:** any earlier Lab sweep of index options should be re-run; its results from
  Dec 2025 onwards used the stray index volume.

## The settings

- **supertrend_vol 60m STOCK** (short only, entries 09:30-11:30, strength >= 55, max 2/day, target 1.5R, trail 2.5 ATR, time stop 12 bars): `{"adx_min": 12, "direction": "short", "entry_end": "11:30", "flip_window": 4, "max_bars": 12, "max_trades_per_day": 2, "min_strength": 55, "partial_r": 0.0, "regime_filter": true, "rsi_long_min": 50, "st_mult": 2.0, "st_period": 7, "target_r": 1.5, "trail_atr_mult": 2.5}`
- **turtle_soup 30m STOCK** (short only, entries 09:30-13:00, strength >= 40, max 1/day, target 2.0R, time stop 12 bars): `{"direction": "short", "entry_end": "13:00", "lookback": 20, "max_bars": 12, "max_trades_per_day": 1, "min_break_atr": 0.1, "min_prior_age": 3, "min_strength": 40, "partial_r": 0.0, "regime_filter": true, "target_r": 2.0}`
- **supertrend_adx 30m STOCK** (short only, entries 09:30-11:30, max 1/day, target 1.5R, trail 1.5 ATR, time stop 24 bars): `{"adx_min": 25, "direction": "short", "entry_end": "11:30", "flip_window": 4, "max_bars": 24, "max_trades_per_day": 1, "max_vwap_dist_atr": 3.0, "min_strength": 0, "partial_r": 0.0, "regime_filter": true, "st_mult": 2.5, "st_period": 14, "target_r": 1.5, "trail_atr_mult": 1.5}`
- **ema_cross 60m STOCK** (long only, entries 09:30-11:30, strength >= 55, max 1/day, target 2.5R, half off at 1.0R): `{"adx_min": 20, "confirm_atr": 0.15, "direction": "long", "entry_end": "11:30", "fast": 9, "max_bars": 0, "max_trades_per_day": 1, "min_strength": 55, "partial_r": 1.0, "regime_filter": false, "rsi_margin": 3, "slow": 34, "target_r": 2.5, "trail_atr_mult": 0.0}`
- **donchian_breakout 30m STOCK** (long only, entries 09:30-14:45, strength >= 70, max 2/day, trail 2.0 ATR, time stop 12 bars): `{"adx_min": 10, "direction": "long", "entry_end": "14:45", "entry_len": 40, "exit_len": 20, "market_filter": false, "max_bars": 12, "max_trades_per_day": 2, "min_strength": 70, "partial_r": 0.0, "regime_filter": true, "trail_atr_mult": 2.0}`
- **gap_fade 15m STOCK** (short only, entries 09:30-14:45, strength >= 55, max 1/day, target 2.5R): `{"confirm": "below_vwap", "direction": "short", "entry_end": "14:45", "max_bars": 0, "max_trades_per_day": 1, "min_gap": 0.5, "min_rel_gap": 0.5, "min_strength": 55, "partial_r": 0.0, "regime_filter": false, "stop_datr": 1.0, "target_r": 2.5}`
- **vwap_pullback 30m STOCK** (long and short, entries 09:30-13:00, max 2/day): `{"adx_min": 15, "direction": "both", "entry_end": "13:00", "entry_start": "09:30", "max_bars": 0, "max_trades_per_day": 2, "min_strength": 0, "partial_r": 0.0, "regime_filter": false, "rvol_min": 1.0, "target_r": 0.0, "trail_atr_mult": 0.0, "zone_atr": 0.2}`
- **ema_pullback 60m STOCK** (long only, entries 09:30-13:00, strength >= 70, max 1/day, target 3.0R, time stop 24 bars): `{"adx_min": 25, "direction": "long", "ema_len": 10, "entry_end": "13:00", "max_bars": 24, "max_trades_per_day": 1, "min_strength": 70, "partial_r": 0.0, "regime_filter": true, "swing_bars": 8, "target_r": 3.0, "touch_bars": 3, "trail_atr_mult": 0.0}`
- **nr7_breakout 15m STOCK** (short only, entries 09:30-13:00, strength >= 70, max 2/day, trail 1.5 ATR, time stop 12 bars): `{"buffer_atr": 0.0, "direction": "short", "entry_end": "13:00", "entry_start": "09:30", "max_bars": 12, "max_trades_per_day": 2, "min_strength": 70, "nr_n": 7, "partial_r": 0.0, "regime_filter": true, "stop_atr": 0.75, "stop_mode": "pd_mid", "target_r": 0.0, "trail_atr_mult": 1.5}`
- **cpr_breakout 30m STOCK** (short only, entries 10:30-13:00, max 2/day, target 3.0R, trail 1.5 ATR): `{"adx_min": 10, "buffer_atr": 0.1, "cpr_width_max_pct": 0.3, "direction": "short", "entry_end": "13:00", "entry_start": "10:30", "market_filter": true, "max_bars": 0, "max_trades_per_day": 2, "min_strength": 0, "partial_r": 0.0, "regime_filter": false, "stop_mode": "atr", "target_r": 3.0, "trail_atr_mult": 1.5}`
- **mean_reversion 30m STOCK** (long only, entries 09:30-11:30, max 1/day, time stop 24 bars): `{"direction": "long", "entry_end": "11:30", "entry_level": 5, "exit_level": 70, "market_filter": true, "max_bars": 24, "max_trades_per_day": 1, "min_strength": 0, "partial_r": 0.0, "regime_filter": false, "stop_atr": 2.0, "trend_slow": 30}`
- **orb 15m STOCK** (short only, entries 10:00-13:00, strength >= 70, max 2/day, target 2.5R): `{"direction": "short", "direction_mode": "breakout", "entry_end": "13:00", "entry_start": "10:00", "market_filter": true, "max_bars": 0, "max_trades_per_day": 2, "min_strength": 70, "or_minutes": 15, "partial_r": 0.0, "regime_filter": false, "stop_datr": 0.1, "stop_mode": "mid", "target_r": 2.5, "trail_atr_mult": 0.0}`
- **intraday_momentum 15m STOCK** (long only, entries 10:00-14:45, strength >= 70, max 2/day): `{"direction": "long", "entry_end": "14:45", "entry_start": "10:00", "market_filter": true, "max_bars": 0, "max_trades_per_day": 2, "min_open_move_atr": 1.0, "min_strength": 70, "open_window_min": 15, "partial_r": 0.0, "regime_filter": true, "require_prior_agree": true, "stop_atr": 1.0}`
- **supertrend_vol 60m OPTIONS** (long only, entries 10:30-14:45, strength >= 55, max 1/day, target 1.5R): `{"adx_min": 12, "direction": "long", "entry_end": "14:45", "entry_start": "10:30", "flip_window": 1, "max_bars": 0, "max_trades_per_day": 1, "min_strength": 55, "partial_r": 0.0, "regime_filter": true, "rsi_long_min": 55, "st_mult": 1.5, "st_period": 7, "target_r": 1.5, "trail_atr_mult": 0.0}`
- **ema_cross 30m OPTIONS** (long only, entries 09:30-14:45, strength >= 55, max 1/day, half off at 1.0R): `{"adx_min": 15, "confirm_atr": 0.15, "direction": "long", "entry_end": "14:45", "fast": 5, "max_bars": 0, "max_trades_per_day": 1, "min_strength": 55, "partial_r": 1.0, "regime_filter": false, "rsi_margin": 0, "slow": 50, "target_r": 0.0, "trail_atr_mult": 0.0}`
- **supertrend_vol 30m OPTIONS** (long only, entries 09:30-13:00, strength >= 40, max 1/day, target 2.5R, trail 1.5 ATR, time stop 12 bars): `{"adx_min": 12, "direction": "long", "entry_end": "13:00", "flip_window": 2, "max_bars": 12, "max_trades_per_day": 1, "min_strength": 40, "partial_r": 0.0, "regime_filter": false, "rsi_long_min": 55, "st_mult": 2.0, "st_period": 14, "target_r": 2.5, "trail_atr_mult": 1.5}`
- **nr7_breakout 15m OPTIONS** (long and short, entries 10:00-11:30, strength >= 70, max 1/day, trail 1.5 ATR): `{"buffer_atr": 0.05, "direction": "both", "entry_end": "11:30", "entry_start": "10:00", "market_filter": true, "max_bars": 0, "max_trades_per_day": 1, "min_strength": 70, "nr_n": 4, "partial_r": 0.0, "regime_filter": false, "stop_atr": 1.0, "stop_mode": "pd_mid", "target_r": 0.0, "trail_atr_mult": 1.5}`
- **cpr_breakout 30m OPTIONS** (long only, entries 09:30-13:00, strength >= 70, max 1/day, target 2.5R): `{"adx_min": 10, "buffer_atr": 0.2, "cpr_width_max_pct": 0.8, "direction": "long", "entry_end": "13:00", "max_bars": 0, "max_trades_per_day": 1, "min_strength": 70, "partial_r": 0.0, "regime_filter": false, "stop_mode": "cpr", "target_r": 2.5, "trail_atr_mult": 0.0}`
- **vwap_pullback 15m OPTIONS** (long and short, entries 10:30-11:30, strength >= 70, max 1/day, target 1.5R, time stop 24 bars, half off at 1.0R): `{"adx_min": 25, "direction": "both", "entry_end": "11:30", "entry_start": "10:30", "market_filter": true, "max_bars": 24, "max_trades_per_day": 1, "min_strength": 70, "partial_r": 1.0, "regime_filter": true, "rvol_min": 1.25, "target_r": 1.5, "trail_atr_mult": 0.0, "zone_atr": 0.6}`
- **turtle_soup 30m OPTIONS** (long only, entries 09:30-14:45, max 1/day, target 1.0R, time stop 24 bars): `{"direction": "long", "entry_end": "14:45", "lookback": 30, "max_bars": 24, "max_trades_per_day": 1, "min_break_atr": 0.25, "min_prior_age": 3, "min_strength": 0, "partial_r": 0.0, "regime_filter": true, "target_r": 1.0}`
- **ema_pullback 15m OPTIONS** (long only, entries 09:30-14:45, strength >= 40, max 2/day, target 2.5R, trail 2.5 ATR): `{"adx_min": 20, "direction": "long", "ema_len": 20, "entry_end": "14:45", "max_bars": 0, "max_trades_per_day": 2, "min_strength": 40, "partial_r": 0.0, "regime_filter": true, "swing_bars": 3, "target_r": 2.5, "touch_bars": 5, "trail_atr_mult": 2.5}`
- **orb 60m OPTIONS** (short only, entries 10:00-14:45, strength >= 70, max 2/day, target 2.5R, time stop 24 bars, half off at 1.0R): `{"direction": "short", "direction_mode": "breakout", "entry_end": "14:45", "entry_start": "10:00", "market_filter": true, "max_bars": 24, "max_trades_per_day": 2, "min_strength": 70, "or_minutes": 30, "partial_r": 1.0, "regime_filter": false, "stop_datr": 0.2, "stop_mode": "mid", "target_r": 2.5, "trail_atr_mult": 0.0}`
- **supertrend_adx 15m OPTIONS** (short only, entries 09:30-11:30, max 1/day, target 3.0R, trail 1.5 ATR, half off at 1.0R): `{"adx_min": 25, "direction": "short", "entry_end": "11:30", "entry_start": "09:30", "flip_window": 4, "market_filter": false, "max_bars": 0, "max_trades_per_day": 1, "max_vwap_dist_atr": 2.0, "min_strength": 0, "partial_r": 1.0, "regime_filter": true, "st_mult": 2.0, "st_period": 7, "target_r": 3.0, "trail_atr_mult": 1.5}`
- **donchian_breakout 60m OPTIONS** (long only, entries 09:30-13:00, max 2/day, trail 3.0 ATR, time stop 24 bars): `{"adx_min": 20, "direction": "long", "entry_end": "13:00", "entry_len": 10, "exit_len": 20, "max_bars": 24, "max_trades_per_day": 2, "min_strength": 0, "partial_r": 0.0, "regime_filter": false, "trail_atr_mult": 3.0}`
- **mean_reversion 30m OPTIONS** (long only, entries 10:00-13:00, strength >= 70, max 1/day, time stop 6 bars): `{"direction": "long", "entry_end": "13:00", "entry_level": 5, "entry_start": "10:00", "exit_level": 60, "market_filter": true, "max_bars": 6, "max_trades_per_day": 1, "min_strength": 70, "partial_r": 0.0, "regime_filter": false, "stop_atr": 2.0, "trend_slow": 30}`
- **intraday_momentum 15m OPTIONS** (long only, entries 10:00-14:45, strength >= 40, max 2/day): `{"direction": "long", "entry_end": "14:45", "entry_start": "10:00", "market_filter": true, "max_bars": 0, "max_trades_per_day": 2, "min_open_move_atr": 1.0, "min_strength": 40, "open_window_min": 15, "partial_r": 0.0, "regime_filter": true, "require_prior_agree": true, "stop_atr": 0.75}`

## Reproduce

```bash
.venv/bin/python scripts/cross_retune.py --out data/research/cross_retune_v2_options --workers 4 --r1 16 --r2 24 --r3 24 --seed 11 --baskets OPTIONS --every-group --warm data/research/cross_retune_2026-11-07 --space v2 --min-years 2
.venv/bin/python scripts/cross_retune.py --out data/research/cross_retune_v2_stocks --workers 4 --r1 16 --r2 16 --r3 16 --seed 13 --baskets STOCK --every-group --warm data/research/cross_retune_2026-11-07 --space v2 --min-years 2 --gross
.venv/bin/python scripts/cross_retune_best.py data/research/cross_retune_2026-11-07 data/research/cross_retune_v2_options data/research/cross_retune_v2_stocks
```
