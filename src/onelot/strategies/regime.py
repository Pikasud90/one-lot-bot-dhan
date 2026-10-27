"""Regime classification (per symbol) and market bias (from the NIFTY 50 index)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import core as ind

TREND, RANGE, VOLATILE, NEUTRAL = "trend", "range", "volatile", "neutral"


def classify_regime(adx: pd.Series, chop: pd.Series, atr_pct: pd.Series,
                    adx_trend: float = 25.0, adx_range: float = 18.0, chop_trend: float = 50.0,
                    chop_range: float = 61.8, vol_extreme: float = 92.0) -> pd.Series:
    out = pd.Series(NEUTRAL, index=adx.index, dtype=object)
    out[(adx >= adx_trend) & (chop < chop_trend)] = TREND
    out[(chop > chop_range) | (adx < adx_range)] = RANGE
    out[atr_pct > vol_extreme] = VOLATILE
    return out


def market_bias(market: pd.DataFrame | None, target_index: pd.DatetimeIndex) -> pd.Series:
    """Index bias in [-3, +3] mapped onto `target_index` (no look-ahead: last *completed* bar).

    +1 each for: close > EMA20, EMA20 > EMA50, close > session open. Negative mirror for bearish.
    """
    if market is None or market.empty:
        return pd.Series(0.0, index=target_index)
    c = market["close"]
    e20, e50 = ind.ema(c, 20), ind.ema(c, 50)
    day_open = market["open"].groupby(ind.session_key(market.index)).transform("first")
    score = np.sign(c - e20).fillna(0) + np.sign(e20 - e50).fillna(0) + np.sign(c - day_open).fillna(0)
    score = pd.Series(score, index=market.index)
    # market bar t is known at its close; stock bar t is also evaluated at its close -> same stamp is fine
    aligned = score.reindex(target_index, method="ffill")
    return aligned.fillna(0.0)
