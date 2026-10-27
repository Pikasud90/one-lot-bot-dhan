"""S1 - VWAP Pullback Continuation.

Trend day: price holds above a rising session VWAP with EMA20 > EMA50. Price pulls back into the
VWAP/EMA20 zone without breaking it, then a reclaim candle closes above the prior bar's high with
short RSI turning up. The trigger is price structure (fast); RSI/ADX/volume are filters.
Shorts mirror the logic below a falling VWAP. Sources: refs 6 (VWAP idea, fixed to session VWAP)
and 1 (VWAP no-chase filter).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field

from ..indicators import core as ind
from .base import Strategy, StrategyParams, opt, vol_pillar, volume_pillar
from .regime import NEUTRAL, TREND, VOLATILE


class VWAPPullbackParams(StrategyParams):
    vwap_slope_bars: int = Field(6, description="Bars over which VWAP must be rising/falling", json_schema_extra=opt(3, 12, 3))
    pullback_bars: int = Field(4, description="Look-back window for the pullback", json_schema_extra=opt(2, 8, 1))
    zone_atr: float = Field(0.3, description="Pullback must come within this many ATR of VWAP/EMA20", json_schema_extra=opt(0.1, 0.8, 0.1))
    max_pierce_atr: float = Field(0.3, description="Pullback may pierce VWAP by at most this many ATR", json_schema_extra=opt(0.0, 0.8, 0.1))
    rsi_len: int = Field(7, description="Short RSI length", json_schema_extra=opt(5, 14, 1))
    rsi_lo: float = Field(40, description="Long trigger RSI lower bound (short: 100-hi)", json_schema_extra=opt(30, 50, 5))
    rsi_hi: float = Field(72, description="Long trigger RSI upper bound (short: 100-lo)", json_schema_extra=opt(60, 80, 4))
    adx_min: float = Field(15, description="Minimum ADX(14)", json_schema_extra=opt(10, 30, 5))
    rvol_min: float = Field(0.8, description="Minimum relative volume", json_schema_extra=opt(0.5, 1.5, 0.25))
    stop_buffer_atr: float = Field(0.1, description="Stop below pullback low by this many ATR")
    vwap_exit: bool = Field(True, description="Exit when a bar closes back through VWAP")


class VWAPPullback(Strategy):
    name = "vwap_pullback"
    label = "VWAP Pullback"
    description = ("Trend continuation: buy the first reclaim after a shallow pullback to a rising VWAP "
                   "(short the mirror below a falling VWAP).")
    regimes = (TREND, NEUTRAL, VOLATILE)
    Params = VWAPPullbackParams

    def raw_signals(self, f: pd.DataFrame) -> pd.DataFrame:
        p: VWAPPullbackParams = self.p  # type: ignore[assignment]
        c, o, h, l = f["close"], f["open"], f["high"], f["low"]
        atr, vwap, e20, e50 = f["atr"], f["vwap"], f["ema20"], f["ema50"]
        r = ind.rsi(c, p.rsi_len)
        slope = vwap - vwap.shift(p.vwap_slope_bars)
        same_day = pd.Series(f["bar_no"].to_numpy() >= p.vwap_slope_bars, index=f.index)
        pb_low = l.rolling(p.pullback_bars, min_periods=1).min()
        pb_high = h.rolling(p.pullback_bars, min_periods=1).max()
        adx_ok = f["adx"] >= p.adx_min
        vol_ok = f["rvol"] >= p.rvol_min

        up = (c > vwap) & (slope > 0) & (e20 > e50) & same_day
        touched_up = pb_low <= np.maximum(vwap, e20) + p.zone_atr * atr
        held_up = pb_low >= vwap - p.max_pierce_atr * atr
        trig_up = (c > o) & (c > h.shift(1)) & (r > r.shift(1)) & r.between(p.rsi_lo, p.rsi_hi)
        long_entry = up & touched_up & held_up & trig_up & adx_ok & vol_ok

        dn = (c < vwap) & (slope < 0) & (e20 < e50) & same_day
        touched_dn = pb_high >= np.minimum(vwap, e20) - p.zone_atr * atr
        held_dn = pb_high <= vwap + p.max_pierce_atr * atr
        trig_dn = (c < o) & (c < l.shift(1)) & (r < r.shift(1)) & r.between(100 - p.rsi_hi, 100 - p.rsi_lo)
        short_entry = dn & touched_dn & held_dn & trig_dn & adx_ok & vol_ok

        dist = (c - vwap) / atr
        out = pd.DataFrame(index=f.index)
        out["long_entry"], out["short_entry"] = long_entry, short_entry
        out["long_stop"] = pb_low - p.stop_buffer_atr * atr
        out["short_stop"] = pb_high + p.stop_buffer_atr * atr
        out["long_exit"] = (c < vwap) if p.vwap_exit else False
        out["short_exit"] = (c > vwap) if p.vwap_exit else False
        # pillars
        out["pl_price_long"] = 0.5 * (e20 > e50) + 0.5 * (1 - (dist - 0.5).abs().clip(0, 1.5) / 1.5)
        out["pl_price_short"] = 0.5 * (e20 < e50) + 0.5 * (1 - (dist + 0.5).abs().clip(0, 1.5) / 1.5)
        out["pl_momentum_long"] = 0.5 * ((r - 45) / 25).clip(0, 1) + 0.5 * ((f["adx"] - 15) / 20).clip(0, 1)
        out["pl_momentum_short"] = 0.5 * ((55 - r) / 25).clip(0, 1) + 0.5 * ((f["adx"] - 15) / 20).clip(0, 1)
        out["pl_volatility_long"] = out["pl_volatility_short"] = vol_pillar(f["atr_pct"])
        out["pl_volume_long"] = out["pl_volume_short"] = volume_pillar(f["rvol"])
        out["long_score"] = np.nan
        out["short_score"] = np.nan
        return out

    def _reasons(self, f, i, side):
        row = f.iloc[i]
        if side == "long":
            return ([f"Uptrend: above rising VWAP, EMA20 {row['ema20']:.2f} > EMA50 {row['ema50']:.2f}",
                     "Pulled back into the VWAP/EMA20 zone and reclaimed it with a strong close"], [])
        return ([f"Downtrend: below falling VWAP, EMA20 {row['ema20']:.2f} < EMA50 {row['ema50']:.2f}",
                 "Rallied into the VWAP/EMA20 zone and was rejected with a weak close"], [])

    def implies(self, side: str) -> str:
        return ("Buyers defended VWAP; trend continuation is likely while price holds above the pullback low."
                if side == "long" else
                "Sellers defended VWAP; downtrend continuation is likely while price stays below the rally high.")
