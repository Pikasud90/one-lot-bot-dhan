# The capped pullback book against the index (US-100, 2026-11-25)

## The answer in five lines

Rs 1 lakh in the product's capped book (K = 10) became Rs 1.30 lakh from 19 Oct 2010 to 6 Nov
2026 (+1.64% a year); NIFTY held (B1) 4.15, NIFTY at the book's exposure (B2) 1.54, the 222-stock
basket at that exposure (B3) 2.27. Not ahead of B3 or B2. Behind B3 by −3.59% a year, borderline:
the fixed seed's interval ends at −0.04, other seeds, more draws and longer blocks cross zero, and
the book's costs (Rs 1.23 lakh) exceed its gap. No order beats RANDOM, no cap beats K = 10.

## What was fixed in advance

The contract `.agile/stories/US-100.md`, before any result: the study's 8,085 trades unchanged;
Rs 1,00,000, never topped up, idle cash earning nothing; places counted as the product counts
them; six orders at K = 10 (RANDOM: 500 seeded shuffles); caps 5, 10, 20 and none (money K x
Rs 10,000; no cap: the most ever open, 137, so Rs 13.7 lakh); B1, B2, B3 without dividends; a
bootstrap over calendar months (2,000 draws, seed 20261125, book and benchmark drawn together);
the rules of answers 3 and 4. Periods by RESEARCH-RULES (TUNE to 7 Nov 2024, CHOOSE to 7 Nov 2025,
TEST to 7 Nov 2026); a trade belongs to the period of its entry, the book's figures to sessions.

Choices the contract left open, made in the code before the tables were read:
- A trade is ACTIVE on an evening from its signal evening until the morning its exit fills
  (`onelot.delivery.wide`). Ties go by the stock key, as in the product. PRODUCT: desk 18 first,
  then the deepest pullback (1 − close / highest close of 20 sessions). LIQUID: median close x
  volume of 20 sessions. CALM: SD of 20 daily returns. All read on the signal's close.
- Cash: the fill pays shares x open x (1 + slippage); a trade that cannot be paid then is REFUSED
  FOR CASH (its place is lost for that evening). Costs are booked at the sale. A morning's exits
  pay back before its entries, paid older signal first, then by order (this changes the held
  trades only at K = 5; below).
- B2 and B3 hold, close to close, the rupees the book held in stocks at the close before, with no
  costs; B3 is the equal weight return of the stocks with a bar. A period starts from the book's
  value before it; "less B" is the gain less the benchmark's on the same rupees, "% a year" the
  difference of growth rates. t is the study's weekly-sum t (RESEARCH-RULES 4). HELD = inside
  the cap; REFUSED BY THE CAP = no free place; REFUSED FOR CASH as above.

## Cross-check: the book against the product's own cap tier

`scripts/pullback_book_product.py` runs the product's evening round (`WideDesk.run_round` through
`replay_rounds`, inputs read-only) over 4,180 sessions: 8,085 trades in tier all, 2,560 within the
cap, none late (the lead's run: 2,560). My place counting on the product's own trades gives the
same tier for 8,085 of 8,085. My book on the STUDY's trades, PRODUCT order, K = 10, places only:
the same 8,085 pairs and decisions, 2,560 held, depth equal to 0.000000 points; one exit date
differs (RPOWER of 2018-03-01) and changes no decision. Both reviewers reproduced this. With the
cash rule 340 are refused for cash: 2,477 held, 5,268 refused by the cap. Places only, the tier
makes +0.19% a trade (t 1.2), TUNE / CHOOSE / TEST +0.17 / +0.28 / +0.71.

## Orders at K = 10

| Order | Period | Held | Refused (cap) | Refused (cash) | Win % | % a trade (t) | End (lakh) | Growth % a year | Worst fall % | Worst year % | Places used | All full % |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| PRODUCT | TUNE | 2286 | 5107 | 336 | 63 | +0.12 (+0.7) | 1.21 | +1.36 | −39.5 | −21.2 (2018) | 6.5 | 37 |
| PRODUCT | CHOOSE | 135 | 111 | 4 | 68 | +0.23 (+0.4) | 1.23 | +1.80 | −7.6 | +0.6 (2024) | 5.9 | 22 |
| PRODUCT | TEST | 56 | 50 | 0 | 73 | +0.71 (+0.7) | 1.30 | +5.47 | −3.7 | +1.9 (2026) | 2.7 | 12 |
| PRODUCT | ALL | 2477 | 5268 | 340 | 64 | +0.14 (+0.9) | 1.30 | +1.64 | −39.5 | −21.2 (2018) | 6.2 | 34 |
| DEEP | ALL | 2408 | 4962 | 715 | 63 | +0.11 (+0.7) | 1.22 | +1.23 | −47.3 | −24.0 (2018) | 6.2 | 32 |
| SHALLOW | ALL | 2507 | 5268 | 310 | 63 | −0.00 (−0.0) | 0.98 | −0.14 | −39.8 | −19.2 (2018) | 6.2 | 33 |
| LIQUID | ALL | 2278 | 4568 | 1239 | 62 | −0.11 (−0.7) | 0.70 | −2.22 | −55.8 | −23.4 (2018) | 6.0 | 29 |
| CALM | ALL | 2375 | 5304 | 406 | 64 | +0.13 (+0.8) | 1.20 | +1.14 | −41.1 | −25.0 (2018) | 6.2 | 34 |

| Order | % a trade TUNE / CHOOSE / TEST (percentile in RANDOM) | Growth % a year TUNE / CHOOSE / TEST (percentile) | ALL: % a trade, growth (percentiles) |
|---|---|---|---|
| RANDOM median [2.5th to 97.5th] | +0.09 [−0.00 to +0.22] / −0.16 [−0.49 to +0.16] / +0.34 [−0.51 to +1.01] | +1.09 [−0.32 to +2.48] / −3.84 [−8.28 to −0.05] / +3.38 [−1.08 to +7.23] | +0.08, +0.88 |
| PRODUCT | +0.12 (67) / +0.23 (99) / +0.71 (87) | +1.36 (64) / +1.80 (100) / +5.47 (85) | +0.14 (81), +1.64 (84) |
| DEEP | +0.11 (61) / −0.11 (62) / +0.83 (93) | +1.12 (52) / −2.15 (82) / +6.37 (93) | +0.11 (68), +1.23 (68) |
| SHALLOW | −0.00 (3) / −0.08 (69) / +0.07 (27) | −0.08 (5) / −2.52 (77) / +1.47 (20) | −0.00 (4), −0.14 (5) |
| LIQUID | −0.07 (0) / −0.69 (1) / −0.47 (3) | −1.62 (0) / −11.34 (0) / −1.09 (3) | −0.11 (0), −2.22 (0) |
| CALM | +0.13 (69) / +0.17 (98) / +0.03 (26) | +1.28 (59) / −1.30 (91) / +1.61 (21) | +0.13 (75), +1.14 (63) |

## Caps with the PRODUCT order

| K (money) | % a trade TUNE / CHOOSE / TEST (t ALL) | Held / refused (cap) / refused (cash) | End (lakh) | Growth % a year | Worst fall % | Places used, all full % | less B3 % a year TUNE / CHOOSE / TEST |
|---|---|---|---|---|---|---|---|
| 5 (Rs 0.5 lakh) | +0.13 / −0.72 / +0.28 (+0.5) | 1358 / 6246 / 481 | 0.58 | +0.92 | −45.5 | 3.4, 48 | −4.90 / −9.12 / +5.67 |
| 10 (Rs 1 lakh) | +0.12 / +0.23 / +0.71 (+0.9) | 2477 / 5268 / 340 | 1.30 | +1.64 | −39.5 | 6.2, 34 | −4.51 / −1.91 / +6.21 |
| 20 (Rs 2 lakh) | +0.23 / +0.04 / +0.98 (+1.5) | 4144 / 3790 / 151 | 2.99 | +2.54 | −38.0 | 10.4, 20 | −2.64 / −1.62 / +3.09 |
| none (Rs 13.7 lakh) | +0.79 / +0.20 / +1.25 (+4.0) | 8085 / 0 / 0 | 20.03 | +2.39 | −10.8 | 20.4, 0 | −0.51 / −0.60 / +0.47 |

## Against the benchmarks (book less B; 95% interval from the month bootstrap)

| Book | Period | B1 / B2 / B3 end (lakh) | less B1, % a year | less B2, % a year | less B3, % a year | less B3, Rs |
|---|---|---|---|---|---|---|
| K 10 PRODUCT | TUNE | 4.01 / 1.53 / 2.23 | −9.04 [−16.65 to −1.37] | −1.69 [−6.12 to +3.05] | −4.51 [−8.59 to −0.45] | −102,147 [−209,625 to −12,093] |
| K 10 PRODUCT | CHOOSE | 1.27 / 1.24 / 1.25 | −3.55 [−29.34 to +21.44] | −0.52 [−12.51 to +14.47] | −1.91 [−10.11 to +8.60] | −2,308 [−12,206 to +10,433] |
| K 10 PRODUCT | TEST | 1.21 / 1.22 / 1.22 | +7.28 [−39.22 to +34.64] | +6.73 [+1.73 to +12.32] | +6.21 [+2.15 to +10.72] | +7,613 [+2,616 to +13,022] |
| K 10 PRODUCT | ALL | 4.15 / 1.54 / 2.27 | −7.64 [−15.83 to −0.48] | −1.08 [−5.09 to +2.88] | −3.59 [−7.25 to −0.04] | −96,841 [−203,838 to −1,677] |
| K 5 | ALL | 2.08 / 0.83 / 1.17 | −8.35 [−17.81 to +0.21] | −2.26 [−8.26 to +3.92] | −4.54 [−10.11 to +1.20] | −59,427 [−1,24,267 to +25,164] |
| K 20 | ALL | 8.31 / 2.98 / 4.16 | −6.74 [−14.52 to +0.32] | +0.03 [−2.99 to +3.15] | −2.13 [−4.63 to +0.34] | −1,16,758 [−2,58,700 to +23,847] |
| no cap | ALL | 56.89 / 17.53 / 21.48 | −6.88 [−15.03 to +1.44] | +0.85 [−0.03 to +1.70] | −0.45 [−1.00 to +0.08] | −1,44,990 [−3,21,632 to +31,256] |

Other orders less B3 over ALL, % a year: DEEP −3.81 [−7.85 to +0.26], SHALLOW −5.02 [−9.05 to
−1.22], LIQUID −7.15 [−11.93 to −2.72], CALM −4.03 [−7.70 to −0.23]. In TEST PRODUCT (+6.21) and
DEEP (+7.76) are ahead of B3 with an interval above zero. Costs (added after the reviews): the
2,476 closed held trades made Rs 1,52,450 before costs and Rs 29,770 after: Rs 1,22,681 (0.50% of
the amount), more than the gap to B3 (Rs 96,841). B1, B2 and B3 carry no costs.

## Sensitivities added after the first results (from the two reviews; not in the contract)

ALL book less B3 (PRODUCT, K = 10), % a year: seeds 0 to 39 at 2,000 draws give an upper end of
−0.03 to +0.42 (median +0.15), below zero for 2 of 40; 100,000 draws at the fixed seed [−7.24 to
+0.18] (3.1% of draws at or above zero; rupees [−2,00,948 to +6,968]); runs of 3, 6, 12 months
[−7.29 to +0.30], [−6.74 to +0.54], [−6.84 to +0.20]. Morning fills by order alone: the same held
trades at K = 10, 20 and no cap; at K = 5, 260 trades change: 1,346 held, Rs 0.65 lakh, +1.69% a
year, less B3 TUNE / CHOOSE / TEST −4.14 / −8.12 / +5.00. A reviewer's B3 rebalanced monthly,
yearly or bought once gives −3.58, −3.81, −3.71 against daily −3.59 (not rerun here).

## What the cap refused (K = 10, PRODUCT order)

| Trades | n | % a trade ALL (t) | TUNE / CHOOSE / TEST | Win % |
|---|---|---|---|---|
| held | 2477 | +0.14 (+0.9) | +0.12 / +0.23 / +0.71 | 64 |
| held, on evenings with a refusal by the cap | 847 | +0.54 (+1.9) | +0.44 / +2.31 / +2.75 | |
| held, on the other evenings | 1630 | −0.07 (−0.4) | −0.05 / −0.34 / +0.04 | |
| refused by the cap | 5268 | +1.09 (+4.3) | +1.10 / +0.03 / +1.85 | 70 |
| refused for cash | 340 | +0.52 (+1.2) | +0.48 / +3.69 (4 trades) / none | 67 |

| Signals that evening | Held n | Held % a trade | Refused by the cap n | Refused by the cap % a trade |
|---|---|---|---|---|
| 1 to 2 | 1140 | −0.16 | 283 | −0.58 |
| 3 to 9 | 1155 | +0.19 | 2180 | +0.23 |
| 10 or more | 182 | +1.72 | 2805 | +1.92 |

Of 2,048 evenings with a signal, 1,000 had a refusal by the cap (median 4 signals, mean 6.3, NIFTY
a median 2.1% below its 20-session high; the others 1, 1.7 and 0.5%). Added after the first results:
the two middle rows, and on the 536 evenings with both kinds refused less held is −0.41 a trade
(t −1.57 over evenings), −0.09 weighted by the count refused. Refusals for cash fall mostly in
years after a loss (2012 to 2014, 2019 to 2023) and on shares over Rs 10,000 (MRF 10 times).

## Power for the paper test (t = 2)

| Trades | Basis | Trades a month | Mean % | SD % | Trades for t = 2 | Months, single trades | Weekly-sum t | Months, week-clustered |
|---|---|---|---|---|---|---|---|---|
| held, K = 10 | whole span | 12.9 | +0.14 | 6.36 | 8,216 | 639 | +0.88 | 1,005 |
| held, K = 10 | TEST | 4.7 | +0.71 | 6.49 | 331 | 71 | +0.74 | 87 |
| tier all | whole span | 42.0 | +0.77 | 6.46 | 278 | 7 | +4.03 | 48 |
| tier all | TEST | 8.8 | +1.25 | 6.22 | 99 | 11 | +1.62 | 18 |

Single trades: (2 x SD / mean)^2, a floor. Clustered (added after the reviews): months x (2/t)^2.

## The seven answers

1. **What Rs 1 lakh became.** Under the product's rule (K = 10, PRODUCT order) Rs 1.30 lakh in 16
   years, +1.64% a year, with a fall of −39.5% from 15 Jan 2018 to 24 Sep 2020 and −21.2% in 2018.
   B1 became Rs 4.15 lakh (+9.3% a year), B2 Rs 1.54 lakh, B3 Rs 2.27 lakh. No dividends in any.
2. **Ahead of B3 or B2 beyond chance?** No. Over the whole span the book is behind B3, borderline:
   −3.59% a year [−7.25 to −0.04], −Rs 96,841 [−2,03,838 to −1,677]; the upper end goes above zero
   with 38 of 40 other seeds, with 100,000 draws (+0.18) and with runs of 3 months or more: not
   robustly shown. The book's costs (Rs 1.23 lakh) exceed that gap; the benchmarks carry none.
   Against B2 −1.08% a year [−5.09 to +2.88]: not shown. TEST is ahead of both (+6.21, +6.73,
   intervals above zero; 56 trades, seen in earlier studies); CHOOSE not shown. B3 removes the
   survival of holding the names, not the extra one of buying their dips (argued, not measured).
3. **A better order?** None by the fixed rule (beyond the 97.5th percentile of RANDOM in TUNE and
   above its median in CHOOSE and TEST), on % a trade or on growth. PRODUCT stands at 67 and 64
   in TUNE, 99 and 100 in CHOOSE, 87 and 85 in TEST: as good as any tested, so it should stay.
   Descriptive only: LIQUID is at 0 / 1 / 3, but with 1,239 refusals for cash against PRODUCT's
   340 it is a different book; SHALLOW is 3rd in TUNE, 69th in CHOOSE and 27th in TEST.
4. **Which cap?** No change. Less B3 in % a year, K = 20 against K = 10: TUNE −2.64 against −4.51,
   CHOOSE −1.62 against −1.91 (better), TEST +3.09 against +6.21 (worse); no cap the same pattern
   (−0.51 / −0.60 / +0.47, TUNE / CHOOSE / TEST); K = 5 (−4.90 / −9.12 / +5.67) worse in all three,
   with fills by order alone better in TUNE (−4.14) only. None beats K = 10 in all three periods.
5. **What the cap costs.** Held trades made +0.14% a trade (t 0.9), the 5,268 refused by the cap
   +1.09% (t 4.3). The cap does not drop an evening's better signals: on the 536 evenings with both
   kinds the refused did no better (−0.41 a trade, t −1.57). The gap is WHICH evenings fill the
   places: held trades of evenings without a refusal make −0.07% (1,630), of full evenings +0.54%
   (847). Full evenings are broad dips (median 4 signals, NIFTY 2.1% below its 20-session high);
   quiet-evening signals take up the places, so only a few of each burst get in.
6. **What to expect from the paper test.** About 13 held trades a month on the span, 4.7 in the
   last year. With one week's trades as one draw, the held trades need about 1,005 months (84
   years) to reach t = 2 at the span's size and 87 months (7 years) at TEST's; single trades give
   639 and 71, a floor. Tier all (42 a month on the span, 8.8 in the last year) needs about 48 and
   18 months. These t are against zero, and about +0.6 of tier all's +0.77 is market drift: t = 2
   against zero is no edge against the index; the excess over the basket needs far more trades.
7. **What this cannot tell.** The 222 names are today's survivors: B3 shares the flattery of
   holding them, not the larger one of buying their dips, B1 and B2 none. Prices are adjusted as
   of November 2026 (five repairs), so a later split makes earlier dear shares look affordable
   (EICHERMOT before 2020: 16 of 29 trades over Rs 10,000 a share then, 3 held). Fills at the open
   with 0.03% slippage; no dividends (they would help B1 most), no taxes, no interest on idle cash,
   no costs on the benchmarks; a start date chosen by the data; 340 refusals for cash.

## Doubts
The ALL verdict against B3 hangs on the seed and the blocks; K = 5 on the order of fills; the book
on the cash rule (places only +0.19% a trade). TEST's intervals rest on 12 months; B2 and B3 run
close to close. A reviewer counted 5 EICHERMOT trades held over Rs 10,000; I find 3 (4 places only).

## Not tested
A place held back for evenings with many signals; sizing by the book's value or topping up; a
waiting list for refused signals; interest on idle cash; dividends; taxes; costs on the
benchmarks; caps other than 5, 10, 20 and none; orders other than the six.

## Reproduce
From the worktree; outputs in the main checkout's ignored `data/research/pullback_book_2026-11-25/`.

    nice -n 10 .venv/bin/python scripts/pullback_book.py --stage prep
    PYTHONPATH=src nice -n 10 .venv/bin/python scripts/pullback_book_product.py \
        --daily MAIN/data/research/swing_2026-11-24/daily.sqlite --nifty-store MAIN/data/onelot.db \
        --nifty-csv MAIN/data/research/alpha_2026-11-12/daily_universe/NIFTY.csv \
        --out MAIN/data/research/pullback_book_2026-11-25/product_trades.csv
    for s in cross books random sens report; do nice -n 10 .venv/bin/python \
        scripts/pullback_book.py --stage $s; done

Backtests are evidence about a rule, never advice to buy or sell.
