# Option setups S1 to S7 and the long-volatility test (US-092), 2026-11-24

Script `scripts/option_setups.py`; outputs (`cells.csv`, `placebo.csv`, `report.txt`) in
`data/research/option_setups_2026-11-24/` of the main checkout. Read only; no Dhan request.
Three numbers in a row are TUNE / CHOOSE / TEST; % a trade is of the premium paid.

## The answer in five lines

1. Nothing passes. 355 cells, 280 examined, 35 discovered, 3 confirmed, 0 passed; luck alone
   gives 24.0 / 2.65 / 0.15 (placebo) and 1.68 / 0.27 / 0.13 (arithmetic).
2. All 35 discoveries are S3 on the MODEL, and the model is wrong for a pair of options held
   intraday: on real premiums 32 of them lose and the best earns +4.33% (t +0.5).
3. Long volatility: the buyer of both at-the-money options did not earn on any day type. Pooled
   09:20 to 15:20, −1.13% a trade (t −2.0) on 2,801 priced trades; best day type +0.31% (t +0.1).
4. India VIX: it tells the SIZE of the next move (|open to close| with the previous VIX level,
   r +0.29), not its direction, and not what a straddle earns (r −0.03); a straddle earns only
   on days VIX itself rises during the session, which is not known at 09:20.
5. S1 (last half hour) is an AVOID in every index and variant; S2, S4, S5 and S6 are REJECTED.

## What was fixed in advance

- The story's setups, variants, day types and exits, the rules of `RESEARCH-RULES.md`, and the
  lead's note: the "always" cuts of S1 and S6 are not re-run as findings. ALPHA_SEARCH_2026-11-12
  already has S1 always: +0.3 bp of index, −1.46% of premium a trade (t −10.6); here it is only a
  check (−1.67% model). S1 cells are the conditional variants (b, size filter, both).
- NIFTY, BANKNIFTY, FINNIFTY, SENSEX options, each and pooled (no MIDCPNIFTY). Cell = index or
  pool x setup variant x condition; examined with 60 TUNE trades on 30 days.
  Discovered on the model (`h.pnl`, IV from the PREVIOUS session's VIX x 1.15, no look-ahead).
- Real readings, two kinds, both from `data/expired_options.db` (SQLite `mode=ro`):
  (a) S1, S2, S5, S6: the model's own contract (0.65 delta strike, near monthly, next monthly
  inside 3 days of expiry) priced like `real_options.RealPricer`, where the store holds its bars;
  elsewhere "model less 1.0 point" (rule section 3).
  (b) S3, S4: the near monthly contract (no roll, so on EXPIRY days it expires that day) at the
  strike Dhan calls at-the-money at the entry minute. The store holds the at-the-money CALL of
  almost every minute; its price gives that minute's implied volatility; both legs are priced
  at it (forward = index x exp((6.5% − 1.2%) T)), so the PUT is the call's put-call parity
  value, not its own print. A volatility outside 0.25 to 4 times the model's is a bad print and
  is dropped (0.1% of minutes on NIFTY and BANKNIFTY, 1.3% and 2.3% on FINNIFTY and SENSEX).
- Expiry days come from the store (the near call's time value after 15:00 falls under 0.10% of
  the index only on its last day), else the weekday of the nearest found expiry: 48 / 52 / 34 /
  22 of 61 months found for NIFTY / BANKNIFTY / FINNIFTY / SENSEX. Only MONTHLY expiries.
- No look-ahead, proven: S1, S2, S4, S6 signals recomputed on data cut right after the signal
  bar, 60 of 60 equal on NIFTY and on SENSEX; size state and NR7 recomputed on daily bars cut
  before the day, 60 of 60 equal on each.
- Placebo: labels shifted by a random number of sessions in index and year (20 runs); S1, S5, S6
  also get a random side.

## S1: last-half-hour momentum (enter 15:00, exit 15:15)

| cell | trades | model % (t) | TUNE / CHOOSE / TEST | real like for like (priced) | status |
|---|---|---|---|---|---|
| POOL, b (14:30 to 14:59 agrees) | 2506 | −1.36 (−6.1) | −1.04 / −1.86 / −1.87 | −2.17 (25%) | AVOID |
| POOL, size filter | 2329 | −1.89 (−9.0) | −2.08 / −1.95 / −1.34 | −2.73 (28%) | AVOID |
| POOL, b and size filter | 1183 | −1.62 (−5.9) | −1.48 / −2.03 / −1.58 | −2.42 (29%) | AVOID |

All 15 S1 cells are AVOID (TUNE t −2.5 to −7.3, negative in CHOOSE and TEST). Fifteen minutes of
an option costs its spread and charges; the last half hour does not move enough to pay them.

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
−2.79), FINNIFTY −1.58%, SENSEX −1.72%. The 35 model discoveries (all S3) read on real premiums
first: POOL 09:20 15:20 ALL −1.13; ACTIVE −2.20; CALM −0.11; EXPIRY −0.91; PRE-EXPIRY +0.31 (t
+0.1); the 3 confirmed: 09:20 12:00 EXPIRY −10.77, 09:20 stop ALL −2.19, 09:20 stop EXPIRY −13.72.
Selected on the real reading itself: 150 cells examined, 0 discovered, 31 AVOID (every exit at
12:00 and the −30% / +60% exit lose in ALL, ACTIVE, CALM and VIX-UP; for example POOL 09:45 12:00
ALL −1.97%, t −6.8, −1.79 / −1.84 / −2.53). Answer: the buyer of volatility pays on every day type
known before the open; the half-day pair and the stop / take-profit pair pay the most. REJECTED.

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

S2 15m pooled loses Monday to Thursday (−0.47 to −1.35%) and earns on Friday +1.65% (real
+0.87%), the Friday effect the earlier note found and called chance. S3 on real premiums by days
to the monthly expiry: 0 days −0.91%, 1 day +1.01%, 2 to 5 −1.04%, 6 to 10 −1.30%, 11 or more
−1.28%. S5 calls on VIX-UP: Thursday −5.9%, Friday +6.9%.

## India VIX through the correlation lens

- Next-day size: |open to close| correlates with the previous VIX level (+0.29) and hardly with
  its change (+0.03); the signed return with the change +0.08. Direction is not in VIX.
- The real straddle result: with the previous VIX level −0.03, with its change −0.02, with the
  index move 09:20 to 15:20 +0.46, with VIX's own change 09:20 to 15:20 +0.29. On days VIX rose
  more than 5% during the session the pair made +23.7% (n 162); on days it fell 0 to 5% it lost
  −4.4% (n 1,591) and on days it fell more than 5% −7.8% (n 222). By previous VIX level the pair
  loses in four of five quintiles; the index moves more when VIX is high (38 bp in the lowest
  quintile, 76 in the highest), and the premium already charges for it.

## Counts against luck

| | examined | discovered | confirmed | passed | AVOID |
|---|---|---|---|---|---|
| this study (model selection) | 280 | 35 | 3 | 0 | 18 |
| placebo, mean of 20 (max) | 280 | 24.0 (29) | 2.65 (4) | 0.15 (2) | |
| arithmetic | | 1.68 | 0.27 | 0.13 | |
| S3 and S4 on the real reading | 152 | 0 | 0 | 0 | 31 |

The model count is above arithmetic luck because the model is biased for S3 on every day (the
placebo, which cannot shift ALL cells, gets 24). By setup: S1, S2, S4, S6 0 discovered (placebo
0), S5 0 (0.05), S3 35 / 3 / 0 (23.95 / 2.65 / 0.15). No real count exceeds luck's.

## What I could not do, and what I doubt

- The store holds MONTHLY contracts only, and only what earlier studies fetched. Near monthly
  at-the-money call, days with bars, 2022 / 2023 / 2024 / 2025 / 2026: NIFTY 210 / 206 / 207 /
  213 / 183; BANKNIFTY 207 / 210 / 205 / 222 / 169; FINNIFTY 116 / 141 / 149 / 127 / 116; SENSEX
  0 / 94 / 135 / 142 / 120. At-the-money PUTS are almost absent (2 to 6 days an index).
- So "real premiums of both legs" for S3 and S4 could not be met: the put is the call's parity
  value at the same minute. Its level depends on an assumed dividend yield (1.2%); its change
  over a day hardly does. Strikes away from the money take the at-the-money volatility (no smile).
- Real priced share of the exact contract: S1 27%, S2 16%, S5 6%, S6 14% of pooled trades (puts
  and FINNIFTY, SENSEX are rarely stored); the rest is model less 1.0 point.
- No weekly contracts in the store: EXPIRY is the monthly one; a filled month may be a day off.

## Rows for the setup book

| Instrument | Kind | Setup and preset | Timeframe | Condition | Exit | Trades a year | % a trade TUNE / CHOOSE / TEST (t whole span) | Real-premium or cost note | Status | Remark |
|---|---|---|---|---|---|---|---|---|---|---|
| 4 indices, pool | option same-day | S1 last-half-hour momentum, b | 1m | 14:30 to 14:59 agrees | 15:15 | 500 | −1.04 / −1.86 / −1.87 (−6.1) | real like for like −2.17 | AVOID | loses in every index and variant |
| 4 indices, pool | option same-day | S1, size filter | 1m | first return above its 60-day median | 15:15 | 470 | −2.08 / −1.95 / −1.34 (−9.0) | real −2.73 | AVOID | larger first moves lose more |
| 4 indices, pool | option same-day | S2 opening range 30m | 1m | gap over 0.5 sd | ratchet, 15:15 | 300 | +1.50 / −1.97 / −1.57 (+0.5) | real −0.38 | REJECTED | best of 50 cells, TUNE t +1.5 |
| 4 indices, pool | option pair same-day | S3 long straddle 09:20 | 1m | any day type | 15:20 | 990 | real −0.33 / −2.12 / −1.99 (−2.0) | model +2.43 is a model artefact | REJECTED | two lots; pays on every day type |
| 4 indices, pool | option pair same-day | S3 long straddle 09:45 | 1m | any | 12:00 | 590 | real −1.79 / −1.84 / −2.53 (−6.8) | real reading, put by parity | AVOID | morning IV fall costs the most |
| 4 indices, pool | option pair same-day | S3 long straddle 09:20 | 1m | any | −30% / +60%, 15:20 | 570 | real −1.95 / −2.24 / −2.71 (−5.2) | real reading | AVOID | the stop locks in the IV fall |
| 4 indices, pool | option pair same-day | S3 long straddle 09:20 | 1m | VIX-UP | 12:00 | 80 | real −3.81 / −2.65 / −4.25 (−4.5) | real reading | AVOID | after a VIX jump IV keeps falling |
| 4 indices, pool | option same-day | S4 expiry afternoon 14:30, 0.25 ATR | 1m | monthly EXPIRY | 15:15 or −40% | 30 | real +2.70 / +109.6 / −16.7 (+0.7) | model unusable on expiry day | REJECTED | few days, carried by a few winners |
| 4 indices, pool | option same-day | S5 call after VIX-UP | 1m | VIX rose over 5% | ratchet, 15:15 | 140 | +1.49 / −3.20 / +0.85 (+0.5) | real −0.27 | REJECTED | TUNE t +0.8 |
| 4 indices, pool | option same-day | S5 put after VIX-DOWN | 1m | VIX fell over 5% | ratchet, 15:15 | 120 | −2.15 / −6.82 / +0.03 (−1.9) | real −3.40 | REJECTED | falls after VIX drops are not there |
| 4 indices, pool | option same-day | S6 high turnover first half hour | 1m | ratio over 1.2 | ratchet, 15:15 | 270 | +0.43 / −1.02 / −3.25 (−0.9) | real −1.62 | REJECTED | bigger range, no direction |
