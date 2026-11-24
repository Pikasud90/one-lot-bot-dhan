"""Hand-made one-minute bars for the Volume Watch tests. No network, no real data."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from onelot.clock import IST

__all__ = ["IST", "day_bars", "sessions"]


def sessions(start: str, n: int) -> list[dt.date]:
    """`n` weekdays from `start`."""
    out: list[dt.date] = []
    d = dt.date.fromisoformat(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def day_bars(
    day: dt.date | str,
    turnover: float = 1e6,
    minutes: int = 375,
    close: float = 100.0,
    noise: np.random.Generator | None = None,
    up: float = 0.5,
) -> pd.DataFrame:
    """One session of one-minute bars from 09:15: every bar's turnover is `turnover` (times a
    random factor with `noise`), its close sits `up` of the way from low to high."""
    d = dt.date.fromisoformat(day) if isinstance(day, str) else day
    t0 = pd.Timestamp(dt.datetime.combine(d, dt.time(9, 15)), tz=IST)
    idx = pd.date_range(t0, periods=minutes, freq="1min", name="ts")
    factor = noise.lognormal(0, 0.4, minutes) if noise is not None else np.ones(minutes)
    lo, hi = close - 1.0, close + 1.0
    c = lo + up * (hi - lo)
    return pd.DataFrame(
        {
            "open": np.full(minutes, close),
            "high": np.full(minutes, hi),
            "low": np.full(minutes, lo),
            "close": np.full(minutes, c),
            "volume": turnover * factor / c,
        },
        index=idx,
    )
