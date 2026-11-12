"""Multi-timeframe Supertrend ladder on closed bars.

The owner's rule (2026-11-11): Supertrend with period 14 and multiplier 1.7 on every
timeframe; a trade is taken only when the timeframes agree, and the Supertrend line of one
of them is the stop and the trail.

Everything here answers one question without look-ahead: "at this moment, what did the last
CLOSED bar of each timeframe say?" A bar counts as closed at its nominal end (start + length;
a daily bar at the following midnight) and never earlier, so the answer for a past moment is
the same whether it is computed live or years later. A bar cut short by the session close
(the 15:15 hour bar) is therefore first used by the next session, which is when it matters.

The research harness (`scripts/mtf_bruteforce.py`), the Lab strategy `supertrend_mtf` and the
desk's trend ladder all read the trend through this module.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from ..data.resample import minutes_of, normalize_resolution, resample_bars
from .core import supertrend

#: the owner's ladder, fastest first
LADDER_TIMEFRAMES: tuple[str, ...] = ("1m", "5m", "15m", "30m", "60m", "1d")
#: the owner's Supertrend setting, the same on every timeframe
ST_PERIOD = 14
ST_MULT = 1.7
#: bars of a timeframe before its Supertrend is trusted (the ATR seed has decayed)
WARMUP_FACTOR = 5

_NS_PER_MINUTE = 60 * 1_000_000_000
_IST = "Asia/Kolkata"  # the exchange's time zone: what the desk shows


def index_ns(index: pd.DatetimeIndex) -> np.ndarray:
    """Bar times as UTC nanoseconds (the unit every array in this module uses)."""
    idx = index if index.tz is not None else index.tz_localize("UTC")
    return np.asarray(idx.as_unit("ns").asi8, dtype=np.int64)


def nominal_close_ns(index: pd.DatetimeIndex, timeframe: str) -> np.ndarray:
    """The moment each bar of `timeframe` is complete: start + length, a day at midnight."""
    tf = normalize_resolution(timeframe)
    if tf == "1d":
        idx = index if index.tz is not None else index.tz_localize("UTC")
        return index_ns(idx.normalize() + pd.Timedelta(days=1))
    return index_ns(index) + minutes_of(tf) * _NS_PER_MINUTE


@dataclass(frozen=True)
class TimeframeTrend:
    """Supertrend of one timeframe, one row per bar of that timeframe."""

    timeframe: str
    start_ns: np.ndarray  #: bar start, UTC ns
    close_ns: np.ndarray  #: nominal close, UTC ns (ascending)
    direction: np.ndarray  #: int8: +1 up, -1 down, 0 not warmed up
    line: np.ndarray  #: the Supertrend line (support when up, resistance when down)

    def at(self, times_ns: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Direction and line of the last bar closed at or before each time."""
        t = np.asarray(times_ns, dtype=np.int64)
        pos = np.searchsorted(self.close_ns, t, side="right") - 1
        ok = pos >= 0
        p = np.where(ok, pos, 0)
        if len(self.direction) == 0:
            return np.zeros(len(t), dtype=np.int8), np.full(len(t), np.nan)
        d = np.where(ok, self.direction[p], 0).astype(np.int8)
        ln = np.where(ok & (d != 0), self.line[p], np.nan)
        return d, ln

    def trend_start(self, time_ns: int) -> tuple[int, bool] | None:
        """Where the trend in force at `time_ns` began: (start of its first bar, exact).

        `exact` is False when that bar is the first one with a reading: the trend may have
        begun earlier than the bars given, so the time is then only "at least since"."""
        pos = int(np.searchsorted(self.close_ns, time_ns, side="right")) - 1
        if pos < 0 or self.direction[pos] == 0:
            return None
        d = self.direction[pos]
        k = pos
        while k > 0 and self.direction[k - 1] == d:
            k -= 1
        return int(self.start_ns[k]), bool(k > 0 and self.direction[k - 1] == -d)


def timeframe_trend(
    bars: pd.DataFrame,
    timeframe: str,
    period: int = ST_PERIOD,
    mult: float = ST_MULT,
    warmup_bars: int | None = None,
) -> TimeframeTrend:
    """Supertrend of `bars`, which are already bars of `timeframe`."""
    tf = normalize_resolution(timeframe)
    if bars is None or bars.empty:
        empty = np.zeros(0)
        return TimeframeTrend(
            tf, empty.astype(np.int64), empty.astype(np.int64), empty.astype(np.int8), empty
        )
    st = supertrend(bars["high"], bars["low"], bars["close"], period, mult)
    direction = st["st_dir"].to_numpy(dtype=np.int8).copy()
    line = st["st_line"].to_numpy(dtype=float)
    warm = WARMUP_FACTOR * period if warmup_bars is None else warmup_bars
    direction[: min(warm, len(direction))] = 0
    direction[np.isnan(line)] = 0
    index = pd.DatetimeIndex(bars.index)
    return TimeframeTrend(tf, index_ns(index), nominal_close_ns(index, tf), direction, line)


def build_ladder(
    minute_bars: pd.DataFrame,
    daily_bars: pd.DataFrame | None = None,
    timeframes: Sequence[str] = LADDER_TIMEFRAMES,
    period: int = ST_PERIOD,
    mult: float = ST_MULT,
    base: str = "1m",
) -> dict[str, TimeframeTrend]:
    """One `TimeframeTrend` per timeframe, all built from the same intraday bars.

    `minute_bars` are bars of resolution `base` (IST index). `daily_bars`, when given, is the
    daily series to use for "1d" (it should reach further back than the intraday bars, so the
    daily Supertrend is warm from the first intraday session); otherwise the day is resampled
    from the intraday bars.
    """
    out: dict[str, TimeframeTrend] = {}
    base = normalize_resolution(base)
    for raw in timeframes:
        tf = normalize_resolution(raw)
        if tf == base:
            bars = minute_bars
        elif tf == "1d":
            bars = daily_bars if daily_bars is not None else resample_bars(minute_bars, "1d")
        else:
            bars = resample_bars(minute_bars, tf)
        out[tf] = timeframe_trend(bars, tf, period, mult)
    return out


def ladder_at(
    ladder: dict[str, TimeframeTrend], times_ns: np.ndarray
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """Closed-bar direction and line of every timeframe at each time."""
    dirs: dict[str, np.ndarray] = {}
    lines: dict[str, np.ndarray] = {}
    for tf, trend in ladder.items():
        dirs[tf], lines[tf] = trend.at(times_ns)
    return dirs, lines


def alignment(dirs: dict[str, np.ndarray], timeframes: Sequence[str]) -> np.ndarray:
    """+1 where every listed timeframe is up, -1 where every one is down, else 0."""
    tfs = [normalize_resolution(t) for t in timeframes]
    if not tfs:
        raise ValueError("alignment needs at least one timeframe")
    first = dirs[tfs[0]]
    up = first == 1
    down = first == -1
    for tf in tfs[1:]:
        up &= dirs[tf] == 1
        down &= dirs[tf] == -1
    return (up.astype(np.int8) - down.astype(np.int8)).astype(np.int8)


def snapshot(ladder: dict[str, TimeframeTrend], now: pd.Timestamp) -> list[dict[str, object]]:
    """The ladder as the desk shows it at `now`: one row per timeframe, fastest first.

    `since` is the start (IST) of the bar the trend began on, `since_exact` False when the
    bars given do not reach back to that turn (the trend is then at least that old)."""
    ts = now if now.tzinfo is not None else now.tz_localize("UTC")
    t_ns = int(ts.tz_convert("UTC").as_unit("ns").value)
    rows: list[dict[str, object]] = []
    for tf, trend in ladder.items():
        d, ln = trend.at(np.array([t_ns], dtype=np.int64))
        began = trend.trend_start(t_ns)
        rows.append(
            {
                "timeframe": tf,
                "direction": int(d[0]),
                "line": None if np.isnan(ln[0]) else float(ln[0]),
                "since": None
                if began is None
                else pd.Timestamp(began[0], tz="UTC").tz_convert(_IST).isoformat(),
                "since_exact": bool(began[1]) if began is not None else False,
            }
        )
    return rows
