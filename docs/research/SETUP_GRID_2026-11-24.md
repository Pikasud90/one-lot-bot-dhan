# Setup grid, instrument by instrument (US-091), 2026-11-24

Script `scripts/setup_grid.py`; outputs in `data/research/setup_grid_2026-11-24/` of the main
checkout. Read-only on both stores; no request to Dhan. Three numbers in a row are TUNE / CHOOSE /
TEST (to 2024-11-07, to 2025-11-07, to 2026-11-07).

## The answer in five lines

(written after the run)

## What was fixed in advance (written and committed before any result)

- Instruments: NIFTY, BANKNIFTY, FINNIFTY, SENSEX options and the 18 desk stocks, each alone,
  and the pools "four indices" and "18 stocks". MIDCPNIFTY is not run (training instrument).
- Strategies: every one of `REGISTRY` that `SUITABLE` marks for the kind: 10 for options, 14
  for stocks. Signals from the Lab's `generate_for_instrument` with the US-086 session window
  (a strategy's own entry window holds), 2021-11-08 to 2026-11-07.
- Trade: entry at the open of the minute after the signal bar closes, one open trade per cell.
  Exit, one setting: `src/onelot/manage/ratchet.py`, first stop 1.5 x ATR(14, Wilder) of 60m
  bars, target 3 risks, extend on, the strategy's own exit kept, the session exit of the
  harness (15:15 options, 15:10 stocks). Exit engine of `scripts/ratchet_study.py` through
  `scripts/regime_study.py` (US-080). Options: % of the entry premium by `h.pnl`. Stocks: one
  share, intraday charges and 0.03% slippage a side.
- Step A, the timeframe: default parameters at 5m, 15m, 30m and 60m; microtrend only at its
  own 3m; ema_cross also at its own default 12m. Per unit (instrument or pool) and strategy the
  timeframe with the best TUNE mean among those with 60 TUNE trades or more is FIXED.
- Step B, the preset, at the fixed timeframe: DEFAULT plus the Lab's own presets
  (`src/onelot/lab/presets.py`, built from the research search spaces before any result):
  `low` (first choice of every entry knob) and `high` (last choice). Table `param_sets` is
  empty, so no third preset. microtrend and supertrend_mtf have no search space: DEFAULT only.
  The presets per strategy:
  - ema_cross low fast 5, slow 21, adx_min 15, confirm_atr 0, rsi_margin 0; high 13, 50, 25,
    0.3, 6.
  - supertrend_vol low st_period 7, st_mult 1.5, adx_min 12, flip_window 1, rsi_long_min 50;
    high 14, 3.0, 24, 4, 55.
  - supertrend_adx low st_period 7, st_mult 2.0, adx_min 15, flip_window 2,
    max_vwap_dist_atr 1.0; high 14, 3.0, 25, 6, 3.0.
  - orb low or_minutes 15, breakout, stop mid, stop_datr 0.1; high 30, first_candle, datr, 0.2.
  - vwap_pullback low zone_atr 0.2, adx_min 10, rvol_min 0.8; high 0.6, 25, 1.25.
  - squeeze low min_squeeze_bars 4, kc_mult 1.25, dc_len 10; high 10, 2.0, 20.
  - donchian_breakout low entry_len 10, exit_len 5, adx_min 10; high 40, 20, 20.
  - mean_reversion low entry_level 5, exit_level 50, trend_slow 30, stop_atr 1.0, max_bars 6;
    high 15, 70, 80, 2.0, 24.
  - intraday_momentum low open_window_min 15, min_open_move_atr 0, stop_atr 0.75, no prior
    agree; high 45, 1.0, 1.5, prior agree.
  - gap_fade low min_rel_gap 0.5, min_gap 0, confirm none, stop_datr 0.5; high 1.0, 0.5,
    below_vwap, 1.0.
  - cpr_breakout low cpr_width_max_pct 0.3, buffer_atr 0, adx_min 10, stop cpr; high 0.8, 0.2,
    20, atr.
  - nr7_breakout low nr_n 4, buffer_atr 0, stop atr, stop_atr 0.75; high 7, 0.15, pd_mid, 1.5.
  - ema_pullback low adx_min 20, ema_len 10, touch_bars 2, swing_bars 3; high 30, 30, 5, 8.
  - turtle_soup low lookback 10, min_prior_age 3, min_break_atr 0, max_bars 12; high 30, 6,
    0.25, 36.
  Per unit and strategy the preset with the best TUNE mean (60 TUNE trades or more) is FIXED.
- Step C, the conditions, at the fixed timeframe and preset, one at a time, as the story lists
  them (C0 all; C1 daily trend; C2 VIX level; C3 VIX change over 5%; C4 active or calm; C5
  gap in sd units; C6 time of entry; C8 side; C9 first-half-hour turnover, entries after 09:45
  only). Additions made here before any result:
  - C7: the harness prices the near monthly contract and rolls 3 calendar days before its
    expiry, so the traded contract never has 0 or 1 sessions left. C7 keeps 0 and 1 (they stay
    empty) and splits "2 or more" into 2 to 5, 6 to 10 and 11 or more sessions to the model
    contract's expiry. That expiry is the harness's own (a last-Tuesday rule); the real store
    holds no expiry calendar, so the story's "never a weekday assumption" cannot be met: said
    here, not hidden.
  - C9 on the four indices: the summed turnover of the 18 desk stocks (index bars carry no
    volume); on a stock its own turnover.
- Pools: per strategy the pool's own best timeframe and preset by the same rule on the pooled
  trades; all trades of one entry day are one draw.
- Selection and statuses: RESEARCH-RULES section 5 unchanged. Every cell of steps A, B and C
  with 60 TUNE trades on 30 entry days counts as examined. Placebo: 20 shuffles; day-level
  labels shuffled over the sessions of each instrument and calendar year; the trade-level C6
  and C8 shuffled over the trades of each instrument and calendar year. In the placebo the
  real-premium test is replaced by the model less the penalty below.
- Real-premium reading for an option cell that passes on the model: real one-minute bars from
  `data/expired_options.db` where the strike traded (store only), otherwise the model less the
  larger of 1.0 point of premium and 1.0% of the premium.
- No look-ahead: a truncation test of the condition labels on 60 random trades, and of the
  strategies' signals on 50 random signals (data cut after the signal bar, signal unchanged).
