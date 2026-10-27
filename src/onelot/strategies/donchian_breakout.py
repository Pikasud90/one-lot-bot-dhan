"""Donchian channel breakout: the classic trend-following rule (Turtle-style).

Enter on a close above the N-bar high (short: below the N-bar low), exit on a
close through the M-bar opposite channel (M < N) or the shared stops. The
oldest documented systematic edge in futures and currencies, included here as
a benchmark for the index-futures and currency instruments at 15m/60m/daily.
An ADX floor removes the churn in flat markets; the initial stop is the
opposite side of the entry channel capped at `stop_atr_max` ATR.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field

from ..indicators import core as ind
from .base import Strategy, StrategyParams, opt, vol_pillar, volume_pillar
from .regime import NEUTRAL, TREND, VOLATILE


class DonchianParams(StrategyParams):
    entry_len: int = Field(20, description="Breakout channel length", json_schema_extra=opt(10, 55, 5))
    exit_len: int = Field(10, description="Exit channel length", json_schema_extra=opt(5, 20, 5))
    adx_min: float = Field(15, description="Minimum ADX(14)", json_schema_extra=opt(10, 30, 5))
    stop_atr_max: float = Field(3.0, description="Cap the initial stop at this many ATR")
    stop_buffer_atr: float = Field(0.1, description="Stop buffer beyond the channel, in ATR")
    target_r: float = Field(0.0, description="Target in R (0 = ride the channel)",
                            json_schema_extra=opt(0.0, 4.0, 0.5))
    trail_atr_mult: float = Field(2.5, description="ATR trail", json_schema_extra=opt(0.0, 4.0, 0.5))
    trail_activate_r: float = Field(1.0, description="Trail after this many R")
    cooldown_bars: int = Field(2, description="Bars to wait after an exit")
    min_risk_pct: float = Field(0.1, description="Friction floor")


class DonchianBreakout(Strategy):
    name = "donchian_breakout"
    label = "Donchian channel breakout"
    description = "Close beyond the N-bar channel with ADX above a floor; exit through the M-bar opposite channel."
    regimes = (TREND, NEUTRAL, VOLATILE)
    Params = DonchianParams

    def raw_signals(self, f: pd.DataFrame) -> pd.DataFrame:
        p: DonchianParams = self.p  # type: ignore[assignment]
        c, atr = f["close"], f["atr"]
        dc = ind.donchian(f["high"], f["low"], p.entry_len, exclude_current=True)
        dx = ind.donchian(f["high"], f["low"], p.exit_len, exclude_current=True)
        adx_ok = f["adx"] >= p.adx_min
        long_entry = (c > dc["dc_upper"]) & adx_ok
        short_entry = (c < dc["dc_lower"]) & adx_ok
        buf = p.stop_buffer_atr * atr
        long_stop = np.maximum(dc["dc_lower"] - buf, c - p.stop_atr_max * atr)
        short_stop = np.minimum(dc["dc_upper"] + buf, c + p.stop_atr_max * atr)
        out = pd.DataFrame(index=f.index)
        out["long_entry"], out["short_entry"] = long_entry, short_entry
        out["long_stop"], out["short_stop"] = long_stop, short_stop
        out["long_exit"] = c < dx["dc_lower"]
        out["short_exit"] = c > dx["dc_upper"]
        out["dc_upper"], out["dc_lower"] = dc["dc_upper"], dc["dc_lower"]
        brk_up = ((c - dc["dc_upper"]) / atr).clip(0, 2)
        brk_dn = ((dc["dc_lower"] - c) / atr).clip(0, 2)
        out["pl_price_long"] = (1 - (brk_up - 0.5).abs() / 1.5).clip(0, 1)
        out["pl_price_short"] = (1 - (brk_dn - 0.5).abs() / 1.5).clip(0, 1)
        out["pl_momentum_long"] = ((f["adx"] - 15) / 25).clip(0, 1)
        out["pl_momentum_short"] = ((f["adx"] - 15) / 25).clip(0, 1)
        out["pl_volatility_long"] = out["pl_volatility_short"] = vol_pillar(f["atr_pct"])
        out["pl_volume_long"] = out["pl_volume_short"] = volume_pillar(f["rvol"])
        out["long_score"] = np.nan
        out["short_score"] = np.nan
        return out

    def _reasons(self, f, i, side):
        row = f.iloc[i]
        if side == "long":
            return [f"Closed above the {self.p.entry_len}-bar high {row['dc_upper']:.2f}"], []
        return [f"Closed below the {self.p.entry_len}-bar low {row['dc_lower']:.2f}"], []

    def implies(self, side: str) -> str:
        return ("Range expansion to new highs; trend followers ride it until the exit channel breaks."
                if side == "long" else
                "Range expansion to new lows; trend followers ride it until the exit channel breaks.")
