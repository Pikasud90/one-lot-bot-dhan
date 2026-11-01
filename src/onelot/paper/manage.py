"""Bar-by-bar management of one OPEN paper trade (US-002; ADR-001 amendments 1, 2, 5, 7).

Pure: no I/O and no clock. `manage_step(state, bars, rules, now)` walks the bars that have
closed by `now` and that the position has not seen yet (from the fill bar inclusive) and
returns a NEW state plus the events to book; the input state is never mutated.

Every level lives on the MANAGED series: the stock itself, or the index under an option
(a BUY_PE is direction "short" on the underlying, with its stop above the entry). Levels
are integer paise on the tick grid; bar prices become paise once, here, with `to_paise`.
`PaperClose.price_paise` is the level the rules close at on that series, BEFORE any exit
slippage or option repricing: the book turns it into the trade's exit_paise.

Rules for one bar, in time order (ported from `lab.simulator.simulate`, never called):
  at the open  an exit that fell due before the bar opened closes at the open: a strategy
               exit whose signal bar closed at or before this open (AC4; Manager decision 1,
               chronological: nothing inside the bar can pre-empt it). So does the stop when
               the open is at or through it (`gap` when strictly through, like the fill
               rule's GAP_THROUGH_STOP), and the target when the open is through it by at
               least one tick (at exactly the target, gap=True). Precedence
               (`desk.rules.exit_priority`) picks among the reasons due at the same open.
  inside       the stop on a touch (a long's low at or below it), at the stop; the target
               only when traded THROUGH by one tick, at exactly the target. STOP beats
               TARGET: which came first inside the bar is unknown, so the adverse one wins.
  at the close when this fine bar closes a STRATEGY bar, the trail ratchets from that close
               once the close is `trail_activate_r` in profit (AC3); the moved stop acts
               from the NEXT bar and is booked as a STOP_MOVED event stamped with this
               bar's close. Strategy bars are `tf_min` wide from 09:15 IST on the bar's date,
               as `data.resample.resample_bars` builds them; a fine bar closes one when its
               close reaches the end of the strategy bar its open falls in (review round 1,
               MAJOR-1: the Lab trails once per strategy bar, never per fine bar).
A strategy exit reported before its exit bar has closed waits in `PositionState.pending_exit`
(the earliest one wins; on a tie, the higher precedence), so a caller that reports it once
still gets it. The session end (the first open at or after `exit_time` on the opening date)
and the time stop (the first open once `max_bars` strategy bars have passed) are due at an
open in the same way (AC5).

No position outlives its session (until US-027): a bar of a later date is never managed,
and once the session is over (`now` at or past 15:30 IST on the opening date) a position
with no bar at or after `exit_time` closes SESSION_END at the close of the last bar it
managed, stamped with that bar's close time (Manager decisions A5 and T2). A position that
never saw a usable bar is logged and left open: no price is invented.
Deliberate differences from the Lab: a target needs a trade-through and fills at exactly T
(the Lab fills on a touch, at the open on a gap); no partial exits; the trail activates on
the strategy-bar close, not on the best price so far; the trail's ATR is FROZEN at the
alert (the strategy's ATR(trail_atr_period), `manage.atr_trail`) while the Lab re-reads its
evolving ATR at every strategy bar; a trailed stop snaps to the tick against the trader;
a strategy bar whose last fine bar is missing from the feed does not move the trail; no
forced end-of-day close in swing (US-027).
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass, replace
from numbers import Integral, Real
from typing import Any, Literal, NamedTuple, cast, get_args

import numpy as np

# See book.py: no pandas-stubs are pinned, so a new module names the missing stubs here.
import pandas as pd  # type: ignore[import-untyped]

from ..clock import IST
from ..desk.rules import (
    EXIT_PRECEDENCE,
    bars_held,
    exit_priority,
    r_multiple,
    ratchet_stop,
    session_due,
    sessions_held,
)
from .events import PaperEvent
from .fill import (
    NS_PER_MINUTE,
    SESSION_CLOSE_IST,
    Bars,
    from_ns,
    infer_resolution_min,
    to_ns,
)
from .money import PAISE_PER_RUPEE, to_decimal, to_paise

log = logging.getLogger(__name__)

CloseReason = Literal[
    "STOP",
    "TRAIL_STOP",
    "TARGET",
    "SIGNAL_EXIT",
    "OPPOSITE_SIGNAL",
    "SESSION_END",
    "TIME_STOP",
    "NO_FILL",
    "GAP_THROUGH_STOP",
    "GAP_THROUGH_TARGET",
]
StrategyExitReason = Literal["SIGNAL_EXIT", "OPPOSITE_SIGNAL"]
Direction = Literal["long", "short"]
Kind = Literal["STOCK", "INDEX"]

CLOSE_REASONS: frozenset[str] = frozenset(get_args(CloseReason))
STRATEGY_EXIT_REASONS: frozenset[str] = frozenset(get_args(StrategyExitReason))
CLOSED = "CLOSED"  # the event kind of a close
STOP_MOVED = "STOP_MOVED"  # the event kind of a trail that tightened the stop
PRICE_COLUMNS = ("open", "high", "low", "close")
# Strategy bars start at the NSE session open, as `data.resample.resample_bars` anchors them
# (origin start_day, offset 09:15, closed and labelled left).
STRATEGY_BAR_ANCHOR = dt.time(9, 15)


@dataclass(frozen=True)
class StrategyExit:
    """The strategy reported an exit (or a reversal) on its closed bar ending at
    `bar_close_ts`. The trade closes at the first fine-bar open at or after that time."""

    reason: StrategyExitReason
    bar_close_ts: dt.datetime

    def __post_init__(self) -> None:
        if self.reason not in STRATEGY_EXIT_REASONS:
            raise ValueError(
                f"StrategyExit: reason={self.reason!r} is not one of "
                f"{sorted(STRATEGY_EXIT_REASONS)}"
            )
        _require_aware(self.bar_close_ts, "StrategyExit.bar_close_ts")


@dataclass(frozen=True)
class PaperClose:
    """How a position closed: the reason, the level on the managed series, when, and
    whether the bar opened beyond the level (a gap)."""

    reason: CloseReason
    price_paise: int
    at: dt.datetime  # the open time of the bar the close happened in
    gap: bool


@dataclass
class PositionState:
    """One OPEN position as the rules see it. Every `*_paise` level is integer paise on the
    managed series (the underlying for an option); `entry_paise` is that series' entry
    (a stock's slipped fill, an index's fill-bar open).

    `last_bar_ts` is the open time of the last bar managed (None until the first);
    `closed` is set once, by the bar that closed the position. `trade_id` is the
    paper_trades id the events are booked under (0 in pure unit tests). `trail_active` is
    True once the trail has moved the stop beyond the initial one. `pending_exit` is a
    strategy exit reported before its exit bar closed (the book persists it with the state).
    `last_close_paise` is the close of the last usable bar managed and `last_close_at` that
    bar's close time: the session-end fallback price and stamp (AC5).
    """

    direction: Direction
    kind: Kind
    qty: int
    entry_paise: int
    initial_stop_paise: int
    stop_paise: int
    target_paise: int | None
    trail_active: bool
    opened_at: dt.datetime
    last_bar_ts: dt.datetime | None
    bars_held: int
    sessions_held: int
    option: dict[str, Any] | None
    trade_id: int = 0
    closed: PaperClose | None = None
    pending_exit: StrategyExit | None = None
    last_close_paise: int | None = None
    last_close_at: dt.datetime | None = None


@dataclass(frozen=True)
class ManageRules:
    """The strategy's exit rules, frozen at the alert.

    `atr` is in RUPEES, exactly as the alert recorded it (manage.atr); None, 0 or NaN
    disables the trail, with no fallback to a live ATR. `tick_paise` is the instrument's
    tick in paise. `resolution_min` is the fine-bar spacing; None infers it from the bars
    (the book always passes the trade's own).
    """

    target_r: float
    trail_atr_mult: float
    trail_activate_r: float
    atr: float | None
    max_bars: int
    tf_min: int
    exit_time: dt.time
    tick_paise: int
    resolution_min: int | None = None


@dataclass(frozen=True)
class OhlcBars:
    """One series' bars normalised once: int64 ns open times (strictly increasing) and the
    feed's float64 rupee prices, unchanged; `usable[i]` is False for a row that cannot be
    judged at all."""

    ns: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    usable: np.ndarray

    def __len__(self) -> int:
        return int(self.ns.size)


class BarPaise(NamedTuple):
    """One bar's prices in paise: exactly what a booked event says it used."""

    open: int
    high: int
    low: int
    close: int

    def as_detail(self) -> dict[str, int]:
        """The flat event-detail keys of the bar (Manager decision A1, 2026-11-01)."""
        return {
            "bar_open_paise": self.open,
            "bar_high_paise": self.high,
            "bar_low_paise": self.low,
            "bar_close_paise": self.close,
        }


def normalise_ohlc(frame: object) -> OhlcBars:
    """A bars frame -> OhlcBars. The caller's frame is never modified.

    Raises ValueError for anything but a DataFrame with a tz-aware DatetimeIndex (bar OPEN
    times) and open/high/low/close columns: a naive time is rejected, never guessed. Rows
    are sorted by time; for a duplicated time the LAST row wins (the feed's latest
    revision). A row with a non-finite or non-positive price, or a high below its low, is
    marked unusable (skipped and logged, never guessed). An open outside high/low (some
    feeds print the auction open apart from the continuous range) is kept: the rules judge
    the open on its own, before the bar's range, so it can still gap a stop or a target.
    Touches inside the bar come from high/low only; the close is used for the trail alone.
    """
    if not isinstance(frame, pd.DataFrame):
        raise ValueError(f"bars must be a pandas DataFrame, not {type(frame).__name__}")
    missing = [c for c in PRICE_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"bars: missing price column(s) {missing}")
    index = frame.index
    if not isinstance(index, pd.DatetimeIndex) or index.tz is None:
        raise ValueError("bars must have a tz-aware DatetimeIndex of bar OPEN times")
    ns = np.asarray(index.as_unit("ns").asi8, dtype=np.int64)
    cols = [
        pd.to_numeric(frame[c], errors="coerce").to_numpy(dtype=np.float64, copy=True)
        for c in PRICE_COLUMNS
    ]
    if ns.size > 1 and not bool(np.all(np.diff(ns) > 0)):
        order = np.argsort(ns, kind="stable")
        ns, cols = ns[order], [c[order] for c in cols]
        last_of_run = np.append(ns[1:] != ns[:-1], True)
        ns, cols = ns[last_of_run], [c[last_of_run] for c in cols]
    o, h, l, c = cols
    with np.errstate(invalid="ignore"):
        usable = (
            np.isfinite(o)
            & np.isfinite(h)
            & np.isfinite(l)
            & np.isfinite(c)
            & (o > 0.0)
            & (l > 0.0)
            & (c > 0.0)
            & (h >= l)
        )
    return OhlcBars(ns=ns, open=o, high=h, low=l, close=c, usable=usable)


def manage_step(
    state: PositionState,
    bars: pd.DataFrame,
    rules: ManageRules,
    now: dt.datetime,
    strategy_exit: StrategyExit | None = None,
) -> tuple[PositionState, list[PaperEvent]]:
    """Manage `state` over the bars of its series that have closed by `now`.

    A bar is eligible iff it opens at or after the fill bar (or after `last_bar_ts`), on the
    opening date, and its open plus the resolution is not later than `now`. Stepping again
    with the same (now, bars) books nothing. `strategy_exit` reports the strategy's exit
    seen on a bar that closed by `now`; one whose bar closed at or before `opened_at` is
    stale and ignored. Raises ValueError for a naive `now`, a malformed frame, a strategy
    exit from after `now` (look-ahead), malformed rules or an inconsistent state (float
    paise, a stop on the wrong side of the entry, a stop looser than the initial one).
    """
    _check_call(state, rules, now, strategy_exit)
    if state.closed is not None:
        return state, []
    return _manage(state, normalise_ohlc(bars), rules, now.astimezone(IST), strategy_exit)


def manage_view(
    state: PositionState,
    view: OhlcBars,
    rules: ManageRules,
    now: dt.datetime,
    strategy_exit: StrategyExit | None = None,
) -> tuple[PositionState, list[PaperEvent]]:
    """`manage_step` over bars already normalised by `normalise_ohlc`: the book normalises
    each series once per step and manages every trade on it from that one view (AC8)."""
    _check_call(state, rules, now, strategy_exit)
    if state.closed is not None:
        return state, []
    return _manage(state, view, rules, now.astimezone(IST), strategy_exit)


def _manage(
    state: PositionState,
    view: OhlcBars,
    rules: ManageRules,
    now: dt.datetime,
    strategy_exit: StrategyExit | None,
) -> tuple[PositionState, list[PaperEvent]]:
    pending = _earliest_exit(state, strategy_exit)
    res_min = rules.resolution_min or infer_resolution_min(Bars(ns=view.ns, opens=view.open))
    res = dt.timedelta(minutes=res_min)
    session_date = state.opened_at.astimezone(IST).date()
    next_midnight = dt.datetime.combine(session_date + dt.timedelta(days=1), dt.time(0), IST)
    if state.last_bar_ts is None:
        lo = int(np.searchsorted(view.ns, to_ns(state.opened_at), side="left"))
    else:
        lo = int(np.searchsorted(view.ns, to_ns(state.last_bar_ts), side="right"))
    hi = min(  # closed by now, and never a bar of a later date (no overnight carry)
        int(np.searchsorted(view.ns, to_ns(now) - res_min * NS_PER_MINUTE, side="right")),
        int(np.searchsorted(view.ns, to_ns(next_midnight), side="left")),
    )

    sign = 1 if state.direction == "long" else -1
    trail_atr = _trail_atr_paise(rules)
    stop, trail_active = state.stop_paise, state.trail_active
    last_close, last_close_at = state.last_close_paise, state.last_close_at
    last_ts = state.last_bar_ts
    seen_date = None if last_ts is None else last_ts.astimezone(IST).date()
    new_dates: list[dt.date] = []
    events: list[PaperEvent] = []
    closed: PaperClose | None = None
    for i in range(lo, hi):
        bar_ts = from_ns(int(view.ns[i]))
        last_ts = bar_ts
        if bar_ts.date() != seen_date:
            seen_date = bar_ts.date()
            new_dates.append(seen_date)
        if not bool(view.usable[i]):
            log.warning("paper: trade %s skips unusable bar %s", state.trade_id, bar_ts)
            continue
        bar = BarPaise(
            *(to_paise(float(col[i])) for col in (view.open, view.high, view.low, view.close))
        )
        due_at_open = _exits_due_at_open(bar_ts, pending, state.opened_at, session_date, rules)
        closed = _close_on_bar(sign, bar, bar_ts, stop, state, rules.tick_paise, due_at_open)
        if closed is not None:
            events.append(_closed_event(state, closed, bar, stop, res_min))
            break
        last_close, last_close_at = bar.close, bar_ts + res
        if trail_atr is not None and closes_strategy_bar(bar_ts, res, rules.tf_min):
            trailed = _trailed_stop(state, stop, bar.close, trail_atr, rules)
            if trailed != stop:  # the strategy bar's close moved it: from the next bar on
                events.append(_stop_moved_event(state, stop, trailed, bar, bar_ts, bar_ts + res))
                stop, trail_active = trailed, True

    session_over = now >= dt.datetime.combine(session_date, SESSION_CLOSE_IST, IST)
    if closed is None and session_over:
        if last_close is None or last_close_at is None:
            log.warning(
                "paper: trade %s: the session of %s is over but no usable bar was managed; "
                "left open (no price is invented)",
                state.trade_id,
                session_date,
            )
        else:  # no bar at or after exit_time: the last close the position saw (A5)
            closed = PaperClose("SESSION_END", last_close, last_close_at, gap=False)
            events.append(_session_over_event(state, closed, stop, res_min))
    if last_ts is None or (hi <= lo and closed is None):
        return replace(state, pending_exit=pending), []  # nothing new: only remember an exit

    new_state = replace(
        state,
        stop_paise=stop,
        trail_active=trail_active,
        last_bar_ts=last_ts,
        bars_held=bars_held(state.opened_at, last_ts + res, rules.tf_min),
        sessions_held=state.sessions_held + sessions_held(new_dates),
        closed=closed,
        pending_exit=None if closed is not None else pending,
        last_close_paise=last_close,
        last_close_at=last_close_at,
    )
    return new_state, events


def _earliest_exit(state: PositionState, reported: StrategyExit | None) -> StrategyExit | None:
    """The strategy exit the position now waits for: the pending one or the newly reported
    one, whichever bar closed first (on a tie, the higher precedence). A report whose bar
    closed at or before the fill is stale: the position did not exist when it was seen."""
    if reported is not None and reported.bar_close_ts <= state.opened_at:
        reported = None
    known = [e for e in (state.pending_exit, reported) if e is not None]
    return min(known, key=lambda e: (e.bar_close_ts, EXIT_PRECEDENCE.index(e.reason)), default=None)


def _exits_due_at_open(
    bar_ts: dt.datetime,
    pending: StrategyExit | None,
    opened_at: dt.datetime,
    session_date: dt.date,
    rules: ManageRules,
) -> tuple[str, ...]:
    """The reasons that fell due before this bar opened: a strategy exit whose signal bar
    has closed (AC4), the session end from `exit_time` on the opening date, and the time
    stop once `max_bars` whole strategy bars have passed since the fill (AC5)."""
    due: list[str] = []
    if pending is not None and bar_ts >= pending.bar_close_ts:
        due.append(pending.reason)
    if session_due(bar_ts, rules.exit_time, session_date):
        due.append("SESSION_END")
    if rules.max_bars > 0 and bars_held(opened_at, bar_ts, rules.tf_min) >= rules.max_bars:
        due.append("TIME_STOP")
    return tuple(due)


def _close_on_bar(
    sign: int,
    bar: BarPaise,
    bar_ts: dt.datetime,
    stop: int,
    state: PositionState,
    tick: int,
    due_at_open: Iterable[str] = (),
) -> PaperClose | None:
    """The close this bar makes, if any: what is due at its open first, then inside it.

    `due_at_open` are the exits that fell due before the bar opened: they close at the
    open (chronological, Manager decision 1), unless a gap through the stop or the target
    is due at the same open and outranks them.
    """
    initial_stop, target = state.initial_stop_paise, state.target_paise
    stop_reason: CloseReason = "TRAIL_STOP" if (stop - initial_stop) * sign > 0 else "STOP"

    at_open: dict[str, tuple[int, bool]] = dict.fromkeys(due_at_open, (bar.open, False))
    through_stop = (bar.open - stop) * sign  # <= 0: the open is at or beyond the stop
    if through_stop <= 0:
        at_open[stop_reason] = (bar.open, through_stop < 0)
    if target is not None and (bar.open - target) * sign >= tick:
        at_open["TARGET"] = (target, True)
    if at_open:
        return _pick(at_open, bar_ts)

    inside: dict[str, tuple[int, bool]] = {}
    adverse, favourable = (bar.low, bar.high) if sign > 0 else (bar.high, bar.low)
    if (adverse - stop) * sign <= 0:
        inside[stop_reason] = (stop, False)
    if target is not None and (favourable - target) * sign >= tick:
        inside["TARGET"] = (target, False)
    return _pick(inside, bar_ts) if inside else None


def _pick(due: dict[str, tuple[int, bool]], bar_ts: dt.datetime) -> PaperClose:
    reason = exit_priority(due)
    price, gap = due[reason]
    return PaperClose(cast(CloseReason, reason), price, bar_ts, gap)


def closes_strategy_bar(bar_ts: dt.datetime, res: dt.timedelta, tf_min: int) -> bool:
    """True when the fine bar opening at `bar_ts` (`res` long) is the last fine bar of its
    strategy bar: its close reaches the end of the `tf_min`-minute strategy bar its open
    falls in, strategy bars running from 09:15 IST on the bar's date (`resample_bars`).

    With fine bars that divide the timeframe this is a close ON a boundary (1-minute bars,
    15-minute strategy: the bars opening 09:29, 09:44, ...). With coarser fine bars it is
    the bar that crosses the boundary (2-minute bars, 5-minute strategy: 09:19 closes the
    09:15 bar at 09:21), which is the last bar resample_bars puts into that strategy bar.
    """
    local = bar_ts.astimezone(IST)
    anchor = dt.datetime.combine(local.date(), STRATEGY_BAR_ANCHOR, IST)
    width = dt.timedelta(minutes=tf_min)
    end = anchor + ((local - anchor) // width + 1) * width
    return local + res >= end


def _trail_atr_paise(rules: ManageRules) -> float | None:
    """The frozen ATR in paise (exact: Decimal x 100), or None when the trail is off: a
    multiple or an ATR that is None, zero, negative or not finite (no live-ATR fallback),
    or an activation level that is not a finite number (a NaN would arm on every bar)."""
    usable = _positive_finite(rules.atr) and _positive_finite(rules.trail_atr_mult)
    if not usable or not _finite(rules.trail_activate_r):
        return None
    return float(to_decimal(rules.atr) * PAISE_PER_RUPEE)


def _trailed_stop(
    state: PositionState, stop: int, close: int, atr_paise: float | None, rules: ManageRules
) -> int:
    """The stop after a bar that closed at `close`: ratcheted from that close once the close
    is at least `trail_activate_r` in profit, snapped to the tick against the trader, never
    loosened. R is measured from the INITIAL stop; a zero-risk position has no R to arm on."""
    entry, initial = state.entry_paise, state.initial_stop_paise
    if atr_paise is None or entry == initial:
        return stop
    if r_multiple(state.direction, entry, initial, close) < rules.trail_activate_r:
        return stop
    mult, tick = rules.trail_atr_mult, rules.tick_paise
    # on the tick grid, or `stop` itself: an exact integer either way
    return int(ratchet_stop(state.direction, stop, close, atr_paise, mult, tick=tick))


def _closed_event(
    state: PositionState, close: PaperClose, bar: BarPaise, stop: int, res_min: int
) -> PaperEvent:
    """The CLOSED event: final, with the bar prices it used (the feed may revise bars).

    `price_paise` is the rule level on the managed series; the book adds the slipped
    `exit_paise` (Manager decisions A1, A13)."""
    detail: dict[str, Any] = {
        "reason": close.reason,
        "price_paise": close.price_paise,
        "gap": close.gap,
        "bar_ts": close.at.isoformat(),
        **bar.as_detail(),
        "stop_paise": stop,
        "initial_stop_paise": state.initial_stop_paise,
        "target_paise": state.target_paise,
        "resolution_min": res_min,
    }
    return PaperEvent(state.trade_id, close.at, CLOSED, detail)


def _session_over_event(
    state: PositionState, close: PaperClose, stop: int, res_min: int
) -> PaperEvent:
    """The CLOSED event of the session-end fallback: no bar at or after exit_time, the
    session is over, so the last close the position saw is the price (decision A5)."""
    detail: dict[str, Any] = {
        "reason": close.reason,
        "price_paise": close.price_paise,
        "gap": close.gap,
        "fallback": "session_over",  # no bar at or after exit_time on the opening date
        "bar_close_ts": close.at.isoformat(),
        "bar_close_paise": close.price_paise,
        "stop_paise": stop,
        "initial_stop_paise": state.initial_stop_paise,
        "target_paise": state.target_paise,
        "resolution_min": res_min,
    }
    return PaperEvent(state.trade_id, close.at, CLOSED, detail)


def _stop_moved_event(
    state: PositionState,
    old: int,
    new: int,
    bar: BarPaise,
    bar_ts: dt.datetime,
    effective_at: dt.datetime,
) -> PaperEvent:
    """The STOP_MOVED event, stamped with the bar CLOSE: the moment the new stop takes
    effect (Manager decision 4). It stores the bar whose close moved it."""
    detail: dict[str, Any] = {
        "from_paise": old,
        "to_paise": new,
        "initial_stop_paise": state.initial_stop_paise,
        "bar_ts": bar_ts.isoformat(),
        **bar.as_detail(),
    }
    return PaperEvent(state.trade_id, effective_at, STOP_MOVED, detail)


# ── validation ────────────────────────────────────────────────────────────────


def _check_call(
    state: PositionState, rules: ManageRules, now: dt.datetime, exit_: StrategyExit | None
) -> None:
    _require_aware(now, "now")
    _check_rules(rules)
    _check_state(state)
    if exit_ is not None:
        _check_strategy_exit(exit_, now)


def _check_rules(rules: ManageRules) -> None:
    if not _is_int(rules.max_bars) or rules.max_bars < 0:
        raise ValueError(f"ManageRules.max_bars={rules.max_bars!r} must be an int >= 0")
    if not isinstance(rules.exit_time, dt.time):
        raise ValueError(f"ManageRules.exit_time={rules.exit_time!r} must be a datetime.time")
    if not _is_int(rules.tick_paise) or rules.tick_paise <= 0:
        raise ValueError(f"ManageRules.tick_paise={rules.tick_paise!r} must be a positive int")
    if not _is_int(rules.tf_min) or rules.tf_min <= 0:
        raise ValueError(f"ManageRules.tf_min={rules.tf_min!r} must be a positive int")
    if rules.resolution_min is not None and (
        not _is_int(rules.resolution_min) or rules.resolution_min <= 0
    ):
        raise ValueError(
            f"ManageRules.resolution_min={rules.resolution_min!r} must be a positive int"
        )


def _check_state(state: PositionState) -> None:
    if state.direction not in ("long", "short"):
        raise ValueError(f"PositionState.direction={state.direction!r} is not long/short")
    if state.kind not in ("STOCK", "INDEX"):
        raise ValueError(f"PositionState.kind={state.kind!r} is not STOCK/INDEX")
    levels = {
        "entry_paise": state.entry_paise,
        "initial_stop_paise": state.initial_stop_paise,
        "stop_paise": state.stop_paise,
        "target_paise": state.target_paise,
    }
    for name, value in levels.items():
        if value is None and name == "target_paise":
            continue
        if not _is_int(value):
            raise ValueError(f"PositionState.{name}={value!r} must be integer paise")
    _require_aware(state.opened_at, "PositionState.opened_at")
    if state.last_bar_ts is not None:
        _require_aware(state.last_bar_ts, "PositionState.last_bar_ts")
    sign = 1 if state.direction == "long" else -1
    if (state.entry_paise - state.initial_stop_paise) * sign < 0:
        raise ValueError(
            f"PositionState: {state.direction} initial_stop_paise={state.initial_stop_paise} "
            f"is on the wrong side of entry_paise={state.entry_paise}"
        )
    if (state.stop_paise - state.initial_stop_paise) * sign < 0:
        raise ValueError(
            f"PositionState: stop_paise={state.stop_paise} is looser than "
            f"initial_stop_paise={state.initial_stop_paise}"
        )
    if state.last_close_paise is not None and not _is_int(state.last_close_paise):
        raise ValueError(f"PositionState.last_close_paise={state.last_close_paise!r} not paise")
    if state.last_close_at is not None:
        _require_aware(state.last_close_at, "PositionState.last_close_at")
    if state.pending_exit is not None and not isinstance(state.pending_exit, StrategyExit):
        raise ValueError(f"PositionState.pending_exit={state.pending_exit!r} is no StrategyExit")


def _check_strategy_exit(strategy_exit: StrategyExit, now: dt.datetime) -> None:
    if not isinstance(strategy_exit, StrategyExit):
        raise ValueError(f"strategy_exit={strategy_exit!r} must be a StrategyExit or None")
    if strategy_exit.bar_close_ts > now:
        raise ValueError(
            f"StrategyExit.bar_close_ts={strategy_exit.bar_close_ts.isoformat()} is after "
            f"now={now.isoformat()}: a signal bar that has not closed is look-ahead"
        )


def _is_int(value: object) -> bool:
    return isinstance(value, Integral) and not isinstance(value, bool)


def _finite(value: object) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def _positive_finite(value: object) -> bool:
    return _finite(value) and float(cast(Real, value)) > 0.0


def _require_aware(ts: object, name: str) -> None:
    if not isinstance(ts, dt.datetime) or ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{name}={ts!r} must be a timezone-aware datetime")
