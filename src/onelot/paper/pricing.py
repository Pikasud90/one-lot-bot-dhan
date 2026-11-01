"""Black-76 for the paper book's option legs (ADR-001 amendment 6). Pure: no I/O.

The option is the one the alert named: strike, expiry, CE/PE and IV are frozen at the alert,
so repricing moves only with the underlying and the clock. Time to expiry uses the Lab's own
convention (`lab.execution.year_frac` to `expiry_datetime`, 15:30 IST on the expiry date),
so a paper premium and a Lab premium for the same inputs agree. Any missing or invalid input
gives None, never a guess: the book then books PRICING_UNAVAILABLE.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from numbers import Real
from typing import Any, NamedTuple, cast

from ..analytics.blackscholes import OptionType, implied_vol
from ..analytics.blackscholes import price as b76_price
from ..lab.execution import expiry_datetime, year_frac

# Bounds for an implied IV (Manager decision h): outside them the quote is not believable.
IV_FLOOR = 0.01
IV_CEILING = 3.0


class OptionTerms(NamedTuple):
    """The frozen contract terms of the alerted option."""

    strike: float
    opt_type: OptionType
    expiry: dt.datetime  # 15:30 IST on the expiry date


def option_terms(option: Mapping[str, Any]) -> OptionTerms | None:
    """Strike, CE/PE and expiry from the alert's option dict, or None if any is unusable."""
    strike = _positive(option.get("strike"))
    opt_type = option.get("opt_type")
    raw_expiry = option.get("expiry")
    if strike is None or opt_type not in ("CE", "PE") or raw_expiry is None:
        return None
    try:
        expiry = dt.date.fromisoformat(str(raw_expiry)[:10])
    except ValueError:
        return None
    return OptionTerms(strike, cast(OptionType, opt_type), expiry_datetime(expiry))


def black76_premium(
    option: Mapping[str, Any], underlying: float, ts: dt.datetime, rate: float
) -> float | None:
    """Black-76 fair premium of the alerted option at `underlying` and `ts`, its IV frozen."""
    terms = option_terms(option)
    iv = _positive(option.get("iv"))
    spot = _positive(underlying)
    if terms is None or iv is None or spot is None or not _aware(ts):
        return None
    t = year_frac(ts, terms.expiry)
    fair = b76_price(spot, terms.strike, t, iv, rate, terms.opt_type)
    return fair if math.isfinite(fair) and fair > 0 else None


def implied_iv(
    option: Mapping[str, Any], premium: object, underlying: object, ts: dt.datetime, rate: float
) -> float | None:
    """The IV that prices `premium` at `underlying` and `ts`, inside [IV_FLOOR, IV_CEILING].

    None when the inputs are unusable or the solver lands on a bound (a quote below the
    1 % vol price or above 300 % is not believable): never a constant IV.
    """
    terms = option_terms(option)
    prem, spot = _positive(premium), _positive(underlying)
    if terms is None or prem is None or spot is None or not _aware(ts):
        return None
    t = year_frac(ts, terms.expiry)
    iv = implied_vol(prem, spot, terms.strike, t, rate, terms.opt_type, lo=IV_FLOOR, hi=IV_CEILING)
    if not math.isfinite(iv) or iv <= IV_FLOOR or iv >= IV_CEILING:
        return None
    return iv


def _positive(value: object) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, Real):
        return None
    f = float(value)
    return f if math.isfinite(f) and f > 0 else None


def _aware(ts: object) -> bool:
    return isinstance(ts, dt.datetime) and ts.tzinfo is not None and ts.utcoffset() is not None
