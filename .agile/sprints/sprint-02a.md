# Sprint 02a — Owner feedback of 2026-11-07 (tab by tab)

Mode: the owner asked for this sprint to run in this session only, with ONE Sonnet assistant
(max effort) for independent testing; no other sub-agents. The Manager implements, the Sonnet
Tester verifies. Gate (scripts/gate.sh) and review-by-test still apply.

## Stories
| ID | Owner feedback | Notes |
|---|---|---|
| OF-1 | Home: Portfolio on top, then Alerts, then Monitoring | Strategy scoreboard side by side, then Archive (10 per page, load more), Engine alerts, Ledger, Journal, Profile | Monitoring shows alert time and scrolls sideways |
| OF-2 | Live Desk: Instrument focus below Market context / Indices / Stocks, above the Bank Nifty engine | |
| OF-3 | Data: filter coverage by source (Dhan/Yahoo) and timeframe; show the history the Lab can really use (Dhan 1m resampled = 5 years); deeper history | currency intraday is Yahoo-capped (60m 2 y, 5/15m 60 d) |
| OF-4 | Lab: IST trade times (sweep showed UTC 04:15 = 09:45 IST); single runs never stuck RUNNING; walk-forward / optimise finish; Matrix populates; leaderboard sorts asc/desc by win rate and profit % | |
| OF-5 | Alerts: one exit alert per entry (from the paper trade close) whether taken or not; taken trades use the alert prices with optional user-reported prices and the difference; no repeat alerts while a trade is live; no risk governor on alerts or paper trades; primary strategies: ema_cross, supertrend_vol, gap_fade, nr7_breakout, turtle_soup, ema_pullback, cpr_breakout | |

## Findings on 2026-11-07 (before changes)
- Dhan access token expired 2026-11-05 00:02; Yahoo fallback failing ("possibly delisted"); 2026-11-06 raised 1 alert. Token renewal is the owner's step.
- 45 of the last 50 notifications were 'EXIT HDFC Bank — SIGNAL EXIT' (taken position re-notified every poll).
- Lab matrix/leaderboard read 1.3 GB of run artifacts per call (>60 s); zombie RUNNING runs from restarts; 'database is locked' left a single run RUNNING at 1 %.
- Lab trades are stored in UTC and served without IST conversion.

## Stand-up log

## Delivered (2026-11-07, branch sprint-02-owner-feedback)
| Story | Commit | What the owner gets |
|---|---|---|
| OF-4 | f0d2a1d | Lab trade times in IST; no run stuck RUNNING (lock retries, start-up recovery marked 4 stale runs FAILED); Matrix in ~1 s; leaderboard sorts asc/desc by win rate and profit % |
| OF-5 | cd6f807 | One EXIT per alert = its paper trade's close (never repeated); a taken position closes at the exit alert's price; alert prices are the record, the owner's own prices sit beside them with the difference; no risk governor on alerts, paper trades or desk trades; one live alert per instrument and never the same bar twice; the 7 primary strategies at 15m; history_days 25 |
| OF-1, OF-2, OF-3 | acd1ae2 | Home in the owner's order, Monitoring with alert/taken times that scrolls sideways, archive 10 per page, paper scoreboard of the primary strategies, journal with alert vs your prices; Instrument focus moved; Data filters by source/timeframe/series and a "what the Lab can use" view |
| OF-3 | 5b5a7e4, 01475b4 | Coverage scan cached (was ~6 s per Lab/Data load); journal alert time for older trades; bar loads commit per batch so a 5-year Dhan backfill never blocks the desk |

Evidence: `scripts/gate.sh` PASS (6203 passed, 1 skipped) before the last two commits; new tests
tests/acceptance/test_of5_alert_lifecycle.py (23), tests/paper/test_scoreboard.py (10),
tests/test_effective_coverage.py (5), tests/test_upsert_chunks.py (1). In the running app:
single backtest #8340, walk-forward #8341 (4 folds, 190 OOS trades) and optimize #8342 reached
DONE; entries 09:30-14:45 IST; the 3 positions open since 2 Nov closed at their alerts' exit
prices (HDFCBANK SIGNAL_EXIT 693.75, SBIN and MIDCPNIFTY SESSION_END); the desk runs 100 slots
(7 primaries x 15 instruments, gap_fade on stocks only) on Dhan live 1m bars.

## Decisions taken by the Manager (the owner may overrule)
1. One live alert per INSTRUMENT (pending alert, taken position or live paper trade), not per
   strategy: no repeats and one clear idea per instrument. Cost: a strategy that fires while
   another holds the instrument is not paper-traded, so the scoreboard under-samples it.
2. A NO_FILL paper trade is still the alert's one exit ("EXIT ... NO FILL"); a position taken on
   it closes at the desk's mark (last price, or the model premium for an option).
3. An alert with no paper trade (older alerts, or paper off) exits once on the desk's own advice.
4. The desk's trades no longer feed the risk governor (it still guards the Bank Nifty engine).
5. "Close now" stays as an escape hatch for an early exit at the owner's price; the alert's exit
   price is added to that trade when the alert exits.
6. All 7 primaries run at 15m; gap_fade is stock-only (it compares a stock's gap with NIFTY's).

## Open after this sprint
- Telegram is not configured: create the bot with @BotFather and put TELEGRAM_BOT_TOKEN and
  TELEGRAM_CHAT_ID in .env yourself (never paste the token in chat).
- Currencies are Yahoo spot FX: 5m-30m history stays at ~60 days (60m 2 years, 1d decades).
  Longer intraday FX needs a Dhan currency-futures series (a backlog decision).
- Dhan 1m backfill for every Dhan-listed series started 2026-11-07 (data jobs #5 and #6).
- Lab evidence: all primaries negative after costs at 5-15m in earlier sweeps; promote nothing
  to real money on this evidence.
