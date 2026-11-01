"""Bar resampling anchored to the NSE session open (09:15 IST)."""

from __future__ import annotations

import pandas as pd

from ..clock import IST

OHLCV_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}

_RES_TO_RULE = {
    "1m": "1min",
    "2m": "2min",
    "3m": "3min",
    "5m": "5min",
    "10m": "10min",
    "15m": "15min",
    "20m": "20min",
    "30m": "30min",
    "45m": "45min",
    "60m": "60min",
    "1h": "60min",
    "120m": "120min",
    "2h": "120min",
    "240m": "240min",
    "4h": "240min",
}

RESOLUTION_ORDER = [
    "1m",
    "2m",
    "3m",
    "5m",
    "10m",
    "15m",
    "20m",
    "30m",
    "45m",
    "60m",
    "120m",
    "240m",
    "1d",
]


def normalize_resolution(res: str) -> str:
    res = res.strip().lower()
    return {"1h": "60m", "2h": "120m", "4h": "240m", "d": "1d", "day": "1d", "1D": "1d"}.get(
        res, res
    )


def minutes_of(res: str) -> int:
    res = normalize_resolution(res)
    if res == "1d":
        return 375
    return int(res[:-1])


def ensure_ist(df: pd.DataFrame) -> pd.DataFrame:
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(IST)
    return df


def resample_bars(df: pd.DataFrame, target: str) -> pd.DataFrame:
    """Resample bars (indexed by bar start, IST) to `target`; bins start at 09:15 each session."""
    target = normalize_resolution(target)
    if df.empty:
        return df.copy()
    df = ensure_ist(df.copy())
    agg = {k: v for k, v in OHLCV_AGG.items() if k in df.columns}
    if "oi" in df.columns:
        agg["oi"] = "last"
    if target == "1d":
        out = df.groupby(df.index.date).agg(agg)
        out.index = pd.DatetimeIndex(
            [
                pd.Timestamp(d).tz_localize(IST) + pd.Timedelta(hours=9, minutes=15)
                for d in out.index
            ]
        )
    else:
        rule = (
            _RES_TO_RULE.get(target) or f"{minutes_of(target)}min"
        )  # any N-minute frame (12m, 4m...)
        out = df.resample(
            rule, origin="start_day", offset="9h15min", label="left", closed="left"
        ).agg(agg)
    out = out.dropna(subset=["open", "close"])
    out.index.name = "ts"
    return out


def can_derive(base: str, target: str) -> bool:
    base, target = normalize_resolution(base), normalize_resolution(target)
    if target == "1d":
        return True
    if base == "1d":
        return False
    return minutes_of(target) % minutes_of(base) == 0 and minutes_of(target) >= minutes_of(base)
