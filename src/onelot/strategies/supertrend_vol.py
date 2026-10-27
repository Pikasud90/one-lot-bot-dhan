"""Supertrend with tuned parameters, volume and momentum confirmation.

The operator's second thesis: the Supertrend indicator by itself, with the
period/multiplier chosen by testing rather than by habit, and the false flips
removed by a momentum and volume check.

Unlike `supertrend_adx` (which also demands a MACD agreement and a VWAP
no-chase), this is the lean version: flip + RSI + relative volume + a
minimum ADX, so the optimizer can see what the Supertrend settings alone are
worth. Stop at the Supertrend line; exit on the opposite flip.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field

from ..indicators import core as ind
from .base import Strategy, StrategyParams, first_since, opt, vol_pillar, volume_pillar
from .regime import NEUTRAL, TREND, VOLATILE


class STVolParams(StrategyParams):
    st_period: int = Field(10, description="Supertrend ATR period", json_schema_extra=opt(5, 21, 1))
    st_mult: float = Field(2.5, description="Supertrend multiplier", json_schema_extra=opt(1.0, 4.0, 0.25))
    flip_window: int = Field(2, description="Bars after a flip during which an entry may trigger",
                             json_schema_extra=opt(1, 5, 1))
    adx_min: float = Field(18, description="Minimum ADX(14)", json_schema_extra=opt(10, 30, 2))
    rsi_long_min: float = Field(52, description="RSI(14) must be above this for longs",
                                json_schema_extra=opt(45, 65, 1))
    rsi_short_max: float = Field(48, description="RSI(14) must be below this for shorts",
                                 json_schema_extra=opt(35, 55, 1))
    rvol_min: float = Field(1.0, description="Minimum relative volume on the entry bar (ignored without volume)",
                            json_schema_extra=opt(0.5, 2.0, 0.25))
    stop_buffer_atr: float = Field(0.1, description="Stop beyond the Supertrend line by this many ATR")
    stop_atr_max: float = Field(8.0, description="Supertrend stops are ~mult x ATR away by construction")
    exit_on_flip: bool = Field(True, description="Exit when Supertrend flips against the position")
    target_r: float = Field(0.0, description="Profit target in R (0 = ride the flip)",
                            json_schema_extra=opt(0.0, 4.0, 0.5))
    trail_atr_mult: float = Field(0.0, description="ATR trail (0 = the Supertrend line is the trail)",
                                  json_schema_extra=opt(0.0, 3.0, 0.5))
    min_risk_pct: float = Field(0.15, description="Friction floor on the initial risk, % of price")


class SupertrendVol(Strategy):
    name = "supertrend_vol"
    label = "Supertrend (tuned) + volume + momentum"
    description = ("A Supertrend flip confirmed by RSI direction, relative volume and a minimum ADX; "
                   "period and multiplier are meant to be tuned in the optimizer.")
    regimes = (TREND, NEUTRAL, VOLATILE)
    Params = STVolParams

    def raw_signals(self, f: pd.DataFrame) -> pd.DataFrame:
        p: STVolParams = self.p  # type: ignore[assignment]
        c = f["close"]
        st = ind.supertrend(f["high"], f["low"], c, p.st_period, p.st_mult)
        d = st["st_dir"]
        flip = (d != d.shift(1)) & d.shift(1).isin([1.0, -1.0])
        since = flip.astype(int).groupby(flip.cumsum().to_numpy()).cumcount()
        recent = (since < p.flip_window) & (flip.cumsum() > 0)
        rv = f["rvol"]
        vol_ok = (rv >= p.rvol_min) | rv.isna()
        adx_ok = f["adx"] >= p.adx_min
        long_c = (d == 1) & recent & adx_ok & (f["rsi"] >= p.rsi_long_min) & vol_ok
        short_c = (d == -1) & recent & adx_ok & (f["rsi"] <= p.rsi_short_max) & vol_ok
        long_entry = first_since(long_c, flip)
        short_entry = first_since(short_c, flip)
        buf = p.stop_buffer_atr * f["atr"]
        out = pd.DataFrame(index=f.index)
        out["long_entry"], out["short_entry"] = long_entry, short_entry
        out["long_stop"] = st["st_line"] - buf
        out["short_stop"] = st["st_line"] + buf
        out["long_exit"] = (d == -1) if p.exit_on_flip else False
        out["short_exit"] = (d == 1) if p.exit_on_flip else False
        out["st_line"], out["st_dir"] = st["st_line"], d
        dist = ((c - st["st_line"]).abs() / f["atr"]).fillna(0)
        out["pl_price_long"] = out["pl_price_short"] = (1 - (dist - 1.0).abs() / 2.0).clip(0, 1)
        out["pl_momentum_long"] = 0.5 * ((f["adx"] - 15) / 20).clip(0, 1) + 0.5 * ((f["rsi"] - 50) / 20).clip(0, 1)
        out["pl_momentum_short"] = 0.5 * ((f["adx"] - 15) / 20).clip(0, 1) + 0.5 * ((50 - f["rsi"]) / 20).clip(0, 1)
        out["pl_volatility_long"] = out["pl_volatility_short"] = vol_pillar(f["atr_pct"])
        out["pl_volume_long"] = out["pl_volume_short"] = volume_pillar(rv)
        out["long_score"] = np.nan
        out["short_score"] = np.nan
        return out

    def _reasons(self, f, i, side):
        row = f.iloc[i]
        word = "up" if side == "long" else "down"
        return ([f"Supertrend({self.p.st_period},{self.p.st_mult}) flipped {word}; line {row['st_line']:.2f}",
                 f"RSI {row['rsi']:.0f}, ADX {row['adx']:.0f}, relative volume {row['rvol']:.1f}x"
                 if not np.isnan(row.get("rvol", np.nan)) else f"RSI {row['rsi']:.0f}, ADX {row['adx']:.0f}"], [])

    def implies(self, side: str) -> str:
        return ("Trend flip with confirmation; the Supertrend line is the stop and the trail."
                if side == "long" else
                "Trend flip down with confirmation; the Supertrend line is the stop and the trail.")
