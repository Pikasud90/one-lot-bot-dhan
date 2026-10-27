# Strategy Reference

Every parameter below lives in `config/config.yaml`. **They are starting
points, not recommendations.** Calibrate them with the replay harness before
you rely on any of them.

## Profile

| | |
|---|---|
| Instrument | NIFTY BANK options, **monthly expiry** (weeklies are discontinued) |
| Direction | Long CE / long PE only. Risk is capped at the premium paid. |
| Holding | **Intraday**, mandatory square-off. No overnight exposure. |
| Signal bar | 5-minute, **closed only** |
| Underlying for greeks/VWAP | **current-month FUTURES** — the index has no volume |

## The eleven gates

Every gate is evaluated on every bar, even after one has already failed, so the
rejection histogram is complete. A system that fires rarely and cannot explain
itself is indistinguishable from a broken one.

### 0 — Preflight
Capital configured · data health `HEALTHY` · inside the entry window · no open
position · risk governor permits · cooldown elapsed · chain fresh · not expiry day.

### 1 — Regime (daily)

**Default: `rule_based`.** EMA21 versus EMA50 plus slope. About fifteen lines,
no dependencies, nothing to fit, nothing to persist — and therefore no
look-ahead surface at all.

**Optional: `hmm`.** A three-state Gaussian HMM on daily log return and log
realised volatility, **forward-filtered**, never Viterbi or smoothed (see
`docs/06_TESTING.md`). States are relabelled by mean return on every fit so
BULL always means BULL, and confidence below `min_state_confidence` is treated
as CHOP. It needs `pip install -r requirements-hmm.txt`.

The simple engine is the default deliberately. A heavier model is not
automatically a better one, and this is the component most capable of fooling
you — it took 27 tests to pin down its look-ahead behaviour alone.

`BULL` permits CE. `BEAR` permits PE. `CHOP` permits nothing — buying premium
in a range is a slow donation to theta.

*Tuning:* `min_state_confidence` is the main lever. Raising it means fewer,
higher-conviction days.

### 2 — Intraday chop
The daily regime persists for days; it cannot gate a 5-minute entry. This does:
ADX above a floor (a trend exists) and ATR percentile inside a band (volatility
is neither dead nor extreme).

### 3 — Trend
Close vs EMA(21) on the closed bar. Above for CE, below for PE.

### 4 — Momentum
RSI(14) inside `[55, 65]` for CE, `[35, 45]` for PE. Deliberately a band, not a
threshold: it targets a trend that is moving but not yet exhausted.

*This is usually the second-most-binding gate* — expect a ~25% pass rate.

### 5 — Volatility (IV percentile)
IVP below `ivp_max`. You want to buy options when they are relatively cheap.

**Cold start matters here.** IVP is a rank over ~252 sessions, and on day one
you have none. Three modes:

- `strict` *(default)* — block all signals until history is sufficient. Safest.
- `degraded` — rank over whatever exists, flagged loudly on every alert.
- `off` — skip the gate, recorded in the audit trail.

History accumulates from your own recorded chain snapshots, so it is Bank
Nifty's own volatility, not a proxy.

### 6 — Contract selection
`0.60 ≤ |delta| ≤ 0.70` — **absolute** delta, because put deltas are negative.

Liquidity is a **relative** sanity gate: `spread ≤ max(abs_floor, pct × mid)`.
A flat rupee cap rejects essentially every ITM monthly strike, whose spreads
run ₹3–15. The spread's true economic cost is not policed here — it is already
priced into `rr_net`, because entry uses the ask and exit uses the bid.

Also: minimum depth, open interest and traded volume. Greeks that fail the
local Black-76 cross-check are excluded before they can be sized against.

Among survivors, the contract whose |delta| is closest to the band midpoint
wins — the most typical instrument, not one clinging to a boundary.

### 7 — Theta
`|theta_per_hour| × expected_hold_hours ≤ max_burn_pct × premium`.

Expressed over the **expected hold**, not overnight: this is an intraday system.

### 8 — Net risk/reward
`rr_net ≥ rr_net_min`, computed on **effective** prices (ask + slippage in,
bid − slippage out) net of brokerage, STT, exchange charges, SEBI fee, IPFT,
stamp duty and GST.

`rr_net_min` must be **below** `rr_target_gross` — costs make the gross
multiple unreachable, so the config validator rejects the alternative.

### 9 — Market

There is no sizing gate. The rule is **one lot, always, whatever it costs**, so
the only thing left to check here is whether a two-sided market exists on the
selected contract. A zero or crossed bid/ask is a data problem, not a risk
decision.

**Why there is no affordability check.** One lot is affordable by definition —
that is the operator's rule. Sizing was removed rather than made trivial,
because a gate that never fires is worse than no gate: it invites tuning, it
produces confusing rejections, and it hides the fact that the real risk control
lives elsewhere.

**Where risk actually lives now:**

1. **The stop** bounds any single trade. A stop is wide in rupees exactly when
   ATR is high, and high ATR is already excluded by gate 2's
   `atr_percentile_max`. That is the extreme-volatility guard.
2. **Absolute rupee limits** on the day and on cumulative drawdown (gate 10).

Rupees rather than percentages, because with a fixed lot the loss on a trade is
a rupee amount set by the market — and because it means the engine never needs
to know your account size. `account.capital_inr` is optional.

`python -m onelot.cost_preview` shows what one lot costs and risks at any market
level, and how many losing trades your limits absorb.

### 10 — Risk governor
Seven switches, all persisted, all requiring a deliberate reset:

| Switch | Default | Standing? |
|---|---|---|
| daily loss limit | ₹6,000 | no — resets with the date |
| **cumulative drawdown limit** | ₹18,000 peak-to-trough | **yes** |
| max trades per day | 2 | — |
| max consecutive losses | 2 | **yes** |
| max concurrent positions | 1 | — |
| manual panic halt | dashboard button | **yes** |

**Standing** means the halt outlives the session and needs a deliberate reset.
This matters more than it sounds: halt state used to be keyed by trade date, so
a new day loaded no row, started ACTIVE, and the cumulative drawdown halt
silently cleared itself overnight. A cumulative limit that resets daily is not
a cumulative limit. Only the daily loss limit is allowed to reset with the date,
because that one genuinely is about today.

Clearing a drawdown halt also **rebases the peak** — otherwise the very next
rupee of loss would breach the same limit again and the reset would be useless.

The cumulative drawdown limit exists because the daily limit alone does not
bound total loss: thirty consecutive days at −3% is −60% of capital without
ever breaching a single daily limit.

## The Guardian

Activates on a confirmed fill, using **your actual fill price**. The stop
travels with your fill so risk stays exactly 1R even when you fill worse than
signalled.

**Exit triggers, in priority order:**

| Trigger | Rule |
|---|---|
| `EOD_SQUAREOFF` | past `mandatory_squareoff` — outranks everything |
| `HARD_SL` / `TRAIL_SL` | **bid** at or below the effective stop |
| `TARGET` | bid at or above target |
| `TIME_STOP` | held `time_stop_minutes` without reaching +1R |
| `REGIME_FLIP` | regime turned definitively opposite |
| `DIVERGENCE` | new extreme on falling **futures** volume with RSI diverging |

Stops are tested against the **bid**, because that is where you sell. LTP can
sit above a bid that has already broken your stop.

**Trailing stop.** Activates only after +1R. `trail = premium − 1.5 × ATR × |Δ|`,
and it **ratchets** — it can never move against you, and never below the
initial stop.

**Hold confidence** (futures-vs-VWAP and ADX) is **advisory only**. It colours
the dashboard. Only stops close positions.

## Expected behaviour

Roughly **0.1 signals per session** on the synthetic tape with default
parameters — about one tradeable setup a fortnight. The most binding gates are
regime, momentum, intraday chop and the risk governor's cooldown.

Nothing is rejected for cost any more, so a quiet stretch means the market is
not offering the setup, not that your account is too small.

If you are getting zero signals for days, open the **gate rejection histogram**
on the dashboard. It will name the gate responsible.
