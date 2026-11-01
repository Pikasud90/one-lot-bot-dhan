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

## Paper trades (every alert, never an order)

Every alert the desk raises also opens a **paper trade**, automatically, so strategy evidence
builds up without you clicking anything (ADR-001, US-001). That includes alerts the risk
governor blocked and alerts you skipped or let expire: the paper book is evidence, not
permission. It never sends an order, never touches the risk governor's counters and never
writes to your desk journal.

* **Unit:** 1 share for a stock, one lot of the alerted option for an index (the lot the alert
  names; a lot that disagrees with `universe.yaml` is kept and logged), whatever `risk.lots_per_trade` says.
* **Fill:** the open of the first desk bar that **opens after the alert and has closed**, on
  the same trading day and opening before the 15:30 close, within the alert's validity (plus
  `paper.feed_delay_minutes` for delayed-grade alerts, whose bars arrive late). Stocks pay
  `backtest.slippage_pct` (a percent) against you, snapped to the tick; options are priced
  like the Lab (`fair × (1 + option_spread_pct) +
  option_slippage_ticks × tick`, rounded up) with the strike, expiry and IV frozen at the alert.
  When the alert carries no IV it is implied once from the alert's premium.
* **No fill:** a fill bar that opens beyond the stop closes the trade `NO_FILL` /
  `GAP_THROUGH_STOP`; no eligible bar before the deadline or the session end closes it `NO_FILL`
  (`expired` / `session_closed`). No price is ever invented while waiting, and these rules
  follow the bars' time, not the wall clock. The one exception: a waiting trade on a series
  the desk no longer tracks (taken off the watchlist) never gets bars, so it closes `NO_FILL`
  (`series_untracked`) once the wall clock passes its deadline (plus the feed delay when
  delayed).
* **Grade:** `live` when Dhan served the bars, `delayed` otherwise; a live trade whose feed
  turns out to mix 1- and 2-minute bars is regraded `delayed` (a `REGRADED` event says when).
* **Restart:** open paper trades reload; alerts from the last `paper.reconcile_days` days that
  never got a paper trade (a crash in between) are booked at the alert's own time. This also
  covers alerts raised while `paper.enabled` was false: switching it back on and restarting
  backfills the ones from the last `reconcile_days` days. The `OPENED` event says which path
  booked a trade (`opened_by`: `on_alert` or `reconcile`) and when (`booked_at`). A paper
  trade that could not be written when its alert fired (a locked database) is retried on the
  following polls (up to 500 kept in memory) and booked once, at the alert's own time.

Config (`config/config.yaml`, section `paper:`): `enabled` (false stops *new* paper trades; open
ones still run), `feed_delay_minutes` (15), and `reconcile_days` (3, not listed in the file;
add it under `paper:` to change it). Trades and their events are in the `paper_trades` and
`paper_events` tables (money in integer paise); the desk snapshot's `paper` block shows the
book's counters next to the desk's own `desk_hook_errors`. Managing an open paper trade to its
close (stop, target, session end) is US-002.

## Data

* Without the Data API the desk polls **Yahoo 2-minute bars** (one batched request per minute,
  ~15 minutes delayed). Everything works; the badge says *desk yahoo delayed ~15 min: A, B, C +N*
  (the first three delayed instruments, then a count of the rest).
* With the Data API active (`dataPlan: Active` on the profile) the desk switches itself to
  **Dhan 1-minute bars** (incremental, 15 requests per poll, far below the 5-per-second limit).
  Provenance is per series: on a Dhan desk the instruments the Yahoo fallback served (Dhan
  returned no bars for them) are named in the badge (*desk dhan+yahoo delayed ~15 min: SBIN*;
  *desk dhan+yahoo live* when there are none), their alerts are tagged delayed, and their SIGNAL
  titles start with "(delayed ~15m)".
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

## Alerts (Home tab) — the agreed lifecycle

Every alert, F&O or stock, is handled the same way:

1. **Raised** with an IST timestamp, the signal bar, the data it was computed on (`yahoo 2m ·
   ~15 min delayed` or `dhan-ws 1m · live`), the strategy and timeframe, the signal strength and a
   **confidence** score (0–100: 35 % signal strength, 30 % saved-backtest evidence for that
   strategy/instrument/timeframe, 20 % the strategy's own archived-alert record, 15 % regime
   fit; neutral 50 where there is no evidence yet — the parts are shown).
2. **Time-to-live** (`signal_valid_minutes`, default 10): a countdown on the card. **Trade taken**
   (with your fill) or **Skipped**; otherwise it **expires**. Either way it is archived.
3. **Taken → Monitoring**: the position is re-checked every poll with the live price (the
   WebSocket tick when the Dhan socket is on): trailing stop, target, the originating strategy's
   own exit/opposite signal on its latest closed bar (`hold_check` says "still long — HOLD" or
   why not), square-off time for intraday, a session time stop for swing. Each trigger is an
   EXIT alert until you press **Closed** with the exit price.
4. **Archive with outcome**: every archived alert (taken or not) is re-audited against the bars
   that followed, at its own entry/stop/target: TARGET, STOP, SQUARE_OFF (intraday horizon
   ended), OPEN, with best/worst excursion in R and the estimated one-unit P&L. Ties inside one
   bar go to the stop. The per-strategy **scoreboard** (hit rate, average R) is built from this
   and feeds the confidence score of the next alert.

Horizons: index options are **intraday** (square-off 15:15). Stocks are **swing** by default
(`stock_horizon: swing`, `swing_max_sessions: 5` in `config/desk.yaml`): a taken stock position is
carried across sessions, recovered at the next start, resubscribed on the socket, and re-checked
against its strategy every poll until the strategy exits, the stop/target hits, or the time stop.

## Instrument focus (Live tab)

A dropdown at the top of the Live tab selects any tracked instrument and shows its Market,
Regime & volatility, Risk governor and Data health panels, its strategies' latest verdicts, and
a chart (any timeframe) with every alert raised on it marked. The accordion rows remain below.

## Live Data API path (once the token is valid and the plan active)

* The engine switches from the synthetic market to the Dhan feed **in place** (Save credentials,
  or Settings → Go live); the desk switches to **Dhan 1-minute bars + WebSocket ticks** for all
  watchlist instruments on the engine's socket (one connection, 5000-instrument capacity).
* Index alerts price the option from the **live option chain** (nearest expiry, strike nearest
  the target delta, bought at the ask, spread shown) instead of the Black-76 model; the alert
  says which.
* The market pulse adds the NIFTY put/call ratio.
