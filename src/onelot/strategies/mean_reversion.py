"""S5 - Trend-filtered Mean Reversion (RSI-2 pullbacks in the direction of the trend).

Buy short-term oversold dips (RSI(2) extreme) only while the intraday trend is up (EMA20 > EMA50,
price above EMA50); exit on the snap-back (RSI(2) recovers) or after a time stop. Typically a
high-win-rate / small-average-win profile - costs matter, so the backtest includes them.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field

from ..indicators import core as ind
from .base import Strategy, StrategyParams, opt, vol_pillar, volume_pillar
from .regime import NEUTRAL, RANGE, TREND


class MRParams(StrategyParams):
    rsi_len: int = Field(2, description="RSI length", json_schema_extra=opt(2, 4, 1))
    entry_level: float = Field(
        10,
        description="Long when RSI below this (short: above 100-x)",
        json_schema_extra=opt(3, 20, 3),
    )
    exit_level: float = Field(
        60,
        description="Exit long when RSI above this (short: below 100-x)",
        json_schema_extra=opt(50, 80, 5),
    )
    trend_fast: int = Field(20, description="Fast EMA for the trend filter")
    trend_slow: int = Field(
        50, description="Slow EMA for the trend filter", json_schema_extra=opt(30, 100, 10)
    )
    max_extension_atr: float = Field(
        2.0,
        description="Skip if price is more than this many ATR below EMA50 (broken trend)",
        json_schema_extra=opt(1.0, 3.0, 0.5),
    )
    stop_atr: float = Field(
        1.5, description="Initial stop distance in ATR(14)", json_schema_extra=opt(0.75, 2.5, 0.25)
    )
    target_r: float = Field(
        0.0,
        description="Profit target in R (0 = exit on RSI recovery)",
        json_schema_extra=opt(0.0, 2.0, 0.5),
    )
    trail_atr_mult: float = Field(
        0.0, description="Trailing stop distance k (0 = off)", json_schema_extra=opt(0.0, 3.0, 0.5)
    )
    max_bars: int = Field(12, description="Time stop in bars", json_schema_extra=opt(6, 24, 6))
    cooldown_bars: int = Field(
        3, description="Bars to wait after an exit", json_schema_extra=opt(0, 12, 3)
    )
    max_trades_per_day: int = Field(
        3, description="Per symbol per day", json_schema_extra=opt(1, 4, 1)
    )


class MeanReversion(Strategy):
    name = "mean_reversion"
    label = "Trend-filtered Mean Reversion"
    description = (
        "Buy RSI(2) dips inside an intraday uptrend (short the mirror); exit on the snap-back."
    )
    regimes = (RANGE, NEUTRAL, TREND)
    Params = MRParams

    def raw_signals(self, f: pd.DataFrame) -> pd.DataFrame:
        p: MRParams = self.p  # type: ignore[assignment]
        c, atr = f["close"], f["atr"]
        r = ind.rsi(c, p.rsi_len)
        ef, es = ind.ema(c, p.trend_fast), ind.ema(c, p.trend_slow)
        # price may dip below EMA20 but not far below EMA50 (that would be a broken trend)
        up = (ef > es) & (c >= es - p.max_extension_atr * atr)
        dn = (ef < es) & (c <= es + p.max_extension_atr * atr)
        long_entry = up & (r < p.entry_level)
        short_entry = dn & (r > 100 - p.entry_level)
        out = pd.DataFrame(index=f.index)
        out["long_entry"], out["short_entry"] = long_entry, short_entry
        out["long_stop"] = c - p.stop_atr * atr
        out["short_stop"] = c + p.stop_atr * atr
        out["long_exit"] = r > p.exit_level
        out["short_exit"] = r < 100 - p.exit_level
        out["rsi_mr"] = r
        trend_strength = ((ef - es) / atr).fillna(0)
        out["pl_price_long"] = 0.5 * (trend_strength / 1.5).clip(0, 1) + 0.5 * (
            (c > f["vwap"]) | (c > es)
        ).astype(float)
        out["pl_price_short"] = 0.5 * (-trend_strength / 1.5).clip(0, 1) + 0.5 * (
            (c < f["vwap"]) | (c < es)
        ).astype(float)
        out["pl_momentum_long"] = ((p.entry_level - r) / p.entry_level).clip(0, 1) * 0.6 + 0.4
        out["pl_momentum_short"] = ((r - (100 - p.entry_level)) / p.entry_level).clip(
            0, 1
        ) * 0.6 + 0.4
        out["pl_volatility_long"] = out["pl_volatility_short"] = vol_pillar(f["atr_pct"])
        # quiet pullbacks are better for mean reversion: penalise very high relative volume
        out["pl_volume_long"] = out["pl_volume_short"] = 1 - (volume_pillar(f["rvol"]) - 0.6).clip(
            0, 1
        )
        out["long_score"] = np.nan
        out["short_score"] = np.nan
        return out

    def _reasons(self, f, i, side):
        row = f.iloc[i]
        if side == "long":
            return [
                f"Intraday uptrend (EMA{self.p.trend_fast} > EMA{self.p.trend_slow})",
                f"RSI({self.p.rsi_len}) = {row['rsi_mr']:.1f}: short-term oversold dip",
            ], []
        return [
            f"Intraday downtrend (EMA{self.p.trend_fast} < EMA{self.p.trend_slow})",
            f"RSI({self.p.rsi_len}) = {row['rsi_mr']:.1f}: short-term overbought bounce",
        ], []

    def implies(self, side: str) -> str:
        return (
            "A sharp dip inside an uptrend; a quick snap-back toward the mean is statistically likely - "
            "small, frequent wins, so respect the stop."
            if side == "long"
            else "A sharp bounce inside a downtrend; a quick fade toward the mean is statistically likely - "
            "small, frequent wins, so respect the stop."
        )
