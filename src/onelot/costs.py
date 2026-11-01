"""Full statutory cost model for NSE index options, plus execution friction.

This module exists because v1 modelled roughly 40% of real trading friction
(defects A1/A2). It under-counted by 3-4x, which is the difference between a
signal that clears a 2.0 net R/R and one that doesn't.

Everything the human is shown in rupees flows through here (INVARIANT I3).

Modelled per leg:
    brokerage, STT (sell), exchange transaction charge, SEBI turnover fee,
    NSE IPFT, stamp duty (buy), GST on the fee components.

Modelled at execution:
    you BUY THE ASK and SELL THE BID, plus configured slippage in ticks.
    v1 assumed you transact at LTP on both sides, which is free money that
    does not exist.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from .config import CostConfig, ExecAssumptionsCfg

Side = Literal["buy", "sell"]


def round_to_tick(price: float, tick: float = 0.05, mode: str = "nearest") -> float:
    """Snap a price to the exchange tick grid.

    Rounding direction matters: an entry rounded *down* and an exit rounded
    *up* would quietly manufacture edge that the exchange will not give you.
    """
    if tick <= 0:
        return round(price, 2)
    q = price / tick
    if mode == "up":
        n = math.ceil(q - 1e-9)
    elif mode == "down":
        n = math.floor(q + 1e-9)
    else:
        n = math.floor(q + 0.5)
    return round(n * tick, 2)


@dataclass(frozen=True)
class CostBreakdown:
    """Itemised charges for one leg. Every field is rupees."""

    side: Side
    premium: float
    qty: int
    turnover: float
    brokerage: float
    stt: float
    exchange_txn: float
    sebi_fee: float
    ipft: float
    stamp_duty: float
    gst: float
    total: float

    def as_dict(self) -> dict[str, float]:
        return {
            "side": self.side,
            "premium": self.premium,
            "qty": self.qty,
            "turnover": round(self.turnover, 2),
            "brokerage": round(self.brokerage, 2),
            "stt": round(self.stt, 2),
            "exchange_txn": round(self.exchange_txn, 2),
            "sebi_fee": round(self.sebi_fee, 4),
            "ipft": round(self.ipft, 4),
            "stamp_duty": round(self.stamp_duty, 2),
            "gst": round(self.gst, 2),
            "total": round(self.total, 2),
        }


class CostModel:
    """Stateless calculator over a loaded `CostConfig`."""

    def __init__(self, cfg: CostConfig):
        self.cfg = cfg

    # ── single leg ────────────────────────────────────────────────────────
    def _brokerage(self, turnover: float) -> float:
        b = self.cfg.brokerage
        if b.mode == "flat":
            return b.per_order_inr
        return min(b.per_order_inr, b.pct_of_turnover * turnover, b.cap_inr)

    def leg(self, premium: float, qty: int, side: Side) -> CostBreakdown:
        t = self.cfg.taxes
        turnover = premium * qty

        brokerage = self._brokerage(turnover)
        # Options/futures/intraday equity: STT on the sell leg only. Delivery
        # equity pays it both ways (stt_buy_pct > 0). Currency pays none.
        stt = (t.stt_sell_pct if side == "sell" else t.stt_buy_pct) * turnover
        exchange_txn = t.exchange_txn_pct * turnover
        sebi_fee = t.sebi_turnover_pct * turnover
        ipft = t.ipft_pct * turnover
        stamp_duty = t.stamp_duty_pct * turnover if side == "buy" else 0.0
        # GST applies to the service components only — never to STT or stamp duty.
        gst = t.gst_pct * (brokerage + exchange_txn + sebi_fee + ipft)

        total = brokerage + stt + exchange_txn + sebi_fee + ipft + stamp_duty + gst
        return CostBreakdown(
            side=side,
            premium=premium,
            qty=qty,
            turnover=turnover,
            brokerage=brokerage,
            stt=stt,
            exchange_txn=exchange_txn,
            sebi_fee=sebi_fee,
            ipft=ipft,
            stamp_duty=stamp_duty,
            gst=gst,
            total=total,
        )

    # ── round trip ────────────────────────────────────────────────────────
    def round_trip_cost(self, entry: float, exit_: float, qty: int) -> float:
        return self.leg(entry, qty, "buy").total + self.leg(exit_, qty, "sell").total

    def net_pnl(self, entry: float, exit_: float, qty: int) -> float:
        """Net rupees for a long-option round trip. Negative = loss."""
        gross = (exit_ - entry) * qty
        return gross - self.round_trip_cost(entry, exit_, qty)

    def breakeven_premium(self, entry: float, qty: int, tick: float = 0.05) -> float:
        """The exit premium at which net P&L is exactly zero.

        Solved by bisection: net_pnl is strictly increasing in the exit price
        (the sell-side variable rate is ~0.15%, nowhere near 100%), so a
        bracket-and-halve is exact to the tick and works for every brokerage
        mode including the nonlinear `min_of`.

        This is the single most useful number on the alert. v1 never computed it.
        """
        lo, hi = entry, entry * 2.0 + 10.0
        # Guarantee the bracket actually contains the root.
        guard = 0
        while self.net_pnl(entry, hi, qty) < 0 and guard < 60:
            hi = hi * 2.0 + 10.0
            guard += 1
        for _ in range(200):
            mid = (lo + hi) / 2.0
            if self.net_pnl(entry, mid, qty) < 0:
                lo = mid
            else:
                hi = mid
            if hi - lo < 1e-7:
                break
        # Round UP to the tick: you cannot break even at a price you can't quote.
        return round_to_tick(hi, tick, mode="up")

    def exercise_stt_if_expired_itm(self, intrinsic_value: float, qty: int) -> float:
        """STT charged if an ITM option is left to expire instead of squared off.

        Levied on INTRINSIC SETTLEMENT VALUE, not premium — which is why this
        dwarfs the entire rest of the cost stack and why mandatory square-off
        is a hard rule (v1 defect A3).
        """
        return self.cfg.taxes.stt_exercise_pct * intrinsic_value * qty


# ─────────────────────── execution-effective pricing ──────────────────────


@dataclass(frozen=True)
class Quote:
    """A two-sided market. `ltp` is deliberately NOT used for sizing decisions."""

    bid: float
    ask: float
    ltp: float = 0.0

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.ltp

    @property
    def spread(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return max(0.0, self.ask - self.bid)
        return float("inf")

    @property
    def spread_pct(self) -> float:
        m = self.mid
        return self.spread / m if m > 0 else float("inf")

    @property
    def is_valid(self) -> bool:
        # A zero bid or ask means no market. v1 would have sized off it.
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid


def effective_entry(quote: Quote, exec_cfg: ExecAssumptionsCfg, tick: float) -> float:
    """What you actually pay to get long: the ask, plus slippage, rounded up."""
    raw = quote.ask + exec_cfg.slippage_ticks_entry * tick
    return round_to_tick(raw, tick, mode="up")


def effective_exit(level: float, exec_cfg: ExecAssumptionsCfg, tick: float) -> float:
    """What you actually receive exiting at `level`: minus slippage, rounded down.

    `level` is a target or stop expressed as a premium. Exiting a long means
    hitting the bid, so realised proceeds are strictly below the quoted level.
    """
    raw = level - exec_cfg.slippage_ticks_exit * tick
    return round_to_tick(max(raw, 0.0), tick, mode="down")


# ────────────────────────── trade economics ───────────────────────────────


@dataclass(frozen=True)
class TradeEconomics:
    """Everything the gates and the human need, in net rupees.

    `rr_net` is the number that gates the signal. v1 gated on a gross ratio
    that no exit path was ever trying to achieve (defect A6).
    """

    qty: int
    lots: int
    lot_size: int

    entry_eff: float
    stop_level: float
    target_level: float
    stop_exit_eff: float
    target_exit_eff: float

    r_per_unit: float  # premium points of risk, per unit, gross
    net_risk: float  # rupees lost if the stop fills
    net_reward: float  # rupees made if the target fills
    rr_net: float
    breakeven: float

    entry_costs: CostBreakdown
    stop_costs: CostBreakdown
    target_costs: CostBreakdown

    premium_outlay: float
    total_cost_at_target: float
    cost_pct_of_gross: float
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "qty": self.qty,
            "lots": self.lots,
            "lot_size": self.lot_size,
            "entry_eff": self.entry_eff,
            "stop_level": self.stop_level,
            "target_level": self.target_level,
            "stop_exit_eff": self.stop_exit_eff,
            "target_exit_eff": self.target_exit_eff,
            "r_per_unit": round(self.r_per_unit, 2),
            "net_risk": round(self.net_risk, 2),
            "net_reward": round(self.net_reward, 2),
            "rr_net": round(self.rr_net, 3),
            "breakeven": self.breakeven,
            "premium_outlay": round(self.premium_outlay, 2),
            "total_cost_at_target": round(self.total_cost_at_target, 2),
            "cost_pct_of_gross": round(self.cost_pct_of_gross, 4),
            "warnings": list(self.warnings),
        }


def compute_economics(
    *,
    quote: Quote,
    stop_distance: float,
    rr_target_gross: float,
    lots: int,
    lot_size: int,
    cost_model: CostModel,
    exec_cfg: ExecAssumptionsCfg,
    tick: float = 0.05,
) -> TradeEconomics:
    """Build the full net-of-everything picture for a candidate trade.

    `stop_distance` is in premium points (e.g. 1.5 * ATR * abs(delta)).
    The target is placed at `rr_target_gross` multiples of that distance —
    a real, quotable level, so the Guardian has something to actually aim at.
    """
    qty = max(0, lots) * lot_size
    entry_eff = effective_entry(quote, exec_cfg, tick)

    stop_distance = max(stop_distance, tick)
    stop_level = round_to_tick(max(entry_eff - stop_distance, tick), tick, mode="down")
    target_level = round_to_tick(entry_eff + rr_target_gross * stop_distance, tick, mode="up")

    stop_exit_eff = effective_exit(stop_level, exec_cfg, tick)
    target_exit_eff = effective_exit(target_level, exec_cfg, tick)

    warnings: list[str] = []
    if qty == 0:
        # Still return a coherent object so the audit log can record *why*.
        zero = cost_model.leg(entry_eff, 0, "buy")
        return TradeEconomics(
            qty=0,
            lots=0,
            lot_size=lot_size,
            entry_eff=entry_eff,
            stop_level=stop_level,
            target_level=target_level,
            stop_exit_eff=stop_exit_eff,
            target_exit_eff=target_exit_eff,
            r_per_unit=stop_distance,
            net_risk=0.0,
            net_reward=0.0,
            rr_net=0.0,
            breakeven=cost_model.breakeven_premium(entry_eff, lot_size, tick),
            entry_costs=zero,
            stop_costs=zero,
            target_costs=zero,
            premium_outlay=0.0,
            total_cost_at_target=0.0,
            cost_pct_of_gross=0.0,
            warnings=["qty is zero — not sizeable within the risk budget"],
        )

    entry_costs = cost_model.leg(entry_eff, qty, "buy")
    stop_costs = cost_model.leg(stop_exit_eff, qty, "sell")
    target_costs = cost_model.leg(target_exit_eff, qty, "sell")

    net_risk = -cost_model.net_pnl(entry_eff, stop_exit_eff, qty)
    net_reward = cost_model.net_pnl(entry_eff, target_exit_eff, qty)
    rr_net = (net_reward / net_risk) if net_risk > 0 else 0.0

    breakeven = cost_model.breakeven_premium(entry_eff, qty, tick)
    gross_at_target = (target_exit_eff - entry_eff) * qty
    total_cost_at_target = entry_costs.total + target_costs.total
    cost_pct = (total_cost_at_target / gross_at_target) if gross_at_target > 0 else float("inf")

    if net_reward <= 0:
        warnings.append("target does not clear costs — the trade cannot win as specified")
    if breakeven > target_level:
        warnings.append("breakeven is above the target level")
    if cost_pct > 0.25:
        warnings.append(f"costs consume {cost_pct:.0%} of gross profit at target")

    return TradeEconomics(
        qty=qty,
        lots=lots,
        lot_size=lot_size,
        entry_eff=entry_eff,
        stop_level=stop_level,
        target_level=target_level,
        stop_exit_eff=stop_exit_eff,
        target_exit_eff=target_exit_eff,
        r_per_unit=stop_distance,
        net_risk=net_risk,
        net_reward=net_reward,
        rr_net=rr_net,
        breakeven=breakeven,
        entry_costs=entry_costs,
        stop_costs=stop_costs,
        target_costs=target_costs,
        premium_outlay=entry_eff * qty,
        total_cost_at_target=total_cost_at_target,
        cost_pct_of_gross=cost_pct,
        warnings=warnings,
    )
