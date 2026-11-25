# Operations Runbook

## Daily

**Before the open (once, ~09:00 IST)**

0. Check what one lot can afford at today's levels:
   `python -m onelot.sizing_check --spot 52000 --iv 0.14 --days 15`
   If it reports that nothing in your delta band is affordable, the engine will
   alert but you will not be able to take anything inside your limits.
1. Start the engine: `scripts/start_mac.sh` (or `scripts\start_win.bat`).
2. Open http://127.0.0.1:8787/ and read the **warnings banner**. It is empty
   when everything is correct.
3. Confirm the badges: mode, health `HEALTHY`, risk `ACTIVE`.
4. Confirm the regime line shows an `as_of` date of the **previous** session.
   Today's data is never used to compute today's regime.
5. If you see a notification self-test toast, alerts are working. If you do
   **not**, fix that before trading — see below.
6. On the phone (US-097): about a minute after every start the desk sends "Desk started 09:02.
   Token valid until 21:31. Feed: live."; a start after 09:15 on a trading day adds "Started
   late: alerts before 10:57 were missed." At 09:05 IST on a trading day a running desk sends
   the same three facts ("Desk up at 09:05. ..."). Every start is sent, a restart later the
   same day too; the 09:05 check comes once a day. Nothing can be sent while the laptop sleeps
   or the desk is stopped: treat a missing 09:05 message as "the desk is down" and start it.

**During the session**

The engine is hands-off. When a signal fires you get a toast and the dashboard
card, with 75 seconds to act.

1. Execute on your Dhan terminal.
2. Enter the price you **actually filled at** and click **Trade taken**.
   The stop travels with your fill, so your risk stays 1R.
3. If you did not take it, click **Skipped** — the record matters for tuning.

When an exit alert fires, close on your terminal, then enter your actual exit
price and click **Close**. That writes the journal entry and updates the risk
governor.

**After the close**

- Check the journal metrics. `costs / gross` is the number to watch: if costs
  are eating a large share of gross profit, your R multiples are too small.
- Check the gate histogram if the day was quiet.

## Failure playbook

| Symptom | What it means | What to do |
|---|---|---|
| Health `DEGRADED`, "WebSocket down" | Socket died; REST failover is live | Signals are suppressed by design. Open positions are still managed. Usually self-heals. |
| Health `DEAD` | No usable data | Manage any open position **manually on your terminal**. Do not trust displayed prices. |
| "access token expires in Xh" | Dhan token is ageing out | Generate a new token and update `.env`, then restart. |
| "access token EXPIRED" | Token is dead | Same, urgently. The engine cannot see the market. |
| `RL001` / rate limited | Broker throttled us | The limiter backs off automatically. If persistent, check nothing else is using the same API key. |
| "another engine is already running" | Lock file held | Two engines would share one quota and race position state. Stop the other. If certain it is stale, delete `data/engine.pid`. |
| No signals for days | Probably by design | Open the gate histogram. It names the responsible gate. |
| Lots of UNAFFORDABLE alerts | One lot does not fit your capital | Expected on a small account — see docs/07. Run `python -m onelot.sizing_check`. Lower the delta band or raise capital if it is constant. |
| Halted on cumulative drawdown | ~3 losing trades at 7% each | Do not reset reflexively. The leash is short by design; understand the losses first. |
| No toast on macOS | Notification permission | System Settings → Notifications → your terminal app → Allow. `osascript` returns success even when the banner is suppressed. |
| Engine evaluating 0 bars | Feed not delivering ticks | Check health; check `chain polls` is increasing. |

## Crash recovery

If the engine dies with an open position, restarting recovers it from the
database and resumes the Guardian. You will see:

```
RECOVERED 1 open position(s) from the database. The Guardian is resuming;
verify against your broker terminal before acting.
```

**Verify against your terminal before acting on anything it says.** The
database records what the engine believed; your broker records the truth.

## Network loss

The Guardian keeps managing positions on the last known prices and marks the
trade `degraded_data`. If you lose connectivity entirely, manage the position
manually and record the exit afterwards through the dashboard so the journal
and risk governor stay correct.

## Halts

A halt is sticky: it survives restarts and needs a deliberate **Clear halt**.
Before clearing an automatic halt, understand *why* it fired. A cumulative
drawdown halt in particular is telling you the strategy is not working in
current conditions.

## Maintenance

| When | What |
|---|---|
| Every session | Glance at warnings and the journal |
| Weekly | Review the gate histogram for a gate that never passes |
| Monthly | Re-check `config/costs.yaml` against a fresh contract note |
| Each January | Refresh `config/nse_holidays.yaml` from the NSE circular |
| Before any config change | Run `python -m onelot.replay` to confirm you have not silently zeroed the signal rate |

## Delivery pullback trades (US-067)
Since 2026-11-12 the desk runs one rule: the pullback rule on the 18 stocks, as delivery
trades of about Rs 10,000 carried for days. Every other slot is paused. These are alerts and
paper trades only: the system never places an order.

- Entry alert: after a session's close, for a stock whose close is above its SMA200, with
  NIFTY above its SMA200, on the first close 8% or more below its 20-session closing high:
  "BUY q shares of S at the next open". It comes about 10 minutes after the close (15:40),
  stays pending until the next session and does not expire after ten minutes.
- Exit alert: after the first close above the SMA20, after 30 sessions, or after a close 15%
  below the entry fill: "SELL q shares of S at the next open" with the reason.
- The paper book fills at the first one-minute bar of the next session: the open plus 0.03%
  to buy, minus 0.03% to sell. Charges are the delivery charges plus Rs 14.75 depository
  charge per sale (`config/costs.yaml`).
- Late: if the desk starts during that session, the fill is the first bar after the start,
  marked late. Missed: if the desk was off for the whole next session, the entry alert is
  closed as "missed: the desk was not running" and no trade opens. An exit is never missed:
  it sells at the first bar after the desk starts, marked late.
- A restart at any moment resumes where it was: one alert and one trade per stock and signal
  date, never two. A desk started after a close it missed reads that session at start.
- A first bar more than 20% from the previous close puts "possible corporate action" on a
  pending or open trade in that stock. Check the figures by hand.
- Pause or resume a slot: in `config/desk.yaml` set `enabled: false` (or `true`) on the slot
  and reload the desk. A paused slot raises no alert; a trade it already opened is still
  managed to its close; its scoreboard row stays and says "paused".
- Is the daily history fresh? Home's Delivery block and each stock card say "daily history
  complete to <date>" and the store's last day. A session counts once its last one-minute bar
  is at or after 15:10 and the clock is 10 minutes past the close (`delivery:` in
  `config/desk.yaml`). A short history (under 201 days) or a missing session stops the entry
  alerts of that stock and shows on the card; it never stops the exit tests of an open trade.
  A day whose bars stop early is read as it is for exits only once a later session is
  complete, with "<date> read from incomplete data" on the card and the trade. A Muhurat
  session counts the next day; a bar with a bad price is left out with a note.
- A day off the calendar counts as a special session only with at least `special_min_bars`
  (30) one-minute bars in `delivery:`; fewer are a stray bar, ignored with a note.
- Known limit: after a catch-up over several sessions, an entry signal on the day after an exit
  signal can be dropped, because that exit is still pending. The reference would take it.
- With the Dhan Data API on, the desk tops up the store's one-minute bars once a day outside
  09:00 to 15:35 when the store is behind (`delivery.top_up`, on by default): at most three
  tries in a window, 20 minutes apart, stopped at 09:00; its errors show on Home. Otherwise run
  a backfill from the Data tab. Since US-097 the top-up also starts when only another series it
  writes is behind: a tracked index's one-minute bars or India VIX (daily or one-minute). A
  series with no stored bars at all does not start it.

### The wide list: a paper test on 222 stocks (US-099)
Owner, 2026-11-25: the same pullback rule as a PAPER test on a wide list of stocks. It never
places an order, and the desk 18 keep working exactly as above.

- The list is data: `config/delivery_universe.yaml` (key, name, Dhan's symbol, security id and
  segment, group desk, alpha or new), written by `scripts/write_delivery_universe.py` from the
  swing-universe study. The 204 stocks that are not desk instruments are delivery-only: daily
  bars only, no feed, no intraday bars, no card.
- The switch: on the pullback slot in `config/desk.yaml`, `wide: true`, `cap_open: 10`,
  `evening_round: true` and `universe:` (the file above). With `wide: false` the desk is as
  before this story; reload or restart after a change.
- The evening round, once per finished session, after the close (never between 09:00 and
  15:35), on its own thread: it fetches the last 30 or so daily bars of each delivery-only
  stock through the desk's adapter (Data API on; at most two requests a second; an error is
  noted and the others go on; a resumed round fetches only what is missing), then reads the
  rule on each stock with the desk 18's own code. A signal on a close is a paper trade filled
  at the next session's open, taken from that session's daily bar in the next round; open
  trades are managed on the daily bars with the rule's own exits. Up to four tries a session,
  30 minutes apart; a round waits for NIFTY's session to be complete (a wait is not a try).
  With the Data API off (a token expired) no round runs: it runs when the API is back.
- Waiting for the broker's bars (US-102): the round reads a session when the broker's daily
  bars of that session are there. It checks from just after 15:35 and then every 30 minutes,
  also through the night and before 09:00 the next morning. A check that finds no bar of the
  session yet costs at most 5 requests (5 stocks, the next check 5 stocks further down the
  list) and is not one of the 4 tries; the round's note says
  "Dhan has not published the daily bars of ... yet" (or "Dhan did not answer" when every
  request failed). When the bars are there, the session's signals are on time: they take the
  free places within the cap and are alerted. Once one stock has the bar, the tries count as
  above. If the bars never come before 15:35 of the next trading day, that day's round reads
  the old session late (recorded, never alerted).

  What Dhan does (found on 25 Nov 2026): its daily history ends at the PREVIOUS trading day.
  Dhan's support page says so ("provides data only up to the previous trading day and does
  not include the current day's data"), and on 25 Nov the bars of the day were not there at
  15:42, at 18:11 or at 19:22. So this round never reads a session on its own evening: it
  reads it once Dhan has added the day, which is some time on the next day. The hour is not
  measured yet (the round's own record, `wide_rounds.finished_at`, will show it). If that
  hour is after 09:00 the round cannot read at all, because from 15:35 it waits for the new
  day instead. Dhan's quote answer does hold the day's open, high, low, closing price and
  volume after the close, for every stock in one request: reading the day from it the same
  evening is the next piece of work (US-106).

- Adjustments: when the fetched closes differ from the stored ones by more than 0.5% on the
  overlap (a split, a bonus, a demerger), the stock's whole history is fetched again and
  replaces the stored one, and the round says so. If that refetch fails, the stock is stale:
  it is not read until a later round agrees or refetches. An active trade of a re-based stock
  is put on the new basis (signal close and entry fill rescaled, the share count of a filled
  trade by the inverse), so its stop is read on the same basis as the closes; the trade's note
  says so. A stock whose stored history is shorter than 201 sessions is fetched whole once only.
- Unadjusted actions: a one-session fall of more than 15% (the close or the open against the
  close before) may be a split or bonus the broker left unadjusted. Within the 20 sessions of
  the rule's high it blocks the entry ("a split or bonus not adjusted?"); since a trade's
  entry it holds that trade's exit for the owner (the note says "exit held for review"; the
  round lists it as held). A held trade keeps its place until the history is re-based or the
  owner decides. A rise is never blocked.
- Two tiers. Every signal is a paper trade (tier "all"). At most `cap_open` trades are open
  within the cap at a time, the desk 18's active trades counted. A signal of the round's own
  session takes a free place, the desk group first, then the deepest pullback first; a trade
  keeps its tier for life. Only a within-the-cap trade is alerted (Home, the computer,
  Telegram: "wide list"), to enter and to exit. The others are recorded: "recorded for the
  test, not alerted: the cap of 10 is full". A signal read late (a round that did not run)
  is recorded and never alerted.
- Where to look: Home and the Live Desk's Stocks card show the Wide list block (the last
  round, the places in use, the open trades of both tiers, the score of both tiers beside the
  backtest's figure, the closed trades and the stocks closest to a signal). The same as data:
  `GET /api/delivery/wide` (reads only). Its own tables: `wide_trades`, `wide_state`,
  `wide_rounds`; the desk 18's `delivery_trades` is never written by the round.
- History: the lead copies the study's daily bars into the store once, with the desk running:

      PYTHONPATH=src .venv/bin/python scripts/import_daily_universe.py \
          --from data/research/swing_2026-11-24/daily.sqlite --store data/onelot.db --dry-run

  then the same without `--dry-run`. It adds only the delivery-only stocks' bars the store
  lacks, 500 bars a commit, never a desk instrument's, never a bar twice. Without it the first
  round fetches each stock's whole history (204 requests, about two minutes).
- The replay: `scripts/pullback_wide_replay.py --daily FILE --nifty-store STORE` runs the
  product's round day by day over a research file and compares its tier "all" with the
  study's trades (dates and exit reasons; sizes differ on purpose: the product buys the
  number of shares nearest Rs 10,000).

## Index options: the micro-trend rule (US-068)
- The only rule on the four traded indices (NIFTY, BANKNIFTY, FINNIFTY, SENSEX). MIDCPNIFTY is
  tracked for training only (`trade: false` in `config/desk.yaml`): it raises no alert and opens
  no paper trade. Supertrend(14, 1.7) on 3m (the trigger), 5m, 10m, 15m,
  30m, 60m and 4h, each read on its last closed bar; a 4h bar runs 09:15 to 13:15 and 13:15 to
  the close, and the afternoon one is first read the next session.
- Alert: at a 3-minute close where all seven newly point up (BUY a call) or down (BUY a put),
  inside the option entry window, one lot of the monthly contract, one live idea per index.
- Contract (US-104): the monthly contract nearest delta 0.65 AMONG CONTRACTS THAT TRADE: a last
  price, a two-sided quote (bid and ask above 0), open interest above 0 and a spread of at
  most 10% (`backtest.option_max_spread_pct`, 1 to 50). The bid, the ask and the spread shown
  are that contract's own. A contract with no quote or no open interest is never named, however
  near 0.65 its delta reads (between 23 and 25 Nov 2026 three alerts named such dead contracts,
  10% to 14% away from the index). When the nearest tradeable delta is more than 0.10 from 0.65 the alert
  notes "nearest tradeable contract: delta 0.52 (wanted about 0.65)". When nothing in the chain
  trades the model prices the alert ("est.") and its first note says "no tradeable contract in
  the live chain". The fill-cost recorder watches the same contract.
- Exit: the paper trade closes at the first one-minute bar at or after the 3-minute close where
  3 of the 7 point against it, else at the session end, unless the stop or the target comes
  first (next section).
- Each trend needs 70 of its own bars before it has a reading. The 4h trend needs about 35
  sessions: the desk reads 75 days of stored one-minute bars for it, so keep the store topped up.
  Without them, or on the delayed 2-minute Yahoo feed (no 3m, 5m or 15m bars), nothing fires.
- The index card shows the seven trends, "aligned up", "aligned down" or "not aligned", and for
  an open trade "reversed k of 7, exit at 3". The backtest of this exact rule lost a little.
- Clean bars (US-097): the rule raises no alert for an index while the bars it reads today are
  not all one-minute bars of the live source: a bar of today from the delayed Yahoo feed (a
  desk started on Yahoo before the token was renewed), or a hole of more than five minutes
  before the signal (the open counts; a bar before 09:15 does not). The card says "waiting for
  clean one-minute bars today: ..." with the reason and from when the bars are clean, under the
  trends and in the slot's "Why". A live bar of the same minute replaces a delayed one: while
  the live source serves, the desk asks it again for the day from the first delayed bar (at
  most every five minutes), so one poll that fell back to Yahoo holds the rule only until the
  live bars are back. A desk that switches source starts its bars afresh. A hole the live
  source cannot fill holds the rule for the rest of the day. The pullback rule is not affected.
- Spread (US-097): when a quote of the alerted contract not older than 60 seconds is at hand
  (the option chain the alert was priced on, or the fill-cost recorder's last quote), the alert
  carries `spread_pct`, half the bid-ask spread in % of the mid, and from 1% on the line "Wide
  spread now: about 1.8% a side" in its text and in the Telegram message. No quote, no line;
  the alert never waits for one and its prices do not change. FINNIFTY and SENSEX are often
  wider than the cost model's 0.6% (`docs/research/LIVE_ALERTS_2026-11-24.md`).

## How a trade is managed: stop, target and trail (US-079)
- A slot with `ratchet: true` in `config/desk.yaml` (today the micro-trend slot) is managed by
  one rule, `src/onelot/manage/ratchet.py`. Every other slot is managed as before, and so is a
  paper trade opened before this rule was switched on: it finishes under its own rules.
- First stop: 1.5 x ATR(14) of 60m bars of the index (of the share for a stock), read on the
  last 60m bar closed at or before the signal. First target: 3 times that risk. On the alert
  both are measured from the alert's price; the paper trade measures them from its fill.
- As the best price covers the planned move (entry to first target): at 25% the stop goes
  above breakeven (costs plus 5% of the move), at 50% it locks 35% of the move, from 80% it
  trails 20% of the move behind the best price and the target moves out by half the move each
  time the price comes near it. The stop never comes back. A level moved by a minute bar holds
  from the next minute bar; a bar touching both the stop and the target counts as the stop; a
  bar opening beyond a level closes at its open.
- The strategy's own exit (3 of 7) and the session end still close the trade as before.
- Close reasons: STOP (first stop), BREAKEVEN_STOP, LOCK_STOP, TRAIL_STOP, TARGET, SIGNAL_EXIT,
  SESSION_END. The Paper panel shows an open trade's stage, its stop and target now and the
  first ones; a closed one shows the reason in words.
- A position you took shows the same levels and stage as its paper trade. Each time the stop
  or the target moves you get one notification: "move your stop to X". Move your own stop to
  that level; the desk never places or changes an order.
- A target that moved out and is then hit is still called TARGET; the event detail says it
  moved (`target_moved`).
- The alert archive audits such an alert at its first stop and target only, not the trail, so
  its result can differ from the paper trade's; the archive says so beside the result.
- The Backtest Lab applies the same rule to a strategy with `ratchet` on, minute by minute. The
  Lab's cost term is its slippage and the paper book's is its charges, so the two can differ
  by a few ticks at a breakeven stop.
- Check the paper book against the rule on stored bars (opens the store read-only):
  `PYTHONPATH=src .venv/bin/python scripts/ratchet_desk_replay.py --store PATH/onelot.db`
  `--sessions 20 --instrument NIFTY`. It prints how many exits match and each one that does not.

## Market snapshot button (US-071)
Home, under "Today": the button "Take market snapshot" saves the last price of the 5 indices and
18 stocks the desk tracks, with the day change, the previous close, the time of each price and
its source, in the table `market_snapshots` (time in UTC; the page shows IST). The card lists the
saved snapshots newest first; "Show" opens one, with each price's change since the snapshot
before it, so you can check by hand what moved. The newest opens after you press the button.
- While the desk is still starting and holds no price, the button answers 409 and saves nothing.
- API: `POST /api/snapshots` (token, like every other POST) and `GET /api/snapshots?limit=30`.
- Snapshots are kept; nothing deletes them. They are read-only records: no order is placed.

## Machine Learning Lab (US-069, US-072)
Read a cell from the top: the range first (the plus or minus figure and the 68% and 95% bands),
then how often each band held in the last 100 candles. The up figure is not a signal: it is right
no more often than a coin. An instrument marked "opening candle" has its own, wider band, because
the day's first candle carries the overnight gap. "Move size" (calm, normal or active) ranks the
band against its own history: after an active reading the next move was about 1.5 times that after
a calm one. The Lab raises no alert and no trade.

## Simulator (US-075)
The Simulator trades one strategy's alerts with dummy money, on stored bars from a start time
(mode `past`) or on the running desk's feed (mode `live`). It never places an order and never
writes to the paper test: its trades are in their own tables.
- Alerts: the strategy's own signals, built as the Lab builds them on 120 days of history. A
  signal becomes an alert once its candle has closed; it lasts 10 market minutes. One alert or
  one trade at a time; a signal meanwhile is dropped.
- Fills: a taken alert fills at the open of the next one-minute bar. A stock pays the Lab's
  slippage of 0.03% on the entry, on a stop and on an exit at an open; a target fills at its
  level. Index options are one lot of the contract the Lab chooses (monthly, delta near 0.65).
- Stop and target: the first stop is 1.5 x ATR(14) of the index or share on 60m bars (on the
  strategy's own bars when they are 60m or longer); the first target is 3 risks away. Then the
  rule in `manage/ratchet.py` moves them minute by minute. A strategy exit closes at the next
  minute's open; open trades close at the session's exit time (15:15). Nothing is carried
  overnight.
- Premiums: the real traded premium from `data/expired_options.db` when it holds that strike's
  bar of that very minute (never a later one: a replay must not use a price the user has not
  seen; a strike that did not trade in the minute is priced by the model and marked `model`;
  read only, never fetched), today's from the desk in mode `live`, otherwise the Black-76 model. The Lab's half spread and two ticks apply to either. Each
  trade records whether its entry and exit premiums were `real` or `model`.
- Costs: the charges of `config/costs.yaml`, intraday equity for a share and index options for
  an option, on both legs. R is the net result over the first risk in rupees.
- One curve per trade: when a premium is real, the model is calibrated to it (the implied
  volatility at that level and time), so a stop and target premium, the rupee risk, R and any
  modelled exit lie on one curve with the real price; each trade records `iv_used` and `iv_source`
  (`implied` or `vix`). The chart's price scale always holds the entry, stop and target.
- India VIX: the model's volatility comes from the stored India VIX. When the cursor is past the
  last stored VIX day (or there is none) the session's price note says so in plain words and the
  model uses the last value (or a fixed volatility).
- Gaps and short days: a hole in the stored bars is crossed (the next advance goes to the next
  stored minute); a short evening session (Diwali Muhurat) ends its day at its last minute and
  closes an open trade there. A start needs one stored minute before it. `minutes` must be a whole
  number from 1 to 390; a session's `capital`, `stop_atr_k` and `target_r` must be sane numbers
  and `extend` / `auto_exit` true or false, or the answer is a 400.
- Checking it on real bars: `scripts/dev_seed_bars.py` copies bars (read only from the source)
  into a private instance's database; `tests/sim/tools/drive_sim.py` drives its API, replays
  every trade with the ratchet and checks the money (not collected by pytest).
- Nothing silently: closing a trade before its fill, or ending the session, the day or the data
  first, gives a `CANCELLED` event and the trade is kept with status CANCELLED (never a result).
  The answers of take, skip, close and end carry `events` and `minutes` so the page shows them.
- Storage: `sim_sessions` (a session, its cursor and settings), `sim_trades` and `sim_events`.
  A session not in memory is rebuilt from them at its cursor. `GET /api/sim/trades` and
  `/api/sim/trades.csv` list the closed simulator trades of every session.

## Telegram on the phone (US-087)

The desk also sends its alerts to the Telegram app on your phone, and warns you before the
Dhan token runs out. It sends only: it never takes an order or a command from Telegram.

**Setup, without a terminal** (Settings tab, "Telegram on your phone" card)

1. In Telegram open **@BotFather** and send `/newbot`. Copy the token it gives you.
2. Paste the token in the card and press **Save**. The card then says "set", "not set" or
   "wrong shape", never the token. It is written to `.env` (mode 0600) and the phone channel
   is switched on in place: no restart.
3. Open your new bot in Telegram and press **Start**.
4. Press **Find my chat id**. The desk asks Telegram which chats have written to the bot and
   lists them (id and first name or title only). Press **Use this chat**. With no message
   yet it says "send any message to your bot first"; with a bad token, "the token was refused".
5. Press **Send a test message**. The card says "sent", or the plain reason it was not.

Keys by hand are still possible: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`
(see `.env.example`), read at start. `notify.telegram: false` in `config/config.yaml` switches
the whole channel off; the card then says so.

**What arrives on the phone** (one automated test per row)

| What | Level and kind | Rule |
|---|---|---|
| A trade alert | SIGNAL | instrument, side, the contract (expiry, strike, CE or PE), entry, stop, target, the "Managed:" line (how the stop and trail rule manages it), strategy, confidence, valid until. Muted (no sound) outside the session and when risk-blocked. Sent at most 8 at once, the best by confidence; the rest are one digest. An alert still queued when its valid-until passes is skipped and you get one "Missed" note. |
| "Move your stop" | WARNING, kind `stop` | one message per move of a taken position (the key is the position and the new levels). Not repeated. Muted outside the session. |
| The exit signal | EXIT | ONE per alert: the desk raises it once (the alert's paper trade closing, or a taken position's own advice with a stored flag). The channel adds no second exit message for it. (The old Bank Nifty engine raises an exit again every poll; for that one the channel keeps its existing budget: the first message, then at most three reminders five minutes apart.) |
| Critical desk states | CRITICAL | token expired at start, feed dead, index disagreement, halt. Always with sound. The same title is not sent again within 30 minutes of the last one sent. |
| Dhan token reminders | WARNING, kind `token` | the ladder below. Always with sound. A stage reminder is dropped if it could not be delivered before the token ran out. |
| Volume Watch | WARNING, kind `volume` | each note under its own key; a repeat of the same key is held back, a new key never. Muted outside the session. |
| Desk start and 09:05 check (US-097) | WARNING, kind `desk` | "Desk started 09:02. Token valid until 21:31. Feed: live." about a minute after a start (with "Started late: ..." after 09:15 on a trading day), and "Desk up at 09:05. ..." on a trading day; every start is sent (a restart the same day too), the 09:05 check once a day. Always with sound. |

Everything else (INFO, other warnings) stays on the page and the computer. Every message is a
row in `telegram_outbox` before it is a request, sent in order (exit and critical first),
one request per second, retried after an outage, and a row is never sent twice on purpose
(a restart in the middle of a send may repeat one, marked "resend").

**The token reminder ladder**

The expiry time is read from your Dhan profile. The desk looks about once a minute (inside
the housekeeping loop; only when Dhan credentials exist) and sends, once per token:

- 60, 30, 15 and 5 minutes before it ends ("Dhan token: 23 minutes left", with the time in
  IST and "Settings, Dhan card: Renew, or paste a new token."; when the desk can renew by
  itself it says it will try);
- "Dhan token expired" when it ends;
- while it is still expired: one reminder at 08:30 IST on each trading day, before 09:15; a
  desk that first looks later that trading day (started after 09:15, or the laptop slept)
  sends it at that first look, once that day, until the close (US-097);
- "Dhan token renewed: valid until 21:31 on 25 Nov" when the expiry moves forward; the ladder
  is armed again.

A start inside a window sends only the nearest stage not yet sent, with the true time left (at
23 minutes left: "23 minutes left", then 15, 5, expired), never a burst of older stages. What
was sent is remembered in the database (`token_reminder_state`), so a restart repeats nothing.
When the desk cannot read the expiry it says so once ("Cannot read when the Dhan token
expires") and still reports an expired token. The same messages show as a toast on the page
and on the computer; the page shows the time left in the header chip, the Today strip and the
Settings card.

**What does NOT arrive**: nothing is sent while the laptop sleeps, is offline or the desk is stopped
(a missing 09:05 "Desk up" message means the desk is down). The reminders are sent by the running
desk: a token that ends overnight while the desk is stopped is told at the next start ("expired"),
not before.

**A network that blocks Telegram** (found on 25 Nov 2026): office and institute networks may
block `api.telegram.org`, the one address these messages go through, while the Telegram app
itself still works. "Find my chat id" and "Send a test message" then say "Could not reach
Telegram (ConnectTimeout). This network may be blocking api.telegram.org ...". Nothing is wrong
with the token or the desk. On such a network no message leaves: each waits in the queue and is
sent when the desk is back on a network that reaches Telegram. To use Telegram there, ask the
network's admin to allow `api.telegram.org` on port 443, or run the desk on another network
(home broadband, a phone hotspot). To test from a terminal, with no token in the command:
`curl -m 10 -o /dev/null -w "%{http_code}\n" https://api.telegram.org/` prints `302` when the
address is reachable and `000` when it is blocked.

**Status** (no secret): `GET /api/notify/status` says for Telegram whether it is configured and
in use, when the last message was confirmed, the class of the last error ("HTTP 403") and how
many messages are waiting.

**Development only**: the environment variable `TELEGRAM_API_BASE` points the client at a fake
on this machine. It is accepted only when its host is `127.0.0.1` or `localhost`; anything else
is ignored with one warning. It cannot be set from the page.

## Volume Watch (US-089)
Index bars carry no volume, so the watch reads the market's volume from the 18 desk stocks (the
basket, `volume.basket` in `config/desk.yaml`). It runs inside the desk for as long as the desk
runs; outside 09:15 to 15:30 on trading days there is no volume, and the page shows the last
session and the day's profile.
- What it reads: the one-minute bars the desk already holds for today, and the store's bars for
  the sessions before (read queries only; it asks the broker for no bars of its own). Turnover is
  volume x close in rupees; a minute's buying part comes from the `bvc` rule (the study's choice).
- Baselines: the expected turnover of a window (one minute, the last 5 minutes, the day so far)
  is the median of that same window's turnover over the previous 20 full sessions, for a stock
  or for the basket as a whole, so an ordinary day reads about 1.0. The spike score compares
  the last 5 minutes with the previous 60 full sessions at the same minute. A session with fewer than 300 one-minute bars is never a baseline.
  They are built once a session on their own thread; until then the page says so in its notes.
  A request never waits for them (US-097): the first view of a session answers at once with
  `"building": true` and "Preparing this session…", and the page asks again every 2 seconds.
- The words: quiet below 0.7 times normal over the last 5 minutes, busy from 1.5 (the study's
  15th and 85th percentiles).
- When it alerts: a market spike score of 2.5 or more (about 1.3 a day), a single stock at 4.0
  or more (about 2.6 a day), never twice for the same market or stock inside 15 minutes, at most
  12 a day, and only for the last three closed minutes (in time or not at all). Each alert is
  stored once in `volume_alerts` (a restart repeats none) and goes through the Notifier as a
  WARNING of kind `volume`: the page's toasts, the computer's notifications and Telegram.
- "Likely to move" on a market alert names an index only when its own pressure in the spike
  (influence x net flow over expected turnover, summed over all the basket's stocks) is among
  the two largest and at least 0.05: a spike led by ITC does not name the bank indices.
- A watch alert is not a trade alert: it never opens a paper trade and never says buy or sell.
  The study found a size effect (the index's next 5 to 60 minutes about 1.5 times as large as
  usual) and no direction: the flow's side was right about half the time.
- Order book: one quote request a minute for the basket while the market is open and the Data
  API is on, the best five price steps a side, stored in `volume_book` (60 days kept). After 3
  failed reads in a row it stops until the next session.
- How to switch it off: in `config/desk.yaml`, `volume.alerts_on: false` (no alerts at all),
  `volume.notify: false` (alerts stored and shown, never sent), `volume.book: false` (no quote
  requests); then "Reload desk config" in Settings or a restart. A failure inside the watch is
  caught, counted and shown in the page's notes; the desk's other work goes on.
- Not studied yet: what follows a single-stock spike, and the order book (recorded, not tested).
- Routes: `GET /api/volume` (`?date=YYYY-MM-DD` for a stored session, `?index=KEY` for the price
  line), `/api/volume/profile`, `/api/volume/alerts?limit=100`, `/api/volume/study`.
- Checking a day by hand: `PYTHONPATH=src .venv/bin/python scripts/volume_replay.py --store
  data/onelot.db --date 2026-11-20` opens the store read-only, prints the day and recomputes the
  newest minute straight from the bars.

## Desk uptime (US-097)
The desk was not running on 5 of the 7 sessions from 13 to 23 Nov 2026 (16 to 20 Nov) and
nothing recorded it.
- Heartbeat: while the desk runs, the housekeeping loop writes at most one small row update
  every five minutes to `desk_uptime`: one row per calendar day with the first and last beat,
  the beats, the minutes of market hours the beats covered (two beats more than ten minutes
  apart are a gap, not covered) and whether the day's first start came after 09:15 on a
  trading day. A restart continues the day's row.
- `GET /api/desk/uptime?days=30`: per trading day of the app's calendar, from when to when the
  desk ran inside market hours, the share of the session covered, missed or not, started late
  or not. Days before the first row ever are marked `known: false`. It reads the store, so it
  answers while the desk is not running.
- Home: one line in the Today strip, only when a finished session of the last ten was missed or
  covered under 90%: "The desk did not run on 5 of the last 10 sessions: 16, 17, 18, 19, 20 Nov."
- On the phone: the start and 09:05 messages (Daily, step 6, and the Telegram table).
