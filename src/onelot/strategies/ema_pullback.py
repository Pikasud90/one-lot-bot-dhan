"""S13 - ADX trend + EMA pullback ("Holy Grail").

Linda Raschke & Larry Connors, *Street Smarts* (1996): with ADX(14) above 30 and rising
(a trend that is actually trending), buy the first pullback to the 20-EMA and enter when
price resumes (close above the prior bar's high); stop under the pullback swing low.
Here the ADX threshold is a parameter because intraday ADX rarely reaches 30 on 5-minute
bars. The pattern is widely re-implemented (TradingView "Holy Grail" scripts, GitHub
backtests); the evidence for it is anecdotal, so it goes through the same gate as the rest.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field

from ..indicators import core as ind
from .base import Strategy, StrategyParams, opt, vol_pillar, volume_pillar
from .regime import TREND, VOLATILE


class HGParams(StrategyParams):
    adx_min: float = Field(
        25, description="Minimum ADX(14): the trend must be real", json_schema_extra=opt(20, 35, 5)
    )
    require_adx_rising: bool = Field(True, description="ADX must be higher than 3 bars ago")
    ema_len: int = Field(20, description="Pullback EMA length", json_schema_extra=opt(10, 30, 5))
    touch_bars: int = Field(
        3,
        description="The low must have touched the EMA within the last k bars",
        json_schema_extra=opt(1, 6, 1),
    )
    swing_bars: int = Field(
        5,
        description="Stop below the lowest low of the last k bars (mirror for shorts)",
        json_schema_extra=opt(3, 10, 1),
    )
    stop_buffer_atr: float = Field(
        0.2, description="Extra stop distance in ATR(14)", json_schema_extra=opt(0.0, 0.5, 0.1)
    )
    target_r: float = Field(
        2.0, description="Profit target in R", json_schema_extra=opt(0.0, 4.0, 0.5)
    )
    trail_atr_mult: float = Field(
        2.0, description="Trailing stop k x ATR (0 = off)", json_schema_extra=opt(0.0, 3.0, 0.5)
    )
    stop_atr_max: float = Field(
        3.0,
        description="Skip setups whose initial risk exceeds this many ATR(14)",
        json_schema_extra=opt(1.5, 5.0, 0.5),
    )


class EMAPullback(Strategy):
    name = "ema_pullback"
    label = "ADX Trend + EMA Pullback"
    description = (
        "Strong, rising ADX; wait for a pullback that touches the EMA, enter on the "
        "resumption bar; stop under the pullback low (Raschke/Connors 'Holy Grail')."
    )
    regimes = (TREND, VOLATILE)
    Params = HGParams

    def raw_signals(self, f: pd.DataFrame) -> pd.DataFrame:
        p: HGParams = self.p  # type: ignore[assignment]
        c, h, l, atr = f["close"], f["high"], f["low"], f["atr"]
        ema = ind.ema(c, p.ema_len)
        adx = f["adx"]
        strong = adx >= p.adx_min
        if p.require_adx_rising:
            strong = strong & (adx > adx.shift(3))
        up, dn = f["plus_di"] > f["minus_di"], f["minus_di"] > f["plus_di"]
        touched_lo = (l <= ema).rolling(p.touch_bars, min_periods=1).max().fillna(0) > 0
        touched_hi = (h >= ema).rolling(p.touch_bars, min_periods=1).max().fillna(0) > 0
        resume_up = (c > h.shift(1)) & (c > ema)
        resume_dn = (c < l.shift(1)) & (c < ema)
        long_raw = strong & up & touched_lo & resume_up & ~f["new_day"]
        short_raw = strong & dn & touched_hi & resume_dn & ~f["new_day"]
        # one entry per resumption: only the first bar where the condition turns on
        long_raw = (long_raw & ~long_raw.shift(1, fill_value=False)).fillna(False)
        short_raw = (short_raw & ~short_raw.shift(1, fill_value=False)).fillna(False)
        out = pd.DataFrame(index=f.index)
        out["long_entry"], out["short_entry"] = long_raw.astype(bool), short_raw.astype(bool)
        out["long_stop"] = l.rolling(p.swing_bars, min_periods=1).min() - p.stop_buffer_atr * atr
        out["short_stop"] = h.rolling(p.swing_bars, min_periods=1).max() + p.stop_buffer_atr * atr
        out["hg_ema"] = ema
        out["pl_price_long"] = ((c - ema) / atr).clip(0, 1)
        out["pl_price_short"] = ((ema - c) / atr).clip(0, 1)
        out["pl_momentum_long"] = out["pl_momentum_short"] = ((adx - p.adx_min) / 20.0 + 0.5).clip(
            0, 1
        )
        out["pl_volatility_long"] = out["pl_volatility_short"] = vol_pillar(f["atr_pct"])
        out["pl_volume_long"] = out["pl_volume_short"] = volume_pillar(f["rvol"])
        out["long_score"] = np.nan
        out["short_score"] = np.nan
        return out

    def _reasons(self, f, i, side):
        row = f.iloc[i]
        return [
            f"ADX {row['adx']:.0f} and rising: a trend that is trending",
            f"Pullback touched EMA{self.p.ema_len} ({row['hg_ema']:.2f}) and price resumed",
        ], []

    def implies(self, side: str) -> str:
        return (
            "Buying the dip in a confirmed uptrend; if the pullback low breaks, the trend thesis is wrong."
            if side == "long"
            else "Selling the bounce in a confirmed downtrend; if the bounce high breaks, the trend thesis is wrong."
        )
