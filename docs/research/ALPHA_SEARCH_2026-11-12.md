# The search for an edge: intraday index options and delivery stocks (2026-11-12)

The owner's rules of 12 November 2026: options are intraday only (calls or puts, out the same
day); stocks may be delivery, long only, held as long as the strategy demands; one unit a
trade. The task: find a strategy with a real edge for each, and prove it, or say that none was
found.

The plan was written before the results were read and is kept beside this file:
[ALPHA_PLAN_2026-11-12.md](ALPHA_PLAN_2026-11-12.md). All figures are after charges unless a
line says otherwise. They are backtest evidence about rules, not advice.

## 1. The answer in short

| Question | Answer |
|---|---|
| Intraday index options, buying only | No edge found. Six kinds of signal and about thirty conditional cuts were tested on five indices. The best lifts the index by 2 to 3 basis points a trade, which is far less than an option buyer's costs |
| Why same-day buying keeps failing | The market's gain is made overnight. Over five years the indices made +17.7% a year between the close and the next open and lost 9.1% a year inside the session |
| Delivery stocks, trend and breakout rules | Not an edge. Buying the 55-session high earns less than holding the same stock for the same number of days in both samples: −1.18% a trade (t = −2.2) in 2011 to 2021 and −0.74% (t = −1.2) in 2021 to 2026 |
| Delivery stocks, buying a pullback | One rule gave the same positive result in two separate samples: +1.19% a trade (t = 3.2) in 2011 to 2021 and +1.36% (t = 2.9) in 2021 to 2026, with 69% and 75% winners. Over a like-for-like hold it adds +0.75% (t = 1.9) and +1.15% (t = 2.4) |
| Does it hold on other stocks? | Partly. Unchanged, on 43 stocks that are not on the desk (30 other large companies and 13 that later left the NIFTY 50), it makes +0.78% a trade (t = 2.7) with 69% winners. Its excess over a like-for-like hold is +0.50% (t = 1.7) |
| Is that proven alpha? | No. It is the strongest stock result found, it agrees with a well-known effect, and it repeats in an older sample and on other stocks. But about 45 stock rules were tried, the edge over simply holding is small (+0.5% to +0.8% a trade), about one year in three is negative, and 2015 to 2018 shows nothing. The paper test must confirm it |
| The one-share rule | At one share the depository charge of ₹14.75 a sale is larger than the rule's average profit for 8 of the 18 stocks and takes half or more of it for 6 others. A delivery rule needs a trade value where that charge is small |

## 2. Data and method

- **Samples.** Indices and stocks: the cleaned one-minute and daily bars of the store,
  2021-11-08 to 2026-11-06 (TUNE to 2024-11-07, CHOOSE to 2025-11-07, TEST after). Stocks
  only: an older sample of daily bars from Yahoo Finance, 2010 to March 2022, used for
  entries from 2011-01-01 to 2021-11-07. On the months both sources share, the ratio of the
  two closes is constant within 0.2% for every stock. Nobody in this project had tested
  anything on the older sample.
- **Stocks.** 18 stocks (Adani Green excluded). The signal is read on a daily close and the
  trade is made at the next open, 0.03% slippage a side, delivery charges from the cost
  configuration. The depository charge is shown separately at today's share prices.
- **Options.** Signals are tested first on the index itself, in basis points (bp) of the
  index, signed for the side of the trade. Option results use the same model as the earlier
  research (Black-76 at India VIX × 1.15, monthly contract, one lot, half-spread 0.6%, the
  product's charges). No option rule reached the stage of real premiums.
- **Statistics.** A t-value treats all trades of one day (options) or of one entry week
  (stocks) as one draw. Three numbers in a row are TUNE / CHOOSE / TEST.
- **Lapses, stated plainly.** The conditional cuts of section 4 were printed with the TEST
  column beside them, so TEST is not a clean check for them. The stock benchmark first used
  (mean daily return × days held) overstates a long hold, because it ignores compounding. It
  was replaced by the stock's own mean return over the same number of sessions, open to
  open, with the same charges.

## 3. Where the return comes from

Log return in % a year, 2021-11-08 to 2026-11-06, split at the open.

| | Overnight (close to next open) | Inside the session | Total |
|---|---|---|---|
| NIFTY | +15.2 | −8.4 | +6.8 |
| BANKNIFTY | +9.9 | −1.0 | +8.9 |
| FINNIFTY | +9.1 | −1.0 | +8.1 |
| MIDCPNIFTY | +41.9 | −28.5 | +13.4 |
| SENSEX | +12.7 | −6.6 | +6.0 |
| Five indices, mean | +17.7 (t = 4.1) | −9.1 (t = −1.7) | +8.6 |
| 18 stocks, mean | +21.3 (t = 5.2) | −12.7 (t = −2.2) | +8.6 |

- Overnight is positive for 5 of 5 indices and 17 of 18 stocks. The session is positive for
  none of the indices and 4 of the stocks.
- By period the overnight part of the indices is +24.0 / +13.9 / +3.1, so it has faded. For
  the stocks it is +27.0 / +13.4 / +12.0.
- The loss inside the session sits in the first half hour (−3.2 bp, t = −3.1) and is over
  within about two minutes of the open. No later half hour differs from zero.
- After a day on which a stock rose 1.5% or more, it opens higher again the next morning
  (+18 to +32 bp, t = 3.7 to 5.8) and gives part of it back in that session.
- One night is worth 8.5 bp on a stock. A delivery round trip costs about 22 bp in charges
  plus the depository charge, so the overnight gain cannot be collected night by night. It
  can only be held through.

What follows from it: a rule that buys and sells inside one session works without the
market's gain and pays charges on top. That is the common reason behind every same-day
result of this project. The owner's two rules (options out the same day; one share against
a fixed ₹14.75) keep this effect out of reach.

## 4. Intraday index options

Five indices, 1,171 to 1,232 sessions each. "Index" is the mean move in the trade's favour.

| Signal | Trades | Index, bp (t) | Winners | Option, % of premium (t) | By period, option % |
|---|---|---|---|---|---|
| Buy a call every session, 09:46 to 15:15 | 6,094 | −0.4 (t −0.2) | 50.0% | −1.15 (t −2.0) | −1.01 / −1.01 / −1.68 |
| Buy a put every session, 09:46 to 15:15 | 6,094 | +0.4 (t 0.2) | 50.0% | −0.53 (t −0.9) | −0.61 / −0.67 / −0.15 |
| Momentum beyond the usual move by that minute (k = 1.0) | 3,426 | +2.3 (t 1.1) | 53.6% | +0.13 (t 0.2) | −0.32 / +0.79 / +0.69 |
| The same, k = 2.0 | 1,331 | +2.8 (t 0.8) | 53.6% | +0.40 (t 0.4) | +0.12 / +1.34 / +0.31 |
| First half hour decides the last half hour (15:00 to 15:25) | 6,094 | +0.3 (t 0.6) | 50.3% | −1.46 (t −10.6) | −1.47 / −1.52 / −1.40 |
| Fade the opening gap from 09:17 (gap of 1 spread or more) | 1,496 | +0.5 (t 0.3) | 49.7% | −0.89 (t −1.6) | −0.86 / −0.03 / −1.73 |
| Fade yesterday's large move from 09:20 to 15:15 | 789 | +12.7 (t 1.9) | 53.9% | +2.29 (t 1.3) | +4.34 / −3.37 / +2.39 |

The momentum signal is the published "noise area" idea: act when the index has moved from
its open (or from the previous close, whichever is further) by more than its average move at
that minute over the last 14 sessions. It was then cut about thirty ways (PLAN, A4 and A5):

- **Big-money confirmation** from the heavyweight stocks. Relative volume of 1.2 or more
  lifts the index move to +6 to +8 bp on NIFTY and BANKNIFTY, and under 0.9 it is −3 to
  −8 bp. Breadth and signed volume sort nothing.
- **Compression.** After the narrowest daily range of seven sessions the move is +8.1 bp
  (t = 2.2), and +14.5 bp (t = 2.6) at k = 2.0, positive in all three periods. But across the
  whole range of compression the result does not rise step by step, so this reads as chance.
- **Weekday.** Fridays +9.1 bp (t = 2.3), Thursdays −5.4 bp. No reason was stated in advance.
- **A score.** A linear score of all the features, fitted on TUNE only, did not sort CHOOSE
  or TEST: its best third made +1.2 bp and +3.2 bp, its worst third +1.0 bp and +6.2 bp.

**Verdict.** An option buyer pays about 1.2% to 1.6% of the premium a round trip and loses
time value on top. That needs 5 to 10 bp of index move in the trade's favour on average.
Nothing tested reaches it in a way that survives a second sample. Thirty cuts will always
show one or two at t = 2.2 to 2.6. No intraday option rule is proposed.

## 5. Delivery stocks

### 5.1 Three families of rules, two samples

Long only, daily bars, one open trade a stock and rule. "Same stock" is the excess over
holding that stock for the same number of sessions; "basket" is the excess over holding all
18 stocks over the same dates. Each cell is the older sample, then the newer one.

| Rule | Trades | % a trade | Sessions held | Excess, same stock (t) | Excess, basket (t) |
|---|---|---|---|---|---|
| Breakout: buy the 55-session high, sell the 20-session low | 539, 227 | +1.35, +0.69 | 36, 35 | −1.18 (t −2.2), −0.74 (t −1.2) | −0.50 (t −1.2), −0.35 (t −0.7) |
| The same, 20 and 10 sessions | 1,145, 532 | +0.69, +0.05 | 18, 17 | −0.31 (t −1.0), −0.36 (t −1.1) | −0.14 (t −0.7), −0.14 (t −0.6) |
| Long while the 50-day average is above the 200-day | 161, 78 | +15.92, +8.10 | 201, 186 | −2.17 (t −0.8), −4.41 (t −1.6) | +2.27 (t 0.6), +0.79 (t 0.4) |
| Momentum rotation, 12 months less 1, top 5 | 64, 30 | +25.57, +9.39 | 212, 203 | not a fair test | +9.45 (t 0.9), −0.90 (t −0.2) |
| New 252-session high on high volume | 234, 109 | +2.58, +0.61 | 44, 37 | −0.82 (t −1.2), −1.20 (t −1.3) | −0.07 (t −0.1), −0.50 (t −0.6) |
| High-volume up day, held 40 sessions | 166, 59 | +1.84, +0.65 | 30, 24 | −0.26 (t −0.3), −0.25 (t −0.2) | +0.67 (t 0.9), +0.56 (t 0.6) |
| The owner's Supertrend (14, 1.7), daily with weekly | 720, 288 | +0.87, +0.28 | 18, 16 | −0.16 (t −0.5), −0.16 (t −0.4) | +0.13 (t 0.6), −0.01 (t −0.0) |
| RSI(2) under 10 in an uptrend | 1,364, 649 | +0.04, −0.13 | 4, 4 | +0.11 (t 0.9), +0.03 (t 0.2) | +0.06 (t 0.8), +0.05 (t 0.6) |
| 10-session low in an uptrend | 1,371, 655 | +0.06, −0.08 | 6, 7 | −0.06 (t −0.3), −0.04 (t −0.2) | −0.02 (t −0.2), +0.03 (t 0.3) |
| Last week's three losers, held a week | 1,389, 637 | +0.15, −0.02 | 6, 6 | +0.11 (t 0.7), +0.10 (t 0.5) | +0.09 (t 0.9), +0.06 (t 0.5) |
| **Pullback of 8% in an uptrend** | 434, 144 | **+1.04, +1.10** | 11, 13 | +0.56 (t 1.3), +0.85 (t 2.0) | +0.73 (t 3.2), +0.41 (t 1.4) |
| The same at 6% | 805, 265 | +0.54, +0.29 | 10, 12 | +0.15 (t 0.6), +0.07 (t 0.2) | +0.33 (t 2.0), −0.10 (t −0.4) |
| The same at 10% | 228, 59 | +1.17, +1.14 | 13, 14 | +0.57 (t 0.9), +0.85 (t 1.2) | +0.93 (t 2.4), +0.72 (t 1.4) |
| Mirror of the breakout: buy its exit, sell its next entry | 530, 221 | +3.75, +2.72 | 54, 61 | +0.31 (t 0.5), +0.47 (t 0.7) | +0.16 (t 0.3), −0.09 (t −0.2) |

- Every long rule makes money in % a trade, because these 18 stocks rose (buy and hold made
  +836% and +68% in the two samples). That is the market's return, not a rule's skill. The
  two excess columns are the test of skill.
- **Buying strength does not pay.** Breakouts and new highs earn less than holding, and the
  daily Supertrend no more than holding. The 55-session breakout is the clearest: −1.18% a
  trade (t = −2.2) in the older sample and −0.74% (t = −1.2) in the newer one.
- **Short dips earn nothing extra.** A fall of a few days (RSI, a 10-session low, last week's
  losers) is followed by about the usual return, which does not cover the charges.
- **A deeper pullback does.** The 8% pullback is the only rule with a positive excess in both
  samples against both benchmarks, and against the basket it is positive in all six
  sub-periods (+1.06 / +0.51 / +0.45 and +0.33 / +1.00 / +0.46). It grows with depth: 6%,
  8%, 10% give +0.54, +1.04, +1.17 in the older sample.
- The momentum rotation is not tested fairly against "the same stock", because it picks the
  stocks that later turn out to be the sample's winners. Against the basket it shows nothing.

### 5.2 The pullback rule

In words: a stock that closes above its 200-session average falls, on closes, to 8% or more
below its highest close of the last 20 sessions. Buy at the next open. Sell at the open
after the first close above the 20-session average, or after 30 sessions. Two risk controls
were added after the base result was known (PLAN, A2 addendum): take the trade only when
NIFTY closes above its own 200-session average, and sell after a close 15% below the entry.

| Form | Sample | Trades | Winners | % a trade (t) | Excess, same stock (t) | 5% of trades worse than | Worst | Years below zero |
|---|---|---|---|---|---|---|---|---|
| Base | 2011 to 2021 | 434 | 70.5% | +1.04 (t 2.7) | +0.55 (t 1.3) | −9.8% | −50.7% | 2 of 11 |
| Base | 2021 to 2026 | 143 | 69.9% | +1.11 (t 2.6) | +0.85 (t 1.9) | −7.8% | −16.0% | 1 of 6 |
| With the market filter | 2011 to 2021 | 329 | 69.3% | +1.16 (t 2.9) | +0.68 (t 1.6) | −9.0% | −50.7% | 3 of 11 |
| With the market filter | 2021 to 2026 | 112 | 75.0% | +1.41 (t 3.1) | +1.20 (t 2.5) | −9.5% | −16.0% | 0 of 6 |
| **Filter and 15% stop** | 2011 to 2021 | 329 | 69.3% | **+1.19 (t 3.2)** | +0.75 (t 1.9) | −9.8% | −21.8% | 3 of 11 |
| **Filter and 15% stop** | 2021 to 2026 | 112 | 75.0% | **+1.36 (t 2.9)** | +1.15 (t 2.4) | −9.5% | −16.3% | 0 of 6 |

For the last form:

- **By sub-period:** +1.56% (128 trades) in 2011 to 2014, +0.06% (95) in 2015 to 2018, +1.76%
  (106) in 2019 to 2021; then TUNE +1.22% (98), CHOOSE +2.07% (6), TEST +2.53% (8). The last
  two years hold only 14 trades, because NIFTY was often below its average or no stock fell
  far enough above its own.
- **By entry year:** 2011 −1.0, 2012 +2.4, 2013 −0.0, 2014 +2.9, 2015 +1.6, 2016 +0.4, 2017
  +2.1, 2018 −3.3, 2019 +0.8, 2020 +1.0, 2021 +2.3 and +0.8, 2022 +1.8, 2023 +0.5, 2024 +1.4,
  2025 +2.2, 2026 +2.4.
- **Depth, with the filter:** 6% gives +0.62% (t = 2.4) and +0.44% (t = 1.2); 10% gives +1.33%
  (t = 2.0) and +1.57% (t = 2.0).
- **How often:** about 30 trades a year in the older sample and 22 in the newer one, held 11
  to 12 sessions. Dips come together: up to 13 trades were open at once.
- **Shape:** in the base form the median trade makes +2.3% and seven in ten win. The losers
  are fewer and larger. Without the stop the worst trades are those bought before the crash
  of March 2020 (−50.7% and −49.7%); the stop cuts the worst to −21.8% and moves the mean by
  0.05 points or less.
- **Capital:** the base rule holds a stock on about one day in ten, so most of the money
  waits. Its worst year was 2020 (−10.2% of the whole capital in the base form).

The rule was checked twice: the sub-agent's script and a separate plain-loop version by
another author give the same trades (434 and 434; 144 and 143, one trade at the edge of the
window).

### 5.3 The same rule on other stocks

A rule found on 18 stocks can be an accident of those 18. So the last form was run
unchanged on two lists that were written down before any of their stocks was looked at
(PLAN, A4): B, thirty other large companies; and C, thirteen companies that were in the
NIFTY 50 around 2011 and later left it, several after long declines (BHEL, DLF, PNB, SAIL,
Reliance Infrastructure, Reliance Power and others). List C is the check on hindsight. All
three lists use Yahoo daily bars, entries from 2011-01-01 to 2026-11-06.

| List | Trades | Winners | % a trade (t) | Excess, same stock (t) | Worst | Stocks above zero | By period: 2011-14 / 2015-18 / 2019-21 / 2022-26 |
|---|---|---|---|---|---|---|---|
| A, the 18 desk stocks | 440 | 70.0% | +1.19 (t 3.9) | +0.83 (t 2.6) | −21.8% | 14 of 18 | +1.56 / +0.06 / +1.56 / +1.33 |
| B, 30 other large companies | 836 | 70.2% | +0.88 (t 3.1) | +0.40 (t 1.4) | −26.8% | 25 of 30 | +1.34 / +0.17 / +0.93 / +0.98 |
| C, 13 that left the NIFTY 50 | 588 | 66.5% | +0.63 (t 1.5) | +0.63 (t 1.5) | −25.5% | 11 of 13 | −0.15 / −0.50 / +0.47 / +1.77 |
| B and C together | 1,424 | 68.7% | +0.78 (t 2.7) | +0.50 (t 1.7) | −26.8% | 36 of 43 | |
| All 61 stocks | 1,864 | 69.0% | +0.88 (t 3.4) | +0.58 (t 2.2) | −26.8% | 50 of 61 | |

- The test written in advance (a mean above zero and an excess above zero in B and in C)
  is met. The size is smaller than on the desk's 18 stocks, and on B and C alone the
  excess is not significant (t = 1.7).
- **Depth again.** At 10% the 43 other stocks give +1.24% a trade (t = 3.1) with an excess
  of +0.90% (t = 2.2); at 6% the mean is +0.46% on B and +0.12% on C. Deeper pullbacks
  recover more on all three lists, which is what a real effect should do.
- **Not every year.** On B and C together the mean is below zero in 6 of 16 calendar
  years; on all 61 stocks in 5 of 16. 2015 to 2018 is weak on every list.
- **The declining companies** did not break the rule: the two worst of list C, Reliance
  Infrastructure and Reliance Power, average −0.19% and −0.21% a trade over 69 and 66
  trades, because the rule only buys above the 200-session average and sells within 30
  sessions.
- Yahoo's bars are adjusted for splits, not for dividends or demergers. Days with a move
  over 35% were treated as suspect; one trade a rule was dropped for that reason.

### 5.4 The depository charge at one share

The charge is ₹14.75 for each sale out of the demat account, whatever the trade is worth.
As a share of one share at the last price in the store:

| Under 0.5% | 0.5% to 1.0% | 1.0% to 1.5% | Over 1.5% |
|---|---|---|---|
| MARUTI 0.12, HEROMOTOCO 0.27, LT 0.36, MM 0.45 | TCS 0.59, BHARTIARTL 0.70, SUNPHARMA 0.79, ICICIBANK 0.94 | AXISBANK 1.12, RELIANCE 1.12, INFY 1.26, BAJFINANCE 1.36, SBIN 1.45 | HDFCBANK 1.88, KOTAKBANK 3.10, TMPV 5.05, ITC 5.22, TATASTEEL 7.94 |

At one share the last form of the rule makes −1.02% and −0.45% a trade in the two samples,
because the charge averages 1.8% over the 18 stocks. On a trade of ₹15,000 the same charge
is 0.1%. The rule is not usable at one share except in the first two columns.

## 6. What this means for the desk

1. **Options.** There is no new option rule to add. The two option slots stay in the paper
   test until 30 closed trades each, as the owner decided on 8 November, or are paused: the
   owner's choice. Section 3 is the reason same-day buying has no wind behind it.
2. **Stocks.** The pullback rule is the one candidate for the paper test. It needs two
   things the desk does not have: a strategy read on daily closes, and a paper book that
   carries a stock trade over several sessions (story US-027, with the depository charge in
   its costs). Until then it can only be followed by hand.
3. **The unit of a delivery trade.** One share makes the fixed charge the largest cost. The
   owner decides whether a delivery trade is one share, or the number of shares nearest to a
   rupee amount.
4. **The overnight gain** is the strongest effect in the data and is outside the owner's
   rules. It has also faded for NIFTY and BANKNIFTY in the last year. It is recorded here
   for information, not as a proposal.

## 7. Limits

- **Many rules were tried.** About 45 stock rules and 30 cuts of the option signal. The
  pullback result is modest: on the stocks it was not found on, its excess over holding
  is +0.50% a trade with t = 1.7. It is not conclusive.
- **The 18 stocks are today's large companies**, chosen with hindsight. Their past returns
  flatter every long rule, and a dip in a company that survived is the kind that recovers.
  The excess over holding the same stock removes the first effect. Section 5.3 tests the
  second on companies that declined; companies that were delisted altogether are in no
  list.
- **The older sample** uses Yahoo's adjusted daily bars and today's charge rates. Its open is
  the exchange's official open; the store's open is the first one-minute bar.
- **Fills** are assumed at the open with 0.03% slippage. Nothing was traded, on paper or
  otherwise.
- **Not used:** delivery percentages, institutional flow data, results dates. They are not
  in the store.
- **The Lab cannot run these rules** (it has no multi-day holding), so the product's own
  simulator has not confirmed them. Two independent scripts have.

## Reproduce

```bash
cd "/Users/sudhanshushekhar/Downloads/ONE LOT BOT - DHAN"
PYTHONPATH=src:. .venv/bin/python -W ignore scripts/alpha_cache.py          # minute cache, 1 min
PYTHONPATH=src:. .venv/bin/python -W ignore scripts/alpha_intraday1.py      # section 4 (also 2 to 5)
PYTHONPATH=src:. .venv/bin/python -W ignore scripts/alpha_stocks_daily.py --family trend --stage 1
PYTHONPATH=src:. .venv/bin/python -W ignore scripts/alpha_stocks_daily.py --family reversion
PYTHONPATH=src:. .venv/bin/python -W ignore scripts/alpha_stocks_daily.py --family mirror
PYTHONPATH=src:. .venv/bin/python -W ignore scripts/alpha_stocks_daily.py --family fair   # section 5.1
PYTHONPATH=src:. .venv/bin/python -W ignore scripts/alpha_pullback_check.py # section 5.2
PYTHONPATH=src:. .venv/bin/python -W ignore scripts/alpha_stocks_daily.py --family universe  # 5.3
```

Outputs are written to `data/research/alpha_2026-11-12/`, which git ignores. The older daily
bars are fetched from Yahoo Finance by `alpha_stocks_daily.py` (`--stage 2`, 19 requests; and
`--family universe`, 62 requests).
