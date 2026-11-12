# Every strategy and rule this project has backtested (2026-11-12)

One table for index options and one for stocks, copied from the four research documents. Figures are
after costs and are backtest evidence, not advice.
Source key: CR = CROSS_RETUNE_2026-11-07.md, RO = REAL_OPTIONS_2026-11-08.md, ST =
SUPERTREND_MTF_2026-11-11.md, AS = ALPHA_SEARCH_2026-11-12.md (all in docs/research/; line numbers
are those of the table). TUNE / CHOOSE / TEST are the three periods; "model" = modelled premiums
(Black-76 from India VIX).

## Table A: index options

CR model = the retune's model, which charged slippage twice (CR line 3 to 6). Corr = RO's corrected
model (no double slippage). Real = Dhan traded premiums on the trades that could be priced; the
share priced is given as priced/all for TUNE, CHOOSE, TEST. Per cell: % a trade.

| Strategy or rule | Holding | Study and date | Trades (TUNE / CHOOSE / TEST) | Modelled premiums, TUNE / CHOOSE / TEST | Real premiums, TUNE / CHOOSE / TEST (priced/all) | Why it is not used | Source |
|---|---|---|---|---|---|---|---|
| supertrend_vol 30m, long | same day | Retune 7 Nov; real 8 Nov | 297 / 101 / 90 | CR +1.54 / +1.12 / −1.65; Corr +3.44 / +3.27 / +0.46 | +1.92 / −1.67 / −3.06 (136/297, 70/101, 59/90) | no setting positive in all three periods; real TEST −3.06. Kept in the paper test by owner decision of 8 Nov | CR 64; RO 78, 96 to 98, 63 and 213 |
| supertrend_vol 60m | same day | same | 47 / 12 / 9 | CR +2.12 / +2.08 / −5.97; Corr +4.02 / +4.11 / −3.41 | +3.07 / +1.59 / −6.59 (24/47, 10/12, 7/9) | "LOST in the test year" | CR 62; RO 79, 99 to 101 |
| ema_cross 30m | same day | same | 58 / 10 / 16 | CR +1.30 / +2.50 / −2.94; Corr +3.19 / +4.63 / −0.88 | +2.31 / +4.15 / −1.55 (34/58, 8/10, 12/16) | "LOST in the test year"; real TEST −1.55 | CR 63; RO 80, 102 to 104 |
| nr7_breakout 15m | same day | same | 46 / 16 / 9 | CR +0.83 / −0.61 / −6.11; Corr +2.96 / +1.76 / −4.28 | −5.88 / −3.76 / −7.54 (24/46, 13/16, 5/9) | lost in CHOOSE; real negative in all three | CR 65; RO 81, 105 to 107 |
| cpr_breakout 30m | same day | same | 69 / 19 / 18 | CR +2.56 / −2.62 / +0.41; Corr +4.59 / −0.93 / +2.78 | +3.86 / −1.87 / +7.17 (36/69, 12/19, 14/18) | lost in CHOOSE (real −1.87) | CR 66; RO 82, 108 to 110 |
| vwap_pullback 15m | same day | same | 45 / 14 / 17 | CR +0.01 / −3.11 / −3.82; Corr +1.79 / −1.41 / −1.60 | −0.93 / +0.85 / −2.65 (21/45, 9/14, 13/17) | lost in CHOOSE on the model; real TEST −2.65 | CR 67; RO 83, 111 to 113 |
| turtle_soup 30m | same day | same | 40 / 19 / 8 | CR +0.57 / −5.06 / −6.81; Corr +2.17 / −3.38 / −4.81 | −1.40 / −3.65 / −11.78 (21/40, 14/19, 3/8) | lost in CHOOSE; real negative in all three | CR 68; RO 84, 114 to 116 |
| ema_pullback 15m | same day | same | 99 / 40 / 38 | CR +3.81 / −4.85 / −3.81; Corr +5.86 / −2.70 / −1.77 | +5.87 / −3.73 / −3.19 (47/99, 31/40, 28/38) | lost in CHOOSE; real TEST −3.19 | CR 69; RO 85, 117 to 119 |
| orb 60m | same day | same | 50 / 28 / 22 | CR +2.89 / −5.59 / −2.97; Corr +4.55 / −3.59 / −1.31 | +2.08 / −5.86 / −5.46 (21/50, 20/28, 9/22) | lost in CHOOSE; real TEST −5.46 | CR 70; RO 86, 120 to 122 |
| supertrend_adx 15m | same day | same | 60 / 32 / 21 | CR +0.87 / −7.23 / −6.34; Corr +2.57 / −5.40 / −4.66 | −13.85 / −6.10 / −3.23 (23/60, 26/32, 11/21) | lost in CHOOSE; real negative in all three | CR 71; RO 87, 123 to 125 |
| donchian_breakout 60m | same day | same | 86 / 31 / 44 | CR −2.97 / −2.45 / −4.94; Corr −0.80 / −0.00 / −2.94 | +1.78 / −4.36 / −7.75 (48/86, 26/31, 30/44) | "profitable on only 20% of instruments in the tuning years" | CR 72; RO 88, 126 to 128 |
| mean_reversion 30m | same day | same | 78 / 26 / 19 | CR −1.67 / −2.34 / −5.19; Corr +0.11 / −0.31 / −3.14 | −1.36 / −4.40 / −4.43 (38/78, 19/26, 11/19) | "profitable on only 0% of instruments in the tuning years" | CR 73; RO 89, 129 to 131 |
| intraday_momentum 15m | same day | same | 371 / 121 / 140 | CR −1.86 / −2.18 / −4.03; Corr −0.01 / −0.06 / −1.94 | −1.58 / −0.20 / −2.19 (183/371, 89/121, 104/140) | "profitable on only 20% of instruments in the tuning years" | CR 74; RO 90, 132 to 134 |
| squeeze | same day | Retune 7 Nov | at most 29 in three years | not given | not given | "Too few trades to judge" | CR 76 |
| Random long entries (benchmark, 10 seeds) | same day | Retune 7 Nov | not given | TUNE −1.61 to −3.05; CHOOSE −2.69 to −3.82; TEST −3.42 to −4.01 | not given | loses in every period: "time decay plus charges" | CR 88 to 93 |
| Retune settings read again (supertrend_vol 30m, 68 settings) | same day | Re-read 11 Nov | 5,752 settings in all | median +0.33 / +1.17 / −4.10; positive in 60% / 69% / 6% of settings | not given | none of 5,752 settings positive after costs in all three periods | ST 185 |
| Lab index slot: supertrend_vol 30m, calls only | same day | Deep run 11 Nov | 485 (294 / 101 / 90) | +3.562 (t 2.24) / +3.286 (t 1.07) / +0.499 (t 0.23); five years +2.936 (t 2.38) | five years −0.139 (t −0.1), 265 of 488 priced (54%); periods +1.917 / −1.673 / −3.059 (ST 774) | up days +9.286, down days −12.403: "the direction of the day"; TEST t 0.23 | ST 214, 235, 774 |
| Owner's multi-timeframe rule as stated, calls, same day (60m line, exit on a close) | same day | Stage 1, 11 Nov | 1,940 | −0.359 / +0.178 / −1.234; five years −0.385 | not given | "Negative for every line"; best −0.39%, TEST −1.2% | ST 364, 28 |
| Same rule, 1m line, touch | same day | Stage 1 | 10,096 | −1.703 / −1.485 / −1.575; five years −1.639 (t −31.9) | not given | negative in every period | ST 358 |
| Owner's rule carried: `carry30` (calls, 30m line, exit on a close) | carried (1.4 sessions) | Stage 1 and 2, 11 Nov | 1,041 | +3.611 (t 1.9) / +11.764 (t 1.9) / −2.158 (t −0.6); five years +4.097 (t 2.3) | priced 423 (41%): −9.140 (t −8.0); periods −7.574 / −11.022 / −11.369 | "Not confirmed"; the unpriced trades are the modelled winners; t 2.3 is the best of about 15,000 | ST 555, 715, 728 to 730 |
| `carry60` (calls, 60m line by touch) | carried | Stage 2 | 715 | +5.086 (t 1.8) / +13.948 (t 1.9) / −1.430 (t −0.2); five years +5.770 (t 2.3) | priced 281 (39%): −8.857 (t −6.6) | not confirmed; negative in TEST | ST 556, 716 |
| `intra_volc` (calls, 30m, fresh turn, vol_c, all six agree) | same day | Stage 2 | 204 | +2.692 (t 1.5) / +5.162 (t 1.4) / +2.932 (t 1.1); five years +3.066 (t 2.2) | priced 110 (54%): +0.737 (t 0.3); periods +1.636 / −1.408 / +0.176 | "Not confirmed"; "not as a proven edge". In the paper test as slot supertrend_mtf | ST 557, 717, 762, 36 |
| `intra_15adx` (calls, 15m, adx_a) | same day | Stage 2 | 116 | +3.643 (t 1.6) / +5.729 (t 1.7) / −6.707 (t −2.3); five years +2.308 (t 1.3) | not given | negative in TEST (t −2.3) | ST 558 |
| `carry15_volc` (calls, 15m, 60m line) | carried | Stage 2 | 170 | +7.049 (t 1.3) / +34.302 (t 1.6) / +18.421 (t 0.9); five years +15.469 (t 2.2) | not given | "found with the TEST year in view" | ST 559 |
| `slot_vol` (supertrend_vol rebuilt with Supertrend(14, 1.7) and the line as stop) | same day | Stage 2 | 948 | +1.453 (t 1.3) / +0.807 / −0.694; five years +0.875 (t 1.0) | priced 544 (57%): −2.093 (t −2.3); periods −0.928 / −2.789 / −3.766 | negative on real premiums; TEST t −2.4 | ST 515, 718, 770 |
| `supertrend_adx` with its slot filter | same day | Stage 2 | 132 | five years +0.015 | not given | no edge (+0.015) | ST 518 |
| Call at 09:30, sold at the close (no signal) | same day | Stage 2 | not given | −1.185 / −0.980 / −1.815 | not given | the benchmark itself loses | ST 502 |
| Intraday buy-only signals (6 kinds, about 30 cuts); best "Fade yesterday's large move" | same day | Alpha search 12 Nov | 789 (best); 6,094 (call every session) | option, five years: +2.29 (t 1.3), periods +4.34 / −3.37 / +2.39; call every session −1.15 (t −2.0) | "No option rule reached the stage of real premiums" (AS 38) | "No edge found"; costs 1.2% to 1.6% a round trip; nothing survives a second sample | AS 84 to 90, 16, 109 |

## Table B: stocks

All retune rows: 18 stocks, one share, same day (intraday, entries until 11:30 where short). "Test
trades" are the TEST-year trades. Retune charges plus slippage cost "about 0.15 % per round trip,
which is the whole edge" (CR 130).

| Strategy or rule | Holding | Study and date | Trades | % a trade after charges (TUNE / CHOOSE / TEST) | Against simply holding | Why it is not used | Source |
|---|---|---|---|---|---|---|---|
| supertrend_vol 60m, short | same day | Retune 7 Nov | test 25 | +0.17 / −0.04 / +0.10 (2x costs −0.01) | not given | lost in CHOOSE; in the paper test; Lab deep run below | CR 110 |
| supertrend_adx 30m, short | same day | Retune 7 Nov | test 34 | +0.17 / −0.13 / +0.06 (2x costs −0.04) | not given | lost in CHOOSE; in the paper test | CR 112 |
| turtle_soup 30m | same day | same | test 38 | +0.04 / −0.10 / −0.17 | not given | lost in CHOOSE | CR 111 |
| ema_cross 60m | same day | same | test 25 | +0.05 / −0.23 / −0.11 | not given | lost in CHOOSE | CR 113 |
| donchian_breakout 30m | same day | same | test 20 | +0.15 / −0.38 / +0.15 | not given | lost in CHOOSE | CR 114 |
| gap_fade 15m | same day | same | test 166 | +0.02 / −0.17 / −0.07 | not given | lost in CHOOSE (brute force: positive in 16% / 2% / 0% of 61 settings, ST 191) | CR 115; ST 191 |
| vwap_pullback 30m | same day | same | test 15 | −0.06 / +0.11 / −0.09 | not given | "profitable on only 29% of instruments in the tuning years" | CR 116 |
| ema_pullback 60m | same day | same | test 9 | −0.02 / −0.07 / −0.30 | not given | "profitable on only 36% of instruments" | CR 117 |
| nr7_breakout 15m | same day | same | test 25 | −0.06 / −0.22 / +0.13 | not given | "profitable on only 33% of instruments" | CR 118 |
| cpr_breakout 30m | same day | same | test 111 | −0.03 / −0.11 / −0.07 | not given | "profitable on only 44% of instruments" | CR 119 |
| mean_reversion 30m | same day | same | test 265 | −0.15 / −0.11 / −0.22 | not given | "profitable on only 0% of instruments" | CR 120 |
| orb 15m | same day | same | test 134 | −0.11 / −0.18 / −0.21 | not given | "profitable on only 31% of instruments" | CR 121 |
| intraday_momentum 15m | same day | same | test 236 | −0.15 / −0.20 / −0.19 | not given | "profitable on only 0% of instruments" | CR 122 |
| squeeze | same day | same | at most 52 in three years | not given | not given | "Too few trades to judge" | CR 124 |
| supertrend_vol 60m, short (Lab deep run) | same day | Deep run 11 Nov | 129 after costs | +0.146 / −0.035 / +0.096; five years +0.096 (t 1.18), net ₹296 | before costs +0.245 (t 3.12) | "Neither result can be told from zero"; negative in CHOOSE | ST 265 |
| supertrend_adx 30m, short (Lab deep run) | same day | Deep run 11 Nov | 175 after costs | +0.136 / −0.132 / +0.064; five years +0.050 (t 0.59), net ₹274 | before costs +0.220 (t 2.63) | same; negative in CHOOSE | ST 266 |
| Buy at 09:30, sell at the close (no signal) | same day | Stage 1 | 22,284 | five years −0.171 (t −9.2); short −0.162 (t −8.7) | "The cost of a round trip is the whole result" | the benchmark loses | ST 292 |
| Owner's rule as stated, six lines (1m to 1d), long and short | same day | Stage 1 | 4,774 to 24,813 | every row negative in each period; best short, 15m line −0.129 (−0.118 / −0.171 / −0.119) | gross +0.037% | "Negative in every period for every line" | ST 301 to 306, 27 |
| Owner's rule, longs carried (15m line, exit on a close) | carried (0.7 sessions) | Stage 1 | 4,578 | −0.154 / −0.112 / −0.243; five years −0.162 | gross +0.106 | negative in all three | ST 317 |
| Owner's rule, longs carried (1d line, touch) | carried (10.1 sessions) | Stage 1 | 971 | +0.502 (t 2.2) / −0.267 / −0.424; five years +0.169 | gross +0.514 | positive only in TUNE | ST 320 |
| Only eligible structure: long, daily line, alert, carried | carried | Stage 1 selection | not given | +0.469 (t 1.5) / +0.016 / −0.098 | not given | with the depository charge −1.865 (t −8.9) | ST 337 |
| Stage 2 supertrend_vol, short, 60m, vol_a, no gate | same day | Stage 2 | 1,243 | −0.054 / −0.072 / −0.054; five years −0.057 | gross +0.109 | negative in every period | ST 453 |
| Stage 2 supertrend_adx, short, 30m, adx_a | same day | Stage 2 | 404 | +0.035 / −0.167 / +0.043; five years −0.000; exit on a close +0.020 (t 0.3) | not given | negative in CHOOSE | ST 455 to 456 |
| `carry60_adx` (top of 14,976 structures, long, 60m) | carried (2.7 sessions) | Stage 2 | 280 | +0.297 (t 1.1) / +0.299 (t 0.8) / +0.208 (t 0.5); five years +0.279 (t 1.44) | 13 of 15 eligible beat the benchmark | with the depository charge −1.636 (t −7.35); "There is no stock candidate" | ST 481 to 485, 474 |
| Breakout: buy the 55-session high, sell the 20-session low | delivery, 36 and 35 sessions | Alpha search 12 Nov, 2011 to 2021 then 2021 to 2026 | 539, 227 | +1.35, +0.69 | excess same stock −1.18 (t −2.2), −0.74 (t −1.2); basket −0.50 (t −1.2), −0.35 (t −0.7) | earns less than holding the same stock | AS 121 |
| Breakout, 20 and 10 sessions | delivery, 18 and 17 | same | 1,145, 532 | +0.69, +0.05 | same stock −0.31 (t −1.0), −0.36 (t −1.1); basket −0.14 (t −0.7), −0.14 (t −0.6) | negative excess | AS 122 |
| Long while 50-day average above 200-day | delivery, 201 and 186 | same | 161, 78 | +15.92, +8.10 | same stock −2.17 (t −0.8), −4.41 (t −1.6); basket +2.27 (t 0.6), +0.79 (t 0.4) | negative against the same stock | AS 123 |
| Momentum rotation, 12 months less 1, top 5 | delivery, 212 and 203 | same | 64, 30 | +25.57, +9.39 | same stock "not a fair test"; basket +9.45 (t 0.9), −0.90 (t −0.2) | "Against the basket it shows nothing" | AS 124 |
| New 252-session high on high volume | delivery, 44 and 37 | same | 234, 109 | +2.58, +0.61 | same stock −0.82 (t −1.2), −1.20 (t −1.3); basket −0.07 (t −0.1), −0.50 (t −0.6) | negative excess | AS 125 |
| High-volume up day, held 40 sessions | delivery, 30 and 24 | same | 166, 59 | +1.84, +0.65 | same stock −0.26 (t −0.3), −0.25 (t −0.2); basket +0.67 (t 0.9), +0.56 (t 0.6) | no excess that can be told from zero | AS 126 |
| Owner's Supertrend (14, 1.7), daily with weekly | delivery, 18 and 16 | same | 720, 288 | +0.87, +0.28 | same stock −0.16 (t −0.5), −0.16 (t −0.4); basket +0.13 (t 0.6), −0.01 (t −0.0) | "no more than holding" | AS 127 |
| RSI(2) under 10 in an uptrend | delivery, 4 and 4 | same | 1,364, 649 | +0.04, −0.13 | same stock +0.11 (t 0.9), +0.03 (t 0.2); basket +0.06 (t 0.8), +0.05 (t 0.6) | "does not cover the charges" | AS 128 |
| 10-session low in an uptrend | delivery, 6 and 7 | same | 1,371, 655 | +0.06, −0.08 | same stock −0.06 (t −0.3), −0.04 (t −0.2); basket −0.02 (t −0.2), +0.03 (t 0.3) | "earn nothing extra" | AS 129 |
| Last week's three losers, held a week | delivery, 6 and 6 | same | 1,389, 637 | +0.15, −0.02 | same stock +0.11 (t 0.7), +0.10 (t 0.5); basket +0.09 (t 0.9), +0.06 (t 0.5) | "earn nothing extra" | AS 130 |
| Mirror of the breakout: buy its exit, sell its next entry | delivery, 54 and 61 | same | 530, 221 | +3.75, +2.72 | same stock +0.31 (t 0.5), +0.47 (t 0.7); basket +0.16 (t 0.3), −0.09 (t −0.2) | excess not significant | AS 134 |
| Pullback of 6% in an uptrend (no filter) | delivery, 10 and 12 | same | 805, 265 | +0.54, +0.29 | same stock +0.15 (t 0.6), +0.07 (t 0.2); basket +0.33 (t 2.0), −0.10 (t −0.4) | shallower than 8%; same stock not significant | AS 132 |
| Pullback of 10% in an uptrend (no filter) | delivery, 13 and 14 | same | 228, 59 | +1.17, +1.14 | same stock +0.57 (t 0.9), +0.85 (t 1.2); basket +0.93 (t 2.4), +0.72 (t 1.4) | fewer trades; not the rule chosen | AS 133 |
| **EXCEPTION, going into the paper test:** pullback of 8% above the 200-session average, NIFTY above its 200-session average, 15% stop | delivery, 11 to 12 sessions | Alpha search 12 Nov, 2011 to 2021 then 2021 to 2026 | 329, 112; 69.3% and 75.0% winners | +1.19 (t 3.2), +1.36 (t 2.9); sub-periods TUNE +1.22% (98), CHOOSE +2.07% (6), TEST +2.53% (8) | same stock +0.75 (t 1.9), +1.15 (t 2.4); worst −21.8%, −16.3%; years below zero 3 of 11, 0 of 6 | "Is that proven alpha? No." AS 21: "The paper test must confirm it". At one share the depository charge turns it to −1.02% and −0.45% | AS 165 to 166, 170 to 175, 233 |
| Same rule on 43 other stocks (B and C together) | delivery | AS 5.3 | 1,424; 68.7% winners | +0.78 (t 2.7) | same stock +0.50 (t 1.7) | partly holds: "2015 to 2018 is weak on every list" | AS 206 |

## What the tests have in common

- No setting of any strategy passed all three periods, on either basket, after 6,225 settings and
  70,619 five-year backtests (CR 135).
- Of the 5,752 settings re-read on 11 Nov, none is positive after costs in all three periods (ST
  181).
- Of the 14,976 stage-2 index structures, 2,511 passed TUNE and CHOOSE and 10% of those were
  positive in TEST, against 36% of the 9,956 that did not pass (ST 540).
- Overnight the five indices made +17.7 (t = 4.1) a year and inside the session −9.1 (t = −1.7);
  "the common reason behind every same-day result" (AS 58, 74).
- A stock round trip costs about 0.15 % to 0.166 %; an option buyer pays about 1.2% to 1.6% of the
  premium (CR 130, ST 470, AS 106).
- Real premiums could price only part of the trades (58% of 2,266 in RO 63; 41% for `carry30`), and
  the unpriced ones were the modelled winners for `carry30`.
