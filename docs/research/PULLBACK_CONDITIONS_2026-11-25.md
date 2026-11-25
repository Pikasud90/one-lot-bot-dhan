# The pullback rule by market condition (US-101, 2026-11-25)

## The answer in brief

After costs, in no condition did the rule beat the same 222 stocks held on the same days: EXCESS is
−0.16% a trade over all 8,085 trades (t −2.3), no cell of 13 is DISCOVERED (luck: 0.08, placebo 0).
Before costs it beats that basket by +0.34 (t +4.5); the 0.51% round trip more than eats it. None
is AVOID (TEST as a whole was +0.93 over the basket). Over the span no book that waits for broad or
deep evenings beats NIFTY held, B2 or B3 beyond chance (QUIET-OUT leads B3 in TEST on single months
only). Answer 6: no change.

## Where the idea came from, and what was fixed in advance

US-100's note (table "Signals that evening") showed, over the whole span and with a split not fixed
in advance, −0.16% a trade on evenings with 1 to 2 signals and +1.72% with 10 or more. So no period
is fully unseen for this idea; TUNE holds 93% of the trades. Fixed before any result, in the
contract `.agile/stories/US-101.md`: the 8,085 trades unchanged; three conditions at the close of
the signal evening, one at a time, four values each, plus ALL (13 cells); RAW and EXCESS over the
equal-weight basket; RESEARCH-RULES section 5 for delivery on the EXCESS, the same on RAW beside it
("with drift"); a placebo of 20 shuffles; three named books against B1, B2, B3; the rule of answer
6. TUNE to 7 Nov 2024, CHOOSE to 7 Nov 2025, TEST to 7 Nov 2026, by entry date.

Choices the contract left open, made in the code before the tables were read:
- US-100's `scripts/pullback_book.py` is imported by file path, not copied: its prep, book, cash
  rule, `figures`, B1, B2, B3, month bootstrap and weekly-sum t are used unchanged.
- BREADTH: trades with the same signal evening. DEPTH: 1 − close / highest of NIFTY's last 20
  closes (that evening's included; `NIFTY.csv` before the store, as US-100 joins it). VIX:
  INDIAVIX close over the median of its last 60 closes. No close that evening: left out.
- EXCESS: the basket is bought at each stock's open on the entry day and sold at its open on the
  exit day, as the trade is; a stock without a bar on either day is left out (167 to 222 stocks,
  median 211); the trade open on 6 Nov 2026 is marked to that close. The basket carries no costs
  and no dividends; RAW is after the rule's costs (0.51% a trade). Prices: the adjusted bars.
- The contract calls `bench_pct` "NIFTY over the same days"; in `swing_universe.py` it is the same
  stock's average net % over as many sessions. Both are shown beside, neither selects: over NIFTY
  open to open (135 trades without a NIFTY bar left out), and over `bench_pct` as the study made it.
- WATCH, second route: examined, positive in all three periods, span t 2 or more, one threshold
  missed of five (TUNE t, TUNE size, CHOOSE trades, CHOOSE t, TEST); counting three steps instead
  gives the same 13 statuses. CANDIDATE: passes above the arithmetic and any of 20 placebo rounds.
- Answer 6 compares growth % a year per period, named books only (QUIET-OUT, BROAD, DEEP).

Left out: VIX for 749 evenings, 2,842 trades (703 before INDIAVIX starts on 2 Jan 2017, 11 without a
bar, 35 with under 60 closes); DEPTH for 1 evening, 3 trades (Saturday 20 Jan 2024). No look-ahead:
50 random signals cut after the evening gave the same values (for BREADTH by construction; the count
is known at the close, as the study's loop lets a stock signal again from its sale day's close).

## The 13 cells

Trades, evenings and entry weeks TUNE / CHOOSE / TEST; % a trade with the weekly-sum t; "n/a": fewer
than 5 entry weeks, so no t is printed (added after review; the statuses use the computed t).

| Condition | Value | Trades | Evenings | Weeks | RAW % (t) TUNE / CHOOSE / TEST | RAW span (t) | EXCESS % (t) TUNE / CHOOSE / TEST | EXCESS span (t) | Status on the EXCESS | With drift (RAW) |
|---|---|---|---|---|---|---|---|---|---|---|
| ALL | all | 7729 / 250 / 106 | 1898 / 104 / 46 | 561 / 37 / 17 | +0.79 (+3.9) / +0.20 (+0.4) / +1.25 (+1.6) | +0.77 (+4.0) | −0.17 (−2.2) / −0.58 (−1.8) / +0.93 (+1.6) | −0.16 (−2.3) | REJECTED, TUNE t −2.25 | WATCH (CHOOSE t +0.4) |
| BREADTH | 1 to 2 | 1379 / 99 / 47 | 998 / 72 / 34 | 485 / 33 / 17 | −0.15 (−0.8) / −0.72 (−0.9) / +0.75 (+0.8) | −0.16 (−0.9) | −0.40 (−2.5) / −0.95 (−1.8) / +1.02 (+1.4) | −0.39 (−2.6) | REJECTED, TUNE t −2.47; not AVOID: TEST +1.02 | REJECTED (TUNE t −0.8) |
| BREADTH | 3 to 9 | 3349 / 139 / 47 | 726 / 31 / 11 | 394 / 20 / 10 | +0.17 (+0.8) / +0.68 (+1.0) / +1.14 (+1.1) | +0.20 (+1.0) | −0.30 (−2.8) / −0.32 (−0.8) / +0.88 (+0.9) | −0.29 (−2.7) | REJECTED, TUNE t −2.79; not AVOID: TEST +0.88 | REJECTED (TUNE t +0.8) |
| BREADTH | 10 to 19 | 1695 / 12 / 12 | 131 / 1 / 1 | 95 / 1 / 1 | +0.75 (+2.3) / +2.25 (n/a) / +3.62 (n/a) | +0.78 (+2.4) | −0.05 (−0.3) / −0.47 (n/a) / +0.75 (n/a) | −0.05 (−0.3) | REJECTED, TUNE t −0.34 | REJECTED (TUNE t +2.3) |
| BREADTH | 20 or more | 1306 / 0 / 0 | 43 / 0 / 0 | 37 / 0 / 0 | +3.40 (+6.1) / none / none | +3.40 (+6.1) | +0.28 (+1.6) / none / none | +0.28 (+1.6) | REJECTED, TUNE t +1.64 | DISCOVERED only (CHOOSE 0 trades) |
| DEPTH | under 1% | 2111 / 64 / 38 | 868 / 39 / 19 | 347 / 17 / 10 | +0.30 (+1.6) / +0.32 (+0.3) / +1.51 (+2.3) | +0.32 (+1.8) | −0.41 (−2.8) / −0.01 (−0.0) / +1.21 (+2.5) | −0.37 (−2.6) | REJECTED, TUNE t −2.81; not AVOID: TEST +1.21 | REJECTED (TUNE t +1.6) |
| DEPTH | 1 to 2% | 1498 / 57 / 29 | 367 / 29 / 14 | 234 / 17 / 7 | +0.58 (+1.7) / +2.03 (+1.9) / −0.66 (−0.4) | +0.61 (+1.9) | −0.15 (−0.9) / +0.65 (+0.8) / −0.03 (−0.0) | −0.12 (−0.7) | REJECTED, TUNE t −0.93 | REJECTED (TUNE t +1.7) |
| DEPTH | 2 to 4% | 2440 / 114 / 21 | 431 / 33 / 10 | 205 / 18 / 3 | +0.91 (+2.6) / −0.89 (−1.0) / +1.85 (n/a) | +0.84 (+2.5) | −0.08 (−0.6) / −1.42 (−2.4) / +1.90 (n/a) | −0.13 (−1.0) | REJECTED, TUNE t −0.64 | DISCOVERED only (CHOOSE −0.89) |
| DEPTH | 4% or more | 1677 / 15 / 18 | 231 / 3 / 3 | 99 / 2 / 2 | +1.39 (+2.6) / +1.00 (n/a) / +3.06 (n/a) | +1.40 (+2.7) | +0.00 (+0.0) / −1.27 (n/a) / +0.74 (n/a) | −0.00 (−0.0) | REJECTED, TUNE t +0.02 | DISCOVERED only (CHOOSE 15 trades) |
| VIX | under 0.9 | 698 / 84 / 9 | 237 / 32 / 8 | 97 / 13 / 4 | −0.05 (−0.1) / −0.78 (−0.6) / +1.67 (n/a) | −0.11 (−0.3) | −0.34 (−1.1) / −0.68 (−1.0) / +1.52 (n/a) | −0.35 (−1.3) | REJECTED, TUNE t −1.10 | REJECTED (TUNE t −0.1) |
| VIX | 0.9 to 1.1 | 2433 / 130 / 47 | 645 / 56 / 21 | 233 / 25 / 9 | +0.56 (+1.8) / +0.69 (+1.1) / +0.43 (+0.4) | +0.56 (+2.0) | −0.32 (−2.6) / −0.21 (−0.5) / +0.23 (+0.2) | −0.30 (−2.6) | REJECTED, TUNE t −2.59; not AVOID: TEST +0.23 | REJECTED (TUNE t +1.8) |
| VIX | 1.1 to 1.3 | 1349 / 27 / 50 | 209 / 13 / 17 | 85 / 8 / 7 | +1.57 (+2.6) / −0.43 (−0.4) / +1.94 (+1.7) | +1.55 (+2.7) | −0.19 (−1.0) / −2.09 (−2.0) / +1.47 (+2.0) | −0.17 (−0.9) | REJECTED, TUNE t −1.03 | DISCOVERED only (CHOOSE −0.43) |
| VIX | 1.3 or more | 407 / 9 / 0 | 58 / 3 / 0 | 25 / 1 / 0 | +2.07 (+1.4) / +4.22 (n/a) / none | +2.11 (+1.4) | −0.38 (−1.3) / −0.44 (n/a) / none | −0.38 (−1.3) | REJECTED, TUNE t −1.26 | REJECTED (TUNE t +1.4) |

All 13 cells are examined. The basket over the same days made +0.94% a trade (+0.95 / +0.78 /
+0.32), more than RAW +0.77. Only BREADTH 20 or more is above zero in TUNE (+0.28, t 1.6), with no
trade after. TEST as a whole was +0.93 over the basket and 9 of the 10 cells with TEST trades are
positive there, so a positive TEST says little about one condition. None is PASSED or AVOID, so no
neighbours.

Beside it, none selecting. Over NIFTY, same days: +0.30 (+0.32 / −0.50 / +1.06; span t +2.7), so
the basket beat NIFTY by about +0.46 a trade (today's 222 survivors); BREADTH 20 or more would be
DISCOVERED only (+1.75, TUNE t +5.9, no CHOOSE trade). Over `bench_pct`: +0.54; ALL (+0.57, TUNE t
+2.8, CHOOSE −0.34) and 20 or more would be DISCOVERED only. Before costs (a sensitivity added after
the first results): +0.34 / −0.07 / +1.45 over the basket (span +0.34, t +4.5); every cell is above
zero in TUNE; 20 or more (+0.79, t +4.6) and DEPTH 4% or more (+0.51, t +3.4) would be DISCOVERED
only. The costs take every cell to zero or below in TUNE except 20 or more (+0.28, t 1.6).

## Against luck

| Measure | | Examined | Discovered | Confirmed | Passed | AVOID |
|---|---|---|---|---|---|---|
| EXCESS | real | 13 | 0 | 0 | 0 | 0 |
| EXCESS | arithmetic (13 x 0.6%, x 16%, x 50%) | 13 | 0.078 | 0.012 | 0.006 | |
| EXCESS | placebo, mean (largest) of 20 rounds | 13 | 0.00 (0) | 0.00 (0) | 0.00 (0) | 0.05 (1) |
| RAW, with drift | real | 13 | 5 | 0 | 0 | 0 |
| RAW, with drift | placebo, mean (largest) | 13 | 5.20 (7) | 0.55 (2) | 0.50 (2) | 0.00 (0) |

The placebo shuffles each condition's labels over the evenings inside each calendar year (an evening
keeps its trades; ALL keeps its real result). On the EXCESS the real counts are no higher than
luck's; on RAW the 5 discoveries are what shuffled labels give too (5.20). For BREADTH a shuffled
label no longer matches the evening's count, so that placebo tests the label; the real count is 0.

## The three books and US-100's book (K = 10, product order, Rs 1 lakh, cash rule)

| Book | Eligible trades (evenings) | Held / capped / refused (cash), span | % a trade (t), span | EXCESS % (t), span | End (lakh) | Growth % a year | Growth TUNE / CHOOSE / TEST | Worst fall % | Places used |
|---|---|---|---|---|---|---|---|---|---|
| US-100 (all evenings) | 8085 (2048) | 2477 / 5268 / 340 | +0.14 (+0.9) | −0.32 (−2.7) | 1.30 | +1.64 | +1.36 / +1.80 / +5.47 | −39.5 | 6.2 |
| BOOK-QUIET-OUT (3 or more) | 6560 (944) | 1865 / 4486 / 209 | +0.33 (+1.6) | −0.16 (−1.2) | 1.51 | +2.59 | +2.39 / +4.43 / +3.51 | −46.7 | 4.8 |
| BOOK-BROAD (10 or more) | 3025 (176) | 812 / 2209 / 4 | +1.11 (+2.9) | +0.03 (+0.1) | 1.90 | +4.08 | +4.53 / −0.06 / +2.11 | −11.0 | 2.1 |
| BOOK-DEEP (NIFTY 2% down) | 4285 (711) | 1112 / 2987 / 186 | +0.43 (+1.2) | −0.02 (−0.1) | 1.50 | +2.58 | +3.43 / −7.16 / +0.88 | −30.3 | 3.1 |

Held: US-100 2286 / 135 / 56, QUIET-OUT 1747 / 82 / 36, BROAD 792 / 10 / 10, DEEP 1049 / 49 / 14.
B1 / B2 / B3 end (lakh): US-100 4.15 / 1.54 / 2.27, QUIET-OUT 4.15 / 1.50 / 2.09, BROAD 4.15 /
1.46 / 1.90, DEEP 4.15 / 1.27 / 1.58. The books are after the rule's costs; B1, B2, B3 carry none.

Book less B, % a year, 95% interval (2,000 draws, seed 20261125); share of seeds 0 to 39 off zero.

| Book | Period | less B1 | less B2 | less B3 | Seeds excluding zero, months | Seeds, blocks of 3 |
|---|---|---|---|---|---|---|
| US-100 | ALL | −7.64 [−15.83 to −0.48] | −1.08 [−5.09 to +2.88] | −3.59 [−7.25 to −0.04] | 95% / 0% / 5% | 100% / 0% / 0% |
| QUIET-OUT | TUNE | −8.00 [−15.76 to −0.15] | −0.32 [−5.22 to +4.73] | −2.86 [−7.07 to +1.37] | 95% / 0% / 0% | 100% / 0% / 0% |
| QUIET-OUT | CHOOSE | −0.92 [−25.37 to +18.84] | +2.50 [−6.36 to +12.30] | +2.81 [−3.82 to +10.90] | 0% / 0% / 0% | 0% / 0% / 0% |
| QUIET-OUT | TEST | +5.33 [−41.71 to +34.77] | +2.33 [−2.03 to +6.72] | +2.53 [+0.30 to +4.94] | 0% / 0% / 100% | 0% / 0% / 0% |
| QUIET-OUT | ALL | −6.69 [−14.92 to +1.04] | +0.03 [−4.30 to +4.38] | −2.12 [−5.82 to +1.51] | 0% / 0% / 0% | 42% / 0% / 0% |
| BROAD | TUNE | −5.87 [−14.89 to +2.91] | +2.03 [−0.75 to +4.87] | −0.00 [−2.62 to +2.58] | 0% / 0% / 0% | 0% / 0% / 0% |
| BROAD | CHOOSE | −5.40 [−31.06 to +15.52] | −1.18 [−3.39 to +0.51] | −0.51 [−1.49 to +0.28] | 0% / 0% / 0% | 0% / 0% / 0% |
| BROAD | TEST | +3.93 [−43.06 to +33.56] | +0.93 [−0.22 to +3.30] | +0.40 [−1.25 to +2.59] | 0% / 0% / 0% | 0% / 0% / 0% |
| BROAD | ALL | −5.19 [−13.52 to +3.35] | +1.71 [−0.87 to +4.28] | −0.01 [−2.31 to +2.26] | 0% / 0% / 0% | 0% / 0% / 0% |
| DEEP | TUNE | −6.96 [−15.32 to +1.73] | +1.88 [−1.56 to +5.68] | +0.07 [−2.74 to +3.20] | 0% / 0% / 0% | 80% / 0% / 0% |
| DEEP | CHOOSE | −12.51 [−36.14 to +8.45] | −8.59 [−15.66 to −1.50] | −6.29 [−11.08 to −1.15] | 0% / 100% / 100% | 0% / 100% / 100% |
| DEEP | TEST | +2.69 [−43.34 to +32.35] | +0.23 [−3.47 to +5.45] | +0.78 [−2.25 to +5.26] | 0% / 0% / 0% | 0% / 0% / 0% |
| DEEP | ALL | −6.70 [−15.08 to +1.39] | +1.06 [−2.12 to +4.15] | −0.31 [−2.88 to +2.29] | 0% / 0% / 0% | 15% / 0% / 0% |

US-100's book reproduces its note (2,477 held, Rs 1.30 lakh, the same intervals). Against it, growth
% a year: QUIET-OUT +1.03 / +2.62 / −1.95, BROAD +3.17 / −1.86 / −3.36, DEEP +2.07 / −8.97 / −4.59.
Lower ends of 0.00 with blocks of 3 in TEST are draws only of months in which the book held nothing.

## Power and how often such evenings come (week-clustered: months x (2 / t)^2)

| Trades | Evenings a month, span | Trades a month span / TEST | EXCESS % (t), span | Months for t = 2, EXCESS | RAW % (t), span | Months for t = 2, RAW |
|---|---|---|---|---|---|---|
| ALL (tier all) | 10.6 | 42.0 / 8.8 | −0.16 (−2.3) | never (below zero) | +0.77 (+4.0) | 48 |
| BREADTH 10 or more | 0.91 | 15.7 / 1.0 | | | | |
| BREADTH 20 or more | 0.22 | 6.8 / 0 | +0.28 (+1.6) | 286 | +3.40 (+6.1) | 21 |
| DEPTH 2% or more | 3.7 | 22.3 / 3.3 | | | | |
| BOOK-BROAD, held | 0.63 | 4.2 / 0.8 | +0.03 (+0.1) | 35,756 | +1.11 (+2.9) | 90 |
| BOOK-DEEP, held | 2.15 | 5.8 / 1.2 | −0.02 (−0.1) | never | +0.43 (+1.2) | 520 |
| BOOK-QUIET-OUT, held | 3.47 | 9.7 / 3.0 | −0.16 (−1.2) | never | +0.33 (+1.6) | 304 |
| US-100 book, held | 7.93 | 12.9 / 4.7 | −0.32 (−2.7) | never | +0.14 (+0.9) | 1,005 |

10 or more signals: 72 of 194 months, 1 evening in CHOOSE, 1 in TEST. NIFTY 2% or more down: 104 of
194 months; in TEST 13 evenings, all in January 2026, so BOOK-DEEP's TEST rests on one month.

## The seven answers

1. **Where did the rule beat the same stocks held on the same days?** Nowhere shown. EXCESS is
   −0.17 / −0.58 / +0.93 (span −0.16, t −2.3); no cell is DISCOVERED. The best TUNE cell, 20 or more
   signals, is +0.28 (t 1.6), none after TUNE; RAW +3.40, basket +3.13. Before costs the trades
   beat the basket by +0.34 (t +4.5; 20 or more +0.79); the costs more than eat it. Against NIFTY
   the sign is positive (+0.30, t +2.7), so "nowhere" is about the basket of today's 222 survivors.
2. **Where should it not be taken (AVOID)?** Nowhere by the rule. BREADTH 3 to 9, DEPTH under 1%
   and VIX 0.9 to 1.1 have TUNE t −2.79, −2.81, −2.59, BREADTH 1 to 2 −2.47, but all four are
   positive in TEST (+0.88, +1.21, +0.23, +1.02), as TEST as a whole was (+0.93). Not an AVOID.
3. **Counts against luck.** EXCESS: 13 examined, 0 discovered, 0 confirmed, 0 passed; luck gives
   0.078 / 0.012 / 0.006 by arithmetic and 0 / 0 / 0 in the placebo; the real counts are no higher.
   RAW with drift: 5 discovered, 0 confirmed, 0 passed against a placebo of 5.20 / 0.55 / 0.50.
4. **Would a book that waits have beaten NIFTY held, B2, B3?** Not shown. Over the span, less B1 /
   B2 / B3: BROAD −5.19 / +1.71 / −0.01, DEEP −6.70 / +1.06 / −0.31, QUIET-OUT −6.69 / +0.03 /
   −2.12; every interval holds zero, 0 of 40 seeds exclude zero for B2 and B3. DEEP is behind B2
   and B3 in CHOOSE (−8.59, −6.29, all seeds); QUIET-OUT leads B3 in TEST (+2.53), single months.
5. **How often, and how long a paper test?** No cell qualifies. 10 or more signals: 0.9 evenings a
   month on the span, 2 in the last two years; BROAD holds about 4 trades a month, under 1 in TEST.
   On the EXCESS its held trades (+0.03, t 0.1) need 35,756 months, DEEP and QUIET-OUT never; on
   RAW, 90, 520 and 304. NIFTY 2% down: 3.7 evenings a month, but all 13 in TEST fell in Jan 2026.
6. **Should the product's wide list change?** no change
7. **What this cannot tell.** The 222 names are today's survivors (the basket shares the flattery
   of holding them, not of buying their dips). The idea came from US-100's whole-span table, so TUNE
   (93% of trades) is not unseen; CHOOSE and TEST hold 2 broad evenings. Adjusted prices; the basket
   has no costs, RAW the rule's 0.51% round trip; no dividends, no taxes; VIX from March 2017 only.

## Doubts and not tested

The contract's `bench_pct` text does not match the study's code; over NIFTY or `bench_pct` some
cells would be DISCOVERED only (above), but neither selects. The basket is held, not rebalanced.
CHOOSE and TEST cells are small (0 to 139 trades); BROAD uses 2.1 of 10 places. One repair artefact:
ADANIENT opens at 347 on 3 Jun 2015 between 67 and 68; 1 trade touches those days, no cell moves.
Not tested: pairs of conditions; other cuts; BREADTH as a share of stocks; a basket with costs;
ranking by the conditions; a place held back for broad evenings; other caps; dividends; India VIX
before 2017.

## Rows for the setup book

| Instrument | Kind | Setup and preset | Timeframe | Condition | Exit | Trades a year | % a trade TUNE / CHOOSE / TEST (t whole span) | Real-premium or cost note | Status | Remark |
|---|---|---|---|---|---|---|---|---|---|---|
| 222 stocks | stock delivery | P1 pullback 8% | 1d | 10 or more signals that evening (broad dip) | own | 188 | EXCESS −0.05 / −0.47 / +0.75 (10 to 19), +0.28 / none / none (20 or more); RAW +3.40 TUNE | about 0.51% round trip; basket without costs | REJECTED | Most of the broad-dip gain is the basket's (RAW +3.40, basket +3.13); before costs +0.79 over it (t +4.6), after costs +0.28 (t 1.6); 2 broad evenings after TUNE; book less B3 −0.01 |
| 222 stocks | stock delivery | P1 pullback 8% | 1d | 1 to 2 signals that evening (quiet) | own | 95 | EXCESS −0.40 / −0.95 / +1.02 (−2.6) | as above | REJECTED | Not an AVOID: TUNE t −2.47 and positive in TEST; leaving quiet evenings out is behind US-100's book in TEST |
| 222 stocks | stock delivery | P1 pullback 8% | 1d | NIFTY 2% or more below its 20-session high | own | 267 | EXCESS −0.08 / −1.42 / +1.90 (2 to 4%), +0.00 / −1.27 / +0.74 (4% or more) | as above | REJECTED | No excess in TUNE; the book is behind B2 and B3 in CHOOSE; its TEST evenings all fall in January 2026 |
| 222 stocks | stock delivery | P1 pullback 8% | 1d | India VIX over its 60-session median, four bands | own | 45 to 270 | best TUNE EXCESS −0.19 (1.1 to 1.3) | as above | REJECTED | No VIX band beats the basket in TUNE; from March 2017 only |

## Reproduce

From the worktree, about a minute; outputs to the ignored folder named in the script (`--out`).

    for s in prep cells books report; do nice -n 10 .venv/bin/python -W ignore \
        scripts/pullback_conditions.py --stage $s; done

Backtests are evidence about a rule, never advice to buy or sell.
