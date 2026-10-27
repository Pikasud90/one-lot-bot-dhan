# 12 — Strategy research notes and sources

What was searched (October 2026), what the evidence actually supports, and how it shaped the
strategy set. The short version: **almost nothing published for Indian intraday retail
strategies is verifiable**; the two things with real evidence are late-session index
momentum and the overnight/intraday asymmetry; everything else is a hypothesis the Lab exists
to test.

## What has evidence

| Effect | Evidence | How it is used |
|---|---|---|
| **Intraday time-series momentum** — the first half-hour return predicts the last half-hour | Gao, Han, Li & Zhou, *Journal of Financial Economics* 2018 (SSRN 2440866): S&P 500 ETF 1993–2013, R² 1.6 %, stronger on volatile/high-volume days. Li, *Intraday Time Series Momentum: International Evidence* (2020): holds across 12 equity markets. | `intraday_momentum` on index futures/options; decision at 14:45, hold to square-off, ATR filter on the opening move. |
| **Overnight vs intraday returns in India** — most of NIFTY's gain since 2011 arrived overnight; intraday returns are flat to negative on aggregate | Zerodha "In the Money" (2000–2025), Capitalmind summaries, a 2011–2020 decomposition on LinkedIn; the Fyers project's own gap-fade study (t ≈ 4.7 in 2015–21 and 2.3 in a 2022–26 holdout) | `gap_fade` on stocks (short the over-extended open, hold to close); a caution against pure intraday long bias on indices. |
| **Trend following via channel breakouts** | Decades of futures/FX evidence (Donchian, Turtle rules); costs and whipsaws decide | `donchian_breakout` with an ADX floor, mainly for index futures and currency. |
| **Low volatility / momentum factors on NSE** | vendor backtests (BacktestIndia 2006–2025: low-vol 12.4 % CAGR vs Nifty 10.4 %; momentum 14 % CAGR with −70 % drawdown) | Not implemented: positional, multi-stock, portfolio-level; the desk is one unit intraday. Noted for a later swing mode. |

## What has no verifiable evidence (and is in the Lab precisely to be tested)

* **9/21 EMA crossovers on Bank Nifty 3-minute charts** are described on every brokerage blog
  (Sahi, Quantzee, OneTradeJournal) without a single out-of-sample number. Your 12-minute
  variant with trend/momentum/volume filters is `ema_cross`.
* **Supertrend** settings: MarketNetra reports win rates of 31–36 % on NIFTY that are positive
  before costs and roughly break-even after on 5-minute bars; methodology undisclosed.
  Every vendor notes whipsaw in ranges. `supertrend_vol` exists so the optimizer, not a blog,
  picks the period and multiplier — and the research gate decides.
* **Opening-range breakout** "55–60 % profitable days" claims (OneTradeJournal and similar)
  have no stated data or costs. `orb` is in the Lab with its filters; the Fyers data rated it
  WATCH on 5 m (PF 1.19, 34 trades) and FAIL on 15 m/60 m.
* **TradingView scripts** with Bank Nifty "backtests" either cover a single year with fixed
  0.4 % targets or double the position after a loss (martingale). Not evidence.
* **Reddit / forum claims** could not be retrieved in a usable form; nothing found there
  carried expectancy, drawdown and costs together.

## The operator's thesis and how the Lab scores it

*"Even a 30 % hit rate over the long run can make a successful trader if winners are 3× losers."*
Arithmetic: at a 30 % win rate the break-even payoff is 0.70/0.30 = 2.33; at payoff 3 the
profit factor is 1.29 before costs. The Lab reports, for every run, the payoff actually
achieved, the break-even win rate it implies, and the PF the same exits would produce at
exactly 30 % wins. The first Yahoo-corpus runs show the common failure: exit-on-cross and
tight time stops cut winners to 0.3–1.3 R, so the geometry fails before the signal is even
judged. The levers are `target_r`, `trail_activate_r`, `trail_atr_mult`, `max_bars` and the
exit-on-flip switches — all tunable, all in the optimizer's space.

## Data caveat that dominates everything

Yahoo serves ~60 sessions of 2m/5m/15m bars and two years of 60m bars. Every intraday
verdict on this corpus is a screen, not a result. Dhan's Data API serves five years of
1-minute bars for every instrument and rolling ATM±N option history; the research gate must
be re-run on that before any strategy is promoted to live.

## Sources

* Gao, Han, Li, Zhou — "Market Intraday Momentum", JFE 2018 — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2440866
* Li — "Intraday Time Series Momentum: International Evidence" (2020) — http://wp.lancs.ac.uk/fofi2020/files/2020/04/FoFI-2020-092-Zeming-Li.pdf
* Zerodha, "The overnight drift" — https://inthemoneybyzerodha.substack.com/p/the-overnight-drift-why-markets-move
* Praveen Kumar, "Overnight vs intraday returns in Indian equity markets" — https://www.linkedin.com/pulse/overnight-vs-intraday-returns-indian-equity-markets-praveen-kumar
* BacktestIndia low-volatility / momentum studies — https://backtestindia.com/blog
* MarketNetra Supertrend on NIFTY — https://marketnetra.in/blog/supertrend-indicator-nifty-settings-strategy-backtest
* OneTradeJournal ORB — https://onetradejournal.com/strategies/opening-range-breakout
* Fyers-era research report (local) — `docs/FYERS_RESEARCH_REPORT_2026-10-05.md`
* DhanHQ v2 docs — https://dhanhq.co/docs/v2/ (historical, expired options, rate limits, annexure)
