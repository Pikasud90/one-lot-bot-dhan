# Regime study (US-080), 2026-11-23

Script `scripts/regime_study.py`; outputs in `data/research/regime_2026-11-23/` of the main
checkout. Read-only on the store; no Dhan request. Three numbers in a row are TUNE / CHOOSE / TEST
(to 2024-11-07, to 2025-11-07, to 2026-11-07).

## What was fixed in advance (written before any result)

- Question: does any registered strategy earn after costs inside one market condition known
  before the session opens?
- Instruments: NIFTY, BANKNIFTY, FINNIFTY, SENSEX options (a long signal buys a call, a short
  buys a put, one lot, same day, % of premium by the harness's model `h.pnl`, and that less 1.0
  point as the real-premium reading); the 18 desk stocks (one share, same day, long and short,
  % of price after intraday equity charges and 0.03% slippage a side).
- Strategies: every strategy of `REGISTRY` that `SUITABLE` marks for the kind (10 for options,
  14 for stocks), default parameters and default timeframe, signals from `generate()` through
  the Lab's `generate_for_instrument`. Entry at the open of the minute after the signal bar
  closes, inside the strategy's own window (its `long_entry` / `short_entry`), one trade at a
  time per instrument and strategy.
- Exit: `src/onelot/manage/ratchet.py`, first stop 1.5 x ATR(14, Wilder) of 60m bars of the
  managed price read on the last bar closed at the entry, target 3 risks away, `extend` on, the
  strategy's own exit kept (next minute's open), the harness's session exit (15:15 options,
  15:10 stocks). Exit engine of `scripts/ratchet_study.py` (proven equal to the module).
- Conditions from daily bars to the previous session only, per instrument: ADX(14, Wilder) and
  close against SMA20. UP-TREND: ADX of 25 or more and close above SMA20; DOWN-TREND: ADX of 25
  or more and close below; CHOPPY: ADX under 20; NEUTRAL: the rest. Robustness cut: India VIX
  above or below its 250-session median, previous day's values.
- Counts: cell = strategy x condition x group, reported with 100 trades or more. CANDIDATE: TUNE
  mean above 0 with t of 2 or more and CHOOSE mean above 0. Its test: TEST mean above 0, and for
  options above 0 after less 1.0 point. Chance alone: about 2.5% of cells pass the TUNE half.
  Switch: per condition and group the candidate with the highest TUNE; no trade without one.
  Every t treats all trades of one entry day (over the group's instruments) as one draw.

## How it was run, and what differs from the Lab

292 strategy x instrument runs (10 x 4 indices, 14 x 18 stocks), no errors. Signals cached in
`signals/`; trades in `trades.csv`; tables in `report.txt`. Where this differs from a Lab run:
- The strategy's own entry window is passed in. The Lab's `_session_params` puts the
  instrument's 09:30 to 14:45 over it, which leaves `gap_fade` (own window 09:15 to 09:45) with
  1 signal in five years on 18 stocks; with its own window it has 1,014 trades.
  `intraday_momentum` (own window 14:30 to 15:00) gives the same signals either way.
- Only the brief's entry rules: no cooldown, no two-a-day cap, no risk-size skip of the Lab's
  simulator; the strategy's own stop is replaced by the ratchet.
- Skipped: 266 entries on 2021-11-08 and 09 (no 60m ATR yet, the one-minute history starts
  there); 5 minutes with a long and a short signal took the long.
- Stock round trip at one share of Rs 1,000: 16.6 bp (intraday charges plus 0.03% slippage a
  side). The option plan cost inside the ratchet is the US-074 stand-in (0.05% of the index).

## Sessions in each condition (instrument-sessions, TUNE / CHOOSE / TEST)

| group | UP-TREND | DOWN-TREND | CHOPPY | NEUTRAL | VIX HIGH | VIX LOW |
|---|---|---|---|---|---|---|
| 4 indices | 600 / 148 / 78 (17%) | 436 / 202 / 161 (16%) | 1128 / 308 / 558 (40%) | 812 / 330 / 191 (27%) | 1444 / 416 / 600 | 1532 / 572 / 388 |
| 18 stocks | 3137 / 855 / 791 (21%) | 2187 / 785 / 1015 (18%) | 4892 / 1906 / 1672 (38%) | 3176 / 900 / 968 (23%) | 6498 / 1872 / 2700 | 6894 / 2574 / 1746 |

Each index is within 2 to 4 points of these shares. No session lacked a condition. TEST has few
up-trend sessions on the indices (78 over four indices), so up-trend TEST cells are thin.

## All conditions together, per strategy

Options, % of premium a trade on the model (t), sorted by TUNE:

| strategy (timeframe) | trades | % a trade (t) | win % | payoff | index bp | TUNE / CHOOSE / TEST |
|---|---|---|---|---|---|---|
| microtrend (3m) | 3116 | −0.27 (−0.7) | 41.1 | 1.35 | +3.3 | −0.10 / +0.08 / −1.20 |
| supertrend_mtf (5m) | 1517 | −0.54 (−0.8) | 47.5 | 1.01 | +1.0 | −0.22 / −0.62 / −1.45 |
| supertrend_adx (5m) | 549 | −0.33 (−0.5) | 41.7 | 1.30 | +3.9 | −0.26 / +1.04 / −2.17 |
| ema_cross (12m) | 2988 | −0.62 (−1.5) | 44.2 | 1.12 | +2.0 | −0.31 / −1.55 / −0.60 |
| supertrend_vol (5m) | 4129 | −0.88 (−3.1) | 37.1 | 1.39 | +1.7 | −0.74 / −0.52 / −1.73 |
| orb (5m) | 3921 | −0.84 (−2.0) | 50.7 | 0.85 | +1.6 | −0.78 / −0.41 / −1.48 |
| nr7_breakout (5m) | 428 | −1.68 (−2.0) | 46.0 | 0.84 | −0.0 | −1.00 / −3.81 / −2.08 |
| cpr_breakout (5m) | 2087 | −1.28 (−2.7) | 49.1 | 0.83 | +0.3 | −1.73 / −0.34 / −0.98 |
| ema_pullback (5m) | 441 | −1.81 (−2.1) | 43.1 | 0.96 | −1.1 | −1.76 / −0.34 / −3.28 |
| intraday_momentum (5m) | 4369 | −1.67 (−9.0) | 37.7 | 0.85 | −0.2 | −1.93 / −1.48 / −1.11 |

All ten lose in TUNE and in TEST; all option trades together −0.94% a trade, −1.94% less 1.0
point. Stocks: every one of 14 strategies loses in every period (−0.09% to −0.23% a
trade, t −2.5 to −94 overall); the best is `gap_fade` (−0.10 / −0.13 / −0.09), the worst
`turtle_soup` (−0.18 / −0.16 / −0.17). Their gross move in favour is −0.6 to +6.3 bp a trade
against a 16.6 bp cost.

## Candidates (the count against chance)

90 cells had 100 trades or more (34 options, 56 stocks; 6 option cells under 100 trades not
examined: ema_pullback, nr7_breakout and supertrend_adx in the two trend conditions). Chance
alone would pass the TUNE half in about 2.2 of them. **0 cells passed even the TUNE half** (TUNE
mean above 0 with t of 2 or more), so there are 0 candidates and nothing reached its TEST.

Highest TUNE cells, shown only to make the "no" concrete (none has a TUNE t near 2):

| cell (options) | trades | % a trade (t) | TUNE t | TUNE / CHOOSE / TEST |
|---|---|---|---|---|
| supertrend_vol, DOWN-TREND | 668 | +0.34 (+0.4) | +0.7 | +0.95 / +0.12 / −0.85 |
| supertrend_mtf, CHOPPY | 607 | −0.38 (−0.4) | +0.5 | +0.68 / +0.66 / −3.14 |
| microtrend, CHOPPY | 1338 | +0.04 (+0.1) | +0.6 | +0.43 / +0.31 / −1.03 |
| ema_cross, DOWN-TREND | 498 | −0.07 (−0.1) | +0.5 | +0.62 / −2.36 / +0.88 |

No stock cell has a positive TUNE mean; the highest, `gap_fade` in UP-TREND, is −0.04 / −0.13 /
+0.04 (221 trades, gross +13.3 bp a trade, still short of the cost).

## The switch on CHOOSE and TEST

No condition has a candidate in either group, so the switch takes no trade on CHOOSE or TEST.

## The VIX cut (robustness only)

48 cells (strategy x VIX side x group, 100 trades or more), chance about 1.2: 0 candidates.
The highest TUNE on options is supertrend_mtf with VIX below its median, +0.93 / +0.82 /
−1.01 (TUNE t +0.7); microtrend there is +0.65 / −0.04 / −0.96. On stocks only `gap_fade`
touches zero (+0.01 TUNE with VIX high, +0.05 TEST with VIX low); every other stock cell is
negative in every period.

## Trend-following and mean-reversion by condition (pooled, % a trade, TUNE / CHOOSE / TEST)

Trend-following here: the ten option strategies, and on stocks all but `mean_reversion`,
`gap_fade`, `turtle_soup` and `vwap_pullback` (the reversion set). No reversion strategy is
marked suitable for options.
- Options, trend-following: best DOWN-TREND −0.31 (t −0.6; −0.39 / −0.74 / +0.54), worst
  UP-TREND −1.19 (t −3.0; −1.23 / −0.80 / −1.63); CHOPPY −0.94, NEUTRAL −1.16.
- Stocks, trend-following: DOWN-TREND, CHOPPY and NEUTRAL −0.14, UP-TREND −0.16 (worst).
- Stocks, reversion: −0.16 in every condition; conditioning changes nothing.

## Verdict

(i) Is there a strategy and condition pair worth a paper test? No. Of 90 cells none passed the
TUNE half of the rule, below the 2.2 chance alone would give, so there is no candidate, no TEST
to read and no switch. With nothing passing, no timeframe or setup is recommended.

(ii) What not to trade where. Options: `intraday_momentum` loses in every condition (t −4.0
to −6.3), and `cpr_breakout`, `orb` and `supertrend_vol` lose in UP-TREND and NEUTRAL. The
trend-following option buys did worst in UP-TREND sessions (−1.19% a trade, t −3.0); why is
not tested here. Stocks: no same-day strategy of the registry, in any condition or VIX state,
covers the 16.6 bp round trip at one share; `mean_reversion` is the plainest loser (51,340
trades, 19% winners, −0.16% a trade).

(iii) Is anything here an edge after costs? No. Conditioning on the daily trend or on VIX did
not turn any registered same-day strategy positive in a way that survives its own t, on index
options or on stocks. This agrees with the earlier studies: the entries carry no measurable
edge, and a filter known before the open does not create one.

Not done or approximate: option results are on model premiums (real premiums taken as 1.0 point
lower, not priced); the option ratchet's plan cost is a stand-in; the Lab's cooldown, daily cap
and risk filters were not applied (the brief's entry rules only); a condition with ADX between
20 and 25 above or below SMA20 is NEUTRAL by the definition, which mixes weak trends of both
signs; conditions use each instrument's own cleaned daily bars.
