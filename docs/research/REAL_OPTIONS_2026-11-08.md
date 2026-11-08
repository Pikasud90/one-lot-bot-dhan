# Real option prices, the paper test, and fill costs (2026-11-08)

The owner's three requests after the cross-instrument retune
([CROSS_RETUNE_2026-11-07.md](CROSS_RETUNE_2026-11-07.md)): "set up the paper test, use real
option prices, measure actual one-share fill costs". Nothing here places an order. Every
number is a backtest or a quote, never investment advice.

## 1. Real option prices (Dhan expired options)

### What Dhan serves, as checked against the live API

`POST /v2/charts/rollingoption` returns one-minute bars of expired (and live) index options on
a rolling basis: per underlying, expiry flag (MONTH) and code, CALL/PUT, and a strike RELATIVE
to each minute's at-the-money strike (ATM-10 .. ATM+10 for index options on the near
contracts). Each bar carries the actual strike, IV, OI, volume and the underlying's spot.

| Fact | Evidence |
|---|---|
| Bars are stamped at their start; `spot` is the minute's close | NIFTY 2026-10-01: mean abs(spot - our 1m close) = 0.39 points over 375 bars; 6.5 points against the open |
| A bar's strike is the strike nearest that minute's spot, plus rel x step | ATM-6 rows: spot 22,582 -> 22,300; 22,570 -> 22,250; 22,464 -> 22,150 |
| Only a ONE-day window is accepted | `fromDate`..`fromDate`+1 works (and returns BOTH days); 2 days or more, including the docs' own one-month example, is refused with DH-905 |
| `expiryCode` 0 is refused | "DH-905 expiryCode is required"; 1 = near contract, 2 = next |
| The `iv` field cannot be inverted for time to expiry | NIFTY 2024-07-26 24,400 CE: Dhan iv 8.0 %, while 13.1 % prices the bar at the true expiry (34 days); 9.1 vs 13.7 %, 18.0 vs 23.1 % on two other days |
| Bad prints exist | a 7,550 strike on a 44,000 BANKNIFTY; prints after 15:30 |
| Thin monthly contracts | FINNIFTY, MIDCPNIFTY and SENSEX monthly options have minutes, days or months without a trade |

Data lives in its own SQLite file (`data/expired_options.db`, one compressed block per series
per day), so the 9 GB bar database and the running desk are never touched.

### How a backtest trade is priced for real (`src/onelot/lab/real_options.py`)

The simulator decides every trade on the index, so the trades do not depend on the premium. The
SAME trades are re-priced:

* contract: the model's strike in the NEAR monthly contract, or the NEXT one when the near
  contract expires within 3 days (the Lab's roll rule). Days left come from the market: the
  near/next at-the-money time-value ratio R on the same minute gives T = R^2 G / (1 - R^2);
* entry: the option's open in the entry minute (or the first trade within 3 minutes);
* exits: the open of the exit minute for signal, time and session exits; for stop, trail and
  target, the first minute whose index range reaches the level, interpolated inside the
  option's own bar;
* friction: the model's own half-spread (0.6 %) and 2 ticks a side and the same charges, so
  the comparison isolates the premium;
* a trade whose option did not trade near a needed minute is UNPRICED, never invented.

### Two model errors found on the way

1. **Double slippage on options (fixed).** `run_backtest` passed the stock slippage (0.03 % of
   the index) to the simulator for options too, and then charged the premium's half-spread and
   ticks on top. With a 0.65-delta option that is roughly 1 % of premium per side, about 2
   points per round trip. `BacktestCfg` documents the 0.03 % as linear-instrument slippage
   only. The runner now passes 0 for options. The desk's paper book never had this error (it
   prices option exits at the unslipped index level).
2. **Monthly expiry weekday (documented, not changed).** The model assumes the monthly
   contract expires on the last Tuesday for all five years. NSE's monthly index contracts
   expired on Thursdays until August 2025, so for a few days each month the model priced a
   contract days from expiry while the market's near contract had a month left (example:
   NIFTY on 2024-07-26, model premium 225.90 for a 4-day contract against 456.25 traded for
   the real 34-day contract). The real-price path uses the market's own contracts.

### Results

**Verdict.** 2,266 option trades from the retune's 13 picks; 1,309 (58 %) could be priced from
real trades: BANKNIFTY 89 % and NIFTY 93 %, but FINNIFTY 40 %, MIDCPNIFTY 41 % and SENSEX 21 %
(their monthly contracts often did not trade within 3 minutes). On the priced trades, real
premiums were 1.3 points per trade BETTER than the retune's model (its double slippage) and 0.9
points WORSE than the corrected model (BANKNIFTY -1.6, NIFTY -0.9; median -0.55). With real
premiums no setting is profitable in all three periods: most are positive in the tuning years
they were picked on and lose afterwards. The options paper-test candidate, supertrend_vol 30m:
+1.92 % / -1.67 % / -3.06 % per trade (tuning / choosing / test). Under the corrected model its
test year is +0.46 % on every trade, but that profit comes from FINNIFTY, MIDCPNIFTY and SENSEX
trades the market cannot confirm; on BANKNIFTY and NIFTY, where it can, the test year lost.

**A. Every trade, corrected model** (no double slippage; what the Lab now computes):

| Strategy | TF | Tuning | Choosing | Test |
|---|---|---|---|---|
| supertrend_vol | 30m | +3.44% · 58% · 297 | +3.27% · 52% · 101 | +0.46% · 50% · 90 |
| supertrend_vol | 60m | +4.02% · 57% · 47 | +4.11% · 67% · 12 | -3.41% · 44% · 9 |
| ema_cross | 30m | +3.19% · 53% · 58 | +4.63% · 50% · 10 | -0.88% · 50% · 16 |
| nr7_breakout | 15m | +2.96% · 46% · 46 | +1.76% · 56% · 16 | -4.28% · 56% · 9 |
| cpr_breakout | 30m | +4.59% · 48% · 69 | -0.93% · 47% · 19 | +2.78% · 50% · 18 |
| vwap_pullback | 15m | +1.79% · 47% · 45 | -1.41% · 36% · 14 | -1.60% · 41% · 17 |
| turtle_soup | 30m | +2.17% · 60% · 40 | -3.38% · 47% · 19 | -4.81% · 25% · 8 |
| ema_pullback | 15m | +5.86% · 62% · 99 | -2.70% · 38% · 40 | -1.77% · 42% · 38 |
| orb | 60m | +4.55% · 58% · 50 | -3.59% · 43% · 28 | -1.31% · 45% · 22 |
| supertrend_adx | 15m | +2.57% · 47% · 60 | -5.40% · 25% · 32 | -4.66% · 19% · 21 |
| donchian_breakout | 60m | -0.80% · 43% · 86 | -0.00% · 45% · 31 | -2.94% · 45% · 44 |
| mean_reversion | 30m | +0.11% · 69% · 78 | -0.31% · 54% · 26 | -3.14% · 53% · 19 |
| intraday_momentum | 15m | -0.01% · 45% · 371 | -0.06% · 41% · 121 | -1.94% · 30% · 140 |

**B. The trades real premiums could price** (same trades in every column; avg net per trade · win rate · trades):

| Strategy | TF | Period | Priced / all | Model as in the retune | Corrected model | REAL premiums | Indices profitable (real) |
|---|---|---|---|---|---|---|---|
| supertrend_vol | 30m | train | 136/297 | +0.86% · 53% · 136 | +2.90% · 59% · 136 | +1.92% · 50% · 136 | 3/5 |
| supertrend_vol | 30m | valid | 70/101 | -2.20% · 37% · 70 | +0.06% · 44% · 70 | -1.67% · 43% · 70 | 3/5 |
| supertrend_vol | 30m | test | 59/90 | -5.64% · 34% · 59 | -3.26% · 42% · 59 | -3.06% · 29% · 59 | 1/5 |
| supertrend_vol | 60m | train | 24/47 | +2.66% · 50% · 24 | +4.45% · 54% · 24 | +3.07% · 54% · 24 | 2/2 |
| supertrend_vol | 60m | valid | 10/12 | +1.03% · 50% · 10 | +3.15% · 60% · 10 | +1.59% · 50% · 10 | 0/1 |
| supertrend_vol | 60m | test | 7/9 | -9.18% · 14% · 7 | -6.31% · 29% · 7 | -6.59% · 29% · 7 | – |
| ema_cross | 30m | train | 34/58 | +1.25% · 38% · 34 | +3.30% · 41% · 34 | +2.31% · 44% · 34 | 1/3 |
| ema_cross | 30m | valid | 8/10 | +3.59% · 50% · 8 | +5.89% · 50% · 8 | +4.15% · 50% · 8 | – |
| ema_cross | 30m | test | 12/16 | -3.88% · 42% · 12 | -1.76% · 50% · 12 | -1.55% · 42% · 12 | 1/2 |
| nr7_breakout | 15m | train | 24/46 | -4.33% · 33% · 24 | -2.13% · 38% · 24 | -5.88% · 29% · 24 | 0/3 |
| nr7_breakout | 15m | valid | 13/16 | -1.26% · 54% · 13 | +1.32% · 54% · 13 | -3.76% · 38% · 13 | 0/2 |
| nr7_breakout | 15m | test | 5/9 | -11.38% · 40% · 5 | -9.28% · 40% · 5 | -7.54% · 40% · 5 | 0/1 |
| cpr_breakout | 30m | train | 36/69 | +4.07% · 44% · 36 | +6.31% · 50% · 36 | +3.86% · 50% · 36 | 3/3 |
| cpr_breakout | 30m | valid | 12/19 | -5.08% · 33% · 12 | -3.39% · 33% · 12 | -1.87% · 42% · 12 | 1/2 |
| cpr_breakout | 30m | test | 14/18 | +1.60% · 36% · 14 | +4.18% · 50% · 14 | +7.17% · 43% · 14 | 3/3 |
| vwap_pullback | 15m | train | 21/45 | -2.50% · 33% · 21 | -0.44% · 33% · 21 | -0.93% · 29% · 21 | 1/2 |
| vwap_pullback | 15m | valid | 9/14 | -0.47% · 56% · 9 | +1.41% · 56% · 9 | +0.85% · 56% · 9 | 1/1 |
| vwap_pullback | 15m | test | 13/17 | -4.93% · 23% · 13 | -2.47% · 38% · 13 | -2.65% · 38% · 13 | 1/3 |
| turtle_soup | 30m | train | 21/40 | -3.12% · 38% · 21 | -1.40% · 43% · 21 | -1.40% · 48% · 21 | 1/2 |
| turtle_soup | 30m | valid | 14/19 | -6.08% · 36% · 14 | -4.36% · 36% · 14 | -3.65% · 29% · 14 | 1/4 |
| turtle_soup | 30m | test | 3/8 | -13.56% · 0% · 3 | -10.54% · 0% · 3 | -11.78% · 0% · 3 | – |
| ema_pullback | 15m | train | 47/99 | +5.14% · 55% · 47 | +7.53% · 57% · 47 | +5.87% · 51% · 47 | 4/4 |
| ema_pullback | 15m | valid | 31/40 | -5.99% · 35% · 31 | -3.63% · 35% · 31 | -3.73% · 32% · 31 | 2/5 |
| ema_pullback | 15m | test | 28/38 | -7.02% · 21% · 28 | -4.73% · 29% · 28 | -3.19% · 43% · 28 | 2/5 |
| orb | 60m | train | 21/50 | +4.87% · 57% · 21 | +7.00% · 57% · 21 | +2.08% · 48% · 21 | 2/2 |
| orb | 60m | valid | 20/28 | -7.10% · 20% · 20 | -4.94% · 35% · 20 | -5.86% · 25% · 20 | 0/3 |
| orb | 60m | test | 9/22 | -5.85% · 22% · 9 | -3.49% · 33% · 9 | -5.46% · 33% · 9 | 0/2 |
| supertrend_adx | 15m | train | 23/60 | -10.37% · 13% · 23 | -8.39% · 22% · 23 | -13.85% · 17% · 23 | 0/4 |
| supertrend_adx | 15m | valid | 26/32 | -7.45% · 23% · 26 | -5.60% · 23% · 26 | -6.10% · 31% · 26 | 1/4 |
| supertrend_adx | 15m | test | 11/21 | -7.56% · 9% · 11 | -5.62% · 9% · 11 | -3.23% · 18% · 11 | 1/2 |
| donchian_breakout | 60m | train | 48/86 | -0.17% · 40% · 48 | +2.41% · 48% · 48 | +1.78% · 44% · 48 | 2/3 |
| donchian_breakout | 60m | valid | 26/31 | -5.26% · 27% · 26 | -2.89% · 38% · 26 | -4.36% · 38% · 26 | 1/5 |
| donchian_breakout | 60m | test | 30/44 | -8.47% · 37% · 30 | -6.34% · 40% · 30 | -7.75% · 37% · 30 | 0/5 |
| mean_reversion | 30m | train | 38/78 | -2.89% · 58% · 38 | -0.97% · 66% · 38 | -1.36% · 66% · 38 | 1/3 |
| mean_reversion | 30m | valid | 19/26 | -4.94% · 26% · 19 | -2.81% · 47% · 19 | -4.40% · 26% · 19 | 1/4 |
| mean_reversion | 30m | test | 11/19 | -3.63% · 45% · 11 | -1.21% · 64% · 11 | -4.43% · 18% · 11 | 0/2 |
| intraday_momentum | 15m | train | 183/371 | -2.55% · 27% · 183 | -0.52% · 42% · 183 | -1.58% · 30% · 183 | 1/5 |
| intraday_momentum | 15m | valid | 89/121 | -2.23% · 34% · 89 | +0.11% · 44% · 89 | -0.20% · 36% · 89 | 2/5 |
| intraday_momentum | 15m | test | 104/140 | -5.02% · 12% · 104 | -2.75% · 23% · 104 | -2.19% · 17% · 104 | 0/5 |

**C. The options paper-test candidate by index** (supertrend_vol 30m; corrected model on every trade, then corrected model and real premiums on the priced trades):

| Index | Period | Every trade (corrected model) | Priced | Corrected model (priced) | REAL (priced) |
|---|---|---|---|---|---|
| BANKNIFTY | train | +4.54% · 58% · 71 | 65/71 | +2.39% · 55% · 65 | +1.55% · 48% · 65 |
| NIFTY | train | +6.28% · 67% · 45 | 44/45 | +5.99% · 66% · 44 | +5.21% · 59% · 44 |
| FINNIFTY | train | +4.38% · 56% · 72 | 17/72 | +3.75% · 65% · 17 | +2.46% · 53% · 17 |
| MIDCPNIFTY | train | +1.27% · 59% · 58 | 6/58 | -1.31% · 67% · 6 | -3.90% · 33% · 6 |
| SENSEX | train | +0.52% · 55% · 51 | 4/51 | -19.88% · 0% · 4 | -21.93% · 0% · 4 |
| BANKNIFTY | valid | -2.08% · 41% · 22 | 19/22 | -2.59% · 37% · 19 | -4.52% · 26% · 19 |
| NIFTY | valid | +4.25% · 53% · 19 | 17/19 | +2.02% · 47% · 17 | +0.30% · 47% · 17 |
| FINNIFTY | valid | +7.03% · 65% · 17 | 13/17 | +6.62% · 62% · 13 | +6.92% · 62% · 13 |
| MIDCPNIFTY | valid | -2.86% · 41% · 22 | 18/22 | -4.80% · 33% · 18 | -7.97% · 39% · 18 |
| SENSEX | valid | +11.37% · 67% · 21 | 3/21 | +6.36% · 67% · 3 | +5.72% · 67% · 3 |
| BANKNIFTY | test | +1.26% · 55% · 20 | 16/20 | -1.13% · 50% · 16 | -3.29% · 25% · 16 |
| NIFTY | test | -0.82% · 35% · 17 | 15/17 | -2.13% · 33% · 15 | -2.02% · 27% · 15 |
| FINNIFTY | test | +5.15% · 59% · 17 | 9/17 | -0.82% · 56% · 9 | +2.94% · 44% · 9 |
| MIDCPNIFTY | test | -1.12% · 58% · 19 | 13/19 | -6.44% · 46% · 13 | -7.79% · 15% · 13 |
| SENSEX | test | -2.16% · 41% · 17 | 6/17 | -8.54% · 17% · 6 | -3.78% · 50% · 6 |

Read each row on the PRICED trades only (the "Priced" column), so the model and the real
columns describe the same trades. "Model, no double slippage" is the corrected model.

## 2. The paper test

The desk runs only the retune's closest candidates, with the retune's exact settings
(`config/desk.yaml`; pinned by `tests/acceptance/test_of_qa_lab_and_data.py`):

| Basket | Strategy | Bars | Side | Exits | Backtest test year |
|---|---|---|---|---|---|
| 5 index options (BUY CE, monthly, delta ~0.65) | supertrend_vol | 30m | long only, entries 09:30-13:00, 1 a day | 2.5R target, 1.5 ATR trail, 12-bar time stop | model -1.65 % per trade (90 trades); real premiums -3.06 % on the 59 that could be priced |
| 18 stocks, 1 share | supertrend_vol | 60m | short only, entries until 11:30, 2 a day | 1.5R target, 2.5 ATR trail | +0.10 % per trade, 25 trades |
| 18 stocks, 1 share | supertrend_adx | 30m | short only, entries until 11:30, 1 a day | 1.5R target, 1.5 ATR trail, 24-bar time stop | +0.06 % per trade, 34 trades |

None of the three passed all three backtest periods. The paper test is forward evidence, not
a recommendation.

### Desk = Lab, rule by rule

Added so a paper trade is the backtest's trade:

* the Lab's entry risk rule at the alert: a stop tighter than max(0.4 ATR, 0.15 % of price;
  0.35 % for supertrend_adx) is widened; a setup riskier than min(8 ATR, 2 % of price) is not
  alerted (`desk.rules.lab_entry_risk`; these strategies' own `stop_atr_max` is 8);
* the strategy's own `max_trades_per_day` and `cooldown_bars` (counted in strategy bars after
  its last paper exit, across sessions, like the simulator). These are the strategy's rules,
  not a risk governor;
* index alerts name the MONTHLY contract, the month's last listed expiry at least 3 days away
  (`desk.rules.pick_expiry`; desk.yaml `option_expiry`, `option_roll_days`). Before, the desk
  named the nearest expiry (a weekly on NIFTY and SENSEX);
* the paper book prices option fills, exits and marks with the alerted contract's TRADED
  one-minute prices (Dhan intraday bars of that contract) whenever that minute traded, and
  records `premium_source` real/model on every fill. The model prices only what did not
  trade;
* (2026-11-08, after the first write-up) the desk zeroes index volume as the Lab does
  (`desk.data._clean`). Dhan's index bars carry a volume figure from 2025-12-03 and the Lab
  sets it to 0 (commit 4904e0c), so the index candidate's `rvol_min` 1.0 and its strength's
  volume pillar are neutral there; the desk read Dhan's figure. On that year the index setting
  gave 87 Lab entries and 81 desk entries, only 38 the same (49 of the Lab's 87 had a desk
  relative volume below 1.0). Fixed before the test's first session.

Differences that remain: the risk rule uses the signal bar's close (the Lab, the next open); the
paper trail uses the ATR frozen at the alert; a paper target needs a trade-through; one live
idea per instrument; when the alerted contract did not trade in a fill minute, the paper book
takes its last trade before it (or the model), where the re-pricing study waits up to 3
minutes for the next trade; the strategy's daily count includes alerts that never filled. Over 5 years the two stock strategies would collide on a stock for 1 of 129
and 9 of 173 alerts.

At the backtest's rate the test gives about 1.7 option alerts and 1.1 stock alerts a week.
Proposed review point, fixed now: 30 closed paper trades per strategy (about 4 months for the
options, 6-12 months for the stocks). A strategy stays only if its paper net per trade is above
zero after costs by then.

The Home scoreboard counts each candidate by strategy, timeframe and market (2026-11-08):
supertrend_vol ran on 15-minute stock and 5-minute index bars before the test, and those 15
paper trades are their own rows, not the candidate's.

Owner's decision, 2026-11-08: keep the index option alerts in the paper test despite the
real-premium result above, and judge them at the review point like the stock candidates.

## 3. Fill costs (what one share or one lot costs to fill)

* **Delay, from history.** For every trade of the two stock candidates over 5 years (302
  trades, 1-minute bars): filling 1 or 2 minutes after the bar the backtest fills at costs
  +0.008 % to +0.013 % on average at the entry and +0.000 % to +0.008 % at the exit; 5 minutes is
  noise (-0.011 % to +0.027 %). Positive = worse for the trader.
* **Spread, measured live from Monday.** The desk samples the best bid/ask every 5 minutes of the
  session for the 18 stocks and each index's ~0.65-delta monthly CE and PE (one quote call), and
  the alerted instrument at every alert and 1, 2 and 5 minutes later. It stores them in
  `fill_quotes` and sets the measured half-spread beside the Lab's assumptions (0.03 % a side on
  stocks; 0.6 % + 2 ticks on options). Read it on the Live Desk's "Fill costs" card,
  `GET /api/desk/fill-costs?days=30`, or `python -m onelot fill-costs`.
* **Your own fills.** The prices you type on a taken alert are compared with the alert's.
* **Charges, from your own trade book.** Dhan's trade history lists what it billed on every
  fill. Over the last year (41 fills, all equity) against the cost table for the same fills:

  | Product | Side | Fills | Billed by Dhan | Cost table |
  |---|---|---|---|---|
  | delivery | BUY | 26 | 0.116 % of turnover | 0.154 % |
  | delivery | SELL | 11 | 0.101 % | 0.139 % |
  | intraday | BUY | 1 | 0.152 % | 0.042 % |

  The statutory parts match the table within 2-4 % (STT 111.00 billed vs 114.89 modelled on the
  sells; stamp duty 19.09 vs 19.32 on the buys). The difference is brokerage: Dhan billed
  0.85 rupees in total on delivery, while the table charges 0.03 %. The one intraday fill's
  lines (STT 3.29 and stamp 0.91 on a 2,834-rupee BUY) do not match intraday rates, so intraday
  charges stay unverified until there are more intraday fills. The paper test's stock trades are
  intraday, so taking a few of its alerts would verify exactly the costs that matter. Shown on
  the Fill costs card and by `onelot fill-costs`.

## Reproduce

```
.venv/bin/python scripts/real_options_study.py --groups priority --rate 3   # fetch + price
.venv/bin/python scripts/real_options_study.py --groups priority --report   # the table
.venv/bin/python -m onelot fill-costs --days 30
```
