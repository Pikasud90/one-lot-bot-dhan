"""Bars in and out of the database as pandas objects.

Bars are keyed by SERIES key (BANKNIFTY, RELIANCE, USDINR, ...), never by a
tradeable instrument: an index future, an index option and the index itself
all read the same underlying series. The Lab's execution models translate
underlying prices into instrument P&L.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
from sqlalchemy import Engine, String, and_, func, select, type_coerce

from ..clock import IST
from .db import upsert
from .resample import RESOLUTION_ORDER, can_derive, normalize_resolution, resample_bars
from .schema import bars

UTC = dt.timezone.utc


def save_bars(
    engine: Engine, series_key: str, resolution: str, df: pd.DataFrame, source: str
) -> int:
    """Upsert bars. `df` indexed by bar start (tz-aware), columns open/high/low/close/volume[/oi]."""
    if df is None or df.empty:
        return 0
    resolution = normalize_resolution(resolution)
    d = df.copy()
    if d.index.tz is None:
        d.index = d.index.tz_localize(IST)
    d.index = d.index.tz_convert(UTC)
    has_oi = "oi" in d.columns
    rows = []
    for ts, r in d.iterrows():
        rows.append(
            {
                "ts": ts.to_pydatetime(),
                "series_key": series_key,
                "resolution": resolution,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r.get("volume", 0) or 0),
                "oi": float(r["oi"]) if has_oi and pd.notna(r["oi"]) else None,
                "source": source,
            }
        )
    return upsert(engine, bars, rows, ["ts", "series_key", "resolution"])


def _to_utc(value) -> dt.datetime:
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        value = dt.datetime.combine(value, dt.time(0, 0), tzinfo=IST)
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize(IST)
    return ts.tz_convert(UTC).to_pydatetime()


def _utc_index(raw: pd.Series) -> pd.DatetimeIndex:
    """Stored bar times as tz-aware UTC. SQLite hands back the stored text
    ('2026-05-29 04:15:00.000000', naive UTC per TZDateTime); another driver may hand back
    datetimes. Either way, naive means UTC."""
    if raw.dtype == object and len(raw) and isinstance(raw.iloc[0], str):
        ts = pd.to_datetime(raw, format="ISO8601")
    else:
        ts = pd.to_datetime(raw)
    idx = pd.DatetimeIndex(ts)
    return idx.tz_localize(UTC) if idx.tz is None else idx.tz_convert(UTC)


def _read_bars(
    engine: Engine, series_key: str, resolution: str, start=None, end=None
) -> pd.DataFrame:
    conds = [bars.c.series_key == series_key, bars.c.resolution == resolution]
    if start is not None:
        conds.append(bars.c.ts >= _to_utc(start))
    if end is not None:
        conds.append(bars.c.ts < _to_utc(end))
    # `ts` is read raw and converted to UTC in one vectorised step: the column type's
    # per-row conversion cost ~4 s per 2 million one-minute bars and made Lab runs crawl.
    q = (
        select(
            type_coerce(bars.c.ts, String).label("ts"),
            bars.c.open,
            bars.c.high,
            bars.c.low,
            bars.c.close,
            bars.c.volume,
            bars.c.oi,
        )
        .where(and_(*conds))
        .order_by(bars.c.ts)
    )
    with engine.connect() as conn:
        df = pd.DataFrame(
            conn.execute(q).fetchall(),
            columns=["ts", "open", "high", "low", "close", "volume", "oi"],
        )
    if df.empty:
        return df.set_index("ts")
    ts = _utc_index(df["ts"])
    df = df.drop(columns=["ts"])
    df.index = pd.DatetimeIndex(ts).tz_convert(IST)
    df.index.name = "ts"
    if df["oi"].isna().all():
        df = df.drop(columns=["oi"])
    return df.astype(dict.fromkeys(["open", "high", "low", "close", "volume"], "float64"))


def available_resolutions(
    engine: Engine, series_key: str
) -> dict[str, tuple[dt.datetime, dt.datetime, int]]:
    q = (
        select(bars.c.resolution, func.min(bars.c.ts), func.max(bars.c.ts), func.count())
        .where(bars.c.series_key == series_key)
        .group_by(bars.c.resolution)
    )
    out = {}
    with engine.connect() as conn:
        for res, lo, hi, n in conn.execute(q).fetchall():
            lo = pd.Timestamp(lo)
            hi = pd.Timestamp(hi)
            lo = lo.tz_localize("UTC") if lo.tzinfo is None else lo
            hi = hi.tz_localize("UTC") if hi.tzinfo is None else hi
            out[res] = (
                lo.tz_convert(IST).to_pydatetime(),
                hi.tz_convert(IST).to_pydatetime(),
                int(n),
            )
    return out


def load_bars(
    engine: Engine, series_key: str, resolution: str, start=None, end=None
) -> pd.DataFrame:
    """Bars at `resolution`, derived from the finest stored resolution that can
    produce it, preferring the finer source where both exist."""
    resolution = normalize_resolution(resolution)
    avail = available_resolutions(engine, series_key)
    direct = (
        _read_bars(engine, series_key, resolution, start, end)
        if resolution in avail
        else pd.DataFrame()
    )
    candidates = [
        r for r in RESOLUTION_ORDER if r in avail and r != resolution and can_derive(r, resolution)
    ]
    best = direct
    if not candidates:
        return best
    # Read ONE base: the one with the widest history (earliest first bar; the finer on a
    # tie). Reading and resampling every candidate cost seconds per Lab run on five years
    # of one-minute bars. A different base that runs later only adds its newer tail.
    widest = min(candidates, key=lambda r: (avail[r][0], RESOLUTION_ORDER.index(r)))
    derived = resample_bars(_read_bars(engine, series_key, widest, start, end), resolution)
    if len(derived) > len(best):
        best = _merge(best, derived)
    latest = max(candidates, key=lambda r: (avail[r][1], -RESOLUTION_ORDER.index(r)))
    if latest != widest and avail[latest][1] > avail[widest][1] and not derived.empty:
        tail_from = derived.index.max()
        if start is not None and _to_utc(start) > tail_from.tz_convert(UTC).to_pydatetime():
            tail_from = start
        tail = resample_bars(_read_bars(engine, series_key, latest, tail_from, end), resolution)
        tail = tail[tail.index > derived.index.max()]  # never a partial bucket twice
        if not tail.empty:
            best = _merge(best, tail)
    return best


def _merge(best: pd.DataFrame, extra: pd.DataFrame) -> pd.DataFrame:
    """`extra` wins where both have a bar."""
    if best.empty:
        return extra
    return pd.concat([best[~best.index.isin(extra.index)], extra]).sort_index()


def coverage_report(engine: Engine) -> pd.DataFrame:
    q = (
        select(
            bars.c.series_key,
            bars.c.resolution,
            bars.c.source,
            func.min(bars.c.ts),
            func.max(bars.c.ts),
            func.count(),
        )
        .group_by(bars.c.series_key, bars.c.resolution, bars.c.source)
        .order_by(bars.c.series_key, bars.c.resolution)
    )
    rows = []
    with engine.connect() as conn:
        for key, res, src, lo, hi, n in conn.execute(q).fetchall():
            lo = pd.Timestamp(lo)
            hi = pd.Timestamp(hi)
            lo = (lo.tz_localize("UTC") if lo.tzinfo is None else lo).tz_convert(IST)
            hi = (hi.tz_localize("UTC") if hi.tzinfo is None else hi).tz_convert(IST)
            rows.append(
                {
                    "series_key": key,
                    "resolution": res,
                    "source": src,
                    "first": lo.strftime("%Y-%m-%d"),
                    "last": hi.strftime("%Y-%m-%d %H:%M"),
                    "bars": int(n),
                    "sessions": None,
                }
            )
    return pd.DataFrame(rows)


def coverage_by_series(engine: Engine) -> dict[str, dict[str, dict]]:
    """{series_key: {resolution: {first, last, bars}}} for the UI."""
    cov = coverage_report(engine)
    out: dict[str, dict[str, dict]] = {}
    for r in cov.to_dict("records"):
        out.setdefault(r["series_key"], {})[r["resolution"]] = {
            "first": r["first"],
            "last": r["last"],
            "bars": r["bars"],
            "source": r["source"],
        }
    return out
