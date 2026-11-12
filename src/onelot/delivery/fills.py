"""Where a delivery trade's entry or exit fills on the desk's fine bars (US-067 D3, D5). Pure.

The next session is read from the bars, not the calendar: the first date in the bars after the
signal date. So a holiday is skipped and a special session (a Muhurat or Budget day) is a
session, as in the research's daily bars.

- The desk ran at that session's open (its process started at or before the first bar): the
  fill is that bar's open, late only when that bar is more than two minutes after the
  session's opening minute (a feed gap).
- It started during that session: the first bar that opens at or after the start, marked late.
- It started after that session was over: an entry is MISSED ("the desk was not running"); an
  exit is never missed, it sells at the first bar after the start, marked late.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd  # type: ignore[import-untyped]

from ..clock import IST


@dataclass(frozen=True)
class Fill:
    ts: dt.datetime  # the bar's open time (IST)
    session: dt.date
    open_: float  # the bar's open, before slippage
    late: bool


@dataclass(frozen=True)
class Missed:
    session: dt.date
    why: str = "missed: the desk was not running"


def _days(bars: pd.DataFrame) -> list[dt.date]:
    return sorted(set(pd.DatetimeIndex(bars.index).date))


def decide(
    bars: pd.DataFrame | None,
    after: dt.date,
    started_at: dt.datetime,
    complete: Callable[[dt.date], bool],
    *,
    may_miss: bool,
    opening: Callable[[dt.date], dt.datetime | None] | None = None,
    given_at: dt.datetime | None = None,
) -> Fill | Missed | None:
    """The fill of an order "at the next open" after the close of session `after`, or Missed,
    or None (wait). `bars`: the desk's fine bars (tz-aware index, an `open` column).
    `complete(day)` says whether that session is over (no later bar can belong to it)."""
    if bars is None or bars.empty:
        return None
    idx = pd.DatetimeIndex(bars.index)
    if idx.tz is None:
        raise ValueError("delivery: the fine bars must be tz-aware")
    idx = idx.tz_convert(IST)
    later = [d for d in _days(bars) if d > after]
    if not later:
        return None
    s1 = later[0]
    dates = idx.date
    first = int(next(i for i, d in enumerate(dates) if d == s1))
    start = pd.Timestamp(started_at).tz_convert(IST)
    if given_at is not None and start <= idx[first] < pd.Timestamp(given_at).tz_convert(IST):
        # the desk ran at that open but the order was given after it (a day read late): the
        # first bar after the order, late; never an open already past
        start = pd.Timestamp(given_at).tz_convert(IST)
    if start <= idx[first]:
        ts = idx[first].to_pydatetime()
        return Fill(ts, s1, float(bars["open"].iloc[first]), _gap(ts, s1, opening))
    after_start = [i for i in range(first, len(idx)) if idx[i] >= start]
    in_s1 = [i for i in after_start if dates[i] == s1]
    if in_s1:
        i = in_s1[0]
        return Fill(idx[i].to_pydatetime(), s1, float(bars["open"].iloc[i]), True)
    s1_over = complete(s1) or any(d > s1 for d in dates)
    if not s1_over:
        return None  # the desk started during s1 and its next bar has not come yet
    if may_miss:
        return Missed(s1)
    if after_start:
        i = after_start[0]
        return Fill(idx[i].to_pydatetime(), dates[i], float(bars["open"].iloc[i]), True)
    return None


#: a fill bar more than this after the session's opening minute is late, whatever the cause
LATE_AFTER = dt.timedelta(minutes=2)


def _gap(
    ts: dt.datetime, session: dt.date, opening: Callable[[dt.date], dt.datetime | None] | None
) -> bool:
    """True when the session's first bar the desk has is more than `LATE_AFTER` after the
    session's opening minute (a feed gap): late as surely as a late start."""
    at = opening(session) if opening is not None else None
    return at is not None and ts > at + LATE_AFTER


def gap_warning(bars: pd.DataFrame | None, since: dt.date, limit: float = 0.20) -> str | None:
    """D15: the first session after `since` whose first bar opens more than `limit` away from
    the previous session's last close, as the warning the trade shows; None if there is none."""
    if bars is None or bars.empty:
        return None
    idx = pd.DatetimeIndex(bars.index).tz_convert(IST)
    frame = pd.DataFrame(
        {"open": bars["open"].to_numpy(float), "close": bars["close"].to_numpy(float)},
        index=idx,
    )
    by_day = frame.groupby(idx.date)
    first_open = by_day["open"].first()
    last_close = by_day["close"].last()
    days = list(first_open.index)
    for prev, day in zip(days, days[1:], strict=False):
        if day <= since:
            continue
        before = float(last_close[prev])
        if before > 0 and abs(float(first_open[day]) / before - 1.0) > limit:
            return f"possible corporate action on {day.isoformat()}: check the figures"
    return None
