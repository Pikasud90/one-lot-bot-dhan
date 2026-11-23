# Ratchet study (US-074), 2026-11-23

Script `scripts/ratchet_study.py`; outputs in `data/research/ratchet_2026-11-23/` of the main
checkout. Read-only on the store; no Dhan request. Three numbers in a row are TUNE / CHOOSE / TEST.

## The rule and what was tested

`src/onelot/manage/ratchet.py`, unchanged, loaded by file path. The owner's part: at 25% of the
planned move the stop goes above breakeven, at 50% it locks 35%, the target is fixed until 80%.
Tested: the first stop (k x ATR(14, Wilder) of 15m or 60m bars for options, of daily bars for
stocks, read on the last bar closed by the entry), `extend`, and whether the strategy's own exit
is kept. First target 3 risks away. For options the managed price is the index on one-minute
bars; the plan `cost` is 0.05% of the index, a stand-in for round-trip premium cost over delta.
The numpy exit equals the module's `exit_on_bar` then `advance` loop on 2,000 of 2,000 random
trades (bar, price, reason; 1,230 exited by the rule). Baseline reproduced exactly: five indices
3,824 trades, −0.24% of premium a trade, 38.3% winners.

## Part A: micro-trend entries, four owner indices, model premiums (24 cells, sorted by TUNE)

% a trade is of premium, t with one entry day as one draw. Exits % are first stop / breakeven
stop / lock stop / trail stop / target / strategy exit (3 of 7) / session end (15:15).

| cell | trades | % a trade (t) | index bp | win % | avg win | avg loss | payoff | TUNE / CHOOSE / TEST | exits % |
|---|---|---|---|---|---|---|---|---|---|
| 60m k2.0 ext kept (chosen) | 3077 | −0.28 (−0.7) | +3.2 | 38.6 | +12.33 | −8.20 | 1.50 | −0.09 / +0.19 / −1.38 | 0/2/1/0/0/54/42 |
| 60m k2.0 fix kept | 3077 | −0.28 (−0.7) | +3.2 | 38.6 | +12.33 | −8.20 | 1.50 | −0.09 / +0.19 / −1.38 | 0/2/1/0/0/54/42 |
| 60m k1.5 fix kept | 3133 | −0.28 (−0.7) | +3.3 | 40.9 | +11.35 | −8.33 | 1.36 | −0.09 / +0.10 / −1.25 | 2/7/2/1/0/47/40 |
| 60m k1.5 ext kept | 3132 | −0.29 (−0.8) | +3.2 | 40.9 | +11.31 | −8.33 | 1.36 | −0.11 / +0.08 / −1.25 | 2/7/2/1/0/47/40 |
| 15m k2.0 ext kept | 3290 | −0.40 (−1.2) | +3.0 | 46.4 | +9.15 | −8.66 | 1.06 | −0.19 / −0.17 / −1.27 | 9/16/6/2/0/33/34 |
| baseline (3 of 7 or 15:15) | 3065 | −0.37 (−0.9) | +3.0 | 37.9 | +12.34 | −8.14 | 1.52 | −0.20 / +0.08 / −1.38 | 0/0/0/0/0/57/43 |
| 15m k2.0 fix kept | 3290 | −0.41 (−1.2) | +2.9 | 46.4 | +9.11 | −8.66 | 1.05 | −0.21 / −0.20 / −1.27 | 9/16/6/2/1/33/33 |
| 60m k1.0 ext kept | 3381 | −0.44 (−1.4) | +3.0 | 47.9 | +8.40 | −8.58 | 0.98 | −0.33 / −0.05 / −1.22 | 12/18/6/3/0/28/33 |
| 60m k1.0 fix kept | 3383 | −0.46 (−1.5) | +3.0 | 47.9 | +8.37 | −8.58 | 0.98 | −0.34 / −0.09 / −1.22 | 12/18/6/2/1/28/32 |
| 15m k2.0 ext drop | 3158 | −0.58 (−1.6) | +2.2 | 50.6 | +8.95 | −10.35 | 0.86 | −0.37 / −0.34 / −1.49 | 27/18/6/3/0/0/46 |
| 15m k2.0 fix drop | 3158 | −0.60 (−1.6) | +2.1 | 50.6 | +8.91 | −10.35 | 0.86 | −0.39 / −0.37 / −1.49 | 27/18/6/2/1/0/45 |
| 15m k1.5 ext kept | 3617 | −0.57 (−2.1) | +2.7 | 51.7 | +6.92 | −8.58 | 0.81 | −0.41 / −0.39 / −1.26 | 25/25/9/5/0/13/24 |
| 15m k1.5 ext drop | 3570 | −0.60 (−2.2) | +2.6 | 53.5 | +6.87 | −9.20 | 0.75 | −0.42 / −0.47 / −1.28 | 33/26/9/5/0/0/27 |
| 15m k1.5 fix kept | 3617 | −0.58 (−2.2) | +2.6 | 51.7 | +6.89 | −8.58 | 0.80 | −0.44 / −0.36 / −1.25 | 25/25/9/3/2/13/23 |
| 60m k1.5 fix drop | 2913 | −0.65 (−1.4) | +1.5 | 47.0 | +11.18 | −11.14 | 1.00 | −0.45 / +0.01 / −1.94 | 19/9/3/1/0/0/68 |
| 15m k1.5 fix drop | 3570 | −0.62 (−2.2) | +2.6 | 53.5 | +6.84 | −9.20 | 0.74 | −0.46 / −0.45 / −1.28 | 33/26/9/3/2/0/26 |
| 60m k1.0 ext drop | 3270 | −0.57 (−1.7) | +2.6 | 51.7 | +8.20 | −9.96 | 0.82 | −0.47 / −0.08 / −1.41 | 28/20/7/3/0/0/43 |
| 60m k1.5 ext drop | 2912 | −0.67 (−1.5) | +1.5 | 47.0 | +11.13 | −11.14 | 1.00 | −0.48 / −0.02 / −1.94 | 19/9/3/1/0/0/68 |
| 60m k1.0 fix drop | 3272 | −0.59 (−1.8) | +2.5 | 51.7 | +8.18 | −9.96 | 0.82 | −0.48 / −0.12 / −1.41 | 28/20/7/2/1/0/42 |
| 60m k2.0 ext drop | 2795 | −0.83 (−1.6) | +0.4 | 45.0 | +12.51 | −11.76 | 1.06 | −0.63 / +0.20 / −2.46 | 12/4/2/0/0/0/82 |
| 60m k2.0 fix drop | 2795 | −0.83 (−1.6) | +0.4 | 45.0 | +12.51 | −11.76 | 1.06 | −0.63 / +0.20 / −2.46 | 12/4/2/0/0/0/82 |
| 15m k1.0 fix kept | 4359 | −1.13 (−7.0) | +1.3 | 53.1 | +4.11 | −7.06 | 0.58 | −1.03 / −1.03 / −1.53 | 39/36/10/4/3/1/7 |
| 15m k1.0 ext kept | 4359 | −1.11 (−6.8) | +1.4 | 53.1 | +4.14 | −7.06 | 0.59 | −1.03 / −1.04 / −1.42 | 39/36/10/7/0/1/7 |
| 15m k1.0 fix drop | 4358 | −1.13 (−7.0) | +1.3 | 53.1 | +4.11 | −7.08 | 0.58 | −1.03 / −1.03 / −1.54 | 39/36/10/4/3/0/7 |
| 15m k1.0 ext drop | 4358 | −1.11 (−6.8) | +1.4 | 53.1 | +4.14 | −7.08 | 0.59 | −1.04 / −1.04 / −1.43 | 39/36/10/7/0/0/7 |

MIDCPNIFTY (training, not in the totals): baseline +0.30% (759 trades, t +0.5), −0.09 / +1.87 /
−0.22; chosen cell +0.47% (762, t +0.8), +0.11 / +1.77 / +0.14.

## The chosen cell and its test

The rule fixed in advance (highest TUNE among cells whose CHOOSE is not below the baseline's
+0.08) picks `60m k2.0 ext kept`. Its TEST is −1.38% a trade, the baseline's −1.38%. Paired by
entry day, chosen minus baseline is +0.35 / +0.37 / −0.00 points a day (t +1.7 / +1.4 / −0.0);
over all days +0.28 (t +2.0), but that is the best TUNE of 24 cells and none of it is in TEST.
Only 4 of 24 cells beat the baseline over five years. Why so little changes: 2 x ATR of 60m is
about 1% of the index and the target 3%, so the first raise needs a 0.75% intraday move; the
stop moved on 3% of trades and the target almost never filled. Tight cells (15m k1.0) lift
winners to 53% and lose −1.1% a trade (t −7, payoff 0.58). Dropping the 3 of 7 exit is worse in
10 of 12 pairs and equal in 2.

## Sensitivity (chosen cell only)

Target 2 R: −0.36% (t −1.0), 42.9% winners, payoff 1.24, −0.11 / −0.20 / −1.28. Target 4 R:
−0.37% (t −0.9), 38.0%, 1.51, −0.19 / +0.07 / −1.37 (the rule almost never acts). Trail gap
0.15 and 0.30: both −0.29% (t −0.7), 38.6%, 1.50, −0.10 / +0.19 / −1.38 (trail rarely reached).

## Real premiums

Still to run (the stage fetches only from 15:40 to 08:50 IST; this work ended before 15:40). It
prices every NIFTY and BANKNIFTY trade of the baseline and the chosen cell with `price_one`
(store first, 2 requests a second) and reports like for like. From the main checkout:

    PYTHONPATH=src:scripts:. .venv/bin/python -W ignore \
      "../ONE LOT BOT - DHAN - wt-us074/scripts/ratchet_study.py" real report

## Part B: the control (random entries, 20 seeds, four indices)

Per index and day as many entries as the micro-trend rule made, at random 3-minute closes from
09:30 to 14:45, random side; after each exit the next entry is drawn from the closes left.

| exit | trades | % a trade: mean (sd, range) | win %: mean (sd) | payoff: mean (sd) |
|---|---|---|---|---|
| hold to 15:15 | 2738 | −1.15 (0.27, −1.72 to −0.77) | 43.0 (0.8) | 1.08 (0.05) |
| 60m k2.0 ext kept (chosen) | 3015 | −0.83 (0.11, −1.05 to −0.64) | 23.3 (0.6) | 2.29 (0.10) |
| 60m k1.5 fix kept | 3016 | −0.84 (0.13, −1.12 to −0.64) | 23.9 (0.6) | 2.20 (0.12) |
| 60m k2.0 ext drop | 2776 | −0.88 (0.26, −1.42 to −0.15) | 44.4 (0.9) | 1.07 (0.03) |
| 15m k1.0 ext drop | 3023 | −1.26 (0.14, −1.59 to −1.06) | 52.1 (0.9) | 0.57 (0.02) |

Every exit loses; no seed of any exit is positive. Holding to 15:15 moves the index −0.1 bp a
trade, as it should. The kept cells' 23% winners at payoff 2.3 come from the 3 of 7 exit cutting
trades against the trend (+2.2 bp of index a trade) and still lose −0.8%. Shorter exits lose
less than holding mostly by paying less model time decay (47 against 187 minutes held).

## Part C: stocks, the pullback delivery rule (18 stocks, 12 cells)

Reference reproduced: 112 trades, +1.36% a trade before the depository charge, 75% winners
(depth 8%, NIFTY above SMA200, closing stop 15%). % a trade is after delivery charges and the
Rs 14.75 depository charge at shares nearest Rs 10,000; "one share" puts it on one share.
Managed on one-minute bars (every session from 2021-11-08). Strategy exit: the rule's own.

| cell | trades | % a trade (t) | win % | avg win | avg loss | payoff | TUNE / CHOOSE / TEST | exits % | one share | before DP | over hold (t) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 112 | +1.21 (+2.8) | 74.1 | +3.29 | −4.74 | 0.69 | +1.07 / +1.92 / +2.40 | 0/0/0/0/0/100/0 | −0.80 | +1.36 | +1.01 (+2.2) |
| k3.0 fix drop (chosen) | 95 | +0.94 (+1.2) | 68.4 | +4.91 | −7.65 | 0.64 | +0.95 / +3.18 / −0.44 | 29/42/20/6/2/0/0 | −0.94 | +1.09 | −0.45 (−0.6) |
| k3.0 ext drop | 95 | +0.90 (+1.2) | 68.4 | +4.84 | −7.65 | 0.63 | +0.90 / +3.18 / −0.44 | 29/42/20/8/0/0/0 | −0.99 | +1.04 | −0.50 (−0.7) |
| k3.0 ext kept | 118 | +0.87 (+2.1) | 73.7 | +3.12 | −5.43 | 0.57 | +0.83 / +1.92 / +0.73 | 15/3/0/0/0/81/0 | −1.20 | +1.02 | +0.77 (+1.8) |
| k3.0 fix kept | 118 | +0.87 (+2.1) | 73.7 | +3.12 | −5.43 | 0.57 | +0.83 / +1.92 / +0.73 | 15/3/0/0/0/81/0 | −1.20 | +1.02 | +0.77 (+1.8) |
| k2.0 ext drop | 116 | +0.65 (+1.1) | 66.4 | +3.83 | −5.63 | 0.68 | +0.71 / +2.31 / −1.51 | 33/38/19/10/0/0/0 | −1.35 | +0.79 | +0.31 (+0.5) |
| k2.0 fix drop | 116 | +0.66 (+1.1) | 66.4 | +3.85 | −5.63 | 0.68 | +0.70 / +2.58 / −1.51 | 33/38/19/6/4/0/0 | −1.34 | +0.80 | +0.32 (+0.5) |
| k2.0 ext kept | 123 | +0.49 (+1.3) | 69.1 | +3.04 | −5.21 | 0.58 | +0.53 / +1.29 / −0.76 | 27/11/1/0/0/62/0 | −1.57 | +0.63 | +0.49 (+1.3) |
| k2.0 fix kept | 123 | +0.49 (+1.3) | 69.1 | +3.04 | −5.21 | 0.58 | +0.53 / +1.29 / −0.76 | 27/11/1/0/0/62/0 | −1.57 | +0.63 | +0.49 (+1.3) |
| k1.5 ext kept | 136 | −0.03 (−0.1) | 63.2 | +2.46 | −4.30 | 0.57 | −0.02 / +0.78 / −0.80 | 35/21/1/0/0/43/0 | −1.99 | +0.11 | +0.07 (+0.2) |
| k1.5 fix kept | 136 | −0.03 (−0.1) | 63.2 | +2.46 | −4.30 | 0.57 | −0.02 / +0.78 / −0.80 | 35/21/1/0/0/43/0 | −1.99 | +0.11 | +0.07 (+0.2) |
| k1.5 fix drop | 130 | −0.11 (−0.3) | 61.5 | +2.58 | −4.41 | 0.58 | −0.06 / +0.01 / −0.87 | 38/38/17/5/3/0/0 | −2.11 | +0.03 | −0.16 (−0.4) |
| k1.5 ext drop | 130 | −0.13 (−0.3) | 61.5 | +2.55 | −4.41 | 0.58 | −0.08 / +0.01 / −0.87 | 38/38/17/8/0/0/0 | −2.13 | +0.01 | −0.19 (−0.5) |

All 12 cells are below the baseline in TUNE and over five years. The same selection rule picks
k3.0 fix drop; its TEST is −0.44% against the baseline's +2.40%. At one share the depository
charge (about 2 points at these prices) makes every cell negative, the baseline too (−0.80%).

## Verdict

(i) Options: no. The cell the fixed rule picked is +0.09 points a trade better over five years
(−0.28% against −0.37%), it was the best TUNE of 24 cells, and its TEST equals the baseline's
(−1.38% each): not distinguishable from chance. 20 of 24 cells are worse than the baseline.
Stocks: every one of 12 cells is worse than the present exits, by 0.3 to 1.3 points a trade.

(ii) Index options: if the product must show a stop and target, use 2 x ATR(14) of 60m bars,
target 3 R, extension on, and keep the 3 of 7 exit and 15:15; it changes about 3% of exits and
costs nothing measurable. Avoid 15m and k 1.0. Delivery stocks: keep the rule's own exits
(close above SMA20, 30 sessions, 15% closing stop); if a ratchet is required, k 3.0 of daily
ATR with those exits kept (−0.34 points a trade against the baseline, positive in all periods).

(iii) The chosen option cell wins 38.6% of trades with a payoff of 1.50 (winner +12.33%, loser
−8.20%), not 30% winners with large winners. Part B's 23% winners and 2.3 payoff come with
random entries and lose −0.8% a trade.

(iv) Nothing here is an edge after costs. An exit rule cannot add an edge the entries lack:
the option rule loses on model premiums in every cell, and random entries lose with every exit.
The stock pullback rule stays a paper-test candidate (+1.21% a trade, +1.01 over holding,
t +2.2) at shares nearest Rs 10,000; at one share the depository charge removes it.

Not done or approximate: real premiums (to run); the option plan cost is a stand-in; the stock
path from 15:14 to the close is unseen; a same-day stock sale pays no depository charge; stock
trades before 2021-11-08 (they only set the order of later ones) are managed on daily bars.
