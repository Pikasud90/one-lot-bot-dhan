# Sprint 01 — Walking skeleton: every alert opens and closes a paper trade and reaches the phone through a durable outbox (fake transport in tests), safely

Started: 2026-11-01T13:57+05:30
Committed: US-028 (1), US-001 (5), US-002 (5), US-003 (5), US-004 (5) — 21 points

## Plan
| Story | Protocol | First driver | Branch | Notes |
|---|---|---|---|---|
| US-028 Fix: alerts served by the Yahoo fallback are labelled live | ping-pong-tdd | dev-beta | story/US-028-fix-alerts-served-by-the-yahoo-fallback | |
| US-001 Every alert opens a paper trade | ping-pong-tdd | dev-alpha | story/US-001-every-alert-opens-a-paper-trade | |
| US-002 Paper trades are managed to a close by the strategy exit rules | ping-pong-tdd | dev-beta | story/US-002-paper-trades-are-managed-to-a-close-by-t | |
| US-003 Telegram client and compact alert formatter, with redaction hardening | ping-pong-tdd | dev-alpha | story/US-003-telegram-client-and-compact-alert-format | |
| US-004 Alerts reach Telegram through a durable outbox | driver-navigator | dev-beta | story/US-004-alerts-reach-telegram-through-a-durable | |

## Stand-up log
- 2026-11-01 13:57 planning: done refinement (Tester + Dev Alpha + Dev Beta, read-only) and baseline gate; US-002 and US-004 re-scoped, US-023..US-028 split off; next: Checkpoint 1 approval, then Sprint 0 (US-000) before any Sprint 1 story; blockers: owner answers to the five questions.

## Sprint review
- Done (8 stories, 28 pts; Sprint 0 US-000 3 pts): US-028 delayed-feed label (1), US-001 every alert opens a paper trade (5), US-002 paper trades managed to a close (5), US-023 costs, net, ledger and marks (3, pulled in from Sprint 2), US-030 Paper panel and /api/paper (2, added), US-032 last-session view (2, added after the owner's first launch), US-003 Telegram formatter and client (5), US-004 durable Telegram outbox (5).
- Integration: branch sprint-01-integration merges all of them; full gate on it is the evidence for the release (see the stand-up log for the final count). Local only, nothing pushed.
- Demo: `.venv/bin/python -m onelot run --port 8787`, Live Desk tab, Paper trades card (Today / Last session / All); put TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env for phone alerts. On 2026-11-01 the book reconciled 68 Thu/Fri alerts: 67 filled and closed (STOP 29, SESSION_END 19, TIME_STOP 19), 1 NO_FILL gap through stop; Friday's 33 closes read -2,508.05 gross.
- Defects caught by review/test before the owner saw them: trail ratcheting on 1m bars instead of strategy bars (Lab parity), a TARGET close at a loss from a slipped entry, a stale EXIT burst and missed-note flood after an outage, the bot token in httpx's INFO log, a chat id registered as a secret, a stock short carried overnight on the desk, the Paper panel hiding Friday's closes on Monday, a Lab behaviour change smuggled into a paper fix, retry/backlog noise under DB locks.
- Slipped: nothing committed was dropped; the Paper panel UI for open-trade mark/unrealised is a follow-up (API ready).
- Decisions the owner may want to revisit: paper is canonical for option execution cost (Lab fixed in US-031); stock shorts are intraday only; CRITICAL repeats reach the phone once per 30 minutes; exit messages show the event time and say 'sent late'.

## Retrospective
- Keep: Tester mode A in parallel with the first pairing turn (every story's acceptance suite caught real bugs); read-only probes against the owner's live DB by the Reviewer (caught the Monday blocker); Lab-parity and differential oracles for money; recording every Manager decision in the story Notes so agents converge.
- Problem: Manager decisions made after mode A repeatedly pinned old behaviour in acceptance tests, costing extra Tester cycles; long driver turns (20-40 min); worktree .venv imports main's src for ad-hoc runs (PYTHONPATH needed); a perl substitution ($110) corrupted an ADR; story .agile files diverge across branches; my first launch hid the evidence (panel filtered to today).
- Try: decide edge semantics BEFORE mode A where possible (a short 'decision pass' after the refinement round); keep story branches short and merge to integration as each closes; use a Python editor or Edit tool for .agile text, never perl with capture groups; launch-check the owner's real data path (weekend/Monday) as an acceptance scenario for every UI story.
