"""Bar sources for the desk.

    YahooBars  one batched request per poll for every ticker (2-minute bars, ~15 min delayed,
               60 days of history). Works with no credentials: the desk runs on day one.
    DhanBars   1-minute bars from the Data API, incremental after the first load. Only when
               the plan is active; anything Dhan cannot serve (spot FX) stays on Yahoo.

Both return IST-indexed OHLCV frames clipped to the instrument's session with the still-
forming bar dropped, so every strategy sees closed bars only.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Protocol

import pandas as pd

from ..clock import IST
from ..data.universe import SeriesSpec

log = logging.getLogger(__name__)

COLUMNS = ["open", "high", "low", "close", "volume"]


class BarSource(Protocol):
    name: str
    delayed: bool
    resolution: str

    def fetch(self, specs: dict[str, SeriesSpec], days: int,
              since: dict[str, pd.Timestamp] | None = None) -> dict[str, pd.DataFrame]: ...


def _session_bounds(spec: SeriesSpec) -> tuple[dt.time, dt.time]:
    s = spec.session_times
    h1, m1 = (int(x) for x in s["open"].split(":"))
    h2, m2 = (int(x) for x in s["close"].split(":"))
    return dt.time(h1, m1), dt.time(h2, m2)


def _clean(df: pd.DataFrame, spec: SeriesSpec, res_min: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=COLUMNS)
    df = df.rename(columns=str.lower)
    df = df[[c for c in COLUMNS if c in df.columns]].copy()
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = 0.0
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx
    df.index = idx.tz_convert(IST)
    df = df.dropna(subset=["close"])
    df = df[(df["high"] > 0) & (df["low"] > 0)]
    lo, hi = _session_bounds(spec)
    t = df.index.time
    df = df[(t >= lo) & (t < hi)]
    now = pd.Timestamp.now(tz=IST)
    df = df[df.index + pd.Timedelta(minutes=res_min) <= now]        # closed bars only
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.index.name = "ts"
    return df.astype("float64")


class YahooBars:
    name = "yahoo"
    delayed = True
    resolution = "2m"

    def __init__(self, interval: str = "2m") -> None:
        self.resolution = interval
        self.last_error: str | None = None

    def fetch(self, specs: dict[str, SeriesSpec], days: int,
              since: dict[str, pd.Timestamp] | None = None) -> dict[str, pd.DataFrame]:
        import yfinance as yf

        tickers = {s.yahoo: k for k, s in specs.items() if s.yahoo}
        if not tickers:
            return {}
        res_min = int(self.resolution[:-1])
        period = f"{min(max(days, 1), 59)}d"
        out: dict[str, pd.DataFrame] = {}
        try:
            raw = yf.download(list(tickers), period=period, interval=self.resolution, group_by="ticker",
                              progress=False, threads=True, auto_adjust=False, prepost=False)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("yahoo desk fetch failed: %s", self.last_error)
            return {}
        self.last_error = None
        for tk, key in tickers.items():
            try:
                sub = raw[tk] if isinstance(raw.columns, pd.MultiIndex) else raw
            except KeyError:
                continue
            out[key] = _clean(sub, specs[key], res_min)
        return out


class DhanBars:
    name = "dhan"
    delayed = False
    resolution = "1m"

    def __init__(self, adapter, pause_s: float = 0.25) -> None:
        from ..data.providers.dhan import DhanHistoryProvider

        self.adapter = adapter
        self.provider = DhanHistoryProvider(adapter)
        self.pause_s = pause_s
        self.last_error: str | None = None

    def can_serve(self, spec: SeriesSpec) -> bool:
        return bool(spec.dhan_security_id) and spec.kind in ("INDEX", "STOCK")

    def fetch(self, specs: dict[str, SeriesSpec], days: int,
              since: dict[str, pd.Timestamp] | None = None) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        now = dt.datetime.now(IST)
        errors = []
        for key, spec in specs.items():
            if not self.can_serve(spec):
                continue
            start_ts = (since or {}).get(key)
            start = (start_ts.to_pydatetime() - dt.timedelta(minutes=5)) if start_ts is not None \
                else (now - dt.timedelta(days=days)).replace(hour=9, minute=0, second=0, microsecond=0)
            try:
                contract = self.provider.contract_for(spec)
                bars = self.adapter.historical_intraday(contract, 1, start, now)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{key}: {type(exc).__name__}: {str(exc)[:80]}")
                continue
            if bars:
                df = pd.DataFrame([{"ts": b.ts, "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                                    "volume": b.volume or 0.0} for b in bars]).set_index("ts")
                out[key] = _clean(df, spec, 1)
            time.sleep(self.pause_s)
        self.last_error = "; ".join(errors)[:300] if errors else None
        if errors:
            log.warning("dhan desk fetch: %s", self.last_error)
        return out


class CompositeBars:
    """Dhan for what it can serve, Yahoo for the rest (and as the fallback on error)."""

    def __init__(self, primary: BarSource | None, fallback: YahooBars) -> None:
        self.primary = primary
        self.fallback = fallback
        self.name = f"{primary.name}+yahoo" if primary else fallback.name
        self.delayed = fallback.delayed if primary is None else False
        self.resolution = primary.resolution if primary else fallback.resolution
        self.last_error: str | None = None
        self.served_by: dict[str, str] = {}

    def fetch(self, specs: dict[str, SeriesSpec], days: int,
              since: dict[str, pd.Timestamp] | None = None) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        rest = dict(specs)
        if self.primary is not None:
            servable = {k: s for k, s in specs.items() if getattr(self.primary, "can_serve", lambda _s: True)(s)}
            got = self.primary.fetch(servable, days, since)
            for k, df in got.items():
                if not df.empty:
                    out[k] = df
                    self.served_by[k] = self.primary.name
                    rest.pop(k, None)
        if rest:
            got = self.fallback.fetch(rest, days, None)
            for k, df in got.items():
                out[k] = df
                self.served_by[k] = self.fallback.name
        errs = [e for e in (getattr(self.primary, "last_error", None), self.fallback.last_error) if e]
        self.last_error = "; ".join(errs) if errs else None
        return out


def build_source(mode: str, adapter, data_api_active: bool) -> CompositeBars:
    yahoo = YahooBars()
    if mode == "yahoo" or adapter is None or (mode == "auto" and not data_api_active):
        return CompositeBars(None, yahoo)
    return CompositeBars(DhanBars(adapter), yahoo)
