"""Dhan historical data as a corpus provider (needs the paid Data API).

Daily bars go back to inception; intraday bars (1/5/15/25/60-minute) go back
five years and are served in windows of at most 90 days, which this provider
walks for you. Everything coarser than 1-minute is derived locally, so the
backfill plan for Dhan is simply ["1d", "1m"].
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from ...broker.base import Contract
from ...clock import IST
from ...logging_setup import get_logger
from .base import empty_bars

log = get_logger("data.dhan")

_INTRADAY = {"1m": 1, "5m": 5, "15m": 15, "25m": 25, "60m": 60}
_MAX_WINDOW_DAYS = 85  # the documented cap is 90; keep a margin
_HISTORY_YEARS = 5


class DhanHistoryProvider:
    name = "dhan"

    def __init__(self, adapter) -> None:
        self.adapter = adapter

    def supported_resolutions(self) -> list[str]:
        return ["1d", *list(_INTRADAY)]

    def earliest_available(self, resolution: str, today: dt.date) -> dt.date | None:
        if resolution == "1d":
            return None
        return today - dt.timedelta(days=365 * _HISTORY_YEARS)

    def contract_for(self, spec) -> Contract:
        """A read-only data Contract for a SeriesSpec (index, stock or FX future)."""
        sid = spec.dhan_security_id
        if not sid:
            raise ValueError(f"{spec.key}: no Dhan security id in universe.yaml")
        return Contract(
            security_id=str(sid),
            exchange_segment=spec.dhan_segment,
            symbol=spec.key,
            underlying=spec.key,
            instrument_type=spec.dhan_instrument,
        )

    def history(
        self,
        series_key: str,
        symbol: str | None,
        resolution: str,
        start: dt.date,
        end: dt.date,
        session: tuple[dt.time, dt.time] | None = None,
        spec=None,
    ) -> pd.DataFrame:
        if spec is None:
            raise ValueError("DhanHistoryProvider.history needs the SeriesSpec (spec=...)")
        c = self.contract_for(spec)
        frames: list[pd.DataFrame] = []
        if resolution == "1d":
            bars = self.adapter.historical_daily(c, start, end + dt.timedelta(days=1))
            frames.append(_frame(bars, daily=True))
        else:
            minutes = _INTRADAY[resolution]
            cur = start
            while cur <= end:
                win_end = min(end, cur + dt.timedelta(days=_MAX_WINDOW_DAYS))
                s = dt.datetime.combine(cur, dt.time(0, 0), tzinfo=IST)
                e = dt.datetime.combine(win_end, dt.time(23, 59), tzinfo=IST)
                bars = self.adapter.historical_intraday(c, minutes, s, e)
                frames.append(_frame(bars, daily=False))
                cur = win_end + dt.timedelta(days=1)
        frames = [f for f in frames if not f.empty]
        if not frames:
            return empty_bars()
        df = pd.concat(frames)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        if session and resolution != "1d":
            t = df.index.time
            df = df[(t >= session[0]) & (t < session[1])]
        df.index.name = "ts"
        return df


def _frame(bars, daily: bool) -> pd.DataFrame:
    if not bars:
        return empty_bars()
    idx = pd.DatetimeIndex([b.ts for b in bars]).tz_convert(IST)
    if daily:
        idx = idx.normalize() + pd.Timedelta(hours=9, minutes=15)
    df = pd.DataFrame(
        {
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume if b.volume is not None else 0.0 for b in bars],
        },
        index=idx,
    )
    return df.astype("float64")
