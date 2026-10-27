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
