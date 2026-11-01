"""Pure position-management helpers (US-002 AC8, ADR-001 amendment 1). No I/O, no clock.

The paper book (`onelot.paper.manage.manage_step`) uses them on closed bars today; the desk
adopts them in US-025 behind characterisation tests. They are deliberately small: the
desk advises from one live price while the paper book works on whole bars, so only the
arithmetic and the vocabulary are shared, never the bar logic.

Prices are unit-agnostic (rupees for the desk, integer paise for the paper book) but
every price argument of one call must share a unit, `tick` included. Arithmetic runs in
Decimal on the shortest repr of each input, so a float that prints as 998.8 is 998.8,
and a result that sits on a tick boundary is never floored a whole tick away. This module
imports nothing from `onelot.paper` (the desk never imports the paper package).
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from numbers import Integral, Real
from typing import cast

from ..clock import IST

# Close-reason precedence (ADR-001 amendment 7): the first listed wins when several are
# due at once. TRAIL_STOP is a STOP whose level has moved; it ranks with STOP.
EXIT_PRECEDENCE: tuple[str, ...] = (
    "STOP",
    "TRAIL_STOP",
    "TARGET",
    "SESSION_END",
    "SIGNAL_EXIT",
    "OPPOSITE_SIGNAL",
    "TIME_STOP",
)
_RANK = {reason: rank for rank, reason in enumerate(EXIT_PRECEDENCE)}
_PREC = 50  # Decimal digits: far beyond any price x multiplier this desk sees


def ratchet_stop(
    direction: str,
    stop: float,
    last_close: float,
    atr: float | None,
    mult: float,
    *,
    tick: float | None = None,
) -> float:
    """The trailing stop after one bar close: `last_close -+ mult x atr`, never loosened.

    A long's stop only rises, a short's only falls. With `tick`, the candidate is snapped
    to the tick grid AGAINST the trader first (a long's stop down, a short's up), so a
    trailed stop is always a price the exchange accepts. An unusable `atr` or `mult`
    (None, 0, negative, NaN or infinite) disables the trail: `stop` comes back unchanged,
    with no fallback to any other ATR. A non-finite `last_close` raises ValueError.
    """
    sign = _sign(direction)
    if not _usable(atr) or not _usable(mult):
        return float(stop)
    if not _finite(last_close):
        raise ValueError(f"ratchet_stop: last_close={last_close!r} is not a finite price")
    with localcontext() as ctx:
        ctx.prec = _PREC
        current = _dec(stop)
        candidate = _dec(last_close) - sign * _dec(mult) * _dec(atr)
        if tick is not None:
            step = _dec(tick)
            if step <= 0:
                raise ValueError(f"ratchet_stop: tick={tick!r} must be positive")
            rounding = ROUND_FLOOR if sign > 0 else ROUND_CEILING
            candidate = (candidate / step).to_integral_value(rounding=rounding) * step
        moved = candidate > current if sign > 0 else candidate < current
        return float(candidate if moved else current)


def r_multiple(direction: str, entry: float, initial_stop: float, price: float) -> float:
    """How many initial risks `price` is in profit (negative in loss), computed exactly.

    Risk is |entry - initial_stop|. A zero risk has no R (ValueError), and so does a stop
    on the wrong side of the entry (a long's stop above it): both are a sign error upstream,
    never a number to trail on.
    """
    sign = _sign(direction)
    with localcontext() as ctx:
        ctx.prec = _PREC
        e, s, p = (_dec(v) for v in (entry, initial_stop, price))
        risk = (e - s) * sign
        if risk < 0:
            raise ValueError(
                f"r_multiple: {direction} initial_stop={initial_stop!r} is on the wrong side "
                f"of entry={entry!r}"
            )
        if risk == 0:
            raise ValueError(f"r_multiple: zero risk (entry == initial_stop == {entry!r})")
        return float(sign * (p - e) / risk)


def bars_held(opened_at: dt.datetime, now: dt.datetime, tf_min: int) -> int:
    """Whole `tf_min`-minute bars between `opened_at` and `now` (0 before it opened)."""
    _require_aware(opened_at, "opened_at")
    _require_aware(now, "now")
    if int(tf_min) <= 0:
        raise ValueError(f"bars_held: tf_min={tf_min!r} must be a positive number of minutes")
    held = (now - opened_at) // dt.timedelta(minutes=int(tf_min))
    return max(int(held), 0)


def sessions_held(session_dates: Iterable[dt.date]) -> int:
    """How many distinct session dates the position has seen (any iterable of dates)."""
    return len(set(session_dates))


def session_due(now: dt.datetime, exit_time: dt.time, session_date: dt.date) -> bool:
    """True once `now` is at or past `exit_time` IST on `session_date`, or any later day.

    Date-aware on purpose: the desk's `now.time() >= exit_time` forgets a position on the
    next morning; this does not (US-002 AC5).
    """
    _require_aware(now, "now")
    due_at = dt.datetime.combine(session_date, exit_time, tzinfo=IST)
    return now >= due_at


def exit_priority(reasons: Iterable[str]) -> str:
    """The reason that wins among `reasons` due together (see EXIT_PRECEDENCE).

    An empty set raises ValueError (there is nothing to close for), and so does any reason
    outside EXIT_PRECEDENCE: a typo must never silently lose to a real exit.
    """
    best: str | None = None
    for reason in reasons:
        if reason not in _RANK:
            raise ValueError(f"exit_priority: unknown exit reason {reason!r}")
        if best is None or _RANK[reason] < _RANK[best]:
            best = reason
    if best is None:
        raise ValueError("exit_priority: no exit reason given")
    return best


# ── internals ─────────────────────────────────────────────────────────────────


def _sign(direction: str) -> int:
    if direction == "long":
        return 1
    if direction == "short":
        return -1
    raise ValueError(f"direction={direction!r} is not 'long' or 'short'")


def _dec(value: object) -> Decimal:
    """Decimal of a finite number: ints and Decimals exactly, floats via their shortest repr."""
    if isinstance(value, Decimal) and value.is_finite():
        return value
    if not _finite(value):
        raise ValueError(f"not a finite number: {value!r}")
    if isinstance(value, Integral):
        return Decimal(int(value))
    return Decimal(repr(float(cast(Real, value))))


def _finite(value: object) -> bool:
    if isinstance(value, Decimal):
        return value.is_finite()
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def _usable(value: object) -> bool:
    """A finite, strictly positive number (ATR and multiplier)."""
    return _finite(value) and _dec(value) > 0


def _require_aware(ts: dt.datetime, name: str) -> None:
    if not isinstance(ts, dt.datetime) or ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{name}={ts!r} must be a timezone-aware datetime")
