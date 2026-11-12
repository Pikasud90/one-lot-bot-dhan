# US-067 design note (stage 1)
Import path: pytest's `pythonpath = ["src"]` (pyproject.toml) makes tests import the worktree's code (checked). A bare
`.venv/bin/python` imports the MAIN checkout through the editable `.pth`, so the D13 script needs `PYTHONPATH=src`.

## A. Today
1. `Desk.poll` (desk.py:471) runs `tracker.evaluate` (tracker.py:312), then `_raise_signal` (desk.py:630): a
   `desk_signals` insert (735), `PaperBook.on_alert` (754), the notifier (776). Alerts expire after 10 minutes (939)
   and are marked EXPIRED at start-up (384). `_busy_reason` (789) allows one idea per instrument. `PaperBook.step`
   (book.py:323) fills, manages and closes everything at the session end, with no carry (manage.py:33-51, 380).
   `_reconcile` (book.py:864) books any unbooked `desk_signals` row. The scoreboard (`_paper_scoreboard`,
   server.py:194) reads `paper_trades` only, through `PaperLedger.by_slot`, `primary_slots` (desk/config.py:73) and
   `slot_scoreboard_view` (paper/view.py:162). `/api/desk` returns the snapshot built at desk.py:2056.
2. Daily bars: `_load_daily_history` (desk.py:516) runs once a day. `daily_history` builds `consistent_daily(420 days of
   1d, clean_intraday(160 days of 1m))` (hygiene.py:126), and `tracker.daily_bars` (tracker.py:232) adds the 25
   in-memory live days. The research's DataCache (lab/runner.py:140-151) uses the same function on ALL the 1m history.
   Nothing tops the store up; the only writer is the manual backfill (server.py:660, data/backfill.py:65). The store
   ends on 2026-11-06.
3. Costs: `CostModel.leg` (costs.py:95) is linear for delivery below Rs 66,667 (costs.yaml:73-77). The paper book uses
   `leg_paise` (paper/charges.py:41) and the spec's product (factory.py:41, `equity_intraday`). NO depository charge
   exists in src/ or config/. `ProductCostCfg` is strict (config.py:32,56).
4. Quantity: `unit_qty` is 1 for a STOCK (paper/alert.py:124-128), pinned by test_us001_ac4_ac5_isolation_and_one_lot
   .py:179-189 and test_invariants.py:157. The card says "1 share" (tracker.py:511).
5. Calendar: `is_trading_day` = weekday and not a holiday (clock.py:98). There is no next-session function.
   `current_window` is None on other days (clock.py:139). Special sessions (Budget Saturdays, Muhurat) exist only in
   `hygiene.REAL_WEEKEND_SESSIONS` and in the data. The fixtures count them as sessions.
6. Config: `_slots` (desk/config.py:93) reads name, timeframe and params; the tracker skips non-REGISTRY names
   (tracker.py:136-154). Pinned by test_primary_slots.py:49 and test_tracker_trend.py:262 (NIFTY's real slots).
7. Page: `accRow` (app.js:782); `loadAlerts` -> `#home-signals` (app.js:345, index.html:100); `drawScoreboard` (376).

## B. Design: a small separate delivery path
Reusing the alert-to-paper pipeline is RISKIER. A delivery alert in `desk_signals` would expire, be marked EXPIRED at
start-up, be booked as an intraday trade by `_reconcile` and be audited. A trade in `paper_trades` would be closed at
the session end, and every reader of that table would need a filter. The separate path reuses only pure helpers.
- **Pure rule** (`src/onelot/delivery/rule.py`, no I/O):
  - `PullbackParams(depth, high_lookback, trend_sma, exit_sma, max_sessions, stop, slippage, unit_inr)`;
    `nifty_ok(dates, nifty_dates, nifty_close, p)`;
  - `entry_reading(close, ok, t, p) -> EntryReading` (ok, first_day, high20, depth_pct, sma200, nifty, why);
  - `exit_reading(close, entry_idx, entry_fill, u, p) -> ExitReading` (hit, reasons, sma20, stop_level, sessions);
  - `quantity(close, unit_inr)`; `simulate(dates, open_, close, ok, p) -> list[RuleTrade]` (the reference loop, cumsum
    SMAs); `net_pct(buy, sell, model)`.
- **Daily loader**: `rule_daily(engine, key, now, fine_days=365)` in desk/history.py is `daily_history` with a longer
  1m window, so the last 221+ days are DataCache's exactly; the ladder is unchanged. `delivery/data.days_for_rule(stored,
  live_fine, upto)` adds the live days. Freshness: at least 221 bars; no calendar trading day missing; the last bar is
  the completed session, with its last fine bar within 5 minutes of the close. A failure gives a card note and one
  WARNING per (stock, session), records no evaluation (the next poll retries) and runs outside the desk lock.
- **Top-up**: incremental `backfill(engine, dhan, 19 series, ["1m"])` on a worker thread when the Data API is on and
  the store lags (after the close, and at start); failures are logged and never block.
- **Evaluation (D1)**: `DeliveryDesk.on_poll(now, trackers)`, called guarded from `Desk.poll` after `_step_paper`.
  Session t is complete when t < today, or when t is a calendar trading day and now >= close_at + 10 minutes (+ the feed
  delay). The sessions not yet in `delivery_evaluations` are evaluated in order (at most 10). Exits use all of them;
  an entry is raised on the newest only, and older ones become MISSED (Q2).
- **Entry alert (D2)**: one transaction writes the PENDING_ENTRY row (UNIQUE series_key+signal_date, insert if absent)
  and the evaluation row. Then the notifier is called (dedup key, TTL to the next session's close) and `entry_told_at`
  is set. Untold rows are re-told at start: at worst one repeated message, never a trade.
- **Entry fill (D3)**: from the tracker's fine bars. s1 is the first date in the data after the signal (covers holidays
  and special sessions). If `Desk.started_at` <= s1's first bar, fill at its open × 1.0003. If the desk started during
  s1, fill at the first bar >= started_at, late. If s1 ended before the start, MISSED. Every transition is a
  compare-and-set UPDATE `WHERE status=<expected>`.
- **Tables** (store/schema.py via `create_all`, no ALTER): `delivery_trades` (series/instrument key, strategy,
  timeframe, signal_date, signal_close, qty, status PENDING_ENTRY|OPEN|EXIT_PENDING|CLOSED|MISSED, entry_ts/session/
  paise/late, exit_signal_date/reason/ts/paise/late, sessions_held, charges, dp, net in paise, net_pct, told_at x2,
  plan JSON); `delivery_evaluations` (PK series_key+session_date, reading JSON). Times are stored in UTC.
- **Exit (D5)**: OPEN trades run `exit_reading` each session; the entry session counts as 1. A hit moves the trade to
  EXIT_PENDING with the reason and raises an alert. The fill follows the entry rule at × 0.9997, late if needed, never
  missed. CLOSED carries the charges.
- **Charges (R6)**: `leg_paise(CostModel(equity_delivery), price, q, side)` for both legs, plus
  `depository_per_sale_inr` (new optional `ProductCostCfg` field, default 0, 14.75 on equity_delivery). Prices round to
  the paisa, not the tick (Q3).
- **Quantity (D10)**: `rule.quantity` at the signal, with the owner-decision comment there, in `unit_qty` (stays 1)
  and in an invariant test. **Restart (D12)**: all state is in the two tables; active rows reload; identity is
  (stock, signal date) and the exit is a state of that row.
- **`enabled` (D9)**: `StrategySlotCfg` gains `enabled=True` and `product="intraday"`. `evaluate` skips paused slots
  (verdict "paused"); delivery slots are not built by the tracker. `DeskConfig.paused_slots()` is new. desk.yaml:
  `enabled: false` on every existing slot; a `pullback` slot (1d, delivery, rule.json params, `unit_inr: 10000`) in
  `defaults.stock_strategies`, first in `primary_strategies`.
- **Snapshot and API**: each stock dict gains `pullback` (close, high20, depth_pct, needed 8, sma200, above, nifty_ok,
  note). The snapshot gains `delivery` (pending entry and exit alerts; open trades with D7's last, unrealised Rs and %,
  "k of 30", SMA20 exit, stop). `/api/desk/signals` gains `delivery_alerts`.
- **Scoreboard**: `DeliveryLedger.by_slot()` gives StrategyTotals for Slot(pullback, 1d, STOCK) (MISSED = no_fill, R
  None), merged in `_paper_scoreboard`. `slot_scoreboard_view(..., paused=())` adds a `paused` flag per row.
- **Page (D11)**: Pullback block in `accRow`; Delivery block atop `#home-signals`; Archive rows; "paused" rows. No reorder.

## C. What could break, and the proof
- The poll hook: test_desk.py, test_desk_paper.py, test_desk_session_over.py, test_of5_alert_lifecycle.py.
- Pausing: test_desk.py:198. D9 edits to test_tracker_trend.py:262 (build with enabled=True) and test_primary_slots.py:
  12,49 (new expected slots; delivery slots checked against `PullbackParams`).
- The cost field: test_costs.py, test_invariants.py:186, paper/test_costs.py. The scoreboard: paper/test_scoreboard.py,
  test_of_qa_scoreboard_properties.py, test_paper_api.py. The tables: test_store.py, paper/test_reconcile.py.
- app.js: desk/*_ui.py, test_of_qa_ui_runtime.py. Untouched: test_history.py and `unit_qty`'s tests. New:
  `PaperBook.step` and `_reconcile` leave delivery rows alone. Then `scripts/gate.sh`.

## D. Tests (new)
- delivery/test_rule.py: R1 (each of a-e fails alone; index 200; NIFTY missing or short), R2 and R3 (each exit, on
  the entry session, the 30 count, the stop on the fill), R4, R5, R6 (paise and dp), R8 (hypothesis: a cut at t).
- delivery/test_parity.py (R7): the 19 fixtures give the 247 rows exactly; net % within 1e-6.
- delivery/test_data.py (D8): short history, a missing session, a partial day, a stale store plus live days.
- delivery/test_book.py: D3 (on time, late, missed, special session), D4, D5, D6, D12 (idempotent, compare-and-set).
- delivery/test_desk_delivery.py: D1 (catch-up), D2 (one notification, no expiry), D9, D7/D11, D4 (session end).
- delivery/test_end_to_end.py: 12 sessions of fake 1m bars (3 stocks plus NIFTY) produce an entry, a stop exit and an
  SMA20 exit. The desk and the book are rebuilt three times (alert pending, trade open, exit pending). Asserts no
  duplicates and the expected fills and nets.
- test_invariants.py (D10); node UI tests; `scripts/pullback_replay.py` (D13, read-only, output kept in docs/research/).

## E. Increments
1. Rule, parity, causality, quantity, net %, dp field (S/M). 2. `enabled`/`product`, desk.yaml, paused, D9 edits (S).
3. Delivery book: tables, state machine, late and missed fills, charges, ledger, restart (M/L).
4. Desk integration: loader and freshness, evaluation and catch-up, alerts, snapshot and API, top-up, end-to-end (L).
5. Page, the D13 replay and its output, the gate (M).

## F. Questions for the lead (recommendation in brackets)
1. Separate tables instead of `desk_signals` and `paper_trades`? [Yes, see B.]
2. Catch-up over several missed sessions: exits on all, an entry on the newest only, older ones MISSED? [Yes.]
3. Round fills to the paisa, not the tick, as the research did? [Yes.]
4. Depository charge location? [costs.yaml equity_delivery `depository_per_sale_inr: 14.75`, default 0.]
5. Should the top-up write 1m bars through `backfill`, making the desk the first automatic store writer? [Yes: 1m, 19
   series, Data API on.]
6. Home: a block at the top of the Alerts card and the Archive, no new section? [Yes.]
7. Does an open delivery trade block a re-enabled intraday slot on the same stock? [No: different products.]
8. Which exit reason is shown when several hold? [All of them, in R3's order: SMA20, time, stop.]

## G. Ambiguities and conflicts with the code
- "Close" is the last continuous minute (15:14 for F&O stocks since 2026-08-03, REVIEW D-1), not the auction close.
  Parity holds, but the alert's "last close" is about 0.3% off the quoted close.
- Special sessions are sessions in the fixtures but not in `TradingCalendar`. I take "next session" from the data (a
  Friday signal fills at a Muhurat Sunday open). A non-calendar day is complete only the next day.
- D3 says "one-minute bar", but the desk's Yahoo bars are 2-minute. The first bar's open is the same.
- R7's 1e-6 is a float per-share check. The book (paise, q shares) differs by about 1e-4%, so D13 compares dates
  exactly and prices to one paisa.
- D8 asks for 221 bars; the rule needs 201. Missed trades can make the desk's state diverge from the reference's.
- "Not running at the open" is judged by this process's start time. Index trackers keep polling (NIFTY feeds R1(b)).
