"""Builders for the delivery tests: one-minute bars of whole sessions, an IST clock."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from onelot.clock import IST


def at(day: dt.date, hhmm: str) -> dt.datetime:
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(h, m), tzinfo=IST)


def session_bars(
    day: dt.date, open_: float, close: float, last: str = "15:14", first: str = "09:15"
) -> pd.DataFrame:
    """One-minute bars of `day` from `first` to `last` (inclusive), walking from `open_` to
    `close`: the first bar opens at `open_`, the last closes at `close`."""
    start, end = at(day, first), at(day, last)
    idx = pd.date_range(start, end, freq="1min")
    n = len(idx)
    path = np.linspace(open_, close, n + 1)
    o, c = path[:-1], path[1:]
    return pd.DataFrame(
        {
            "open": o,
            "high": np.maximum(o, c),
            "low": np.minimum(o, c),
            "close": c,
            "volume": 1000.0,
        },
        index=idx,
    )


def bars_of(days: dict[dt.date, tuple[float, float]], last: str = "15:14") -> pd.DataFrame:
    return pd.concat([session_bars(d, o, c, last) for d, (o, c) in sorted(days.items())])


def weekdays(start: dt.date, n: int) -> list[dt.date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out
