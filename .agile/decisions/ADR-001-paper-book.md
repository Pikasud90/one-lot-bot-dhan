# ADR-001: The paper book is a separate, always-on engine component
Status: proposed (confirm at Checkpoint 1)

Context: Today a position exists only after the owner clicks "Trade taken". Strategy quality is therefore measured only by the post-hoc audit
(entry/stop/target replayed on bars) — which ignores fill timing, slippage, spread and exit management. The owner wants paper trades running
for *every* new alert so strategy evidence accumulates automatically, with no real orders ever.

Options:
A — Reuse the existing manual-position path (`Desk.acknowledge`) and auto-"take" every alert. Pros: no new code. Cons: pollutes the owner's real
    manual journal and the RiskGovernor counters (paper trades would trip the daily-loss and max-trades limits meant for real behaviour); can't
    distinguish paper from "I really took it"; the governor would block most paper trades.
B — New `onelot.paper` package: `PaperBook` listens to alert creation, opens a paper trade, and a `PaperManager.step()` runs inside the desk poll to manage it;
    separate tables; separate ledger; never touches the governor. Pros: clean evidence, independent of risk limits, testable in isolation. Cons: exit-rule logic
    must not be duplicated — extract the shared management rules from `Desk._manage_positions` into a pure function both use.
C — Analytics-only "virtual" trades computed from the audit. Cons: no fills/slippage/costs model; cannot show a live paper position.

Decision: **B.**
- Trigger: every alert, including risk-blocked and later-expired ones (the paper book is evidence, not permission). Trade is flagged `grade=live|delayed` from the alert's data source; delayed-feed trades are recorded but excluded from promotion statistics.
- Fill model: entry at the open of the first 1-minute bar *after the alert timestamp* + slippage (stocks: configured pct per side; options: ask + spread when a live chain exists, else the Black-76 estimate + configured half-spread); exit fills pessimistic (stop = gap-through-aware, target = limit-fill only if traded through).
- Management: the same rules as manual positions (stop, trail, target, strategy exit/opposite signal, intraday square-off for index options, swing time stop for stocks), via one shared pure function.
- Ledger: integer paise (`int`), Decimal at the cost-model boundary; charges from the versioned cost table; analytics may use float64.
- Persistence: new tables `paper_trades`, `paper_events` (open, fill, adjust, close with reason); restart recovery reopens managed state.
- No sizing and no capital: one unit per trade; P&L in rupees and R.
- Naming: must not trip the order-verb invariant (no `exit_position`, `square_off`, `place_*`): use `open_paper_trade`, `close_paper_trade`, `session_end_close`.

Consequences: manual "Taken" trades and paper trades are compared (slippage report, US-012); `Desk` gains one hook (`on_alert`) and one call in `poll`; shared management function is a refactor under test (acceptance tests first).

## Amendments after refinement (2026-11-01) — decided by the Manager as tech lead after three independent reviews
Evidence: Dev Alpha, Dev Beta and Tester read the real code; their reports agree on every point below.
1. **Shared RULES module, not one shared function.** The desk advises from one live price (a touch counts, nothing is closed); the paper book works on closed bars (gaps, trade-through targets, stop-first ties). One function for both would change the desk's behaviour. Decision: pure helpers in `onelot/desk/rules.py` (`ratchet_stop`, `r_multiple`, `bars_held`, `sessions_held`, `session_due`, `exit_priority`) used by `onelot.paper.manage.manage_step`. The desk adopts the same helpers later (US-025) behind characterisation tests written first. `Desk` is NOT touched by US-002.
2. **Do not call `lab.simulator.simulate()`.** It cannot be stepped or persisted, forces an end-of-day close, fills targets on touch. Port its rules into `manage_step`; a parity test on intraday fixtures lists the deliberate differences. `lab.execution.execute_trade` is the differential oracle for option legs and costs.
3. **TTL and NO_FILL are judged on DATA time.** A bar is eligible iff `open > created_at` AND `open + resolution <= now` AND `open <= expires_at` (+ `paper.feed_delay_minutes`, default 15, for delayed grade). Otherwise Yahoo alerts (data ~15 min late, TTL 10 min) would all be NO_FILL. A fill bar that opens beyond the stop is NO_FILL (GAP_THROUGH_STOP). An alert after the session end never fills on a later day.
4. **Grade comes from `data.source`**: `dhan*` = live, `yahoo` = delayed. (The desk's own `data.delayed` flag is wrong for Yahoo-fallback series — fixed in US-028.) Mixed 1m/2m spacing grades the trade delayed.
5. **Rounding.** Bar prices and levels convert to integer paise once at the boundary (Decimal, ROUND_HALF_EVEN). Fills snap to the tick against the trader (buys and short-covers up, sells down). Costs are rounded once per leg (ROUND_HALF_UP) from `CostModel.leg()` with the real sides (shorts pay STT on the entry leg — `round_trip_cost` assumes buy-then-sell). `risk_paise` excludes costs; R = net / risk. Units: `slippage_pct` is a PERCENT (0.03 = 0.03%), `option_spread_pct` is a FRACTION (0.006).
6. **Options.** IV frozen at the alert; for live-chain alerts a constant basis (chain premium minus model premium at alert) keeps the trade from showing P&L at birth; fill mirrors `execute_trade` (`ceil_tick(fair*(1+spread) + ticks*tick)`); if repricing fails emit a `PRICING_UNAVAILABLE` event, never a silent zero.
7. **Management semantics.** Trailing stop ratchets at bar close and applies from the NEXT bar; ATR frozen at alert time (no fallback to live regime ATR). Close-reason precedence: STOP > TARGET > SESSION_END > SIGNAL_EXIT > OPPOSITE_SIGNAL > TIME_STOP. Enum member is `SESSION_END` — never `SQUARE_OFF` (the invariant scanner bans that name). Booked events are final and store the bar prices they used (bars may be revised by the feed).
8. **Stock shorts are intraday-only** (a retail cash-equity short cannot be carried overnight): they close at SESSION_END. Swing carry applies to longs only, charged on `equity_delivery`; large overnight gaps quarantine instead of stopping out (corporate-action guard). Specified in US-027; until then every paper trade closes at SESSION_END.
9. **Performance.** Trackers hold ~26,000 1-minute bars (70-day retention), not 3,000: use a per-trade cursor and `searchsorted`; budget tested at 30,000 rows.
10. **Isolation.** `PaperBook` takes its dependencies by injection (`cost_for`, `premium_at`, `BacktestCfg`, specs, clock); it never calls `current_settings()`. `on_alert` deep-copies the alert (the desk mutates it later). Alerts present in `desk_signals` without a paper row (crash between insert and hook) are reconciled at startup.
11. **Option execution cost (review of US-001, 2026-11-01).** A bought option is executed at the option's ask only: `ceil_tick(fair*(1+option_spread_pct) + option_slippage_ticks*tick)` priced at the UNSLIPPED underlying. The paper book is canonical. The Lab currently slips the underlying first on option runs, which double-counts execution cost (about 1% of premium per side); US-031 removes that so Lab R and paper R are comparable.
