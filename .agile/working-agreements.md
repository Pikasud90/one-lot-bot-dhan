# Working agreements

Updated at each retrospective. The Manager copies the relevant lines into delegation briefs.

## Definition of Done (team default)
- Every acceptance criterion is covered by passing automated tests (unit + acceptance).
- quality_gate.py verdict PASS (coverage threshold below).
- domain_lint.py reports no ERROR; every WARN is triaged by the Reviewer.
- Reviewer APPROVE and Tester PASS.
- Docs updated: README / run instructions, docstrings, CHANGELOG; an ADR for architectural decisions.
- No secrets in code; dependencies pinned; paper/sandbox mode is the default for anything that trades or moves money.

## Conventions
- Language and stack: Python 3.12 (src/onelot), FastAPI, SQLAlchemy/SQLite (WAL), vanilla JS UI with vendored Lightweight Charts (no build step). Venv: `.venv/bin/python`.
- Run tests: `.venv/bin/python -m pytest -q -m "not slow and not live" -p no:warnings`; lint: `.venv/bin/python -m ruff check src tests`.
- HARD SAFETY INVARIANT (tests/test_invariants.py scans every identifier in src/): never use `place_order`, `modify_order`, `cancel_order`, `exit_position`, `square_off`, `convert_position`, `kill_switch_order` (or any alias in that file) as names, and never reference an `/orders` endpoint. Use `close_paper_trade`, `session_end_close`, etc.
- Money: the paper ledger is integer paise (Decimal at boundaries); never float for ledger sums. Analytics may be float64.
- Time: timezone-aware IST (`onelot.clock.IST`, `now_ist()`); never naive datetimes in new code.
- Secrets: only via env/.env (mode 0600) and `register_secrets`; never in code, tests, fixtures, logs or exception text. Telegram token must be redacted inside URLs.
- Network in tests: forbidden — use `httpx.MockTransport` / fakes. No test may touch the real Dhan or Telegram.
- Existing UI conventions: Day theme default, large type, mascots Ichi/Moo, all colours via CSS variables; app.js is a single IIFE today (US-018 splits it).
- Branches: story/US-xxx-short-name; conventional commits; nothing is pushed or merged without the user's go-ahead.
- Test layout: existing flat `tests/` + `tests/lab`, `tests/desk` are developer unit tests; the Tester adds `tests/acceptance/`, `tests/integration/`, `tests/property/` (new dirs).
- Coverage threshold: 80% on NEW core modules (`onelot.paper`, `onelot.notify.telegram`); legacy modules are measured but not gated until their story.
- Coverage threshold: 80%

## Retrospective log
