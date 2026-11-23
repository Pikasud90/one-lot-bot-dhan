# One Lot Desk: signal-only Dhan trade desk

One process, one web app. The desk watches two rules and nothing else, and tells you what to
do; **you** place every order on your Dhan terminal. There is no order-placement code anywhere
in this repository and a test enforces that.

* **Micro-trend rule, on index options.** Four traded indices (NIFTY, BANKNIFTY, FINNIFTY,
  SENSEX), intraday, one lot of the monthly contract per trade. MIDCPNIFTY is followed for
  training only: it raises no alert and opens no paper trade.
* **Pullback rule, on stocks.** 18 stocks as delivery trades of about Rs 10,000, carried for
  days (about 7 winners in 10 in the backtest, not proven: it is a paper test).

Every alert is priced net of every Indian charge and opens a paper trade at the alert's own
prices. Nothing here has proven an edge yet; the figures are a forward test.

It merges two earlier builds, the Bank Nifty live option engine (`bnta`) and the Fyers
strategy/backtest framework (`tradebot`), into a single Dhan-only project. The old folders
are reference only.

> Decision support, not advice. Every rupee figure is an estimate until reconciled with a
> real contract note (see `docs/05_COST_RECONCILIATION.md`). See [DISCLAIMER.md](DISCLAIMER.md).

> **Current state (2026-11-23).** The rules above are the whole desk; every other slot is
> paused (`config/desk.yaml`, `docs/04_OPERATIONS.md`). Each entry alert gets exactly one exit
> alert, taken or not, and no risk governor blocks or hides an alert. The paper book prices
> option fills with the alerted contract's traded one-minute prices where it has them.

---

## What you get

| Tab | What it does | Needs |
|---|---|---|
| **Home** | **Today** strip (session, feed, token, the two rules, the next thing to do); the **market snapshot** button, which saves the last price of the indices and stocks to compare by hand; **alerts** (IST time, source and delay, strategy, confidence, time to live, Taken or Skipped) and each alert's outcome at its own levels; the paper test; then your Dhan account (funds, holdings, positions, ledger, trade book, cached, never polled). | free-tier token |
| **Live Desk** | The 4 traded indices (buy a call or a put, 1 lot) and MIDCPNIFTY for training only, and the 18 stocks (pullback, delivery). Each card shows its seven micro-trend readings, market context (VIX, NIFTY, BANKNIFTY, SENSEX, USD/INR, GIFT NIFTY slot) and the paper trade. A trade on an option is managed by one stop, target and trail rule: first stop 1.5 ATR, first target 3 risks, then the stop only moves up; you get one notice each time to move your own stop. Runs on Yahoo bars (delayed) until the Data API is active. `docs/14_LIVE_DESK.md` | nothing (Yahoo) / Data API |
| **Simulator** | Practice with dummy money: pick a strategy, an instrument and a start time (or follow the live feed), press Play, and take or skip each alert at the replay's own speed. Same stop, target and trail rule, full charges, real premiums where stored. It never places an order and never touches the paper test. `?simfake=1` shows demo data. | stored history |
| **Backtest Lab** | Any instrument, TradingView-style charts (vendored, offline) with trade markers; equity and drawdown; 30 % rule panel. **Walk-forward**, **optimizer**, **sweeps** (every strategy by instrument by timeframe, each saved) and a **leaderboard** over every saved run. Sentiment gate. The strategy library is research material; the desk trades only the two rules. `docs/15_LAB_SWEEPS_SENTIMENT.md` | nothing (Yahoo corpus) |
| **Data** | Corpus coverage, backfill jobs (Yahoo now, Dhan 1-minute x 5 years once the Data API is on), quality report, and an **explore** section: daily closes vs VIX, return distribution, intraday range profile, sessions per month, weekday seasonality, correlation heatmap. | nothing / Data API |
| **News & Sentiment** | **Market pulse** (breadth, index trend, VIX percentile, NIFTY PCR with the Data API) recorded every 15 min, score history chart, board, news feed, manual posts, **bulk import** of your scorer's history for backtests. | nothing |
| **Machine Learning Lab** | **Market states**: how each session is classified and how the states follow each other (counting only; a mock or new store says plainly it has too few sessions). Below it, statistical forecasts of the next candle's close with 68% and 95% bands and how often each held. The up figure is not a signal and the Lab raises no alert. | stored history |
| **Settings** | Paste Dhan credentials (written to `.env`, mode 0600, never echoed), **Test entitlements**, **Renew token** (TOTP self-renewal, no daily pasting), **Go live** (switch to the Dhan feed in place), desk watchlist, read-only config. | none |

Everything runs in **one Python process** (`python -m onelot run`) on macOS, Linux and Windows,
with SQLite in WAL mode. Docker is optional; there are no external services.

The web app opens in a bright **Day** theme sized for reading at arm's length; the Theme
select in the header offers four more (Night, Laxmi-Ganesha, Ramayana, Batman) and the choice
is remembered per browser. Keys 1 to 8 switch tabs, in the order of the tab bar. The
header clock is system time in IST (seconds and milliseconds); click it for the NSE holiday
calendar (2026 cross-checked; past holidays shaded, the next one highlighted).

---

## Quick start

Needs Python 3.10 or newer and git. Every command here was run on a fresh clone.

```bash
git clone https://github.com/Pikasud90/one-lot-bot-dhan.git onelot
cd onelot
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m onelot run --mock --port 8787     # synthetic market, no credentials
```

Wait about half a minute, then open http://127.0.0.1:8787/ . Ctrl-C stops it. On Windows use
`.venv\Scripts\python`. The launchers do the environment and install steps on first run:

```bash
scripts/start_mac.sh --mock          # macOS;  scripts/start_linux.sh on Linux
scripts\start_win.bat --mock         # Windows (or scripts\start_win.ps1)
scripts/start_mac.sh --lan           # also reachable from other devices on your network
scripts/stop_mac.sh                  # stop (scripts\stop_win.bat on Windows)
```

### What works without history

A fresh clone has no market history; it is not in the repository. Home, Live Desk, Data, News
and Settings work without it. **Backtest Lab, Simulator and Machine Learning Lab (60m and 4h)
need it.** Download it once (free, from Yahoo; the full download is long):

```bash
.venv/bin/python -m onelot backfill
.venv/bin/python -m onelot quality
```

For a quick first try: `backfill --series BANKNIFTY --resolutions 1d`.

### Real data

Paste your Dhan client ID and access token in the Settings tab, or copy `.env.example` to
`.env` and fill it in (`cp .env.example .env`). Then run `python -m onelot preflight`: it says
in plain words what is missing (history, credentials) and which Dhan endpoints your token
reaches. Start without `--mock` to use them.

### Update

```bash
git pull
.venv/bin/python -m pip install -r requirements.txt
```

`requirements.lock.txt` pins the exact versions this desk was tested with (Python 3.12). Use
`pip install -r requirements.lock.txt` for that exact environment, and `requirements.txt` for
newer compatible versions or on Windows.

### Container

```bash
docker build -t onelot-desk .
docker compose up --build            # http://127.0.0.1:8787/ ; ./data and ./logs are mounted
```

The image installs from the lock file, runs as a non-root user and publishes the port on
127.0.0.1 only. Credentials come from `.env` if you made one. These two commands have not
been run end to end yet: the machine that wrote them had no running Docker daemon.

### Using the tabs

1. **Settings, Dhan credentials**: paste your client ID and the free-tier access token, Save,
   then **Test entitlements**. The report shows two tiers: *trading* (profile, funds, holdings,
   positions, ledger, trade book, which must all pass) and *data* (expiry list, option chain,
   historical bars; `NO_DATA_API` until the plan is bought).
2. **Home** then shows your ledger and funds. Press Refresh whenever you want; it never polls.
3. **Data**: coverage of the local history, backfill jobs and the quality report.
4. **Backtest Lab** (needs history): `Desk watchlist, NIFTY BANK options, EMA Cross, 12m, Run
   backtest`. Then *Walk-forward* on anything that looks good. Only out-of-sample numbers count.
   `python -m onelot sweep` and `python -m onelot leaderboard` do the same from the shell.
5. **Live Desk** works immediately: 4 traded indices, MIDCPNIFTY for training, 18 stocks.
6. Once the Data API is active: `python -m onelot backfill --provider dhan` (five years of
   1-minute bars) and re-run the research gate.

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
  endpoint, see `docs/13_DHAN_SETUP.md`). Because the entitlement is on the account, either of
  your two tokens works for everything once the plan is active. `.env` has two slots so the
  paid one can be kept separate: `DHAN_ACCESS_TOKEN` and optional `DHAN_DATA_ACCESS_TOKEN`.
* **Postback URL**: optional; it only receives **order updates**. This desk never places orders,
  so leave it blank, or point it at `http://<host>:8787/api/dhan/postback`, which records what
  arrives and acts on nothing.

---

## Telegram: alerts on your phone

SIGNAL, EXIT and CRITICAL alerts also go to a private Telegram chat (INFO, WARNING and the
startup self-test stay in the app). Messages are queued in the database first (`telegram_outbox`),
sent at most one per second and retried after outages; an alert that expired while Telegram was
unreachable arrives as a short "Missed" note instead (one "Missed N alerts" digest after a long
outage), and a late EXIT or CRITICAL shows the time it happened plus "sent late". Outside market
hours SIGNAL and EXIT arrive silently; CRITICAL always sounds.

1. In Telegram, open **@BotFather**, send `/newbot` and follow the prompts. BotFather replies with
   the bot token: put it in `.env` as `TELEGRAM_BOT_TOKEN`.
2. Open a chat with your new bot and send it any message (a bot cannot write to you first).
3. Find your chat id: open `https://api.telegram.org/bot<your token>/getUpdates` in a browser
   and copy `result[0].message.chat.id` (a group id starts with `-`). Put it in `.env` as
   `TELEGRAM_CHAT_ID`. Treat that URL like a password: it contains the token.
4. Restart the app. The log says `Telegram not configured` when either value is missing or
   malformed. `notify.telegram: false` in `config/config.yaml` turns phone alerts off.

The token is never logged, shown in the UI or stored in the outbox.

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
  IPFT, stamp, GST. Still `verified_against_contract_note: false`, reconcile it.
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

The Lab's library, for research. The desk itself trades only the two rules above.

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
| `intraday_momentum` | First half-hour predicts the last half-hour (Gao, Han, Li and Zhou 2018; international evidence Li 2020). | index futures / options |
| `gap_fade` | Short stocks that gap far above NIFTY's own gap; the one Fyers-era finding that held on a 2022-26 holdout (now WATCH). | stocks |
| `benchmark_random` | Control: random entries with the same exits. Real strategies must beat it out of sample. | research only |

Every parameter is tunable in the Lab and searched by the optimizer; the indicators each
strategy uses are listed next to the form. Sources and what the evidence actually supports are
in `docs/12_STRATEGY_RESEARCH.md`.

---

## The original Bank Nifty engine

The first build's live option engine (`bnta`) still runs underneath the Live Desk. Two
defects were fixed in the merge:

* **Crash recovery now really recovers**: open positions restored from the database are
  re-subscribed on the feed before the Guardian starts.
* **A dead option quote is no longer silent**: the Guardian pulls a REST quote, marks the
  position *degraded* after `quote_stale_after_sec`, warns, and escalates to CRITICAL every
  minute while the position stays unmanaged.

The desk's two rules are described in `docs/04_OPERATIONS.md`; `config/desk.yaml` holds their
exact settings and the pause switch of every slot.

---

## Configuration

| File | What |
|---|---|
| `config/desk.yaml` | the Live Desk: the two rules and their settings, the 5 indices, the 18 stocks |
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
docs/                    00 to 08 live-engine design docs · 09 to 13 unified desk docs · research/
```

---

## Tests

```bash
scripts/gate.sh                          # the quality gate: format, lint, mypy ratchet, tests
.venv/bin/python -m pytest               # tests only; `live` tests are skipped by default
```

`scripts/gate.sh` runs exactly the commands listed in
[CONTRIBUTING.md](CONTRIBUTING.md#running-everything).

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
