"""The charges of a closed paper trade, in integer paise (US-023 AC1; ADR-001 amendment 5).

Pure: no I/O. Each leg is priced by the cost table's own `CostModel.leg` on its REAL side and
rounded ONCE to paise, ROUND_HALF_UP; the trade's charges are the sum of its two legs, so a
half paisa is never rounded twice and never lost in a float sum.

Sides: a stock long is BUY then SELL; a stock SHORT is SELL then BUY, so it pays STT on its
ENTRY leg and stamp duty on its exit (`round_trip_cost` assumes buy-then-sell and would get a
short wrong); an index option is long premium (BUY then SELL) whichever way the underlying
points. Prices go to the model in rupees as floats (its own unit); its total comes back as a
float and is read through its shortest repr, like every other boundary value
(`money.to_decimal`).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from ..costs import CostModel
from .money import PAISE_PER_RUPEE, to_decimal

Side = Literal["buy", "sell"]

_LONG_THEN_SHORT: tuple[Side, Side] = ("buy", "sell")
_SHORT_THEN_COVER: tuple[Side, Side] = ("sell", "buy")


def leg_sides(kind: str, direction: str) -> tuple[Side, Side]:
    """(entry side, exit side) of a paper trade. Raises ValueError for an unknown kind or
    direction: a side is never guessed, because a wrong one moves STT and stamp duty."""
    if direction not in ("long", "short"):
        raise ValueError(f"paper: no cost sides for direction {direction!r}")
    if kind == "INDEX":  # one lot of a bought CE or PE: long premium either way
        return _LONG_THEN_SHORT
    if kind == "STOCK":
        return _LONG_THEN_SHORT if direction == "long" else _SHORT_THEN_COVER
    raise ValueError(f"paper: no cost sides for kind {kind!r}")


def leg_paise(model: CostModel, price_paise: int, qty: int, side: Side) -> int:
    """round_half_up(model.leg(price_rupees, qty, side).total x 100): one leg, in paise.

    Raises ValueError when qty is below one or the model's total is not a finite, non-negative
    number (a broken cost table is reported, never booked as a charge).
    """
    if int(qty) < 1:
        raise ValueError(f"paper: a cost leg needs qty >= 1, got {qty!r}")
    total = model.leg(int(price_paise) / PAISE_PER_RUPEE, int(qty), side).total
    if isinstance(total, str):  # to_decimal would parse it: a model never answers text
        raise ValueError(f"paper: cost leg total {total!r} is not a number")
    try:
        rupees = to_decimal(total)
    except ValueError as exc:
        raise ValueError(f"paper: cost leg total {total!r} is not a finite number") from exc
    if rupees < 0:
        raise ValueError(f"paper: cost leg total {total!r} is negative")
    return int((rupees * PAISE_PER_RUPEE).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def round_trip_paise(
    model: CostModel, kind: str, direction: str, entry_paise: int, exit_paise: int, qty: int
) -> int:
    """The entry leg plus the exit leg, each on its real side and rounded once."""
    entry_side, exit_side = leg_sides(kind, direction)
    return leg_paise(model, entry_paise, qty, entry_side) + leg_paise(
        model, exit_paise, qty, exit_side
    )
