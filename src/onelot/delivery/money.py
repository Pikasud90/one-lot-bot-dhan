"""The charges and net of a delivery trade in integer paise (US-067 R6). Pure.

Each leg is the delivery product's `CostModel.leg` on the trade's whole turnover, rounded once
to paise by `paper.charges.leg_paise` (the paper book's own rounding); the depository charge is
added once per sale. Net % is of the entry value. A price in rupees becomes paise rounded half
up to the paisa, not to the exchange tick, as the research priced its fills (design Q3).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from ..config import CostConfig
from ..costs import CostModel
from ..paper.charges import leg_paise

PRODUCT = "equity_delivery"


def rupees_to_paise(rupees: float) -> int:
    """Half up to the paisa (a float goes through its shortest repr, never its binary noise)."""
    return int((Decimal(repr(float(rupees))) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def delivery_model(costs: CostConfig) -> CostModel:
    return CostModel(costs.product(PRODUCT))


def depository_paise(costs: CostConfig) -> int:
    """The depository charge of one sale, in paise (0 when the cost table has none)."""
    product = costs.products.get(PRODUCT)
    return rupees_to_paise(product.depository_per_sale_inr) if product is not None else 0


@dataclass(frozen=True)
class TradeResult:
    gross_paise: int
    charges_paise: int  # both legs of the delivery product
    dp_paise: int  # the depository charge of the sale
    net_paise: int
    net_pct: float  # of the entry value

    def as_dict(self) -> dict[str, float | int]:
        return {
            "gross": self.gross_paise / 100,
            "charges": self.charges_paise / 100,
            "dp": self.dp_paise / 100,
            "net": self.net_paise / 100,
            "net_pct": round(self.net_pct, 4),
        }


def trade_result(
    model: CostModel, entry_paise: int, exit_paise: int, qty: int, dp_paise: int
) -> TradeResult:
    """A long delivery trade of `qty` shares bought at `entry_paise` and sold at `exit_paise`."""
    if qty < 1 or entry_paise <= 0 or exit_paise <= 0 or dp_paise < 0:
        raise ValueError(f"delivery: no result for qty {qty}, {entry_paise}, {exit_paise}")
    gross = (exit_paise - entry_paise) * qty
    charges = leg_paise(model, entry_paise, qty, "buy") + leg_paise(model, exit_paise, qty, "sell")
    net = gross - charges - dp_paise
    return TradeResult(gross, charges, dp_paise, net, net / (entry_paise * qty) * 100.0)
