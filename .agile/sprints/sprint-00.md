# Sprint 00 — Green, reproducible gate and a complete repository

Started: 2026-11-01T13:57+05:30
Committed: US-000 (3) — 3 points

## Plan
| Story | Protocol | First driver | Branch | Notes |
|---|---|---|---|---|
| US-000 Quality gate baseline is green: format, pins, mypy ratchet, repo hygiene | driver-navigator | dev-beta | story/US-000-quality-gate-baseline-is-green-format-pi | |

## Stand-up log
- 2026-11-01 13:50 repo hygiene: src/onelot/data was untracked (unanchored ignore); fixed and committed by the Manager (400b683), fresh clone verified; next: US-000 (pins, format, ratchet, gate.sh).

## Sprint review
- Done: US-000 (3 pts). scripts/gate.sh green: 816 passed, 1 skipped; skill gate PASS; mypy ratchet at 198 errors (mypy 2.4.1); one behaviour-neutral reformat commit. Reviewer APPROVE in 2 rounds, Tester PASS in 1 cycle.
- Demo: `scripts/gate.sh` from any directory.
- Slipped: nothing. Deferred: --python-version vs requires-python, CI baseline check (US-008), dev lockfile, test speed-ups.

## Retrospective
- Keep: Tester mode A in parallel with the driver (found 2 real ratchet bugs before review); navigator READY-CHECK with mutation probes (found the flags-widening hole); Reviewer re-running exploits on scratch snapshots.
- Problem: the driver turn ran about 40 min because one turn held six steps; Manager left .agile edits uncommitted while agents worked, which showed up as noise in their status checks; zsh treats `echo =====` as a command lookup.
- Try: smaller driver turns (2-3 steps); commit .agile before launching agents; agents run targeted tests in the inner loop and the full gate only before PROPOSE_READY.
