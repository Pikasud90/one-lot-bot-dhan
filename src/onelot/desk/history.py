"""Stored daily history for the desk's trend ladder. Read-only.

The desk polls about 25 days of fine bars. That warms every intraday rung of the
multi-timeframe Supertrend ladder (`indicators/mtf.py`), but not the daily one: a daily
Supertrend(14) is trusted after 70 daily bars. The stored history supplies them.

The days are built the way the Lab builds them (`data/hygiene.consistent_daily`), so the
desk and a backtest read the same daily trend: from the stored one-minute bars of the last
`FINE_DAYS` days wherever they exist (the same vendor and price scale as the live bars), with
the older stored daily bars kept only as a warm-up, rescaled to that scale. The tracker then
lays its own live days on top (`InstrumentTracker.daily_bars`), so the series runs up to the
last closed session even when the store ends a few days back.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd  # type: ignore[import-untyped]
from sqlalchemy import Engine

from ..data.hygiene import clean_intraday, consistent_daily
from ..data.store import _read_bars

#: calendar days of stored fine bars the days are derived from (about 110 sessions)
FINE_DAYS = 160
#: calendar days of stored daily bars read as the warm-up before them
DAILY_DAYS = 420
#: the stored resolution the days are derived from
FINE_RESOLUTION = "1m"


def daily_history(
    engine: Engine, series_key: str, now: dt.datetime, fine_days: int = FINE_DAYS
) -> pd.DataFrame:
    """Daily bars of `series_key` up to `now` from the store (empty when it has none).

    Two indexed range reads: about 0.1 s for 160 days of one-minute bars. Nothing is written.
    """
    at = pd.Timestamp(now)
    fine = _read_bars(
        engine, series_key, FINE_RESOLUTION, start=at - pd.Timedelta(days=fine_days), end=at
    )
    stored = _read_bars(engine, series_key, "1d", start=at - pd.Timedelta(days=DAILY_DAYS), end=at)
    daily = consistent_daily(stored, clean_intraday(series_key, fine))
    return daily if daily is not None else pd.DataFrame()
