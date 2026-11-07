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
