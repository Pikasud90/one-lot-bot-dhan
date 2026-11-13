# Machine Learning Lab: does "p_up" mean anything? (2026-11-13)
Question: when the Lab's models say "above 50% up", is the next candle more likely to rise?
No: not for single models, not when all of them agree, on no timeframe.

## How it was tested
- The product's own `walk_forward` from `src/onelot/mllab/models.py`: the forecast for bar t sees
  closes up to t−1 only, parameters re-fit every 20 steps. Script: `scripts/mllab_confidence.py`.
- 23 instruments (5 indices, 18 stocks), bars up to 2026-11-12 (complete bars only). Most recent
  15m 3,000 (about 4 months), 30m 3,000, 60m 2,500, 4h 1,200, 1d 1,200 (about 5 years);
  the first 300 bars are history only. 62,100 / 62,100 / 50,600 / 20,700 / 20,700 steps.
- Directional models: arima, sarima, garch, empirical. `ewma` has a zero mean, so its p_up is
  always exactly 50%: it is left out of every direction test.
- "Up%" is the share of next bars that closed higher (a flat bar counts as not up, so the base is
  a little under 50% intraday). Returns are log returns in basis points (bp). t treats all bars of
  one calendar day, across instruments, as one draw. Run time 101 s.

## 1. Single models (pooled, next bar)
| tf | base up% | arima >.5 / <.5 | sarima >.5 / <.5 | garch >.5 / <.5 | empirical >.5 / <.5 |
|----|----|----|----|----|----|
| 15m | 48.2 | 48.7 / 47.8 | 48.1 / 48.4 | 48.5 / 48.1 | 49.1 / 47.9 |
| 30m | 48.0 | 48.1 / 47.8 | 47.9 / 48.0 | 47.8 / 48.0 | 48.9 / 47.6 |
| 60m | 49.0 | 49.3 / 48.7 | 49.4 / 48.6 | 48.6 / 49.4 | 49.4 / 48.8 |
| 4h | 48.9 | 49.3 / 48.3 | 49.5 / 48.0 | 49.1 / 48.4 | 49.7 / 48.4 |
| 1d | 51.4 | 51.3 / 51.9 | 51.3 / 51.7 | 51.3 / 52.4 | 52.0 / 50.4 |

The gap between "above" and "below" is under 2 points everywhere and goes the wrong way about
as often as the right way. Mean next-bar returns differ by under 2 bp intraday (no t above 1.7).
Calibration: by p_up bucket (<.45, .45 to .48, .48 to .52, .52 to .55, >.55) the realised up share
is flat. Example 15m arima: 49.0, 47.9, 48.0, 49.1, 50.3%. The only bucket with a small slope is
`empirical` at 15m (46.6% below .45 to 53.9% above .55, 1,375 bars), which did not repeat at 60m
(48.1 to 49.5%). A "58%" on the page is in practice a coin flip.

## 2. Agreement: all four above 0.5 ("all up"), all four below ("all down")
| tf | all up: share, up%, bp, t | all down: share, up%, bp, t | mixed: up%, bp |
|----|----|----|----|
| 15m | 8.6%, 47.3, −0.19, −0.4 | 26.8%, 47.0, 0.05, 0.1 | 48.9, 0.36 |
| 30m | 7.0%, 49.0, −0.16, −0.3 | 31.5%, 48.3, 0.11, 0.2 | 47.7, −0.40 |
| 60m | 11.8%, 49.8, 0.40, 0.4 | 20.7%, 48.0, 0.41, 0.4 | 49.2, −0.13 |
| 4h | 17.6%, 48.8, −2.09, −0.9 | 18.1%, 47.4, 0.11, 0.0 | 49.3, 1.81 |
| 1d | 41.9%, 52.0, 4.28, 1.4 | 3.4%, 51.4, 1.38, 0.2 | 50.9, 5.61 |

"All up" is never better than an ordinary bar, and "all down" is not bearish. Strong agreement (all
above 0.52 / all below 0.48) is rare (0.2 to 10% of bars) and no better: 15m strong up 45.7% up,
−0.84 bp; 30m 39.3%, −5.4 bp (117 bars); 1d 52.2%, 5.4 bp. Indices and stocks alike: e.g. 4h
indices all up −4.1 bp (t −1.6), 60m stocks all up 0.84 bp (t 0.8). On 1d "all up" just tracks
the market's drift: every daily bar averaged 4.9 bp, "all up" 4.3 bp.

## 3. Stickiness
| tf | all up share | still all up t+1/3/5 | bp after all up, 1/3/5 | bp any bar, 1/3/5 |
|----|----|----|----|----|
| 15m | 8.6% | 65 / 62 / 59% | −0.2 / 0.8 / 1.0 | 0.2 / 0.7 / 1.1 |
| 30m | 7.0% | 62 / 59 / 57% | −0.2 / −0.9 / −0.3 | −0.2 / −0.7 / −1.1 |
| 60m | 11.8% | 69 / 65 / 64% | 0.4 / 0.4 / −0.5 | 0.1 / 0.1 / 0.2 |
| 4h | 17.6% | 71 / 68 / 67% | −2.1 / −8.9 / −10.2 | 0.8 / 2.5 / 3.9 |
| 1d | 41.9% | 78 / 76 / 74% | 4.3 / 11.3 / 19.7 | 4.9 / 14.6 / 24.2 |

The SIGNAL sticks (an "all up" bar is followed by another about two times in three; runs last
about 3 bars), because the models are re-fitted only every 20 bars and move slowly. The PRICE does
not follow: returns after "all up" are the same as, or below, those after any bar. After "all down"
likewise nothing (15m 0.05 / 0.3 / 0.8 bp; 1d 1.4 / 14.5 / 27.5 bp, i.e. up, not down).
First bar of an "all up" run, 1 / 3 / 5 bars: 15m −0.2 / 1.2 / 1.3 bp; 60m −0.4 / −1.0 / −4.8;
4h 2.3 / −4.3 / −3.6; 1d 9.5 / 19.7 / 32.0 bp (t 2.3 / 2.7 / 3.4 against zero). The daily figure is
the one hint in the study: 9.5 bp against 4.9 bp for any day, a difference of about 1 t, and the
3 and 5 day t-values are inflated by overlapping windows (every day's 5-day return also has
t 3.9). Not a finding.

## 4. Could it pay?
On 15m to 60m no "agree" cell beats 1.3 bp a bar (60m strong up, t 0.6); the best 4h cell is
5.2 bp (indices "all down", 105 bars, t 1.0, the wrong way for a bullish reading). A stock
intraday round trip costs about 16 bp; an option buyer needs 5 to 10 bp of index move. The 15m to
60m best case is 10 times too small, and most cells are zero or negative. The daily "first all up"
9.5 bp is 4.6 bp above the market's own drift and not significant. Nothing here covers cost.

## 5. Stability (first half / second half of each instrument's bars)
| tf | all up bp (t) | all down bp (t) | all up share |
|----|----|----|----|
| 15m | −0.11 (−0.2) / −0.44 (−0.4) | 0.32 (0.6) / −0.09 (−0.2) | 12.8% / 4.4% |
| 60m | 0.94 (1.0) / −1.51 (−0.7) | 1.04 (0.9) / 0.10 (0.1) | 18.3% / 5.2% |

Signs flip between halves; how often "all up" fires depends on the period's trend, not on skill.
## Verdict for the owner
1. "All models above 50%" is not a bullish sign. The next candle closed up 47 to 52% of the time,
   the same as any candle, on every timeframe, for indices and for stocks.
2. It sticks as a display (the models move slowly), but the price does not stick to it: returns
   over the next 1, 3 and 5 bars after agreement equal those after any bar.
3. Not usable for trading. Treat p_up as "no edge, roughly 50/50"; the Lab's useful output is the
   size of the bands (expected range), which held about as often as stated in the earlier check.

Caveats: the product's own settings only; intraday samples span 4 to 12 months; the overnight gap
counts as a "next bar" as on the page. Outputs: `data/research/mllab_2026-11-13/`.
