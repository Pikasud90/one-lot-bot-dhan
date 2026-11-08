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


def is_delayed_source(name: str | None) -> bool:
    """True when bars from the source called `name` run behind the exchange.

    The single rule behind every "delayed" label (alert data, tracker health, the Desk
    header badge, Telegram): Yahoo bars run ~15 minutes behind; Dhan REST/WebSocket,
    mock and manual sources are live. `name` is the source that actually served a series
    (`CompositeBars.served_by`), not the composite's own name such as "dhan+yahoo".
    None or "" (nothing served yet) is not delayed.
    """
    return name is not None and name.lower().startswith("yahoo")


class BarSource(Protocol):
    name: str
    delayed: bool
    resolution: str

    def fetch(
        self, specs: dict[str, SeriesSpec], days: int, since: dict[str, pd.Timestamp] | None = None
    ) -> dict[str, pd.DataFrame]: ...


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
    df = df[df.index + pd.Timedelta(minutes=res_min) <= now]  # closed bars only
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if not spec.has_volume:
        # An index or spot FX has no traded volume. Dhan's index bars carry a volume figure
        # from 2025-12-03 (the socket's bars none), and the Lab zeroes it (lab/runner.py), so
        # the desk does too: every volume filter is neutral on both, and a paper trade is
        # the backtest's trade (the paper test, 2026-11-08).
        df["volume"] = 0.0
    df.index.name = "ts"
    return df.astype("float64")


class YahooBars:
    name = "yahoo"
    delayed = True
    resolution = "2m"

    def __init__(self, interval: str = "2m") -> None:
        self.resolution = interval
        self.last_error: str | None = None

    def fetch(
        self, specs: dict[str, SeriesSpec], days: int, since: dict[str, pd.Timestamp] | None = None
    ) -> dict[str, pd.DataFrame]:
        import yfinance as yf

        tickers = {s.yahoo: k for k, s in specs.items() if s.yahoo}
        if not tickers:
            return {}
        res_min = int(self.resolution[:-1])
        period = f"{min(max(days, 1), 59)}d"
        out: dict[str, pd.DataFrame] = {}
        try:
            raw = yf.download(
                list(tickers),
                period=period,
                interval=self.resolution,
                group_by="ticker",
                progress=False,
                threads=True,
                auto_adjust=False,
                prepost=False,
            )
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

    def fetch(
        self, specs: dict[str, SeriesSpec], days: int, since: dict[str, pd.Timestamp] | None = None
    ) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        now = dt.datetime.now(IST)
        errors = []
        for key, spec in specs.items():
            if not self.can_serve(spec):
                continue
            start_ts = (since or {}).get(key)
            start = (
                (start_ts.to_pydatetime() - dt.timedelta(minutes=5))
                if start_ts is not None
                else (now - dt.timedelta(days=days)).replace(
                    hour=9, minute=0, second=0, microsecond=0
                )
            )
            try:
                contract = self.provider.contract_for(spec)
                bars = self.adapter.historical_intraday(contract, 1, start, now)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{key}: {type(exc).__name__}: {str(exc)[:80]}")
                continue
            if bars:
                df = pd.DataFrame(
                    [
                        {
                            "ts": b.ts,
                            "open": b.open,
                            "high": b.high,
                            "low": b.low,
                            "close": b.close,
                            "volume": b.volume or 0.0,
                        }
                        for b in bars
                    ]
                ).set_index("ts")
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

    def fetch(
        self, specs: dict[str, SeriesSpec], days: int, since: dict[str, pd.Timestamp] | None = None
    ) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        rest = dict(specs)
        if self.primary is not None:
            servable = {
                k: s
                for k, s in specs.items()
                if getattr(self.primary, "can_serve", lambda _s: True)(s)
            }
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
        errs = [
            e for e in (getattr(self.primary, "last_error", None), self.fallback.last_error) if e
        ]
        self.last_error = "; ".join(errs) if errs else None
        return out


class DhanLiveBars:
    """Dhan 1-minute bars with the WebSocket on top.

    REST (`DhanBars`) seeds history and self-heals every poll; ticks from the engine's
    socket build the forming minute in real time and expose the last trade for each
    desk instrument (`quotes`). Closed bars from ticks are merged with REST bars by
    timestamp (REST wins on conflict), so a dropped socket never corrupts the series.
    """

    name = "dhan-ws"
    delayed = False
    resolution = "1m"

    def __init__(self, adapter, feed, specs: dict[str, SeriesSpec]) -> None:
        from ..broker.base import Contract
        from ..market.candles import CandleSeries

        self.rest = DhanBars(adapter)
        self.feed = feed
        self.last_error: str | None = None
        self.quotes: dict[str, dict] = {}
        self._series: dict[str, CandleSeries] = {}
        self._id_to_key: dict[str, str] = {}
        self._closed: dict[str, list] = {}
        self._lock = __import__("threading").Lock()
        contracts: list[Contract] = []
        session_open = dt.datetime.now(IST).replace(hour=9, minute=15, second=0, microsecond=0)
        for key, spec in specs.items():
            if not self.rest.can_serve(spec):
                continue
            try:
                c = self.rest.provider.contract_for(spec)
            except Exception:  # noqa: BLE001
                continue
            contracts.append(c)
            self._id_to_key[str(c.security_id)] = key
            self._series[key] = CandleSeries(
                key, 1, session_open, has_volume=bool(spec.has_volume), max_bars=3000
            )
            self._closed[key] = []
        if feed is not None and contracts:
            feed.add_tick_listener(self._on_tick)
            feed.subscribe_extra(contracts)
        self.subscribed = len(contracts)

    def can_serve(self, spec: SeriesSpec) -> bool:
        return self.rest.can_serve(spec)

    def _on_tick(self, q) -> None:
        key = self._id_to_key.get(str(q.security_id))
        if key is None or q.ltp is None:
            return
        with self._lock:
            self.quotes[key] = {
                "ltp": float(q.ltp),
                "bid": q.bid,
                "ask": q.ask,
                "volume": q.volume,
                "ts": q.ts.isoformat() if q.ts else None,
                "source": q.source,
            }
            series = self._series.get(key)
            if series is not None:
                for b in series.on_tick(q.ts, q.ltp, q.volume):
                    self._closed[key].append(b)

    def socket_alive(self) -> bool:
        try:
            return bool(self.feed and self.feed.broker.stream_alive())
        except Exception:  # noqa: BLE001
            return False

    def fetch(
        self, specs: dict[str, SeriesSpec], days: int, since: dict[str, pd.Timestamp] | None = None
    ) -> dict[str, pd.DataFrame]:
        now = dt.datetime.now(IST)
        out = self.rest.fetch(specs, days, since)  # history + self-heal (closed bars only)
        self.last_error = self.rest.last_error
        with self._lock:
            for key, series in self._series.items():
                if key not in specs:
                    continue
                for b in series.mark_closed_through(now, settle_delay_sec=2.0):
                    self._closed[key].append(b)
                closed, self._closed[key] = self._closed[key], []
                if not closed:
                    continue
                df = pd.DataFrame(
                    [
                        {
                            "ts": b.ts,
                            "open": b.open,
                            "high": b.high,
                            "low": b.low,
                            "close": b.close,
                            "volume": b.volume or 0.0,
                        }
                        for b in closed
                    ]
                ).set_index("ts")
                df = _clean(df, specs[key], 1)
                if df.empty:
                    continue
                if key in out and not out[key].empty:
                    merged = pd.concat([df, out[key]])  # REST last -> wins on duplicates
                    out[key] = merged[~merged.index.duplicated(keep="last")].sort_index()
                else:
                    out[key] = df
        return out


def build_source(
    mode: str, adapter, data_api_active: bool, feed=None, specs: dict[str, SeriesSpec] | None = None
) -> CompositeBars:
    yahoo = YahooBars()
    if mode == "yahoo" or adapter is None or (mode == "auto" and not data_api_active):
        return CompositeBars(None, yahoo)
    if feed is not None and specs:
        return CompositeBars(DhanLiveBars(adapter, feed, specs), yahoo)
    return CompositeBars(DhanBars(adapter), yahoo)
