"""Exploratory views of the corpus for the Data tab.

Everything is computed from the local `bars` table, so it works offline and
describes exactly what the backtests see: return distribution, intraday
range/volume profile by time of day, gaps, weekday seasonality, coverage per
month and the cross-series correlation of daily returns.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import Engine

from .store import load_bars


def _hist(x: pd.Series, bins: int = 30, clip_pct: float = 1.0) -> dict:
    x = x.dropna().astype(float)
    if x.empty:
        return {"edges": [], "counts": []}
    lo, hi = np.percentile(x, clip_pct), np.percentile(x, 100 - clip_pct)
    if hi <= lo:
        lo, hi = float(x.min()), float(x.max()) + 1e-9
    counts, edges = np.histogram(x.clip(lo, hi), bins=bins, range=(lo, hi))
    return {"edges": [round(float(e), 4) for e in edges], "counts": [int(c) for c in counts]}


def _stats(x: pd.Series) -> dict:
    x = x.dropna().astype(float)
    if len(x) < 3:
        return {"n": int(len(x))}
    return {
        "n": int(len(x)),
        "mean": round(float(x.mean()), 4),
        "std": round(float(x.std()), 4),
        "skew": round(float(x.skew()), 3),
        "kurtosis": round(float(x.kurt()), 3),
        "min": round(float(x.min()), 3),
        "max": round(float(x.max()), 3),
        "p05": round(float(np.percentile(x, 5)), 3),
        "p95": round(float(np.percentile(x, 95)), 3),
        "positive_pct": round(float((x > 0).mean() * 100), 1),
    }


def series_eda(engine: Engine, series_key: str, resolution: str = "5m", days: int = 250) -> dict:
    out: dict = {"series": series_key, "resolution": resolution}
    daily = load_bars(engine, series_key, "1d")
    intra = load_bars(engine, series_key, resolution)
    if daily.empty and not intra.empty:
        key = intra.index.normalize()
        daily = intra.groupby(key).agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        )
    if not daily.empty:
        d = daily.tail(days)
        ret = d["close"].pct_change() * 100
        gap = (d["open"] / d["close"].shift(1) - 1) * 100
        out["daily"] = {
            "dates": [str(i.date()) for i in d.index],
            "close": [round(float(v), 2) for v in d["close"]],
            "returns_hist": _hist(ret),
            "returns_stats": _stats(ret),
            "gap_hist": _hist(gap),
            "gap_stats": _stats(gap),
        }
        wd = pd.DataFrame({"ret": ret, "wd": d.index.weekday})
        g = wd.dropna().groupby("wd")["ret"]
        out["weekday"] = [
            {
                "weekday": ["Mon", "Tue", "Wed", "Thu", "Fri"][int(k)] if k < 5 else str(k),
                "mean_ret": round(float(v.mean()), 3),
                "win_pct": round(float((v > 0).mean() * 100), 1),
                "n": int(len(v)),
            }
            for k, v in g
        ]
        hv = (ret.rolling(20).std() * np.sqrt(252)).dropna()
        out["daily"]["hv20"] = {
            "dates": [str(i.date()) for i in hv.index],
            "values": [round(float(v), 2) for v in hv],
        }
    if not intra.empty:
        i = intra.tail(60 * 400)
        tod = pd.Series(i.index.strftime("%H:%M"), index=i.index)
        rng = (i["high"] - i["low"]) / i["close"] * 100
        prof = pd.DataFrame(
            {
                "tod": tod.to_numpy(),
                "range_pct": rng.to_numpy(),
                "volume": i["volume"].to_numpy(),
                "ret": (i["close"] / i["open"] - 1).abs().to_numpy() * 100,
            }
        )
        p = prof.groupby("tod").agg(
            range_pct=("range_pct", "mean"),
            volume=("volume", "mean"),
            move=("ret", "mean"),
            n=("ret", "size"),
        )
        out["profile"] = {
            "times": list(p.index),
            "range_pct": [round(float(v), 4) for v in p["range_pct"]],
            "volume": [round(float(v), 0) for v in p["volume"]],
            "move_pct": [round(float(v), 4) for v in p["move"]],
            "sessions": int(p["n"].max()),
        }
        sessions = pd.Series(1, index=intra.index.normalize()).groupby(level=0).first()
        months = sessions.groupby(sessions.index.strftime("%Y-%m")).sum()
        out["coverage"] = {"months": list(months.index), "sessions": [int(v) for v in months]}
        out["bars"] = int(len(intra))
        out["first"], out["last"] = str(intra.index[0]), str(intra.index[-1])
    vix = load_bars(engine, "INDIAVIX", "1d")
    if not vix.empty and series_key != "INDIAVIX" and "daily" in out:
        v = vix["close"].copy()
        v.index = v.index.normalize()
        dd = (
            pd.DatetimeIndex(pd.to_datetime(out["daily"]["dates"])).tz_localize(v.index.tz)
            if v.index.tz
            else pd.DatetimeIndex(pd.to_datetime(out["daily"]["dates"]))
        )
        al = v.reindex(dd)
        out["daily"]["vix"] = [None if pd.isna(x) else round(float(x), 2) for x in al]
    return out


def correlation(engine: Engine, keys: list[str], days: int = 250) -> dict:
    closes = {}
    for k in keys:
        d = load_bars(engine, k, "1d")
        if not d.empty:
            s = d["close"].tail(days)
            s.index = s.index.normalize()
            closes[k] = s
    if len(closes) < 2:
        return {"keys": list(closes), "matrix": []}
    df = pd.DataFrame(closes).pct_change(fill_method=None).dropna(how="all")
    corr = df.corr(min_periods=20)
    ks = list(corr.columns)
    return {
        "keys": ks,
        "matrix": [
            [
                None if pd.isna(corr.iloc[i, j]) else round(float(corr.iloc[i, j]), 2)
                for j in range(len(ks))
            ]
            for i in range(len(ks))
        ],
        "sessions": int(len(df)),
    }
