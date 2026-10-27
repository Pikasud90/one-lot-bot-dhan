"""Replay runner: feed recorded or synthetic history through the live engine code."""

from __future__ import annotations

import datetime as dt
import logging
import warnings
from dataclasses import dataclass, field

import numpy as np

from ..analytics.ivp import IVPercentileEngine
from ..analytics.regime import RegimeModel, compute_for_date
from ..broker.mock import MockBroker, SyntheticConfig, SyntheticMarket
from ..clock import IST, SessionClock, TradingCalendar
from ..config import AppConfig, CostConfig
from ..costs import CostModel, Quote
from ..market.candles import CandleSeries
from ..market.chain import ChainCache
from ..risk.governor import RiskGovernor
from ..store import init_database
from ..store.repo import Repo
from ..strategy.alpha import AlphaEngine, EvaluationContext
from ..trade.guardian import Guardian, GuardianInputs
from ..trade.journal import TradeJournal
from ..trade.state import ExitReason, TradeStateMachine
from .paper import PaperFillModel

log = logging.getLogger(__name__)


@dataclass
class ReplayConfig:
    sessions: int = 40
    seed: int = 7
    drift_per_session: float = 0.0
    trend_strength: float = 0.006
    total_history: int = 300
    ivp_mode: str = "degraded"
    capital: float | None = None      # informational only
    take_every_signal: bool = True      # simulate a perfectly obedient human
    step_seconds: int = 60


@dataclass
class ReplayResult:
    sessions: int
    bars_evaluated: int
    signals: int
    trades: int
    metrics: dict = field(default_factory=dict)
    gate_histogram: list[dict] = field(default_factory=list)
    signals_per_session: list[int] = field(default_factory=list)
    exit_reasons: dict = field(default_factory=dict)

    def report(self) -> str:
        m = self.metrics
        lines = [
            "=" * 68,
            "  REPLAY RESULT  (identical code path to live)",
            "=" * 68,
            f"  sessions              {self.sessions}",
            f"  bars evaluated        {self.bars_evaluated}",
            f"  signals               {self.signals}"
            f"   ({self.signals / max(1, self.sessions):.2f}/session)",
            f"  trades closed         {self.trades}",
        ]
        if m.get("trades"):
            def _n(v, fmt="{:>12,.0f}"):
                return "n/a".rjust(12) if v is None else fmt.format(v)

            lines += [
                "-" * 68,
                f"  win rate              {m['win_rate']:.1%}"
                f"   ({m['wins']}W / {m['losses']}L)",
                f"  NET P&L               Rs {_n(m['net_pnl'])}",
                f"  gross P&L             Rs {_n(m['gross_pnl'])}",
                f"  total costs           Rs {_n(m['total_costs'])}"
                f"   ({(m.get('costs_pct_of_gross') or 0):.1%} of gross)",
                "-" * 68,
                f"  expectancy            {m['expectancy_r']:+.3f} R per trade",
                f"  avg R on winners      {m.get('avg_r_win'):+.2f}",
                f"  avg R on losers       {m.get('avg_r_loss'):+.2f}",
                f"  profit factor         {m.get('profit_factor')}",
                (f"  Sharpe (annualised)   {m.get('sharpe')}"
                 if m.get("sharpe") is not None
                 else f"  Sharpe                {m.get('sharpe_note')}"),
                (f"  Sortino (annualised)  {m.get('sortino')}"
                 if m.get("sortino") is not None else "  Sortino               n/a"),
                f"  SQN                   {m.get('sqn')}",
                "-" * 68,
                f"  best trade            Rs {_n(m['best'])}",
                f"  worst trade           Rs {_n(m['worst'])}",
                f"  best day              Rs {_n(m.get('best_day'))}",
                f"  worst day             Rs {_n(m.get('worst_day'))}",
                f"  peak equity           Rs {_n(m.get('peak_equity'))}",
                f"  max drawdown          Rs {_n(m['max_drawdown'])}"
                + (f"   ({m['max_drawdown_pct_of_peak']:.0%} of peak)"
                   if m.get("max_drawdown_pct_of_peak") else ""),
                f"  avg hold              {m['avg_hold_minutes']:.0f} min"
                f"   over {m.get('trading_days')} trading days",
                "-" * 68,
                "  exits by reason:",
            ]
            for k, v in sorted(m.get("by_exit_reason", {}).items(),
                               key=lambda kv: -kv[1]["count"]):
                lines.append(f"    {k:<16} n={v['count']:<4} "
                             f"net Rs {v['net_pnl']:>10,.0f}  win {v['win_rate']:.0%}")
        else:
            lines.append("  (no trades closed)")
        if self.gate_histogram:
            lines += ["-" * 68, "  top gate rejections:"]
            for g in self.gate_histogram[:8]:
                lines.append(f"    {g['gate']:<26} rejected {g['rejections']:<5} "
                             f"pass rate {g['pass_rate']:.0%}")
        lines.append("=" * 68)
        return "\n".join(lines)


class ReplayRunner:
    """Drives the real engine components over a synthetic or recorded tape."""

    def __init__(self, app_cfg: AppConfig, replay_cfg: ReplayConfig,
                 db_url: str = "sqlite:///:memory:"):
        cap = (replay_cfg.capital if replay_cfg.capital is not None
               else app_cfg.account.capital_inr)
        self.cfg = app_cfg.model_copy(update={
            "account": app_cfg.account.model_copy(update={"capital_inr": cap})})
        self.rc = replay_cfg
        self.cost_model = CostModel(
            CostConfig.load(self.cfg.project_root / "config" / "costs.yaml"))
        self.calendar = TradingCalendar({}, verified=True)
        self.session_clock = SessionClock(self.cfg.session, self.calendar)
        self.alpha = AlphaEngine(self.cfg, self.cost_model)
        self.guardian = Guardian(self.cfg, self.cost_model)
        self.fills = PaperFillModel(self.cfg.execution_assumptions,
                                    self.cfg.instrument.tick_size)
        self.ivp_engine = IVPercentileEngine(replay_cfg.ivp_mode, 252, 60)

        self.db = init_database(db_url)
        self.repo = Repo(self.db)
        self.journal = TradeJournal(self.repo)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.market = SyntheticMarket(SyntheticConfig(
                seed=replay_cfg.seed, sessions=replay_cfg.total_history,
                drift_per_session=replay_cfg.drift_per_session,
                trend_strength=replay_cfg.trend_strength))
        daily = self.market.daily_bars("index")
        self.daily_dates = [b.ts.date() for b in daily]
        self.daily_closes = np.array([b.close for b in daily])
        self.regime_model: RegimeModel | None = None

    def run(self) -> ReplayResult:
        sessions = self.market.session_dates[-self.rc.sessions:]
        bars = signals = trades = 0
        per_session: list[int] = []
        exit_reasons: dict[str, int] = {}

        for day in sessions:
            n_sig, n_trade, n_bars, reasons = self._run_session(day)
            signals += n_sig
            trades += n_trade
            bars += n_bars
            per_session.append(n_sig)
            for k, v in reasons.items():
                exit_reasons[k] = exit_reasons.get(k, 0) + v

        return ReplayResult(
            sessions=len(sessions), bars_evaluated=bars, signals=signals,
            trades=trades, metrics=self.journal.metrics(self.cfg.mode),
            gate_histogram=self.repo.gate_rejection_histogram(
                dt.datetime(2000, 1, 1, tzinfo=IST)),
            signals_per_session=per_session, exit_reasons=exit_reasons)

    def _run_session(self, day: dt.date) -> tuple[int, int, int, dict]:
        broker = MockBroker(self.market,
                            now=dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST))
        window = self.session_clock.window_for(day)

        idx = CandleSeries("INDEX", self.cfg.data.bar_interval_min,
                           window.open_at, has_volume=False)
        fut = CandleSeries("FUTURES", self.cfg.data.bar_interval_min,
                           window.open_at, has_volume=True)
        for d in [x for x in self.market.session_dates if x < day][-8:]:
            idx.seed_from_history(self.market.bars(d, self.cfg.data.bar_interval_min,
                                                   "index"))
            fut.seed_from_history(self.market.bars(d, self.cfg.data.bar_interval_min,
                                                   "futures"))

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            regime, self.regime_model = compute_for_date(
                self.daily_dates, self.daily_closes, day, model=self.regime_model,
                engine=self.cfg.strategy.regime.engine)

        iv_hist = [self.market.atm_iv(dt.datetime.combine(d, dt.time(12, 0), tzinfo=IST))
                   for d in self.market.session_dates if d < day]

        chain = ChainCache(broker, self.cfg.instrument.underlying_symbol,
                           broker.expiry_date.isoformat(),
                           forward_price_fn=lambda: self.market.futures_price(broker.now()))

        governor = RiskGovernor(self.cfg.risk, self.cfg.account.capital_inr,
                                self.cfg.mode, day.isoformat(), self.repo)
        state = TradeStateMachine(self.repo, self.cost_model, self.cfg, governor)

        idx_c = next(c for c in broker.instruments("BANKNIFTY")
                     if c.instrument_type == "INDEX")
        fut_c = next(c for c in broker.instruments("BANKNIFTY")
                     if c.instrument_type == "FUTIDX")

        n_sig = n_trade = n_bars = 0
        reasons: dict[str, int] = {}
        last_signal_at: dt.datetime | None = None
        recent_keys: set[str] = set()
        t = window.open_at

        while t <= window.close_at:
            broker.set_now(t)
            quotes = broker.quote([idx_c, fut_c])
            closed = []
            for c, series in ((idx_c, idx), (fut_c, fut)):
                q = quotes.get(c.security_id)
                if q and q.ltp:
                    got = series.on_tick(t, q.ltp, q.volume)
                    if series is idx:
                        closed += got
            closed += idx.mark_closed_through(t, self.cfg.data.bar_settle_delay_sec)
            fut.mark_closed_through(t, self.cfg.data.bar_settle_delay_sec)

            # ── manage any open position on every step ────────────────────
            for pos in list(state.open_positions.values()):
                n_trade += self._manage(pos, broker, chain, idx, fut, state,
                                        window, regime, t, reasons)

            if closed:
                chain.poll_once(broker.expiry_dt, t)
                a = idx.arrays()
                ivp = self.ivp_engine.compute(iv_hist, chain.atm_iv())
                allowed, why = governor.may_open(t)
                out = self.alpha.evaluate(EvaluationContext(
                    now=t, bar_ts=closed[-1].ts, session_window=window,
                    health_may_signal=True, health_reasons=[],
                    index_high=a["high"], index_low=a["low"], index_close=a["close"],
                    chain_rows=chain.snapshot.rows if chain.snapshot else [],
                    chain_age_sec=chain.age_seconds(t), regime=regime, ivp=ivp,
                    expiry=broker.expiry_date.isoformat(), lot_size=35,
                    risk_allowed=allowed, risk_reason=why,
                    risk_state=governor.state.value,
                    open_positions=len(state.open_positions),
                    last_signal_at=last_signal_at, recent_signal_keys=set(recent_keys)))
                n_bars += 1
                self.repo.record_gates(out.eval_id, t, None, out.all_gate_dicts())

                if out.signal:
                    n_sig += 1
                    last_signal_at = t
                    recent_keys.add(out.signal.contract_key)
                    state.register_signal(out.signal)
                    if self.rc.take_every_signal:
                        fill = self.fills.entry(out.signal.quote)
                        if fill:
                            state.acknowledge(out.signal.signal_id, "TAKE", "replay",
                                              fill.price, t)
            t += dt.timedelta(seconds=self.rc.step_seconds)

        # Force-close anything still open at the bell — same rule as live.
        for pos in list(state.open_positions.values()):
            q = self._option_quote(chain, pos)
            if q:
                f = self.fills.exit(q)
                if f:
                    state.confirm_exit(pos.position_id, f.price,
                                       ExitReason.EOD_SQUAREOFF, window.close_at)
                    n_trade += 1
                    reasons["EOD_SQUAREOFF"] = reasons.get("EOD_SQUAREOFF", 0) + 1
        return n_sig, n_trade, n_bars, reasons

    def _option_quote(self, chain: ChainCache, pos) -> Quote | None:
        snap = chain.snapshot
        if snap is None:
            return None
        for r in snap.rows:
            if r.strike == pos.strike and r.opt_type == pos.opt_type:
                return Quote(bid=r.bid or 0.0, ask=r.ask or 0.0, ltp=r.ltp or 0.0)
        return None

    def _manage(self, pos, broker, chain, idx, fut, state, window, regime, t,
                reasons) -> int:
        chain.poll_once(broker.expiry_dt, t)
        q = self._option_quote(chain, pos)
        if q is None or not q.is_valid:
            return 0
        a = idx.arrays()
        fa = fut.session_arrays(t.date())
        decision = self.guardian.evaluate(pos, GuardianInputs(
            now=t, quote=q,
            index_high=a["high"], index_low=a["low"], index_close=a["close"],
            futures_high=fa["high"], futures_low=fa["low"],
            futures_close=fa["close"], futures_volume=fa["volume"],
            session_window=window,
            regime_direction=regime.regime.value if regime else None))

        if decision.trail_stop is not None:
            state.update_trail(pos.position_id, decision.trail_stop,
                               decision.trail_active,
                               decision.high_water or pos.entry_price)
        if not decision.is_exit:
            return 0

        level = None
        if decision.reason in (ExitReason.HARD_SL, ExitReason.TRAIL_SL):
            level = decision.details.get("effective_stop")
        f = self.fills.exit(q, level)
        if f is None:
            return 0
        state.confirm_exit(pos.position_id, f.price, decision.reason, t)
        k = decision.reason.value
        reasons[k] = reasons.get(k, 0) + 1
        return 1
