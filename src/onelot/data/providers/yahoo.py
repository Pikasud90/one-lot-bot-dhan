"""Yahoo Finance history (via yfinance): the keyless corpus until Dhan's Data API is on.

Yahoo intraday limits: 1m = last ~30 days, 2m-30m = last 60 days, 60m = last 730
days, daily = full history. Index volume (^NSEI, ^NSEBANK) is zero, exactly like
NSE index feeds; spot FX pairs (USDINR=X) have no volume either and trade
around the clock, so they are clipped to the NSE currency session.
"""

from __future__ import annotations

import datetime as dt
import time

import pandas as pd

from ...clock import IST
from ...logging_setup import get_logger
from .base import BAR_COLUMNS, empty_bars

log = get_logger("data.yahoo")

_INTERVAL = {"1m": "1m", "2m": "2m", "5m": "5m", "15m": "15m", "30m": "30m", "60m": "60m", "1d": "1d"}
_MAX_DAYS_BACK = {"1m": 29, "2m": 59, "5m": 59, "15m": 59, "30m": 59, "60m": 729, "1d": None}
_CHUNK_DAYS = {"1m": 7, "2m": 59, "5m": 59, "15m": 59, "30m": 59, "60m": 365, "1d": 3650}


class YahooProvider:
    name = "yahoo"

    def __init__(self, pause_s: float = 0.4) -> None:
        self.pause_s = pause_s

    def supported_resolutions(self) -> list[str]:
        return list(_INTERVAL)

    def earliest_available(self, resolution: str, today: dt.date) -> dt.date | None:
        back = _MAX_DAYS_BACK.get(resolution)
        return None if back is None else today - dt.timedelta(days=back)

    def history(self, series_key: str, symbol: str | None, resolution: str, start: dt.date,
                end: dt.date, session: tuple[dt.time, dt.time] | None = None) -> pd.DataFrame:
        import yfinance as yf

        if resolution not in _INTERVAL:
            raise ValueError(f"Yahoo does not serve {resolution}")
        ticker = symbol or series_key
        session = session or (dt.time(9, 15), dt.time(15, 30))
        today = dt.datetime.now(IST).date()
        earliest = self.earliest_available(resolution, today)
        if earliest and start < earliest:
            start = earliest
        if start > end:
            return empty_bars()
        frames = []
        cur = start
        step = dt.timedelta(days=_CHUNK_DAYS[resolution])
        tk = yf.Ticker(ticker)
        while cur <= end:
            chunk_end = min(end, cur + step - dt.timedelta(days=1))
            df = pd.DataFrame()
            for attempt in range(3):
                try:
                    df = tk.history(start=cur.isoformat(),
                                    end=(chunk_end + dt.timedelta(days=1)).isoformat(),
                                    interval=_INTERVAL[resolution], auto_adjust=False,
                                    actions=False, prepost=False, raise_errors=False)
                    break
                except Exception as exc:  # noqa: BLE001
                    log.warning("yahoo %s %s attempt %d failed: %s", ticker, resolution, attempt + 1, exc)
                    time.sleep(2 * (attempt + 1))
                    df = pd.DataFrame()
            if df is not None and not df.empty:
                frames.append(df)
            cur = chunk_end + dt.timedelta(days=1)
            time.sleep(self.pause_s)
        if not frames:
            return empty_bars()
        df = pd.concat(frames)
        df = df.rename(columns=str.lower)
        df = df[[c for c in BAR_COLUMNS if c in df.columns]]
        idx = df.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        df.index = idx.tz_convert(IST)
        if resolution == "1d":
            df.index = df.index.normalize() + pd.Timedelta(hours=9, minutes=15)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df = df[(df["high"] > 0) & (df["low"] > 0)]
        if resolution != "1d":
            t = df.index.time
            df = df[(t >= session[0]) & (t < session[1])]
            now = pd.Timestamp.now(tz=IST)
            df = df[df.index + pd.Timedelta(minutes=int(resolution[:-1])) <= now]
            # Yahoo omits the opening-auction volume: the first bar reports 0.
            # Impute from the next bar so session VWAP / relative volume hold.
            first = (df.index.time == session[0]) & (df["volume"] <= 0)
            if first.any() and df["volume"].sum() > 0:
                nxt = df["volume"].shift(-1)
                df.loc[first, "volume"] = nxt[first].fillna(0)
        df.index.name = "ts"
        return df.astype("float64")
