# Known Limitations

An honest list. Read it before trusting the system with money.

## What it does not know

**It has no edge, and this repository does not claim one.** The parameters
shipped are illustrative. The replay harness validates the *machinery* and the
cost model; it runs on a synthetic tape with no exploitable structure, so a
negative result there is expected and says nothing about real-market
performance. Only recorded live data can tell you that.

**Backtest results on the synthetic tape are not a forecast.** They confirm
that signals fire, that sizing respects the budget, that stops trigger, and
that costs are subtracted correctly. Nothing more.

## The one-lot rule

The system trades **exactly one Bank Nifty lot, always**. Four consequences to
accept before starting, not discover later.

**1. Risk-per-trade is an output, not a setting.** It is
`1.5 x ATR x |delta| x 35`. At Bank Nifty ~57,800 with ATR(5m) 90 that is about
**Rs 3,100 per trade** on a 0.65-delta contract. It does not depend on your
account and you cannot dial it down — only the stop multiplier and the delta
band move it.

**2. One lot is a large absolute position.** Around **Rs 34,000 of premium** at
current levels. Whether that is a large share of your capital is a question the
system deliberately does not ask; `account.capital_inr` is optional.

**3. Signals are infrequent.** Roughly **0.1 per session** in replay — about one
tradeable setup a fortnight. Nothing is rejected for cost, so a quiet stretch
means the market is not offering the setup.

**4. The drawdown leash is short.** At Rs 3,100 per losing trade, the shipped
Rs 18,000 drawdown halt is about **six losing trades**. That halt is standing:
it survives restarts and new sessions, and needs a deliberate reset.

## What the backtest numbers actually mean

Replay over 120 synthetic sessions, shipped configuration:

| | |
|---|---|
| trades | 11 |
| win rate | 27.3% (3W / 8L) |
| net P&L | **-Rs 14,814** |
| costs as share of gross | 17.9% |
| expectancy | -0.476 R |
| avg R on winners / losers | +1.29 / -1.14 |
| max drawdown | Rs 19,413 |
| Sharpe | **not computed** — 8 trading days, needs 20 |

**Read this correctly.** The synthetic tape is a random walk with no
exploitable structure. A trend-following system on a random walk loses exactly
the friction plus the stop/target asymmetry — a negative result there is the
*expected* outcome and says nothing about real-market performance. The engine
refuses to print a Sharpe from 8 trading days for the same reason: annualising
by sqrt(252) from a sample that small is noise dressed as a statistic.

What these numbers **do** establish:

- The machinery runs end to end and the exit attribution is coherent.
- **Costs consumed 17.9% of gross P&L.** That is the number worth carrying
  forward. It is high because the R multiples are small relative to fixed
  per-order costs, and it is the single strongest argument for demanding a
  higher net R/R rather than more trades.
- Winners averaged +1.29R against losers at -1.14R. At a 27% win rate you would
  need winners near +3R to break even. Either the trailing stop is cutting
  winners short or the entry needs to be more selective — that is a real,
  testable question, and it is exactly what the replay harness is for.

**Real performance can only be measured on recorded live data.** The recorder
exists for that; run in PAPER mode and accumulate it.

## Structural limitations## Structural limitations

| Limitation | Consequence |
|---|---|
| Regime is computed **daily** (as of T−1) | `REGIME_FLIP` almost never fires intraday. It is a slow guard, useful only if a position is carried. |
| Single instrument, single position | No portfolio effects, no hedging, no spreads. |
| **Fixed one lot** | No scaling in, no scaling out, no partial exits. A conviction signal and a marginal one are the same size. |
| Long options only | No premium selling, so no theta income; you are always paying decay. |
| Intraday only | Any multi-day move is out of reach by construction. |
| IV percentile needs history | With `ivp_mode: strict`, the system will not signal at all until ~60 sessions of ATM IV have accumulated. That is the intended behaviour, but it means a long cold start. |
| Fixed 5-minute bar | No adaptation to volatility regime in the sampling itself. |

## Data limitations

- **Dhan Data API is a paid add-on.** Without it there is no live feed and no
  option chain. `python -m onelot.preflight` reports exactly what is missing.
- **Access tokens expire.** The engine detects and alerts, but cannot renew.
- **The index has no volume.** VWAP and volume divergence depend on the futures
  contract. If futures cannot be resolved, those analytics are unavailable and
  the Guardian says so rather than inventing a number.
- **Holiday calendar ships incomplete.** Only fixed-date national holidays are
  pre-filled; lunar-calendar holidays move annually and are deliberately not
  guessed. An unlisted holiday means no data, health `DEAD`, and no signals —
  annoying, never dangerous.

## Operational limitations

- **The human is the bottleneck, by design.** A 75-second TTL assumes you are
  at the machine. There is no remote acknowledgement channel in this build
  (Telegram and webhook are stubbed, not implemented).
- **No auto-execution, ever.** Slippage between the alert and your manual fill
  is real and is not modelled beyond the configured tick allowance.
- **Single machine.** No failover. If the machine sleeps, the engine stops.
- **macOS notification permission** must be granted manually, and `osascript`
  reports success even when the banner is suppressed.

## Modelling caveats

- Greeks are Black-76 with `r = 0` by default. For intraday horizons the
  discounting effect is negligible; over weeks it is not.
- The paper fill model assumes you get the bid on exit. A fast move through
  your stop will fill worse. The model takes the **worse** of bid and stop
  level, but real gaps can exceed both.
- `expected_hold_hours` in the theta gate is a configured constant, not a
  prediction.
- The HMM is fitted on two features. It is a coarse regime filter, not a
  forecast.

## Things deliberately not built

- Order placement — permanently out of scope.
- Any form of martingale, averaging down, or position pyramiding.
- Automatic parameter optimisation. Fitting parameters to a synthetic tape
  would be a way to fool yourself efficiently.
