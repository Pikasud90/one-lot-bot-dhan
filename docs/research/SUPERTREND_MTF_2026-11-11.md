# The multi-timeframe Supertrend, with the line as stop and trail (2026-11-11)

The owner's request of 11 November 2026, in order: read the saved brute-force runs again; backtest
the three paper-test slots in depth; then fine-tune with a multi-timeframe "micro trend"
(Supertrend with period 14 and multiplier 1.7 on 1m, 5m, 15m, 30m, 60m and 1d bars, trade only
when they agree) with the Supertrend line as the stop and the trail: "that trail and stop loss
should be very perfectly implemented. It should move across with the market itself, such that it
can properly capture the long horizon profits."

This file holds the method and the tables. It follows
[CROSS_RETUNE_2026-11-07.md](CROSS_RETUNE_2026-11-07.md) and
[REAL_OPTIONS_2026-11-08.md](REAL_OPTIONS_2026-11-08.md). What the results mean for the app is in
section 3 of [REVIEW.md](../../REVIEW.md). Nothing here places an order. Every number is a
backtest on past data after modelled costs, never investment advice and never a forecast.

Unless a table says otherwise, a result is the mean net return of one trade in percent of the
entry price (for an option, percent of the premium paid), pooled over the basket, and three
numbers in a row are the periods TUNE / CHOOSE / TEST.

## 1. The answer in short

| Question | Answer |
|---|---|
| Do the saved brute-force runs hold a setting that works? | No. Of 5,752 settings, none is positive after costs in all three periods |
| The index paper slot (`supertrend_vol` 30m, calls) | +2.94% a trade on modelled premiums over five years (485 trades, t = 2.4); +0.50% in TEST (t = 0.2). The result is the direction of the day: +9.3% on days NIFTY closed up, −12.4% on days it closed down |
| The two stock paper slots (short, one share) | Net ₹296 and ₹274 in five years. The gross result is +0.22% to +0.25% a trade against a cost of about 0.16% |
| The owner's rule as stated, same day, stocks | Negative in every period for every line and every way of acting on it. Best: −0.13% (short, 15m line) |
| The owner's rule as stated, same day, index options | Negative for every line. Best: −0.39% (calls, 60m line, exit on a close), TEST −1.2% |
| The owner's rule with calls carried overnight behind the 30m line (`carry30`) | +4.10% a trade on modelled premiums (1,041 trades, t = 2.3), TEST −2.16%. On real premiums −9.14% for the 41% of trades that could be priced; the trades that could not be priced are the modelled winners. Not confirmed |
| The best same-day form (`intra_volc`: fresh 30m turn, ADX and RSI, all six agree) | +3.07% a trade modelled (204 trades, t = 2.2), positive in all three periods. On real premiums +0.74% (t = 0.3) for 110 priced trades. Not confirmed |
| Did passing TUNE and CHOOSE predict TEST? | No. Of the 2,511 index-option structures that passed, 10% were positive in TEST; of the 9,956 that did not pass, 36% were |
| Stocks at one share | No candidate. The best carried long makes +0.28% a trade (t = 1.4) and −1.64% once the depository charge of ₹14.75 a sale is counted |
| The line as stop and trail | Built into the Lab simulator and the paper book. A replay of stored bars through the desk's own code gave the Lab's trades exactly: 10 of 10 and 57 of 57 |

The form `intra_volc` went into the paper test as the slot `supertrend_mtf`. It is there as the
owner's rule in its best same-day form, not as a proven edge.

## 2. Data

### 2.1 What was used

- One-minute bars from Dhan in `data/onelot.db`, from 8 November 2021 to 7 November 2026.
- 18 stocks at one share: AXISBANK, BAJFINANCE, BHARTIARTL, HDFCBANK, HEROMOTOCO, ICICIBANK,
  INFY, ITC, KOTAKBANK, LT, MARUTI, MM, RELIANCE, SBIN, SUNPHARMA, TATASTEEL, TCS and TMPV. Adani
  Green is left out, as the owner decided earlier.
- 5 indices, traded only by buying one lot of an option: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY
  and SENSEX.
- India VIX, for the option model.
- Dhan's one-minute bars of expired index options, for section 9.

### 2.2 Problems found in the store, and the repair

All repairs are made when the bars are read (`src/onelot/data/hygiene.py`, used by the Lab's data
cache and by the desk through `src/onelot/desk/history.py`). The database itself is not changed.

| Problem | Evidence | Repair |
|---|---|---|
| Sessions that are not real trading | 2021-11-04 (the store holds only flat bars from 14:47 to 15:29), 2021-11-13, 2022-01-08, 2022-01-22, 2022-02-05, 2022-04-09 and 2022-04-30 (exchange mock trading) | Dropped |
| Real special sessions | 2024-01-20, 2024-03-02, 2024-05-18, 2025-02-01, 2026-02-01, and the Muhurat sessions of 2025-10-21 and 2026-11-08 | Kept |
| TMPV (Tata Motors) demerger | A gap of −39.8% on 2025-10-14 that the vendor left unadjusted | Earlier bars adjusted |
| ITC demerger | A gap of −5.8% on 2025-01-06, also unadjusted | Earlier bars adjusted |
| Daily bars up to 2026-10-26 came from Yahoo, one-minute bars from Dhan | The scale differs: RELIANCE about 2% (2021-11 to 2023-07), ITC 3.6%, TMPV 39.5%, MIDCPNIFTY 24.6% (Yahoo's ticker is a different index, with a false −19% jump on 2026-10-27). Yahoo lacks four real sessions and has bars for three days without a regular session | Daily bars rebuilt from the cleaned one-minute bars |
| Stock bars end at 15:14 since 2026-08-03 | The Closing Auction Session: 375 one-minute bars a day before, 360 after | The research exits stocks at 15:10. The product's session table is still stale (finding D-1 of the review) |
| The option model reads the same day's VIX close | The level of implied volatility is known only at the close | Kept, since it is the Lab's model. Every finalist is run again with the VIX of the day before (section 8) |

The repair changes little in the index slot (489 trades at +2.858% on the bars as stored, 485
at +2.936% after the repair). It matters for the daily Supertrend and for the two stocks with a
demerger.

## 3. Method

### 3.1 Periods

| Period | Dates | Use |
|---|---|---|
| TUNE | 8 November 2021 to 7 November 2024 | Three years, also read year by year |
| CHOOSE | 8 November 2024 to 7 November 2025 | A structure must also be positive here |
| TEST | 8 November 2025 to 7 November 2026 | Read only after the ranking is fixed |

TEST was already read by the studies of 7 and 8 November, so nothing below is a clean test.
The only untouched bars are those from 8 November 2026 onward, and the paper test is the test.

### 3.2 One setting for a whole basket

One structure is applied to all 18 stocks, or to all 5 index options. No setting is chosen for
one instrument. A result is the pooled mean over the basket.

### 3.3 The ladder of timeframes, and the fill

- The six timeframes are 1m, 5m, 15m, 30m, 60m and 1d, all resampled from the cleaned one-minute
  bars. The owner also named 6h and 12h; the session lasts 6 hours 15 minutes, so both are the
  daily bar.
- Supertrend(14, 1.7) is computed on each timeframe. The first 70 bars of a timeframe are
  warm-up.
- No look-ahead. The state used at the start of a one-minute bar is the last bar of each
  timeframe that had closed by then (its start plus its length is at or before that minute; a
  daily bar counts as closed at midnight). The fill is at the open of that one-minute bar.
- The entry window is 09:30 to 14:45. One position at a time for each instrument. An entry is
  skipped when the open is already beyond the line that would be the stop.
- Same-day trades end at 15:10 for stocks and at 15:15 for index options. Carried trades: stocks
  long only, with delivery charges; options are held at the latest to the monthly expiry chosen
  at the entry.

### 3.4 Costs

| Basket | Model |
|---|---|
| Stocks | 0.03% slippage a side plus the charges of `config/costs.yaml`. A same-day round trip costs about 0.166% of the price |
| Stocks, carried | Delivery charges. The depository charge (₹12.50 plus GST, ₹14.75 for each delivery sale) is not in `config/costs.yaml`. Structures are ranked without it and reported with it |
| Index options | The Lab's model: a monthly call or put of about 0.65 delta priced with Black-76, implied volatility = India VIX × 1.15, a half-spread of 0.6% plus two ticks a side, and the charges |

Two known biases of the option model. It charges time decay by calendar time, so six trading
hours cost too little: a call bought at 09:30 and sold at the close shows a gross +0.35% a
trade. Same-day option results on modelled premiums are therefore optimistic. And the Live Desk
measures half-spreads far above 0.6% on FINNIFTY and SENSEX contracts (finding S-6 of the
review).

### 3.5 The numbers reported

- The mean net return of a trade, in percent, for each period and for all five years.
- t: the t-statistic of that mean, with all trades entered on one day counted as one
  observation.
- The share of winning trades, the result before costs ("gross"), and the net rupees at one
  share or one lot a trade.
- A no-signal benchmark: one trade every session on every instrument, entered at 09:30 and held
  to the close or for 1, 2, 3, 5 or 8 sessions.

### 3.6 The selection rules, written before the results

Stage 1 (written before stage 1 ran). A structure is eligible when all of these hold:

1. Trades in TUNE: at least 150 for stocks, 60 for options. Trades in CHOOSE: at least 40 and 20.
2. The pooled mean is above zero in TUNE and in CHOOSE.
3. At least two of the three TUNE years are positive.
4. At least half of the instruments are positive over TUNE and CHOOSE together.

Eligible structures are ranked by the smaller of the two t values (TUNE, CHOOSE). TEST is read
only after the ranking is fixed, for the top structure, for the top 20, and as the share of
eligible structures that are positive in TEST. The owner's exact rule (all six timeframes, each
line, each way of acting on it) is reported whatever it shows.

Stage 2 (written before stage 2 ran). The rule above stays. A structure is called a paper-test
candidate only if it also:

1. beats the no-signal benchmark of its side and nearest holding time in TUNE and in CHOOSE;
2. stays positive in TUNE and CHOOSE with costs × 1.5, with the entry one minute late and, for
   options, with the VIX of the day before.

A third condition was added when stage 2 had been read, before any stress result or real-premium
result existed: an option structure must also hold up on real premiums, because the modelled
premiums carry the biases of section 3.4.

TEST is reported, and nothing is called validated. The validation is the paper test, reviewed
at 30 closed trades a slot.

### 3.7 The research harness is checked against the product code

The grids are too large for the Lab simulator, so two scripts simulate them directly. Before a
result was read, each was checked:

| Check | Result |
|---|---|
| Charges of the harness against `CostModel.leg` | Equal to 1e-12 |
| 144 random structures on SBIN and the BANKNIFTY option (135,055 trades) against a plain bar-by-bar loop | The same list of trades |
| 1,080 sampled trades against the Lab's `execute_trade` | Equal to the rounding (₹0.005, 0.00005%) |
| Stage 2 against stage 1: SBIN, 18 structures, 8,810 trades; BANKNIFTY option, 24 structures, 12,456 trades | Equal |
| The whole stage-2 grid of SBIN (1,295,570 trades) and of the BANKNIFTY option (1,882,727 trades) against the path that runs one structure at a time, on 200 samples | Equal |

Stage 1 ran 23 instruments, 5,760 structures each, about 115 million simulated trades, in 70
seconds. Stage 2 ran 14,976 structures for each instrument in 72 seconds on 5 processes.

### 3.8 How many things were tried

5,752 settings in the retune of 7 November; 2,500 backtests of the three slots; 5,760 structures
for each instrument in stage 1; 14,976 in stage 2. A structure reported below with t = 2.3 is the
best of about 15,000, so that t is not evidence by itself.

## 4. The saved brute-force runs, read again

`scripts/bruteforce_analysis.py` reads the saved results of the retune of 7 November: 5,752
settings of the scoreboard strategies. None is positive after costs in all three periods.

| Group | Strategy, timeframe | Settings | Positive in TUNE / CHOOSE / TEST | Median % a trade, TUNE / CHOOSE / TEST |
|---|---|---|---|---|
| Index options | `supertrend_vol` 30m | 68 | 60% / 69% / 6% | +0.33 / +1.17 / −4.10 |
| Index options | `cpr_breakout` 60m | 69 | 51% / 0% / 48% | +0.07 / −3.52 / −0.06 |
| Index options | `supertrend_vol` 60m | 67 | 25% / 25% / 34% | −0.91 / −1.66 / −3.10 |
| Index options | every other family | | | −1 to −5 |
| Stocks | `supertrend_vol` 30m | 70 | 41% / 0% / 9% | −0.06 / −0.14 / −0.07 |
| Stocks | `supertrend_adx` 30m | 30 | 17% / 0% / 3% | −0.07 / −0.20 / −0.11 |
| Stocks | `gap_fade` 15m | 61 | 16% / 2% / 0% | −0.07 / −0.13 / −0.12 |
| Stocks | every other family | | | −0.05 to −0.27 |

- Direction. Index options: calls only are positive in 17% / 17% / 2% of settings, puts only in
  9% / 9% / 19%. Stocks: short only 9% / 2% / 3%, long only 3% / 1% / 0%.
- Timeframe. None is consistently better. Index options: 15m 6% / 9% / 10%, 30m 10% / 18% / 4%,
  60m 16% / 6% / 17%.
- Instrument. None is consistently favourable. An index is profitable in 12% to 29% of settings
  in a given period, a stock in 6% to 31%.
- Supertrend multiplier on index options. 2.0 is the only value with a positive median in TUNE
  and CHOOSE (+0.45, +1.12), and it loses 2.39% in TEST.

## 5. The three paper slots in depth

`scripts/deep_backtest_candidates.py` runs the three slots of `config/desk.yaml` through the
Lab's own `run_backtest` on the cleaned bars: 2,500 backtests in all, counting the runs with one
setting changed.

### 5.1 The index slot: `supertrend_vol` on 30m bars, calls only

| Run | Trades | TUNE | CHOOSE | TEST | All five years | t | Net ₹ at one lot |
|---|---|---|---|---|---|---|---|
| Before costs | 485 | | | | +3.304 | 2.68 | 392,782 |
| After costs | 485 (294 / 101 / 90) | +3.562 (t 2.24) | +3.286 (t 1.07) | +0.499 (t 0.23) | +2.936 | 2.38 | 341,789 |
| Costs × 1.5 | 485 | | | | +2.752 | | |
| Costs × 2 | 485 | | | | +2.568 | | |

55.7% of the trades win; the median trade makes +1.778%. Charges are ₹50,993, or ₹105.1 a trade.

- By year: +1.957, +5.559, +3.246, +3.286, +0.499.
- By index: BANKNIFTY 113 trades +2.594, NIFTY 81 +4.281, FINNIFTY 104 +5.262, MIDCPNIFTY 99
  −0.060, SENSEX 88 +2.761.
- 271 of the 485 trades are entered on the 09:45 bar (+3.174).

| How the trade ended | Trades | Winners | % a trade | Net ₹ |
|---|---|---|---|---|
| Session close | 366 | 68.6% | +7.988 | +777,734 |
| Stop | 53 | 0% | −20.663 | −336,268 |
| Opposite signal (flip) | 45 | 0% | −18.333 | −221,768 |
| Trail | 18 | 88.9% | +14.895 | |
| Target | 3 | | +50.811 | |

| The day, known only afterwards | Trades | Winners | % a trade | Net ₹ |
|---|---|---|---|---|
| NIFTY closed up | 343 | 72.6% | +9.286 | +845,157 |
| NIFTY closed down | 142 | 14.8% | −12.403 | −503,369 |
| Trend day | 211 | | +8.506 | |
| Range day | 274 | | −1.353 | |

In units of the initial risk (R): the average trade went +0.62R in its favour at best and 0.43R
against it at worst, and ended at +0.10R. 48% of trades reached +0.5R, 20% reached +1R and 2%
reached +2R.

One setting changed at a time (all five years, % a trade; the slot's own value is marked):

| Setting | Values and results |
|---|---|
| Direction | calls only **+2.936** (485 trades); both sides +1.117 (992); puts only −0.099 (630; TEST +5.123) |
| Timeframe | 15m −1.264 (977 trades); **30m +2.936**; 60m +0.735 (307) |
| Last entry time | 11:30 +3.029; **13:00 +2.936**; 14:45 +1.991 |
| Supertrend, period 14 | multiplier 1.5 −0.644 (745 trades; TEST −3.677); 1.7: +1.314 (639 trades; +2.599 / +1.508 / −2.534; net ₹184,732); **2.0 +2.936**; 2.5 +2.144; 3.0 +4.125 (244 trades) |
| Supertrend, other periods | 7/2.0 +2.723; 7/3.0 +1.503; 10/2.0 +2.834; 10/3.0 +2.998; 21/2.0 +2.446 |
| Minimum ADX | 0: +2.883; **12: +2.936**; 20: +3.590; 25: +3.792 |
| Bars since the turn | 1: +2.628; **2: +2.936**; 4: +2.732 |
| Longest hold | 6 bars +1.200; **12 bars +2.936** |
| Minimum strength | 0: +1.642 (693 trades); **40: +2.936** |

The owner's multiplier of 1.7, put into this slot in place of 2.0, gives more trades and less
than half the result, and it is negative in TEST.

### 5.2 The two stock slots, short only, one share, 18 stocks

| Slot | Before costs | After costs | Costs × 1.5 / × 2 | TUNE / CHOOSE / TEST | Rupees in five years |
|---|---|---|---|---|---|
| `supertrend_vol` 60m | 132 trades, +0.245% (t 3.12) | 129 trades, 51.9% winners, +0.096% (t 1.18) | +0.043 / −0.010 | +0.146 / −0.035 / +0.096 | Gross 524, charges 228 (1.8 a trade), net 296 |
| `supertrend_adx` 30m | 179 trades, +0.220% (t 2.63) | 175 trades, 48.6% winners, +0.050% (t 0.59) | −0.003 / −0.056 | +0.136 / −0.132 / +0.064 | Gross 635, charges 362 (2.1 a trade), net 274 |

Each slot trades two to three times a month across the 18 stocks. Both are negative in CHOOSE.
Neither result can be told from zero.

## 6. Stage 1: every reading of the owner's rule

### 6.1 The grid

The sentence "when the timeframes agree, trade, and let the Supertrend line be the stop and the
trail" leaves choices open. `scripts/mtf_bruteforce.py` tries all of them:

| Choice | Values |
|---|---|
| Side | Long or short. For an index: buy the call or buy the put, never a sold option |
| Which timeframes must agree | Every non-empty subset of the six (63) |
| Whose line is the stop and the trail | Each timeframe of the subset |
| How the line is acted on | `touch`: a resting stop at the line. `alert`: the desk alerts when the line is touched and the trade is closed at the next one-minute open. `close`: exit only when that Supertrend flips |
| When to enter | `fresh`: the minute the agreement appears. `once`: the first allowed minute of an agreement. `daily`: the first allowed minute of an agreement in each session |
| How long to hold | Same day, or carried until the line ends the trade |

That is 5,760 structures for each instrument.

### 6.2 Stocks, one share

**No signal.** Buying every stock at 09:30 and selling at the close makes −0.171% a trade
(22,284 trades, t = −9.2, 41.5% winners, gross −0.005%). Selling short makes −0.162% (t = −8.7).
The cost of a round trip is the whole result. The basket itself moved +8.6%, +10.7%, +29.5%,
+9.5% and −4.6% in the five research years.

**The rule as stated: all six timeframes agree, same day, entry the minute the agreement
appears.** All five years, with the line touched as a resting stop:

| Line used as stop | Long: trades | Long: % a trade | Long: winners | Short: trades | Short: % a trade | Short: winners | Short: held |
|---|---|---|---|---|---|---|---|
| 1m | 24,813 | −0.168 | 13.4% | 20,697 | −0.164 | 14.9% | 12 minutes |
| 5m | 11,629 | −0.157 | 23.4% | 9,397 | −0.144 | 25.9% | 63 minutes |
| 15m | 6,788 | −0.137 | 32.2% | 5,667 | −0.129 | 35.0% | 150 minutes |
| 30m | 5,944 | −0.141 | 36.8% | 4,992 | −0.144 | 38.8% | 203 minutes |
| 60m | 5,730 | −0.138 | 38.9% | 4,810 | −0.152 | 41.2% | 236 minutes |
| 1d | 5,704 | −0.138 | 39.3% | 4,774 | −0.157 | 42.1% | 255 minutes |

Every row is negative in each of the three periods. The best is the short side behind the 15m
line: −0.118 / −0.171 / −0.119, gross +0.037%. An exit on a close instead of a touch changes a
row by about 0.01 points at most. The 1m line is reached after about 12 minutes.

**The rule as stated, longs carried overnight** (entry at the first allowed minute of an
agreement; delivery charges, without the depository charge):

| Line used as stop | Trades | TUNE | CHOOSE | TEST | All five years | Gross | Held |
|---|---|---|---|---|---|---|---|
| 15m, exit on a close | 4,578 | −0.154 | −0.112 | −0.243 | −0.162 | +0.106 | 0.7 sessions |
| 30m, exit on a close | 3,235 | −0.130 | −0.059 | −0.228 | −0.135 | +0.180 | 1.4 |
| 60m, exit on a close | 2,340 | −0.037 | −0.078 | −0.291 | −0.090 | +0.249 | 2.5 |
| 1d, touch | 971 | +0.502 (t 2.2) | −0.267 | −0.424 | +0.169 | +0.514 | 10.1 |
| 1d, exit on a close | 643 | +1.089 (t 2.6) | −0.089 | −0.423 | +0.526 | +0.881 | 17.1 |

The gross result grows with the slowness of the line. Only the daily line is positive after
costs, and only in TUNE, which holds the bull years.

**The whole grid.**

| Group | Structures with enough trades | Positive in TUNE / CHOOSE / TEST | Positive in all three | Median, all five years | Median gross |
|---|---|---|---|---|---|
| Same day | 3,450 | 0% / 0% / 0% | 0 | −0.152 | +0.015 |
| Carried | 1,149 | 16% / 0% / 0% | 0 | −0.171 | +0.077 |

More timeframes in agreement do lift the gross result of a same-day trade: +0.012% with one
timeframe, +0.013, +0.014, +0.016, +0.018 and +0.021% with six. The effect is there, and it is
about a tenth of the cost.

**Selection.** One structure is eligible: long, the daily line alone, `alert`, carried, one
entry for each trend. TUNE +0.469 (t 1.5), CHOOSE +0.016, TEST −0.098. With the depository
charge on one share it makes −1.865% a trade (t = −8.9): a fixed ₹14.75 on a share of ₹1,500 is
about 1%.

### 6.3 Index options, one lot, modelled premiums

**No signal.**

| One trade every session | TUNE | CHOOSE | TEST | All five years | t | Gross |
|---|---|---|---|---|---|---|
| Buy the call at 09:30, sell at the close | −1.185 | −0.980 | −1.815 | −1.271 | −2.1 | +0.348 |
| Buy the put at 09:30, sell at the close | −0.171 | −0.296 | +0.229 | −0.116 | −0.2 | +1.508 |

The indices themselves moved +1.3%, +8.0%, +25.4%, +8.3% and +0.3% in the five research years
(the mean of the five).

**The rule as stated, calls, same day** (entry the minute the agreement appears):

| Line used as stop | Trades | TUNE | CHOOSE | TEST | All five years | Winners |
|---|---|---|---|---|---|---|
| 1m, touch | 10,096 | −1.703 | −1.485 | −1.575 | −1.639 (t −31.9) | 17.1% |
| 5m, touch | 4,211 | −1.225 | −1.175 | −1.519 | −1.258 (t −6.5) | 27.4% |
| 15m, touch | 2,338 | −0.665 | −0.070 | −0.753 | −0.557 | 35.9% |
| 30m, touch | 2,041 | −0.684 | +0.315 | −0.981 | −0.528 | 40.7% |
| 30m, exit on a close | 1,971 | −0.934 | +0.336 | −1.231 | −0.723 | 42.6% |
| 60m, touch | 1,964 | −0.586 | −0.007 | −0.993 | −0.530 | 43.9% |
| 60m, exit on a close | 1,940 | −0.359 | +0.178 | −1.234 | −0.385 | 44.8% |
| 1d, touch | 1,942 | −0.405 | +0.390 | −1.536 | −0.420 | 44.9% |

**The rule as stated, carried** (entry at the first allowed minute of an agreement):

| Line used as stop | Calls: trades | Calls: TUNE / CHOOSE / TEST | Calls: all five years | Calls: held | Puts: all five years |
|---|---|---|---|---|---|
| 5m, touch | 4,314 | −0.967 / −0.609 / −1.207 | −0.929 (t −3.9) | 169 minutes | −1.164 |
| 15m, touch | 1,985 | +0.497 / +2.634 / −0.004 | +0.835 (t 1.1) | 909 minutes | −1.536 |
| 15m, exit on a close | 1,502 | +1.597 / +5.053 / −0.716 | +1.893 (t 1.7) | 0.7 sessions | −2.398 |
| 30m, touch | 1,346 | +1.458 / +6.877 / −1.621 | +1.987 (t 1.6) | 0.9 | −2.677 |
| 30m, exit on a close (`carry30`) | 1,041 | +3.611 / +11.764 / −2.158 | +4.097 (t 2.3) | 1.4 | −2.920 |
| 60m, touch | 942 | +4.572 / +9.065 / −0.522 | +4.609 (t 2.2) | 1.7 | −4.228 |
| 60m, exit on a close | 734 | +8.369 / +11.209 / −1.669 | +7.140 (t 2.5) | 2.5 | −5.424 |
| 1d, touch | 386 | +17.602 / +17.166 / −19.110 | +10.194 (t 1.7) | 7.1 | −13.030 |
| 1d, exit on a close | 305 | +15.651 / +19.189 / −27.297 | +7.769 (t 1.0) | 10.0 | −14.677 |

Carried calls gain more the slower the line, in TUNE and in CHOOSE, and every one of them from
the 15m line up is negative in TEST. Carried puts lose more the slower the line.

**The whole grid.**

| Group | Structures with enough trades | Positive in TUNE / CHOOSE / TEST | Positive in all three | Median, all five years | Median gross |
|---|---|---|---|---|---|
| Same day | 3,450 | 11% / 37% / 6% | 21 (0.6%) | −0.870 | +0.751 |
| Carried | 2,298 | 32% / 49% / 20% | 50 (2.2%) | −0.878 | +0.742 |
| Carried calls, 15m line | 192 | 83% / 97% / 19% | 17% | +0.543 | +2.193 |
| Carried calls, 30m line | 192 | 100% / 100% / 2% | 2% | +1.836 | +3.505 |
| Carried calls, 60m line | 192 | 100% / 100% / 5% | 5% | +2.533 | +4.197 |
| Carried calls, 1d line | 189 | 99% / 97% / 0% | 0% | +7.769 | +9.495 |

On index options more agreement does not help. The median same-day structure makes −0.679%
with one timeframe and −0.944% with six; carried, −0.655% with one and −1.354% with six.

Of the 50 carried structures that are positive in all three periods, 33 are calls behind the
15m line, 9 are calls behind the 60m line, 3 are calls behind the 30m line and 5 are puts behind
the 15m line. One example is [30m + 60m + 1d] with the 30m line and `alert`: +3.552 / +3.754 /
+1.127, +3.170% over five years (791 trades, t = 2.2). They were found with the TEST year in
view, so they describe the past and are not a selection.

**Selection.** 893 of the 5,760 structures are eligible. 7% of them are positive in TEST, against
12% of those that are not eligible. The top structure by the rule (calls, the daily line alone,
`touch`, carried) made +23.2 / +48.9 / −25.2, negative in TEST on all five indices. A pass in
TUNE and CHOOSE said nothing about TEST.

## 7. Stage 2: the scoreboard strategies under the owner's logic

The question of stage 2: do the two scoreboard strategies, `supertrend_vol` and `supertrend_adx`,
earn anything under the owner's logic, that is with Supertrend(14, 1.7), with agreement of the
timeframes, and with the line as stop and trail?

### 7.1 The grid

`scripts/mtf_stage2.py` runs 14,976 structures for each instrument. The design was written down
before any stage-2 result existed.

| Choice | Values |
|---|---|
| Side | Long or short. For an index: buy the call or buy the put |
| Entry timeframe E | 15m, 30m or 60m. The Supertrend(14, 1.7) trend of E must be on the trade's side |
| Age of the turn | The flip is at most 2 bars old; at most 4 bars old; any age with one entry for each trend (`any/once`); any age with one entry for each trend and session (`any/daily`) |
| Filter | None, or one of five. `vol_a`: ADX at least 12, RSI at least 55 for a long and at most 45 for a short, relative volume at least 1.0 (the index slot). `vol_b`: 18, 52 and 48, 1.0 (the defaults of `supertrend_vol`). `vol_c`: 25, 60 and 40, 1.5. `adx_a`: ADX at least 25 and rising over 3 bars, DI on the trade's side, MACD histogram on that side and not weakening, price on that side of VWAP and within 3.0 ATR of it, relative volume at least 0.9 (the stock slot). `adx_b`: the same with ADX at least 20 and 1.5 ATR (the defaults of `supertrend_adx`) |
| Gate: the other timeframes that must agree with E | None (`E only`); the daily bar; all slower than E; all faster than E; all six |
| Line used as stop and trail | E's line, or the 60m or the 1d line when that timeframe is in the gate. Acted on by `touch` or by `close` |
| Last entry time | 11:30, 13:00 or 14:45. The window opens at 09:30 |
| Holding | Same day, or carried: stocks long only, options to the monthly expiry at the latest |

The filters are computed on E bars and read from the last closed bar. An index has no volume,
so the volume condition is neutral there, as in the Lab. The rule is checked every minute, and
the entry is the first minute when every condition holds, once for each trend of E. The data,
the costs and the option model are those of stage 1.

### 7.2 Stocks, one share

**No signal.**

| One trade every session | TUNE | CHOOSE | TEST |
|---|---|---|---|
| Buy at 09:30, sell at the close | −0.165 | −0.154 | −0.206 |
| Buy at 09:30, hold 1 session | −0.285 | −0.297 | −0.413 |
| Buy at 09:30, hold 3 sessions | −0.155 | −0.202 | −0.475 |
| Buy at 09:30, hold 5 sessions | −0.029 | −0.089 | −0.560 |
| Buy at 09:30, hold 8 sessions | +0.155 | +0.082 | −0.724 |
| Sell short at 09:30, buy back at the close | −0.168 | −0.179 | −0.126 |

**The two stock slots under the owner's logic.**

| Structure | Trades | TUNE | CHOOSE | TEST | All five years |
|---|---|---|---|---|---|
| `supertrend_vol`: short, 60m, turn at most 4 bars old, `vol_a`, no gate, 60m line by `touch`, entries to 11:30 | 1,243 | −0.054 | −0.072 | −0.054 | −0.057 (gross +0.109) |
| The same, all six agree | | −0.117 | −0.061 | −0.135 | |
| `supertrend_adx`: short, 30m, turn at most 4 bars old, `adx_a`, no gate, 30m line by `touch`, entries to 11:30 | 404 | +0.035 | −0.167 | +0.043 | −0.000 |
| The same, exit on a close | 404 | +0.068 | −0.177 | +0.056 | +0.020 (t 0.3) |
| The same, all six agree | | +0.053 | −0.089 | −0.170 | |

The long versions make −0.09% to −0.18% a trade on the same day.

**The whole grid.**

| Group | Structures with enough trades | Positive in TUNE / CHOOSE / TEST | Positive in all three | Median, all five years | Median gross |
|---|---|---|---|---|---|
| Same day, long | 3,460 | 2% / 0% / 1% | 0 | −0.134 | +0.032 |
| Same day, short | 3,194 | 5% / 0% / 2% | 0 | −0.135 | +0.032 |
| Carried, long | 3,388 | 34% / 2% / 7% | 10 (0.3%) | −0.135 | +0.177 |

No filter, gate, age of the turn or entry window changes the same-day picture. The gross result
is +0.01% to +0.06% a trade against a cost of about 0.166%.

**Selection.** Of the 14,976 structures, 10,050 have enough trades, 1,396 of those are positive
in TUNE and 16 of those are positive in CHOOSE. 15 are eligible, all of them carried longs. 13
beat the benchmark, and 9 are positive in TEST (the median of the 15 is +0.029%).

The top structure by the rule, `carry60_adx`: long, 60m, turn at most 2 bars old, `adx_b`, E and
all faster timeframes agree, the 60m line, exit on a close, entries to 14:45, carried.

| Period | Trades | % a trade |
|---|---|---|
| TUNE | 161 | +0.297 (t 1.1) |
| CHOOSE | 62 | +0.299 (t 0.8) |
| TEST | 57 | +0.208 (t 0.5) |
| All five years | 280 | +0.279 (t 1.44) |
| All five years, with the depository charge | 280 | −1.636 (t −7.35) |

Over five years 36.1% of its trades win, the profit factor is 1.31, the gross result is +0.623%,
a trade is held 2.7 sessions, and the net is ₹1,895 at one share a trade. By year: +0.470,
−0.394, +0.734, +0.299, +0.208.

**Verdict.** Nothing on stocks survives the costs. On the same day both strategies lose on both
sides under the owner's logic. The one eligible carried family has a t of 1.5 at most after
about 15,000 tries, and the fixed depository charge removes it at one share. There is no stock
candidate.

### 7.3 Index options, one lot, modelled premiums

**No signal.**

| One trade every session | TUNE | CHOOSE | TEST |
|---|---|---|---|
| Call at 09:30, sold at the close | −1.185 | −0.980 | −1.815 |
| Call at 09:30, held 1 session | −0.904 | −0.518 | −4.159 |
| Call at 09:30, held 2 sessions | −0.221 | −0.289 | −7.385 |
| Call at 09:30, held 3 sessions | −0.016 | +0.336 | −11.113 |
| Call at 09:30, held 5 sessions | +0.828 | +1.367 | −18.193 |
| Put at 09:30, sold at the close | −0.171 | −0.296 | +0.229 |
| Put at 09:30, held 1 session | −2.847 | −1.971 | +1.189 |
| Put at 09:30, held 5 sessions | −10.958 | −6.580 | +8.054 |

**The scoreboard strategies under the owner's logic.**

| Structure | Trades | TUNE | CHOOSE | TEST | All five years |
|---|---|---|---|---|---|
| `slot_vol`: `supertrend_vol`, calls, 30m, turn at most 2 bars old, `vol_a`, no gate, 30m line by `touch`, entries to 13:00, same day | 948 | +1.453 (t 1.3) | +0.807 | −0.694 | +0.875 (t 1.0) |
| The same, all six agree | | −0.669 | −1.746 | +1.136 | |
| The same, carried, exit on a close | | +3.400 | +10.968 | −5.950 | |
| `supertrend_adx` with its slot filter (`adx_a`) | 132 | | | | +0.015 |

`slot_vol` is the running index slot rebuilt in the research harness with Supertrend(14, 1.7)
and the line as stop. It is not the Lab's 485-trade slot of section 5.1.

**The whole grid.**

| Group | Structures with enough trades | Positive in TUNE / CHOOSE / TEST | Positive in all three | Median, all five years |
|---|---|---|---|---|
| Calls, same day | 2,960 | 34% / 65% / 14% | 119 (4.0%) | −0.419 |
| Puts, same day | 2,984 | 21% / 39% / 37% | 85 (2.8%) | −0.981 |
| Calls, carried | 2,893 | 89% / 78% / 6% | 4.3% | +1.318 |
| Calls, carried, 60m line | | 93% / 80% / 24% | 20% | +3.784 |
| Puts, carried | | | | −3.258 |

- Same-day calls. A fresh turn helps: the median structure makes +0.157% when the flip is at
  most 2 bars old and −0.697% with one entry a session at any age. `vol_c` is the only filter
  with a positive median (+0.033%). The ADX filters are the worst (`adx_a` −1.160%, `adx_b`
  −2.230%).
- Carried calls. The median is +1.876% with an exit on a close and +0.991% with a touch.

**Selection.** Of the 14,976 structures, 11,897 have enough trades, 4,388 of those are positive
in TUNE, 2,887 of those in CHOOSE, and 2,511 are eligible. 2,459 of the eligible beat the
benchmark. 254 of the eligible (10%) are positive in TEST, against 36% of the 9,956 that are
not eligible. The median TEST result of the eligible is −2.794%, and of the top 20 −2.095%.

| Eligible group | Structures | Positive in TEST | Median TEST |
|---|---|---|---|
| Calls, same day | 542 | 14% | −1.269 |
| Puts, same day | 104 | 53% | +0.250 |
| Calls, carried | 1,843 | 6% | −3.869 |
| Puts, carried | 22 | 59% | +1.362 |

**The finalists.**

| Name | Structure | Trades | TUNE | CHOOSE | TEST | All five years |
|---|---|---|---|---|---|---|
| `carry30` | Calls, 30m, any age with one entry a session, no filter, all six agree, 30m line, exit on a close, carried. Top of the ranking, and the owner's rule in carried form | 1,041 | +3.611 (t 1.9) | +11.764 (t 1.9) | −2.158 (t −0.6) | +4.097 (t 2.3) |
| `carry60` | Calls, 60m, any age with one entry a trend, no filter, all six agree, 60m line by `touch`, carried | 715 | +5.086 (t 1.8) | +13.948 (t 1.9) | −1.430 (t −0.2) | +5.770 (t 2.3) |
| `intra_volc` | Calls, 30m, turn at most 4 bars old, `vol_c`, all six agree, 1d line, entries to 13:00, same day. In effect it holds to the close | 204 | +2.692 (t 1.5) | +5.162 (t 1.4) | +2.932 (t 1.1) | +3.066 (t 2.2) |
| `intra_15adx` | Calls, 15m, turn at most 2 bars old, `adx_a`, no gate, 15m line by `touch`, entries to 13:00, same day. Top eligible same-day structure | 116 | +3.643 (t 1.6) | +5.729 (t 1.7) | −6.707 (t −2.3) | +2.308 (t 1.3) |
| `carry15_volc` | Calls, 15m, turn at most 2 bars old, `vol_c`, all six agree, 60m line, exit on a close, carried. Positive in all three periods, but found with the TEST year in view | 170 | +7.049 (t 1.3) | +34.302 (t 1.6) | +18.421 (t 0.9) | +15.469 (t 2.2) |

`carry30` is the same structure as the row "30m, exit on a close" of the carried table in
section 6.3, and a trade lasts 1.4 sessions on average. It is ahead of the no-signal call held
one session by 4.5, 12.3 and 2.0 points in the three periods, and ahead of the call held two
sessions by 3.8, 12.1 and 5.2 points. In absolute terms it is negative in TEST.

**Reading.** The strategies' own filters add little, and the ADX family hurts on index options.
What earns in the model is exposure to a trend: calls carried behind the 30m or the 60m line
while the slower timeframes agree. That beat the no-signal benchmark in all three periods and
lost money in the flat year. A t of 2.3 is the best of about 15,000 structures, so it is biased
by the selection, and the premiums are modelled.

## 8. The finalists under stress

`scripts/mtf_robust.py` runs each finalist again with one thing changed: costs × 1.5 and × 2;
the entry one and two minutes late; each of the three ways of acting on the line; the VIX of
the day before (options); six other Supertrend settings; and the no-signal benchmark of the
nearest holding time. 23 instruments took 209 seconds.

### 8.1 The stress part of the candidate rule

The test of section 3.6 uses TUNE and CHOOSE only. TEST is shown beside it.

| Finalist | Base positive | Beats the benchmark | Costs × 1.5 | Entry 1 minute late | Yesterday's VIX | Stress part | TEST |
|---|---|---|---|---|---|---|---|
| `carry30` | yes | yes | yes | yes | yes | Passes | −2.158 |
| `carry60` | yes | yes | yes | yes | yes | Passes | −1.430 |
| `carry15_volc` | yes | yes | yes | yes | yes | Passes | +18.421 |
| `intra_volc` | yes | yes | yes | yes | yes | Passes | +2.932 |
| `intra_15adx` | yes | yes | yes | yes | yes | Passes | −6.707 |
| `slot_vol` (index options) | yes | yes | yes | yes | yes | Passes | −0.694 |
| `owner_intra`: the owner's rule, calls, same day, 30m line, exit on a close | no | yes | no | no | no | Fails | −1.136 |
| `owner_puts`: the owner's rule, puts, carried, 30m line, exit on a close | no | no | no | no | no | Fails | −2.808 |
| `carry60_adx` (stocks) | yes | yes | yes | yes | | Passes | +0.208 |
| `slot_adx` (stocks): short, 30m, exit on a close | no | yes | no | no | | Fails | +0.056 |
| `slot_vol` (stocks): short, 60m | no | yes | no | no | | Fails | −0.054 |
| `owner_long` (stocks): the owner's rule, long, same day | no | yes | no | no | | Fails | −0.178 |
| `owner_short` (stocks): the owner's rule, short, same day | no | no | no | no | | Fails | −0.162 |

Six option structures pass. A pass says the result does not come from the level of the costs,
from one minute of delay or from reading the VIX of the same day. It says nothing about the
next year: four of the six are negative in TEST. `carry60_adx` passes and is then removed by
the depository charge (section 7.2).

### 8.2 `carry30` under stress

| Run | Trades | TUNE | CHOOSE | TEST | All five years | Net ₹ at one lot |
|---|---|---|---|---|---|---|
| Base | 1,041 | +3.611 | +11.764 | −2.158 | +4.097 (t 2.3) | 982,412 |
| Costs × 1.5 | 1,041 | +3.417 | +11.586 | −2.326 | +3.911 (t 2.2) | 927,707 |
| Costs × 2 | 1,041 | +3.224 | +11.408 | −2.494 | +3.725 (t 2.1) | 873,002 |
| Entry 1 minute late | 1,041 | +3.489 | +11.172 | −2.390 | +3.869 (t 2.2) | 932,946 |
| Entry 2 minutes late | 1,041 | +3.609 | +11.443 | −2.392 | +3.994 (t 2.2) | 968,030 |
| The line as a resting stop (`touch`) | 1,291 | +1.561 | +6.936 | −1.510 | +2.080 (t 1.7) | 626,327 |
| The line as an alert, exit the next minute (`alert`) | 1,291 | +1.681 | +7.483 | −1.301 | +2.297 (t 1.9) | 685,245 |
| Yesterday's VIX | 1,041 | +2.851 | +9.450 | −3.720 | +2.907 (t 1.7) | 644,810 |
| Supertrend(10, 1.7) | 1,033 | +3.371 | +11.618 | −2.355 | +3.829 (t 2.1) | 929,029 |
| Supertrend(21, 1.7) | 1,031 | +4.113 | +11.090 | −1.970 | +4.357 (t 2.5) | 1,108,383 |
| Supertrend(14, 1.4) | 1,252 | +1.351 | +11.574 | −0.945 | +2.729 (t 1.8) | 927,933 |
| Supertrend(14, 2.0) | 869 | +6.040 | +8.247 | −3.018 | +4.811 (t 2.1) | 959,580 |
| Supertrend(14, 2.5) | 740 | +2.704 | +5.896 | −5.269 | +1.739 (t 0.6) | 182,930 |
| Supertrend(10, 3.0) | 603 | +2.982 | +6.812 | −7.799 | +1.144 (t 0.4) | 187,349 |
| No signal: call at 09:30, held 1 session | 6,127 | −0.904 | −0.518 | −4.159 | −1.480 (t −1.5) | −1,639,778 |

- The way the line is acted on matters most. Waiting for a close beyond the line gives +4.097%
  a trade. A resting stop at the line gives +2.080%, and the desk's alert with an exit a minute
  later +2.297%. A stop that sits at the line itself costs about half of the modelled result.
- Reading the VIX of the same day was worth 1.2 points a trade.
- Settings near (14, 1.7) give similar results. Wide lines (multipliers 2.5 and 3.0) give much
  less, and TEST is negative for every setting.

### 8.3 `carry30` in depth

| Index | Trades | All five years | TEST | Net ₹ |
|---|---|---|---|---|
| BANKNIFTY | 207 | +3.27 | −3.97 | 135,654 |
| NIFTY | 208 | +3.72 | +0.00 | 181,304 |
| FINNIFTY | 187 | +5.13 | +0.60 | 219,752 |
| MIDCPNIFTY | 228 | +5.14 | −5.55 | 285,011 |
| SENSEX | 211 | +3.25 | −0.10 | 160,692 |

| How long the trade lasted | Trades | % a trade |
|---|---|---|
| Ended the same day | 243 | −16.63 |
| 1 session | 428 | −6.29 |
| 2 or 3 sessions | 315 | +17.00 |
| 4 sessions or more | 55 | +102.60 |

- By year: 191 trades +4.958% (₹282,287); 232 trades +3.288% (₹86,076); 240 trades +2.850%
  (₹163,083); 193 trades +11.764% (₹508,205); 185 trades −2.158% (−₹57,239).
- One trade: worst −64.8%, median −7.54%, 95th percentile +76.9%, best +405.5%. The 381 winners
  (36.6%) average +38.68% and the 660 losers −15.87%, a payoff ratio of 2.44.
- Concentration. The best 52 trades (5%) earn ₹1,979,561, twice the total. Without the best 5
  trades the mean is +2.508%, without the best 1% it is +1.560%, and without the best 5% it is
  −3.152%.
- At one lot for each signal: the deepest fall is ₹358,903 (23 October 2025 to 28 October 2026),
  379 days pass below an earlier high, 29 of 59 months are positive, and the median month is
  −₹2,863. The worst month is April 2026 (−₹104,344) and the best April 2025 (+₹309,540). Up to
  20 trades lose in a row.
- By entry time: 09:30 to 10:29, 574 trades +3.15%; 10:30 to 11:29, 129 trades +4.11%; 11:30 to
  12:59, 141 trades +4.66%; 13:00 to 14:45, 197 trades +6.45%.

The whole result is the 370 trades that lasted two sessions or more. That is the shape the
owner asked for, a trail that lets the long moves run. It is also the part that the real
premiums could not confirm (section 9).

### 8.4 `intra_volc` under stress and in depth

| Run | Trades | TUNE | CHOOSE | TEST | All five years | Net ₹ at one lot |
|---|---|---|---|---|---|---|
| Base | 204 | +2.692 | +5.162 | +2.932 | +3.066 (t 2.2) | 131,766 |
| Costs × 1.5 | 204 | +2.496 | +4.977 | +2.758 | +2.876 (t 2.0) | 121,325 |
| Costs × 2 | 204 | +2.299 | +4.791 | +2.585 | +2.685 (t 1.9) | 110,884 |
| Entry 1 minute late | 204 | +2.722 | +4.456 | +2.574 | +2.922 (t 2.0) | 123,536 |
| Entry 2 minutes late | 204 | +2.747 | +4.518 | +2.951 | +3.021 (t 2.1) | 131,534 |
| Yesterday's VIX | 204 | +2.627 | +4.795 | +2.815 | +2.951 (t 2.1) | 131,037 |
| Supertrend(10, 1.7) | 216 | +2.307 | +4.802 | +2.388 | +2.669 (t 2.1) | 130,088 |
| Supertrend(21, 1.7) | 194 | +2.759 | +4.144 | +2.064 | +2.834 (t 2.0) | 115,232 |
| Supertrend(14, 1.4) | 271 | +2.651 | +2.995 | −0.008 | +2.176 (t 1.8) | 120,484 |
| Supertrend(14, 2.0) | 155 | +2.839 | +6.573 | +2.652 | +3.386 (t 2.0) | 121,340 |
| Supertrend(14, 2.5) | 131 | +0.302 | +4.201 | −0.044 | +1.104 (t 0.6) | 44,706 |
| Supertrend(10, 3.0) | 109 | +0.708 | +6.447 | −1.480 | +1.199 (t 0.5) | 31,332 |
| No signal: call at 09:30, sold at the close | 6,132 | −1.185 | −0.980 | −1.815 | −1.271 (t −2.1) | −2,103,100 |

The three ways of acting on the line give the same trades here. The daily line is never reached
within the session, so every trade ends at the session close and the trail does nothing.

- By year: 41 trades −0.273% (−₹1,019); 44 trades +7.101% (₹53,029); 52 trades +1.299% (−₹81);
  27 trades +5.162% (₹35,895); 40 trades +2.932% (₹43,942).
- By index: BANKNIFTY 37 trades −1.82%; NIFTY 47 trades +4.14%; FINNIFTY 38 trades +5.60%;
  MIDCPNIFTY 39 trades +1.62%; SENSEX 43 trades +5.18%. Four of the five are positive.
- One trade: worst −42.1%, median +2.86%, best +76.7%. The 121 winners (59.3%) average +12.31%
  and the 83 losers −10.41%, a payoff ratio of 1.18.
- Without the best 5% of the trades the mean is +0.822%.
- At one lot for each signal: the deepest fall is ₹44,981 (2 August to 27 October 2022), 523
  days pass below an earlier high, 25 of 46 months are positive, and at most 6 trades lose in
  a row.

## 9. Real premiums

`scripts/mtf_real_premiums.py` prices the same trades twice: with the model (Black-76 at India
VIX × 1.15, a monthly contract) and with the premiums that traded, from Dhan's one-minute bars
of expired index options. The entry minute, the exit minute and the strike are the same, and so
are the half-spread and the charges.

A trade is priced when its strike traded within 3 minutes of the entry and of the exit. Dhan
serves the near and the next monthly series, and in each minute only the strikes up to 10 steps
from the money. A carried trade also needs the contract of the exit day; whether an expiry fell
between the entry and the exit is read from the market's own estimate of the days left. Trades
that cannot be priced are counted, with the model's result for them.

### 9.1 The four finalists

| Finalist | Trades | Priced | Model, all trades | Model, priced trades | Real, priced trades | Real minus model |
|---|---|---|---|---|---|---|
| `carry30` | 1,041 | 423 (41%) | +4.097 (t 2.3) | −8.225 (t −6.4) | −9.140 (t −8.0) | −0.916 |
| `carry60` | 715 | 281 (39%) | +5.770 (t 2.3) | −9.797 (t −6.2) | −8.857 (t −6.6) | +0.940 |
| `intra_volc` | 204 | 110 (54%) | +3.066 (t 2.2) | +2.558 (t 1.2) | +0.737 (t 0.3) | −1.820 |
| `slot_vol` | 948 | 544 (57%) | +0.875 (t 1.0) | −1.754 (t −1.9) | −2.093 (t −2.3) | −0.338 |

On the same trades the model and the market differ by −1.8 to +0.9 points a trade. The median
ratio of the real entry premium to the modelled one is 1.06 to 1.08. So the model is not far
off trade by trade. The problem is which trades can be priced.

### 9.2 `carry30`

| Group | Trades | Priced | Model, all trades | Model, priced trades | Real, priced trades |
|---|---|---|---|---|---|
| TUNE | 663 | 240 | +3.611 | −6.186 | −7.574 (t −5.0) |
| CHOOSE | 193 | 92 | +11.764 | −10.485 | −11.022 (t −4.8) |
| TEST | 185 | 91 | −2.158 | −11.315 | −11.369 (t −4.5) |
| NIFTY | 208 | 178 | +3.715 | −4.776 | −5.770 |
| BANKNIFTY | 207 | 164 | +3.268 | −9.252 | −10.226 |
| FINNIFTY | 187 | 25 | +5.126 | −15.231 | −16.614 |
| MIDCPNIFTY | 228 | 43 | +5.138 | −11.553 | −11.016 |
| SENSEX | 211 | 13 | +3.249 | −18.002 | −21.023 |
| NIFTY and BANKNIFTY together | 415 | 342 | +3.492 | −6.922 | −7.907 (t −6.8) |
| Ended the same session | 243 | 139 | −16.629 | −17.653 | −17.270 |
| Held 1 session | 428 | 178 | −6.292 | −9.093 | −9.630 |
| Held 2 sessions or more | 370 | 106 | +29.727 (t 7.3) | +5.597 (t 1.6) | +2.342 (t 0.9) |

22.7% of the priced trades win, with real and with modelled premiums alike.

| Why a trade could not be priced | Trades | The model's result for them |
|---|---|---|
| The call did not trade within 3 minutes of the entry | 302 | +2.889 |
| The days to expiry at the entry are unknown (carried trade) | 137 | +17.941 |
| The call was more than 10 strikes from the money at the entry | 102 | +1.471 |
| The call was more than 10 strikes from the money at the exit | 65 | +61.594 |
| Cannot tell whether the near contract expired before the exit | 6 | +35.293 |
| The call did not trade within 3 minutes of the exit | 4 | −14.778 |
| The near contract expired before the model's exit | 2 | +53.660 |

The 423 priced trades are the losing part of the modelled result (−8.2% in the model as well),
and the market confirms that part. The winners are the trades that cannot be priced: a call
that has moved more than 10 strikes into the money is no longer in the stored data, and those
65 trades alone average +61.6% in the model. Even on NIFTY and BANKNIFTY, where 82% of the
trades are priced, the real result is −7.9% a trade. Among the trades held two sessions or
more, the 106 that are priced make +2.3% (t = 0.9) against +29.7% modelled for all 370.

### 9.3 `intra_volc`, `carry60` and `slot_vol`

- `intra_volc`. Real results by period: +1.636 / −1.408 / +0.176. NIFTY and BANKNIFTY together
  (79 of 84 trades priced): −0.996% (t = −0.7). 44.5% of the priced trades win on real premiums
  against 57.3% in the model. Not priced: 64 with no trade near the entry, 19 with an unknown
  expiry, 8 beyond the stored strikes at the exit (+24.112% in the model) and 3 others.
- `carry60`. Held two sessions or more (99 of 339 priced): +26.276% modelled for all, −1.241%
  modelled for the priced, −0.226% real. Not priced: 187 with no trade near the entry, 108 with
  an unknown expiry (+17.577% in the model), 73 beyond the stored strikes at the entry and 51
  beyond them at the exit (+71.802%).
- `slot_vol`. Real results by period: −0.928 / −2.789 / −3.766 (t = −2.4 in TEST). By index:
  NIFTY −0.425 (170 of 184 priced), BANKNIFTY −2.561 (192 of 214), FINNIFTY −1.787 (75 of 192),
  MIDCPNIFTY −3.977 (76 of 170), SENSEX −4.455 (31 of 188). 37.5% of the priced trades win on
  real premiums against 40.8% in the model.
- The running slot with its own settings (section 5.1) was priced in the study of 8 November
  ([REAL_OPTIONS_2026-11-08.md](REAL_OPTIONS_2026-11-08.md)), on the bars as stored then: 265 of
  488 trades priced (54%), real +1.917 / −1.673 / −3.059, and −0.139% over all five years
  (t = −0.1) against +0.779% in the model for the same trades. NIFTY and BANKNIFTY together (176
  of 194 priced): +0.943% (t = 0.7). The other three indices (89 of 294 priced): −2.281%.

### 9.4 What this check can and cannot say

- No finalist is confirmed by real premiums. The same-day finalist is at zero (+0.74%, t = 0.3)
  and the running slot's structure is negative (−2.09%, t = −2.3).
- The carried finalists are not disproved either. Their modelled profit sits in a few very large
  winners, and the stored data cannot price exactly those trades.
- So the third condition of section 3.6 is not met by any structure, and nothing in this study
  is called a paper-test candidate.

## 10. Through the product code

The grids of sections 6 and 7 ran in the research harness, which decides every minute and lets
trades overlap. The product decides at the close of each 5-minute bar, holds one position for
each symbol and ends every trade at the session close. This section runs the rule through the
product's own code: first the Lab's backtest engine, then the desk's tracker and the paper
book's rules.

### 10.1 The strategy `supertrend_mtf` in the Lab

`scripts/mtf_lab_check.py` runs the registered strategy `supertrend_mtf` through the Lab's
`run_backtest` on the five index options and the 18 stocks, over the five years to 7 November
2026, with the costs and the modelled premiums of section 3.4.

Two rules are run:

- **The owner's rule.** All six timeframes agree, the 30m trend is the trade's trend, there is
  one entry for each trend in every session, and entries run from 09:30 to 14:45.
- **The fresh turn with ADX and RSI.** All six agree, the 30m trend turned at most 4 bars ago,
  ADX(14) of the 30m bars is at least 25 and RSI(14) at least 60, entries run to 13:00, and
  there is one trade a day at most. This is `intra_volc` of section 7.3 with the line named in
  each row.

And two ways of acting on the line:

- `touch`: the line itself is a resting stop, and the first touch ends the trade.
- "close + N ATR": the trade ends at the open of the next bar once a bar of the line's timeframe
  has closed beyond the line. The stop itself rests N ATR of that timeframe beyond the line, as
  a backstop for a fast move.

The last column is the same structure in the stage-2 grid of the harness. Its `close` has no
backstop, so one harness figure stands beside the three backstops.

| Rule, side, line, way of acting | Trades | TUNE | CHOOSE | TEST | All five years | Winners | Net ₹ at one unit | Harness: trades, all five years |
|---|---|---|---|---|---|---|---|---|
| Owner's rule, calls, 30m line, `touch` | 2,023 | −0.697 | +1.229 | −1.005 | −0.358 (t −0.64) | 41.3% | −349,883 | 2,027, −0.370 |
| Owner's rule, calls, 30m line, close + 1 ATR | 2,023 | −0.852 | +0.949 | −0.984 | −0.512 (t −0.86) | 42.9% | −476,733 | 2,027, −0.541 |
| Owner's rule, calls, 60m line, close + 1 ATR | 1,987 | −0.392 | +0.975 | −1.192 | −0.240 (t −0.37) | 45.5% | −347,431 | 1,990, −0.189 |
| Owner's rule, puts, 30m line, close + 1 ATR | 1,291 | −1.060 | +0.456 | −2.775 | −1.188 (t −1.14) | 42.0% | −771,722 | 1,306, −1.168 |
| Fresh turn with ADX and RSI, calls, 60m line, `touch` | 202 | +0.048 | +5.489 | +2.688 | +1.271 (t 0.93) | 56.9% | 66,721 | 204, +1.181 |
| The same, 60m line, close + 1 ATR | 202 | +0.999 | +4.924 | +2.931 | +1.887 (t 1.40) | 57.9% | 83,104 | 204, +2.779 |
| The same, 60m line, close + 2 ATR: **the paper slot** | 202 | +2.119 | +4.924 | +2.931 | +2.641 (t 1.79) | 58.9% | 106,385 | 204, +2.779 |
| The same, 60m line, close + 3 ATR | 202 | +2.246 | +4.924 | +2.931 | +2.727 (t 1.86) | 58.9% | 113,500 | 204, +2.779 |
| The same, 30m line, close + 1 ATR | 202 | −0.284 | +4.425 | +2.789 | +0.931 (t 0.65) | 53.5% | 43,174 | 204, +0.850 |
| The same, 1d line, close + 1 ATR | 169 | +2.837 | +4.608 | −0.881 | +2.418 (t 1.52) | 58.0% | 72,053 | 204, +3.066 |
| Owner's rule, stocks, long, 30m line, close + 1 ATR | 5,884 | −0.139 | −0.107 | −0.188 | −0.141 (t −8.08) | 38.1% | −14,309 | 5,923, −0.131 |
| Owner's rule, stocks, short, 30m line, close + 1 ATR | 4,909 | −0.138 | −0.172 | −0.162 | −0.151 (t −6.10) | 40.4% | −16,819 | 4,983, −0.157 |

The paper slot's row in detail. It has 136 trades in TUNE (t = 1.11), 26 in CHOOSE (t = 1.26)
and 40 in TEST (t = 1.14). The median trade makes +2.822% and the profit factor is 1.42. 95% of
the trades end at the session close, 4% on a 60m close beyond the line and 1% at the backstop.
By index: BANKNIFTY −1.81% (37 trades), FINNIFTY +5.63% (38), MIDCPNIFTY +0.89% (38), NIFTY
+3.24% (46) and SENSEX +4.73% (43).

**Reading.**

- The Lab and the harness agree on the owner's rule. The trade counts are within 1.5% of each
  other and the five-year means within 0.06 points, for calls, for puts and for both sides of
  the stocks. The rule loses on the same day in every row.
- The fresh-turn form keeps its sign. The Lab makes 202 trades against 204, and behind the 60m
  line its 40 TEST trades make +2.931% against +2.932%.
- The daily line cannot be the stop in the product. The Lab skips a setup whose stop stands
  more than 5% of the price away (`max_risk_pct`). With the daily line that removes 33 of the
  202 entries, 28 of them on BANKNIFTY, FINNIFTY and MIDCPNIFTY, and in the 169 trades that
  remain the line is never reached: every trade ends at the session close. So the slot follows
  the 60m line.
- An exit on a close is better than a touch: +1.887% against +1.271% behind the 60m line. The
  harness shows the same order (+2.779% against +1.181%).
- The backstops of 1, 2 and 3 ATR differ only in TUNE (+0.999%, +2.119% and +2.246%), and the
  result moves toward the harness's +2.779% as the backstop stands further away. The default of
  the strategy is 2 ATR.
- These three choices (the 60m line, the exit on a close, the backstop of 2 ATR) were made on
  this table with all three periods in view. They are design choices and not the result of a
  test.

### 10.2 The desk's tracker and the paper book's rules

A backtest engine and a live desk can disagree even when they share the strategy code. The desk
builds its bars minute by minute, and the paper book fills and manages a trade with its own
rules. `scripts/mtf_desk_replay.py` feeds the stored one-minute bars of the five indices,
minute by minute, through the desk's `InstrumentTracker` (5-minute slots, 25 days of bars at
the start of each day, the stored daily history) and through the paper book's `decide_fill` and
`manage_step`. Each trade is then compared with the Lab's trade.

The windows are 3 to 25 August 2026 and 27 October to 6 November 2026. A trade matches when the
replay fills in the Lab's entry bar, ends in the Lab's exit bar and ends for the same reason.
Two timings are replayed:

- `lab`: the alert is stamped a second before its bar closes, so the fill is the open of the
  very next minute. That is the Lab's fill.
- `desk`: the alert is stamped at the poll, five seconds after the bar closes, so the fill is
  one minute later. That is what the running desk does.

| Setting | Timing | Lab trades | Replay trades | Matched | Exits in the replay | Stop moves along the line |
|---|---|---|---|---|---|---|
| The paper slot (fresh turn with ADX and RSI, 60m line, close + 2 ATR) | `lab` | 10 | 10 | 10 | 10 at the session close | 52 |
| The same | `desk` | 10 | 10 | 10 | 10 at the session close | 52 |
| The owner's rule, calls, 30m line, `touch` | `lab` | 57 | 57 | 57 | 33 at the session close, 19 at a trailed stop, 5 at the first stop | 245 |
| The same | `desk` | 57 | 57 | 57 | 33, 19 and 5 | 245 |

Every trade matches the Lab. No entry is missing, none is extra, and no trade has the Lab's
entry and another exit. In the 57 trades of the owner's rule the stop moved 245 times along the
line, and 19 trades ended at a stop that the line had moved.

What the replay leaves out:

- `Desk.poll` itself, which reads the wall clock in many places, and the `PaperBook` class. The
  replay calls the functions that they call.
- The option chain and the premiums. The levels are the index's, which is what the stop and the
  trail act on.
- The rule of one live idea for each instrument. Each setting keeps its own book.

Two findings of the review follow from this. No test runs an alert from `Desk.poll()` into a
real `PaperBook` (finding T-1, story US-044). And the replay is a script that was run once on
two windows. It reads the 12 GB store and a saved run of the Lab check, so it is not part of
the gate (finding T-2).

## 11. What went into the product

| Part | Where | What it does |
|---|---|---|
| The ladder of trends | `src/onelot/indicators/mtf.py` | Supertrend(14, 1.7) on 1m, 5m, 15m, 30m, 60m and 1d bars, read from closed bars only, after 70 bars of warm-up |
| The strategy | `src/onelot/strategies/supertrend_mtf.py`, `mtf_support.py` | The rules of section 10.1. Options: which timeframes must agree, the entry timeframe, the age of the turn, ADX, RSI and relative volume, whose line is followed, the backstop, the exit on a close |
| The line as stop and trail in the Lab | `src/onelot/lab/simulator.py`, `runner.py` | The runner hands the strategy the whole one-minute history. The simulator reads the strategy's columns `long_trail` and `short_trail`. The stop moves only in the trade's favour, and a new level is in force from the next bar |
| The line as stop and trail in the paper book | `src/onelot/paper/manage.py`, `book.py`, `position.py` | The same rule for an open paper trade. Every move is recorded as a `STOP_MOVED` event |
| The desk | `src/onelot/desk/tracker.py`, `desk.py` | The tracker computes the six trends and the line of every instrument. The desk hands the line to the paper book through `follow_trail` |
| The earlier strategies | `src/onelot/strategies/supertrend_vol.py`, `supertrend_adx.py` | An option `line_trail`, off by default, so the three earlier slots trade as before |
| The repairs of section 2.2 | `src/onelot/data/hygiene.py`, `src/onelot/desk/history.py` | Applied when bars are read, in the Lab and on the desk |
| The screen | `src/onelot/ui/app.js`, `styles.css` | A "Micro trend" strip of six marks on every card, a "Micro trend" table in the detail view, and a Trail column in the paper table and the slot tables |
| Tests | 13 new files under `tests/desk`, `tests/lab` and `tests/paper` | 369 tests: the ladder, the strategy, the repairs, the trail in the simulator, in the paper book and on the desk, and the screen |

**The paper slot.** `config/desk.yaml` now names three strategies in `primary_strategies`
(`supertrend_vol`, `supertrend_adx` and `supertrend_mtf`) and holds four paper slots:

| Slot | Instruments | Bars | Side |
|---|---|---|---|
| `supertrend_vol` | Index options | 30m | Calls |
| `supertrend_mtf` (new) | Index options | Decisions every 5 minutes | Calls |
| `supertrend_vol` | Stocks | 60m | Short |
| `supertrend_adx` | Stocks | 30m | Short |

The settings of the new slot: `st_period: 14`, `st_mult: 1.7`, `gate: all`, `trend_tf: 30m`,
`flip_window: 4`, `trend_adx_min: 25`, `trend_rsi_min: 60`, `entry_end: "13:00"`,
`trail_tf: 60m`, `stop_buffer_atr: 2.0`, `max_trades_per_day: 1`, calls only.

- While the new slot's paper trade is open, the other index slot is silent on that index (one
  live idea for each instrument), and the other way round.
- The plain rule (all six agree, at any time of day) is not alerted, because it lost after
  costs in every same-day form. Every card shows the six trends instead.
- To drop the slot, delete its entry and its name from `primary_strategies`.
- A desk that was started before these changes must be stopped and started again. "Reload desk
  config" does not load new code (finding F-4 of the review).
- The gate (`bash scripts/gate.sh`: the formatter, the linter, the mypy ratchet and the tests)
  passes on this tree with 7,754 tests passed and 1 skipped.

## 12. Limits

- **TEST is not clean.** It was read by the studies of 7 and 8 November, and again here for
  neighbouring structures. The bars from 8 November 2026 onward are the only untouched sample.
- **Many structures were tried** (section 3.8). The best t among about 15,000 structures for a
  basket is 2.3, which is no evidence by itself.
- **The premiums are modelled.** Black-76 at India VIX × 1.15, time decay by calendar time, a
  fixed half-spread, and the VIX of the same day. With the VIX of the day before, `carry30`
  falls from +4.097% to +2.907% a trade.
- **Real premiums cover 39% to 57% of the trades**, and the trades that cannot be priced are the
  modelled winners. For `carry30` only 81 of the 626 trades on FINNIFTY, MIDCPNIFTY and SENSEX
  could be priced (finding S-3 of the review).
- **The product holds a trade for one session at most.** The carried structures cannot be paper
  tested until an index-option trade can be carried overnight (finding S-2, story US-041, owner
  decision Q-6).
- **The live path is not tested end to end.** No test runs from `Desk.poll` into the paper book
  (finding T-1), and the replay of section 10.2 ran once on two windows (finding T-2).
- **The cost table is unverified.** `config/costs.yaml` has not been checked against a contract
  note and has no depository charge (finding L-4, story US-021).
- **The product's session table is out of date for stocks** since 3 August 2026 (finding D-1).
  The research ends stock trades at 15:10.
- **Spreads.** The half-spreads that the Live Desk measures on FINNIFTY and SENSEX contracts
  are far above the model's 0.6% (finding S-6).
- **The paper test will be slow.** The slot `supertrend_mtf` made 202 trades in five years on
  five indices, about 40 a year, so 30 closed trades take about nine months. Finding S-7 says
  what a review at 30 trades can and cannot conclude.
- **One live day is no sample.** On 11 November 2026 the running index slot opened two paper
  trades and both were stopped out: −₹20,579 net for the two lots.

## Reproduce

Every script reads the store `data/onelot.db` and adds `src` to its own import path. The
outputs go to `data/research/mtf_2026-11-11/`, which git ignores. `NOTES.md` in that folder is
the running log, with each rule as it was written before its run. The real-premium run asks
Dhan for the one-minute bars of expired options with the Data API token of `.env` and caches
them. With `--offline` it uses the cache only.

```bash
# sections 4 and 5
.venv/bin/python scripts/bruteforce_analysis.py > data/research/mtf_2026-11-11/bruteforce_analysis.txt
.venv/bin/python scripts/deep_backtest_candidates.py --run      # about 15 minutes, 5 processes
.venv/bin/python scripts/deep_backtest_candidates.py --report

# section 6: stage 1
.venv/bin/python scripts/mtf_bruteforce.py --validate
.venv/bin/python scripts/mtf_bruteforce.py --run
.venv/bin/python scripts/mtf_bruteforce.py --report

# section 7: stage 2
.venv/bin/python scripts/mtf_stage2.py --validate
.venv/bin/python scripts/mtf_stage2.py --run
.venv/bin/python scripts/mtf_stage2.py --report

# section 8: the finalists under stress
.venv/bin/python scripts/mtf_robust.py --run [--workers 4]
.venv/bin/python scripts/mtf_robust.py --report

# section 9: real premiums
.venv/bin/python scripts/mtf_real_premiums.py --run [--keys carry30,intra_volc] [--rate 2.5]
.venv/bin/python scripts/mtf_real_premiums.py --run --offline --limit 20   # cache only, saves nothing
.venv/bin/python scripts/mtf_real_premiums.py --report

# section 10: the product code
.venv/bin/python scripts/mtf_lab_check.py --run [--workers 5] [--only INDEX_OPTIONS]
.venv/bin/python scripts/mtf_lab_check.py --report
.venv/bin/python scripts/mtf_desk_replay.py --run [--workers 5]   # needs the Lab check's run
.venv/bin/python scripts/mtf_desk_replay.py --report

# the full gate, about 9 minutes
bash scripts/gate.sh
```

`mtf_stage2.py` imports `mtf_bruteforce.py`; `mtf_robust.py` and `mtf_real_premiums.py` import
both; `mtf_lab_check.py` imports `deep_backtest_candidates.py`.
