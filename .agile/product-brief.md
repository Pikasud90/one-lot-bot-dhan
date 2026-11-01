# Product brief — One Lot Desk (production release 1.0)

**Problem:** A solo retail trader (home setup, Mac, Dhan account) wants timely, trustworthy one-unit trade alerts on 5 indices
(traded as options, buy-only) and 10 stocks (buy/sell, 1 share), and must learn *which strategies are actually reliable*
before any money is risked. The prototype raises alerts and can audit them after the fact, but: alerts are only "tested" if the
user clicks Taken; nothing reaches the phone; the web app has no authentication on reads; there is no supervision, CI, migration or
backup story; and the strategy matrix has never been run on real history.

**Users / personas:** (1) the owner-operator (trader and quant researcher); (2) a supervisor who reviews the system's evidence
(read-only). Single user, single machine; phone is a second screen, not a second operator.

**Outcome and success metric** (proposed targets — to confirm at Checkpoint 1):
- Paper-trade coverage: 100% of alerts (incl. risk-blocked and expired) open a paper trade within one poll cycle; 0 alerts without a paper outcome.
- Telegram delivery: every alert on the phone with p95 ≤ 10 s from alert creation; 0 silently dropped (outbox + retry; failures visible in-app).
- Evidence: every strategy shows live paper statistics with n, win rate, expectancy (R), profit factor net of costs; a strategy is called
  "live-grade" only after n ≥ 30 paper trades on live (non-delayed) data with PF ≥ 1.2 — thresholds pre-registered before results are seen.
- Safety: 0 order-placement code paths (invariant test stays green); 0 secrets in code/logs; reads authenticated whenever the app is reachable off-loopback.
- Quality gate: ruff, mypy (configured), full tests, domain-lint no ERROR, coverage ≥ 80% on new core modules, CI on every push.
- By when: Sprint 1 (walking skeleton) → Sprint 4 (1.0 release candidate).

**Scope of the first production release:**
1. Paper trading engine — every alert auto-opens a paper trade, managed by the same exit rules, costs and fill realism, with its own ledger and analytics.
2. Telegram bot — compact alert on the phone for every alert (action, instrument, unit, price, SL/TGT, strategy, confidence, TTL, data source, IST time) + lifecycle messages; commands and inline Taken/Skip as a follow-up story.
3. Production hardening — authenticated reads, supervised service with auto-restart, CI + pinned dependencies, migrations + backups, observability, graceful recovery.
4. Signal quality — alert confluence/dedup, confidence calibrated on paper outcomes, a pre-registered promotion gate (live-grade vs experimental).
5. UI/UX ergonomics — mobile-responsive/PWA, keyboard shortcuts, accessibility pass, a Paper book view, simpler information hierarchy; frontend split into modules with smoke tests.
6. Research at scale — resumable 5-year Dhan backfill, the preset matrix sweep run end to end with pre-registered gates; ranked "missing Data API features" spike.

**Non-goals:** any real order, position, or fund movement (placing, modifying, cancelling) — permanent, not "for now"; auto-trading; multi-user accounts;
cloud hosting (this release runs on the owner's Mac); options *selling* (indices are buy-only); position sizing (one unit always); investment advice.

**Inputs, outputs, integrations:** Dhan Trading API (free reads) and Data API (1-minute bars, WebSocket ticks, option chain, scrip master) over HTTPS/WSS;
Yahoo as the keyless fallback (delayed ~15 min — alerts built on it are labelled and excluded from "live-grade" statistics); Telegram Bot API (outbound HTTPS only);
SQLite (WAL); local web app (FastAPI + vanilla JS, vendored charts, works offline). Outputs: alerts (web, OS toast, Telegram), paper ledger, journal, backtest runs.

**Constraints:** Python 3.12, FastAPI, SQLite, no build step for the UI; macOS first (Windows scripts kept working); Dhan limits (quote 1 req/s, data 5 req/s, option chain 1/3 s, 100k data req/day);
Telegram limits (1 msg/s per chat, 30/s global; Bot API HTTPS); token expiry 24 h (TOTP self-renewal exists); free-tier friendly; zero cloud cost.

**Domain notes:** *Finance* — NSE/BSE, IST, holiday calendar verified for 2026 (2027 pending the December circular); lot sizes from the scrip master at runtime; STT/charges in a versioned
cost table (**still unverified against a contract note** — Sprint 3 story); option premiums in backtests are Black-76 synthetic until the Dhan expired-options endpoint is wired; stocks may be carried
(swing), index options are intraday. Paper P&L is kept in integer paise (Decimal at the boundary); analytics stay float64. *AI* — no ML in the signal path today; the "confidence" score is a heuristic blend,
to be calibrated on paper outcomes (never on the data it was tuned on). Backtest numbers must be out-of-sample with costs and carry their limitations.

**Risks and unknowns (→ spikes):**
- SP-001 fnolab.in reference review — needs the owner's Brave session via Claude in Chrome (not connected); blocked until then.
- SP-002 Which Data API features add edge (OI build-up, max pain, PCR, IV skew, depth, expired-options rolling data)?
- Paper realism: Yahoo-delayed alerts have stale prices; paper fills on them are not evidence → must be labelled and excluded.
- Sample size: a 10-stock/5-index watchlist yields few alerts per day per strategy; reaching n ≥ 30 per strategy may take weeks — the promotion gate must not pretend otherwise.
- Overnight (swing) paper positions need a gap/corporate-action policy.
- Telegram token leakage risk (token in URL path) → log redaction must cover URLs and exceptions.
- 91 broad `except Exception` blocks in the prototype can hide failures → triage in the end-to-end review.

**Assumptions to confirm:** see the five questions at Checkpoint 1 (Telegram credentials and buttons; paper-fill semantics; alert volume policy; deployment and LAN auth; promotion gate and fnolab access).

## Epics
- E1: Platform hardening and security
- E2: Paper trading engine and analytics
- E3: Telegram alerting
- E4: Signal quality and promotion gate
- E5: UI/UX ergonomics
- E6: Research at scale (data, matrix, gates)
- E7: Data API feature expansion
- E8: Documentation and release
