# Delivery setups on a wide stock universe (US-093, 2026-11-24)

## The answer in five lines

Nothing passes: 59 cells examined, 25 discovered, 2 confirmed, 0 passed (luck: 0.03 to 0.65).
P1 (pullback) on 161 untouched stocks: +0.81% a trade (t 4.2) at Rs 10,000 after all costs, +0.55
over a same-stock hold (t 2.8), above all 200 random-date runs; but CHOOSE +0.10% (t 0.2): WATCH.
On the 13 stocks that left the NIFTY 50, P1 makes +0.41% (t 1.0), no better than random dates.
New highs, gap-ups and the strongest 10% are survivorship (below random dates, negative in TEST);
the RSI(2) dip does not cover the 0.50% round trip.

## The universe and its survivorship caveat

- **Built.** The alpha study's 61 (18 desk, 30 large, 13 that left the NIFTY 50) plus 163 large
  and mid-size liquid names written in `scripts/swing_universe.py` before any bar was fetched.
  Dhan's instrument master (one download through the app's adapter, `_download_scrip_master`)
  gave the security ids; the desk 18 match `config/universe.yaml`. Renamed: LTIM is LTM,
  ICICIPRULI is ICICILIFE. Not in the master: PEL (merged), GUJGASLTD. Adani Green excluded.
- **222 stocks**: 18 desk, 43 alpha, 161 new. Daily bars from 2010-01-04 for 170 of them, later
  listings from 2011 to 2023 (each needs bars before 2023-11-08; all have).
- **New names**: HDFCLIFE SBILIFE JIOFIN BANKBARODA CANBK UNIONBANK INDIANB BANKINDIA FEDERALBNK
  IDFCFIRSTB AUBANK BANDHANBNK YESBANK RBLBANK CHOLAFIN BAJAJHLDNG MUTHOOTFIN MANAPPURAM
  LICHSGFIN M&MFIN SBICARD HDFCAMC ABCAPITAL PFC RECLTD ICICIGI ICICIPRULI MFSL LTF IIFL ANGELONE
  BSE MCX CDSL IRFC LICI POONAWALLA TVSMOTOR ASHOKLEY BALKRISIND BHARATFORG ESCORTS EXIDEIND
  MOTHERSON MRF APOLLOTYRE TIINDIA BOSCHLTD UNOMINDA SONACOMS ABB HAL BDL MAZDOCK COCHINSHIP
  CGPOWER CUMMINSIND POLYCAB KEI SUZLON THERMAX GMRAIRPORT IRB NBCC NCC RVNL CONCOR INDIGO
  HINDPETRO IOC PETRONET IGL MGL OIL NHPC SJVN TORNTPOWER JSWENERGY ADANIPOWER NLCINDIA CESC
  ADANIENSOL NATIONALUM NMDC HINDZINC JSL APLAPOLLO HINDCOPPER SHREECEM DALBHARAT RAMCOCEM
  JKCEMENT PIDILITIND BERGEPAINT KANSAINER ASTRAL SUPREMEIND DABUR MARICO GODREJCP COLPAL UNITDSPR
  UBL VBL JUBLFOOD PAGEIND BATAINDIA VOLTAS HAVELLS CROMPTON WHIRLPOOL BLUESTARCO DIXON TRENT DMART
  INDHOTEL IRCTC ETERNAL KALYANKJIL NAUKRI LTIM MPHASIS COFORGE PERSISTENT LTTS TATAELXSI OFSS
  KPITTECH TATACOMM IDEA INDUSTOWER SUNTV ZEEL TORNTPHARM ZYDUSLIFE ALKEM LUPIN AUROPHARMA BIOCON
  GLENMARK IPCALAB LAURUSLABS ABBOTINDIA FORTIS MAXHEALTH SYNGENE GRANULES GODREJPROP OBEROIRLTY
  PRESTIGE PHOENIXLTD LODHA SRF PIIND UPL DEEPAKNTR AARTIIND TATACHEM NAVINFLUOR CHAMBLFERT
  COROMANDEL.
- **Data.** 223 Dhan requests (1 master, 222 daily), none refused, one a second, cached in
  `data/research/swing_2026-11-24/daily.sqlite`. Bars after 2026-11-07 are not used. NIFTY: the
  store's daily closes (from 2017-01-02) and, before that, the alpha study's cached Yahoo closes.
- **Adjustments.** Dhan's daily bars are split, bonus and demerger adjusted: on the 61 stocks the
  Dhan to Yahoo close ratio is constant except at demergers Yahoo leaves unadjusted (Tata Motors,
  Vedanta, Siemens) and rights issues of 5% to 11% Dhan leaves unadjusted (Bharti, Tata Steel,
  Grasim, Tata Power). Five one-day moves over 35% that never reverse are corporate actions Dhan
  missed (Adani Enterprises 2015, Jindal Stainless 2015, Tata Chemicals 2020, two before the first
  entry): earlier prices are rescaled. Other such moves are real (2017 bank recap, 2020, Yes Bank).
- **Survivorship.** The list is today's survivors: companies delisted or merged away (PEL, for
  one) are in no group, and the 13 leavers still trade. Three counters, each reported: excess over
  the same stock's average hold of the same length; 200 runs of random entry dates in the same
  stock and calendar year with the same holding days; the 13 leavers apart.

## What was fixed in advance

The story's setups, unchanged: P1 the product's pullback rule (`onelot.delivery.rule.simulate`,
depth 8%, own exits), P1b at 10%; M1 new 252-session high with volume and NIFTY filter, exits
(a) SMA50 or 60 sessions, (b) the desk's ratchet (`onelot.manage.ratchet`, 2 x ATR(14), 3 R,
extension on); M2 gap up on volume, (a) 20 or (b) 40 sessions, both ending on a close below the gap
day's low; M3 the top 10% by return from 252 to 21 sessions back, rebalanced on the close of each
month's first session while NIFTY is above its SMA200 (a stock still in the top 10% is kept);
R1 RSI(2) under 10 above SMA200, out above SMA5 or after 7 sessions. Entry at the next open.
Quantity: whole shares at or just under Rs 10,000 from the signal close, at least one (the product
rounds P1 to the nearest share; the story's rule is used for all). Costs: delivery charges both
legs, 0.03% slippage a side, Rs 14.75 depository charge a sale: 0.50% a round trip at Rs 10,000.
Median trade Rs 9,872; 10% of trades under Rs 9,000; 12 stocks cost more than Rs 10,000 a share
(MRF, BOSCHLTD, PAGEIND, ABBOTINDIA, SHREECEM, MARUTI, DIXON, ULTRACEMCO, BAJAJHLDNG, BAJAJ-AUTO,
DIVISLAB, OFSS) and trade one share. Cells: each setup on the pool, and by group, by size bucket
(tertiles of median rupee turnover in TUNE) and by NIFTY above or below SMA200 (only where the
setup does not already require it). t by entry week. Selection: RESEARCH-RULES section 5.

No look-ahead: for every setup 60 random signals and 20 other bars read the same on bars cut after
the signal day (stock and NIFTY); for M3 the top 10% of 30 of 30 random rebalance days.

## P1 on the new names (the headline)

| Stocks | Trades | Entry days | % a trade (t) | Win | Payoff | TUNE / CHOOSE / TEST (n) | Excess (t) | Random dates [5% to 95%], runs at or above |
|---|---|---|---|---|---|---|---|---|
| 161 new | 6,102 | 1,883 | +0.81 (4.2) | 68% | 0.67 | +0.83 (5,813) / +0.10 (209) / +1.19 (80) | +0.55 (2.8) | +0.60 [+0.44, +0.76], 0 of 200 |
| 18 desk | 520 | | +0.85 (3.1) | 66% | 0.78 | +0.82 (507) / +1.57 (5) / +2.43 (8) | +0.70 (2.4) | |
| 43 alpha | 1,463 | | +0.58 (2.1) | 67% | 0.62 | +0.57 (1,409) / +0.57 (36) / +0.97 (18) | +0.44 (1.5) | |
| 13 leavers | 599 | 410 | +0.41 (1.0) | 66% | 0.61 | +0.39 (573) / +0.32 (19) / +2.13 (7) | +0.54 (1.3) | +0.49 [−0.02, +1.04], 116 of 200 |
| All 222 | 8,085 | 2,049 | +0.77 (4.0) | 68% | 0.67 | +0.79 (7,729) / +0.20 (250) / +1.25 (106) | +0.54 (2.7) | +0.58 [+0.44, +0.72], 1 of 200 |

- The rule holds out of sample in size: the new names give what the desk 18 gave, and the result
  beats both the same-stock hold and random dates in the same stock and year.
- It fails the confirm step: CHOOSE (Nov 2024 to Nov 2025) is +0.10% (t 0.2) on 209 trades. The
  NIFTY filter kept it out most of that year; 96% of all trades fall in TUNE.
- By entry year, new names: 2010 −1.81, 2011 −7.69, 2012 +0.70, 2013 −1.07, 2014 +1.95, 2015 +0.63,
  2016 +0.18, 2017 +1.90, 2018 −1.65, 2019 −0.39, 2020 +0.63, 2021 +1.51, 2022 +0.28, 2023 +2.15,
  2024 +1.06, 2025 +0.34, 2026 +1.04. Six of 17 years below zero on the pool.
- By size (pool): small +1.01 (t 5.4), mid +0.61, large +0.62. Strongest stocks: KALYANKJIL, VBL,
  IRCTC, PETRONET, CDSL; weakest: BAJAJ-AUTO, WHIRLPOOL, MARUTI, AUBANK. 171 of 214 above zero.
- P1b (10%) is larger in TUNE and TEST (+1.31 / −0.26 / +2.57 on the pool; new names +1.36, t 4.8,
  excess +0.98, t 3.4, 0 of 200 random runs at or above) but negative in CHOOSE: REJECTED by
  the rule. Deeper is better again, as in the alpha study.

## The other setups

| Setup | Trades | % a trade (t) | Win | Payoff | Held | TUNE / CHOOSE / TEST | Excess (t) | Random dates (runs at or above) | Leavers |
|---|---|---|---|---|---|---|---|---|---|
| M1a high, SMA50 or 60 | 2,360 | +4.12 (7.3) | 42% | 2.72 | 35 | +4.41 / +0.47 / −2.07 | +1.49 (3.0) | +6.33 (200 of 200) | +3.77 (118) |
| M1b high, ratchet | 2,480 | +0.13 (0.7) | 58% | 0.76 | 8 | +0.16 / +0.21 / −1.64 | −0.06 (−0.4) | +0.88 (200 of 200) | +2.42 (129) |
| M2a gap, 20 sessions | 431 | +1.04 (1.3) | 44% | 1.58 | 14 | +1.05 / +4.07 / −0.86 | +0.32 (0.4) | +2.12 (191 of 200) | +3.28 (33) |
| M2b gap, 40 sessions | 424 | +3.25 (3.0) | 42% | 2.30 | 24 | +3.56 / +6.19 / −1.53 | +1.56 (1.5) | +4.26 (182 of 200) | +3.18 (33) |
| M3 strongest 10% | 910 | +11.62 (4.4) | 57% | 3.01 | 64 | +12.81 / +0.65 / −4.77 | +3.32 (1.8) | +18.85 (200 of 200) | +4.91 (50) |
| R1 RSI(2) dip | 21,744 | +0.06 (0.7) | 59% | 0.72 | 4 | +0.10 / −0.34 / −0.15 | +0.27 (3.1) | −0.07 (0 of 200) | +0.14 (1,224) |

- **M1a and M3** look strong in TUNE because the universe rose: a random entry in the same stock
  and year, held as long, makes more (+6.33 against +4.12; +18.85 against +11.62). Both are
  negative in TEST. M3 is +2.40% a trade on the desk 18 and −1.96 under a same-stock hold there.
  Doubt: a year-matched placebo is harsh on momentum, because a stock that makes a new high is
  in a rising year partly after the signal; the excess column is the fairer test, and it fails
  in TEST all the same. REJECTED.
- **M1b**: the ratchet cuts the new-high trade to 8 sessions and nothing is left (+0.13%). The
  ratchet study found the same on the pullback rule. REJECTED.
- **M2** is rare (26 trades a year on 222 stocks). M2b is confirmed (CHOOSE +6.19, t 2.0, 21
  trades) and then −1.53% in TEST on 36, below its random dates. REJECTED. Exploratory: when NIFTY
  is below its SMA200, M2b makes +7.68 / +8.10 / +0.76 but on 8 and 18 trades in CHOOSE and TEST.
- **R1** beats its random dates by 0.13 points but stays near zero after 0.50% of costs, and is
  negative in CHOOSE and TEST. REJECTED (the alpha study said the same on 18 stocks).
- Losing calendar years: P1 6 of 17, P1b 5, M1a 5 of 16, M1b 9, M2a 9, M2b 7, M3 3 of 16, R1 9.

## What the owner would live with (P1, the WATCH setup; no setup passed)

A book that takes every P1 signal on the 222 stocks at Rs 10,000 each, 2010 to 2026:

| | P1 (8%) | P1b (10%) |
|---|---|---|
| Signals a month | 42 | 25 |
| Trades open at once, median and worst | 12 and 134 | 7 and 96 |
| Longest run of losing trades | 30 | 16 |
| Worst month (by exit) | −Rs 63,445 (March 2020) | −Rs 41,969 (March 2020) |
| Worst calendar year | −Rs 89,075 (2018) | −Rs 34,415 (2018) |
| Total over 16 years | +Rs 6.33 lakh | +Rs 6.14 lakh |

Dips come together: in March 2020 the book held 134 positions (Rs 13.4 lakh); a cap was not tested.

## Counts against luck

| | Examined | Discovered | Confirmed | Passed |
|---|---|---|---|---|
| All cells (8 pool cells and 51 condition cells) | 59 | 25 | 2 | 0 |
| Condition cells only | 51 | 20 | 1 | 0 |
| Label placebo, condition cells, mean of 20 shuffles | 49.6 | 23.4 | 1.8 | 0.65 |
| Arithmetic (59 x 0.6% x 16% x 50%) | | | | 0.03 |

The two confirmed cells are M2b on the pool and M1a on mid-size stocks; both lose in TEST. The real
counts are no higher than luck's. The NIFTY label is shuffled over the days inside each stock and
year; group and size are labels of a stock, so they are shuffled over the stocks. No AVOID cell:
no TUNE t reaches −2.5.

## What could not be done

- Stocks delisted or merged away before today are missing; the leavers group is the only check,
  and its 13 stocks still trade. Rights issues of 5% to 11% are not adjusted in Dhan's bars.
- Sectors are not in the instrument master (stocks are named instead); earnings dates are not
  stored, so M2 is a stand-in; a cap on open trades was not part of the story.
- TEST was seen before for P1 on the desk 18 and the 43 (alpha and ratchet studies); for the
  161 new names every period is clean.

## What the desk would need to trade P1 on this universe

If P1 is ever confirmed (a paper test on the desk 18 is the present step): daily bars for 204
stocks the desk does not watch (one Dhan daily request each a session, about two minutes at
two a second), and about 10 alerts a week at 8% depth (6 at 10%), up to 94 (75) in a week of dips.

## Rows for the setup book

| Instrument | Kind | Setup and preset | Timeframe | Condition | Exit | Trades a year | % a trade TUNE / CHOOSE / TEST (t whole span) | Real-premium or cost note | Status | Remark |
|---|---|---|---|---|---|---|---|---|---|---|
| 222 stocks | stock delivery | P1 pullback 8% | 1d | NIFTY above SMA200 | own (SMA20, 30 sessions, 15% stop) | 504 | +0.79 / +0.20 / +1.25 (4.0) | Rs 10,000, 0.50% round trip, DP included | WATCH | Positive in all periods, beats random dates; CHOOSE t 0.2 misses the confirm step |
| 161 new stocks | stock delivery | P1 pullback 8% | 1d | NIFTY above SMA200 | own | 380 | +0.83 / +0.10 / +1.19 (4.2) | as above | WATCH | Clean out-of-sample test; works in rising years, loses in 2011, 2013, 2018, 2019; flat in CHOOSE |
| 13 NIFTY leavers | stock delivery | P1 pullback 8% | 1d | NIFTY above SMA200 | own | 37 | +0.39 / +0.32 / +2.13 (1.0) | as above | REJECTED | No better than random dates (116 of 200 runs at or above) |
| 222 stocks | stock delivery | P1b pullback 10% | 1d | NIFTY above SMA200 | own | 302 | +1.31 / −0.26 / +2.57 (4.7) | as above | REJECTED | Larger than 8% but negative in CHOOSE (127 trades) |
| 222 stocks | stock delivery | M1a new 52-week high | 1d | NIFTY above SMA200 | SMA50 or 60 sessions | 149 | +4.41 / +0.47 / −2.07 (7.3) | as above | REJECTED | Below random dates (+6.33); the rise of survivors, not skill |
| 222 stocks | stock delivery | M1b new 52-week high | 1d | NIFTY above SMA200 | ratchet 2 ATR, 3 R, extend | 157 | +0.16 / +0.21 / −1.64 (0.7) | as above | REJECTED | The ratchet removes the trend gain |
| 222 stocks | stock delivery | M2a gap up on volume | 1d | all | 20 sessions or gap-day low | 26 | +1.05 / +4.07 / −0.86 (1.3) | as above | REJECTED | Not discovered |
| 222 stocks | stock delivery | M2b gap up on volume | 1d | all | 40 sessions or gap-day low | 26 | +3.56 / +6.19 / −1.53 (3.0) | as above | REJECTED | Confirmed, then negative in TEST and below random dates |
| 222 stocks | stock delivery | M3 strongest 10% | 1d, monthly | NIFTY above SMA200 | next month's first session | 57 | +12.81 / +0.65 / −4.77 (4.4) | as above | REJECTED | Survivorship: below random dates (+18.85), negative in TEST |
| 222 stocks | stock delivery | R1 RSI(2) under 10 | 1d | close above SMA200 | close above SMA5 or 7 sessions | 1,355 | +0.10 / −0.34 / −0.15 (0.7) | as above | REJECTED | The dip edge (+0.13 over random) is smaller than the costs |

Reproduce: `scripts/swing_universe.py <stage>` run as RESEARCH-RULES section 1 says (stages:
universe fetch check p1 m1 m2 m3 r1 select placebo lookahead report; outputs git-ignored).
