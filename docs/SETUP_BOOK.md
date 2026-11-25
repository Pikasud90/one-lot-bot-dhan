# The setup book

Owner, 2026-11-24: "maintain proper setup-strategy book, noting the best alphas we see till now."
This is that book: one place that says, for each instrument, which setup was tested at which
timeframe, under which conditions it worked and failed, and what its standing is today. It is a
living document: it is brought up to date after every study and every month of paper trades.
Last brought up to date: 25 November 2026 (first edition, from the five studies of 24 November;
the capped book of US-100 and the market conditions of US-101 added the same day).

Everything here is evidence from backtests and from the desk's paper record. It is not advice
to trade, and a backtest is not a promise.

## 1. How to read it

- Three numbers in a row are the result per trade, after costs, in three periods kept apart on
  purpose: TUNE (to 7 Nov 2024, where a setup is found), CHOOSE (to 7 Nov 2025, where it must
  repeat) and TEST (to 7 Nov 2026, read once at the end).
- An option result is in % of the premium paid, one lot, same day. "Real" means real traded
  premiums from the store; "model" means a pricing model, which has run about 1 point of premium
  a trade better than real prices. A stock result is in % of the price (same day, one share) or
  of about Rs 10,000 (delivery, held for days).
- The owner now allows a different setup per instrument. That multiplies what is tried, so
  every study counts what it tried and compares its hits with what luck alone would give
  (`.agile/stories/RESEARCH-RULES.md`). A setup is found only when TUNE is clearly positive
  (t of 2.5 or more), CHOOSE repeats it, and TEST and real premiums do not contradict it.
- Status words:
  - CANDIDATE: passed all of that, and more setups passed than luck explains. Next step: a
    paper test on the desk, never real money on a backtest alone.
  - WATCH: positive in all three periods but short of a threshold, or no better than luck's
    count. Worth following on paper; not worth an alert.
  - AVOID: clearly and steadily negative. A setup or a condition in which NOT to trade.
  - REJECTED: tested and not good enough; the number that ruled it out is given.

## 2. Where things stand

**No setup has passed yet.** 41 rows are in the register: 0 CANDIDATE,
6 WATCH, 7 AVOID, 28 REJECTED.

**The best so far: the stock pullback rule, as a delivery trade (WATCH).** Buy a large stock
that has fallen 8% from its 20-session high while it and NIFTY are above their 200-session
averages; sell above the 20-session average, after 30 sessions, or at a 15% stop.
- It has now been positive on three sets of stocks: the 18 desk stocks, 43 other large stocks,
  and 161 stocks nobody had looked at (+0.81% a trade after costs, 6,102 trades since 2010,
  68% winners, t 4.2; all 222 together +0.79 / +0.20 / +1.25).
- Honest size of the edge: about +0.6% of that is what ANY entry in those stocks in those
  years earned (they are survivors in rising years). The rule's own timing adds +0.2% to +0.3%.
- It missed CANDIDATE because the middle period had few trades and a small result (t 0.2).
- Living with it: about 10 alerts a week on 222 stocks; taking every signal needed up to
  Rs 13.4 lakh at the worst moment (March 2020), so a cap on open trades is needed.
- The stop, target and trail rule makes it worse; it keeps its own exits (owner, 24 Nov).
- As a BOOK the owner would hold (Rs 1 lakh, at most 10 open, the product's order;
  `docs/research/PULLBACK_BOOK_2026-11-25.md`): Rs 1 lakh became Rs 1.30 lakh in 16 years
  (+1.64% a year, worst fall −39.5%), against Rs 4.15 lakh in NIFTY held and Rs 2.27 lakh in the
  222-stock basket at the same exposure: behind the basket by −3.59% a year, borderline (the
  fixed seed's 95% interval −7.25 to −0.04 crosses zero with most other seeds and longer blocks),
  and the book's costs of Rs 1.23 lakh exceed that gap (the benchmarks carry none). Held trades
  make +0.14% a trade, those refused by the cap +1.09%: on the same evening the cap does not drop
  the better signals; quiet-evening signals (−0.07%) take up the places before a broad dip comes.
  No other order or cap tested does better by the fixed rules.
- By market condition (US-101, `docs/research/PULLBACK_CONDITIONS_2026-11-25.md`): against the
  same 222 stocks held over the same days, the rule earned −0.16% a trade (t −2.3; −0.17 / −0.58 /
  +0.93). Of 13 cells (signals that evening, NIFTY below its 20-session high, India VIX against
  its 60-session median, and all) none is discovered on that excess and none is AVOID: the cells
  most negative in TUNE are positive in TEST. Luck gives the same (0 in the placebo). The money
  of broad dips is the basket's (20 or more signals: +3.40 a trade, the basket +3.13 over the
  same days), and after TUNE there were 2 broad evenings. Books that wait for broad or deep
  evenings, or skip quiet ones, are not ahead of NIFTY or the basket beyond chance and are
  behind US-100's book in TEST, so the wide list stays as it is.

**Index options, bought and sold the same day: nothing earns after costs.** The grid tried
every registered rule at four timeframes with three presets under nine kinds of condition:
3,948 cells with enough trades, 4 found, none confirmed. Four cells are WATCH (section 3).
The desk's own rule (`microtrend`, 3m) is about zero on the model and about −1.3% a trade on
real premiums, and no filter suggested by the live record improves it.

**Stocks, same day, one share: nothing.** The round trip costs 16.6 basis points, more than
any edge found. `mean_reversion` at 5m, the desk's stock slot until 12 November, lost 0.16% a
trade in every period, on every stock, under every condition, and lost the same live.

**Buying both a call and a put (long volatility): it earned on no kind of day** known before
the open. The size of the loss is not proven, because the store holds real put prices for
only 12 days.

**Facts that held everywhere, and how to use them:**
- The SIZE of the next move can be told: an active session tends to follow an active one
  (57% against 50%); a high India VIX means a larger next day; after a market-wide volume spike
  the next 5 to 60 minutes move about 1.5 times as much as usual.
- The DIRECTION cannot be told from any of: market states, India VIX and its changes, the
  first half hour, volume flow, buying or selling pressure, the side of the last alert.
- Costs differ by index: the desk's recorded quotes show a median half spread of about 0.25%
  to 0.3% of the premium on NIFTY and BANKNIFTY options and about 1.5% on FINNIFTY and SENSEX
  (far wider before 10:00). The model assumes 0.6% for all four.
- Being 1 to 5 minutes late on an alert costs nothing that can be measured.
- Slower timeframes (30m, 60m) lose least for trend rules, because they trade less.

## 3. Instrument by instrument

### The four indices (options bought, one lot, same day)

| Index | Timeframe the grid fixed | Best setup on TUNE, and after | WATCH | Live half spread |
|---|---|---|---|---|
| NIFTY | 60m for trend rules, 30m for ema_cross | supertrend_vol 60m high: +2.56 / −2.10 / −0.72 | cpr_breakout 60m low after an active session: +3.75 / +1.20 / +2.00 | 0.25% |
| BANKNIFTY | 60m (cpr, orb, supertrend_mtf), 30m ema_cross, 15m (nr7, supertrend_vol) | nr7_breakout 15m: +2.35 / −3.61 / +0.16 | none | 0.31% |
| FINNIFTY | 60m (cpr, orb), 30m (ema_cross, both supertrends) | cpr_breakout 60m: +2.66 / −1.97 / −3.59 | none (two cells found on TUNE failed after) | 1.51% |
| SENSEX | 60m (cpr, orb), 30m (ema_cross, both supertrends) | ema_cross 30m high: +1.45 / +3.37 / +1.34 | supertrend_mtf 30m (VIX low: +3.26 / +1.05 / +1.13; after a calm session: +2.86 / +2.01 / +3.72); ema_cross 30m high, VIX low: +2.93 / +4.37 / +1.38 | 1.47% |

- When index setups did best: India VIX below its one-year median, and for the trend rules
  the slower timeframes. When they did worst: `intraday_momentum` at any time; the first half
  hour's direction traded into the last half hour (−1.4% a trade); after a VIX jump.
- SENSEX shows the most WATCH cells, but on model premiums; at its real spreads most of that
  result would be paid away. NIFTY and BANKNIFTY are the cheapest to trade.
- MIDCPNIFTY is a training instrument: simulated and studied, never alerted.

### The 18 desk stocks

- Same day, one share: every registered rule is REJECTED or AVOID on every stock. The fixed
  timeframe and best setup of each stock are in `docs/research/SETUP_GRID_2026-11-24.md`.
- Delivery, about Rs 10,000: the pullback rule above (WATCH). On the desk it trades rarely
  with 18 stocks; its last backtest entry before this edition was in January 2026.

## 4. The register

Every setup the five studies named, best standing first. The study notes are in
`docs/research/` (`LIVE_ALERTS`, `OPTION_SETUPS`, `SWING_UNIVERSE`, `VOLUME_STUDY`, `SETUP_GRID`,
each `_2026-11-24.md`); earlier work is listed in `STRATEGIES_TESTED_2026-11-12.md`.

| Instrument | Kind | Setup and preset | Timeframe | Condition | Exit | Trades a year | % a trade TUNE / CHOOSE / TEST (t whole span) | Real-premium or cost note | Status | Remark | Study |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 222 stocks | stock delivery | P1 pullback 8% | 1d | NIFTY above SMA200 | own (SMA20, 30 sessions, 15% stop) | 504 | +0.79 / +0.20 / +1.25 (4.0) | Rs 10,000, 0.50% round trip, DP included | WATCH | Positive in all periods, beats random dates; CHOOSE t 0.2 misses the confirm step. As a book of Rs 1 lakh with 10 places (US-100): +0.12 / +0.23 / +0.71 a trade (t 0.9), +1.64% a year, behind the 222-stock basket at the same exposure by −3.59% a year, borderline (interval −7.25 to −0.04 with the fixed seed, crossing zero with most other seeds; costs Rs 1.23 lakh exceed the gap); held trades +0.14, refused by the cap +1.09, because quiet-evening signals fill the places, not because the cap drops an evening's better signals | delivery, 222 stocks; pullback book |
| 161 new stocks | stock delivery | P1 pullback 8% | 1d | NIFTY above SMA200 | own | 380 | +0.83 / +0.10 / +1.19 (4.2) | as above | WATCH | Clean out-of-sample test; works in rising years, loses in 2011, 2013, 2018, 2019; flat in CHOOSE. The book reading above holds for the whole list, not this group alone | delivery, 222 stocks |
| NIFTY | option same-day | cpr_breakout low | 60m | after an active session | desk rule | 20 | +3.75 / +1.20 / +2.00 (+2.6) | model; less 1 point +2.09 | WATCH | thin: 14 and 17 trades in CHOOSE and TEST | setup grid |
| SENSEX | option same-day | supertrend_mtf default | 30m | VIX below its median | desk rule | 36 | +3.26 / +1.05 / +1.13 (+2.2) | model; live spreads wide | WATCH | spread of 1.5% a side would take it | setup grid |
| SENSEX | option same-day | supertrend_mtf default | 30m | after a calm session | desk rule | 38 | +2.86 / +2.01 / +3.72 (+2.6) | model; live spreads wide | WATCH | the steadiest by year of the four | setup grid |
| SENSEX | option same-day | ema_cross high | 30m | VIX below its median | desk rule | 19 | +2.93 / +4.37 / +1.38 (+2.2) | model; live spreads wide | WATCH | 97 trades in five years | setup grid |
| 4 indices, pool | option same-day | S1 last-half-hour momentum, b | 1m | 14:30 to 14:59 agrees | 15:15 | 500 | −1.04 / −1.86 / −1.87 (−6.1) | real like for like −2.17 | AVOID | loses in every index and variant | option setups |
| 4 indices, pool | option same-day | S1, size filter | 1m | first return above its 60-day median | 15:15 | 470 | −2.08 / −1.95 / −1.34 (−9.0) | real −2.73 | AVOID | larger first moves lose more | option setups |
| 4 indices, pool | option pair same-day | S3 long straddle 09:45 | 1m | any | 12:00 | 590 | real −1.79 / −1.84 / −2.53 (−6.8) | real reading, put by parity | AVOID | morning IV fall costs the most | option setups |
| 4 indices, pool | option pair same-day | S3 long straddle 09:20 | 1m | any | −30% / +60%, 15:20 | 570 | real −1.95 / −2.24 / −2.71 (−5.2) | real reading | AVOID | the stop locks in the IV fall | option setups |
| 4 indices, pool | option pair same-day | S3 long straddle 09:20 | 1m | VIX-UP | 12:00 | 80 | real −3.81 / −2.65 / −4.25 (−4.5) | real reading | AVOID | after a VIX jump IV keeps falling | option setups |
| Four indices | option same-day | intraday_momentum, any | any | all | desk rule | 800 | −1.83 / −1.20 / −1.20 (−9.5) | model | AVOID | loses in every period on every index | setup grid |
| 18 stocks | stock same-day | mean_reversion, any | 5m | all | desk rule | 10,000 | −0.16 / −0.16 / −0.16 (−90) | 16.6 bp round trip | AVOID | no condition helps; the old desk slot | setup grid |
| NIFTY, BANKNIFTY, FINNIFTY, SENSEX | option same-day | microtrend, Supertrend(14, 1.7) x 7 | 3m | entry before 10:15 | 3 of 7 or 15:15 | 146 | −0.20 / +1.92 / −1.86 (−0.1) | real-like N+BN −0.57 | REJECTED | TUNE not above zero; the live first-hour loss is not in the history | live alerts |
| NIFTY, BANKNIFTY, FINNIFTY, SENSEX | option same-day | microtrend | 3m | puts only | 3 of 7 or 15:15 | 253 | +0.26 / +0.05 / −2.05 (−0.5) | real-like −1.72 | REJECTED | puts and calls the same over five years | live alerts |
| NIFTY, BANKNIFTY, FINNIFTY, SENSEX | option same-day | microtrend | 3m | previous session active | 3 of 7 or 15:15 | 291 | −1.08 / +0.41 / −2.72 (−1.9) | real-like −2.22 | REJECTED | worse after active sessions, not better; misses AVOID (t −1.4 in TUNE) | live alerts |
| NIFTY, BANKNIFTY, FINNIFTY, SENSEX | option same-day | microtrend | 3m | India VIX fell the session before | 3 of 7 or 15:15 | 327 | +0.21 / +0.54 / −1.12 (+0.1) | real-like −0.99 | REJECTED | about flat on the model, negative on real premiums | live alerts |
| NIFTY, BANKNIFTY, FINNIFTY, SENSEX | option same-day | microtrend | 3m | entry 3 minutes late | 3 of 7 or 15:15 | 613 | −0.13 / −0.15 / −1.52 (−1.0) | real-like −1.33 | REJECTED | a delay costs 0.04 points a trade: harmless, but the rule has no edge to keep | live alerts |
| 4 indices, pool | option same-day | S2 opening range 30m | 1m | gap over 0.5 sd | ratchet, 15:15 | 300 | +1.50 / −1.97 / −1.57 (+0.5) | real −0.38 | REJECTED | best of 50 cells, TUNE t +1.5 | option setups |
| 4 indices, pool | option pair same-day | S3 long straddle 09:20 | 1m | any day type | 15:20 | 990 | real −0.33 / −2.12 / −1.99 (−2.0) | model +2.43 is a model artefact | REJECTED | two lots; pays on every day type | option setups |
| 4 indices, pool | option same-day | S4 expiry afternoon 14:30, 0.25 ATR | 1m | monthly EXPIRY | 15:15 or −40% | 30 | real +2.70 / +109.6 / −16.7 (+0.7) | model unusable on expiry day | REJECTED | few days, carried by a few winners | option setups |
| 4 indices, pool | option same-day | S5 call after VIX-UP | 1m | VIX rose over 5% | ratchet, 15:15 | 140 | +1.49 / −3.20 / +0.85 (+0.5) | real −0.27 | REJECTED | TUNE t +0.8 | option setups |
| 4 indices, pool | option same-day | S5 put after VIX-DOWN | 1m | VIX fell over 5% | ratchet, 15:15 | 120 | −2.15 / −6.82 / +0.03 (−1.9) | real −3.40 | REJECTED | falls after VIX drops are not there | option setups |
| 4 indices, pool | option same-day | S6 high turnover first half hour | 1m | ratio over 1.2 | ratchet, 15:15 | 270 | +0.43 / −1.02 / −3.25 (−0.9) | real −1.62 | REJECTED | bigger range, no direction | option setups |
| 222 stocks | stock delivery | P1 pullback 8% | 1d | 10 or more signals that evening (broad dip) | own | 188 | EXCESS over the basket −0.05 / −0.47 / +0.75 (10 to 19), +0.28 / none / none (20 or more); RAW +3.40 TUNE | 0.50% round trip; basket without costs | REJECTED | The broad-dip gain is the basket's (RAW +3.40, basket +3.13 over the same days); 2 broad evenings after TUNE; a book of such evenings less B3 −0.01% a year [−2.31 to +2.26] | pullback conditions |
| 222 stocks | stock delivery | P1 pullback 8% | 1d | 1 to 2 signals that evening (quiet) | own | 95 | EXCESS −0.40 / −0.95 / +1.02 (−2.6) | as above | REJECTED | Not an AVOID: TUNE t −2.47 and positive in TEST; a book that skips quiet evenings is behind US-100's book in TEST | pullback conditions |
| 222 stocks | stock delivery | P1 pullback 8% | 1d | NIFTY 2% or more below its 20-session high | own | 267 | EXCESS −0.08 / −1.42 / +1.90 (2 to 4%), +0.00 / −1.27 / +0.74 (4% or more) | as above | REJECTED | No excess in TUNE; the book of such evenings is behind B2 and B3 in CHOOSE | pullback conditions |
| 222 stocks | stock delivery | P1 pullback 8% | 1d | India VIX over its 60-session median, four bands | own | 45 to 270 | best TUNE EXCESS −0.19 (1.1 to 1.3) | as above | REJECTED | No VIX band beats the basket in TUNE; from March 2017 only | pullback conditions |
| 13 NIFTY leavers | stock delivery | P1 pullback 8% | 1d | NIFTY above SMA200 | own | 37 | +0.39 / +0.32 / +2.13 (1.0) | as above | REJECTED | No better than random dates (116 of 200 runs at or above) | delivery, 222 stocks |
| 222 stocks | stock delivery | P1b pullback 10% | 1d | NIFTY above SMA200 | own | 302 | +1.31 / −0.26 / +2.57 (4.7) | as above | REJECTED | Larger than 8% but negative in CHOOSE (127 trades) | delivery, 222 stocks |
| 222 stocks | stock delivery | M1a new 52-week high | 1d | NIFTY above SMA200 | SMA50 or 60 sessions | 149 | +4.41 / +0.47 / −2.07 (7.3) | as above | REJECTED | Below random dates (+6.33); the rise of survivors, not skill | delivery, 222 stocks |
| 222 stocks | stock delivery | M1b new 52-week high | 1d | NIFTY above SMA200 | ratchet 2 ATR, 3 R, extend | 157 | +0.16 / +0.21 / −1.64 (0.7) | as above | REJECTED | The ratchet removes the trend gain | delivery, 222 stocks |
| 222 stocks | stock delivery | M2a gap up on volume | 1d | all | 20 sessions or gap-day low | 26 | +1.05 / +4.07 / −0.86 (1.3) | as above | REJECTED | Not discovered | delivery, 222 stocks |
| 222 stocks | stock delivery | M2b gap up on volume | 1d | all | 40 sessions or gap-day low | 26 | +3.56 / +6.19 / −1.53 (3.0) | as above | REJECTED | Confirmed, then negative in TEST and below random dates | delivery, 222 stocks |
| 222 stocks | stock delivery | M3 strongest 10% | 1d, monthly | NIFTY above SMA200 | next month's first session | 57 | +12.81 / +0.65 / −4.77 (4.4) | as above | REJECTED | Survivorship: below random dates (+18.85), negative in TEST | delivery, 222 stocks |
| 222 stocks | stock delivery | R1 RSI(2) under 10 | 1d | close above SMA200 | close above SMA5 or 7 sessions | 1,355 | +0.10 / −0.34 / −0.15 (0.7) | as above | REJECTED | The dip edge (+0.13 over random) is smaller than the costs | delivery, 222 stocks |
| NIFTY, BANKNIFTY, FINNIFTY, SENSEX | option same-day | Follow the flow after a market volume spike (z 2.5) | 1m | flow share 0.60 or more / 0.40 or less | 15 minutes | about 180 | +1.1 / +0.5 / −2.1 bp of NIFTY (t −1.2) | needs 5 to 10 bp; not reached | REJECTED | Hit rate 41% to 53%: a coin flip | volume |
| NIFTY, BANKNIFTY, FINNIFTY, SENSEX | option same-day | Trade with pressure_5m (top against bottom fifth) | 5m | pressure_5m top or bottom fifth | 15 minutes | many | −1.2 / −1.4 / −0.9 bp of NIFTY (t below −3) | a fifth of the cost; against the pressure | REJECTED | A small reversal, too small to pay | volume |
| NIFTY, BANKNIFTY | option same-day | First half hour's flow share decides the day | 1d | flow share above or below 0.5 at 09:44 | session end | about 245 | −0.4 / +0.9 / +3.3 bp of NIFTY (t at most 0.9) | not reached | REJECTED | The flow of the open says nothing of the close | volume |
| 18 stocks | stock same-day | every other registered rule | fixed per stock | all | desk rule | varies | best stock +0.10 on TUNE, none holds | 16.6 bp round trip | REJECTED | one share cannot carry the costs | setup grid |
| Each index | option same-day | its best fixed setup | see table | all | desk rule | 25 to 50 | positive on TUNE, not after | model | REJECTED | a timeframe chosen on the past did not hold | setup grid |

## 5. What the desk runs today, and what this book says about it

| Slot | Standing | Note |
|---|---|---|
| `microtrend` 3m, options on NIFTY, BANKNIFTY, FINNIFTY, SENSEX | no edge shown | The owner's chosen paper test. Since it went live the desk ran on 2 of 7 sessions, so the live record is 2 alerts. |
| `pullback` delivery, 18 stocks, about Rs 10,000 | WATCH | Keeps its own exits. Few alerts with 18 stocks. |
| `pullback` wide list, 204 more stocks, daily bars (US-099) | WATCH, paper test | Since 25 Nov 2026: every signal recorded, at most 10 open within the cap (the desk 18 counted), only those alerted. About ten signals a week. The product's round reproduces the study's trades on the research file (`docs/04_OPERATIONS.md`). Book reading (US-100): the cap tier holds 2,560 of 8,085 trades, +0.19% a trade without a cash limit, +0.14 with Rs 1 lakh; tier all +0.77. The product's order (desk 18, then deepest) is as good as any of six tested; no cap beats 10 in all three periods. By market condition (US-101): no change; no condition beats the basket and no condition is AVOID. |
| Stop, target and trail rule on micro-trend trades | neutral | Five years: it costs nothing measurable and adds nothing; the entries decide. |
| Volume Watch alerts | information | A spike means a larger move is likely, never a direction. Not a trade alert. |

## 6. What to test next, most promising first

1. The pullback rule on the wide universe as a PAPER test on the desk: built (US-099, see
   section 5); what is left is to let it run and read its score in both tiers each month.
   What the paper test must show (US-100): its yardstick is tier all, not the cap. With one
   week's trades counted as one draw, tier all (about 42 trades a month over the history, 9 in
   the last year) needs about 48 months to tell its +0.77% a trade from zero at t = 2 (about
   18 at the last year's size); the held trades of the cap (about 13 a month, 5 in the last
   year) at their +0.14% would need over 80 years (about 7 at the last year's size), so their
   score says nothing on its own. A t of 2 against zero is not an edge: about +0.6 of the +0.77
   is market drift. Each month compare both tiers with NIFTY and with the 222-stock basket at
   the same exposure; a book that only matches the basket shows no edge. Since US-101 the rule's
   trades also trail the same stocks held over the same days (−0.16% a trade over the span; the
   basket carries no costs), so the test should score each trade against that basket too.
2. Real put prices from the broker's history, to settle the long-volatility question and to
   price the Simulator's put trades exactly.
3. The four WATCH option cells, followed on paper with real quotes for three months.
4. Whether option alerts should be kept to NIFTY and BANKNIFTY until the spreads on FINNIFTY
   and SENSEX are measured over more sessions (the owner's rule says four indices).

## 7. Changes

- 2026-11-25: the pullback rule by market condition (US-101): 13 cells, none discovered on the
  excess over the basket, none AVOID; three books that wait for broad or deep evenings or skip
  quiet ones, none ahead beyond chance; no change to the wide list. Four REJECTED headline rows in
  the register (no cell is other than REJECTED); sections 2, 5 and 6 updated.
- 2026-11-25: the capped pullback book (US-100): the book of Rs 1 lakh and 10 places against
  NIFTY and the basket; rows in sections 2, 4, 5 and 6 updated. After two reviews the same day:
  "behind the basket" is borderline (outside zero with the fixed seed only); paper-test months are
  week-clustered (48 for tier all, not 7 to 11); the cap's cost is read on the same evenings.
- 2026-11-25: first edition. Five studies of 24 November 2026: the desk's own live alerts,
  new option setups and long volatility, delivery setups on 222 stocks, the volume study, and
  the setup grid (22 instruments, 3,948 cells).
