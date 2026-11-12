"""Known defects of the stored bar history, repaired when the Lab loads bars.

The store keeps what each vendor sent. Checked on 2026-11-11, three things in it are not
what a backtest should trade:

1. **Mock-trading sessions.** NSE and BSE run mock sessions on some Saturdays and Dhan's
   one-minute history carries them (2021-11-13, 2022-01-08, 2022-01-22, 2022-02-05,
   2022-04-09, 2022-04-30; SENSEX also 2021-11-20, 2021-11-27, 2026-03-21). Their prices are
   test prices, up to 10% away from the market, and the next real session then opens with a
   "gap" of the same size. The 2021 Muhurat day (2021-11-04) is stored as flat afternoon
   bars. A weekend date is real only when it is a known special session.
2. **Two demergers the one-minute history is not adjusted for**: ITC Hotels out of ITC
   (ex-date 2025-01-06, -5.8% at the open) and the commercial-vehicle business out of Tata
   Motors, now TMPV (ex-date 2025-10-14, -39.8%). Splits, bonuses and the Jio Financial
   demerger of Reliance are adjusted by the vendor.
3. **Daily bars that do not match the intraday bars.** The daily history up to 2026-10-26
   came from Yahoo and the intraday history from Dhan. Yahoo adjusts demergers with its own
   factors (ITC 3.7% and Reliance 2% away from the intraday scale, Tata Motors 39.5%), its
   MIDCPNIFTY ticker is a different index (11-25% higher than Dhan's Nifty Midcap Select),
   and it has no bar for several real sessions (2024-01-20, 2024-03-02, 2024-05-18,
   2026-02-01; FINNIFTY and MIDCPNIFTY also 2022-12-26, 2024-01-01, 2024-02-19, 2025-01-01,
   2026-01-01). Every "previous day" level and the daily ATR came from that series.

`clean_intraday` repairs 1 and 2; `consistent_daily` builds the daily series from the cleaned
one-minute bars wherever they exist and keeps the older daily history only as a warm-up,
rescaled to the intraday price scale. Nothing in the database is changed.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from .resample import resample_bars

PRICE_COLUMNS = ("open", "high", "low", "close")

#: Weekend dates on which the exchanges really traded (Budget days, the special sessions of
#: 2024 and Muhurat Sundays). Any other Saturday or Sunday in the store is a mock session.
REAL_WEEKEND_SESSIONS: frozenset[dt.date] = frozenset(
    {
        dt.date(2020, 2, 1),
        dt.date(2023, 11, 12),
        dt.date(2024, 1, 20),
        dt.date(2024, 3, 2),
        dt.date(2024, 5, 18),
        dt.date(2025, 2, 1),
        dt.date(2026, 2, 1),
        dt.date(2026, 11, 8),
    }
)

#: Weekday dates stored as something other than a real session.
JUNK_WEEKDAY_SESSIONS: frozenset[dt.date] = frozenset({dt.date(2021, 11, 4)})

#: series -> [(ex-date, factor)]: every intraday price BEFORE the ex-date is multiplied by
#: the factor (ex-date open / previous close of the one-minute history).
INTRADAY_BACK_ADJUST: dict[str, tuple[tuple[dt.date, float], ...]] = {
    "ITC": ((dt.date(2025, 1, 6), 455.6 / 483.9),),
    "TMPV": ((dt.date(2025, 10, 14), 400.0 / 664.9),),
}

#: overlapping sessions used to put the older daily history on the intraday price scale
SCALE_SESSIONS = 20

_NS_PER_DAY = 86_400 * 1_000_000_000
_EPOCH_ORDINAL = dt.date(1970, 1, 1).toordinal()


def is_junk_session(day: dt.date) -> bool:
    """True for a stored session that was not real continuous trading."""
    if day in JUNK_WEEKDAY_SESSIONS:
        return True
    return day.weekday() >= 5 and day not in REAL_WEEKEND_SESSIONS


def _day_numbers(index: pd.DatetimeIndex) -> np.ndarray:
    """The local calendar day of every bar, as days since 1970-01-01.

    Whole-number arithmetic on the wall-clock time: `index.normalize()` on a timezone-aware
    index costs about 15 times as much, and the desk runs this on every poll."""
    local = index.tz_localize(None) if index.tz is not None else index
    return np.asarray(local.as_unit("ns").asi8, dtype=np.int64) // _NS_PER_DAY


def drop_junk_sessions(df: pd.DataFrame) -> pd.DataFrame:
    """`df` without the bars of mock sessions (any resolution)."""
    if df is None or df.empty:
        return df
    day_no = _day_numbers(pd.DatetimeIndex(df.index))
    junk = [
        int(n)
        for n in np.unique(day_no)
        if is_junk_session(dt.date.fromordinal(_EPOCH_ORDINAL + int(n)))
    ]
    if not junk:
        return df
    return df[~np.isin(day_no, junk)]


def back_adjust_intraday(series_key: str, df: pd.DataFrame) -> pd.DataFrame:
    """Prices before an unadjusted demerger put on the scale after it."""
    steps = INTRADAY_BACK_ADJUST.get(series_key)
    if not steps or df is None or df.empty:
        return df
    tz = df.index.tz
    factor = np.ones(len(df))
    for ex_date, f in steps:
        factor[df.index < pd.Timestamp(ex_date, tz=tz)] *= f
    if (factor == 1.0).all():
        return df
    out = df.copy()
    for col in PRICE_COLUMNS:
        if col in out.columns:
            out[col] = out[col].to_numpy(dtype=float) * factor
    return out


def clean_intraday(series_key: str, df: pd.DataFrame) -> pd.DataFrame:
    """Intraday bars of any resolution without mock sessions, on one price scale."""
    return back_adjust_intraday(series_key, drop_junk_sessions(df))


def consistent_daily(stored_daily: pd.DataFrame, minute_bars: pd.DataFrame) -> pd.DataFrame:
    """Daily bars on the price scale of the (cleaned) intraday bars.

    From the first intraday session on, a day is the resample of its intraday bars, so a
    daily level can be compared with an intraday price. The stored daily history before that
    is kept as a warm-up for daily indicators, multiplied by the median ratio of the two
    series over the first `SCALE_SESSIONS` sessions they share.
    """
    if minute_bars is None or minute_bars.empty:
        return drop_junk_sessions(stored_daily) if stored_daily is not None else stored_daily
    derived = resample_bars(minute_bars, "1d")
    if stored_daily is None or stored_daily.empty:
        return derived
    stored = drop_junk_sessions(stored_daily)
    first = derived.index[0]
    older = stored[stored.index < first]
    if older.empty:
        return derived
    shared = stored.index.intersection(derived.index)[:SCALE_SESSIONS]
    scale = 1.0
    if len(shared):
        ratio = derived.loc[shared, "close"].to_numpy(float) / stored.loc[shared, "close"].to_numpy(
            float
        )
        ratio = ratio[np.isfinite(ratio) & (ratio > 0)]
        if len(ratio):
            scale = float(np.median(ratio))
    older = older.copy()
    if older.index.tz is not None and derived.index.tz is not None:
        # one zone object for both parts: pandas joins the same zone from two libraries
        # (pytz and zoneinfo) into an index of plain objects, which no time lookup accepts
        older.index = older.index.tz_convert(derived.index.tz)
    for col in PRICE_COLUMNS:
        if col in older.columns:
            older[col] = older[col].to_numpy(dtype=float) * scale
    cols = [c for c in derived.columns if c in older.columns]
    return pd.concat([older[cols], derived[cols]]).sort_index()
