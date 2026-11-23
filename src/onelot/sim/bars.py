"""Where the Simulator's one-minute bars come from.

The engine never holds a database handle of its own: it asks a `BarSource`.

* `StoreBars`: the stored bars (`data.store.load_bars`), cleaned the way the Lab's `DataCache`
  cleans them (`data.hygiene.clean_intraday`, `consistent_daily`). Any SQLAlchemy engine
  works, a read-only one included: nothing is written.
* `DeskBars`: mode `live`. The running desk's tracker bars joined with the store's
  (`mllab.service.join_bars`, as the Machine Learning Lab does), and only CLOSED one-minute
  bars: a bar counts once the clock has passed its end.

Every frame is indexed by the bar START in IST, oldest first, with open, high, low, close and
volume columns.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any, Protocol

import pandas as pd  # type: ignore[import-untyped]
from sqlalchemy import Engine

from ..clock import IST
from ..data.hygiene import clean_intraday, consistent_daily, drop_junk_sessions
from ..data.store import _read_bars, available_resolutions, load_bars
from ..data.universe import load_specs
from ..mllab.service import join_bars

MINUTE = pd.Timedelta(minutes=1)
COLUMNS = ["open", "high", "low", "close", "volume"]


class BarSource(Protocol):
    def minute_bars(
        self, series_key: str, start: pd.Timestamp | None, end: pd.Timestamp | None
    ) -> pd.DataFrame:
        """Cleaned one-minute bars with start in [start, end) (None: open-ended)."""
        ...

    def daily_bars(self, series_key: str) -> pd.DataFrame | None:
        """Daily bars on the price scale of the minute bars, or None."""
        ...

    def minute_range(self, series_key: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
        """The first and last one-minute bar this source has, or None."""
        ...


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz=IST, name="ts"))


def _tidy(df: pd.DataFrame | None, has_volume: bool) -> pd.DataFrame:
    if df is None or df.empty:
        return _empty()
    out = df[[c for c in COLUMNS if c in df.columns]].copy()
    if "volume" not in out.columns:
        out["volume"] = 0.0
    out.index = pd.DatetimeIndex(out.index).tz_convert(IST)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    if not has_volume:
        # an index has no traded volume; the Lab's DataCache and the desk zero it the same way
        out = out.assign(volume=0.0)
    return out.astype(float)


def _ts(value: Any) -> pd.Timestamp:
    t = pd.Timestamp(value)
    return t.tz_localize(IST) if t.tzinfo is None else t.tz_convert(IST)


class StoreBars:
    """The stored history. Minute frames read are kept, so the daily bars can be built on
    the same scale without reading the minutes twice."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.series, _ = load_specs()
        self._minutes: dict[str, pd.DataFrame] = {}
        self._daily: dict[str, tuple[int, pd.DataFrame | None]] = {}

    def has_volume(self, series_key: str) -> bool:
        spec = self.series.get(series_key)
        return bool(spec.has_volume) if spec is not None else True

    def minute_bars(
        self, series_key: str, start: pd.Timestamp | None, end: pd.Timestamp | None
    ) -> pd.DataFrame:
        raw = load_bars(self.engine, series_key, "1m", start, end)
        df = _tidy(clean_intraday(series_key, raw), self.has_volume(series_key))
        seen = self._minutes.get(series_key)
        self._minutes[series_key] = df if seen is None else join_bars(seen, df)  # type: ignore[assignment]
        return df

    def daily_bars(self, series_key: str) -> pd.DataFrame | None:
        minutes = self._minutes.get(series_key)
        n = 0 if minutes is None else len(minutes)
        hit = self._daily.get(series_key)
        if hit is not None and hit[0] == n:
            return hit[1]
        avail = available_resolutions(self.engine, series_key)
        stored = _read_bars(self.engine, series_key, "1d") if "1d" in avail else pd.DataFrame()
        out: pd.DataFrame | None
        if minutes is not None and not minutes.empty:
            out = consistent_daily(stored, minutes)
        elif not stored.empty:
            out = drop_junk_sessions(stored)
        elif "1m" in avail:
            lo, hi = avail["1m"][0], avail["1m"][1]
            mins = self.minute_bars(series_key, _ts(max(lo, hi - dt.timedelta(days=400))), None)
            out = consistent_daily(pd.DataFrame(), mins) if not mins.empty else None
            n = len(self._minutes.get(series_key, mins))
        else:
            out = None
        self._daily[series_key] = (n, out)
        return out

    def minute_range(self, series_key: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
        got = available_resolutions(self.engine, series_key).get("1m")
        if got is None:
            return None
        return _ts(got[0]), _ts(got[1])


class DeskBars:
    """Mode `live`: the desk tracker's one-minute bars on top of the store's, closed bars
    only. `clock()` is the present; a bar starting at t is closed once t + 1 minute has
    passed. The stored part is read once per series and kept."""

    def __init__(
        self,
        desk: Any,
        clock: Callable[[], dt.datetime],
        store: BarSource | None = None,
    ) -> None:
        self.desk = desk
        self.clock = clock
        self.store = store
        self._stored: dict[str, pd.DataFrame] = {}

    def _tracker(self, series_key: str) -> Any:
        trackers = getattr(self.desk, "trackers", None) or {}
        return trackers.get(series_key)

    def _live(self, series_key: str) -> pd.DataFrame | None:
        t = self._tracker(series_key)
        if t is None or getattr(t, "fine_minutes", 1) != 1:
            return None
        raw = getattr(t, "bars", None)
        if raw is None or raw.empty:
            return None
        has_volume = bool(getattr(t.series, "has_volume", True))
        return _tidy(clean_intraday(t.series.key, raw), has_volume)

    def _closed(self, df: pd.DataFrame) -> pd.DataFrame:
        now = _ts(self.clock())
        return df[df.index + MINUTE <= now]

    def minute_bars(
        self, series_key: str, start: pd.Timestamp | None, end: pd.Timestamp | None
    ) -> pd.DataFrame:
        stored = self._stored.get(series_key)
        if stored is None and self.store is not None:
            # the history under the live bars: from `start` to now, read once
            stored = self.store.minute_bars(series_key, start, None)
            self._stored[series_key] = stored
        joined = join_bars(stored, self._live(series_key))
        if joined is None or joined.empty:
            return _empty()
        df = self._closed(joined)
        if start is not None:
            df = df[df.index >= _ts(start)]
        if end is not None:
            df = df[df.index < _ts(end)]
        return df

    def daily_bars(self, series_key: str) -> pd.DataFrame | None:
        t = self._tracker(series_key)
        history = getattr(t, "daily_history", None) if t is not None else None
        live = self._live(series_key)
        stored = self._stored.get(series_key)
        fine = join_bars(stored, live)
        if history is not None and not history.empty:
            return consistent_daily(history, fine if fine is not None else pd.DataFrame())
        if self.store is not None:
            return self.store.daily_bars(series_key)
        if fine is not None and not fine.empty:
            return consistent_daily(pd.DataFrame(), fine)
        return None

    def minute_range(self, series_key: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
        """The store's first bar to the last closed live one (the store's alone without)."""
        stored = self.store.minute_range(series_key) if self.store is not None else None
        live = self._live(series_key)
        live = self._closed(live) if live is not None else None
        if live is None or live.empty:
            return stored
        first = pd.Timestamp(live.index[0]) if stored is None else min(stored[0], live.index[0])
        return pd.Timestamp(first), pd.Timestamp(live.index[-1])
