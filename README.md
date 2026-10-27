# One Lot Desk — signal-only Dhan trade desk

One process, one web app, one rule: **one unit per trade**. One lot of a Bank Nifty option,
one lot of a future, one share of a stock, one lot of a currency future. The desk finds the
setup, prices it net of every Indian charge, tells you the instrument, the entry, the stop
and the target, and then keeps telling you *hold / tighten / exit* until you are flat.
**You** place every order on your Dhan terminal. There is no order-placement code anywhere
in this repository and a test enforces that.

It merges two earlier builds — the Bank Nifty live option engine (`bnta`) and the Fyers
strategy/backtest framework (`tradebot`) — into a single Dhan-only project. The old folders
are reference only.

> Decision support, not advice. Every rupee figure is an estimate until reconciled with a
> real contract note (see `docs/05_COST_RECONCILIATION.md`). See [DISCLAIMER.md](DISCLAIMER.md).

---

## What you get

| Tab | What it does | Needs |
|---|---|---|
| **Home** | Your Dhan account: funds, holdings, positions, 30-day ledger and trade book, token/Data-API validity. **Cached** — fetched only when you press Refresh or after you confirm a fill/exit. | free-tier token |
| **Live Desk** | The Bank Nifty option engine: closed-bar signals with net-of-cost entry/stop/target/breakeven, a 75 s decision window, the Guardian (ratcheting trail, time stop, square-off), six risk kill-switches, "why no signal" histogram. | Data API |
| **Backtest Lab** | Pick a category → instrument → strategy → timeframe → parameters, run on the local corpus. Equity, drawdown, candles with trade markers, monthly bars, breakdowns, trade list, the **30 % rule** panel. Optimizer with plateau-robust selection and rolling **walk-forward**. | nothing (Yahoo corpus) |
| **Data** | The historical corpus: coverage per series/resolution, backfill jobs (Yahoo now, Dhan 1-minute × 5 years once the Data API is on), quality report. | nothing / Data API |
| **News & Sentiment** | Sentiment board and news feed with `POST /api/sentiment` and `POST /api/news` for your own scorer (built separately). | nothing |
| **Settings** | Paste Dhan credentials (written to `.env`, mode 0600, never echoed), **Test entitlements** (free tier vs Data API, per endpoint), postback guidance, read-only config. | — |

Everything runs in **one Python process** (`python -m onelot run`) on macOS and Windows,
with SQLite in WAL mode. No Docker, no external services.

The web app opens in a bright **Day** theme sized for reading at arm's length; the **☾ Night**
button in the header switches to the dark desk and the choice is remembered per browser.

---

## Quick start

```bash
# macOS / Linux
scripts/start_mac.sh --mock          # synthetic market, no credentials; opens http://127.0.0.1:8787/
scripts/start_mac.sh                 # real Dhan feed once credentials + Data API exist
scripts/start_mac.sh --lan           # also reachable from other devices on your network
```

```bat
:: Windows
scripts\start_win.bat --mock
scripts\start_win.bat --lan
```

First run creates `.venv` and installs everything. Then:

1. **Settings → Dhan credentials**: paste your client ID and the free-tier access token, Save,
   then **Test entitlements**. The report shows two tiers: *trading* (profile, funds, holdings,
   positions, ledger, trade book — must all pass; verified live on a free-tier account) and
   *data* (expiry list, option chain, historical bars — `NO_DATA_API` until the plan is bought).
2. **Home** now shows your ledger and funds. Press Refresh whenever you want; it never polls.
3. **Data**: the Yahoo corpus is already loaded on this machine (406 k bars: 26 series × 2m/5m/15m/60m/1d).
   Run a backfill any time to top it up.
4. **Backtest Lab**: `OPTIONS → NIFTY BANK options → EMA Cross → 12m → Run backtest`.
   Then *Walk-forward* on anything that looks good. Only out-of-sample numbers count.
5. Once the Data API is active: `python -m onelot backfill --provider dhan` (five years of
   1-minute bars), re-run the research gate, and promote a PASS strategy to the live engine
   with `strategy.live.source: lab` in `config/config.yaml`.

Command line equivalents:

```bash
.venv/bin/python -m onelot preflight                       # which Dhan endpoints this token reaches
.venv/bin/python -m onelot backfill [--provider yahoo|dhan] [--series BANKNIFTY RELIANCE]
.venv/bin/python -m onelot backtest --strategy ema_cross --instrument BANKNIFTY_OPT --timeframe 12m
.venv/bin/python -m onelot research --instruments BANKNIFTY_OPT NIFTY_FUT --timeframes 5m,15m
.venv/bin/python -m onelot quality --resolution 5m
.venv/bin/python -m onelot.replay --sessions 60            # live-engine replay on a synthetic tape
.venv/bin/python -m pytest -q                              # 500+ tests
```

---

## Dhan: free tier, Data API, two tokens, postback

* **Free Trading API** (any token): `/profile`, `/fundlimit`, `/holdings`, `/positions`,
  `/ledger`, `/trades`. The Home tab and the entitlement test use exactly these. The desk
  uses *none* of the order endpoints.
* **Data API** (₹499 + GST per month, bought on the **account**): live WebSocket feed, market
  quotes, option chain, historical daily and intraday bars (1/5/15/25/60-minute, five years).
  The Live Desk and the Dhan corpus provider need it. The free-after-N-trades offer is
  discontinued per Dhan's FAQ.
* **Tokens**: web-generated tokens are valid 24 hours; regenerate daily (or use the TOTP
  endpoint — see `docs/13_DHAN_SETUP.md`). Because the entitlement is on the account, either of
  your two tokens works for everything once the plan is active. `.env` has two slots so the
  paid one can be kept separate: `DHAN_ACCESS_TOKEN` and optional `DHAN_DATA_ACCESS_TOKEN`.
* **Postback URL**: optional; it only receives **order updates**. This desk never places orders,
  so leave it blank — or point it at `http://<host>:8787/api/dhan/postback`, which records what
  arrives and acts on nothing.

---

## The Backtest Lab, honestly

* **One unit, no sizing.** Rupee P&L per trade is `(exit − entry) × unit − charges`. Metrics are
  in rupees first (expectancy ₹, max drawdown ₹, Monte-Carlo drawdown ₹); percentages use a
  `reference_capital_inr` that affects nothing else.
* **Signals on the underlying, P&L in the instrument.** Every strategy reads the index / stock /
  spot-FX series. Futures and currency use that price directly (basis ignored, documented).
  **Options are priced with Black-76** off the underlying path, IV = India VIX × 1.15, strike
  chosen by target delta 0.65, nearest monthly expiry, half-spread + tick slippage on the
  premium. This is a *model*, flagged on every result as `black76_synthetic`, until Dhan's
  expired-options endpoint replaces it with real premiums.
* **Full charges.** `config/costs.yaml` has a table per product (index/stock options, index/stock
  futures, equity intraday/delivery, currency futures/options): brokerage, STT, exchange, SEBI,
  IPFT, stamp, GST. Still `verified_against_contract_note: false` — reconcile it.
* **Closed bars only**, fills at the next open ± slippage, same-bar stop/target resolved on the
  finer (2-minute) path when stored, square-off at the instrument's session end (currency
  16:45, equity 15:15).
* **The 30 % rule panel.** Your thesis: 30 % hit rate pays if winners are ~3× losers. Every
  result reports the achieved payoff, the break-even win rate it implies, and the profit
  factor the *same exits* would produce at exactly 30 % wins. If that number is below 1, the
  stop/target geometry does not yet support the thesis regardless of signal quality.
* **Research gate** (`python -m onelot research`): rolling walk-forward with plateau-robust
  parameter selection, then cost ×2, slippage ×2, parameter jitter and Monte Carlo. Verdict
  PASS / WATCH / FAIL from out-of-sample numbers only. Report in `docs/research/RESEARCH_REPORT.md`.

### Strategies

| Name | Idea | Built for |
|---|---|---|
| `ema_cross` | Your thesis #1: fast/slow EMA cross in a trending market (ADX, slow-EMA slope), RSI and relative volume agreeing, one entry per cross, structural stop. Default 12m. | options, futures, stocks, FX |
| `supertrend_vol` | Your thesis #2: Supertrend with period/multiplier tuned by the optimizer, RSI + relative volume + ADX confirmation, line as the stop/trail. | options, futures, stocks, FX |
| `supertrend_adx` | Supertrend + ADX/DI + MACD + VWAP no-chase (from the Fyers build). | futures, stocks |
| `orb` | Opening-range breakout with VWAP, volume, gap and range-width filters. | futures, options, stocks |
| `vwap_pullback` | Trend-day pullback to a rising session VWAP, reclaim trigger. | stocks, futures |
| `squeeze` | Bollinger-inside-Keltner release + Donchian breakout with momentum. | stocks, futures, FX |
| `donchian_breakout` | Classic N/M channel trend following with an ADX floor. | futures, FX, stocks |
| `mean_reversion` | RSI(2) dips inside the intraday trend; small, frequent wins. | stocks, futures |
| `intraday_momentum` | First half-hour predicts the last half-hour (Gao–Han–Li–Zhou 2018; international evidence Li 2020). | index futures / options |
| `gap_fade` | Short stocks that gap far above NIFTY's own gap; the one Fyers-era finding that held on a 2022-26 holdout (now WATCH). | stocks |
| `benchmark_random` | Control: random entries with the same exits. Real strategies must beat it out of sample. | research only |

Every parameter is tunable in the Lab and searched by the optimizer; the indicators each
strategy uses are listed next to the form. Sources and what the evidence actually supports are
in `docs/12_STRATEGY_RESEARCH.md`.

---

## The live engine (Bank Nifty options)

Unchanged in substance from the previous build, with two defects fixed in this merge:

* **Crash recovery now really recovers**: open positions restored from the database are
  re-subscribed on the feed before the Guardian starts (previously the Guardian "resumed"
  on a contract that was never subscribed and silently did nothing).
* **A dead option quote is no longer silent**: the Guardian pulls a REST quote, marks the
  position *degraded* after `quote_stale_after_sec`, warns, and escalates to CRITICAL every
  minute while the position stays unmanaged.

Direction can come from the built-in gate stack (`strategy.live.source: gates`) or from any
Lab strategy evaluated on the same closed index bars (`source: lab`). Health, session,
contract selection (0.60–0.70 |Δ|, liquidity), net R/R, cooldown and the risk governor gate
it either way; the Lab strategy only ever supplies the side and a structural stop.

---

## Configuration

| File | What |
|---|---|
| `config/config.yaml` | engine, session, risk (absolute rupees), live source, backtest defaults, API bind |
| `config/costs.yaml` | per-product statutory charges |
| `config/universe.yaml` | series (indices, stocks, FX) → one-unit instruments; lot-size fallbacks |
| `config/nse_holidays.yaml` | holiday calendar (fixed-date only until you verify it) |
| `.env` | credentials (see `.env.example`) |

---

## Project structure

```
src/onelot/
  __main__.py            CLI: run | backfill | backtest | research | quality | preflight | cost
  engine.py              the live process (feed, chain, alpha, guardian, risk, account, lab jobs)
  config.py  settings.py costs.py  clock.py  ratelimit.py  lock.py  logging_setup.py
  api/server.py          FastAPI: live desk, account, lab, data, news/sentiment, settings, postback
  ui/                    index.html · app.js · styles.css (no build step)
  broker/                base.py · dhan.py (read-only; free-tier reads + data) · account.py · mock.py
  data/                  universe.py · store.py · resample.py · backfill.py · providers/{yahoo,dhan}.py
  indicators/core.py     vectorised, TA-Lib-validated indicators
  strategies/            base.py · registry.py · one file per strategy
  lab/                   simulator.py · execution.py · runner.py · metrics.py · optimizer.py · research.py · jobs.py
  strategy/              live gate stack: alpha.py · gates.py · selector.py · sizer.py · lab_source.py
  market/  trade/  risk/  analytics/  notify/  store/  replay/
tests/                   engine tests + tests/lab/ + account/Dhan-reads + lab-source tests
docs/                    00–08 live-engine design docs · 09–13 unified desk docs · research/
```

---

## Tests

```bash
.venv/bin/python -m pytest -q            # all (slow tests excluded with -m "not slow")
.venv/bin/python -m ruff check src tests
```

Invariants enforced by tests: no order-placement identifiers anywhere in `src/`; secrets never
reach logs; closed bars only (signals on truncated history equal signals on full history);
the Guardian compares regime to the correct option side; both bar-close paths fire; every
strategy produces stops on the right side of price; volume-free series (indices, FX) still
produce setups; free-tier Dhan reads are GETs that map DH-901/DH-902/429 to typed errors; the
account cache never hits the broker unless asked.

---

## Known limitations (read before trusting a number)

* Costs and the holiday calendar are **unverified** (`verified: false` flags warn at startup).
* Option backtests are **synthetic** (Black-76 + VIX) until Dhan expired-options history is wired.
* The Yahoo corpus is short for intraday work (≈ 60 sessions at 2m/5m/15m). Every intraday
  research verdict is a preliminary screen until the Dhan 1-minute history (5 years) replaces it.
* The Dhan adapter's data endpoints have not yet been exercised against a live Data-API
  account from this machine; `python -m onelot preflight` is the gate.
* Futures backtests ignore basis; currency backtests use spot FX as the proxy for the future.

More: `docs/07_KNOWN_LIMITATIONS.md`, `docs/11_BACKTEST_LAB.md`.
