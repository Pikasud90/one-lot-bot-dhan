"""The stop, target and trail rule (`ratchet.py`) as its callers apply it (US-079).

`ratchet.py` holds the rule as pure functions on prices. This module holds what the callers
share around it, so the desk's alert, the paper book, the Lab's `simulate()` and the
Simulator read the first stop the same way and step the rule the same way:

* `settings_of(params)`: the strategy's `ratchet*` parameters, or None when the slot does
  not ask for the rule (every slot today but the micro-trend one).
* `first_risk(bars, signal_close, timeframe, settings)`: the first risk, `stop_atr` x
  ATR(14, Wilder) of the managed price on `atr_tf` bars (bins from 09:15, cut at the 15:30
  close), read on the last of those bars closed at or before the signal. A strategy whose
  own timeframe is 60m or longer reads it on its own bars (`atr_timeframe`).
* `on_bar(plan, state, open, high, low, params)`: one bar, in the rule's order: the exit
  the bar forces with the levels in force, else the levels for the NEXT bar.
* `close_reason(label)`: the rule's exit label as a close reason and a gap flag.
* JSON helpers for the plan and the state a restart must not lose.

Pure apart from pandas: no I/O and no clock.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from ..data.resample import minutes_of, resample_bars
from ..indicators import core as ind
from . import ratchet

ATR_PERIOD = 14
#: the session close a bin of the ATR's bars is cut at (the last 60m bin opens at 15:15)
SESSION_CLOSE = dt.time(15, 30)
#: the stage of a trade in plain words (the page and the notifications)
STAGE_WORDS = (
    "first stop",
    "stop above breakeven",
    "35% of the move locked",
    "trailing behind the best price",
)
#: what a stop-out at each stage is called, in plain words
REASON_WORDS = {
    "STOP": "stopped out at the first stop",
    "BREAKEVEN_STOP": "stopped out above breakeven",
    "LOCK_STOP": "stopped out with 35% of the move locked",
    "TRAIL_STOP": "stopped out by the trailing stop",
}


@dataclass(frozen=True)
class RatchetSettings:
    """The slot's settings of the rule (`StrategyParams.ratchet*`)."""

    stop_atr: float = 1.5
    atr_tf: str = "60m"
    target_r: float = 3.0
    extend: bool = True

    @property
    def params(self) -> ratchet.RatchetParams:
        return ratchet.RatchetParams(extend=self.extend)


@dataclass(frozen=True)
class RatchetTrack:
    """A managed trade under the rule: the plan fixed at the fill and the state in force."""

    plan: ratchet.Plan
    state: ratchet.State


def settings_of(params: object) -> RatchetSettings | None:
    """The rule's settings when `params.ratchet` is exactly True, else None (rule off)."""
    if getattr(params, "ratchet", False) is not True:
        return None
    return RatchetSettings(
        stop_atr=float(getattr(params, "ratchet_stop_atr", 1.5)),
        atr_tf=str(getattr(params, "ratchet_atr_tf", "60m")),
        target_r=float(getattr(params, "ratchet_target_r", 3.0)),
        extend=bool(getattr(params, "ratchet_extend", True)),
    )


def managed_line(extend: bool) -> str:
    """One line for the alert: how the trade will be managed."""
    line = "Managed: stop above breakeven at 25% of the way, 35% locked at 50%"
    return line + (", the target moves out from 80%." if extend else ", trailing from 80%.")


# ── the first risk ────────────────────────────────────────────────────────────


def atr_timeframe(timeframe: str, stop_atr_tf: str) -> str:
    """The bars the first stop's ATR is read on: 60m when `stop_atr_tf` is "60m" and the
    strategy's timeframe is shorter than 60m, otherwise the strategy's own timeframe."""
    if stop_atr_tf == "60m" and minutes_of(timeframe) < 60:
        return "60m"
    return timeframe


def atr_table(
    bars: pd.DataFrame | None, timeframe: str, atr_tf: str
) -> tuple[np.ndarray, np.ndarray]:
    """(close times as int64 ns, ATR(14, Wilder)) of `bars` (OPEN times, tz-aware) on the
    ATR's timeframe. A bin closes at its start plus its length, cut at the session close."""
    empty = (np.zeros(0, np.int64), np.zeros(0))
    if bars is None or len(bars) == 0:
        return empty
    tf = atr_timeframe(timeframe, atr_tf)
    ab = resample_bars(bars[["open", "high", "low", "close"]], tf)
    if ab.empty:
        return empty
    atr = ind.atr(ab["high"], ab["low"], ab["close"], ATR_PERIOD).to_numpy(float)
    starts = pd.DatetimeIndex(ab.index)
    close = starts.normalize() + pd.Timedelta(
        hours=SESSION_CLOSE.hour, minutes=SESSION_CLOSE.minute
    )
    ends = starts + pd.Timedelta(minutes=minutes_of(tf))
    ends = ends.where(ends <= close, close)
    return pd.DatetimeIndex(ends).as_unit("ns").asi8.astype(np.int64), atr


def atr_at(table: tuple[np.ndarray, np.ndarray], when: Any) -> float:
    """The ATR of the last bin closed at or before `when`; NaN when there is none."""
    ends, atr = table
    t_ns = int(pd.Timestamp(when).as_unit("ns").value)
    i = int(np.searchsorted(ends, t_ns, side="right")) - 1
    return float(atr[i]) if i >= 0 else math.nan


def first_risk(
    bars: pd.DataFrame | None, signal_close: Any, timeframe: str, settings: RatchetSettings
) -> float:
    """`stop_atr` x ATR(14) read at the signal's close; NaN when there is no ATR yet."""
    table = atr_table(bars, timeframe, settings.atr_tf)
    return settings.stop_atr * atr_at(table, signal_close)


def usable(value: float) -> bool:
    return math.isfinite(value) and value > 0


# ── one bar ───────────────────────────────────────────────────────────────────


def on_bar(
    plan: ratchet.Plan,
    state: ratchet.State,
    open_: float,
    high: float,
    low: float,
    params: ratchet.RatchetParams,
) -> tuple[tuple[str, float] | None, ratchet.State, tuple[str, ...]]:
    """(the exit this bar forces, the state for the next bar, what changed). A bar that
    exits leaves the state as it was: nothing after the exit counts."""
    hit = ratchet.exit_on_bar(plan, state, open_, high, low)
    if hit is not None:
        return hit, state, ()
    new, changes = ratchet.advance(plan, state, high, low, params)
    return None, new, changes


def close_reason(label: str) -> tuple[str, bool]:
    """The rule's exit label as (close reason, gap): GAP_LOCK_STOP is LOCK_STOP at the open."""
    if label.startswith("GAP_"):
        return label[4:], True
    return label, False


def unit_cost(costs_inr: float, qty: int, delta: float | None = None) -> float:
    """Round-trip costs per unit of the managed price: per share, or per unit of premium
    over the option's delta (floored at 0.05, as the Simulator floors it)."""
    if not (math.isfinite(costs_inr) and costs_inr > 0 and qty > 0):
        return 0.0
    per_unit = costs_inr / qty
    return per_unit / max(float(delta), 0.05) if delta is not None else per_unit


# ── JSON ──────────────────────────────────────────────────────────────────────


def track_to_json(track: RatchetTrack) -> dict[str, Any]:
    p, s = track.plan, track.state
    return {
        "plan": {
            "side": p.side,
            "entry": p.entry,
            "stop": p.stop,
            "target": p.target,
            "cost": p.cost,
        },
        "state": {
            "stop": s.stop,
            "target": s.target,
            "best": s.best,
            "stage": s.stage,
            "stage_name": ratchet.STAGES[s.stage],
        },
    }


def track_from_json(data: Mapping[str, Any]) -> RatchetTrack:
    p, s = data["plan"], data["state"]
    plan = ratchet.Plan(
        int(p["side"]), float(p["entry"]), float(p["stop"]), float(p["target"]), float(p["cost"])
    )
    state = ratchet.State(float(s["stop"]), float(s["target"]), float(s["best"]), int(s["stage"]))
    return RatchetTrack(plan, state)
