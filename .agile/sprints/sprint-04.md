# Sprint 04: the owner's requests of 24 and 25 November (alpha, watchdog, wide list)

## Read this first to resume (state at the end of 25 Nov 2026, late evening)
- The verified version is the head of `sprint-04-alpha-and-watchdog`, and GitHub `main` points
  to it once the full gate has passed on it (see the last line of this section). It holds
  everything listed under "Shipped" below.
- The owner's desk still RUNS the earlier verified commit `fe14a03` until it is restarted on
  the new one. After a restart, reload the browser tab (from the new version on, an open tab
  shows a bar asking for the reload).
- How work is done here: a git worktree beside the live folder for every change, never an edit
  in the live folder while the desk runs; the full gate (`bash scripts/gate.sh`, about 20 to
  26 minutes on a quiet machine: run it when no other test run shares the machine) in a gate
  worktree; the secret scan as its own command before every push
  (`scripts/secret_scan.py --env-root <the live folder>`, exit 0); `main` only fast-forwards
  to a commit that passed the gate; the app never places an order.
- To put a new verified commit on the desk: `bash scripts/stop_mac.sh`, then
  `git merge --ff-only <commit>` in the live folder, then
  `nohup .venv/bin/python -m onelot run --port 8787 > logs/app.out 2>&1 &`.
- The board (`.agile/board.md`, made by the board tool) lists every story: 50 in the backlog,
  US-106 ready, the rest done. Stories for future reviews: US-106 to US-126 and the studies
  SP-003 to SP-009.

## Shipped on 25 Nov (the first six rows are in `fe14a03`, all rows in the branch head)
| Story | What it does | Proof |
|---|---|---|
| US-102 | The wide list's round waits for Dhan's daily bars; a look is not a try | live check on a private store |
| US-104 | An option alert names only a contract that trades: last price, bid and ask, open interest, spread at most 10% | two reviews, 35 mutations killed, 6,000 healthy chains give the old pick |
| US-105 | Paper trades on contracts with no open interest are tagged and left out of every score; records kept | three reviews; on a copy of the real records exactly five trades are marked, six other slots unchanged |
| 14px type | No text under 14px on any tab; phone layouts fit | measured on a private desk: nine tabs, three widths, Day and Night |
| US-098 | Long volatility settled on real puts of NIFTY and BANKNIFTY: it loses 2.61% a pair a day | independent re-computation: 2.65% |
| Telegram | Set up and in use; a blocked network is named in plain words | the desk's own queue sent the token reminders |
| US-103 | A wide-list stock that stops trading frees its place; a release for review books no money | three reviews, a fix round, two more reviews; 47 tests |
| US-120 | A cheat sheet in every tab with how far the tab can be trusted | about 70 statements checked against the code; nine tabs measured on a private desk |
| US-122 | The defects of the validity check fixed one by one (Volume texts, News times, the reload bar, the price source of a paper trade, the confidence formula, the guides) | two reviews; a test an issue |

Data changed in the live folder, both approved by the owner and done after market hours:
- `data/onelot.db`: the daily history of the 204 delivery-only stocks (757,835 bars).
- `data/expired_options.db`: 21,279 day blocks of real NIFTY and BANKNIFTY options added, none
  replaced (the 23,104 earlier blocks are unchanged). The file as it was before is kept at
  `data/backups/expired_options_before_puts_2026-11-25.db`; delete it when no longer wanted.
- The desk's start on `fe14a03` made the new table `paper_void` and marked five paper trades
  (ids 81, 151, 153, 155, 156). The trades themselves are untouched.

## Open, in the order to take it
1. US-106, the wide list reads each day the same evening from Dhan's quote answer (approved
   by the owner, status ready). Its first step is a data check that needs no code change:
   compare `data/research/quote_vs_daily_2026-11-25/quote_1938.json` (in the live folder) with
   Dhan's official daily bars of 25 Nov. Until it ships the wide list reads a day only once
   Dhan has added it (the next day, before 09:00), and not at all if that is after 09:00.
2. US-121, the desk stocks' day close is the exchange's closing price. Measured on 25 Nov:
   the stored close of a stock is the 15:14 price, up to 0.60% off the exchange's close.
   Build it after US-106 (same quote answer).
3. The hour at which Dhan adds a day to its daily history: read `wide_rounds` of the live
   store for the session 2026-11-25 (`finished_at`) and write it into
   `docs/04_OPERATIONS.md` under "Waiting for the broker's bars".
4. US-117, the 2027 holiday list (the file holds 4 dates for 2027): before 1 January 2027.
5. The rest of the board by priority; the studies SP-003 to SP-009 follow
   `.agile/stories/RESEARCH-RULES.md`.
6. Housekeeping: the worktrees `wt-fix-*`, `wt-cheats`, `wt-backlog` and the side sessions'
   worktrees are merged and can be removed; `data/research/real_puts_2026-11-25/
   expired_options_copy.db` and `data/backups/expired_options_before_puts_2026-11-25.db` can
   be deleted when no longer wanted.

## Facts found on 25 Nov that change how things work
- Dhan's daily history ends at the previous trading day (its support page says so; the day's
  bar was not there at 15:42, 18:11, 19:22 or 21:12). Dhan's quote answer holds the day's
  open, high, low, closing price and volume after the close, for every stock in one request.
- The institute's network blocks api.telegram.org and at times GitHub: on it no phone message
  leaves the desk (they wait in the queue) and a push can time out.
- Five of the 16 index alerts priced from a live chain had named a contract with no open
  interest (two before 23 Nov). The micro-trend rule's paper score is now 2 counted trades:
  far too few to say anything.
- No study to date has shown a tradable edge: the pullback rule as a ten-trade book did not
  beat NIFTY held; long volatility loses on real prices; the option setups stay on WATCH.
