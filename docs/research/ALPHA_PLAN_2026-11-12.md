# Alpha search of 2026-11-12: plan written BEFORE any result was read

Owner's rules (2026-11-12): options are intraday only (calls or puts, out the same day);
stocks may be delivery, long only, held as long as the strategy demands; one unit a trade.
Goal: a strategy with a real edge for each, proven after costs, or an honest "not found".

Periods by entry date: TUNE 2021-11-08..2024-11-07, CHOOSE ..2025-11-07, TEST ..2026-11-07.
TEST was read for earlier strategy families, never with the features below. Parameters below
are fixed from the literature or from the owner's logic; "neighbours" are shown for
robustness and are not picked from.

## A. Stocks (18, Adani Green excluded), daily bars, long only, one share
Signal on the close of day t, fill at the open of day t+1 (0.03% slippage a side), delivery
charges from the cost config, plus Rs 14.75 depository charge per sale at one share.
- S1 Donchian 55/20 on closes (neighbours 20/10, 100/50).
- S2 Golden cross: long while SMA50 > SMA200.
- S3 12-1 momentum rotation, month end: hold the top 5 with positive momentum, sell below
  rank 9 (neighbour 6-1).
- S4 New 252-day closing high with volume >= 1.5 x its 50-day mean; exit on a close below
  SMA50 (neighbour: no volume condition).
- S5 High-volume up day: return >= +3%, volume >= 3 x mean50, close in the top quarter of the
  range; hold 40 sessions, stop on a close below the signal day's low (neighbours 20, 60).
- S6 Owner's logic at swing horizon: daily Supertrend(14, 1.7) turns up while the weekly one
  is up; exit when the daily flips down (neighbour: multiplier 3.0).
- S7 S1 and S4 with a market filter: entries only when NIFTY closes above its SMA200.
- Benchmark: for each trade, the stock's mean daily drift over the sample x the days held.
Candidate rule: after the depository charge the mean trade is > 0 in TUNE and in CHOOSE,
pooled t >= 2 on TUNE+CHOOSE, at least 12 of 18 stocks positive, and the mean beats the
benchmark. Then TEST is read once and reported. Then an older sample (2010-01..2021-11,
split-adjusted daily bars) is run with the same fixed parameters, before the depository
charge, and must be > 0 with t >= 2.

## B. Index options, intraday only, one lot, out by 15:15 (late signals: 15:25)
Step 1, on the index itself (5 indices, one-minute bars), signed forward return to the exit:
- A1 Noise-area momentum: z = (P/ref - 1) / s14, where s14 is the mean absolute move from
  the open at that minute over the last 14 sessions and ref is max(open, previous close) for
  a long, min(open, previous close) for a short. Enter at the first quarter-hour checkpoint
  from 09:45 to 13:30 where |z| >= k (k = 1.0; neighbours 1.5, 2.0). Hold to the exit.
- A2 First half hour to last half hour: sign of P(09:45)/previous close - 1, in at 15:00.
- A3 The opening gap: continuation or fade by the size of the gap.
- A4 Big-money confirmation for A1 on NIFTY and BANKNIFTY from their heavyweight stocks:
  weighted breadth in the direction >= 70% and relative volume >= 1.0.
- A5 Day filters, each tried once: narrowest range of 7 yesterday, VIX regime, expiry day,
  turn of the month.
Step 2: option results. Modelled premiums for all five; then real premiums on NIFTY and
BANKNIFTY for the final candidates.
Candidate rule: at most three variants go to step 2, chosen on TUNE+CHOOSE. A candidate has
a modelled mean > 0 after costs in TUNE and in CHOOSE, pooled day-clustered t >= 2, at least
4 of 5 indices positive. Then TEST is read once. Then real premiums must be > 0.

## A2. Stocks, second family: buying weakness. Written 2026-11-12 after section A was read
What was known when this was written: no strategy of section A passed; the breakout rules
(S1) did WORSE than the stock's own drift for the same days held, in both samples (excess
-1.77% a trade, t -3.97 in 2011-2021; -1.12%, t -1.97 in 2021-2026). If buying strength is
below the drift, buying weakness should be above it. That is the hypothesis; the rules
below use published default parameters and were not tried before this was written.
Same fills, charges and benchmark as section A. One open trade per stock per rule.
- R1 RSI(2) < 10 with the close above SMA200; exit on a close above SMA5, or after 10
  sessions (neighbours: RSI(2) < 5, < 20).
- R2 R1 without the SMA200 condition.
- R3 The close is the lowest of 10 sessions, above SMA200; exit on a close above SMA10 or
  after 15 sessions (neighbours: 5-session low with SMA5 and 10 sessions; 20-session low with
  SMA20 and 30 sessions).
- R4 The close falls to 8% or more below the highest close of 20 sessions (first day of the
  condition), above SMA200; exit on a close above SMA20 or after 30 sessions (neighbours 6%,
  10%).
- R5 Four lower closes in a row; exit after 5 sessions (neighbours: 3 and 5 in a row).
- R6 Weekly losers: at the last session of a week buy the 3 stocks with the lowest
  5-session return at the next open, hold one week (neighbour: the lowest 5).
- R7 The close is below SMA20 - 2 sd(20), above SMA200; exit on a close above SMA20 or after
  20 sessions.
Candidate rule: before the depository charge, the mean trade and the mean excess over the
benchmark are both > 0 in the old sample (2011-01..2021-11) and in TUNE+CHOOSE; in the old
sample the excess has t >= 2 with trades clustered by entry week and at least 12 of 18 stocks
are positive. Then TEST is read once. The depository charge at one share is reported beside
it at today's share prices, as a separate question for the owner.

## A3. Stocks, third family: the mirror of the breakout rule. Written 2026-11-12 after A2 was read
What was known: none of R1 to R7 passed. Short dips earn about the drift before charges.
The one significant, repeated fact is that S1 (buy the N-session closing high, sell the
M-session closing low) earns LESS than the drift: excess -1.77% (t -3.97) and -0.78%
(t -3.20) in 2011-2021 for 55/20 and 20/10; -1.12% (t -1.97) and -0.78% (t -3.08) in
2021-2026. A stock is either in an S1 trade or not, so the time S1 is flat must earn MORE
than the drift. The rule below is that complement. It is derived from a result, not
predicted before it, and must be read with that in mind.
- C_N_M: after S1's own exit signal (a close below the lowest close of the previous M
  sessions while S1 is long) buy at the next open; hold until S1's next entry signal (a close
  above the highest close of the previous N sessions) and sell at the next open. Long only
  when S1 is flat. Main: 55/20. Shown beside it: 20/10 and 100/50.
- Risk variants of 55/20, stated now and not to be picked from: a time stop of 120 sessions;
  entries only when NIFTY closes above its SMA200; a stop on a close 15% below the entry.
Candidate rule: before the depository charge, mean > 0 and excess over the benchmark > 0
with t >= 2 (trades clustered by entry week) in the old sample AND in TUNE+CHOOSE, at least
12 of 18 stocks positive in the old sample. TEST is then read once. The forward paper test
is the validation; nothing here is proof before that.

## A2 addendum. Written 2026-11-12 after R4's base result was known
R4 (8% pullback) was the one rule of A2 with a positive mean in both samples. Two risk
variants were written into scripts/alpha_pullback_check.py before they were run: a closing
stop 12% or 15% below the entry, and entries only when NIFTY closes above its SMA200 (the
same market filter as S7). They are risk controls on a known result, not new discoveries.
The final form to be reported: 8% depth, NIFTY filter, with and without the 15% stop, and
the depths 6% and 10% beside it as neighbours.

## A4. One more test of the pullback rule: other stocks. Written 2026-11-12 before it was run
The rule is fixed (8% below the 20-session closing high, close above SMA200, NIFTY above its
SMA200, out above SMA20 or after 30 sessions, 15% closing stop). It is run unchanged, with
the base form and the 6% and 10% depths beside it, on Yahoo daily bars 2011-01-01 to
2026-11-06 for two lists of stocks that were named here before any of them was looked at:
- B, thirty other large companies: ADANIENT, ADANIPORTS, APOLLOHOSP, ASIANPAINT, BAJAJ-AUTO,
  BAJAJFINSV, BEL, BPCL, BRITANNIA, CIPLA, COALINDIA, DIVISLAB, DRREDDY, EICHERMOT, GRASIM,
  HCLTECH, HINDALCO, HINDUNILVR, INDUSINDBK, JSWSTEEL, NESTLEIND, NTPC, ONGC, POWERGRID,
  SHRIRAMFIN, TATACONSUM, TECHM, TITAN, ULTRACEMCO, WIPRO.
- C, thirteen companies that were in the NIFTY 50 around 2011 and later left it, several
  after long declines: ACC, AMBUJACEM, BHEL, DLF, GAIL, JINDALSTEL, PNB, RELINFRA, RPOWER,
  SAIL, SIEMENS, TATAPOWER, VEDL. This list is the check on hindsight: the 18 desk stocks
  and list B are today's survivors.
What would count: in B and in C the final form has a mean above zero and an excess over a
like-for-like hold of the same stock above zero. If C is negative the rule depends on
picking survivors, and that is reported as a failure of the rule.
