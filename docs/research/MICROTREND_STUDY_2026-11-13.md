# The microtrend options rule: real premiums, situations, exits (13 November 2026)

Rule (the owner's, strategy `microtrend`): Supertrend(14, 1.7) on closed 3m, 5m, 10m, 15m,
30m, 60m and 4h bars; enter at a 3-minute close where all seven newly agree (call up, put
down), 09:30 to 14:45, one trade at a time per index; exit at the first 3-minute close where
3 of 7 point against, or at 15:15. Data 2021-11-08 to 2026-11-06, five indices. Three numbers
in a row are TUNE / CHOOSE / TEST. % is net % of premium a trade, model friction and charges.
t treats the trades of one entry day as one observation. Script: `scripts/microtrend_study.py`
(stages `trades`, `price`, `report`); outputs in `data/research/microtrend_2026-11-13/`.

## 1. Real premiums, NIFTY and BANKNIFTY

Every trade (no sampling, k = 1) was priced on Dhan's expired-option one-minute bars with the
same strike, entry minute and exit minute as the model. 2,243 Dhan requests at 2 a second,
07:14 to 07:34 IST; no 401, no deadline stop.

| | trades | priced | MODEL all | MODEL priced | REAL priced | real winners |
|---|---|---|---|---|---|---|
| ALL | 1,518 | 1,333 (88%) | −0.29 (t −0.7) | −1.77 (t −4.5) | −2.78 (t −6.6) | 31.9% |
| TUNE | 899 | 814 | −0.01 (t −0.0) | −1.26 (t −2.3) | −2.43 (t −4.6) | 32.9% |
| CHOOSE | 321 | 286 | −0.07 (t −0.1) | −1.89 (t −2.5) | −2.32 (t −2.4) | 33.2% |
| TEST | 298 | 233 | −1.35 (t −1.7) | −3.40 (t −4.3) | −4.54 (t −4.6) | 26.6% |
| NIFTY | 790 | 721 | −0.19 (t −0.4) | −1.25 (t −2.8) | −2.26 (t −4.4) | 33.4% |
| BANKNIFTY | 728 | 612 | −0.39 (t −0.7) | −2.38 (t −4.4) | −3.38 (t −6.2) | 30.1% |
| calls | 904 | 838 | −0.41 (t −0.8) | −1.50 (t −3.5) | −2.15 (t −5.2) | 33.3% |
| puts | 614 | 495 | −0.10 (t −0.1) | −2.22 (t −2.9) | −3.84 (t −4.3) | 29.5% |
| exit 3 of 7 | 866 | 797 | −6.51 | −6.92 | −7.95 | 15.1% |
| exit 15:15 | 652 | 536 | +7.98 | +5.88 | +4.91 | 56.9% |

Selection is strong. The 185 unpriced trades are the big moves: model +10.41% (t +6.5), index
+44.7 bp, against −2.9 bp on the priced ones; 157 of them are strikes that went beyond ATM±10
(what Dhan serves). So "REAL priced" understates the rule. The fair reading is the gap on the
same trades: real is 1.00 point a trade below the model (median −1.02). Putting the unpriced
trades back at their model result gives −1.17% (t −2.6), −1.07 / −0.46 / −2.24, 35.9% winners.
Every period is negative on real premiums, and for calls and puts alike.

## 2. When it works and when it does not (model, all 3,824 trades)

All five indices: −0.24% (t −0.6), +3.1 bp, 38.3% winners, −0.18 / +0.44 / −1.15 (matches the
reference). Cuts fixed in advance, known at the entry minute; "holds" = all three periods
positive and t at least 2.

| cut | trades | % (t) | bp | win | TUNE / CHOOSE / TEST |
|---|---|---|---|---|---|
| 09:30 to 10:29 | 1,455 | −0.49 (−0.8) | +1.7 | 35.0 | −0.35 / +0.65 / −2.01 |
| 10:30 to 11:59 | 689 | −0.20 (−0.3) | +3.4 | 36.9 | +0.14 / −0.55 / −1.03 |
| 12:00 to 13:29 | 992 | +0.34 (+0.5) | +4.9 | 41.7 | −0.40 / +2.35 / +0.34 |
| 13:30 to 14:45 | 688 | −0.60 (−1.1) | +3.3 | 42.0 | +0.12 / −2.11 / −1.46 |
| call / put | 2,319 / 1,505 | −0.52 (−1.3) / +0.19 (+0.3) | +2.5 / +4.2 | 38 / 39 | |
| with / against the gap | 2,681 / 1,143 | −0.55 (−1.4) / +0.49 (+0.7) | +2.8 / +4.1 | 39 / 38 | |
| with prev day | 2,856 | −0.65 (−1.5) | +1.9 | 37.0 | −0.59 / +0.71 / −2.27 |
| against prev day | 968 | +0.98 (+1.4) | +6.8 | 42.1 | +1.20 / −0.20 / +1.79 |
| VIX above median | 1,886 | −0.82 (−1.5) | +2.2 | 36.6 | −0.95 / +0.72 / −1.59 |
| VIX below median | 1,938 | +0.32 (+0.6) | +4.0 | 40.0 | +0.55 / +0.25 / −0.47 |
| 60m age 0 to 2 / 3 to 6 / 7+ | 834 / 614 / 2,376 | +0.92 (+1.3) / −0.53 / −0.57 | +6.0 / +2.0 / +2.4 | | +1.73 / +0.39 / −0.66 (0 to 2) |
| 4h age 0 / 1 to 2 / 3+ | 612 / 489 / 2,723 | −0.61 / −0.36 / −0.13 | +4.7 / +1.6 / +3.1 | | |
| 60m dist <1 / 1 to 2 / >2 ATR | 17 / 1,803 / 2,004 | +1.70 / −0.77 (−1.9) / +0.22 | | | |
| first / later alignment | 2,915 / 909 | −0.22 / −0.31 | +3.3 / +2.8 | | |
| yday range <0.7 / 0.7 to 1.2 | 1,052 / 1,863 | +0.08 / +0.02 | +4.4 / +4.0 | | |
| yday range >1.2 | 909 | −1.13 (−1.4) | −0.1 | 38.0 | −1.27 / +1.88 / −3.53 |
| Mon / Thu | 760 / 769 | −0.23 / +0.50 (+0.5) | +5.2 / +4.8 | | |
| Tue | 776 | −1.32 (−1.8) | −0.8 | 31.3 | −1.85 / −0.96 / +0.07 |
| Wed | 767 | −1.59 (−2.4) | −0.6 | 36.1 | −1.08 / +0.34 / −4.16 |
| Fri | 739 | +1.69 (+1.8) | +8.1 | 44.1 | +1.06 / +3.55 / +1.02 |
| BANKNIFTY / NIFTY / FINNIFTY | 728 / 790 / 733 | −0.39 / −0.19 / −0.50 | | | |
| MIDCPNIFTY / SENSEX | 759 / 814 | +0.30 / −0.43 | | | |

36 cells; 0 hold. With no edge about 0.5 would pass by chance, so none is a finding. The
nearest is Friday (positive in all three, t +1.8). The 60m distance under 1 ATR has only 17
trades (the 60m line sits about 1.7 ATR away by construction).

On the real-priced subset (32 cells with at least 80 trades) every cell is negative but
Friday at −0.55% (t −0.5, TEST −5.11); 0 hold. The worst on real premiums: VIX above its
median −4.89% (t −8.6), Tuesday −4.54% (t −6.0), 4h trend just turned −4.05%, yesterday's
range over 1.2 times normal −4.01%, puts −3.84%, BANKNIFTY −3.38%, entries 09:30 to 10:29
−3.48%. The least bad: VIX below median −1.03% (t −1.7), against the previous day −1.77%.

## 3. Exit variants (model, five indices)

| exit | trades | % (t) | bp | win | TUNE / CHOOSE / TEST | held |
|---|---|---|---|---|---|---|
| 3 of 7 (base) | 3,824 | −0.24 (−0.6) | +3.1 | 38.3 | −0.18 / +0.44 / −1.15 | 140 min |
| 2 of 7 | 4,838 | −0.61 (−2.4) | +2.4 | 33.7 | −0.67 / +0.06 / −1.09 | 86 min |
| 4 of 7 | 3,632 | −0.62 (−1.5) | +1.6 | 39.5 | −0.62 / +0.01 / −1.31 | 167 min |

Both variants are worse than the base; the exit is not the problem.

## 4. Verdict

The rule does not hold on real premiums: on the same NIFTY and BANKNIFTY trades the premiums
that traded give about 1 point a trade less than the model, which turns a flat model result
into a loss of about −1.2% a trade (t −2.6), negative in all three periods. No situation known
at entry passes the pre-set test (0 of 36 against about 0.5 by chance); Friday and trades
against the previous day's move look least bad but fail on real premiums. Avoid above-median
VIX, wide yesterday ranges, Tuesday and Wednesday, and the first hour: they are the worst in
both the model and real premiums. The paper test should log the real fill against the model
premium on every trade, the share of trades that end at 15:15 (they carry all the profit:
+4.91% real against −7.95% for reversal exits), and whether the TEST-period slide continues.

Unsure: the real price uses the model's friction on top of traded premiums, so the 1-point
gap is the premium level and path, not extra cost; the 12% unpriced trades are filled at their
model result in the blend, which is generous since the model already beats real by 1 point.
