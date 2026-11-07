"""S3 - Supertrend + ADX + MACD trend entry ("Brahmastra-lite", refs 1/3/5, fixed).

Fixes the reference repo's main flaw (fresh Supertrend flip AND fresh MACD cross on the *same* bar,
which almost never happens): here the flip opens a window of `flip_window` bars during which ADX
strength/direction, MACD agreement and a VWAP no-chase filter must line up. Only the first
qualifying bar after each flip is taken.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field

from ..indicators import core as ind
from .base import Strategy, StrategyParams, first_since, opt, vol_pillar, volume_pillar
from .regime import NEUTRAL, TREND, VOLATILE


class STParams(StrategyParams):
    st_period: int = Field(10, description="Supertrend ATR period", json_schema_extra=opt(7, 20, 1))
    st_mult: float = Field(
        3.0, description="Supertrend multiplier", json_schema_extra=opt(1.5, 4.0, 0.5)
    )
    flip_window: int = Field(
        3,
        description="Bars after a flip during which an entry may trigger",
        json_schema_extra=opt(1, 6, 1),
    )
    adx_min: float = Field(20, description="Minimum ADX(14)", json_schema_extra=opt(15, 30, 5))
    adx_rising_bars: int = Field(
        3,
        description="ADX must exceed its value this many bars ago",
        json_schema_extra=opt(1, 5, 1),
    )
    macd_fast: int = Field(12, description="MACD fast EMA")
    macd_slow: int = Field(26, description="MACD slow EMA")
    macd_signal: int = Field(9, description="MACD signal EMA")
    max_vwap_dist_atr: float = Field(
        1.5,
        description="No-chase: max distance from VWAP in ATR",
        json_schema_extra=opt(0.5, 3.0, 0.5),
    )
    rvol_min: float = Field(
        0.9, description="Minimum relative volume", json_schema_extra=opt(0.5, 1.5, 0.2)
    )
    stop_buffer_atr: float = Field(
        0.05, description="Stop beyond the Supertrend line by this many ATR"
    )
    stop_atr_max: float = Field(
        8.0, description="Supertrend stops are ~mult x ATR away by construction"
    )
    exit_on_flip: bool = Field(True, description="Exit when Supertrend flips against the position")
    target_r: float = Field(
        0.0,
        description="Profit target in R (0 = ride the trend with the trailing stop)",
        json_schema_extra=opt(0.0, 4.0, 0.5),
    )


class SupertrendADX(Strategy):
    name = "supertrend_adx"
    label = "Supertrend + ADX + MACD"
    description = (
        "Trend-following entry on a Supertrend flip confirmed by rising ADX, DI direction and MACD."
    )
    regimes = (TREND, NEUTRAL, VOLATILE)
    Params = STParams

    def raw_signals(self, f: pd.DataFrame) -> pd.DataFrame:
        p: STParams = self.p  # type: ignore[assignment]
        c = f["close"]
        st = ind.supertrend(f["high"], f["low"], c, p.st_period, p.st_mult)
        d = st["st_dir"]
        flip = (d != d.shift(1)) & d.shift(1).isin([1.0, -1.0])
        bars_since_flip = flip.astype(int).groupby(flip.cumsum().to_numpy()).cumcount()
        recent = (bars_since_flip < p.flip_window) & (flip.cumsum() > 0)
        m = ind.macd(c, p.macd_fast, p.macd_slow, p.macd_signal)
        hist = m["macd_hist"]
        adx_up = f["adx"] > f["adx"].shift(p.adx_rising_bars)
        adx_ok = (f["adx"] >= p.adx_min) & adx_up
        dist = (c - f["vwap"]) / f["atr"]
        vol_ok = (f["rvol"] >= p.rvol_min) | f["rvol"].isna()  # neutral without volume

        long_c = (
            (d == 1)
            & recent
            & adx_ok
            & (f["plus_di"] > f["minus_di"])
            & (hist > 0)
            & (hist >= hist.shift(1))
            & (dist > 0)
            & (dist <= p.max_vwap_dist_atr)
            & vol_ok
        )
        short_c = (
            (d == -1)
            & recent
            & adx_ok
            & (f["minus_di"] > f["plus_di"])
            & (hist < 0)
            & (hist <= hist.shift(1))
            & (dist < 0)
            & (dist >= -p.max_vwap_dist_atr)
            & vol_ok
        )
        long_entry = first_since(long_c, flip)
        short_entry = first_since(short_c, flip)

        buf = p.stop_buffer_atr * f["atr"]
        out = pd.DataFrame(index=f.index)
        out["long_entry"], out["short_entry"] = long_entry, short_entry
        out["long_stop"] = st["st_line"] - buf
        out["short_stop"] = st["st_line"] + buf
        out["long_exit"] = (d == -1) if p.exit_on_flip else False
        out["short_exit"] = (d == 1) if p.exit_on_flip else False
        out["st_line"], out["st_dir"], out["macd_hist"] = st["st_line"], d, hist
        di_spread = (f["plus_di"] - f["minus_di"]) / 30
        out["pl_price_long"] = 0.5 * (1 - (dist - 0.6).abs() / 1.2).clip(0, 1) + 0.5 * (
            c > f["ema20"]
        )
        out["pl_price_short"] = 0.5 * (1 - (dist + 0.6).abs() / 1.2).clip(0, 1) + 0.5 * (
            c < f["ema20"]
        )
        out["pl_momentum_long"] = 0.5 * ((f["adx"] - 18) / 20).clip(0, 1) + 0.5 * di_spread.clip(
            0, 1
        )
        out["pl_momentum_short"] = 0.5 * ((f["adx"] - 18) / 20).clip(0, 1) + 0.5 * (
            -di_spread
        ).clip(0, 1)
        out["pl_volatility_long"] = out["pl_volatility_short"] = vol_pillar(f["atr_pct"])
        out["pl_volume_long"] = out["pl_volume_short"] = volume_pillar(f["rvol"])
        out["long_score"] = np.nan
        out["short_score"] = np.nan
        return out

    def _reasons(self, f, i, side):
        row = f.iloc[i]
        word = "up" if side == "long" else "down"
        return (
            [
                f"Supertrend({self.p.st_period},{self.p.st_mult}) flipped {word}; line {row['st_line']:.2f}",
                f"ADX {row['adx']:.0f} rising, +DI {row['plus_di']:.0f} / -DI {row['minus_di']:.0f}",
                f"MACD histogram {row['macd_hist']:+.3f} confirming",
            ],
            [],
        )

    def implies(self, side: str) -> str:
        return (
            "A new up-trend leg has started with expanding directional strength; ride it while the Supertrend holds."
            if side == "long"
            else "A new down-trend leg has started with expanding directional strength; ride it while the Supertrend holds."
        )
