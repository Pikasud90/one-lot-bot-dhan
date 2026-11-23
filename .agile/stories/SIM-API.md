# The Simulator's API contract (US-075 serves it, US-076 calls it)

Owner's request of 2026-11-23: "simulate live trade on past data with dummy money but real
scenarios ... on both past data as well as the live feed". This file is the one source of
truth for the JSON between the engine and the page. Neither side changes it alone: if it is
wrong, say so in your report and keep to it.

## Words

- **Session**: one instrument, one strategy, one timeframe, from a start instant (mode `past`)
  or following the live feed (mode `live`). One open trade at a time.
- **Cursor**: the start time of the last one-minute bar the session has revealed. The server
  owns it. The page asks for more minutes; the server never runs a clock of its own.
- **Managed price**: the price the stop and target live on and the chart shows: the share
  price for a stock, the INDEX for an index option (a call is long the index, a put short).
- **Traded price**: what is bought and sold: the share, or the option premium.
- All times are ISO 8601 with the +05:30 offset. Candle and minute arrays carry the bar START
  as unix seconds (UTC). Money is in rupees as numbers with two decimals.

## Routes

Reads are open; every POST needs the header `x-onelot-token` (the page's `api()` helper sends
it). Errors: 400 with `{"detail": "<plain sentence>"}` for a bad request, 404 for an unknown
session, 409 when the action does not fit the state (no alert to take, a trade already open).

### GET /api/sim/options
```json
{"instruments": [{"key": "NIFTY", "label": "NIFTY 50", "kind": "INDEX",
                  "first": "2021-11-08T09:15:00+05:30", "last": "2026-11-20T15:29:00+05:30",
                  "live": true, "trade": true}],
 "strategies": [{"name": "microtrend", "label": "Micro trend (7 timeframes)",
                 "kinds": ["INDEX"], "timeframes": ["3m"], "default_timeframe": "3m",
                 "description": "one line"}],
 "speeds": [1, 2, 5, 10, 60, 300],
 "defaults": {"capital": 100000, "stop_atr_k": 1.5, "target_r": 3.0, "extend": true}}
```
`live` says whether the running desk has a feed for the instrument. `trade` is false for a
training instrument (MIDCPNIFTY): it can be simulated, and the page says "training only".

### POST /api/sim/sessions
Body: `{"instrument": "NIFTY", "strategy": "microtrend", "timeframe": "3m", "mode": "past",
"start": "2026-10-05T09:15:00+05:30", "params": {}}`. `start` is null for `live`. `params`
may override `stop_atr_k`, `target_r`, `extend`, `capital`, `auto_exit` (default true: a
strategy exit signal closes the trade at the next minute's open; false: the page must).
Answer:
```json
{"session": {"id": "s_9f2c", "instrument": {"key": "NIFTY", "label": "NIFTY 50", "kind": "INDEX"},
             "strategy": "microtrend", "timeframe": "3m", "tf_minutes": 3, "mode": "past",
             "start": "...", "cursor": "2026-10-05T09:14:00+05:30", "status": "RUNNING",
             "capital": 100000, "params": {"stop_atr_k": 1.5, "target_r": 3.0, "extend": true},
             "premium_note": "real traded premiums where stored, otherwise modelled"},
 "candles": [[1759635900, 25010.5, 25031.0, 25001.2, 25022.4, 0]],
 "state": {"...": "as GET /api/sim/sessions/{id}"}}
```
`candles`: up to 300 CLOSED candles of the timeframe that end at or before the cursor, oldest
first, so the chart has history. The cursor starts on the minute before `start`.

### POST /api/sim/sessions/{id}/advance
Body: `{"minutes": 1}` (1 to 390; ignored in `live`, which returns what has arrived).
```json
{"cursor": "2026-10-05T09:17:00+05:30",
 "minutes": [[1759635900, 25010.5, 25018.0, 25005.0, 25016.2, 0]],
 "events": [{"seq": 12, "ts": "2026-10-05T09:18:00+05:30", "kind": "ALERT", "text": "Buy a CALL: all seven trends up", "data": {}}],
 "alert": null, "trade": null, "summary": {"...": "see state"},
 "last_price": 25016.2, "over": false, "day_end": false}
```
- `minutes`: the newly revealed one-minute bars, oldest first. The page builds its candles
  from them (bins anchored at 09:15 each day, `tf_minutes` wide).
- `events`: everything that happened in those minutes, in order. Kinds: `ALERT`,
  `ALERT_EXPIRED`, `FILLED`, `STAGE` (data.stage one of BREAKEVEN, LOCKED, TRAILING),
  `STOP_MOVED`, `TARGET_MOVED` (data.stop / data.target), `EXIT_SIGNAL`, `CLOSED`, `DAY_END`,
  `DATA_END`. `ts` is the instant the user could know it (a bar's close).
- `alert`, `trade`, `summary`: the state AFTER these minutes (same shapes as below).
- `day_end`: the session's trading day just finished (the next advance starts the next
  trading day). `over`: no more data (past) so the session is finished.

### POST /api/sim/sessions/{id}/take   body `{"alert_id": "a_17"}`
Opens the trade. The fill is the OPEN of the next one-minute bar after the cursor, so the
answer carries `trade.status = "PENDING_FILL"`; the fill arrives as a `FILLED` event in the
next `advance`. 409 if the alert is gone or a trade is open.

### POST /api/sim/sessions/{id}/skip   body `{"alert_id": "a_17"}`
### POST /api/sim/sessions/{id}/close  body `{}`
Closes the open trade at the open of the next one-minute bar (`CLOSED` event, reason MANUAL).
### POST /api/sim/sessions/{id}/end    body `{}`
Closes an open trade the same way and marks the session ENDED.

### GET /api/sim/sessions/{id}
The body below plus `"candles"`: the same closed candles as in the create answer (up to 300, to
the cursor). `take`, `skip`, `close` and `end` answer with this body WITHOUT `candles`.
```json
{"session": {"...": "as above, with the present cursor and status RUNNING|DAY_END|ENDED"},
 "alert": {"id": "a_17", "ts": "...", "expires": "...", "side": "LONG", "action": "BUY CALL",
           "entry": 25016.2, "stop": 24970.0, "target": 25154.8, "risk": 46.2, "reward": 138.6,
           "qty": 75, "traded": {"what": "NIFTY 25000 CE 28 Oct", "price": 412.5, "source": "real",
                                 "stop_price": 382.0, "target_price": 502.0},
           "risk_inr": 2287.5, "reward_inr": 6712.5, "costs_inr": 92.4, "reasons": ["..."]},
 "trade": {"id": 41, "status": "OPEN", "side": "LONG", "action": "BUY CALL", "qty": 75,
           "entry_ts": "...", "entry": 25018.0, "entry_price": 413.2, "source": "real",
           "stop": 25031.5, "target": 25154.8, "stop0": 24970.0, "target0": 25154.8,
           "stage": "BREAKEVEN", "best": 25055.0, "progress": 0.27,
           "mark": 25049.0, "mark_price": 431.0, "open_pnl": 1335.0,
           "exit_ts": null, "exit": null, "exit_price": null, "exit_reason": null,
           "gross": null, "charges": null, "net": null, "r_multiple": null,
           "traded": {"what": "NIFTY 25000 CE 28 Oct", "source": "real"}},
 "summary": {"trades": 3, "winners": 1, "net": -412.5, "gross": -180.0, "charges": 232.5,
             "avg_win": 2210.0, "avg_loss": -1311.2, "best": 2210.0, "worst": -1800.0,
             "equity": 99587.5},
 "trades": [{"...": "closed trades of this session, newest first, shape as trade"}]}
```
- `entry`, `stop`, `target`, `best`, `mark` are MANAGED prices (they go on the chart).
  `entry_price`, `mark_price`, `exit_price` are TRADED prices. For a stock the two are equal.
- `source` is `real` or `model` (a modelled premium); `traded.what` is the share's name for a
  stock. `r_multiple` is the net result over the first risk in rupees.
- `stage` is FIRST_STOP | BREAKEVEN | LOCKED | TRAILING (the ratchet's stages).
- `ALERT`, `FILLED` and `CLOSED` events carry ids and prices in `data`: `ALERT` {alert_id, side,
  action}, `FILLED` {trade_id, entry, entry_price, stop, target, source}, `CLOSED` {trade_id, exit,
  exit_price, exit_reason, net}.
- Session `params` also carries `auto_exit` and `stop_atr_tf` ("60m" or "own"). Limits: `stop_atr_k`
  0.1 to 10, `target_r` 0.5 to 20, `capital` 1,000 to 1e9, `extend` and `auto_exit` booleans,
  `minutes` a whole number 1 to 390 (a bool, a string or a fraction is a 400).
- `session.premium_note` is plain words about where option prices come from and, when it applies,
  that India VIX is stored only to a date before the cursor (modelled premiums then use that last
  value) or not at all.
- A start needs one stored minute before it (a start on the very first stored minute is a 400);
  a start in a hole of the store goes on at the next stored minute. A real option price may come
  from the next traded minute within 3 minutes for a fill or an exit (`entry_wait_min`,
  `exit_wait_min` in the stored detail); marks never look ahead.
- `exit_reason`: STOP, BREAKEVEN_STOP, LOCK_STOP, TRAIL_STOP, the same with a `GAP_` prefix,
  TARGET, GAP_TARGET, SIGNAL_EXIT, SESSION_END, MANUAL, DATA_END.

### GET /api/sim/trades?session_id=&limit=200
`{"trades": [...]}`: closed simulator trades, newest first, each with `session_id`,
`instrument`, `strategy`, `timeframe`, `mode` added. All sessions when `session_id` is absent.
### GET /api/sim/trades.csv
The same rows as a CSV download (one header row).

## Rules both sides rely on

1. No look-ahead: a signal on a candle is an `ALERT` only once the cursor has passed that
   candle's close. An alert lasts 10 market minutes (`expires`), then `ALERT_EXPIRED`.
2. Fills are at the open of the next one-minute bar, with the desk's slippage and charges.
3. The stop and target follow `src/onelot/manage/ratchet.py`, bar by one-minute bar. A level
   moved by a minute holds from the next minute. A stop or target hit closes the trade by
   itself (a resting order would); a strategy exit closes at the next minute's open when
   `auto_exit` is true, and otherwise only raises `EXIT_SIGNAL`.
4. An index option is closed at the session's option exit time (reason SESSION_END); so is an
   intraday stock trade. Nothing is carried overnight in this version.
5. Simulator trades are stored in their own tables (`sim_*`). They never appear in the paper
   test, its scoreboard or its CSV.
6. One lot for an index option, one share for a stock.
