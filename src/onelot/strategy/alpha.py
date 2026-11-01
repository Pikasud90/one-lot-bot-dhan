"""Alpha engine — gate orchestration on CLOSED bars only.

Runs once per closed 5-minute bar (INVARIANT I2). Evaluates BOTH directions
and ALL gates every time, so the audit trail is complete even on bars that
produce nothing — the histogram that answers "why no signal today?".

Emits at most one Signal, fully sized, fully costed, with a TTL.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field

import numpy as np

from ..analytics.indicators import atr, ema, rsi
from ..analytics.ivp import IVPResult
from ..analytics.regime import IntradayChopResult, RegimeResult, intraday_chop_filter
from ..broker.base import ChainRow
from ..config import AppConfig
from ..costs import CostModel, Quote, TradeEconomics
from . import gates as G
from .selector import SelectionResult, select_contract
from .sizer import SizingResult, size_position

log = logging.getLogger(__name__)


@dataclass
class EvaluationContext:
    """Everything the engine needs, gathered once per bar."""

    now: dt.datetime
    bar_ts: dt.datetime
    session_window: object | None

    health_may_signal: bool
    health_reasons: list[str]

    index_high: np.ndarray
    index_low: np.ndarray
    index_close: np.ndarray

    chain_rows: list[ChainRow]
    chain_age_sec: float

    regime: RegimeResult
    ivp: IVPResult

    expiry: str
    lot_size: int

    risk_allowed: bool
    risk_reason: str
    risk_state: str
    open_positions: int

    last_signal_at: dt.datetime | None = None
    recent_signal_keys: set[str] = field(default_factory=set)
    entries_enabled: bool = True
    # Needed only when a Lab strategy supplies the direction.
    index_open: np.ndarray | None = None
    index_ts: np.ndarray | None = None


@dataclass
class DirectionEvaluation:
    direction: str
    report: G.GateReport
    selection: SelectionResult | None = None
    sizing: SizingResult | None = None
    economics: TradeEconomics | None = None
    contract: ChainRow | None = None

    @property
    def passed(self) -> bool:
        return self.report.passed and self.sizing is not None and self.sizing.is_tradeable


@dataclass
class Signal:
    signal_id: str
    created_at: dt.datetime
    expires_at: dt.datetime
    direction: str
    underlying: str
    expiry: str
    strike: float
    opt_type: str
    security_id: str | None
    quote: Quote
    economics: TradeEconomics
    lots: int
    qty: int
    lot_size: int
    delta: float
    iv: float | None
    theta_per_hour: float | None
    regime: RegimeResult
    ivp: IVPResult
    chop: IntradayChopResult | None
    rsi_value: float
    ema_value: float
    atr_value: float
    warnings: list[str] = field(default_factory=list)

    @property
    def contract_key(self) -> str:
        return f"{self.expiry}_{self.strike:g}_{self.opt_type}"

    def is_expired(self, now: dt.datetime) -> bool:
        return now >= self.expires_at

    def seconds_remaining(self, now: dt.datetime) -> float:
        return max(0.0, (self.expires_at - now).total_seconds())

    def as_dict(self) -> dict:
        e = self.economics
        return {
            "signal_id": self.signal_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "direction": self.direction,
            "underlying": self.underlying,
            "expiry": self.expiry,
            "strike": self.strike,
            "opt_type": self.opt_type,
            "security_id": self.security_id,
            "quote": {
                "bid": self.quote.bid,
                "ask": self.quote.ask,
                "ltp": self.quote.ltp,
                "spread": round(self.quote.spread, 2),
            },
            "entry_eff": e.entry_eff,
            "stop_level": e.stop_level,
            "target_level": e.target_level,
            "breakeven": e.breakeven,
            "lots": self.lots,
            "qty": self.qty,
            "lot_size": self.lot_size,
            "net_risk": round(e.net_risk, 2),
            "net_reward": round(e.net_reward, 2),
            "rr_net": round(e.rr_net, 3),
            "premium_outlay": round(e.premium_outlay, 2),
            "delta": round(self.delta, 4),
            "iv": self.iv,
            "theta_per_hour": self.theta_per_hour,
            "regime": self.regime.as_dict(),
            "ivp": self.ivp.as_dict(),
            "chop": self.chop.as_dict() if self.chop else None,
            "rsi": round(self.rsi_value, 2),
            "ema": round(self.ema_value, 2),
            "atr": round(self.atr_value, 2),
            "warnings": list(self.warnings),
            "cost_detail": {
                "entry": e.entry_costs.as_dict(),
                "target_exit": e.target_costs.as_dict(),
                "cost_pct_of_gross": round(e.cost_pct_of_gross, 4),
            },
        }

    def alert_text(self) -> str:
        """What the human actually reads. Every rupee figure is net of costs."""
        e = self.economics
        flags = ""
        if self.warnings:
            flags = "\n! " + "\n! ".join(self.warnings)
        return (
            f"BUY {self.underlying} {self.expiry[5:]} {self.strike:g} {self.opt_type}"
            f"  x{self.lots} lot{'s' if self.lots > 1 else ''} ({self.qty} qty)\n"
            f"Entry ~{e.entry_eff:.2f} (ask {self.quote.ask:.2f})"
            f"   Breakeven {e.breakeven:.2f}\n"
            f"Target {e.target_level:.2f} (net +Rs {e.net_reward:,.0f})   "
            f"Stop {e.stop_level:.2f} (net -Rs {e.net_risk:,.0f})   "
            f"R/R {e.rr_net:.2f}\n"
            f"Delta {self.delta:+.2f}  IV {(self.iv or 0) * 100:.1f}  "
            f"IVP {self.ivp.percentile:.0f}  Spread {self.quote.spread:.2f}\n"
            f"{self.regime.regime.value} {self.regime.confidence:.2f} | "
            f"RSI {self.rsi_value:.1f} | ATR {self.atr_value:.0f}"
            f"{flags}"
        )


@dataclass
class EvaluationOutcome:
    eval_id: str
    bar_ts: dt.datetime
    now: dt.datetime
    signal: Signal | None
    evaluations: list[DirectionEvaluation]
    global_gates: list[G.GateResult]
    blocked_by: str | None

    def all_gate_dicts(self) -> list[dict]:
        out = [g.as_dict() for g in self.global_gates]
        for ev in self.evaluations:
            for r in ev.report.results:
                d = r.as_dict()
                d["gate"] = f"{ev.direction}:{d['gate']}"
                out.append(d)
        return out

    def summary(self) -> str:
        if self.signal:
            return f"SIGNAL {self.signal.direction} {self.signal.strike:g}"
        if self.blocked_by:
            return f"no signal (blocked at {self.blocked_by})"
        parts = [f"{e.direction}: {e.report.summary()}" for e in self.evaluations]
        return "no signal — " + " | ".join(parts)


class AlphaEngine:
    def __init__(self, cfg: AppConfig, cost_model: CostModel):
        self.cfg = cfg
        self.cost_model = cost_model
        self.lab = None
        if cfg.strategy.live.source == "lab":
            from .lab_source import LabDirectionSource

            self.lab = LabDirectionSource(cfg.strategy.live, cfg.data.bar_interval_min)
            log.info(
                "Live direction source: Lab strategy %s @ %s", self.lab.name, self.lab.timeframe
            )

    # ── global, direction-independent gates ───────────────────────────────
    def _global_gates(self, ctx: EvaluationContext) -> list[G.GateResult]:
        c = self.cfg
        return [
            G.gate_health(ctx.health_may_signal, ctx.health_reasons),
            G.gate_session(ctx.now, ctx.session_window, ctx.entries_enabled),
            G.gate_no_open_position(ctx.open_positions, c.risk.max_open_positions),
            G.gate_risk_governor(ctx.risk_allowed, ctx.risk_reason, ctx.risk_state),
            G.gate_cooldown(ctx.last_signal_at, ctx.now, c.strategy.signal.cooldown_minutes),
            G.gate_chain_fresh(ctx.chain_age_sec, c.data.quote_stale_after_sec),
            G.gate_expiry_safety(ctx.expiry, ctx.now.date()),
        ]

    # ── per-direction evaluation ──────────────────────────────────────────
    def _evaluate_direction(
        self,
        ctx: EvaluationContext,
        direction: str,
        chop: IntradayChopResult,
        ema_val: float,
        rsi_val: float,
        atr_val: float,
        lab=None,
    ) -> DirectionEvaluation:
        s = self.cfg.strategy
        rep = G.GateReport(direction=direction)
        ev = DirectionEvaluation(direction=direction, report=rep)

        close = float(ctx.index_close[-1]) if len(ctx.index_close) else float("nan")

        rep.add(G.gate_regime(ctx.regime, direction, s.regime.enabled))
        rep.add(G.gate_intraday_chop(chop))
        if lab is not None:
            # A Lab strategy replaces the trend + momentum gates; it is the
            # only thing allowed to say "which side". The chop filter above
            # stays as the extreme-volatility guard, and IVP below as the
            # premium-richness guard.
            fired = lab.fired(direction)
            rep.add(
                G.GateResult(
                    "lab_strategy",
                    fired,
                    f"{lab.strategy}@{lab.timeframe}",
                    "" if fired else (lab.reason or "strategy did not fire this side"),
                )
            )
        else:
            rep.add(G.gate_trend(close, ema_val, direction))
            rep.add(G.gate_momentum(rsi_val, direction, s.momentum.ce_band, s.momentum.pe_band))
        rep.add(G.gate_ivp(ctx.ivp, s.volatility.ivp_max))

        # Contract selection runs regardless, so the rejection histogram is
        # complete even when an earlier gate already failed.
        sel = select_contract(ctx.chain_rows, direction, s.contract, ctx.lot_size)
        ev.selection = sel
        rep.add(
            G.GateResult(
                "contract",
                sel.ok,
                sel.chosen and f"{sel.chosen.strike:g}{sel.chosen.opt_type}" or "",
                sel.reason,
            )
        )
        if not sel.ok or sel.quote is None:
            return ev

        row = sel.chosen
        ev.contract = row
        premium = sel.quote.mid

        rep.add(
            G.gate_theta(
                row.theta / 24.0 if row.theta is not None else None,
                premium,
                s.theta.expected_hold_hours,
                s.theta.max_theta_burn_pct,
            )
        )

        # Stop distance in PREMIUM points: index ATR scaled by the contract's
        # delta. This is where v1's ATR-in-index-points and premium-denominated
        # stop had to be reconciled explicitly.
        if np.isnan(atr_val) or atr_val <= 0 or row.delta is None:
            rep.add(
                G.GateResult(
                    "net_rr", False, "n/a", "ATR or delta unavailable — cannot compute a stop"
                )
            )
            return ev
        stop_distance = s.exits.sl_atr_mult * atr_val * abs(row.delta)
        if lab is not None and lab.fired(direction) and lab.stop_points(direction):
            # The Lab strategy's STRUCTURAL stop (swing / Supertrend line / channel),
            # translated to premium points through delta, capped so a very wide
            # structure cannot silently turn one lot into an outsized rupee risk.
            structural = lab.stop_points(direction) * abs(row.delta)
            stop_distance = min(max(structural, stop_distance * 0.5), stop_distance * 2.0)

        sizing = size_position(
            quote=sel.quote,
            stop_distance=stop_distance,
            rr_target_gross=s.exits.rr_target_gross,
            lot_size=ctx.lot_size,
            risk_cfg=self.cfg.risk,
            cost_model=self.cost_model,
            exec_cfg=self.cfg.execution_assumptions,
            tick=self.cfg.instrument.tick_size,
            capital=self.cfg.account.capital_inr,
        )
        ev.sizing = sizing
        ev.economics = sizing.economics

        rep.add(
            G.gate_net_rr(sizing.economics.rr_net, s.exits.rr_net_min, sizing.economics.warnings)
        )
        rep.add(G.gate_market(sizing.lots, sizing.rejected_reason))
        rep.add(G.gate_dedup(ctx.recent_signal_keys, f"{ctx.expiry}_{row.strike:g}_{row.opt_type}"))
        return ev

    # ── main entry point ──────────────────────────────────────────────────
    def evaluate(self, ctx: EvaluationContext) -> EvaluationOutcome:
        eval_id = uuid.uuid4().hex[:16]
        s = self.cfg.strategy

        global_gates = self._global_gates(ctx)
        blocking = [g for g in global_gates if not g.passed and not g.advisory]

        # Indicators on CLOSED bars only.
        n = len(ctx.index_close)
        ema_val = float(ema(ctx.index_close, s.trend.ema_period)[-1]) if n else float("nan")
        rsi_val = float(rsi(ctx.index_close, s.momentum.rsi_period)[-1]) if n else float("nan")
        atr_arr = atr(ctx.index_high, ctx.index_low, ctx.index_close, s.exits.atr_period)
        atr_val = float(atr_arr[-1]) if n else float("nan")

        chop = intraday_chop_filter(
            ctx.index_high,
            ctx.index_low,
            ctx.index_close,
            adx_min=s.intraday_chop.adx_min,
            atr_pct_min=s.intraday_chop.atr_percentile_min,
            atr_pct_max=s.intraday_chop.atr_percentile_max,
            adx_period=s.guardian.adx_period,
            atr_period=s.exits.atr_period,
        )

        lab = None
        if self.lab is not None and ctx.index_ts is not None and ctx.index_open is not None:
            lab = self.lab.evaluate(
                ctx.index_ts, ctx.index_open, ctx.index_high, ctx.index_low, ctx.index_close
            )

        evaluations = [
            self._evaluate_direction(ctx, d, chop, ema_val, rsi_val, atr_val, lab)
            for d in ("CE", "PE")
        ]

        if blocking:
            return EvaluationOutcome(
                eval_id, ctx.bar_ts, ctx.now, None, evaluations, global_gates, blocking[0].gate
            )

        winners = [e for e in evaluations if e.passed]
        if not winners:
            return EvaluationOutcome(
                eval_id, ctx.bar_ts, ctx.now, None, evaluations, global_gates, None
            )

        winners.sort(key=lambda e: -(e.economics.rr_net if e.economics else 0.0))
        signal = self._build_signal(ctx, winners[0], chop, rsi_val, ema_val, atr_val)
        log.info(
            "SIGNAL %s %s %g %s  lots=%d  rr_net=%.2f  net_risk=Rs%.0f",
            signal.signal_id,
            signal.direction,
            signal.strike,
            signal.opt_type,
            signal.lots,
            signal.economics.rr_net,
            signal.economics.net_risk,
        )
        return EvaluationOutcome(
            eval_id, ctx.bar_ts, ctx.now, signal, evaluations, global_gates, None
        )

    def _build_signal(
        self,
        ctx: EvaluationContext,
        ev: DirectionEvaluation,
        chop,
        rsi_val: float,
        ema_val: float,
        atr_val: float,
    ) -> Signal:
        s = self.cfg.strategy
        warnings: list[str] = list(ev.economics.warnings)
        if ctx.ivp.mode == "degraded" and not ctx.ivp.sufficient:
            warnings.append(
                f"IV percentile is DEGRADED ({ctx.ivp.n_observations}/"
                f"{ctx.ivp.required_observations} sessions)"
            )
        if ev.contract.greek_source == "local":
            warnings.append("greeks computed locally — broker did not supply them")

        # Concentration is a property of a FIXED lot on a small account, not a
        # mistake -- but it must never be silent.
        cap = self.cfg.account.capital_inr
        if cap > 0 and ev.economics.premium_outlay > 0:
            share = ev.economics.premium_outlay / cap
            if share >= self.cfg.risk.deployment_warn_pct:
                warnings.append(
                    f"CONCENTRATION: this one lot is Rs "
                    f"{ev.economics.premium_outlay:,.0f} of premium — "
                    f"{share:.0%} of your capital in a single option"
                )

        return Signal(
            signal_id=uuid.uuid4().hex[:16],
            created_at=ctx.now,
            expires_at=ctx.now + dt.timedelta(seconds=s.signal.ttl_seconds),
            direction=ev.direction,
            underlying=self.cfg.instrument.underlying_symbol,
            expiry=ctx.expiry,
            strike=ev.contract.strike,
            opt_type=ev.contract.opt_type,
            security_id=ev.contract.security_id,
            quote=ev.selection.quote,
            economics=ev.economics,
            lots=ev.sizing.lots,
            qty=ev.sizing.qty,
            lot_size=ctx.lot_size,
            delta=ev.contract.delta,
            iv=ev.contract.iv,
            theta_per_hour=(ev.contract.theta / 24.0 if ev.contract.theta is not None else None),
            regime=ctx.regime,
            ivp=ctx.ivp,
            chop=chop,
            rsi_value=rsi_val,
            ema_value=ema_val,
            atr_value=atr_val,
            warnings=warnings,
        )
