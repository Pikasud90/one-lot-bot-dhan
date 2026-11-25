# Setup grid, instrument by instrument (US-091), 2026-11-24

Script `scripts/setup_grid.py`; outputs in `data/research/setup_grid_2026-11-24/` of the main
checkout. Read-only on both stores; no request to Dhan. Three numbers in a row are TUNE / CHOOSE /
TEST (to 2024-11-07, to 2025-11-07, to 2026-11-07).

## The answer in five lines

1. Nothing passes. 7,632 cells were tried and 3,948 had enough trades to be examined: 4 were
   discovered, 0 confirmed, 0 passed. Luck alone gives about 24, 4 and 2.
2. Fixing the timeframe per instrument does not carry forward: the best setup of every index
   by TUNE fell to about zero or below in CHOOSE or TEST (table below).
3. Four option cells are WATCH: positive in all three periods on model premiums, none strong
   enough to be confirmed. Three are on SENSEX, whose option spreads measured live are wide.
4. What NOT to trade is clear and stable (1,343 AVOID cells): `intraday_momentum` on index
   options at every timeframe, and every registered same-day rule on stocks at one share,
   `mean_reversion` at 5m first (it was the desk's own stock slot until 12 November).
5. Slower timeframes lose least for the index trend rules (30m and 60m against 5m and 15m),
   because they trade less; none of them earns.

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
  real-premium test is replaced by the model less the penalty below. "Clearly above luck"
  (for CANDIDATE), fixed here: the PASSED count is at least twice luck's count (arithmetic
  over all examined cells plus the placebo mean for the conditioned ones) and above the
  highest count of the 20 shuffles.
- Real-premium reading for an option cell that passes on the model: real one-minute bars from
  `data/expired_options.db` where the strike traded (store only), otherwise the model less the
  larger of 1.0 point of premium and 1.0% of the premium.
- No look-ahead: a truncation test of the condition labels on 60 random trades, and of the
  strategies' signals on 50 random signals (data cut after the signal bar, signal unchanged).

## Results (run finished 25 November 2026; the lead wrote this part from `report.txt`)

No look-ahead: condition labels 60 of 60 unchanged with later data cut away, strategy signals
50 of 50. 1,177 signal runs, no error. Stock round trip at one share of Rs 1,000: 16.6 bp.

### Counts against luck

| | examined | discovered | confirmed | passed |
|---|---|---|---|---|
| This study | 3,948 (A 819, B 417, C 2,712) | 4 | 0 | 0 |
| Luck, by arithmetic | | 23.7 | 3.8 | 1.9 |
| Conditioned cells only (step C) | 2,712 | 4 | 0 | 0 |
| Placebo on those, mean of 20 (highest) | 2,691 | 1.0 (3) | 0.0 (0) | 0.0 (0) |

The four discoveries are a little more than the shuffles gave, but none was confirmed.

### Step A: the timeframe each index fixed (default preset, TUNE % a trade)

| Strategy | NIFTY | BANKNIFTY | FINNIFTY | SENSEX |
|---|---|---|---|---|
| cpr_breakout | 60m +0.08 | 60m +0.37 | 60m +2.66 | 60m +0.66 |
| ema_cross | 30m +0.45 | 30m +1.10 | 30m +1.09 | 30m +0.30 |
| intraday_momentum | 5m −1.78 | 5m −2.13 | 30m −2.27 | 5m −1.45 |
| microtrend (own 3m) | −0.34 | +0.52 | −0.41 | −0.13 |
| nr7_breakout | 5m −1.63 | 15m +2.35 | 5m −0.27 | 5m −1.32 |
| orb | 60m +0.17 | 60m −0.41 | 60m −0.76 | 60m +0.61 |
| supertrend_adx | 5m −0.25 | 5m +0.52 | 5m −1.21 | 5m −0.12 |
| supertrend_mtf | 60m +0.27 | 60m +0.17 | 30m +1.34 | 30m +1.15 |
| supertrend_vol | 60m +0.71 | 15m +0.68 | 30m +0.25 | 30m −0.10 |

`ema_pullback` never reached 60 TUNE trades on an index. For the stocks the fixed timeframe is
5m for most rules that trade often and 30m or 60m for the trend rules; the per-stock table is
in `report.txt` (step A) and one `profile_<INSTRUMENT>.csv` per instrument.

### Per instrument: the fixed setup with the best TUNE mean

| Instrument | Setup, timeframe, preset | TUNE / CHOOSE / TEST | TUNE t, trades | Status |
|---|---|---|---|---|
| NIFTY options | supertrend_vol 60m high | +2.56 / −2.10 / −0.72 | +1.3, 112 | REJECTED |
| BANKNIFTY options | nr7_breakout 15m default | +2.35 / −3.61 / +0.16 | +1.3, 116 | REJECTED |
| FINNIFTY options | cpr_breakout 60m default | +2.66 / −1.97 / −3.59 | +1.8, 252 | REJECTED |
| SENSEX options | ema_cross 30m high | +1.45 / +3.37 / +1.34 | +1.1, 198 | REJECTED |
| Four indices pooled | ema_pullback 30m default | +1.21 / +2.14 / −7.27 | +0.4, 120 | REJECTED |
| RELIANCE | ema_cross 60m low | +0.06 / −0.03 / −0.14 | +0.8, 192 | REJECTED |
| M&M | cpr_breakout 15m default | +0.03 / −0.11 / −0.42 | +0.2, 137 | REJECTED |
| SBI | cpr_breakout 30m default | +0.04 / −0.22 / −0.35 | +0.4, 136 | REJECTED |
| HDFC Bank | ema_cross 60m default | −0.03 / −0.18 / −0.18 | −0.4, 157 | REJECTED |
| ITC | supertrend_mtf 15m default | −0.01 / −0.24 / −0.06 | −0.3, 319 | REJECTED |
| Tata Motors (TMPV) | vwap_pullback 15m low | +0.09 / −0.22 / −0.03 | +1.1, 144 | REJECTED |
| Maruti | ema_cross 60m default | +0.04 / −0.22 / −0.16 | +0.5, 165 | REJECTED |
| ICICI Bank | turtle_soup 15m default | −0.02 / −0.20 / −0.26 | −0.3, 161 | REJECTED |
| Hero MotoCorp | supertrend_vol 30m high | +0.08 / −0.25 / −0.44 | +0.6, 126 | REJECTED |
| Infosys | cpr_breakout 30m high | −0.00 / −0.34 / −0.20 | −0.0, 113 | REJECTED |
| TCS | orb 30m default | −0.03 / +0.13 / −0.40 | −0.5, 199 | REJECTED |
| Axis Bank | supertrend_vol 60m low | +0.07 / −0.31 / +0.05 | +0.7, 108 | REJECTED |
| Kotak Bank | supertrend_vol 30m high | +0.10 / −0.24 / −0.21 | +1.1, 107 | REJECTED |
| L&T | cpr_breakout 5m low | −0.06 / −0.07 / −0.01 | −0.8, 164 | REJECTED |
| Bajaj Finance | supertrend_vol 30m high | +0.05 / +0.01 / +0.17 | +0.5, 147 | REJECTED |
| Bharti Airtel | supertrend_vol 30m low | +0.07 / −0.21 / −0.17 | +0.8, 193 | REJECTED |
| Tata Steel | cpr_breakout 15m default | +0.08 / −0.21 / +0.00 | +0.9, 165 | REJECTED |
| Sun Pharma | ema_pullback 5m default | −0.07 / −0.08 / −0.41 | −0.7, 138 | REJECTED |
| 18 stocks pooled | ema_pullback 60m default | +0.03 / −0.08 / −0.16 | +0.3, 305 | REJECTED |

### WATCH: positive in all three periods, t of 2 or more over the whole span (model premiums)

| Cell | TUNE / CHOOSE / TEST | t whole, trades | By year (2022 to 2026) |
|---|---|---|---|
| NIFTY cpr_breakout 60m low, after an ACTIVE session | +3.75 / +1.20 / +2.00 | +2.6, 100 | −2.04, +6.27, +6.94, +0.16, +1.27 |
| SENSEX supertrend_mtf 30m, VIX below its median | +3.26 / +1.05 / +1.13 | +2.2, 181 | +1.11, +2.93, +7.11, +1.51, +0.29 |
| SENSEX supertrend_mtf 30m, after a CALM session | +2.86 / +2.01 / +3.72 | +2.6, 192 | +1.21, +4.39, +5.05, +1.68, +4.46 |
| SENSEX ema_cross 30m high, VIX below its median | +2.93 / +4.37 / +1.38 | +2.2, 97 | +0.07, +3.11, +11.23, +2.83, +2.45 |

Each misses a threshold (TUNE t under 2.5, or CHOOSE t under 1.0). They are four of 2,712
conditioned cells, on model premiums; the desk's recorded quotes show a median half spread of
about 1.5% on SENSEX options (`LIVE_ALERTS_2026-11-24.md`), against 0.6% in the model, which
takes most of what these cells show. Worth watching on paper, not worth an alert.

### Discovered on TUNE, then failed

| Cell | TUNE / CHOOSE / TEST | TUNE t |
|---|---|---|
| FINNIFTY cpr_breakout 60m, VIX below its median | +6.08 / −0.68 / −2.63 | +2.9 |
| Four indices cpr_breakout 60m, VIX below its median | +3.01 / −1.12 / −1.59 | +2.6 |
| FINNIFTY cpr_breakout 60m, after an ACTIVE session | +5.83 / −1.58 / −5.52 | +2.6 |
| Four indices ema_cross 30m, high first-half-hour volume | +3.44 / −2.58 / −1.77 | +2.5 |

### AVOID: 1,343 cells (63 on options, 1,280 on stocks)

- Index options, `intraday_momentum`, every timeframe and preset: −1.3% to −2.1% a trade, t of
  −5 to −9, negative in all three periods on each index (pool, 5m: −1.83 / −1.20 / −1.20).
- Stocks at one share, same day: `mean_reversion` 5m −0.16% a trade in every period, on every
  stock and under every condition (pool t beyond −80; 336 cells); then donchian_breakout
  (200 cells), turtle_soup (149), ema_cross (124), vwap_pullback (108), supertrend_mtf (77),
  orb (71), squeeze (68), supertrend_vol (52), ema_pullback (41), cpr_breakout (30).
  No condition turns any of them positive: the round trip of 16.6 bp is larger than any edge.

### What could not be done, and doubts

- TEST was already seen for the default settings in earlier studies; it is not a clean test.
- No cell passed on the model, so the real-premium stage had nothing to price.
- Presets are only the Lab's `low` and `high` beside the default; conditions one at a time.
- Stage 0 (what earlier runs say, per instrument) is in `inventory/` of the output folder as
  one CSV per instrument; it proposed nothing that this grid did not then test.
- The expiry condition rests on the harness's own contract calendar (see C7 above).

## Rows for the setup book

| Instrument | Kind | Setup and preset | Timeframe | Condition | Exit | Trades a year | % a trade TUNE / CHOOSE / TEST (t whole span) | Real-premium or cost note | Status | Remark |
|---|---|---|---|---|---|---|---|---|---|---|
| NIFTY | option same-day | cpr_breakout low | 60m | after an active session | desk rule | 20 | +3.75 / +1.20 / +2.00 (+2.6) | model; less 1 point +2.09 | WATCH | thin: 14 and 17 trades in CHOOSE and TEST |
| SENSEX | option same-day | supertrend_mtf default | 30m | VIX below its median | desk rule | 36 | +3.26 / +1.05 / +1.13 (+2.2) | model; live spreads wide | WATCH | spread of 1.5% a side would take it |
| SENSEX | option same-day | supertrend_mtf default | 30m | after a calm session | desk rule | 38 | +2.86 / +2.01 / +3.72 (+2.6) | model; live spreads wide | WATCH | the steadiest by year of the four |
| SENSEX | option same-day | ema_cross high | 30m | VIX below its median | desk rule | 19 | +2.93 / +4.37 / +1.38 (+2.2) | model; live spreads wide | WATCH | 97 trades in five years |
| Four indices | option same-day | intraday_momentum, any | any | all | desk rule | 800 | −1.83 / −1.20 / −1.20 (−9.5) | model | AVOID | loses in every period on every index |
| 18 stocks | stock same-day | mean_reversion, any | 5m | all | desk rule | 10,000 | −0.16 / −0.16 / −0.16 (−90) | 16.6 bp round trip | AVOID | no condition helps; the old desk slot |
| 18 stocks | stock same-day | every other registered rule | fixed per stock | all | desk rule | varies | best stock +0.10 on TUNE, none holds | 16.6 bp round trip | REJECTED | one share cannot carry the costs |
| Each index | option same-day | its best fixed setup | see table | all | desk rule | 25 to 50 | positive on TUNE, not after | model | REJECTED | a timeframe chosen on the past did not hold |
