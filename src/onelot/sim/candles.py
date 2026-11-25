"""Candles to a minute, without a session (`GET /api/sim/candles`, SIM-API-2.md).

The candles are the session's own: `data.resample.resample_bars` of the cleaned one-minute bars
(bins from 09:15 each day), a candle closing at its start plus its length, cut at the day's
close. Only minutes up to `until` are read, so nothing after it can show (the forming candle
is built from the minutes of its bin up to `until`).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from ..data.resample import minutes_of, resample_bars

_NS = 1_000_000_000
_MIN_NS = 60 * _NS
_DAY_NS = 86_400 * _NS
_IST_NS = 19_800 * _NS
#: the chart's timeframes (a view only, SIM-API-2.md)
CHART_TIMEFRAMES: tuple[str, ...] = ("1m", "3m", "5m", "10m", "15m", "30m", "60m")
COUNT_DEFAULT, COUNT_MIN, COUNT_MAX = 300, 50, 1000
TRADING_MINUTES = 375  # one NSE session, 09:15 to 15:30


def _r(v: float, nd: int = 2) -> float | None:
    x = float(v)
    return round(x, nd) if math.isfinite(x) else None


def _row(t_ns: int, o: float, h: float, low: float, c: float, v: float) -> list:
    return [int(t_ns // _NS), _r(o), _r(h), _r(low), _r(c), _r(v, 0)]


def calendar_days_for(count: int, tf_minutes: int) -> int:
    """Calendar days that hold about `count` candles of `tf_minutes` (with weekends and a few
    holidays to spare)."""
    sessions = math.ceil((count + 1) * tf_minutes / TRADING_MINUTES) + 1
    return math.ceil(sessions * 7 / 5) + 4


def candles_to(
    minutes: pd.DataFrame, timeframe: str, close_min: int, until_ns: int, count: int
) -> tuple[list[list], list | None, int]:
    """(up to `count` closed candles ending at or before the minute `until_ns`, oldest first;
    the forming candle or None; how many closed candles the minutes held)."""
    m = minutes[pd.DatetimeIndex(minutes.index).as_unit("ns").asi8 <= until_ns]
    if m.empty:
        return [], None, 0
    tfm = minutes_of(timeframe)
    cd = m if timeframe == "1m" else resample_bars(m, timeframe)
    start = pd.DatetimeIndex(cd.index).as_unit("ns").asi8.astype(np.int64)
    local = start + _IST_NS
    close = (local // _DAY_NS) * _DAY_NS - _IST_NS + close_min * _MIN_NS
    end = np.minimum(start + tfm * _MIN_NS, close)
    closed = end <= until_ns + _MIN_NS
    o, h, low, c = (cd[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    v = cd["volume"].to_numpy(float) if "volume" in cd.columns else np.zeros(len(cd))
    idx = np.flatnonzero(closed)
    n_closed = len(idx)
    idx = idx[-count:]
    rows = [_row(int(start[i]), o[i], h[i], low[i], c[i], v[i]) for i in idx]
    forming = None
    if len(cd) and not closed[-1]:
        i = len(cd) - 1
        forming = _row(int(start[i]), o[i], h[i], low[i], c[i], v[i])
    return rows, forming, n_closed
