# The Volume Watch contract (US-089 serves it, US-090 shows it)

Owner, 2026-11-24: "indices don't have volume, so you can take into account of the top Indian
stocks volume data and try to understand the volume flow in the overall market ... make me an
Indian market volume indicator, it should show via chart what's the current volume in what
directions on a separate dashboard tab, it runs constantly 24*7 and we also try to identify
high volume time and low volume time, it should act as our volume watchdog, alerting us seeing
abnormal spikes in volume, also it should understand the correlational effect of this volume
and where it might effect, alerting me in time."

This file is the one source of truth for the words, the formulas and the JSON. Neither side
changes it alone: if it is wrong, say so in your report and keep to it.

## Words and formulas
- BASKET: the stocks watched (config `volume.basket`; default: the 18 desk stocks). Index
  bars carry no volume, so the market's volume is read from the basket.
- TURNOVER of a minute: volume x close of the stock's one-minute bar, in rupees (shares are
  not comparable across stocks, rupees are).
- BUYING and SELLING turnover of a minute: turnover x u and turnover x (1 - u), where u is
  the bar's "up share". Default rule: u = (close - low) / (high - low), 0.5 when high equals
  low. US-089's study may replace the rule by a better one of the three it compares; the
  JSON does not change.
- NET FLOW: buying minus selling. FLOW SHARE: buying / (buying + selling) over a window
  (0.50 is balanced; shown as "buyers 58%").
- EXPECTED turnover of a WINDOW (one minute, the last 5 minutes, or the day so far) is the
  median, over the previous 20 full sessions, of the turnover of that same window: the same
  minutes of the day, the same stock, or the basket taken as a whole (a median of sums, never a
  sum of per-stock or per-minute medians). A session with fewer than 300 one-minute bars is
  not full.
- RELATIVE VOLUME is actual over expected for the same window, so about 1.0 is an ordinary day
  by construction.
- SPIKE SCORE z: (ln(basket turnover of the last 5 minutes) - m) / s, where m and s are the
  mean and sd of the same quantity ending at the same minute of the day over the previous 60
  full sessions. Per stock the same with the stock's own turnover.
- STATE of the volume: `quiet` (rvol_5m below 0.6), `normal`, `busy` (1.5 or more), `spike`
  (z at or above the alert level). The numbers live in config and come from the study.
- INFLUENCE of a stock on an index: non-negative weights from a regression of the index's
  one-minute returns on the basket stocks' one-minute returns over the previous 60 sessions,
  scaled to sum to 1; `r2` says how much of the index's minute moves the basket explains.
- PRESSURE on an index: the sum over the basket of influence x (net flow of the last 5
  minutes / expected turnover of those minutes). Above zero: net buying in units of normal
  volume; below zero: net selling.
- A WATCH ALERT is not a trade alert: it never opens a paper trade and never says buy or sell.

## Routes (all GET, open like the other reads)

### GET /api/volume            (the page polls it every 20 seconds while the tab is shown)
### GET /api/volume?date=YYYY-MM-DD   (a stored past session, same body)
```json
{"as_of": "2026-11-25T11:42:00+05:30", "session": "2026-11-25", "market_open": true,
 "source": "live",
 "basket": {"count": 18, "label": "18 large stocks", "missing": ["TMPV"]},
 "now": {"minute": "11:41", "turnover_1m": 412000000.0, "expected_1m": 300000000.0,
         "rvol_1m": 1.37, "rvol_5m": 1.31, "rvol_day": 1.08, "z": 1.4, "state": "normal",
         "flow_share_5m": 0.58, "flow_share_day": 0.51,
         "turnover_day": 34000000000.0, "expected_day": 31500000000.0,
         "net_flow_day": 680000000.0, "up": 11, "down": 6, "flat": 1,
         "text": "Volume is 1.3 times normal for this time of day. Buyers 58%."},
 "minutes": [[1764046500, 412000000.0, 300000000.0, 239000000.0, 173000000.0, 680000000.0, 1.31, 1.4]],
 "index": {"key": "NIFTY", "label": "NIFTY 50", "minutes": [[1764046500, 25016.2]]},
 "stocks": [{"key": "HDFCBANK", "label": "HDFC Bank", "last": 1712.5, "chg_pct": -0.8,
             "turnover_day": 4100000000.0, "share": 0.12, "rvol_5m": 2.1, "rvol_day": 1.2,
             "z": 2.8, "flow_share_5m": 0.31, "flow_share_day": 0.47, "state": "busy"}],
 "indices": [{"key": "BANKNIFTY", "label": "NIFTY BANK", "trade": true, "pressure_5m": -0.42,
              "pressure_day": -0.10, "r2": 0.93,
              "top": [["HDFCBANK", 0.31], ["ICICIBANK", 0.27], ["SBIN", 0.10]],
              "text": "Selling pressure, led by HDFC Bank and ICICI Bank."}],
 "alerts": [{"id": "v_20261125_1139_MARKET", "ts": "2026-11-25T11:39:00+05:30",
             "scope": "MARKET", "key": "MARKET", "rvol": 3.2, "z": 3.4, "flow_share": 0.31,
             "direction": "SELLING", "leaders": ["HDFCBANK", "ICICIBANK"],
             "indices": ["BANKNIFTY", "FINNIFTY"],
             "title": "Volume watch: 3.2 times normal, mostly selling",
             "text": "Led by HDFC Bank and ICICI Bank. Likely to move: NIFTY BANK, FINNIFTY.",
             "evidence": "After such spikes the next 15 minutes were 1.7 times as large as usual; direction was a coin flip."}],
 "book": {"as_of": "2026-11-25T11:41:30+05:30", "bid_share": 0.54,
          "stocks": [{"key": "HDFCBANK", "bid_qty": 182000, "ask_qty": 240000, "bid_share": 0.43, "spread_bps": 1.2}],
          "note": "Resting orders in the best five price steps. Recorded since 25 Nov 2026: not yet studied."},
 "settings": {"z_alert": 3.0, "stock_z_alert": 4.0, "cooldown_min": 15, "alerts_on": true,
              "notify": true},
 "notes": ["Stock bars end at 15:14 since the closing auction began."]}
```
- `minutes`: one row per minute of the shown session, oldest first: `[bar start as unix
  seconds, turnover, expected, buying, selling, net flow summed over the day so far,
  rvol_5m, z]`. `index.minutes`: `[unix seconds, close]` of the index chosen by `?index=KEY`
  (default NIFTY), for a price line over the flow.
- Added 25 Nov: the body also carries `"index_options": [{"key": "NIFTY", "label": "NIFTY
  50"}]`, the indices `?index=` accepts (an unknown key is a 400). `now` is null when no
  minute of the shown session has closed yet. An alert's `evidence` may be absent.
- `source`: `live` (today's session from the running desk's bars), `stored` (a session read
  from the store: a past date, or the last stored session while the market is closed or the
  desk has no bars yet). `market_open` is the desk's own session flag.
- `direction` of an alert: `BUYING` (flow share 0.60 or more), `SELLING` (0.40 or less),
  `TWO_WAY` otherwise. `scope` is `MARKET` or `STOCK` (then `key` is the stock).
- `book` is null when the order book is off, the market is closed or the broker gave none.
- `stocks` sorted by `turnover_day`, largest first. `share` is the stock's part of the
  basket's turnover today. Rupee figures are plain numbers; the page formats them.
- With `?date=` the `alerts` are what the watch WOULD have raised that day (recomputed), and
  `book` is null.
- Errors: 400 with `{"detail": "<plain sentence>"}` for a bad or unknown date (the answer
  names the first and last stored session), never a 500 for missing bars: a stock without
  bars goes to `basket.missing` and the rest still answers.

### GET /api/volume/profile
```json
{"as_of": "2026-11-24", "sessions": 60,
 "minutes": [["09:15", 1210000000.0, 980000000.0, 1500000000.0]],
 "half_hours": [{"from": "09:15", "to": "09:44", "share": 0.19, "rank": 1}],
 "busiest": [{"from": "09:15", "to": "09:44", "share": 0.19}],
 "quietest": [{"from": "12:15", "to": "12:44", "share": 0.04}],
 "weekdays": [{"day": "Mon", "rvol": 0.97}],
 "expiry": {"expiry_day_rvol": 1.12, "other_days_rvol": 0.98, "note": "NIFTY expiry days"},
 "text": "The first half hour carries about a fifth of the day's volume; 12:15 to 13:15 is the quietest hour."}
```
`minutes`: `[minute of day, median basket turnover, 25th percentile, 75th percentile]` over
the last 60 full sessions. `share`: that half hour's part of a normal day's turnover.

### GET /api/volume/alerts?limit=100
`{"alerts": [...]}`: stored watch alerts, newest first, same shape as above plus `session`.

### GET /api/volume/study
What history says, for the page's evidence box and the alerts' `evidence` line:
```json
{"as_of": "2026-11-24", "note": "docs/research/VOLUME_STUDY_2026-11-24.md",
 "facts": [{"id": "size_after_market_spike", "text": "After a market-wide spike the index's next 15 minutes were 1.7 times as large as usual (1.6 in the last year).", "value": 1.7},
           {"id": "direction_after_market_spike", "text": "The direction followed the flow 51% of the time: no edge.", "value": 0.51}],
 "alerts_per_day": {"market": 1.8, "stock": 4.5}}
```

## Rules both sides rely on
1. Only CLOSED one-minute bars are used; the newest minute shown is the last closed one.
2. Nothing here asks the broker for bars of its own: the live numbers come from the bars the
   desk already has; history comes from the store. The order book is the one exception: at
   most one quote request a minute for the whole basket, only while the market is open and
   the Data API is on, and it can be switched off (`volume.book: false`).
3. The watch runs inside the desk for as long as the desk runs. Outside market hours (the
   cash market trades 09:15 to 15:30 IST on trading days) there is no volume: the page shows
   the last session and the profile and says so in plain words.
4. A failure inside the watch never stops the desk's other work: it is caught, counted and
   shown in `notes`.
5. A watch alert is raised once (its `id` is the key), respects the cooldown per scope and
   key, and goes out through the desk's Notifier as `Notification(level=WARNING,
   kind="volume", title=..., body=...)`: the page's toasts, the computer's notifications and
   Telegram (US-087 makes the Telegram channel forward kind `volume`).
