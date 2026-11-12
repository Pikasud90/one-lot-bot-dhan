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
- Known limit: after a catch-up over several sessions, an entry signal on the day after an exit
  signal can be dropped, because that exit is still pending. The reference would take it.
- With the Dhan Data API on, the desk tops up the store's one-minute bars once a day outside
  09:00 to 15:35 when the store is behind (`delivery.top_up`, on by default): at most three
  tries in a window, 20 minutes apart, stopped at 09:00; its errors show on Home. Otherwise run
  a backfill from the Data tab.
