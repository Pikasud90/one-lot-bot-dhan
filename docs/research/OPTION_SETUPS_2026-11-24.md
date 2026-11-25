# Option setups S1 to S7 and the long-volatility test (US-092), 2026-11-24

Script `scripts/option_setups.py`; outputs in `data/research/option_setups_2026-11-24/` of the
main checkout. Read only; no Dhan request. Three numbers in a row are TUNE / CHOOSE / TEST.

## The answer in five lines
1. Nothing passes. 355 cells, 280 examined, 35 discovered, 3 confirmed, 0 passed; luck alone
   gives 24.0 / 2.65 / 0.15 (placebo) and 1.68 / 0.27 / 0.13 (arithmetic).
2. All 35 discoveries are S3 on the MODEL, and the model is wrong for a pair of options held
   intraday: on real premiums 32 of them lose and the best earns +4.33% (t +0.5).
3. Long volatility: the buyer of both at-the-money options did not earn on any day type. Pooled
   09:20 to 15:20, −1.13% a trade (t −2.0) on 2,801 priced trades; best day type +0.31% (t +0.1).
   Settled on real puts of NIFTY and BANKNIFTY (25 Nov): the buyer paid −2.61% a pair (t −5.2,
   95% −3.59 to −1.63) on 2,444 trades, worse than the parity put said (section at the end).
4. India VIX: it tells the SIZE of the next move (|open to close| with the previous VIX level,
   r +0.29), not its direction, and not what a straddle earns (r −0.03); a straddle earns only
   on days VIX itself rises during the session, which is not known at 09:20.
5. S1 (last half hour) is an AVOID in every index and variant; S2, S4, S5 and S6 are REJECTED.

## What was fixed in advance
- The story's setups, variants, day types and exits, the rules of `RESEARCH-RULES.md`, and the
  lead's note: the "always" cuts of S1 and S6 are not findings (ALPHA_SEARCH_2026-11-12 has S1
  always: −1.46% a trade, t −10.6; here −1.67%). S1 cells: b, size filter, both.
- NIFTY, BANKNIFTY, FINNIFTY, SENSEX options, each and pooled (no MIDCPNIFTY); cell = index or
  pool x setup variant x condition; discovered on the model (`h.pnl`, IV from the PREVIOUS
  session's VIX x 1.15, no look-ahead). Real readings from `data/expired_options.db` (`mode=ro`):
  (a) S1, S2, S5, S6: the model's own contract priced like `real_options.RealPricer` where the
  store holds its bars, else "model less 1.0 point" (rule section 3).
  (b) S3, S4: the near monthly contract (no roll) at Dhan's at-the-money strike. The store holds
  the at-the-money CALL of almost every minute; its price gives that minute's implied volatility;
  both legs are priced at it (forward = index x exp((6.5% − 1.2%) T)), so the PUT is the call's
  put-call parity value, not its own print. A volatility outside 0.25 to 4 times the model's is
  a bad print and is dropped (about 0.15% of NIFTY and BANKNIFTY minutes; see the check).
- Expiry days come from the store (the near call's time value after 15:00 falls under 0.10% of
  the index only on its last day), else the weekday of the nearest found expiry: 48 / 52 / 34 /
  22 of 61 months found for NIFTY / BANKNIFTY / FINNIFTY / SENSEX. Only MONTHLY expiries.
  One filled date is a day early (NIFTY 2025-05, see the check).
- No look-ahead, proven: S1, S2, S4, S6 signals recomputed on data cut after the signal bar, 60
  of 60 equal (NIFTY, SENSEX); size state and NR7 on daily bars cut before the day, 60 of 60.
- Placebo: labels shifted by random sessions in index and year (20 runs); S1, S5, S6 random side.

## S1: last-half-hour momentum (enter 15:00, exit 15:15)
| cell | trades | model % (t) | TUNE / CHOOSE / TEST | real like for like (priced) | status |
|---|---|---|---|---|---|
| POOL, b (14:30 to 14:59 agrees) | 2506 | −1.36 (−6.1) | −1.04 / −1.86 / −1.87 | −2.17 (25%) | AVOID |
| POOL, size filter | 2329 | −1.89 (−9.0) | −2.08 / −1.95 / −1.34 | −2.73 (28%) | AVOID |
| POOL, b and size filter | 1183 | −1.62 (−5.9) | −1.48 / −2.03 / −1.58 | −2.42 (29%) | AVOID |

All 15 S1 cells are AVOID (TUNE t −2.5 to −7.3, negative in CHOOSE and TEST): the last half hour
does not move enough to pay the spread and charges of 15 minutes of an option.

## S2: opening range, then one option, the desk's ratchet exit
50 cells examined, 0 discovered. Best TUNE: POOL 30m GAP +1.50% (t +1.5), then −1.97 / −1.57;
real −0.38%. POOL 15m ALL −0.46% (−0.34 / −0.74 / −0.54), real −1.33%. NR7 TEST is positive
in six of eight index cells (+0.2 to +4.0) while its TUNE is at most +0.52 (t +0.4). REJECTED.

## S3: long volatility (call and put of one strike, TWO lots: a question for the owner)
The model says a pair bought at 09:20 and sold at 15:20 earns +2.43% a trade (t +4.3) on every
day. The real premiums say −1.13% (t −2.0). Why: the model keeps the volatility of the day fixed
and charges time value in calendar hours, so six of 24 hours; the market's at-the-money
volatility falls through the session (BANKNIFTY 09:20 to 15:20: −3.7% on average over 1,002
days, at a mean 18.3% against the model's 17.3%), and that alone takes about 3.5 points of the
pair. On expiry days the model reads +41% a trade; the real reading is −0.9%.

Pooled, held 09:20 to 15:20 (what the buyer of volatility earned or paid), % a trade:

| day type | model (n) | real (t, n) | index move 09:20 to 15:20 |
|---|---|---|---|
| ALL | +2.43 (4940) | −1.13 (−2.0, 2801) | 55 bp |
| ACTIVE-EXPECTED | +2.32 (2446) | −2.20 (−2.9, 1364) | 58 bp |
| CALM-EXPECTED | +2.54 (2494) | −0.11 (−0.1, 1437) | 52 bp |
| NR7 | +4.15 (762) | −0.52 (−0.3, 441) | 53 bp |
| VIX-UP | +4.06 (704) | −2.39 (−1.5, 396) | 66 bp |
| VIX-DOWN | +4.05 (620) | −0.02 (−0.0, 355) | 65 bp |
| EXPIRY | +41.26 (240) | −0.91 (−0.1, 125) | 55 bp |
| PRE-EXPIRY | +12.21 (240) | +0.31 (+0.1, 157) | 61 bp |

Per index, real, ALL: NIFTY −0.57% (−0.26 / −0.51 / −1.54), BANKNIFTY −1.30% (−0.11 / −3.23 /
−2.79), FINNIFTY −1.58%, SENSEX −1.72%. The 3 confirmed model cells read on real premiums: 09:20
12:00 EXPIRY −10.77, 09:20 stop ALL −2.19, 09:20 stop EXPIRY −13.72. Selected on the real reading
itself: 150 cells examined, 0 discovered, 31 AVOID (every 12:00 exit and the −30% / +60% exit lose
in ALL, ACTIVE, CALM and VIX-UP; POOL 09:45 12:00 ALL −1.97%, t −6.8, −1.79 / −1.84 / −2.53).
Answer: the buyer of volatility earns on no day type known before the open (the check below
doubts the size of the loss, not any profit); the half-day and stop pairs pay most. REJECTED.

## S4: expiry-day afternoon trend (near monthly, at the money)
Only 12 monthly expiries a year per index: 3 of 30 cells examined (the pools). The model is
useless here (+142% a trade on BANKNIFTY 14:30). Real: POOL 14:30 0.25 +22.2% (t +0.7, 100 priced,
18% winners; +2.70 / +109.6 / −16.7), POOL 13:30 0.25 −8.7%, 14:00 0.25 −4.5%. REJECTED.

## S5: after a VIX shock (09:20, the desk's exit)
| cell | trades | model % (t) | TUNE / CHOOSE / TEST | real like for like (priced) |
|---|---|---|---|---|
| POOL call on VIX-UP | 704 | +0.64 (+0.5) | +1.49 / −3.20 / +0.85 | −0.27 (9%) |
| POOL put on VIX-DOWN | 617 | −2.22 (−1.9) | −2.15 / −6.82 / +0.03 | −3.40 (4%) |

TUNE t at most +0.9 in any index. Plain fact, index open to close (four indices) by decile of
the previous day's VIX change: the top decile (+6.2% to +66%) is +14.2 bp (t +1.8) and the
bottom decile (−26% to −5.6%) −10.3 bp (t −1.2); the eight between swing from −7.8 to +6.9 with
no order. The size is clearer: |open to close| 70 and 73 bp in the two outer deciles against 52
to 61 in the middle. REJECTED.

## S6: volume-confirmed first half hour (09:45, the desk's exit)
POOL HIGH (turnover ratio above 1.2): −0.66% (+0.43 / −1.02 / −3.25), real −1.62%. POOL LOW
(under 0.8): −1.22% (−2.31 / −1.27 / +1.37), real −2.10%. Plain fact by quintile of the ratio:
the rest-of-day range rises step by step, 87, 92, 96, 101, 105 bp; the return in the first half
hour's direction is −0.1, −1.0, +3.2, +8.2 (t +2.2), −0.2 bp: no order. REJECTED.

## S7: by weekday and days to expiry (descriptive, never selected)
S2 15m pooled loses Monday to Thursday (−0.47 to −1.35%), earns Friday +1.65% (real +0.87%; the
earlier note called it chance). S3 real by days to expiry: 0 −0.91%, 1 +1.01%, 2 to 5 −1.04%, 6 to
10 −1.30%, 11 or more −1.28%. S5 calls on VIX-UP: Thursday −5.9%, Friday +6.9%.

## India VIX through the correlation lens
- Next-day size: |open to close| correlates with the previous VIX level (+0.29) and hardly with
  its change (+0.03); the signed return with the change +0.08. Direction is not in VIX.
- The real straddle result: with the previous VIX level −0.03, with its change −0.02, with the
  index move 09:20 to 15:20 +0.46, with VIX's own change 09:20 to 15:20 +0.29. On days VIX rose
  more than 5% in the session the pair made +23.7% (n 162); fell 0 to 5% −4.4% (n 1,591); fell
  more than 5% −7.8% (n 222). By previous VIX level the pair loses in four of five quintiles;
  the index moves more when VIX is high (38 bp lowest quintile, 76 highest); the premium charges it.

## Counts against luck
| | examined | discovered | confirmed | passed | AVOID |
|---|---|---|---|---|---|
| this study (model selection) | 280 | 35 | 3 | 0 | 18 |
| placebo, mean of 20 (max) | 280 | 24.0 (29) | 2.65 (4) | 0.15 (2) | |
| arithmetic | | 1.68 | 0.27 | 0.13 | |
| S3 and S4 on the real reading | 152 | 0 | 0 | 0 | 31 |

The model count is above arithmetic luck because the model is biased for S3 on every day (the
placebo, which cannot shift ALL cells, gets 24): S3 35 / 3 / 0 against 23.95 / 2.65 / 0.15, every
other setup 0 (placebo 0 to 0.05). No real count exceeds luck's.

## What I could not do, and what I doubt
- The store holds MONTHLY contracts only, what earlier studies fetched. Days with near monthly
  at-the-money call bars, 2022 / 2023 / 2024 / 2025 / 2026: NIFTY 210 / 206 / 207 / 213 / 183;
  BANKNIFTY 207 / 210 / 205 / 222 / 169; FINNIFTY 116 / 141 / 149 / 127 / 116; SENSEX 0 / 94 /
  135 / 142 / 120. At-the-money PUTS: 2 to 6 days an index.
- So "real premiums of both legs" for S3 and S4 could not be met: the put is the call's parity
  value at the same minute. Its level depends on an assumed dividend yield (1.2%); its change
  over a day hardly does. Strikes away from the money take the at-the-money volatility (no smile).
- Priced share of the exact contract: S1 27%, S2 16%, S5 6%, S6 14% (puts, FINNIFTY, SENSEX are
  rarely stored); the rest is model less 1.0 point. No weekly contracts: EXPIRY is the monthly one.

## Independent check (own code `scripts/option_setups_check.py`, 2026-11-25)
- Agree: counts 355 / 280 / 35 / 3 / 0 and 152 / 0 (real) recomputed from the cell numbers; S1 b
  NIFTY −1.34% (t −6.0; −1.07 / −1.63 / −1.89), BANKNIFTY −1.38% (t −5.1). VIX lists equal the
  harness series (704 and 620 index-days), no look-ahead (80 of 80); the stored daily bars give 652
  and 576, and S3 on them moves VIX-UP −1.98 to −1.54, VIX-DOWN −0.03 to +0.14: nothing.
- S3 pair, own calendar (store-found expiry months only): −0.72% (t −1.2, 1,820 trades) on four
  indices, −0.78% (t −1.4) on NIFTY and BANKNIFTY; +0.5 / −2.7 / −2.9 by period; yield 0% or 2%
  gives −0.59 or −0.81. The sign agrees, the t is weaker than −2.0.
- The put decides it. Real and parity put of one strike: 12 days, 4,334 minutes; the parity put is
  a median +13% off (IQR −3 to +26%; NIFTY −19%, BANKNIFTY +23%). On 10 days with both ends the pair
  made −18.5% (real put) and −20.1% (parity put), sd 5.1. A put 5% dearer at entry and 5% cheaper
  at exit gives −5.1%; the reverse +3.9%. So "the buyer paid" is NOT shown (zero is possible); the
  model's +2.43% is refuted either way. A flat smile understates the exit by about 0.5 point.
- Volatility fall 09:20 to 15:20: BANKNIFTY −3.9% (Dhan's own field −3.1%), NIFTY −1.8%: agree.
- Bad prints: NIFTY 0.18%, BANKNIFTY 0.15% agree; FINNIFTY 3.6%, SENSEX 9.6% do not (1.3%, 2.3%).
  Filter off: NIFTY and BANKNIFTY stay −0.98%; the four-index pool turns +1.17% (t +0.6).
- Expiries filled by weekday: 13 / 9 / 27 / 39 months. NIFTY 2022-02 and 2026-08, BANKNIFTY
  2022-08 are right; NIFTY 2025-05 (Wed 28) is a day early (real Thursday 29): three S3 rows. All
  116 priced S4 days sit on store expiries (two borderline at 0.26% and 0.155% time value).

## Settled on real puts (25 Nov)
US-098: `scripts/real_puts_fetch.py`, `long_vol_real.py`, `data/research/real_puts_2026-11-25/`.
NIFTY and BANKNIFTY; both legs are their OWN real prints of the strike bought, at both ends.
- Fetch: 11,079 requests by the app's adapter (3 before 15:40, then one a second to 19:55); none
  refused, 3 failed in a network outage (17:38 to 17:48; stopped, resumed). At-the-money put and
  call on 1,237 of 1,238 sessions of each index, and the strikes sold (up to ten steps away).
- Checks: median 375 minutes a day; 09:20 put strike within half a step of Dhan's spot on 1,235
  of 1,236 and 1,233 of 1,234 days; bad prints 0.26% and 0.17%; 8,770 of 9.8 million bars had
  another index's spot (dropped); 302 day blocks the store held came back identical.
- Left out: 32 of 2,476 sessions at 09:20 to 15:20, 17 with the strike over ten steps away at
  15:20 (Dhan serves no further; at intrinsic value they move −2.61 to −2.66). The −30% / +60%
  rule watches real closes of both legs 81% of the minutes, else the minute's real at-the-money
  volatility; its fills are real.

| cell (both legs real) | trades | % a pair (t) | win | TUNE / CHOOSE / TEST | parity put before | status |
|---|---|---|---|---|---|---|
| POOL 09:20 15:20 ALL | 2444 | −2.61 (−5.2) | 26% | −1.77 / −4.44 / −3.30 | −1.15 same trades, −0.94 all | REJECTED |
| NIFTY 09:20 15:20 ALL | 1229 | −2.06 (−3.8) | 27% | −1.30 / −3.15 / −3.26 | −0.57 | REJECTED |
| BANKNIFTY 09:20 15:20 ALL | 1215 | −3.17 (−5.2) | 24% | −2.25 / −5.72 / −3.35 | −1.30 | AVOID |
| POOL 09:20 15:20 ACTIVE-EXP | 1205 | −3.46 (−6.1) | 25% | −2.53 / −4.74 / −4.94 | −1.52 | AVOID |
| POOL 09:20 15:20 CALM-EXP | 1239 | −1.79 (−2.3) | 26% | −1.04 / −4.15 / −1.70 | −0.37 | REJECTED |
| POOL 09:20 15:20 VIX-UP | 346 | −3.60 (−2.9) | 29% | −3.15 / −6.40 / −2.98 | −2.63 | REJECTED |
| POOL 09:20 15:20 VIX-DOWN | 301 | −3.36 (−3.9) | 25% | −2.86 / −6.93 / −2.80 | −1.27 | AVOID |
| POOL 09:20 15:20 EXPIRY | 116 | −5.70 (−0.7) | 35% | +10.38 / −31.18 / −25.80 | +2.27 | REJECTED |
| POOL 09:20 12:00 ALL | 2463 | −2.09 (−7.3) | 25% | −1.69 / −2.28 / −3.10 | −1.57 | AVOID |
| POOL 09:20 stop ALL | 2446 | −2.82 (−8.0) | 25% | −2.44 / −3.59 / −3.17 | −1.84 | AVOID |
| POOL 09:45 15:20 ALL | 2452 | −2.85 (−6.4) | 24% | −2.09 / −5.05 / −2.95 | −1.36 | AVOID |
| POOL 09:45 12:00 ALL | 2468 | −2.19 (−9.6) | 21% | −1.93 / −2.67 / −2.48 | −1.88 | AVOID |
| POOL 09:45 stop ALL | 2457 | −2.76 (−8.2) | 23% | −2.36 / −3.74 / −2.98 | −1.94 | AVOID |

Counts: 144 cells, 120 examined, 0 discovered / confirmed / passed; luck 0 / 0 / 0 (placebo),
0.72 / 0.12 / 0.06 (arithmetic). 63 AVOID, placebo 59.9 (max 79): the loss belongs to the pair,
not to a kind of day. All cells in `cells.csv` and `report.txt`. The three answers:
1. Yes, the buyer of both options paid, on all days and on every kind of day: pooled 09:20 to
   15:20 −2.61% a pair (95% −3.59 to −1.63), median −3.9%, negative in all three periods. No
   kind of day is positive beyond luck: the best examined cell is NIFTY NR7 +0.48% (t +0.3;
   +1.59 / −0.89 / −1.86); the earlier EXPIRY +2.27% is −5.70% on real puts (t −0.7, 116 trades).
2. The parity put was a median +4.3% off the real put (IQR −5.1 to +14.9%) over 925,258 minutes
   on 1,237 days (dividend yield 1.2%; at 0% the median is −0.3%); at 09:20 +3.1%, at 15:20
   +5.8%. The 12-day sample's +13% was not typical. It biased the answer toward zero: on the
   same 2,014 trades parity read −1.15%, real −1.93% (parity less real +0.78, t +3.7), because
   it priced the put dearer at the exit than at the entry. The rest of the gap is the sample:
   the 430 sessions the old store lacked lose −5.8% a pair; the old store was not a random draw.
3. One leg alone, at the money, 09:20 to 15:20, one lot, real: the call −3.07% (t −2.7, 95% −5.32
   to −0.82; −1.56 / −4.31 / −6.37), the put −2.17% (t −1.6, 95% −4.77 to +0.43; −2.32 / −4.39 /
   +0.51). A buyer without a view pays about 2 to 3% of the premium a day.
Doubts: monthly contracts of two indices only; the earlier expiry calendar; a partly estimated
stop trigger; the model's spread and charges, not measured spreads.

## Rows for the setup book
| Instrument | Kind | Setup and preset | Timeframe | Condition | Exit | Trades a year | % a trade TUNE / CHOOSE / TEST (t whole span) | Real-premium or cost note | Status | Remark |
|---|---|---|---|---|---|---|---|---|---|---|
| 4 indices, pool | option same-day | S1 last-half-hour momentum, b | 1m | 14:30 to 14:59 agrees | 15:15 | 500 | −1.04 / −1.86 / −1.87 (−6.1) | real like for like −2.17 | AVOID | loses in every index and variant |
| 4 indices, pool | option same-day | S1, size filter | 1m | first return above its 60-day median | 15:15 | 470 | −2.08 / −1.95 / −1.34 (−9.0) | real −2.73 | AVOID | larger first moves lose more |
| 4 indices, pool | option same-day | S2 opening range 30m | 1m | gap over 0.5 sd | ratchet, 15:15 | 300 | +1.50 / −1.97 / −1.57 (+0.5) | real −0.38 | REJECTED | best of 50 cells, TUNE t +1.5 |
| NIFTY and BANKNIFTY, pool | option pair same-day | S3 long straddle 09:20 | 1m | any day type | 15:20 | 490 | −1.77 / −4.44 / −3.30 (−5.2) | both legs real (25 Nov); parity read −0.94 | REJECTED | two lots; pays on every day type, worst after an active day |
| NIFTY and BANKNIFTY, pool | option pair same-day | S3 long straddle 09:45 | 1m | any | 12:00 | 490 | −1.93 / −2.67 / −2.48 (−9.6) | both legs real | AVOID | morning IV fall costs the most |
| NIFTY and BANKNIFTY, pool | option pair same-day | S3 long straddle 09:20 | 1m | any | −30% / +60%, 15:20 | 490 | −2.44 / −3.59 / −3.17 (−8.0) | fills real, trigger 81% real | AVOID | the stop locks in the IV fall |
| NIFTY and BANKNIFTY, pool | option pair same-day | S3 long straddle 09:20 | 1m | VIX-UP | 12:00 | 70 | −3.15 / −4.05 / −3.13 (−4.7) | both legs real | AVOID | after a VIX jump IV keeps falling |
| NIFTY and BANKNIFTY, pool | option same-day | at-the-money call alone 09:20 | 1m | any | 15:20 | 490 | −1.56 / −4.31 / −6.37 (−2.7) | real, one lot | REJECTED | a buyer without a view pays about 3% a day |
| NIFTY and BANKNIFTY, pool | option same-day | at-the-money put alone 09:20 | 1m | any | 15:20 | 490 | −2.32 / −4.39 / +0.51 (−1.6) | real, one lot | REJECTED | about 2% a day |
| 4 indices, pool | option same-day | S4 expiry afternoon 14:30, 0.25 ATR | 1m | monthly EXPIRY | 15:15 or −40% | 30 | real +2.70 / +109.6 / −16.7 (+0.7) | model unusable on expiry day | REJECTED | few days, carried by a few winners |
| 4 indices, pool | option same-day | S5 call after VIX-UP | 1m | VIX rose over 5% | ratchet, 15:15 | 140 | +1.49 / −3.20 / +0.85 (+0.5) | real −0.27 | REJECTED | TUNE t +0.8 |
| 4 indices, pool | option same-day | S5 put after VIX-DOWN | 1m | VIX fell over 5% | ratchet, 15:15 | 120 | −2.15 / −6.82 / +0.03 (−1.9) | real −3.40 | REJECTED | falls after VIX drops are not there |
| 4 indices, pool | option same-day | S6 high turnover first half hour | 1m | ratio over 1.2 | ratchet, 15:15 | 270 | +0.43 / −1.02 / −3.25 (−0.9) | real −1.62 | REJECTED | bigger range, no direction |
