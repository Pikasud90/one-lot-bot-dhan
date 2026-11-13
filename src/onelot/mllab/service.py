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


def closed_bars(
    fine: pd.DataFrame | None, daily: pd.DataFrame | None, timeframe: str, now: dt.datetime
) -> pd.DataFrame:
    """The bars of `timeframe` closed at `now`, oldest first.

    An intraday bar is closed when both the clock and the fine bars have reached its end
    (the desk reads bars at the data's time, so a late feed never closes a bar early)."""
    stamp = pd.Timestamp(now)
    t_ns = int((stamp if stamp.tzinfo else stamp.tz_localize("UTC")).as_unit("ns").value)
    if timeframe == "1d":
        bars = daily if daily is not None and not daily.empty else None
        if bars is None and fine is not None and not fine.empty:
            bars = resample_bars(fine, "1d")
    else:
        bars = (
            resample_bars(fine, _RULE[timeframe]) if fine is not None and not fine.empty else None
        )
        if bars is not None and not bars.empty:
            fine_ns = index_ns(pd.DatetimeIndex(fine.index).sort_values())  # type: ignore[union-attr]
            gaps = np.diff(fine_ns[-50:])
            step = int(gaps[gaps > 0].min()) if np.any(gaps > 0) else 60 * 10**9
            t_ns = min(t_ns, int(fine_ns[-1]) + step)
    if bars is None or bars.empty:
        return pd.DataFrame(columns=["close"])
    idx = pd.DatetimeIndex(bars.index)
    done = nominal_close_ns(idx, _RULE[timeframe]) <= t_ns
    return bars[done].sort_index()


def next_bar_start(last_ts: pd.Timestamp, timeframe: str) -> pd.Timestamp:
    """When the bar after `last_ts` starts: the next bin, or the next session's 09:15 when
    the session ends first (weekends skipped; exchange holidays are not known here)."""
    ts = pd.Timestamp(last_ts)
    if timeframe == "1d":
        nxt = ts.normalize() + pd.Timedelta(days=1)
    else:
        nxt = ts + pd.Timedelta(minutes=_STEP[timeframe])
        if nxt.time() < dt.time(15, 30) and nxt.date() == ts.date():
            return nxt
        nxt = ts.normalize() + pd.Timedelta(days=1)
    while nxt.weekday() >= 5:
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


class MlLab:
    """`matrix(timeframe)`: one row per instrument, one cell per model."""

    def __init__(
        self,
        bars_getter: BarsGetter,
        instruments: Callable[[], Iterable[dict[str, Any]]] | Iterable[dict[str, Any]],
        clock: Callable[[], dt.datetime],
    ) -> None:
        self.bars_getter = bars_getter
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
        try:
            fine, daily = self.bars_getter(key)
            bars = closed_bars(fine, daily, timeframe, now)
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
            return {**hit[1], **head}
        closes = bars["close"].to_numpy(dtype=float)[-MAX_BARS:]
        season = m.SEASON[timeframe]
        row = {
            **head,
            "last_close": _num(closes[-1]),
            "last_bar_ts": last_ts.isoformat(),
            "next_bar_ts": next_bar_start(last_ts, timeframe).isoformat(),
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
            "cells": {model: dict(cell) for model in m.MODELS},
        }


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
