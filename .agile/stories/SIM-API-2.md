# The Simulator's contract, second edition: what changes for the rebuild

Owner, 2026-11-25: "the window does not fit to scale properly for index and stock due to scale
difference, changing the instruments lag the whole app for few seconds, fix the entire
simulator end to end again as i would say the experience was no where near the premium
professional app so fix it properly as well."

An audit of 25 Nov (four agents, real bars, a real browser, the 15 GB store read-only) found
the causes. It is summarised in `.agile/stories/SIM-AUDIT-2026-11-25.md`. This file is the
contract between the rebuilt page (US-094) and the rebuilt server side (US-095). Everything
in `SIM-API.md` stays as it is unless this file changes it. Neither side changes this file
alone: if it is wrong, say so in your report and keep to it.

## Budgets (the reason for most changes; measured on a store of the owner's size)
- `GET /api/sim/options`: under 100 ms warm, under 1 s cold (today 20 s cold).
- `GET /api/sim/candles`: under 200 ms warm, under 1 s cold.
- `POST /api/sim/sessions` answers in under 150 ms ALWAYS. The session is RUNNING within
  400 ms when its instrument, strategy and timeframe were prepared or used before in this
  run of the app, within 2.5 s cold (today 6 to 48 s).
- While a session is being built no other request of the app is slowed by more than 100 ms
  at the 95th percentile (probe: a thread that ticks every 10 ms sees no gap over 250 ms).
- `advance` of up to 30 minutes: 95th percentile under 20 ms.

## Words added
- CHART TIMEFRAME: what the chart shows (1m, 3m, 5m, 10m, 15m, 30m, 60m). A view only.
- STRATEGY TIMEFRAME: the session's own timeframe; alerts come from it. A session property.
- BUILDING: a session whose bars and signals are still being prepared in the background.
- CHUNK: the minutes one `advance` reveals. The server's cursor may be ahead of what the
  page has drawn by at most one chunk; the page never draws a minute beyond the cursor.

## Routes

### GET /api/sim/options (changed)
Same shape, plus:
- each instrument: `"tick": 0.05` (price step of the managed price), `"lot": 75` (units of
  one trade: the lot of an index option, 1 for a stock), `"unit": "lot"` or `"share"`;
- each strategy: `"plots": [{"id": "st", "label": "Supertrend 3m", "pane": "price"}]`, the
  lines the server can send for it (may be empty);
- `"defaults"` gains `"instrument": "NIFTY", "strategy": "microtrend", "timeframe": "3m"`;
- `"chart_timeframes": ["1m", "3m", "5m", "10m", "15m", "30m", "60m"]`.
First and last come from a cache of the store's first and last minute per series, never from
a count of bars.

### GET /api/sim/days?instrument=KEY (new, open)
`{"instrument": "NIFTY", "days": ["2021-11-03", "..."], "first": "...", "last": "..."}`: the
stored trading days (at least 300 one-minute bars), oldest first, cached per series. For the
date picker and the previous day, next day and random day buttons.

### GET /api/sim/candles?instrument=KEY&timeframe=3m&until=ISO&count=300 (new, open)
No session, no token, no strategy. One bounded read of the store.
```json
{"instrument": {"key": "NIFTY", "label": "NIFTY 50", "kind": "INDEX", "tick": 0.05},
 "timeframe": "3m", "tf_minutes": 3, "until": "2026-11-03T11:42:00+05:30",
 "candles": [[1762141500, 25010.5, 25031.0, 25001.2, 25022.4, 0]],
 "forming": [1762150200, 25020.0, 25024.5, 25018.0, 25021.0, 0]}
```
- `candles`: up to `count` (50 to 1000, default 300) CLOSED candles of the timeframe that end
  at or before the minute `until` (that minute is the last one included), oldest first; the
  sixth value is the volume (0 for an index).
- `forming`: the partial candle of the bin that holds `until` when that bin is not complete,
  built only from minutes up to `until`; else null.
- `until` absent: the last stored minute. A bad instrument or timeframe is a 400 whose
  detail names what is allowed; an `until` outside the stored range is clamped and said so
  in `"note"`.
The page uses it to redraw at once when the instrument, the date or the chart timeframe
changes (with `until` = the minute before the chosen start, or the session's cursor).

### GET /api/sim/prepare?instrument=&strategy=&timeframe=&start= (new, open)
Warms the caches for that choice in the background (bars, daily bars, the signal frame). It
creates no session and writes nothing. `{"key": "NIFTY|microtrend|3m", "status": "building",
"ms": 0}` then `"status": "ready"` with the build time. Identical keys join one build; at
most one build runs and one waits (a newer choice replaces the waiting one). The page calls
it when the choice changes, so that Play finds the work done.

### POST /api/sim/sessions (changed)
Same body. New optional key in `params`: none. The answer always comes at once:
- when the choice is ready: as before, `session.status` is `"RUNNING"`;
- when it is not: `session.status` is `"BUILDING"`, `candles` are there (the cheap read),
  `alert` and `trade` are null, and `"build": {"step": "reading bars", "elapsed_ms": 230}`
  (`step` is one of `reading bars`, `building signals`, `pricing`). The page polls
  `GET /api/sim/sessions/{id}` every 250 ms until the status is `RUNNING` or `FAILED` (then
  `"detail"` holds a plain sentence). Any action on a BUILDING session is a 409 with "The
  session is still being prepared."
`session` gains `"chart": {"tick": 0.05, "lot": 75, "unit": "lot"}`.
To keep the replay clock when the user changes the instrument, the strategy or the strategy
timeframe in the middle of a session, the page starts the new session at the old cursor plus
one minute.

### POST /api/sim/sessions/{id}/advance (changed)
Body `{"minutes": N, "until": null}` with `until` one of:
- `null`: reveal exactly N minutes (as before);
- `"event"`: reveal up to N minutes and STOP after the first minute that produced any event,
  so that all events of the answer belong to its last minute;
- `"alert"`: reveal up to N minutes and stop after the first minute that produced one of
  ALERT, EXIT_SIGNAL, CLOSED, CANCELLED, DAY_END, DATA_END (the things that need the user);
  FILLED, STAGE, STOP_MOVED, TARGET_MOVED and ALERT_EXPIRED do not stop it.
The answer gains:
- `"marks": [[1762150200, 25049.0, 431.0, 1335.0]]`: one row per revealed minute at whose
  close a trade was OPEN: `[minute start, mark (managed price), mark_price (traded price),
  open_pnl in rupees]`; empty when flat;
- `"overlays": {"st": [[1762150200, 24980.2]]}`: for each plot of the strategy, the new
  points of strategy-timeframe candles that CLOSED within the revealed minutes (`[candle
  start, value]`; a point whose value is not defined yet is left out).

### GET /api/sim/sessions/{id} and the create answer (changed)
Gain:
- `"events"`: the session's last 200 events, oldest first (so a reload restores the log and
  the chart's markers);
- `"overlays"`: as above, for the candles in `candles` (closed candles to the cursor);
- `"marks"`: as above, for the open trade since its entry (at most 400 rows); empty when flat.
`take`, `skip`, `close` and `end` answer as before, plus `marks` for the minutes they reveal.

### Alert, trade and event objects (changed)
- alert: gains `"lot"`, `"unit"`, `"expires_in_min"` (market minutes left at the cursor).
- trade: gains `"lot"`, `"unit"`, `"stop_price"` and `"target_price"` (the traded-price
  equivalents of the PRESENT stop and target: for an option by the trade's own curve, see
  SIM-API "One curve per trade"; equal to stop and target for a stock), `"risk_inr"` (the
  first risk in rupees), `"open_r"` (open result over the first risk), `"minutes_in_trade"`.
- event `ts`: FILLED, and a CLOSED with reason MANUAL, SIGNAL_EXIT or SESSION_END, carry the
  START of the minute at whose open the trade was filled or closed; every other event keeps
  the close of its minute (today a fill at the 11:43 open is logged as 11:42).
- session `status`: `BUILDING`, `RUNNING`, `DAY_END`, `ENDED`, `FAILED`.

## Rules both sides rely on (added)
7. A switch is a hard reset on the page: nothing of the previous session (candles, lines,
   markers, levels in the price scale, cards) survives the start of the next one.
8. Events change the picture only at the end of a chunk: with `until: "event"` the page can
   draw a chunk's minutes one by one and apply the answer's `alert`, `trade` and `summary`
   when it reaches the last one. Between two events an open trade is drawn from `marks`.
9. A manual close or end pressed while the page has not yet drawn the whole chunk: the page
   first draws the rest of the chunk at once, then sends the action, so the fill the user
   sees follows the last minute on screen.
10. The server keeps at most a few sessions in memory and rebuilds an older one from its
    stored rows when it is asked for; the answers are the same either way.
11. Caches never change an answer: a session built from warm caches equals one built cold,
    minute for minute and event for event (a test proves it), and a cache is dropped when
    bars are written to its series.

## Clarifications of 25 Nov
Written by US-095 (the server side) where this file left room; nothing above is changed. The
page (US-094) and the join (a third agent) can rely on these.

1. `build` and `detail`. The create answer carries `build` (BUILDING) or `detail` (FAILED) at
   its top level AND inside `state`; `GET /api/sim/sessions/{id}` carries them at its top level
   (its body is the state). A FAILED read has `detail` and no `build`; a RUNNING one has neither.
2. The create answer's `events`, `overlays` and `marks` are at its top level and inside `state`.
   For `take`, `skip`, `close` and `end`, `events` stays what SIM-API.md says (the events the
   action produced, not the last 200); `marks` is added (rows only for minutes `end` revealed).
3. "Prepared or used before" means the same instrument, strategy, strategy timeframe AND a
   start in the same block of 30 IST calendar days: the signals are built per block window
   (from the block's first day, minus the strategy's history, to the end of the next block).
   Another day in the same block is warm (RUNNING in the create answer itself); a day in
   another block builds its signals again (the bars are kept, so it is faster than cold).
4. A newer create takes the waiting place of a build: a session whose build was still WAITING
   then turns FAILED with "A newer choice replaced this session before it was ready." A build
   that is RUNNING is never stopped. Two creates of the same block window join one build.
5. `GET /api/sim/prepare`: `start` may be left out (the last stored minute then). When the
   waiting place holds a session's build, a prepare is not queued: it answers
   `"status": "building"` and the page asks again (it polls every second). `ms` is the time so
   far while building, the build's own time once ready. A bad instrument, strategy or timeframe
   is a 400, as for a create. It writes nothing.
6. A session that is not in memory (a restart, or it left memory: four built ones are kept) is read
   from its stored row at once: status BUILDING with its stored trades, summary and last
   events and the candles to its stored cursor, while it is rebuilt; RUNNING as soon as the
   rebuild is done (in the same answer when its frames are kept). Actions on it are a 409 until
   then. `GET /api/sim/options` never touches a session.
7. Resume: the cursor is saved on every event, on take, skip, close and end, and at least every
   30 revealed minutes. After a restart `session.cursor` of the read is the cursor it resumed at,
   at most 29 minutes before the last minute the page drew, never before the last event; the
   page redraws from there.
8. `GET /api/sim/candles`: a `count` that is not a whole number from 50 to 1000 is a 400 (not
   clamped). `until` names a minute (seconds dropped); in a hole of the store the candles end at
   the last stored minute before it; `until` in the answer is the minute asked for, or the first
   or last stored minute when it was clamped (then `note` says so). An offset whose `+` a query
   string turned into a space is read as `+`. The candles equal the session's own candles at
   the same cursor (a test proves it).
9. `GET /api/sim/days`: mock sessions (weekend dates that were not real trading, per
   `data/hygiene.py`) are left out; `first` and `last` are the first and last stored minute.
10. `marks` and `overlays` values are rounded to two decimals. A mark row exists for every minute
    at whose close the trade was OPEN, from the fill's minute on. On a read, `marks` are the
    last 400 rows of the open trade. The opening range's high and low (`orb`) are sent only
    from the first candle after the range is complete (before that a later bar could change
    them).
11. New trade fields: `minutes_in_trade` counts the stored one-minute bars from the fill's minute
    to the cursor's (0 while PENDING_FILL); `open_r` is null while not OPEN; `stop_price` and
    `target_price` of an option are the model premium of the trade's curve at the next minute.
    Closed trades in `trades` (and `/api/sim/trades`) carry `lot`, `unit`, `risk_inr`,
    `minutes_in_trade` and the traded stop and target at the close; `open_r` is null. Rows
    stored before US-095 have null in these.
12. `alert.expires_in_min` is `10` minus the minutes revealed since the alert (never below 0).
13. `chart.tick` (and the options' `tick`) is the instrument's tick size in
    `config/universe.yaml`: for an index that is its options' tick (0.05).
14. Event times, as above and as the engine already did: FILLED and a CLOSED at an open
    (MANUAL, SIGNAL_EXIT, SESSION_END at the exit time, a GAP_ exit) carry the start of that
    minute; a SESSION_END or MANUAL close at the day's or the data's last close carries that
    close. The audit's "11:42" for a fill at the 11:43 open was the page's printing.
15. (the join on real bars) A manual `close` answers with the trade still OPEN: it fills at the
    next minute's open. A page that is paused then reveals that one minute itself (an `advance`
    of 1), so the close is seen at once; a playing page lets the clock reveal it. No change on
    the server.
16. (the join on real bars) An `overlays` series is drawn with a gap wherever points are left out
    (rule 10's opening range, a warm-up): the page never joins two points across a missing
    candle, except a line that goes on at the next day's first candle. No change on the server.
