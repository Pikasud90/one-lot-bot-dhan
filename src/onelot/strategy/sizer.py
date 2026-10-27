"""Position sizing — or rather, the absence of it.

THE RULE: exactly one lot, always, whatever it costs.

This module used to search for a lot count against a risk budget, then briefly
acted as an affordability gate. Both are gone. One lot is affordable by
definition, so there is no quantity decision and no signal is ever rejected for
being "too expensive". What remains is pricing that one lot and reporting what
it will cost you.

Why this is safe rather than reckless: risk on a single trade is bounded by the
STOP, and extreme volatility -- the thing that makes a stop wide in rupees -- is
already excluded upstream by the intraday chop filter's ATR-percentile band.
Beyond that, loss is capped by absolute daily and cumulative-drawdown limits in
`RiskCfg`, which is where risk control actually belongs for a fixed-size system.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import ExecAssumptionsCfg, RiskCfg
from ..costs import CostModel, Quote, TradeEconomics, compute_economics


@dataclass(frozen=True)
class SizingResult:
    lots: int
    qty: int
    economics: TradeEconomics
    rejected_reason: str | None = None
    concentration_pct: float | None = None      # of declared capital, if any

    @property
    def is_tradeable(self) -> bool:
        return self.lots >= 1 and self.rejected_reason is None

    def as_dict(self) -> dict:
        return {"lots": self.lots, "qty": self.qty,
                "rejected_reason": self.rejected_reason,
                "concentration_pct": self.concentration_pct,
                "economics": self.economics.as_dict()}


def size_position(
    *,
    quote: Quote,
    stop_distance: float,
    rr_target_gross: float,
    lot_size: int,
    risk_cfg: RiskCfg,
    cost_model: CostModel,
    exec_cfg: ExecAssumptionsCfg,
    tick: float = 0.05,
    capital: float = 0.0,
) -> SizingResult:
    """Price exactly `risk_cfg.lots_per_trade` lots. Nothing is rejected on cost.

    The only rejection left is a market that does not exist: a zero or crossed
    bid/ask means there is nothing to buy, which is a data problem rather than
    a risk decision.
    """
    lots = risk_cfg.lots_per_trade
    econ = compute_economics(
        quote=quote, stop_distance=stop_distance, rr_target_gross=rr_target_gross,
        lots=lots, lot_size=lot_size, cost_model=cost_model,
        exec_cfg=exec_cfg, tick=tick,
    )

    if not quote.is_valid:
        return SizingResult(0, 0, econ,
                            "no two-sided market (zero or crossed bid/ask)")

    concentration = (econ.premium_outlay / capital) if capital > 0 else None
    return SizingResult(lots, econ.qty, econ, None, concentration)
