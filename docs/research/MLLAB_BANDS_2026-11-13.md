# Machine Learning Lab: what its bands are worth and how to use it (2026-11-13)

The owner asked: the up probability is no signal (MLLAB_CONFIDENCE_2026-11-13.md), so is the
Lab usable at all, as it is or after a retune? This note measures the other half of every
cell, the band. It is evidence about the tool, not advice.

## 1. What was measured

- The product's own `walk_forward` on the same bars as the confidence study: 23 instruments,
  the most recent 3,000 bars of 15m and of 30m, 2,500 of 60m, 1,200 of 4h and of 1d (the
  first 300 are history only). 216,200 forecasts for each of the five models.
- Coverage: how often the next close lands inside the "68%" band and inside the "95%" band.
- Sharpness: is a wider band followed by a larger move? Bands are ranked into thirds within
  each instrument. "Wide over narrow" is the mean absolute move after the widest third over
  that after the narrowest third; 1.00 means the width says nothing.
- Script: `scripts/mllab_bands.py` (read-only, about two minutes).

## 2. The bands as the product draws them today

Share of next closes inside the "68%" band: all candles (the day's first candle / the others).

| Timeframe | arima, sarima | garch | ewma | empirical |
|---|---|---|---|---|
| 15m | 82.9 (27.8 / 85.3) | 82.9 (27.5 / 85.3) | 79.7 (22.7 / 82.1) | 67.6 (19.0 / 69.7) |
| 30m | 81.5 (35.4 / 85.5) | 82.7 (34.6 / 86.8) | 79.6 (29.8 / 83.8) | 66.4 (22.7 / 70.1) |
| 60m | 80.1 (45.1 / 86.1) | 80.5 (43.6 / 86.9) | 78.3 (40.1 / 84.9) | 66.4 (30.0 / 72.6) |
| 4h | 78.2 (63.7 / 92.7) | 78.0 (63.0 / 93.1) | 75.7 (59.7 / 91.7) | 67.6 (50.9 / 84.3) |
| 1d | 77.4 | 74.2 | 72.1 | 67.2 |

Share inside the "95%" band, the same layout.

| Timeframe | arima, sarima | garch | ewma | empirical |
|---|---|---|---|---|
| 15m | 95.4 (48.7 / 97.4) | 95.6 (47.7 / 97.6) | 94.3 (40.8 / 96.6) | 94.1 (43.8 / 96.3) |
| 30m | 94.7 (61.5 / 97.5) | 95.1 (61.4 / 98.0) | 93.9 (54.9 / 97.3) | 93.7 (56.9 / 96.8) |
| 60m | 94.0 (72.7 / 97.7) | 94.3 (72.3 / 98.1) | 93.2 (67.8 / 97.6) | 93.7 (71.4 / 97.5) |
| 4h | 93.6 (88.2 / 99.1) | 93.9 (88.7 / 99.2) | 92.7 (86.6 / 98.8) | 93.8 (88.7 / 99.0) |
| 1d | 95.0 | 94.4 | 93.5 | 93.8 |

Wide over narrow (the day's first candle left out).

| Timeframe | arima | sarima | garch | ewma | empirical |
|---|---|---|---|---|---|
| 15m | 1.05 | 1.05 | 1.27 | 1.51 | 1.32 |
| 30m | 1.01 | 1.02 | 1.28 | 1.48 | 1.28 |
| 60m | 1.20 | 1.20 | 1.40 | 1.53 | 1.41 |
| 4h | 1.03 | 1.03 | 1.29 | 1.42 | 1.06 |
| 1d | 0.84 | 0.84 | 1.26 | 1.30 | 1.00 |

What this says:

- The bands are mislabelled inside the day. The day's first candle carries the overnight
  gap, and the models put it in the same series as the ordinary candles. One gap a day makes
  a fitted band too wide for an ordinary candle (the "68%" band holds 82 to 93% of them) and
  every band far too narrow for the first candle (it holds 19 to 64% of them, and the "95%"
  band only 41 to 89%).
- Over all candles the "95%" band looks right (93 to 96%) only because the two errors cancel.
- `empirical` is the one band that is labelled about right today (66 to 68% over all candles).
- The width of `ewma` and of `garch` carries real information about the SIZE of the next
  move: after a wide band the move averaged 1.3 to 1.5 times the move after a narrow one.
- `arima` and `sarima` add nothing of their own. Their direction is a coin flip and their
  width hardly changes (1.01 to 1.20; on 1d it points the wrong way).

Other checks on the same forecasts:

- Five candles ahead: the "68%" band widened by the square root of 5 held 73 to 78% of the
  closes five candles later for the fitted models, 68 to 74% for `ewma`.
- A close outside the "95%" band happened on about 1 candle in 20. The next candle did
  nothing special: +1.4 bp after a close above (t 0.9) and +1.3 bp after a close below
  (t 0.7) on 15m; the largest reading is +11.8 bp after a 4h close below (t 1.5). No signal.
- Typical width, the median "68%" half width of `garch` in basis points of price: indices
  16 (15m), 25 (30m), 30 (60m), 60 (4h), 83 (1d); stocks 28, 43, 52, 101, 137.

## 3. The retune that was tested

Each model is given two return series of its own instead of one: the opening candles only
(previous close to the first candle's close, one a session) and the ordinary candles only.
The forecast for the next candle uses the series that candle belongs to. Run with
`scripts/mllab_bands.py regimes`; the opening series there starts from 120 sessions of
history and grows, where the product would read 180 calendar days.

| Timeframe | Model | Ordinary candles: 68 / 95 | Wide over narrow | Opening candles: 68 / 95 | Wide over narrow |
|---|---|---|---|---|---|
| 15m | arima, sarima | 75.9 / 94.2 | 1.08 | 70.3 / 90.6 | 1.06 |
| 15m | garch | 75.7 / 94.5 | 1.42 | 72.8 / 92.9 | 1.40 |
| 15m | ewma | 73.3 / 93.5 | 1.47 | 73.0 / 93.2 | 1.54 |
| 15m | empirical | 67.2 / 94.0 | 1.32 | 57.6 / 90.7 | 1.02 |
| 30m | arima, sarima | 73.8 / 93.3 | 1.06 | 70.1 / 90.9 | 1.08 |
| 30m | garch | 74.4 / 94.3 | 1.49 | 72.0 / 92.4 | 1.30 |
| 30m | ewma | 72.6 / 93.6 | 1.58 | 73.3 / 93.2 | 1.48 |
| 30m | empirical | 66.6 / 93.7 | 1.36 | 59.0 / 90.9 | 1.06 |
| 60m | arima, sarima | 73.3 / 92.9 | 1.21 | 69.0 / 90.5 | 1.06 |
| 60m | garch | 74.3 / 94.0 | 1.50 | 70.9 / 91.9 | 1.28 |
| 60m | ewma | 72.8 / 93.2 | 1.54 | 71.6 / 92.9 | 1.51 |
| 60m | empirical | 66.3 / 93.6 | 1.36 | 59.7 / 91.0 | 1.03 |
| 4h | arima, sarima | 72.9 / 93.2 | 0.93 | 74.6 / 93.4 | 0.96 |
| 4h | garch | 73.3 / 93.4 | 1.16 | 73.8 / 94.0 | 1.28 |
| 4h | ewma | 72.5 / 93.0 | 1.35 | 72.4 / 93.8 | 1.44 |
| 4h | empirical | 64.5 / 93.2 | 0.87 | 66.1 / 93.6 | 0.93 |

With its own series the "68%" band holds 69 to 76% and the "95%" band 90 to 95% for both
kinds of candle (the fitted models; returns have fat tails, so a normal band holds a little
more than 68% in the middle and a little less than 95% at the edge). The opening candle goes
from a band that held 23 to 64% to one that holds 69 to 75%. `garch` also reads the size of
the next move better once the gap is out of its series (1.42 to 1.50 on 15m to 60m, from
1.27 to 1.40). `empirical` is too narrow on opening candles (58 to 60% on 15m to 60m): its
percentiles need more than 120 to 360 sessions.

## 4. How the Lab can be used

What it can tell you:

1. **How far the next candle is likely to go.** That is the band. As drawn today its half
   width for an index is about 16 bp on 15m and 30 bp on 60m, for a stock about 28 and 52;
   retuned, the band of an ordinary candle is narrower and that of the opening candle wider.
2. **Whether an instrument is calm or active now.** The `ewma` and `garch` widths rise and
   fall with the market, and the next move follows them: about 1.5 times larger after a wide
   band than after a narrow one.
3. **That a candle was unusual.** A close outside the "95%" band is about 1 candle in 20. It
   marks news or a break. It did not say where the next candle goes.

Readings a trader can take from it (what the numbers support, not advice):

- A stop or a target nearer than one band half width of the timeframe traded is inside
  ordinary noise: with a retuned band about one candle in four ends beyond it by chance.
- An index option needs about 5 to 10 bp of index move to cover its costs
  (ALPHA_SEARCH_2026-11-12.md, section 4). The band of one 15m index candle is 16 bp or
  less, so a trade pays only through a run of candles one way, the part no model here
  predicts.
- The opening candle is a different animal: its typical move is about 5 times an ordinary
  15m candle's. A band drawn for ordinary candles says nothing about it.
- The daily band of a stock is about 137 bp. The pullback rule's stop at 15% below the entry
  is about 11 daily bands away.

What it cannot tell you: direction. The up probability, agreement of all models, strong
agreement and the candle after an unusual close were all tested, and none beats a coin flip
(MLLAB_CONFIDENCE_2026-11-13.md and section 2 above).

## 5. Verdict

- **As it is:** a rough range gauge only. Trust `empirical` for the "68%" band, trust no
  band for the day's first candle, and read every "up %" as 50%.
- **Retuned (section 3, with the page leading on the range instead of the up probability):**
  an honest gauge of range and of calm or active markets. That has a real use for placing
  stops and targets and for judging whether a move is ordinary.
- **Not on offer either way:** a signal for the direction of the next candle. Five years of
  tests on these timeframes found none (STRATEGIES_TESTED_2026-11-12.md), and re-fitting
  ARIMA or GARCH does not change that; they model the size of moves, not their sign.

**Built (US-072, 2026-11-13).** The retune of section 3 is in the Lab: each model gets two return
series, the opening candles and the ordinary candles, and forecasts the next candle from the one
it belongs to (every intraday timeframe now reads about 120 stored sessions). The cells lead with
the range and both band hit rates, the up figure is marked "not a signal", and each instrument
shows its "Move size" (calm, normal or active).
