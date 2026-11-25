"""Clean one-minute bars for the micro-trend rule (US-097).

The micro-trend rule reads one-minute bars of the live source. On 23 Nov 2026 the desk started
on the delayed source (the token had expired), so its bars of the morning were two-minute bars
from Yahoo, and after the renewal the live bars were laid on top of them: an alert of that day
cannot be reproduced on the stored bars. So a slot whose strategy needs one-minute bars raises
no alert for an instrument while the bars it reads today are not all one-minute bars of the
live source:

* a bar of today that came from the delayed source (or from a source of coarser bars), unless a
  live bar of the same minute has replaced it since;
* a hole longer than `HOLE_MINUTES` before the signal (the session's open counts as a bar).

The desk records which bars of today came from where (`note_frame`); `problem` is pure. Nothing
here changes a bar, a verdict's prices or any other gate.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd  # type: ignore[import-untyped]

from ..clock import IST

HOLE_MINUTES = 5


@dataclass(frozen=True)
class Unclean:
    """Why today's bars are not clean, and from when on they are."""

    since: pd.Timestamp  # the end of the last unclean stretch: clean bars from here on
    detail: str

    def text(self) -> str:
        return f"waiting for clean one-minute bars since {self.since:%H:%M}: {self.detail}"


def _ist(ts: pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize(IST) if ts.tzinfo is None else ts.tz_convert(IST)


def note_frame(
    unclean: dict[pd.Timestamp, int],
    frame: pd.DataFrame | None,
    day: dt.date,
    clean: bool,
    width_min: int,
) -> dict[pd.Timestamp, int]:
    """The unclean bars of `day` after a polled `frame` was laid on the bars (the later frame
    wins on a shared minute, as `InstrumentTracker.update_bars` merges). `unclean` maps a bar's
    start to its width in minutes; entries of other days are dropped. Returns a new dict."""
    out = {ts: w for ts, w in unclean.items() if _ist(ts).date() == day}
    if frame is None or frame.empty:
        return out
    idx = pd.DatetimeIndex(frame.index)
    idx = idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)
    start = pd.Timestamp(dt.datetime.combine(day, dt.time(0)), tz=IST)
    today = list(idx[(idx >= start) & (idx < start + pd.Timedelta(days=1))])
    if clean:
        for ts in today:
            out.pop(ts, None)
    else:
        for ts in today:
            out[ts] = max(int(width_min), 1)
    return out


def problem(
    bars: pd.DataFrame | None,
    unclean: dict[pd.Timestamp, int],
    day: dt.date,
    until: pd.Timestamp,
    session_open: dt.time = dt.time(9, 15),
) -> Unclean | None:
    """Why the bars of `day` that close by `until` are not clean one-minute bars of the live
    source, or None when they are. The latest problem is the one named."""
    until = _ist(until)
    found: list[Unclean] = []
    day_start = pd.Timestamp(dt.datetime.combine(day, dt.time(0)), tz=IST)
    stamps: list[pd.Timestamp] = []
    if bars is not None and not bars.empty:
        idx = pd.DatetimeIndex(bars.index)
        idx = idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)
        end = min(until, day_start + pd.Timedelta(days=1))
        stamps = list(idx[(idx >= day_start) & (idx < end)])
    shown = set(stamps)
    bad = [(ts, w) for ts, w in ((_ist(t), w) for t, w in unclean.items()) if ts in shown]
    if bad:
        last, width = max(bad)
        found.append(
            Unclean(
                last + pd.Timedelta(minutes=width),
                "today's bars before that came from the delayed feed, not one-minute live bars",
            )
        )
    if stamps:
        prev = pd.Timestamp(dt.datetime.combine(day, session_open), tz=IST)
        first = True
        for ts in sorted(stamps):
            start = prev if first else prev + pd.Timedelta(minutes=1)  # the first minute missing
            if ts - start > pd.Timedelta(minutes=HOLE_MINUTES):
                found.append(Unclean(ts, f"no bar from {start:%H:%M} to {ts:%H:%M}"))
            prev, first = ts, False
    if not found:
        return None
    return max(found, key=lambda u: u.since)
