"""The daily bars the pullback rule reads, and the test that they are fresh (US-067 D8).

The stored days (`desk.history.rule_daily`: built like the research's from the cleaned
one-minute bars) are joined with the days of the desk's own fine bars. A stored day stands
where the store has its one-minute bars to at least `last_bar_at`; a live day is used where
the store lacks the day or has it short (not the first live day, which may begin mid-session).
A day with a missing or non-positive open or close is left out and noted.

A session t is COMPLETE for a series when the clock is `settle` past the session's close and
t's last fine bar is at or after `last_bar_at` (15:10 by default: F&O stocks stop continuous
trading at 15:15 since 2026-08-03, so their last bar is 15:14; NIFTY's is 15:29). A day that is
not a calendar trading day (a special session: Muhurat, a Budget weekend) is complete the next
day whatever the time of its last bar. A calendar day whose bars stop early is incomplete; the
desk reads it for exit tests only once a later session is complete (`runner`).

An ENTRY then needs at least `min_bars` days up to t (the rule's own 201 by default) and no
calendar trading day missing among them (in the years the holiday calendar covers:
`config/nse_holidays.yaml` lists 2026 on, so an earlier holiday would otherwise read as a hole).
Anything else is a reason, shown on the stock's card and logged; no entry alert is raised. A
hole never stops the exit tests of an open trade (lead's rulings after the review).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from ..clock import IST, SessionClock, TradingCalendar
from ..data.hygiene import clean_intraday
from ..data.resample import resample_bars

#: the daily bars the rule reads at least (D8; the rule's own minimum, lead's ruling)
MIN_BARS = 201


@dataclass
class RuleHistory:
    """One series' days and the time of each day's last fine bar."""

    days: pd.DataFrame  # index: dates (dt.date), columns open/close
    last_bar: dict[dt.date, dt.time] = field(default_factory=dict)
    stored_to: dt.date | None = None  # the last day the store had
    #: stored days that come from stored daily bars only (no one-minute bars): a past date
    #: there is a whole session, as the research read it
    daily_only: frozenset[dt.date] = frozenset()
    #: days left out because a price is missing or not positive, with why
    bad: dict[dt.date, str] = field(default_factory=dict)

    def upto(self, day: dt.date) -> pd.DataFrame:
        return self.days[[d <= day for d in self.days.index]]


def _by_date(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["open", "close"])
    idx = pd.DatetimeIndex(frame.index)
    if idx.tz is not None:
        idx = idx.tz_convert(IST)
    out = pd.DataFrame(
        {"open": frame["open"].to_numpy(float), "close": frame["close"].to_numpy(float)},
        index=list(idx.date),
    )
    return out[~out.index.duplicated(keep="last")]


def join_live(
    series_key: str,
    stored: pd.DataFrame,
    stored_last_bar: dict[dt.date, dt.time],
    live_fine: pd.DataFrame | None,
    last_bar_at: dt.time = dt.time(15, 10),
) -> RuleHistory:
    """The stored days with the desk's live days laid on top (see the module docstring).

    A stored day stands where the store has that day's one-minute bars to at least
    `last_bar_at`, or the store has it as a stored daily bar only (a whole session); the live
    day is used where the store lacks the day or has it short, except the first live day,
    which may begin mid-session. A day whose open or close is missing or not positive is left
    out (`bad`), so it can never disable an average."""
    days = _by_date(stored)
    last = dict(stored_last_bar)
    stored_to = max(days.index) if len(days) else None
    if live_fine is not None and not live_fine.empty:
        fine = clean_intraday(series_key, live_fine)
        idx = pd.DatetimeIndex(fine.index).tz_convert(IST)
        live = _by_date(resample_bars(fine, "1d"))
        first = min(idx.date)
        stamps = pd.Series(idx, index=idx).groupby(idx.date).max()
        have = set(days.index)
        use = []
        for d in live.index:
            if d in have:
                whole = d not in stored_last_bar or stored_last_bar[d] >= last_bar_at
                if whole or d == first:
                    continue  # the store's day stands
            use.append(d)
        if use:
            days = pd.concat([days.drop(index=[d for d in use if d in have]), live.loc[use]])
            days = days[~days.index.duplicated(keep="last")].sort_index()
            for d in use:
                last[d] = stamps[d].time()
    o = pd.to_numeric(days["open"], errors="coerce").to_numpy(float)
    c = pd.to_numeric(days["close"], errors="coerce").to_numpy(float)
    good = np.isfinite(o) & np.isfinite(c) & (o > 0) & (c > 0)
    bad = {
        d: "a missing or non-positive open or close"
        for d, g in zip(days.index, good, strict=True)
        if not g
    }
    days = days[good]
    daily_only = frozenset(d for d in days.index if d not in last)
    return RuleHistory(days, last, stored_to, daily_only, bad)


def session_complete(
    day: dt.date,
    now: dt.datetime,
    last_bar: dt.time | None,
    clock: SessionClock,
    calendar: TradingCalendar,
    last_bar_at: dt.time,
    settle: dt.timedelta,
    daily_only: bool = False,
) -> bool:
    """Whether `day` is a completed session for a series whose last fine bar that day is
    `last_bar` (None: no bar). A stored daily bar with no fine bars (`daily_only`) is a whole
    session once its date is past."""
    now = now.astimezone(IST)
    if daily_only:
        return day < now.date()
    if last_bar is None:
        return False
    if not calendar.is_trading_day(day):
        # a special session (Muhurat, a Budget weekend): whole the next day, whatever the
        # time of its last bar (an afternoon Muhurat ends at 14:44)
        return now.date() > day
    if last_bar < last_bar_at:
        return False
    return now >= clock.window_for(day).close_at + settle


def problem(
    hist: RuleHistory, day: dt.date, calendar: TradingCalendar, min_bars: int = MIN_BARS
) -> str | None:
    """Why the rule cannot read an entry on `day` from `hist`, or None when it can. A day left
    out for a bad price (`hist.bad`) is noted elsewhere and is not a hole here: it must not
    silence the rule."""
    days = [d for d in hist.days.index if d <= day]
    if not days or days[-1] != day:
        return f"no daily bar for {day.isoformat()}"
    if len(days) < min_bars:
        return f"only {len(days)} daily bars up to {day.isoformat()}, {min_bars} needed"
    have = set(days)
    first = days[-min_bars]
    missing = []
    d = first
    while d <= day:
        # only years the holiday calendar covers: elsewhere a holiday would read as a hole
        if (
            calendar.has_year(d.year)
            and calendar.is_trading_day(d)
            and d not in have
            and d not in hist.bad
        ):
            missing.append(d)
        d += dt.timedelta(days=1)
    if missing:
        shown = ", ".join(m.isoformat() for m in missing[:3])
        more = f" and {len(missing) - 3} more" if len(missing) > 3 else ""
        return f"trading session(s) missing from the daily history: {shown}{more}"
    return None
