# 14 — Live Desk: every watchlist instrument under several strategies at once

The Live Desk tab replaces the single-instrument Bank Nifty view with a two-panel desk:
**indices on the left, stocks on the right**, an **open-positions strip** above everything and a
**market-context strip** (India VIX, NIFTY, BANKNIFTY, SENSEX, USD/INR, a manual GIFT NIFTY slot)
between them. The original gate-based Bank Nifty option engine still runs and is folded away
under the panels.

## What is tracked (`config/desk.yaml`)

| Panel | Instruments | Expressed as | Default strategies |
|---|---|---|---|
| Indices | BANKNIFTY, NIFTY, FINNIFTY, MIDCPNIFTY, SENSEX | **buy a CE** (bullish) / **buy a PE** (bearish), 1 lot; exit = sell it back | ema_cross 12m · supertrend_vol 5m · orb 5m |
| Stocks | RELIANCE, M&M, SBIN, HDFCBANK, ITC, TMPV (Tata Motors PV), MARUTI, ADANIGREEN, ICICIBANK, HEROMOTOCO | **buy or sell 1 share**, intraday | ema_cross 15m · supertrend_vol 15m · mean_reversion 5m |

Every instrument can list its own `strategies:` (name, timeframe, params — the same keys the
Lab's *Apply best params* prints). Edit the file and press **Reload desk config** in Settings.

Tata Motors demerged in 2025: `TMPV` is the passenger-vehicle entity you hold; `TMCV` (commercial
vehicles) is in `universe.yaml` and can be swapped in.

## How a row works

Each instrument row shows name · type · unit, the last price and day change, one chip per running
strategy (green/red when its last bar fired), and a state badge:

* **TRACKING** — bars arriving, no entry on the last closed bar of any strategy.
* **BUY / SELL** — a signal is pending (a card appears above the panels with the full alert text).
* **HOLD** — you confirmed a trade; the desk manages it every poll.
* **HALTED** — the risk governor has stopped everything.

Expanding a row shows four panels — **Market**, **Regime & volatility** (regime label, ADX, ATR
and its percentile, RSI, VWAP, EMA20/50 trend, NIFTY bias), **Risk governor** (this instrument's
trades today plus the global limits) and **Data health** (bar age, source, delay) — then a table of
the strategies with their last verdict and reason, and the recent signals.

## Signals and positions (nothing is placed for you)

A strategy fires only on the **bar that just closed** on its own timeframe. The alert carries the
instrument, the unit (1 share / 1 lot), the entry, stop and target, the rupee risk and reward
**after charges**, the net R/R and plain instructions, for example:

```
BUY 1 lot (30) BANKNIFTY 54300 CE (Δ≈0.65, est. premium ₹412.30 → outlay ₹12,369)
Underlying 54,267.40 → stop 54,120.00 (premium ≈ ₹330.10, risk ≈ ₹2,560)
Target 54,560.00 (premium ≈ ₹575.40, reward ≈ ₹4,810; net R/R 1.88)
Exit = SELL the same option back. Charges ≈ ₹84 round trip. premium is a Black-76 estimate …
Square off by 15:15. Strategy ema_cross 12m, signal bar 10:36.
```

Press **Trade taken** (with your actual fill) or **Skipped**. A taken signal becomes a position
the desk re-evaluates every poll: trailing stop, target, time stop, square-off time. When one of
those triggers you get an **EXIT** alert every poll until you press **Closed** with your exit
price; the trade then lands in the desk journal (Home) with net P&L after charges, and the risk
governor's daily loss / drawdown / consecutive-loss counters update.

Option premiums are **synthetic** (Black-76 off India VIX, the same model the Lab uses) until the
Dhan option chain is wired into the desk; the alert says so. Place the order off the live chain.

## Data

* Without the Data API the desk polls **Yahoo 2-minute bars** (one batched request per minute,
  ~15 minutes delayed). Everything works; the badge says *desk yahoo ~15m*.
* With the Data API active (`dataPlan: Active` on the profile) the desk switches itself to
  **Dhan 1-minute bars** (incremental, 15 requests per poll, far below the 5-per-second limit).
* Outside market hours it polls every 5 minutes and the rows show *CLOSED* health with the last bar.

Freshness rule: a strategy bar counts as "just closed" when it completed within one fine bar of
the latest data. With 2-minute bars a 5/15-minute bar never closes on an exact boundary, so an
equality test (which the 1-minute Bank Nifty engine uses) would never fire.

## Endpoints

| | |
|---|---|
| `GET /api/desk` | full snapshot (also inside `/api/stream` as `state.desk`) |
| `POST /api/desk/signal/{id}/ack` | `{"action":"TAKE","fill_price":…}` or `SKIP` |
| `POST /api/desk/position/{id}/close` | `{"exit_price":…,"reason":"MANUAL"}` |
| `POST /api/desk/context` | `{"key":"GIFTNIFTY","last":25410,"change_pct":0.3}` — manual slot |
| `POST /api/desk/reload` · `POST /api/desk/poll` | reload `desk.yaml` · poll now |
| `GET /api/desk/journal` | closed desk trades |

GIFT NIFTY has no free feed (it trades on NSE IX); the slot exists so your own scraper or a paid
feed can post it.
