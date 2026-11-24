# Volume Watch study, 24 November 2026 (US-089)

## The answer in five lines
1. Heavy market volume tells SIZE, not direction: after a market spike the index's next 5 to 60
   minutes were about 1.5 times as large as usual (median 1.2 to 1.6), in all three periods.
2. Direction after a spike is a coin flip: the flow's side was right 41% to 53% of the time,
   within ±3 basis points (bp) of nothing; an option buyer needs 5 to 10 bp.
3. Pressure on an index (the basket's net flow weighted by influence) reverses a little in the
   next 15 minutes (−1 to −2 bp between the top and bottom fifth): real, but far below costs.
4. The product's settings: the `bvc` up-share rule, market alerts from a spike score of 2.5
   (about 1.3 a day), single-stock alerts from 4.0 (about 2.6 a day), 15-minute cooldown.
5. Nothing here is tradable for an option buyer. The watch is a size warning, never a signal.

## What was fixed in advance
- Data: one-minute bars of the 18 desk stocks (volume since November 2021), of NIFTY,
  BANKNIFTY, FINNIFTY, SENSEX and India VIX from `data/onelot.db`, read only, up to 2026-11-07.
- Periods (RESEARCH-RULES 2): TUNE to 2024-11-07, CHOOSE to 2025-11-07, TEST to 2026-11-07.
  Three numbers in a row are always TUNE / CHOOSE / TEST. Sessions used: 722 / 246 / 247 (the
  basket full, 15 of 18 stocks with 300 bars or more, and a spike score defined).
- Formulas: `.agile/stories/VOL-API.md`, computed by `src/onelot/volume/indicator.py`, loaded by
  file path; the product runs the same functions.
- V2: the rule with the highest TUNE rank correlation of the basket's 5-minute flow share with
  NIFTY's return over the same 5 minutes; within 0.01, the better next-5-minute correlation.
- V3: of 2.5, 3.0, 3.5 the market level with 1 to 3 TUNE alerts a day, closest to 2; the
  lowest stock level of 3.5, 4.0, 4.5 with at most 6 a day; a 15-minute cooldown.
- t statistics treat one session as one draw. Costs: an option buyer needs 5 to 10 bp of index
  move in the trade's favour (ALPHA_SEARCH_2026-11-12).
- Set after seeing V1 (labelled so): the words quiet and busy at the TUNE 15th and 85th
  percentiles of rvol_5m (0.95 and 2.06, set as 1.0 and 2.1), because the median-based
  expected turnover runs low.

## V1. The day's shape
Share of a normal day's basket turnover by half hour (09:15 first):

| Period | 09:15 | 09:45 | 10:15 | 11:45 | 12:15 | 12:45 | 14:15 | 14:45 | 15:15 |
|---|---|---|---|---|---|---|---|---|---|
| TUNE | 15.7% | 8.1% | 6.7% | 5.5% | 5.6% | 5.6% | 7.3% | 13.0% | 8.7% |
| CHOOSE | 14.1% | 7.1% | 6.3% | 5.4% | 5.5% | 5.5% | 7.5% | 15.0% | 10.2% |
| TEST | 13.7% | 7.3% | 6.2% | 5.9% | 5.9% | 5.9% | 7.9% | 15.4% | 7.1% |

- Busiest: the first half hour and 14:45 to 15:14; since 2025 the last half hour leads.
- Quietest whole hour: 11:45 to 12:44 (TUNE, CHOOSE), 12:15 to 13:14 (TEST).
- 15:15 to 15:29 is only 15 minutes, and empty since the closing auction (2026-08-03).
- Weekdays: Thursday and Friday a little busier (day rvol 1.59 and 1.56 in TUNE against 1.33 on
  Monday); no weekday is quiet in all three periods.
- NIFTY expiry days (Thursday, Tuesday from September 2025): day rvol 1.59 / 1.58 / 1.69
  against 1.46 / 1.46 / 1.53 on other days, about 9% busier.
- The scale: a day's rvol averages about 1.45, not 1, because the expected turnover is a sum of
  per-minute medians and minute turnover is skewed. The contract keeps the median; the state
  words are set to this scale.

## V2. The up-share rule
Rank correlation of the basket's 5-minute flow share with NIFTY's return, same 5 minutes and
next 5 minutes (BANKNIFTY similar):

| Rule | Same 5 minutes | Next 5 minutes |
|---|---|---|
| clv | 0.680 / 0.658 / 0.659 | −0.010 / −0.019 / −0.003 |
| tick | 0.762 / 0.747 / 0.743 | −0.014 / −0.023 / −0.004 |
| bvc | 0.808 / 0.793 / 0.790 | −0.015 / −0.027 / −0.003 |

`bvc` reads the market's own move best and is chosen. No rule keeps information about the next
5 minutes (all within 0.03 of zero): the flow describes the minute, it does not foretell it.

## V3. Alert levels
The market spike score has a median of −0.1, a 99th percentile near 3.0 and a 99.9th near 5.
Alerts a day with a 15-minute cooldown:

| Market z | a day | | Stock z (18 stocks) | a day |
|---|---|---|---|---|
| 2.5 | 1.23 / 1.42 / 1.50 | | 3.5 | 6.02 / 5.61 / 5.13 |
| 3.0 | 0.60 / 0.76 / 0.66 | | 4.0 | 3.01 / 2.85 / 2.49 |
| 3.5 | 0.34 / 0.41 / 0.30 | | 4.5 | 1.48 / 1.41 / 1.24 |

Chosen on TUNE: market 2.5, stock 4.0. With the cap of 12 a day the product raises about 1.1 /
1.3 / 1.4 market and 2.7 / 2.7 / 2.3 stock alerts a day. Truncation check: 50 random alerts
recomputed with every later bar cut away, 0 changed (expected turnover and z alike).

## V4. Size after a market spike (1,455 alerts)
The index's absolute move over the next minutes over the usual move at that minute on days
without an alert near it (mean over mean, 95% interval by session; the median ratio after):

| Index | 5 min | 15 min | 30 min | 60 min |
|---|---|---|---|---|
| NIFTY | 1.48 / 1.59 / 1.62 | 1.48 / 1.52 / 1.45 | 1.45 / 1.45 / 1.38 | 1.52 / 1.50 / 1.36 |
| BANKNIFTY | 1.50 / 1.45 / 1.52 | 1.55 / 1.36 / 1.38 | 1.54 / 1.29 / 1.31 | 1.60 / 1.34 / 1.35 |
| FINNIFTY | 1.53 / 1.50 / 1.53 | 1.53 / 1.42 / 1.37 | 1.56 / 1.38 / 1.31 | 1.61 / 1.47 / 1.29 |
| SENSEX | 1.49 / 1.61 / 1.62 | 1.50 / 1.55 / 1.45 | 1.45 / 1.48 / 1.38 | 1.52 / 1.53 / 1.37 |

Every interval lies above 1 (NIFTY 15 minutes: TEST 1.16 to 1.75). Median ratios 1.15 to 1.57.
A SIZE effect, in all periods and indices. Whether it pays an option buyer depends on whether
the premium already prices it (implied volatility rises with volume); not tested here.

## V5. Direction after a market spike
885 of 1,455 alerts had a side (BUYING 441, SELLING 444). The index's next move in the flow's
direction (hit rate, mean bp, t by session):

| Index | 5 min | 15 min | 30 min |
|---|---|---|---|
| NIFTY | 47% / 46% / 45%, −0.4 bp (t −1.6) | 47% / 50% / 46%, +0.2 bp (t −1.2) | 51% / 47% / 41%, +0.8 bp (t −0.6) |
| BANKNIFTY | 53% / 47% / 45%, +0.3 bp (t 0.1) | 47% / 51% / 47%, +0.6 bp (t −0.1) | 52% / 49% / 48%, +1.5 bp (t 0.6) |

(bp and t over the whole span.) FINNIFTY and SENSEX alike. NO direction effect; if anything the
TEST year leans slightly against the flow. After the 5 minutes before the spike: after a fall
NIFTY's next 15 minutes were −0.6 / +0.3 / +2.6 bp, after a rise −0.3 / −1.0 / −0.3 bp: heavy
volume after a fall does not mark its end in a way that survives three periods.
Known before (ALPHA_SEARCH_2026-11-12, section on the momentum signal): heavyweight relative
volume of 1.2 or more lifted a same-day momentum signal to +6 to +8 bp, breadth and signed
volume sorted nothing, and no cut survived a second sample. That cut was not repeated here.

## V6. Pressure and the next minutes
Every 5 minutes, the index's `pressure_5m` (influence from the previous 60 sessions) against its
next 5 and 15 minutes. Rank correlation inside each session (mean, t over sessions) and the top
fifth minus the bottom fifth, by session:

| Index | 5 min: rho; top minus bottom | 15 min: rho; top minus bottom |
|---|---|---|
| NIFTY | −0.040 / −0.042 / −0.023; −0.4 / −0.7 / −0.3 bp | −0.058 / −0.063 / −0.040; −1.2 / −1.4 / −0.9 bp |
| BANKNIFTY | −0.055 / −0.040 / −0.031; −0.8 / −0.9 / −0.2 bp | −0.069 / −0.052 / −0.042; −1.9 / −1.4 / −1.0 bp |
| FINNIFTY | −0.046 / −0.038 / −0.026; −0.6 / −0.8 / −0.4 bp | −0.063 / −0.049 / −0.036; −1.7 / −1.2 / −1.2 bp |
| SENSEX | −0.023 / −0.021 / −0.007; −0.2 / −0.4 / 0.0 bp | −0.049 / −0.053 / −0.032; −1.0 / −1.2 / −0.8 bp |

The 15-minute correlations have t of −3 to −15 in every period: buying pressure is followed by a
small give-back. With the `bvc` rule pressure is close to the last 5 minutes' move, so this is
the known short reversal of index minutes. At 1 to 2 bp it is a fifth of an option buyer's cost:
a DIRECTION effect too small to trade. Pooled over sessions the spread is within ±0.5 bp.

## V7. Where it shows
Influence (non-negative least squares on one-minute returns, previous 60 sessions):

| Index | r2 | Largest weights in TEST |
|---|---|---|
| NIFTY | 0.94 / 0.95 / 0.91 | HDFC Bank 0.13, Reliance 0.11, ICICI Bank 0.10, L&T 0.07 |
| BANKNIFTY | 0.93 / 0.92 / 0.83 | HDFC Bank 0.24, ICICI Bank 0.19, SBI 0.19, Axis 0.13 |
| FINNIFTY | 0.92 / 0.94 / 0.89 | HDFC Bank 0.23, ICICI Bank 0.17, Bajaj Finance 0.13 |
| SENSEX | 0.81 / 0.79 / 0.75 | HDFC Bank 0.14, Reliance 0.12, ICICI Bank 0.10 |

The basket explains about 90% of NIFTY's minute moves. Spikes led by two financial stocks (521)
against spikes led by none (268), 15-minute size ratio (whole span): NIFTY 1.37 against 1.49,
BANKNIFTY 1.53 against 1.40, FINNIFTY 1.52 against 1.40. Bank-led spikes lean toward the bank
indices, but the intervals overlap: a hint, not a finding. The alert's "likely to move" names
the indices where its leaders weigh most; that is the arithmetic of the weights, not a forecast.

## V8. India VIX
- During a spike (5 minutes): VIX rises after SELLING spikes (+0.10 / +0.07 / +0.09 points, t
  above 3.7) and falls after BUYING spikes (−0.07 / −0.03 / −0.07). It moves with the price.
- In the 30 minutes after: nothing (all t within ±1.5).
- A day's rvol and the same day's VIX change: rho 0.05 / −0.02 / 0.15, no steady link.
- Day rvol by the previous day's VIX (terciles from TUNE, cuts 13.2 and 16.6): 1.53, 1.49,
  1.45 / 1.57, 1.44, 1.38 / 1.51, 1.72, 1.47. High VIX does not mean high basket turnover.

## V9. The day ahead from the first half hour
- Size: the first half hour's rvol against the rest of the day's NIFTY range over its 20-session
  median: rho 0.18 / 0.04 / 0.24 (BANKNIFTY 0.16 / 0.01 / 0.21); top fifth 1.28 / 1.12 / 1.38 of
  the usual range, bottom fifth 0.97 / 1.05 / 1.08. A size effect, weak in CHOOSE.
- Direction: the first half hour's flow share against the rest of the day's return: rho 0.03 /
  −0.03 / 0.02, hit 52% / 52% / 54%, −0.4 / +0.9 / +3.3 bp (t at most 0.9). Nothing.
  ALPHA_SEARCH found the same for the price (first half hour to last half hour: +0.3 bp).

## What could not be done, and doubts
- Single-stock spikes were counted (V3) but not followed: what a stock does after its own spike
  is not studied. The alert says so.
- The order book is recorded from today; it has no history and is not studied.
- Whether the size effect pays an option buyer needs implied volatility around spikes (real
  premiums in `data/expired_options.db`); not done here.
- The basket's expected turnover is a median per minute, so "1.0 times normal" is a quiet day.
  A mean or a trimmed mean would read more naturally; the contract would have to change.
- TEST had been looked at in earlier studies, but not for volume; the volume numbers are new.
- MIDCPNIFTY was left out (a training instrument).

## Rows for the setup book

| Instrument | Kind | Setup and preset | Timeframe | Condition | Exit | Trades a year | % a trade TUNE / CHOOSE / TEST (t whole span) | Real-premium or cost note | Status | Remark |
|---|---|---|---|---|---|---|---|---|---|---|
| NIFTY, BANKNIFTY, FINNIFTY, SENSEX | option same-day | Follow the flow after a market volume spike (z 2.5) | 1m | flow share 0.60 or more / 0.40 or less | 15 minutes | about 180 | +1.1 / +0.5 / −2.1 bp of NIFTY (t −1.2) | needs 5 to 10 bp; not reached | REJECTED | Hit rate 41% to 53%: a coin flip |
| NIFTY, BANKNIFTY, FINNIFTY, SENSEX | option same-day | Trade with pressure_5m (top against bottom fifth) | 5m | pressure_5m top or bottom fifth | 15 minutes | many | −1.2 / −1.4 / −0.9 bp of NIFTY (t below −3) | a fifth of the cost; against the pressure | REJECTED | A small reversal, too small to pay |
| NIFTY, BANKNIFTY | option same-day | First half hour's flow share decides the day | 1d | flow share above or below 0.5 at 09:44 | session end | about 245 | −0.4 / +0.9 / +3.3 bp of NIFTY (t at most 0.9) | not reached | REJECTED | The flow of the open says nothing of the close |
