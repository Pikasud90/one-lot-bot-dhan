# What the desk's own live alerts say (US-088, 24 November 2026)

## The answer in five lines
1. The live record is small and almost all of it is the old line-up: 153 alerts from 29 Oct to
   23 Nov, 151 of them before 13 Nov; the `microtrend` slot raised 2 alerts, both on 23 Nov.
2. As recorded the paper trades lose: index options −5.3% of premium a trade (23, t −1.4),
   stocks −0.08% of price (127, t −1.3), the 5m mean-reversion slot −0.16% (106, t −6.2).
3. Where the desk ran, it copied the backtest (same bar, side and exit); but it was down from
   16 to 20 Nov and missed 16 of the 17 micro-trend trades of the owner's four indices.
4. NIFTY and BANKNIFTY options are cheaper to fill than modelled (half spread 0.3% against
   0.6%); FINNIFTY and SENSEX dearer (1.5%, with a long tail). A late entry cost nothing.
5. Five hypotheses from the live record, tested on five years: 0 of 9 cells discovered. Nothing
   to change in the rule; the change worth making is in operations and the cost model.

## What was fixed in advance
`scripts/live_alerts_study.py`; outputs in the main checkout's `data/research/live_alerts_
2026-11-24/`. Stores opened read-only; the private tables were never read. The line-up split is at
13 Nov: every slot but pullback was paused on 12 Nov at 11:06 (after that day's 4 alerts) and
`microtrend` ran from 13 Nov. Entry in parts C and D is the open of the minute after the alert.
Model premiums are `h.pnl`'s. Part D's hypotheses were written into the script after parts A
to C were read and before the history stage ran. Three numbers in a row: TUNE / CHOOSE / TEST.

## Part A: what happened
**A1. Inventory.** 153 alerts on 10 days: 127 stock (one share), 26 index options (18 calls,
8 puts). Slots: mean_reversion 5m 106, ema_cross 15m 15, supertrend_vol 5m 9, ema_cross 12m 7,
supertrend_vol 15m 6 and 30m 4, orb 5m 2, supertrend_mtf 5m 2, microtrend 3m 2. Status: 148
expired, 3 taken, 2 skipped. Data: 74 on Dhan bars, 14 on delayed Yahoo bars, 65 unstamped
(reconciled later); grade 52 live, 101 delayed. ADANIGREEN (15) is left out of parts C and D.

| Session | Alerts | Index / stock | Data | Desk state |
|---|---|---|---|---|
| 29 Oct to 5 Nov (6 days) | 144 | 18 / 126 | Dhan and Yahoo | alerts from 09:30 to 14:40 |
| 6 Nov | 1 | 0 / 1 | Yahoo | one alert |
| 9 Nov | 0 | | | no alert, no quote: down or silent |
| 11, 12 Nov | 6 | 6 / 0 | Dhan, live | quotes 09:15 to 15:26 |
| 13 Nov | 0 | | | up all day (quotes 09:15 to 15:29), no micro-trend signal |
| 16 to 20 Nov | 0 | | | no alert, no quote: down |
| 23 Nov | 2 | 2 / 0 | Dhan, regraded delayed | up from 11:07 (token renewed); 2-minute bars until 10:57 |

**A2. Paper trades as recorded** (152 closed, 1 no fill). % of premium (options) or of price
(stocks), t with one entry day as one draw, rupees a trade.

| Group | Trades | Days | % a trade (t) | Winners | Payoff | Rs a trade |
|---|---|---|---|---|---|---|
| Before 13 Nov, all | 150 | 9 | −0.88 (−1.4) | 34.7% | 0.68 | −224 |
| Index options | 23 | 5 | −5.27 (−1.4) | 39.1% | 0.48 | −1,433 |
| Stocks | 127 | 7 | −0.08 (−1.3) | 33.9% | 1.26 | −5.6 |
| mean_reversion 5m (stocks) | 106 | 7 | −0.16 (−6.2) | 30.2% | 0.68 | −5.6 |
| ema_cross 15m (stocks) | 15 | 4 | +0.41 (+1.9) | 60.0% | 1.82 | −4.4 |
| supertrend_vol 5m (options) | 9 | 2 | +1.30 (+0.5) | 66.7% | 0.72 | +642 |
| supertrend_vol 30m (options) | 4 | 2 | −19.65 (−1.7) | 25.0% | 0.05 | −5,653 |
| ema_cross 12m (options) | 6 | 3 | −4.05 (−1.1) | 16.7% | 1.31 | −1,862 |
| Calls / puts | 17 / 6 | | −7.27 / +0.42 | 29% / 67% | | |
| Exit at session end / strategy exit / stop | 36 / 44 / 46 | | +0.64 / −0.67 / −2.57 | 61% / 34% / 0% | | |
| From 13 Nov: microtrend (puts: BANKNIFTY +12.8, MIDCPNIFTY +0.1) | 2 | 1 | +6.48 | 2 of 2 | | +5,457 |


**A3. Excursions** of the managed price (index or share) between fill and exit. Planned move:
the alert's target where it had one (27 trades), else 3 risks (125).

| Kind | Trades | Best / worst (median) | Best / worst in risks | Minutes to best of held | Reached 25 / 50 / 80 / 100% of the move (of those, lost) |
|---|---|---|---|---|---|
| Index, before 13 Nov | 23 | +0.22% / −0.25% | 0.22 / −0.34 | 70 of 215 | 35% (25%) / 13% (67%) / 9% (50%) / 0% |
| Index, from 13 Nov | 2 | +0.62% / −0.15% | 0.15 / −0.03 | 112 of 119 | 0 / 0 / 0 / 0 |
| Stocks | 127 | +0.24% / −0.21% | 0.54 / −0.45 | 10 of 25 | 37% (34%) / 13% (0%) / 6% (0%) / 5% (0%) |

Index trades seldom went a quarter of the way; 2 of 3 that went half way still lost.

**A4. Fill costs** from `fill_quotes` (11, 12, 13 and 23 Nov only; 2,522 option quotes of the
monthly ~0.65-delta call and put, every 5 minutes). Model: half spread 0.6% + 2 ticks a side.

| Index | Quotes | Premium | Half spread Rs median (p90) | Half spread % median (p90) | Model % |
|---|---|---|---|---|---|
| NIFTY | 512 | 478 | 1.19 (22.0) | 0.25 (1.31) | 0.62 |
| BANKNIFTY | 511 | 1,509 | 4.95 (34.8) | 0.31 (1.34) | 0.61 |
| FINNIFTY | 482 | 673 | 9.75 (168.6) | 1.51 (10.07) | 0.61 |
| SENSEX | 510 | 1,514 | 21.73 (679.2) | 1.47 (22.51) | 0.61 |
| MIDCPNIFTY (training) | 507 | 323 | 2.00 (6.4) | 0.60 (1.74) | 0.63 |

The median widens through the day (0.62% before 10:00, 0.77% after 13:00); SENSEX before
10:00: 7.5%. Alerted contracts at the alert: median 0.91% (8); live-chain alerts with bid and
ask: median 0.49% (13), one 17%. Stocks: 0.007% (0.003 to 0.011) against the model's 0.03%.
The model should assume NIFTY and BANKNIFTY about 0.3%, FINNIFTY and SENSEX about 1.5% (p90 10
to 22%), stocks 0.01%. Four sessions only: a proposal.

## Part B: live against the backtest
The product's `microtrend` slot (settings of `config/desk.yaml`, `sim/engine.build_signals`,
120 days of history) on the stored bars of every session from 29 Oct to 23 Nov, one trade at a
time per index, out at the 3 of 7 exit or 15:15. Model premiums.

| | Owner four: trades, % a trade (t) | MIDCPNIFTY | Desk state | Live alerts |
|---|---|---|---|---|
| 29 Oct to 12 Nov (slot not live yet) | 37, +1.10 (+0.3) | 8, +1.6 | | |
| 13 Nov | 0 | 0 | up all day | 0 |
| 16 to 20 Nov | 16, +25.66 (+1.4) | 3, −12.5 | down | 0 |
| 23 Nov | 1 (BANKNIFTY put 13:15) | 0 | up from 11:07 | 2 |
| From 13 Nov, all | 17, +24.82 (+1.4) | 3, −12.5 | | 2 |

Matched: BANKNIFTY 23 Nov, same signal bar (13:12), side and exit (15:15); the desk filled a
minute later (13:16), 3.8 bp of index worse. Missed: 16 trades while the desk was down 16 to 20
Nov. Extra: the MIDCPNIFTY put is not on the stored bars (3 of 7 down at 13:12); the desk's bars
that morning were 2-minute bars until 10:57 (its paper trade was regraded), the likely cause.
In one sentence: on clean one-minute bars the desk is a faithful copy, but it ran on 2 of the 7
sessions since the rule went live, so the live record of the rule is one trade.

## Part C: the same alerts under other rules
22 index alerts on 6 days (no MIDCPNIFTY), 109 stock alerts on 7 days. "less 1.0": the model
less the 1.0 point real premiums ran below it in the micro-trend study. The option store (to
7 Nov) priced 2 of the 22 index alerts: real −4.02% against model −2.24% on those two.

| Rule | Index % a trade (t) | less 1.0 | Winners | Stocks % a trade (t) | Winners |
|---|---|---|---|---|---|
| Paper, as recorded | −5.12 (−1.3) | | 40.9% | −0.08 (−1.2) | 33.9% |
| Recorded exits, model | −3.15 (−0.9) | −4.15 | 45.5% | −0.07 (−1.1) | 35.8% |
| Ratchet (1.5 ATR 60m, 3 R, extend) | −1.90 (−0.8) | −2.90 | 50.0% | −0.04 (−0.3) | 44.0% |
| Out after 15 / 30 / 60 minutes | −2.21 / −2.44 / −1.87 | −3.2 / −3.4 / −2.9 | 27 / 23 / 32% | −0.11 / −0.14 / −0.10 | 28 / 26 / 41% |
| Fade (the opposite side) | +0.45 (+0.1) | −0.55 | 36.4% | −0.26 (−3.8) | 39.4% |
| Entry 1 / 3 / 5 minutes late | −2.71 / −2.80 / −2.60 | −3.7 / −3.8 / −3.6 | 45.5% | −0.07 / −0.10 / −0.12 | 35 / 33 / 30% |
| Entry in the first hour (paper / model) | −9.31 / −6.39 (11 tr) | | 27.3% | +0.23 / +0.17 (12) | 50.0% |
| Middle of the day | +0.26 / +1.67 (8 tr) | | 75.0% | −0.15 / −0.12 (84) | 29.8% |
| Last 90 minutes | −4.13 / −4.14 (3 tr) | | 0% | +0.05 / +0.03 (13) | 46.2% |
| Previous session active / calm (model) | +1.56 (14) / −11.40 (8) | | | −0.14 (47) / −0.02 (62) | |
| India VIX rose / fell in the previous session (model) | −7.82 (4) / +1.50 (12) | | | +0.07 (46) / −0.18 (63) | |

The ratchet stopped 3 of 22 index trades. A minute of delay cost nothing measurable; the
quotes agree: 1, 2 and 5 minutes after 8 alerts the ask was better by a median 0.9%, 0.7% and
0.4% of premium (worse in 1, 1, 2). On this sample no number can be told from chance.

## Part D: hypotheses from the live record, tested on five years
The rule's 3,065 trades on the four owner indices (`microtrend_2026-11-13`), model premiums;
real-like: NIFTY and BANKNIFTY, real where priced, else the model less the measured gap.

| Cell | TUNE n / days, % (t) | CHOOSE n, % (t) | TEST n, % | Whole span % (t) | Real-like N+BN | Verdict |
|---|---|---|---|---|---|---|
| H1 entry before 10:15 | 410 / 216, −0.20 (−0.2) | 166, +1.92 (+1.0) | 156, −1.86 | −0.07 (−0.1) | −0.57 | examined |
| H1 opposite (10:15 on) | 1,410 / 527, −0.20 (−0.4) | 474, −0.57 (−0.6) | 449, −1.21 | −0.47 (−1.1) | −1.52 | examined |
| H2 puts | 669 / 230, +0.26 (+0.3) | 279, +0.05 (+0.0) | 316, −2.05 | −0.37 (−0.5) | −1.72 | examined |
| H2 opposite (calls) | 1,151 / 382, −0.46 (−0.8) | 361, +0.10 (+0.1) | 289, −0.65 | −0.38 (−0.8) | −1.05 | examined |
| H3 previous session active | 860 / 329, −1.08 (−1.4) | 301, +0.41 (+0.4) | 292, −2.72 | −1.10 (−1.9) | −2.22 | examined |
| H3 opposite (calm) | 960 / 361, +0.59 (+0.9) | 339, −0.22 (−0.2) | 313, −0.14 | +0.28 (+0.6) | −0.44 | examined |
| H4 VIX fell the session before | 962 / 314, +0.21 (+0.3) | 358, +0.54 (+0.4) | 313, −1.12 | +0.03 (+0.1) | −0.99 | examined |
| H4 opposite (VIX rose) | 858 / 268, −0.66 (−0.9) | 282, −0.50 (−0.4) | 292, −1.66 | −0.83 (−1.4) | −1.64 | examined |
| H5 entry 3 minutes late | 1,820 / 582, −0.13 (−0.2) | 640, −0.15 (−0.2) | 605, −1.52 | −0.41 (−1.0) | −1.33 | examined |
| (same trades on time) | 1,820 / 582, −0.20 (−0.4) | 640, +0.08 (+0.1) | 605, −1.38 | −0.37 (−0.9) | −1.29 | reference |

Counts: 9 examined, 0 discovered, confirmed, passed or AVOID. Luck: 9 x 0.6% x 16% x 50% =
0.004 passes; placebo (day labels shuffled over days, trade labels over trades, inside index
and year, 20 shuffles): 0.00 of each. No higher than luck: nothing here is a finding.

H1 (first hour), H2 (puts) and H4 (VIX fell) are flat in history; H3 is reversed (after an
active session the rule loses more, −1.10, t −1.9). All four are dropped. H5 holds in substance:
3 minutes late costs 0.04 points a trade. No filter is proposed for the rule. MIDCPNIFTY
(training, apart): every cell positive, +0.66 to +3.11% a trade, as in earlier work.

## What this says about the desk
On clean one-minute bars the desk raises the alerts the backtest says it should. Its live
record of the micro-trend rule is one trade: it was down on 5 of 7 sessions, and the missed
week was the best the replay has seen. The old slots lost, mean-reversion on stocks clearly so.
NIFTY and BANKNIFTY fills cost half what the model assumes; FINNIFTY and SENSEX twice. Late
entries do not hurt; no time of day, side or market state from the live record holds up.

Proposals (no product code changed): (1) a heartbeat event, so a day the desk did not run is
a fact; (2) no alert while an instrument's live bars mix resolutions; (3) per-index option
spreads in the cost model (0.3% NIFTY, BANKNIFTY; 1.5% FINNIFTY, SENSEX) after more quotes.

## What I could not do, and what I doubt
- Real premiums: 2 of 22 index alerts; the rest is the model less 1.0 point.
- India VIX in the store ends on 6 Nov: later premiums use that VIX; 6 alerts lack "VIX rose".
- Down days are inferred from missing alerts and quotes. The stored bars may have been topped
  up after the session, so the replay may see cleaner bars than the desk did.
- Fill quotes cover 4 sessions. The old slots are retired, so their record is of dead rules.

## Rows for the setup book
| Instrument | Kind | Setup and preset | Timeframe | Condition | Exit | Trades a year | % a trade TUNE / CHOOSE / TEST (t whole span) | Real-premium or cost note | Status | Remark |
|---|---|---|---|---|---|---|---|---|---|---|
| NIFTY, BANKNIFTY, FINNIFTY, SENSEX | option same-day | microtrend, Supertrend(14, 1.7) x 7 | 3m | entry before 10:15 | 3 of 7 or 15:15 | 146 | −0.20 / +1.92 / −1.86 (−0.1) | real-like N+BN −0.57 | REJECTED | TUNE not above zero; the live first-hour loss is not in the history |
| same | option same-day | microtrend | 3m | puts only | 3 of 7 or 15:15 | 253 | +0.26 / +0.05 / −2.05 (−0.5) | real-like −1.72 | REJECTED | puts and calls the same over five years |
| same | option same-day | microtrend | 3m | previous session active | 3 of 7 or 15:15 | 291 | −1.08 / +0.41 / −2.72 (−1.9) | real-like −2.22 | REJECTED | worse after active sessions, not better; misses AVOID (t −1.4 in TUNE) |
| same | option same-day | microtrend | 3m | India VIX fell the session before | 3 of 7 or 15:15 | 327 | +0.21 / +0.54 / −1.12 (+0.1) | real-like −0.99 | REJECTED | about flat on the model, negative on real premiums |
| same | option same-day | microtrend | 3m | entry 3 minutes late | 3 of 7 or 15:15 | 613 | −0.13 / −0.15 / −1.52 (−1.0) | real-like −1.33 | REJECTED | a delay costs 0.04 points a trade: harmless, but the rule has no edge to keep |
