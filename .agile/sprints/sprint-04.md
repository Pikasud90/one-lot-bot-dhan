# Sprint 04: the owner's requests of 24 and 25 November (alpha, watchdog, wide list)

## Read this first to resume (state at the end of 25 Nov 2026)
- GitHub `main` is `fe14a03`: the last VERIFIED version (full gate 9,064 passed, 1 skipped;
  secret scan exit 0). The owner's desk runs it since 21:00 on 25 Nov, on port 8787.
- The sprint branch `sprint-04-alpha-and-watchdog` is `fe14a03` plus documents only (this note
  and the story US-106).
- How work is done here: a git worktree beside the live folder for every change, never an edit
  in the live folder while the desk runs; the full gate (`bash scripts/gate.sh`, about 26
  minutes) in the gate worktree; the secret scan as its own command before every push
  (`scripts/secret_scan.py --env-root <the live folder>`, exit 0); `main` only fast-forwards
  to a commit that passed the gate; the app never places an order.
- To put a new verified commit on the desk: `bash scripts/stop_mac.sh`, then
  `git merge --ff-only <commit>` in the live folder, then
  `nohup .venv/bin/python -m onelot run --port 8787 > logs/app.out 2>&1 &`, then reload the
  browser tab (an open tab keeps the old page script).

## Shipped on 25 Nov (all in `fe14a03`)
| Story | What it does | Proof |
|---|---|---|
| US-102 | The wide list's round waits for Dhan's daily bars; a look is not a try | live check on a private store |
| US-104 | An option alert names only a contract that trades: last price, bid and ask, open interest, spread at most 10% | two reviews, 35 mutations killed, 6,000 healthy chains give the old pick |
| US-105 | Paper trades on contracts with no open interest are tagged and left out of every score; records kept | three reviews; on a copy of the real records exactly five trades are marked, six other slots unchanged |
| 14px type | No text under 14px on any tab; phone layouts fit | measured on a private desk: nine tabs, three widths, Day and Night |
| US-098 | Long volatility settled on real puts of NIFTY and BANKNIFTY: it loses 2.61% a pair a day | independent re-computation: 2.65% |
| Telegram | Set up and in use; a blocked network is named in plain words | the desk's own queue sent the token reminders |

Data changed in the live folder, both approved by the owner and done after market hours:
- `data/onelot.db`: the daily history of the 204 delivery-only stocks (757,835 bars).
- `data/expired_options.db`: 21,279 day blocks of real NIFTY and BANKNIFTY options added, none
  replaced (the 23,104 earlier blocks are unchanged). The file as it was before is kept at
  `data/backups/expired_options_before_puts_2026-11-25.db`; delete it when no longer wanted.
- The desk's start on `fe14a03` made the new table `paper_void` and marked five paper trades
  (ids 81, 151, 153, 155, 156). The trades themselves are untouched.

## Open, in the order to take it
1. US-103, a stock that stops trading frees its place. Branch `story/US-103-wide-no-data`,
   head `d416f23`, rebased on `fe14a03`, local only (not pushed). Three reviews asked for
   changes; the builder reports every point done, 1,435 focused tests passed and 42 broken
   variants of the new code each caught by a test (the story file on that branch has the
   list under "State on 25 Nov, 21:40"). NOT yet verified by the lead and NOT merged. To
   do: an independent check of the fix round (commits `b329f97` and `d8cb12c`), the money
   and rule points first: a release for review books no money; a release on a later try of
   the evening gives the place to that session's signal; an evening counts toward the
   confirmation only once the missing sessions reach the threshold (the release then falls
   on the round of the 12th session); a stock the try never asked is not counted. Then merge
   into the sprint branch (it sits on `fe14a03`, so no conflict is expected beyond this note
   and the US-106 story), the full gate, the push.
2. US-106, the wide list reads each day the same evening from Dhan's quote answer. The owner
   approved it on 25 Nov. Contract: `.agile/stories/US-106.md`. Its first step is a data
   check that needs no code change. Build it after US-103 (both change the same file), with
   the usual pipeline: one developer, three independent reviews, a fix round, the full gate.
   Until it ships the wide list reads a day only once Dhan has added it (the next day, before
   09:00), and not at all if Dhan adds it later than 09:00.
3. The hour at which Dhan adds a day to its daily history: read `wide_rounds` of the live
   store for the session 2026-11-25 (`finished_at`) and write it into
   `docs/04_OPERATIONS.md` under "Waiting for the broker's bars".
4. Real puts of FINNIFTY and SENSEX and the weekly contracts: `scripts/real_puts_fetch.py`
   (resumable, after 15:40 only, one request a second), then `scripts/merge_option_bars.py`
   with a dry run first.
5. The owner's feedback after testing `fe14a03`.
6. Housekeeping: the worktrees of merged stories (US-098, US-104, US-105 and the 14px
   branch) can be removed; `data/research/real_puts_2026-11-25/expired_options_copy.db` is a
   proof copy and can be deleted.

## Facts found on 25 Nov that change how things work
- Dhan's daily history ends at the previous trading day (its support page says so; the day's
  bar was not there at 15:42, 18:11, 19:22 or 20:52). Dhan's quote answer holds the day's
  open, high, low, closing price and volume after the close, for every stock in one request.
- The institute's network blocks api.telegram.org and at times GitHub: on it no phone message
  leaves the desk (they wait in the queue) and a push can time out.
- Five of the 16 index alerts priced from a live chain had named a contract with no open
  interest (two before 23 Nov). The micro-trend rule's paper score is now 2 counted trades:
  far too few to say anything.
- No study to date has shown a tradable edge: the pullback rule as a ten-trade book did not
  beat NIFTY held; long volatility loses on real prices; the option setups stay on WATCH.
