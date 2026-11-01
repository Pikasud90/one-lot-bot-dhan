"""Rupees -> integer paise, once, at the boundary (ADR-001 amendment 5).

Bar prices and alert levels arrive as floats (or numpy floats). They are converted
here exactly once with Decimal and ROUND_HALF_EVEN; everything downstream in the
paper ledger is a plain Python `int` of paise, so no float sum ever reaches a row.
The way back out, for display only, is `format_rupees` (US-030): exact, never a float.
"""

from __future__ import annotations

import math
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from numbers import Integral, Real

PAISE_PER_RUPEE = 100


def to_decimal(value: object) -> Decimal:
    """A finite Decimal of `value` (rupees). Floats go through their shortest repr.

    Raises ValueError for None, bool, NaN, infinities and anything non-numeric.
    """
    if value is None or isinstance(value, bool):
        raise ValueError(f"not a price: {value!r}")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, Integral):  # int and numpy integer scalars
        d = Decimal(int(value))
    elif isinstance(value, str):
        try:
            d = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError(f"not a price: {value!r}") from exc
    elif isinstance(value, Real):  # float and numpy floating scalars
        f = float(value)
        if not math.isfinite(f):
            raise ValueError(f"not a finite price: {value!r}")
        d = Decimal(repr(f))
    else:
        raise ValueError(f"not a price: {value!r}")
    if not d.is_finite():
        raise ValueError(f"not a finite price: {value!r}")
    return d


def to_paise(value: object) -> int:
    """Rupees -> integer paise, ROUND_HALF_EVEN. `1000.3` -> `100030`."""
    paise = (to_decimal(value) * PAISE_PER_RUPEE).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    return int(paise)


def format_rupees(paise: int | None) -> str | None:
    """Integer paise -> a plain rupee string with exactly two decimals, for display.

    Exact (Decimal, no rounding, never a float, never scientific notation):
    `100030` -> `"1000.30"`, `-1205` -> `"-12.05"`, `-5` -> `"-0.05"`. None stays None, so
    a not-yet-computed amount reaches JSON as null. Anything that is not integer paise (a
    bool, a float, a string, a Decimal) is a caller bug and raises TypeError.
    """
    if paise is None:
        return None
    if isinstance(paise, bool) or not isinstance(paise, Integral):  # np.bool_ is not Integral
        raise TypeError(f"not integer paise: {paise!r}")
    n = int(paise)
    # Built from its digits with exponent -2: exact at any size, whatever the Decimal
    # context's precision (scaleb or a division would round beyond 28 digits).
    rupees = Decimal((int(n < 0), tuple(int(d) for d in str(abs(n))), -2))
    return format(rupees, "f")
