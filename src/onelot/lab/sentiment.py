"""Sentiment as a backtest input.

The operator is building a sentiment scorer. Whatever it produces (today a manual
score, tomorrow 20 years of daily scores from `POST /api/sentiment/import`) lands in
the `sentiment_scores` table and can gate any strategy here:

    mode = off         ignore sentiment
    mode = follow      block longs while sentiment is bearish, shorts while bullish
    mode = strict      longs only while bullish, shorts only while bearish
    mode = contrarian  longs only while bearish, shorts only while bullish

Scores are stored on -1..+1. A 0..1 scale (0 bearish, 0.5 chop, 1 bullish) is
accepted and mapped to -1..+1. The score used for a bar is the latest one whose
timestamp is at or before the bar's close -- never a later one (no look-ahead) -- and
not older than `max_age_days`.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
from sqlalchemy import Engine, select

from ..clock import IST
from ..data.schema import sentiment_scores

MODES = ("off", "follow", "strict", "contrarian")


def normalise(values: pd.Series) -> pd.Series:
    """Map a 0..1 scale to -1..+1 when every score sits inside [0, 1]."""
    v = values.astype(float)
    if len(v) and v.min() >= 0.0 and v.max() <= 1.0 and not ((v == 0.0) | (v == 1.0)).all():
        return (v - 0.5) * 2.0
    if len(v) and v.min() >= 0.0 and v.max() <= 1.0 and v.nunique() > 2:
        return (v - 0.5) * 2.0
    return v.clip(-1.0, 1.0)


def load_scores(
    engine: Engine, scope: str = "MARKET", symbol: str | None = None, source: str | None = None
) -> pd.Series:
    q = select(sentiment_scores.c.ts, sentiment_scores.c.score).where(
        sentiment_scores.c.scope == scope.upper()
    )
    q = (
        q.where(sentiment_scores.c.symbol == symbol.upper())
        if symbol
        else q.where(sentiment_scores.c.symbol.is_(None))
    )
    if source:
        q = q.where(sentiment_scores.c.source == source)
    with engine.connect() as conn:
        rows = conn.execute(q.order_by(sentiment_scores.c.ts)).all()
    if not rows:
        return pd.Series(dtype=float)
    idx = pd.DatetimeIndex([(_aware(r[0])) for r in rows])
    s = pd.Series([float(r[1]) for r in rows], index=idx).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    return normalise(s)


def _aware(ts: dt.datetime) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return (t.tz_localize("UTC") if t.tzinfo is None else t).tz_convert(IST)


def bias_at(
    scores: pd.Series,
    index: pd.DatetimeIndex,
    bar_minutes: int,
    threshold: float = 0.2,
    max_age_days: float = 5.0,
) -> np.ndarray:
    """-1 / 0 / +1 per bar from the latest score at or before each bar's CLOSE."""
    if scores.empty or len(index) == 0:
        return np.zeros(len(index), dtype=int)
    closes = pd.DataFrame({"t": index + pd.Timedelta(minutes=bar_minutes)})
    sc = pd.DataFrame({"t": scores.index, "score": scores.to_numpy()})
    merged = pd.merge_asof(
        closes, sc, on="t", direction="backward", tolerance=pd.Timedelta(days=max_age_days)
    )
    v = merged["score"].to_numpy(dtype=float)
    out = np.zeros(len(index), dtype=int)
    out[v >= threshold] = 1
    out[v <= -threshold] = -1
    return out


def apply_gate(f: pd.DataFrame, bias: np.ndarray, mode: str) -> pd.DataFrame:
    """Mutates and returns `f`: masks long/short entries per `mode`, records the blocks."""
    if mode not in MODES:
        raise ValueError(f"sentiment mode must be one of {MODES}")
    b = pd.Series(bias, index=f.index)
    f["sentiment_bias"] = b
    if mode == "off":
        f["blk_sentiment_long"] = False
        f["blk_sentiment_short"] = False
        return f
    if mode == "follow":
        blk_l, blk_s = b < 0, b > 0
    elif mode == "strict":
        blk_l, blk_s = b <= 0, b >= 0
    else:  # contrarian
        blk_l, blk_s = b >= 0, b <= 0
    f["blk_sentiment_long"] = blk_l & f["long_entry"].astype(bool)
    f["blk_sentiment_short"] = blk_s & f["short_entry"].astype(bool)
    f["long_entry"] = f["long_entry"].astype(bool) & ~blk_l
    f["short_entry"] = f["short_entry"].astype(bool) & ~blk_s
    return f


def parse_import(text: str) -> list[dict]:
    """CSV/whitespace rows: `date[,score[,confidence]]` (header optional). Returns rows
    with aware timestamps at 09:15 IST when only a date is given."""
    rows: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.replace("\t", ",").replace(";", ",").split(",")]
        if len(parts) < 2:
            parts = line.split()
        if len(parts) < 2:
            continue
        try:
            score = float(parts[1])
        except ValueError:
            continue  # header
        try:
            ts = pd.Timestamp(parts[0])
        except (ValueError, TypeError):
            continue
        if ts.tzinfo is None:
            ts = ts.tz_localize(IST)
        if ts.hour == 0 and ts.minute == 0:
            ts = ts + pd.Timedelta(hours=9, minutes=15)
        conf = None
        if len(parts) >= 3:
            try:
                conf = float(parts[2])
            except ValueError:
                conf = None
        rows.append({"ts": ts.to_pydatetime(), "score": score, "confidence": conf})
    if rows:
        s = pd.Series([r["score"] for r in rows])
        norm = normalise(s)
        for r, v in zip(rows, norm.to_numpy(), strict=True):
            r["score"] = float(v)
    return rows
