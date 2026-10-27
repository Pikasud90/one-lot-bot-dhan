# 11 — The Backtest Lab

## Mental model

A backtest is **one strategy × one instrument × one timeframe × one parameter set**. The
strategy reads the instrument's *underlying series* (index, stock, spot FX) and emits
closed-bar entries with stops; the simulator fills and manages them on that series; the
execution model turns each simulated trade into rupees for the instrument actually traded.

| Category | Series read | What is traded | Unit | Cost table |
|---|---|---|---|---|
| OPTIONS | index / stock | a CE on long signals, a PE on short signals, delta ≈ 0.65, nearest monthly expiry | 1 lot | `index_options` / `stock_options` |
| FUTURES | index / stock | the near-month future at the underlying price (basis ignored) | 1 lot | `index_futures` / `stock_futures` |
| STOCK | stock | the stock, intraday | 1 share | `equity_intraday` |
| CURRENCY | spot FX | the near-month currency future at the spot level | 1 lot (1000 units; JPYINR 100 000) | `currency_futures` |
| INDEX | index | nothing — study only | — | — |

Sessions follow the instrument: equity 09:15–15:30 (entries 09:30–14:45, square-off 15:15);
currency 09:00–17:00 (entries 09:15–16:00, square-off 16:45). User-set `entry_start`,
`entry_end`, `exit_time` parameters override the defaults.

## The option model (read this)

No keyless source serves Indian option history, so until the Dhan expired-options endpoint is
wired the Lab prices options itself:

* premium = Black-76(underlying, K, T, σ, r) with σ = India VIX(day) / 100 × `option_iv_mult`
  (1.15) or a constant; the VIX series is part of the corpus (`INDIAVIX`, daily);
* K = the exchange-grid strike whose |delta| is closest to `option_target_delta` (0.65) at entry;
* expiry = last `option_expiry_weekday` (Tuesday) of the month, rolled forward when fewer than
  `option_min_days_to_expiry` remain — **verify the weekday against the current NSE circular**;
* entry fill = fair × (1 + `option_spread_pct`) + slippage ticks; exit fills mirror it; the
  stop is monitored on the **underlying** and the premium is marked at that moment;
* rupee risk per trade = (entry premium − premium at the underlying stop) × lot, so R multiples
  are in option rupees, not index points;
* theta is real: a trade held from 10:00 to 15:15 loses a day's time value in the model.

What it cannot do: skew, intraday IV changes, bid-ask that widens at 09:15 or near expiry,
liquidity gaps. Every option result is tagged `black76_synthetic`. Treat the option numbers
as "the futures result, discounted for premium behaviour", not as a fill history.

## Reading a result

* **Net P&L / expectancy ₹ / max drawdown ₹** — the rupee facts for one unit.
* **Profit factor, expectancy R, payoff, win rate** — the shape of the edge.
* **Sharpe / Sortino** — from daily equity over the window; noise under 20 trading days (flagged).
* **Costs and cost share** — the full stack; a strategy whose cost share is above ~30 % of gross
  moves is paying the exchange, not you.
* **Setups → signals → candidates → trades** — where the funnel narrows (filters, session
  window, cooldown, stop caps).
* **30 % rule** — payoff achieved vs 2.33 needed; PF at a 30 % win rate with the same exits.
* **Breakdowns** by exit reason, side, hour, weekday; **monthly** bars; candle chart with ▲▼ entries
  and × exits; full trade list with MFE/MAE in R.

## Optimizer and walk-forward

* **Optimize**: random search over the strategy's declared ranges (or the research space when
  one exists), objective = SQN-style `mean(R)/sd(R)·√min(N,100)` with a minimum trade count,
  then **plateau selection**: the best of the top-5 after averaging each with 4 jittered
  neighbours. In-sample. Optimistic by construction.
* **Walk-forward**: rolling in-sample / out-of-sample windows (defaults 40/10 sessions in the
  UI; 20/6 for intraday and 120/40 for hourly+ in the research gate); only the concatenated
  OOS folds are reported, next to the default-parameter OOS for comparison.
* **Research gate** (`python -m onelot research`): walk-forward + final robust parameters +
  costs ×2 + slippage ×2 + parameter jitter + Monte-Carlo drawdown (rupees) → PASS / WATCH /
  FAIL. PASS/WATCH parameter sets are stored in `param_sets` and listed in the Lab.

## Promoting a strategy to live

```yaml
strategy:
  live:
    source: lab
    lab_strategy: ema_cross
    lab_timeframe: "15m"          # must be a multiple of data.bar_interval_min
    lab_params: {fast: 9, slow: 21, adx_min: 20}
```

The live engine then evaluates the Lab strategy on its closed index bars (resampled to the
strategy timeframe, complete bars only) and uses it in place of the trend + momentum gates.
Everything else — chop filter, IV percentile, contract selection, net R/R, cooldown, dedup,
risk governor, Guardian — is unchanged. The Lab stop (structural) is translated to premium
points through delta and clamped to 0.5–2× the ATR stop so a wide structure cannot make one
lot an outsized rupee risk.

## CLI

```bash
python -m onelot backtest --strategy supertrend_vol --instrument NIFTY_FUT --timeframe 15m --params '{"st_mult": 2.0}'
python -m onelot research --strategies ema_cross supertrend_vol --instruments BANKNIFTY_OPT BANKNIFTY_FUT --timeframes 5m,15m --trials 30
```
