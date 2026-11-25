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
import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Protocol

import pandas as pd  # type: ignore[import-untyped]
from sqlalchemy import Engine, String, and_, func, select, type_coerce

from ..clock import IST
from ..data.hygiene import clean_intraday, consistent_daily, drop_junk_sessions, is_junk_session
from ..data.schema import bars as bars_table
from ..data.store import _read_bars, load_bars, series_generation, series_spans
from ..data.universe import load_specs
from ..mllab.service import join_bars

MINUTE = pd.Timedelta(minutes=1)
COLUMNS = ["open", "high", "low", "close", "volume"]
#: one-minute bars are read and kept in blocks of this many IST calendar days
BLOCK_DAYS = 30
#: the cap of all kept one-minute blocks (about 2 MB holds 150 days of one series)
MINUTE_CACHE_MB = 192.0
#: a stored trading day has at least this many one-minute bars (`GET /api/sim/days`)
MIN_DAY_BARS = 300
_NS = 1_000_000_000
_DAY_NS = 86_400 * _NS
_IST_NS = 19_800 * _NS


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


def day_number(ts: Any) -> int:
    """The IST calendar day of an instant, as days since 1970-01-01."""
    ns = int(_ts(ts).value)
    return (ns + _IST_NS) // _DAY_NS


def day_start(day: int) -> pd.Timestamp:
    """IST midnight that starts day number `day`."""
    return pd.Timestamp(day * _DAY_NS - _IST_NS, tz="UTC").tz_convert(IST)


def frame_nbytes(df: pd.DataFrame | None) -> int:
    if df is None:
        return 0
    return int(df.memory_usage(index=True, deep=False).sum())


class StoreBars:
    """The stored history, read through three caches (Simulator audit SIM-02, SIM-05, F3, F10):

    * one-minute bars in BLOCKS of `BLOCK_DAYS` IST calendar days, cleaned as the Lab cleans
      them (cleaning is bar by bar, so a frame joined from blocks equals one read at once),
      kept in one LRU across all series under a cap of `cap_mb` megabytes;
    * the daily bars of a series, computed once per data version;
    * the stored trading days of a series (`days`), once per data version.

    A series' data version is the number of writes to it in this process and its first and last
    stored bar (`data.store.series_spans`, which another process' writes reach within its TTL):
    when it moves, everything kept for that series is dropped. `reads` counts the store reads
    (the tests count them)."""

    def __init__(self, engine: Engine, cap_mb: float = MINUTE_CACHE_MB) -> None:
        self.engine = engine
        self.series, _ = load_specs()
        self.cap_bytes = int(cap_mb * 1024 * 1024)
        self.lock = threading.RLock()
        self.reads = 0
        self._blocks: OrderedDict[tuple[str, int], tuple[tuple, pd.DataFrame]] = OrderedDict()
        self._bytes = 0
        self._daily: dict[str, tuple[tuple, pd.DataFrame | None]] = {}
        self._days: dict[str, tuple[tuple, list[str]]] = {}

    def has_volume(self, series_key: str) -> bool:
        spec = self.series.get(series_key)
        return bool(spec.has_volume) if spec is not None else True

    # ── the data version ──────────────────────────────────────────────────
    def version(self, series_key: str) -> tuple:
        spans = series_spans(self.engine, series_key)
        return (series_generation(series_key), spans.get("1m"), spans.get("1d"))

    def cached_bytes(self) -> int:
        with self.lock:
            return self._bytes

    def _drop_series(self, series_key: str) -> None:
        for k in [k for k in self._blocks if k[0] == series_key]:
            self._bytes -= frame_nbytes(self._blocks.pop(k)[1])
        self._daily.pop(series_key, None)
        self._days.pop(series_key, None)

    # ── one-minute bars ───────────────────────────────────────────────────
    def _read_block(self, series_key: str, block: int) -> pd.DataFrame:
        lo = day_start(block * BLOCK_DAYS)
        hi = day_start((block + 1) * BLOCK_DAYS)
        self.reads += 1
        raw = load_bars(self.engine, series_key, "1m", lo, hi)
        return _tidy(clean_intraday(series_key, raw), self.has_volume(series_key))

    def _block(self, series_key: str, block: int, version: tuple) -> pd.DataFrame:
        key = (series_key, block)
        with self.lock:
            hit = self._blocks.get(key)
            if hit is not None and hit[0] == version:
                self._blocks.move_to_end(key)
                return hit[1]
        df = self._read_block(series_key, block)
        with self.lock:
            old = self._blocks.pop(key, None)
            if old is not None:
                self._bytes -= frame_nbytes(old[1])
            self._blocks[key] = (version, df)
            self._bytes += frame_nbytes(df)
            while self._bytes > self.cap_bytes and len(self._blocks) > 1:
                _, (_, gone) = self._blocks.popitem(last=False)
                self._bytes -= frame_nbytes(gone)
        return df

    def minute_bars(
        self, series_key: str, start: pd.Timestamp | None, end: pd.Timestamp | None
    ) -> pd.DataFrame:
        version = self.version(series_key)
        span = version[1]
        with self.lock:
            stale = [k for k, v in self._blocks.items() if k[0] == series_key and v[0] != version]
            if stale:
                self._drop_series(series_key)
        if span is None:
            return _empty()
        first, last = _ts(span[0]), _ts(span[1])
        lo = first if start is None else max(_ts(start), first)
        hi = last + MINUTE if end is None else min(_ts(end), last + MINUTE)
        if lo >= hi:
            return _empty()
        b0 = day_number(lo) // BLOCK_DAYS
        b1 = day_number(hi - pd.Timedelta(microseconds=1)) // BLOCK_DAYS
        parts = [self._block(series_key, b, version) for b in range(b0, b1 + 1)]
        parts = [p for p in parts if not p.empty]
        if not parts:
            return _empty()
        df = parts[0] if len(parts) == 1 else pd.concat(parts)
        idx = df.index
        i = int(idx.searchsorted(lo, side="left"))
        j = int(idx.searchsorted(hi, side="left"))
        return df.iloc[i:j]

    # ── daily bars ────────────────────────────────────────────────────────
    def daily_bars(self, series_key: str) -> pd.DataFrame | None:
        """The stored daily bars without mock sessions; for a series with none stored, the
        days of its last 400 calendar days of one-minute bars. Once per data version. (The
        frame a strategy reads is `consistent_daily` of these and the session's own minutes,
        `engine._daily_for`: the days before the session's first minute come from here.)"""
        version = self.version(series_key)
        with self.lock:
            hit = self._daily.get(series_key)
            if hit is not None and hit[0] == version:
                return hit[1]
        avail = series_spans(self.engine, series_key)
        out: pd.DataFrame | None
        if "1d" in avail:
            self.reads += 1
            stored = _read_bars(self.engine, series_key, "1d")
            out = drop_junk_sessions(stored) if not stored.empty else None
        elif "1m" in avail:
            lo, hi = avail["1m"][0], avail["1m"][1]
            mins = self.minute_bars(series_key, _ts(max(lo, hi - dt.timedelta(days=400))), None)
            out = consistent_daily(pd.DataFrame(), mins) if not mins.empty else None
        else:
            out = None
        with self.lock:
            self._daily[series_key] = (version, out)
        return out

    def minute_range(self, series_key: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
        got = series_spans(self.engine, series_key).get("1m")
        if got is None:
            return None
        return _ts(got[0]), _ts(got[1])

    # ── the stored trading days ───────────────────────────────────────────
    def days(self, series_key: str) -> list[str]:
        """The days (YYYY-MM-DD, IST) with at least `MIN_DAY_BARS` one-minute bars, oldest
        first: one grouped read of the series' index, once per data version."""
        version = self.version(series_key)
        with self.lock:
            hit = self._days.get(series_key)
            if hit is not None and hit[0] == version:
                return list(hit[1])
        out = stored_days(self.engine, series_key)
        self.reads += 1
        with self.lock:
            self._days[series_key] = (version, out)
        return list(out)


def stored_days(engine: Engine, series_key: str, min_bars: int = 0) -> list[str]:
    """Days with at least `min_bars` (default `MIN_DAY_BARS`) stored one-minute bars, oldest
    first. Bar times are stored in UTC; an NSE or BSE session (09:15 to 15:30 IST, 03:45 to
    10:00 UTC) falls on the same calendar date in both zones, so the date is the text's first
    ten characters."""
    need = min_bars or MIN_DAY_BARS
    day = func.substr(type_coerce(bars_table.c.ts, String), 1, 10)
    q = (
        select(day.label("d"), func.count().label("n"))
        .where(and_(bars_table.c.series_key == series_key, bars_table.c.resolution == "1m"))
        .group_by(day)
        .order_by(day)
    )
    with engine.connect() as conn:
        rows = conn.execute(q).fetchall()
    return [
        str(d)
        for d, n in rows
        if int(n) >= need and not is_junk_session(dt.date.fromisoformat(str(d)))
    ]


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
