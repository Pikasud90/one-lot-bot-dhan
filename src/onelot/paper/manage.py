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
  at the open  the stop is due when the open is at or through it (closes at the open;
               `gap` when strictly through, like the fill rule's GAP_THROUGH_STOP); the
               target is due when the open is through it by at least one tick (closes at
               exactly the target, gap=True). Precedence (`desk.rules.exit_priority`)
               picks among the reasons due at the same open.
  inside       the stop on a touch (a long's low at or below it), at the stop; the target
               only when traded THROUGH by one tick, at exactly the target. STOP beats
               TARGET: which came first inside the bar is unknown, so the adverse one wins.
  at the close (AC3-AC5, next slices) the trail ratchets; strategy, session and time exits
               become due at the next bar's open.
Deliberate differences from the Lab: a target needs a trade-through and fills at exactly T
(the Lab fills on a touch, at the open on a gap); no partial exits; the trail activates on
the close, not on the bar's best price; no forced end-of-day close in swing (US-027).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, replace
from numbers import Integral
from typing import Any, Literal, NamedTuple, cast, get_args

import numpy as np

# See book.py: no pandas-stubs are pinned, so a new module names the missing stubs here.
import pandas as pd  # type: ignore[import-untyped]

from ..clock import IST
from ..desk.rules import bars_held, exit_priority, sessions_held
from .events import PaperEvent
from .fill import NS_PER_MINUTE, Bars, from_ns, infer_resolution_min, to_ns
from .money import to_paise

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
]
StrategyExitReason = Literal["SIGNAL_EXIT", "OPPOSITE_SIGNAL"]
Direction = Literal["long", "short"]
Kind = Literal["STOCK", "INDEX"]

CLOSE_REASONS: frozenset[str] = frozenset(get_args(CloseReason))
STRATEGY_EXIT_REASONS: frozenset[str] = frozenset(get_args(StrategyExitReason))
CLOSED = "CLOSED"  # the event kind of a close
PRICE_COLUMNS = ("open", "high", "low", "close")


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
    paper_trades id the events are booked under (0 in pure unit tests).
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
    """One series' bars normalised once: int64 ns open times (strictly increasing) and
    float64 rupee prices; `usable[i]` is False for a row that cannot be judged."""

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
        return {
            "open_paise": self.open,
            "high_paise": self.high,
            "low_paise": self.low,
            "close_paise": self.close,
        }


def normalise_ohlc(frame: object) -> OhlcBars:
    """A bars frame -> OhlcBars. The caller's frame is never modified.

    Raises ValueError for anything but a DataFrame with a tz-aware DatetimeIndex (bar OPEN
    times) and open/high/low/close columns: a naive time is rejected, never guessed. Rows
    are sorted by time; for a duplicated time the LAST row wins (the feed's latest
    revision). A row with a non-finite or non-positive price, or a low above the open or
    close, or a high below them, is marked unusable.
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
            & (l > 0.0)
            & (l <= np.minimum(o, c))
            & (h >= np.maximum(o, c))
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

    A bar is eligible iff it opens at or after the fill bar (or after `last_bar_ts`) and
    its open plus the resolution is not later than `now`. Stepping again with the same
    (now, bars) books nothing. Raises ValueError for a naive `now`, a malformed frame or
    an inconsistent state (float paise, a stop on the wrong side of the entry, a stop
    looser than the initial one).
    """
    _require_aware(now, "now")
    _check_rules(rules)
    _check_state(state)
    if state.closed is not None:
        return state, []
    return _manage(state, normalise_ohlc(bars), rules, now.astimezone(IST), strategy_exit)


def _manage(
    state: PositionState,
    view: OhlcBars,
    rules: ManageRules,
    now: dt.datetime,
    strategy_exit: StrategyExit | None,
) -> tuple[PositionState, list[PaperEvent]]:
    sign = 1 if state.direction == "long" else -1
    res_min = rules.resolution_min or infer_resolution_min(Bars(ns=view.ns, opens=view.open))
    res = dt.timedelta(minutes=res_min)
    if state.last_bar_ts is None:
        lo = int(np.searchsorted(view.ns, to_ns(state.opened_at), side="left"))
    else:
        lo = int(np.searchsorted(view.ns, to_ns(state.last_bar_ts), side="right"))
    hi = int(np.searchsorted(view.ns, to_ns(now) - res_min * NS_PER_MINUTE, side="right"))
    if hi <= lo:
        return state, []

    stop, target = state.stop_paise, state.target_paise
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
        closed = _close_on_bar(sign, bar, bar_ts, stop, state.initial_stop_paise, target, rules)
        if closed is not None:
            events.append(_closed_event(state, closed, bar, stop, res_min))
            break
        # AC3-AC5 (next slices): ratchet the trail at this close; strategy, session and
        # time exits become due at the next bar's open.

    assert last_ts is not None  # hi > lo: at least one bar was walked
    new_state = replace(
        state,
        stop_paise=stop,
        last_bar_ts=last_ts,
        bars_held=bars_held(state.opened_at, last_ts + res, rules.tf_min),
        sessions_held=state.sessions_held + sessions_held(new_dates),
        closed=closed,
    )
    return new_state, events


def _close_on_bar(
    sign: int,
    bar: BarPaise,
    bar_ts: dt.datetime,
    stop: int,
    initial_stop: int,
    target: int | None,
    rules: ManageRules,
) -> PaperClose | None:
    """The close this bar makes, if any: what is due at its open first, then inside it."""
    stop_reason: CloseReason = "TRAIL_STOP" if (stop - initial_stop) * sign > 0 else "STOP"
    tick = rules.tick_paise

    at_open: dict[str, tuple[int, bool]] = {}
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


def _closed_event(
    state: PositionState, close: PaperClose, bar: BarPaise, stop: int, res_min: int
) -> PaperEvent:
    """The CLOSED event: final, with the bar prices it used (the feed may revise bars)."""
    detail: dict[str, Any] = {
        "reason": close.reason,
        "price_paise": close.price_paise,
        "gap": close.gap,
        "bar_ts": close.at.isoformat(),
        "bar": bar.as_detail(),
        "stop_paise": stop,
        "initial_stop_paise": state.initial_stop_paise,
        "target_paise": state.target_paise,
        "resolution_min": res_min,
    }
    return PaperEvent(state.trade_id, close.at, CLOSED, detail)


# ── validation ────────────────────────────────────────────────────────────────


def _check_rules(rules: ManageRules) -> None:
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


def _is_int(value: object) -> bool:
    return isinstance(value, Integral) and not isinstance(value, bool)


def _require_aware(ts: object, name: str) -> None:
    if not isinstance(ts, dt.datetime) or ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{name}={ts!r} must be a timezone-aware datetime")
