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

import numpy as np
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


#: calendar days of stored one-minute bars the pullback rule's days are built from (US-067):
#: about 245 sessions, so the last 221 days are derived from one-minute bars wherever the
#: store has them, exactly as the research's `DataCache` built them
RULE_FINE_DAYS = 365
#: calendar days of stored daily bars read as the warm-up before them
RULE_DAILY_DAYS = 600


def rule_daily(
    engine: Engine,
    series_key: str,
    now: dt.datetime,
    fine_days: int = RULE_FINE_DAYS,
    daily_days: int = RULE_DAILY_DAYS,
) -> tuple[pd.DataFrame, dict[dt.date, dt.time]]:
    """The pullback rule's daily bars of `series_key` up to `now` from the store, and the time
    of each derived day's last one-minute bar (the freshness test reads it). Read-only.

    The same construction as `daily_history` (and the Lab's `DataCache`) with a longer
    one-minute window; the trend ladder's `daily_history` is unchanged."""
    at = pd.Timestamp(now)
    fine = _read_bars(
        engine, series_key, FINE_RESOLUTION, start=at - pd.Timedelta(days=fine_days), end=at
    )
    stored = _read_bars(engine, series_key, "1d", start=at - pd.Timedelta(days=daily_days), end=at)
    clean = clean_intraday(series_key, fine)
    daily = consistent_daily(stored, clean)
    last: dict[dt.date, dt.time] = {}
    if daily is None:
        daily = pd.DataFrame()
    if clean is not None and not clean.empty:
        idx = pd.DatetimeIndex(clean.index).tz_convert("Asia/Kolkata")
        stamps = pd.Series(idx, index=idx).groupby(idx.date).max()
        last = {d: ts.time() for d, ts in stamps.items()}
        if not daily.empty:  # how many one-minute bars made each day (a stray bar shows)
            counts = pd.Series(1, index=idx).groupby(idx.date).sum()
            didx = pd.DatetimeIndex(daily.index)
            days = (didx.tz_convert("Asia/Kolkata") if didx.tz is not None else didx).date
            daily = daily.assign(minutes=[float(counts.get(d, np.nan)) for d in days])
    return daily, last


#: calendar days of stored one-minute bars a micro-trend ladder reads (US-068): about 50
#: sessions, so the 4-hour Supertrend (70 bars, two a session) has a reading from the start
MICRO_DAYS = 75


def minute_history(
    engine: Engine, series_key: str, now: dt.datetime, days: int = MICRO_DAYS
) -> pd.DataFrame:
    """The cleaned stored one-minute bars of `series_key` for the last `days` days up to `now`
    (empty when the store has none). Read-only."""
    at = pd.Timestamp(now)
    fine = _read_bars(
        engine, series_key, FINE_RESOLUTION, start=at - pd.Timedelta(days=days), end=at
    )
    return clean_intraday(series_key, fine) if fine is not None else pd.DataFrame()
