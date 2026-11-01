"""The fill rule of the paper book (US-001 AC2/AC3; ADR-001 amendments 3, 5 and 6). Pure: no I/O.

A PENDING_FILL trade fills at the OPEN of the first bar that is

    open > created_at                     strictly later: no look-ahead
    open + resolution <= now              the bar has closed
    open <= deadline                      expires_at, + paper.feed_delay_minutes if delayed
    on created_at's IST session date      never a fill on a later day

and whose open is a usable price (finite, > 0). Everything is judged on DATA time: the trade
waits while the data has not reached the end of its window, however far the wall clock
moves, and closes NO_FILL only once a closed bar proves the window is over. A fill bar that
opens strictly beyond the stop is NO_FILL (GAP_THROUGH_STOP); an open exactly AT the stop
fills (Manager decision 4, 2026-11-01). One that opens AT or beyond the alert's target is
NO_FILL too (GAP_THROUGH_TARGET, US-002 decision 3): its target would be behind the entry.

Exits mirror the entry: `stock_exit_paise` slips a stock exit against the trader (a long
sells down, a short buys back up) like the Lab; an option sells at `option_exit_paise`.

Times are compared as int64 nanoseconds since the epoch (UTC), so a frame indexed in UTC or
IST gives the same answer. Prices become integer paise once (`to_paise`), then fills snap to
the tick against the trader: a long pays up, a short sells down.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from typing import Literal

import numpy as np

# See book.py: no pandas-stubs are pinned, so a new module names the missing stubs here.
import pandas as pd  # type: ignore[import-untyped]

from ..clock import IST
from ..costs import round_to_tick
from .money import to_decimal, to_paise

log = logging.getLogger(__name__)

# NSE cash and F&O close (config/config.yaml session.market_close). The PaperBook signature
# is frozen by the contract, so it cannot be injected there; the Engine may move it to PaperCfg.
SESSION_CLOSE_IST = dt.time(15, 30)

NS_PER_MINUTE = 60_000_000_000
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)

Outcome = Literal["fill", "wait", "no_fill"]
NoFillReason = Literal["gap_through_stop", "gap_through_target", "expired", "session_closed"]


@dataclass(frozen=True)
class Bars:
    """One series' bars, normalised once per step.

    `ns` holds bar-open times as int64 ns since the epoch (UTC), strictly increasing; `opens`
    the matching opens as float64, NaN where the open is not a usable price.
    """

    ns: np.ndarray
    opens: np.ndarray

    def __len__(self) -> int:
        return int(self.ns.size)


@dataclass(frozen=True)
class FillDecision:
    """What the fill rule says for one PENDING_FILL trade at `now`."""

    outcome: Outcome
    reason: NoFillReason | None = None  # set when outcome == "no_fill"
    bar_ts: dt.datetime | None = None  # the fill (or gapping) bar's open time, IST
    bar_open_paise: int | None = None
    # Set once the data proves no later bar can fill this trade: why the window is over.
    window_closed: Literal["expired", "session_closed"] | None = None
    data_through: dt.datetime | None = None  # close time of the last closed bar, IST


def to_ns(ts: dt.datetime) -> int:
    """An aware datetime -> int ns since the epoch (exact to the microsecond)."""
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{ts!r} must be timezone-aware")
    return (ts - _EPOCH) // dt.timedelta(microseconds=1) * 1000


def from_ns(ns: int) -> dt.datetime:
    """int ns since the epoch -> an IST datetime (to the microsecond)."""
    return (_EPOCH + dt.timedelta(microseconds=int(ns) // 1000)).astimezone(IST)


def normalise_bars(frame: object) -> Bars | None:
    """`bars(series_key)` output -> Bars, or None when nothing usable is there.

    None, an empty frame, a frame without an `open` column and a tz-naive (or mixed) index
    give None: a naive time is rejected, never guessed. Rows are sorted by time; for a
    duplicated time the LAST row wins (the feed's latest revision). Non-finite or
    non-positive opens become NaN so they can never be a fill price. The caller's frame is
    never modified.
    """
    if not isinstance(frame, pd.DataFrame) or frame.empty or "open" not in frame.columns:
        return None
    index = frame.index
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        log.warning("paper: ignoring bars without a tz-aware DatetimeIndex (%s)", type(index))
        return None
    ns = np.asarray(index.as_unit("ns").asi8, dtype=np.int64)
    opens = pd.to_numeric(frame["open"], errors="coerce").to_numpy(dtype=np.float64, copy=True)
    opens[~(np.isfinite(opens) & (opens > 0.0))] = np.nan
    if ns.size > 1 and not bool(np.all(np.diff(ns) > 0)):
        order = np.argsort(ns, kind="stable")
        ns, opens = ns[order], opens[order]
        last_of_run = np.append(ns[1:] != ns[:-1], True)  # keep the last row of each time
        ns, opens = ns[last_of_run], opens[last_of_run]
    return Bars(ns=ns, opens=opens)


def infer_resolution_min(bars: Bars) -> int:
    """The most common positive bar spacing in whole minutes (1 when it cannot be told)."""
    if len(bars) < 2:
        return 1
    steps = np.diff(bars.ns) // NS_PER_MINUTE
    steps = steps[steps > 0]
    if steps.size == 0:
        return 1
    values, counts = np.unique(steps, return_counts=True)
    return int(values[int(np.argmax(counts))])


def spacing_gaps(
    bars: Bars, *, base_min: int, session_date: dt.date, now: dt.datetime
) -> tuple[dt.datetime, ...]:
    """The bar opens around the first run of TWO OR MORE consecutive gaps larger than
    `base_min`, among the bars of `session_date` (IST) that have closed by `now`; () if none.

    That run is a feed that changed resolution (Manager decision 6). One hole is an outage,
    and the overnight gap never counts because only one date is looked at.
    """
    base_ns = max(int(base_min), 1) * NS_PER_MINUTE
    day_ns = to_ns(dt.datetime.combine(session_date, dt.time(0), tzinfo=IST))
    lo = int(np.searchsorted(bars.ns, day_ns, side="left"))
    hi = min(
        int(np.searchsorted(bars.ns, day_ns + 24 * 60 * NS_PER_MINUTE, side="left")),
        int(np.searchsorted(bars.ns, to_ns(now) - base_ns, side="right")),
    )
    day = bars.ns[lo:hi]
    wide = np.diff(day) > base_ns  # wide[i]: the gap between day[i] and day[i + 1]
    pairs = np.flatnonzero(wide[:-1] & wide[1:])
    if pairs.size == 0:
        return ()
    start = end = int(pairs[0])
    while end + 1 < wide.size and wide[end + 1]:
        end += 1
    return tuple(from_ns(int(ns)) for ns in day[start : end + 2])


def mixed_spacing(bars: Bars, *, base_min: int, session_date: dt.date, now: dt.datetime) -> bool:
    """True when the session's closed bars mix resolutions (see `spacing_gaps`)."""
    return bool(spacing_gaps(bars, base_min=base_min, session_date=session_date, now=now))


def decide_fill(
    bars: Bars,
    *,
    created_at: dt.datetime,
    deadline: dt.datetime,
    resolution_min: int,
    now: dt.datetime,
    direction: str,
    stop_paise: int,
    target_paise: int | None = None,
) -> FillDecision:
    """Apply the fill rule to one trade. Uses only bars that have closed by `now`."""
    res_ns = max(int(resolution_min), 1) * NS_PER_MINUTE
    created_ist = created_at.astimezone(IST)
    day = created_ist.date()
    midnight_ns = to_ns(dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), tzinfo=IST))
    close_at = dt.datetime.combine(day, SESSION_CLOSE_IST, tzinfo=IST)
    # An alert raised after the close (a special session) is bounded by the date alone.
    session_end_ns = to_ns(close_at) if created_ist < close_at else midnight_ns
    deadline_ns = to_ns(deadline)

    ns = bars.ns
    n_closed = int(np.searchsorted(ns, to_ns(now) - res_ns, side="right"))
    if n_closed == 0:
        return FillDecision("wait")
    last_ns = int(ns[n_closed - 1])
    data_through = from_ns(last_ns + res_ns)
    window_closed: Literal["expired", "session_closed"] | None = None
    if (
        last_ns >= min(deadline_ns, midnight_ns)  # a bar at/after the deadline or a later date
        or last_ns + res_ns >= session_end_ns  # the data has reached the session close
    ):  # whichever bound came first names the reason
        window_closed = "expired" if deadline_ns < session_end_ns else "session_closed"
    lo = int(np.searchsorted(ns, to_ns(created_at), side="right"))
    hi = min(
        int(np.searchsorted(ns, deadline_ns, side="right")),
        # Strictly before the session end: a bar opening AT 15:30 (a closing print) never
        # fills. session_end_ns is midnight for an alert raised after the close.
        int(np.searchsorted(ns, session_end_ns, side="left")),
        n_closed,
    )
    for i in range(lo, hi):
        if np.isnan(bars.opens[i]):
            continue  # an unusable open is never a fill price
        open_paise = to_paise(float(bars.opens[i]))
        if open_paise <= 0:
            continue
        bar_ts = from_ns(int(ns[i]))
        reason = _gap_reason(direction, open_paise, stop_paise, target_paise)
        return FillDecision(
            "fill" if reason is None else "no_fill",
            reason=reason,
            bar_ts=bar_ts,
            bar_open_paise=open_paise,
            window_closed=window_closed,
            data_through=data_through,
        )
    if window_closed is None:
        return FillDecision("wait", data_through=data_through)
    return FillDecision(
        "no_fill", reason=window_closed, window_closed=window_closed, data_through=data_through
    )


def _gap_reason(
    direction: str, open_paise: int, stop_paise: int, target_paise: int | None
) -> NoFillReason | None:
    """Why a fill bar's open cannot be the entry: strictly beyond the stop (an open AT the
    stop fills, Manager decision 4), or AT or beyond the alert target (US-002 decision 3:
    the 'target' would sit on the wrong side of the entry). None when it fills."""
    sign = 1 if direction == "long" else -1
    if (open_paise - stop_paise) * sign < 0:
        return "gap_through_stop"
    if target_paise is not None and (open_paise - target_paise) * sign >= 0:
        return "gap_through_target"
    return None


def stock_entry_paise(open_paise: int, direction: str, slippage_pct: float, tick_paise: int) -> int:
    """open x (1 +- slippage_pct / 100), snapped to the tick against the trader, exactly.

    `slippage_pct` is a PERCENT (0.03 means 0.03 %). A long rounds up, a short down.
    """
    tick = max(int(tick_paise), 1)
    sign = 1 if direction == "long" else -1
    with localcontext() as ctx:
        ctx.prec = 50
        factor = Decimal(1) + sign * to_decimal(slippage_pct) / Decimal(100)
        ticks = Decimal(int(open_paise)) * factor / Decimal(tick)
        n = ticks.to_integral_value(rounding=ROUND_CEILING if sign > 0 else ROUND_FLOOR)
    return int(n) * tick


def stock_exit_paise(level_paise: int, direction: str, slippage_pct: float, tick_paise: int) -> int:
    """A stock exit at the rule `level_paise`, slipped by `slippage_pct` (a PERCENT) against
    the trader and snapped to the tick against the trader, exactly: a long SELLS, so the
    level falls and floors; a short BUYS BACK, so it rises and ceils. The mirror of
    `stock_entry_paise`, and what `lab.simulator` books for a stop, a gap, an exit at an open
    or the end of day (Manager decision 2). A target is a limit fill: never call this for it.
    """
    covering = "short" if direction == "long" else "long"  # the side of the exit's trade
    return stock_entry_paise(level_paise, covering, slippage_pct, tick_paise)


def option_entry_paise(fair: float, spread_frac: float, slippage_ticks: int, tick: float) -> int:
    """The premium `lab.execution.execute_trade` pays: ceil_tick(fair*(1+spread) + ticks*tick).

    `spread_frac` is a FRACTION (0.006 means 0.6 %). Mirrors the Lab to the paise on purpose
    (it is the differential oracle), so it uses the Lab's own `round_to_tick`.
    """
    return to_paise(round_to_tick(fair * (1 + spread_frac) + slippage_ticks * tick, tick, "up"))


def option_exit_paise(fair: float, spread_frac: float, slippage_ticks: int, tick: float) -> int:
    """The premium `execute_trade` receives when it SELLS the option back:
    floor_tick(fair*(1-spread) - ticks*tick), never below one tick (the Lab's own floor).

    Used for the stop leg of option risk, so paper R and Lab R are comparable (Manager
    decision (a), US-001).
    """
    sold = round_to_tick(fair * (1 - spread_frac) - slippage_ticks * tick, tick, "down")
    return to_paise(max(sold, tick))
