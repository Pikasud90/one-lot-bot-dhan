# The capped pullback book against the index (US-100, 2026-11-25)

## The answer in five lines

Rs 1 lakh in the product's capped book (K = 10) became Rs 1.30 lakh from 19 Oct 2010 to 6 Nov
2026 (+1.64% a year); NIFTY bought and held (B1) became 4.15, NIFTY at the book's exposure (B2)
1.54, the 222-stock basket at the book's exposure (B3) 2.27. The book is BEHIND B3 beyond chance
over the whole span (−3.59% a year, 95% interval −7.25 to −0.04); against B2 not shown. The cap
keeps +0.14% a trade (t 0.9) of the rule's +0.77; it refuses the signals of broad dips (+1.09).
No order beats RANDOM by the fixed rule; no cap beats K = 10 by it.

## What was fixed in advance

The contract `.agile/stories/US-100.md`, written before any result: the study's 8,085 trades
(`trades_P1.pkl`) unchanged; a book of Rs 1,00,000, never topped up, idle cash earning nothing;
places counted as the product counts them; six orders at K = 10 (RANDOM as 500 seeded shuffles);
caps 5, 10, 20 and none with the PRODUCT order (money K x Rs 10,000, no cap: the largest number
ever open, 137, so Rs 13.7 lakh); benchmarks B1, B2, B3, no dividends in any leg; a block
bootstrap over calendar months (2,000 draws, seed 20261125, book and benchmark drawn TOGETHER);
the rules of answers 3 and 4. Periods by RESEARCH-RULES: TUNE to 7 Nov 2024, CHOOSE to 7 Nov 2025,
TEST to 7 Nov 2026; a trade belongs to the period of its entry, the book's figures to its sessions.

Choices the contract left open, made in the code before the tables were read:
- A trade is ACTIVE on an evening from its signal evening until the morning its exit fills
  (`onelot.delivery.wide`: pending entry, open and waiting to exit all count; an exit at an
  evening's open frees the place that evening). Ties in an order go by the stock key, as in the
  product. PRODUCT: the desk 18 first, then the deepest pullback (1 − close / highest close of
  the last 20 sessions). LIQUID: median of close x volume of the last 20 sessions. CALM: standard
  deviation of the last 20 close to close returns. All read on the signal's close.
- Cash: the fill pays shares x open x (1 + slippage); a trade that cannot be paid at its fill
  is REFUSED (its place, taken the evening before, is lost for that evening). The trade's costs
  are booked at the sale (its net rupees). Exits of a morning pay back before its entries.
- B2 and B3 hold, from each close to the next, the rupees the book held in stocks at the close
  before; they carry no costs. B3 is the equal weight daily return of the stocks with a bar.
- A period's figures start from the book's value at the close before the period; "less B" is
  the book's gain less the benchmark's from that value; "% a year" is the difference of yearly
  growth rates. "Places used" is the mean count of active trades each evening; "all full" the
  share of sessions with every place in use.
- On which evenings the book is full (answer 5): the count of signals that evening and how far
  NIFTY's close is below its highest close of 20 sessions.

## Cross-check: the book against the product's own cap tier

`scripts/pullback_book_product.py` runs the product's evening round (`WideDesk.run_round`,
through `replay_rounds`, as `scripts/pullback_wide_replay.py` does; book in a temporary file,
inputs read-only) over 4,180 sessions in 419 s and dumps every trade with its tier.
- Product: 8,085 trades in tier all, 2,560 within the cap, none read late (the lead's run: 2,560).
- My place counting on the product's own trades and depths: the same tier for 8,085 of 8,085.
- My book on the STUDY's trades, PRODUCT order, K = 10, places only (no cash rule): the same
  8,085 (stock, signal date) pairs, the same place decision on all 8,085, 2,560 held. Depth
  agrees to 0.000000 points. One exit date differs (RPOWER signalled 2018-03-01: the study sells
  2018-03-26, the product 2018-04-02; one of the few mismatches of the lead's replay run); it
  changed no place decision. The product's counting is the contract's
  words: no difference to follow.
- The product has no cash rule. With it (the contract's book), 340 of those trades are refused
  for cash and their places go to later signals: 2,477 held, 5,268 capped. Without it the cap
  tier makes +0.19% a trade (t 1.2): +0.17 / +0.28 / +0.71.

## Orders at K = 10

| Order | Period | Held | Capped | Refused (cash) | Win % | % a trade (t) | End (lakh) | Growth % a year | Worst fall % | Worst year % | Places used | All full % |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| PRODUCT | TUNE | 2286 | 5107 | 336 | 63 | +0.12 (+0.7) | 1.21 | +1.36 | −39.5 | −21.2 (2018) | 6.5 | 37 |
| PRODUCT | CHOOSE | 135 | 111 | 4 | 68 | +0.23 (+0.4) | 1.23 | +1.80 | −7.6 | +0.6 (2024) | 5.9 | 22 |
| PRODUCT | TEST | 56 | 50 | 0 | 73 | +0.71 (+0.7) | 1.30 | +5.47 | −3.7 | +1.9 (2026) | 2.7 | 12 |
| PRODUCT | ALL | 2477 | 5268 | 340 | 64 | +0.14 (+0.9) | 1.30 | +1.64 | −39.5 | −21.2 (2018) | 6.2 | 34 |
| DEEP | ALL | 2408 | 4962 | 715 | 63 | +0.11 (+0.7) | 1.22 | +1.23 | −47.3 | −24.0 (2018) | 6.2 | 32 |
| SHALLOW | ALL | 2507 | 5268 | 310 | 63 | −0.00 (−0.0) | 0.98 | −0.14 | −39.8 | −19.2 (2018) | 6.2 | 33 |
| LIQUID | ALL | 2278 | 4568 | 1239 | 62 | −0.11 (−0.7) | 0.70 | −2.22 | −55.8 | −23.4 (2018) | 6.0 | 29 |
| CALM | ALL | 2375 | 5304 | 406 | 64 | +0.13 (+0.8) | 1.20 | +1.14 | −41.1 | −25.0 (2018) | 6.2 | 34 |

The worst fall: 15 Jan 2018 to 24 Sep 2020. Every period and interval is in `report.md`.

| Order | % a trade TUNE / CHOOSE / TEST (percentile in RANDOM) | Growth % a year TUNE / CHOOSE / TEST (percentile) | ALL: % a trade, growth (percentiles) |
|---|---|---|---|
| RANDOM median [2.5th to 97.5th] | +0.09 [−0.00 to +0.22] / −0.16 [−0.49 to +0.16] / +0.34 [−0.51 to +1.01] | +1.09 [−0.32 to +2.48] / −3.84 [−8.28 to −0.05] / +3.38 [−1.08 to +7.23] | +0.08, +0.88 |
| PRODUCT | +0.12 (67) / +0.23 (99) / +0.71 (87) | +1.36 (64) / +1.80 (100) / +5.47 (85) | +0.14 (81), +1.64 (84) |
| DEEP | +0.11 (61) / −0.11 (62) / +0.83 (93) | +1.12 (52) / −2.15 (82) / +6.37 (93) | +0.11 (68), +1.23 (68) |
| SHALLOW | −0.00 (3) / −0.08 (69) / +0.07 (27) | −0.08 (5) / −2.52 (77) / +1.47 (20) | −0.00 (4), −0.14 (5) |
| LIQUID | −0.07 (0) / −0.69 (1) / −0.47 (3) | −1.62 (0) / −11.34 (0) / −1.09 (3) | −0.11 (0), −2.22 (0) |
| CALM | +0.13 (69) / +0.17 (98) / +0.03 (26) | +1.28 (59) / −1.30 (91) / +1.61 (21) | +0.13 (75), +1.14 (63) |

## Caps with the PRODUCT order

| K (money) | % a trade TUNE / CHOOSE / TEST (t ALL) | Held / capped / refused | End (lakh) | Growth % a year | Worst fall % | Places used, all full % |
|---|---|---|---|---|---|---|
| 5 (Rs 0.5 lakh) | +0.13 / −0.72 / +0.28 (+0.5) | 1358 / 6246 / 481 | 0.58 | +0.92 | −45.5 | 3.4, 48 |
| 10 (Rs 1 lakh) | +0.12 / +0.23 / +0.71 (+0.9) | 2477 / 5268 / 340 | 1.30 | +1.64 | −39.5 | 6.2, 34 |
| 20 (Rs 2 lakh) | +0.23 / +0.04 / +0.98 (+1.5) | 4144 / 3790 / 151 | 2.99 | +2.54 | −38.0 | 10.4, 20 |
| none (Rs 13.7 lakh) | +0.79 / +0.20 / +1.25 (+4.0) | 8085 / 0 / 0 | 20.03 | +2.39 | −10.8 | 20.4, 0 |

## Against the benchmarks (book less B; 95% interval from the month bootstrap)

| Book | Period | B1 / B2 / B3 end (lakh) | less B1, % a year | less B2, % a year | less B3, % a year | less B3, Rs |
|---|---|---|---|---|---|---|
| K 10 PRODUCT | TUNE | 4.01 / 1.53 / 2.23 | −9.04 [−16.65 to −1.37] | −1.69 [−6.12 to +3.05] | −4.51 [−8.59 to −0.45] | −102,147 [−209,625 to −12,093] |
| K 10 PRODUCT | CHOOSE | 1.27 / 1.24 / 1.25 | −3.55 [−29.34 to +21.44] | −0.52 [−12.51 to +14.47] | −1.91 [−10.11 to +8.60] | −2,308 [−12,206 to +10,433] |
| K 10 PRODUCT | TEST | 1.21 / 1.22 / 1.22 | +7.28 [−39.22 to +34.64] | +6.73 [+1.73 to +12.32] | +6.21 [+2.15 to +10.72] | +7,613 [+2,616 to +13,022] |
| K 10 PRODUCT | ALL | 4.15 / 1.54 / 2.27 | −7.64 [−15.83 to −0.48] | −1.08 [−5.09 to +2.88] | −3.59 [−7.25 to −0.04] | −96,841 [−203,838 to −1,677] |
| K 5 | TUNE / CHOOSE / TEST | | | | −4.90 / −9.12 / +5.67 | |
| K 5 | ALL | 2.08 / 0.83 / 1.17 | −8.35 [−17.81 to +0.21] | −2.26 [−8.26 to +3.92] | −4.54 [−10.11 to +1.20] | −59,427 [−1,24,267 to +25,164] |
| K 20 | TUNE / CHOOSE / TEST | | | | −2.64 / −1.62 / +3.09 | |
| K 20 | ALL | 8.31 / 2.98 / 4.16 | −6.74 [−14.52 to +0.32] | +0.03 [−2.99 to +3.15] | −2.13 [−4.63 to +0.34] | −1,16,758 [−2,58,700 to +23,847] |
| no cap | TUNE / CHOOSE / TEST | | | | −0.51 / −0.60 / +0.47 | |
| no cap | ALL | 56.89 / 17.53 / 21.48 | −6.88 [−15.03 to +1.44] | +0.85 [−0.03 to +1.70] | −0.45 [−1.00 to +0.08] | −1,44,990 [−3,21,632 to +31,256] |

The other orders against B3 over ALL, % a year: DEEP −3.81 [−7.85 to +0.26], SHALLOW −5.02 [−9.05
to −1.22], LIQUID −7.15 [−11.93 to −2.72], CALM −4.03 [−7.70 to −0.23]. In TEST every order but
SHALLOW, LIQUID and CALM is ahead of B3 with an interval above zero (PRODUCT +6.21, DEEP +7.76).

## What the cap refused (K = 10, PRODUCT order)

| Trades | n | % a trade ALL (t) | TUNE / CHOOSE / TEST | Win % |
|---|---|---|---|---|
| held | 2477 | +0.14 (+0.9) | +0.12 / +0.23 / +0.71 | 64 |
| capped (no free place) | 5268 | +1.09 (+4.3) | +1.10 / +0.03 / +1.85 | 70 |
| refused for cash | 340 | +0.52 (+1.2) | +0.48 / +3.69 (4 trades) / none | 67 |

| Signals that evening | Held n | Held % a trade | Capped n | Capped % a trade |
|---|---|---|---|---|
| 1 to 2 | 1140 | −0.16 | 283 | −0.58 |
| 3 to 9 | 1155 | +0.19 | 2180 | +0.23 |
| 10 or more | 182 | +1.72 | 2805 | +1.92 |

Of 2,048 evenings with a signal, 1,000 had a signal capped. On those the median count of signals
was 4 (mean 6.3) and NIFTY's close stood a median 2.1% below its 20-session high; on the others 1
(mean 1.7) and 0.5%. The refused for cash are mostly the years after a loss (2012 to 2014, 2019
to 2023) and the shares over Rs 10,000 (MRF 10 times).

## Power for the paper test (t = 2 from the mean and the spread of single trades)

| Trades | Basis | Trades a month | Mean % | SD % | Trades for t = 2 | Months |
|---|---|---|---|---|---|---|
| capped book, K = 10 | whole span | 12.9 | +0.14 | 6.36 | 8,216 | 639 |
| capped book, K = 10 | TEST | 4.7 | +0.71 | 6.49 | 331 | 71 |
| tier all | whole span | 42.0 | +0.77 | 6.46 | 278 | 7 |
| tier all | TEST | 8.8 | +1.25 | 6.22 | 99 | 11 |

Trades of one dip move together, so these months are a floor, not an estimate.

## The seven answers

1. **What Rs 1 lakh became.** Under the product's rule (K = 10, PRODUCT order) Rs 1.30 lakh in 16
   years, +1.64% a year, with a fall of −39.5% from Jan 2018 to Sep 2020 and −21.2% in 2018. B1
   became Rs 4.15 lakh (+9.3% a year), B2 Rs 1.54 lakh, B3 Rs 2.27 lakh. Dividends are in none.
2. **Ahead of B3 or B2 beyond chance?** No. Over the whole span the book is BEHIND B3 beyond
   chance: −3.59% a year [−7.25 to −0.04], −Rs 96,841. Against B1 −Rs 2,85,509 [−12,09,100 to
   −10,725]. Against B2 −Rs 24,049 [−94,567 to +1,06,223], −1.08% a year [−5.09 to +2.88]: not
   shown. In TEST alone it is ahead of both (+6.21 and +6.73% a year, intervals above zero) on
   56 trades in 12 months, a period already looked at in earlier studies; CHOOSE: not shown.
3. **A better order?** None by the fixed rule (beyond the 97.5th percentile of RANDOM in TUNE and
   above its median in CHOOSE and TEST), on % a trade or on growth. PRODUCT stands at the 67th
   and 64th percentile in TUNE and above the median in CHOOSE (99, 100) and TEST (87, 85). The
   product's order is as good as any tested and should stay. LIQUID (0th percentile) and
   SHALLOW (3rd to 5th) are worse than chance: the big liquid name or the shallow dip hurts.
4. **Which cap?** No change. Against B3 in % a year K = 20 is better than K = 10 in TUNE (−2.64 to
   −4.51) and CHOOSE (−1.62 to −1.91) but worse in TEST (+3.09 to +6.21); no cap the same pattern
   (−0.51 / −0.60 / +0.47); K = 5 worse in TUNE. None beats K = 10 in all three periods.
5. **What the cap costs.** Nearly all of the edge. The held trades made +0.14% a trade (t 0.9),
   the 5,268 capped +1.09% (t 4.3). The book fills on single signals of quiet evenings (−0.16%
   when 1 to 2 signals come) and is full when a broad dip brings many at once (10 or more in an
   evening: +1.92% for the capped ones), with NIFTY a median 2.1% below its 20-session high.
6. **What to expect from the paper test.** About 13 capped trades a month on the span, 4.7 a
   month in the last year. At the capped mean of +0.14% a trade (SD 6.4) a t of 2 needs about
   8,200 trades, 639 months; at TEST's +0.71%, 71 months. Tier all, about 42 a month (8.8 in the
   last year), needs about 278 trades, 7 months at +0.77% (11 at TEST's rate). The capped tier
   cannot be told from zero in a paper test of any practical length; tier all might, in about
   7 to 11 months, if the size holds.
7. **What this cannot tell.** The 222 names are today's survivors (B3 shares that flattery, B1
   and B2 do not); prices are Dhan's adjusted bars with five repairs; fills at the open are
   assumed, with 0.03% slippage; no dividends in any leg (they would help B1 most, as it is
   always fully invested); no taxes; idle cash earns nothing; the start date (the first signal,
   19 Oct 2010) is chosen by the data; the cash rule refuses 340 trades a real owner might have
   topped up for.

## Doubts
The cash rule moves the book (places only +0.19% a trade, with it +0.14). TEST's intervals come
from 12 months resampled. B2 and B3 hold the exposure of the close before, close to close, so the
book's entry day (open to close) and exit day (close to open) are not matched exactly.

## Not tested
A place held back for evenings with many signals; sizing by the book's value or topping up; a
waiting list for capped signals; interest on idle cash; dividends; taxes; caps other than 5, 10,
20 and none; orders other than the six.

## Reproduce

From the worktree; inputs read-only by absolute path, outputs in the main checkout's ignored
`data/research/pullback_book_2026-11-25/` (`cross.txt`, `books.json`, `random.pkl`, `report.md`).

    nice -n 10 .venv/bin/python scripts/pullback_book.py --stage prep
    PYTHONPATH=src nice -n 10 .venv/bin/python scripts/pullback_book_product.py \
        --daily MAIN/data/research/swing_2026-11-24/daily.sqlite --nifty-store MAIN/data/onelot.db \
        --nifty-csv MAIN/data/research/alpha_2026-11-12/daily_universe/NIFTY.csv \
        --out MAIN/data/research/pullback_book_2026-11-25/product_trades.csv
    for s in cross books random report; do
        nice -n 10 .venv/bin/python scripts/pullback_book.py --stage $s; done
    .venv/bin/python -m pytest -q tests/delivery/test_pullback_book.py

Backtests are evidence about a rule, never advice to buy or sell.
