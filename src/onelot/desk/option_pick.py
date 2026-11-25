"""Which option contract an index alert names, and the fill-cost recorder watches (US-104).

A contract is TRADEABLE when the chain shows it trading now: a last price, a bid and an ask
above 0, the ask at or above the bid, open interest above 0, and a spread (ask - bid) / ask of
at most `max_spread_pct` per cent. A chain that reports open interest for no row of the side
(a source without that field) skips the open interest test for that pick; an open interest of
0 always means none. None, NaN, infinite and negative numbers make a row not tradeable.

Among the tradeable rows of the side the pick is the one whose |delta| is nearest the target;
with no delta on any of them, the nearest in-the-money strike (else the nearest strike). A
dead contract's stale last price makes the broker's delta nonsense, and such a delta can land
near the target by accident: on 25 Nov 2026 it named NIFTY 29200 PE with the index at 25,653.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

# a spread exactly at the cap is allowed: a hair of float rounding never turns it away
_CAP_SLACK = 1e-9


def _num(v: Any) -> float | None:
    """`v` as a finite float, else None (None, NaN, infinities, non-numbers)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _positive(v: Any) -> bool:
    f = _num(v)
    return f is not None and f > 0


def oi_reported(rows: Iterable[Any]) -> bool:
    """True when the chain reports open interest for at least one of `rows`."""
    return any(getattr(r, "oi", None) is not None for r in rows)


def tradeable(row: Any, max_spread_pct: float, need_oi: bool = True) -> bool:
    """Whether `row` can be traded now (the module docstring has the rule). Never raises."""
    try:
        if not (_positive(row.strike) and _positive(row.ltp)):
            return False
        bid, ask = _num(row.bid), _num(row.ask)
        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
            return False
        if need_oi and not _positive(row.oi):
            return False
        if row.delta is not None and _num(row.delta) is None:
            return False
        if row.iv is not None and (_num(row.iv) is None or row.iv < 0):
            return False
        return (ask - bid) * 100.0 / ask <= max_spread_pct + _CAP_SLACK
    except Exception:  # noqa: BLE001 - an odd row is not tradeable, never an error
        return False


def tradeable_rows(rows: Iterable[Any], opt_type: str, max_spread_pct: float) -> list[Any]:
    """The tradeable rows of one side (CE or PE), in the chain's own order."""
    side = [r for r in rows if getattr(r, "opt_type", None) == opt_type]
    need_oi = oi_reported(side)
    return [r for r in side if tradeable(r, max_spread_pct, need_oi)]


def choose(rows: Sequence[Any], opt_type: str, entry: float | None, target_delta: float):
    """Among `rows` (all tradeable), the |delta| nearest `target_delta`; with no delta on any,
    the nearest in-the-money strike to `entry`, else the nearest strike. None: no rows, or no
    delta and no usable `entry`."""
    if not rows:
        return None
    with_delta = [r for r in rows if r.delta is not None]
    if with_delta:
        return min(with_delta, key=lambda r: abs(abs(r.delta) - target_delta))
    spot = _num(entry)
    if spot is None:
        return None
    itm = [r for r in rows if (r.strike <= spot if opt_type == "CE" else r.strike >= spot)]
    return min(itm or rows, key=lambda r: abs(r.strike - spot))


def pick_contract(
    rows: Iterable[Any],
    opt_type: str,
    entry: float | None,
    target_delta: float,
    max_spread_pct: float,
):
    """The contract to name: `choose` over the tradeable rows of `opt_type`. None: nothing
    tradeable."""
    return choose(tradeable_rows(rows, opt_type, max_spread_pct), opt_type, entry, target_delta)
