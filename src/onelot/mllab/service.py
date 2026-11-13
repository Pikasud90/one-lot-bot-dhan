"""The Machine Learning Lab's matrix: every tracked instrument x the five models, on one
timeframe, for the NEXT bar.

Bars: the desk's own fine bars resampled with the project's resampler (bins from 09:15),
and for `1d` the tracker's daily history laid under the live days (`consistent_daily`, as
the trend ladder builds them). Only CLOSED bars are used: a bar counts from its nominal
close (`indicators/mtf.nominal_close_ns`: start + length, a day at the next midnight).

Cache: one row per (instrument, timeframe), kept until that series' last closed bar
changes, so a poll with no new bar runs no model. The work runs in the request thread on
a snapshot of the bars (references to the tracker's own frames, taken without the desk
lock); it never blocks a desk poll. A failure in one instrument or model gives that cell
a `note`, never a failed matrix. It informs; it raises no alert and no trade.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import threading
from collections.abc import Callable, Iterable
from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from ..clock import IST
from ..data.hygiene import clean_intraday, consistent_daily
from ..data.resample import resample_bars
from ..indicators.mtf import index_ns, nominal_close_ns
from ..strategies.mtf_support import fingerprint
from . import models as m

log = logging.getLogger(__name__)

TIMEFRAMES: tuple[str, ...] = ("15m", "30m", "60m", "4h", "1d")
_RULE = {"15m": "15m", "30m": "30m", "60m": "60m", "4h": "240m", "1d": "1d"}
_STEP = {"15m": 15, "30m": 30, "60m": 60, "4h": 240}
BACKTEST_BARS = 100
#: closes a model is given at most (the models fit on the last 1000 returns anyway)
MAX_BARS = 1200

#: (fine bars, daily bars) of an instrument, or (None, None)
BarsGetter = Callable[[str], tuple[pd.DataFrame | None, pd.DataFrame | None]]
#: (instrument key, now) -> the store's cleaned one-minute bars before the live ones
StoredReader = Callable[[str, dt.datetime], pd.DataFrame | None]
#: timeframes built from the stored one-minute bars joined with the live ones: the desk
#: keeps about 25 sessions; the store gives every intraday timeframe about 120 sessions, so
#: about 120 opening candles for the "open" series and enough bars for the fitted models
STORED_TIMEFRAMES: frozenset[str] = frozenset({"15m", "30m", "60m", "4h"})
#: calendar days of stored one-minute bars read for them
STORED_DAYS = 180
#: a failed stored read is tried again after this long (a good one is kept for the day)
STORED_RETRY = dt.timedelta(minutes=30)


def join_bars(stored: pd.DataFrame | None, live: pd.DataFrame | None) -> pd.DataFrame | None:
    """Stored and live bars as one sorted series; the live bar wins a timestamp both have."""
    parts = [f for f in (stored, live) if f is not None and not f.empty]
    if not parts:
        return live
    if len(parts) == 1:
        return parts[0]
    both = pd.concat(parts)
    return both[~both.index.duplicated(keep="last")].sort_index()


#: when a session ends, by date (the desk's session clock; 15:30 IST without one)
SessionClose = Callable[[dt.date], dt.datetime]
#: after a session's close, its last bar counts as closed once the fine bars reach the close
#: or, for a feed that stopped a little early (no trade in the last minute), this much later
SESSION_SETTLE = dt.timedelta(minutes=10)


def nse_close(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(15, 30), tzinfo=IST)


def _ns(ts: Any) -> int:
    t = pd.Timestamp(ts)
    return int((t if t.tzinfo else t.tz_localize("UTC")).as_unit("ns").value)


def closed_bars(
    fine: pd.DataFrame | None,
    daily: pd.DataFrame | None,
    timeframe: str,
    now: dt.datetime,
    session_close: SessionClose | None = None,
) -> pd.DataFrame:
    """The bars of `timeframe` closed at `now`, oldest first.

    A bar is closed when the clock and the fine bars have both reached its nominal end (a
    late feed never closes a bar early), OR when the session it belongs to is over: a past
    date, or `now` past that session's close with the fine bars reaching the close (or
    `SESSION_SETTLE` later). So after 15:30 the day's last, shorter bar is in the series."""
    close_of = session_close or nse_close
    now_ns = _ns(now)
    data_end: int | None = None
    if fine is not None and not fine.empty:
        fine_ns = index_ns(pd.DatetimeIndex(fine.index).sort_values())
        gaps = np.diff(fine_ns[-50:])
        step = int(gaps[gaps > 0].min()) if np.any(gaps > 0) else 60 * 10**9
        data_end = int(fine_ns[-1]) + step
    if timeframe == "1d":
        bars = daily if daily is not None and not daily.empty else None
        if bars is None and fine is not None and not fine.empty:
            bars = resample_bars(fine, "1d")
        t_ns = now_ns
    else:
        bars = (
            resample_bars(fine, _RULE[timeframe]) if fine is not None and not fine.empty else None
        )
        t_ns = min(now_ns, data_end) if data_end is not None else now_ns
    if bars is None or bars.empty:
        return pd.DataFrame(columns=["close"])
    idx = pd.DatetimeIndex(bars.index)
    done = nominal_close_ns(idx, _RULE[timeframe]) <= t_ns
    days = (idx.tz_convert(IST) if idx.tz is not None else idx).date
    at = pd.Timestamp(now)
    today = (at.tz_convert(IST) if at.tzinfo else at).date()
    over: dict[dt.date, bool] = {}
    for d in set(days):
        if d < today:
            over[d] = True
            continue
        c = _ns(close_of(d))
        over[d] = now_ns >= c and (
            (data_end is not None and data_end >= c)
            or now_ns >= c + int(SESSION_SETTLE.total_seconds() * 1e9)
        )
    done |= np.array([over[d] for d in days], dtype=bool)
    return bars[done].sort_index()


def next_bar_start(
    last_ts: pd.Timestamp,
    timeframe: str,
    session_close: SessionClose | None = None,
    is_trading_day: Callable[[dt.date], bool] | None = None,
) -> pd.Timestamp:
    """When the bar after `last_ts` starts: the next bin, or the next session's 09:15 when
    the session ends first (weekends skipped, and exchange holidays when a calendar is given)."""
    ts = pd.Timestamp(last_ts)
    close = pd.Timestamp((session_close or nse_close)(ts.date()))
    if timeframe != "1d":
        nxt = ts + pd.Timedelta(minutes=_STEP[timeframe])
        if nxt < close and nxt.date() == ts.date():
            return nxt
    nxt = ts.normalize() + pd.Timedelta(days=1)
    for _ in range(30):  # a long run of holidays is a handful of days
        if nxt.weekday() < 5 and (is_trading_day is None or is_trading_day(nxt.date())):
            break
        nxt += pd.Timedelta(days=1)
    return nxt.normalize() + pd.Timedelta(hours=9, minutes=15)


def _num(v: Any) -> float | None:
    """A finite float or None (the API sends strict JSON)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _cell(closes: Any, model: str, season: int) -> dict[str, Any]:
    try:
        fc = m.forecast(model, closes, season).as_dict()
    except Exception as exc:  # noqa: BLE001 - one cell, never the matrix
        log.exception("mllab: %s failed", model)
        fc = m.Forecast(note=f"{model} failed: {type(exc).__name__}").as_dict()
    cell = {k: (_num(v) if isinstance(v, float) else v) for k, v in fc.items()}
    if fc.get("note") is None:
        try:
            bt = m.backtest(closes, model, BACKTEST_BARS, season)
        except Exception as exc:  # noqa: BLE001
            log.exception("mllab: backtest of %s failed", model)
            bt = {"steps": 0, "in68": None, "in95": None, "direction": None}
            cell["backtest_note"] = f"backtest failed: {type(exc).__name__}"
        cell["backtest"] = {k: _num(v) if k != "steps" else int(v) for k, v in bt.items()}
    else:
        cell["backtest"] = None
    return cell


def _ist_dates(index: Any) -> np.ndarray:
    idx = pd.DatetimeIndex(index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx
    return np.asarray(idx.tz_convert(IST).date)


def regime_closes(bars: pd.DataFrame, timeframe: str, next_ts: Any) -> tuple[np.ndarray, int, str]:
    """The closes the models are given for the NEXT bar, the seasonal lag, and the regime:
    "daily" (1d: the closes as they are), "open" (the next bar is a session's first: only the
    returns INTO first bars), "ordinary" (only the returns into the other bars)."""
    closes = bars["close"].to_numpy(dtype=float)
    ok = np.isfinite(closes) & (closes > 0)
    closes = closes[ok]
    if timeframe == "1d":
        return closes[-MAX_BARS:], m.SEASON["1d"], "daily"
    if len(closes) < 2:
        return closes[-MAX_BARS:], max(m.SEASON[timeframe] - 1, 1), "ordinary"
    days = _ist_dates(bars.index[ok])
    r = np.diff(np.log(closes))
    into_first = days[1:] != days[:-1]
    nxt = pd.Timestamp(next_ts)
    nxt = nxt.tz_localize("UTC") if nxt.tzinfo is None else nxt
    opens = nxt.tz_convert(IST).date() != days[-1]
    if opens:
        sel, season, regime = r[into_first], 5, "open"
    else:
        sel, season, regime = r[~into_first], max(m.SEASON[timeframe] - 1, 1), "ordinary"
    last = float(closes[-1])
    synth = last * np.exp(np.r_[0.0, np.cumsum(sel)] - sel.sum())
    synth[-1] = last
    return synth[-MAX_BARS:], season, regime


def move_state(closes: Any) -> dict[str, Any] | None:
    """How big the next move is likely to be against this series' own history: the EWMA
    sd now, ranked among its past values (calm: lowest third, active: highest third)."""
    try:
        c = np.asarray(closes, dtype=float)
        r = np.diff(np.log(c))
        r = r[np.isfinite(r)]
        if len(r) < 80:
            return None
        path = m.ewma_sd_path(r)
        cur = float(path[-1])
        pct = float(np.mean(path[20:-1] <= cur))
        label = "calm" if pct <= 1 / 3 else "active" if pct >= 2 / 3 else "normal"
        sd_bp = cur * 1e4
        if not (math.isfinite(pct) and math.isfinite(sd_bp)):
            return None
        return {"label": label, "pct": pct, "sd_bp": sd_bp}
    except Exception:  # noqa: BLE001 - never the row
        log.exception("mllab: move state failed")
        return None


class MlLab:
    """`matrix(timeframe)`: one row per instrument, one cell per model."""

    def __init__(
        self,
        bars_getter: BarsGetter,
        instruments: Callable[[], Iterable[dict[str, Any]]] | Iterable[dict[str, Any]],
        clock: Callable[[], dt.datetime],
        stored_reader: StoredReader | None = None,
        session_close: SessionClose | None = None,
        is_trading_day: Callable[[dt.date], bool] | None = None,
    ) -> None:
        self.bars_getter = bars_getter
        self.session_close = session_close  # None: 15:30 IST
        self.is_trading_day = is_trading_day  # None: weekdays only
        self.stored_reader = stored_reader
        #: key -> (day read, the stored bars or None, a note when the read failed, when)
        self._stored: dict[str, tuple[dt.date, pd.DataFrame | None, str | None, dt.datetime]] = {}
        self._joined: dict[str, tuple[tuple, pd.DataFrame | None]] = {}
        self.stored_reads = 0  # reads of the store (for tests)
        self._instruments = instruments
        self.clock = clock
        self._cache: dict[tuple[str, str], tuple[Any, dict[str, Any]]] = {}
        self._lock = threading.Lock()  # one matrix at a time: two tabs never compute twice
        self.model_runs = 0  # rows computed (for tests and the log)

    def instruments(self) -> list[dict[str, Any]]:
        src = self._instruments() if callable(self._instruments) else self._instruments
        rows = list(src)
        return sorted(rows, key=lambda r: 0 if r.get("kind") == "INDEX" else 1)  # stable

    def matrix(self, timeframe: str) -> dict[str, Any]:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"unknown timeframe {timeframe}; one of {', '.join(TIMEFRAMES)}")
        now = self.clock()
        with self._lock:
            rows = [self._row(inst, timeframe, now) for inst in self.instruments()]
        return {
            "timeframe": timeframe,
            "generated_at": pd.Timestamp(now).isoformat(),
            "models": [{"key": k, "description": m.DESCRIPTIONS[k]} for k in m.MODELS],
            "rows": rows,
        }

    def _row(self, inst: dict[str, Any], timeframe: str, now: dt.datetime) -> dict[str, Any]:
        key = str(inst.get("key"))
        head = {"key": key, "label": inst.get("label") or key, "kind": inst.get("kind")}
        note = None
        try:
            fine, daily = self.bars_getter(key)
            if timeframe in STORED_TIMEFRAMES and self.stored_reader is not None:
                fine, note = self._with_stored(key, fine, now)
            bars = closed_bars(fine, daily, timeframe, now, self.session_close)
        except Exception as exc:  # noqa: BLE001 - one row, never the matrix
            log.exception("mllab: bars of %s failed", key)
            return self._empty(head, f"bars unavailable: {type(exc).__name__}")
        if bars.empty:
            return self._empty(head, "no closed bars yet")
        last_ts = pd.Timestamp(bars.index[-1])
        # the last closed bar, and the count (stored days arriving later lengthen the series)
        stamp = (int(index_ns(pd.DatetimeIndex([last_ts]))[0]), len(bars))
        hit = self._cache.get((key, timeframe))
        if hit is not None and hit[0] == stamp:
            return {**hit[1], **head, "note": note}
        next_ts = next_bar_start(last_ts, timeframe, self.session_close, self.is_trading_day)
        real = bars["close"].to_numpy(dtype=float)
        closes, season, regime = regime_closes(bars, timeframe, next_ts)
        row = {
            **head,
            "last_close": _num(real[-1]),
            "last_bar_ts": last_ts.isoformat(),
            "next_bar_ts": next_ts.isoformat(),
            "bars": len(closes),  # closes the forecasts used
            "regime": regime,
            "state": move_state(closes),
            "note": note,
            "cells": {model: _cell(closes, model, season) for model in m.MODELS},
        }
        self.model_runs += 1
        self._cache[(key, timeframe)] = (stamp, row)
        return row

    @staticmethod
    def _empty(head: dict[str, Any], note: str) -> dict[str, Any]:
        cell = {"note": note, "backtest": None}
        return {
            **head,
            "last_close": None,
            "last_bar_ts": None,
            "next_bar_ts": None,
            "bars": 0,
            "regime": None,
            "state": None,
            "note": None,
            "cells": {model: dict(cell) for model in m.MODELS},
        }

    def _with_stored(
        self, key: str, live: pd.DataFrame | None, now: dt.datetime
    ) -> tuple[pd.DataFrame | None, str | None]:
        """The live fine bars joined with the store's, and a note when the store failed.
        The store is read lazily, once a day per instrument, in this (request) thread: never
        under the desk lock, and never written."""
        today = pd.Timestamp(now).date()
        got = self._stored.get(key)
        stale = (
            got is None or got[0] != today or (got[2] is not None and now - got[3] >= STORED_RETRY)
        )
        if stale:
            self.stored_reads += 1
            try:
                frame = self.stored_reader(key, now)  # type: ignore[misc]
                got = (today, frame, None, now)
            except Exception as exc:  # noqa: BLE001 - the live bars alone, with a note
                log.warning("mllab: stored bars of %s could not be read: %s", key, exc)
                why = f"stored bars unreadable ({type(exc).__name__}): live bars only"
                got = (today, None, why, now)
            self._stored[key] = got
        assert got is not None
        stored, note = got[1], got[2]
        mark = (id(stored), *fingerprint(live))
        hit = self._joined.get(key)
        if hit is None or hit[0] != mark:
            hit = (mark, join_bars(stored, live))
            self._joined[key] = hit
        return hit[1], note


def desk_bars_getter(desk: Any) -> BarsGetter:
    """Bars of a desk tracker the way the desk itself reads them from another thread: a
    reference to the tracker's own frames (`tracker.bars`, `tracker.daily_history`, which
    a poll replaces, never mutates), cleaned like the trend ladder's (`clean_intraday`,
    `consistent_daily`). The cleaned frames are kept until the raw ones change."""
    memo: dict[str, tuple[tuple, tuple[pd.DataFrame | None, pd.DataFrame | None]]] = {}

    def get(key: str) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
        t = desk.trackers.get(key)
        if t is None:
            return None, None
        raw, history = t.bars, t.daily_history
        stamp = (*fingerprint(raw), *fingerprint(history))
        hit = memo.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        fine = clean_intraday(t.series.key, raw) if raw is not None and not raw.empty else None
        daily = None
        if history is not None and not history.empty:
            daily = consistent_daily(history, fine if fine is not None else pd.DataFrame())
        memo[key] = (stamp, (fine, daily))
        return memo[key][1]

    return get


def desk_instruments(desk: Any) -> list[dict[str, Any]]:
    """The desk's tracked instruments (indices and stocks), in the desk's own order."""
    return [
        {"key": k, "label": t.series.label, "kind": t.cfg.kind}
        for k, t in list(desk.trackers.items())
    ]


def desk_stored_reader(desk: Any, days: int = STORED_DAYS) -> StoredReader:
    """The store's one-minute bars of a desk instrument for the last `days` calendar days,
    cleaned like the live ones (`clean_intraday`). One indexed range read; nothing written."""
    from ..data.store import _read_bars

    def read(key: str, now: dt.datetime) -> pd.DataFrame | None:
        t = desk.trackers.get(key)
        if t is None:
            return None
        at = pd.Timestamp(now)
        raw = _read_bars(
            desk.engine, t.series.key, "1m", start=at - pd.Timedelta(days=days), end=at
        )
        return clean_intraday(t.series.key, raw) if raw is not None and not raw.empty else None

    return read


def desk_session_close(desk: Any) -> SessionClose | None:
    """The desk's session clock's close for a date (None without one: 15:30 IST)."""
    clock = getattr(desk, "session_clock", None)
    if clock is None or not hasattr(clock, "window_for"):
        return None
    return lambda day: clock.window_for(day).close_at


def desk_trading_day(desk: Any) -> Callable[[dt.date], bool] | None:
    """The desk's exchange calendar (None without one: weekdays only)."""
    calendar = getattr(desk, "calendar", None)
    return getattr(calendar, "is_trading_day", None) if calendar is not None else None
