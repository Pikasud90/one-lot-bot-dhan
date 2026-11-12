"""The paper book's side of `manage_step` (US-002): rules from the alert, the position
state as JSON, and the strategy's answers (its exit, its trail level). Pure: no I/O.

The state a restart must not lose (a trailed stop, a pending strategy exit, the cursor,
the last close seen) lives in the trade's `plan["position"]`, written in the same
transaction as the events it explains. Every price is integer paise on the managed
series (the underlying for an option). The exit rules are read from the alert ONCE, when
the trade opens, into `plan["rules"]` (review round 1 NIT): a step never re-parses them.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections.abc import Mapping
from numbers import Real
from typing import Any, cast

from .alert import resolution_min
from .manage import Direction, Kind, ManageRules, PositionState, StrategyExit, TrailLevel
from .money import to_paise

log = logging.getLogger(__name__)

POSITION_VERSION = 1
RULES_VERSION = 1
DEFAULT_EXIT_TIME = dt.time(15, 15)  # the desk's default exit_time (config/desk.yaml)


def rules_from_alert(
    alert: Mapping[str, Any], *, tick_paise: int, resolution: int | None, timeframe: str
) -> ManageRules:
    """The exit rules frozen in the alert's `manage` block (Desk._raise_signal).

    `atr` stays in rupees as the alert recorded it. It is the strategy's TRAIL ATR,
    `manage.atr_trail` (ATR(trail_atr_period), what the Lab trails with); only an alert that
    predates that key falls back to `manage.atr` (review round 1, MAJOR-1). A missing, zero
    or non-finite ATR or multiple leaves the trail off: never another ATR, never a live one.
    A missing or unreadable value takes the desk's default and is logged, never guessed
    silently: exit_time 15:15, max_bars 0, trail_activate_r 1.0, tf_min from the timeframe.
    `manage.line_trail` (the stop follows the strategy's own line) counts only when it is
    exactly True: an alert without it, i.e. every alert of a strategy with no line, never
    follows a reported level.
    """
    manage = alert.get("manage")
    manage = manage if isinstance(manage, Mapping) else {}
    tf_default = resolution_min(timeframe) or 1
    trail_atr = manage["atr_trail"] if "atr_trail" in manage else manage.get("atr")
    return ManageRules(
        target_r=_number(manage.get("target_r"), 0.0),
        trail_atr_mult=_number(manage.get("trail_atr_mult"), 0.0),
        trail_activate_r=_number(manage.get("trail_activate_r"), 1.0),
        atr=_atr(trail_atr),
        max_bars=_whole(manage.get("max_bars"), 0, "max_bars"),
        tf_min=_whole(manage.get("tf_min"), tf_default, "tf_min") or tf_default,
        exit_time=_exit_time(manage.get("exit_time")),
        tick_paise=int(tick_paise),
        resolution_min=resolution,
        line_trail=manage.get("line_trail") is True,
    )


def rules_to_json(rules: ManageRules) -> dict[str, Any]:
    """The rules as the plan stores them, JSON-safe (the resolution is the trade's own).
    `line_trail` is written only when it is on, so a plan without the strategy's own trail
    reads exactly as it did before that rule existed."""
    stored: dict[str, Any] = {
        "version": RULES_VERSION,
        "target_r": rules.target_r,
        "trail_atr_mult": rules.trail_atr_mult,
        "trail_activate_r": rules.trail_activate_r,
        "atr": rules.atr,
        "max_bars": rules.max_bars,
        "tf_min": rules.tf_min,
        "exit_time": rules.exit_time.strftime("%H:%M"),
        "tick_paise": rules.tick_paise,
    }
    if rules.line_trail:
        stored["line_trail"] = True
    return stored


def rules_from_json(data: Mapping[str, Any], *, resolution: int | None) -> ManageRules:
    """`rules_to_json` read back: values already checked at the open, so no defaults here
    (`line_trail` is off for a plan stored without it)."""
    if data.get("version") != RULES_VERSION:
        raise ValueError(f"paper rules version {data.get('version')!r} is not supported")
    atr = data["atr"]
    return ManageRules(
        target_r=float(data["target_r"]),
        trail_atr_mult=float(data["trail_atr_mult"]),
        trail_activate_r=float(data["trail_activate_r"]),
        atr=None if atr is None else float(atr),
        max_bars=int(data["max_bars"]),
        tf_min=int(data["tf_min"]),
        exit_time=dt.time.fromisoformat(str(data["exit_time"])),
        tick_paise=int(data["tick_paise"]),
        resolution_min=resolution,
        line_trail=data.get("line_trail") is True,
    )


def initial_position(
    *,
    trade_id: int,
    direction: str,
    kind: str,
    qty: int,
    entry_paise: int,
    levels: Mapping[str, Any],
    opened_at: dt.datetime,
    option: Mapping[str, Any] | None,
) -> PositionState:
    """A freshly filled position: nothing managed yet, the stop and target the alert's."""
    stop = int(levels["underlying_stop_paise"])
    target = levels.get("underlying_target_paise")
    return PositionState(
        direction=cast(Direction, direction),
        kind=cast(Kind, kind),
        qty=int(qty),
        entry_paise=int(entry_paise),
        initial_stop_paise=stop,
        stop_paise=stop,
        target_paise=None if target is None else int(target),
        trail_active=False,
        opened_at=opened_at,
        last_bar_ts=None,
        bars_held=0,
        sessions_held=0,
        option=dict(option) if option else None,
        trade_id=int(trade_id),
    )


def position_to_json(state: PositionState) -> dict[str, Any]:
    """The part of the state that changes as bars are managed, JSON-safe (ISO times)."""
    pending = state.pending_exit
    return {
        "version": POSITION_VERSION,
        "entry_paise": state.entry_paise,
        "initial_stop_paise": state.initial_stop_paise,
        "stop_paise": state.stop_paise,
        "target_paise": state.target_paise,
        "trail_active": state.trail_active,
        "opened_at": state.opened_at.isoformat(),
        "last_bar_ts": _iso(state.last_bar_ts),
        "bars_held": state.bars_held,
        "sessions_held": state.sessions_held,
        "pending_exit": None
        if pending is None
        else {"reason": pending.reason, "bar_close_ts": pending.bar_close_ts.isoformat()},
        "last_close_paise": state.last_close_paise,
        "last_close_at": _iso(state.last_close_at),
    }


def position_from_json(
    data: Mapping[str, Any],
    *,
    trade_id: int,
    direction: str,
    kind: str,
    qty: int,
    option: Mapping[str, Any] | None,
) -> PositionState:
    """`position_to_json` read back; the trade row supplies what never changes."""
    if data.get("version") != POSITION_VERSION:
        raise ValueError(f"paper position version {data.get('version')!r} is not supported")
    pending = data.get("pending_exit")
    target = data.get("target_paise")
    last_close = data.get("last_close_paise")
    return PositionState(
        direction=cast(Direction, direction),
        kind=cast(Kind, kind),
        qty=int(qty),
        entry_paise=int(data["entry_paise"]),
        initial_stop_paise=int(data["initial_stop_paise"]),
        stop_paise=int(data["stop_paise"]),
        target_paise=None if target is None else int(target),
        trail_active=bool(data["trail_active"]),
        opened_at=_time(data["opened_at"]),
        last_bar_ts=_time_or_none(data.get("last_bar_ts")),
        bars_held=int(data["bars_held"]),
        sessions_held=int(data["sessions_held"]),
        option=dict(option) if option else None,
        trade_id=int(trade_id),
        pending_exit=None
        if not pending
        else StrategyExit(pending["reason"], _time(pending["bar_close_ts"])),
        last_close_paise=None if last_close is None else int(last_close),
        last_close_at=_time_or_none(data.get("last_close_at")),
    )


def strategy_exit_of(answer: object) -> StrategyExit | None:
    """The strategy-exit callback's answer: None, a StrategyExit, or the desk's plain
    (reason, bar_close_ts) tuple (the desk does not import the paper package). Anything
    else raises ValueError, as does a tuple StrategyExit itself rejects."""
    if answer is None or isinstance(answer, StrategyExit):
        return answer
    if isinstance(answer, tuple) and len(answer) == 2:
        reason, bar_close_ts = answer
        return StrategyExit(reason, bar_close_ts)
    raise ValueError(f"strategy exit answer {answer!r} is not a StrategyExit, a tuple or None")


def trail_level_of(answer: object) -> TrailLevel | None:
    """The trail callback's answer: None, a TrailLevel, or the desk's plain
    (level in rupees, bar_close_ts) tuple (the desk does not import the paper package, so
    the level becomes paise here, once). Anything else raises ValueError, as does a level
    that is not a positive finite price or a time TrailLevel itself rejects."""
    if answer is None or isinstance(answer, TrailLevel):
        return answer
    if isinstance(answer, tuple) and len(answer) == 2:
        level, bar_close_ts = answer
        return TrailLevel(to_paise(level), bar_close_ts)
    raise ValueError(f"trail level answer {answer!r} is not a TrailLevel, a tuple or None")


# ── parsing ───────────────────────────────────────────────────────────────────


def _number(value: object, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    log.warning("paper: manage value %r is not a finite number; using %s", value, default)
    return default


def _atr(value: object) -> float | None:
    """The frozen ATR in rupees, or None (trail off): never a guess."""
    if isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value):
        return float(value) if float(value) > 0 else None
    return None


def _whole(value: object, default: int, name: str) -> int:
    if value is None:
        return default
    if isinstance(value, Real) and not isinstance(value, bool) and float(value).is_integer():
        if float(value) >= 0:
            return int(float(value))
    log.warning("paper: manage %s=%r is not a whole number >= 0; using %d", name, value, default)
    return default


def _exit_time(value: object) -> dt.time:
    if value is None:
        return DEFAULT_EXIT_TIME
    try:
        hh, mm = str(value).strip().split(":")
        return dt.time(int(hh), int(mm))
    except ValueError:
        log.warning("paper: manage exit_time=%r is not HH:MM; using %s", value, DEFAULT_EXIT_TIME)
        return DEFAULT_EXIT_TIME


def _iso(ts: dt.datetime | None) -> str | None:
    return None if ts is None else ts.isoformat()


def _time(value: object) -> dt.datetime:
    ts = dt.datetime.fromisoformat(str(value))
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"paper position time {value!r} has no timezone")
    return ts


def _time_or_none(value: object) -> dt.datetime | None:
    return None if value is None else _time(value)
