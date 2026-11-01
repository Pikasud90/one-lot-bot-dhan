"""EMA crossover in a trending market, with momentum and volume confirmation.

The operator's first thesis: on Bank Nifty 12-minute bars, a fast/slow EMA
cross taken only when the market is trending, with momentum and volume in
agreement, is a clean alert once the false crosses are filtered out.

Filters that remove the false crosses (all tunable):
    * trend:    ADX(14) >= adx_min, and the slow EMA sloping the same way
    * momentum: RSI on the right side of 50 by `rsi_margin`
    * conviction: the close must sit at least `confirm_atr` x ATR beyond the
      slow EMA (a cross that barely clears the line is noise)
    * volume:   relative volume >= rvol_min (neutral when the series has none)
    * one entry per cross (`first_since` on the cross event)
Stop: the lower of the slow EMA and the recent swing low, minus a buffer, so
a losing cross exits as soon as the structure that justified it breaks.
Exit: the opposite cross (or the shared target/trail/time stops).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field

from ..indicators import core as ind
from .base import Strategy, StrategyParams, first_since, opt, vol_pillar, volume_pillar
from .regime import NEUTRAL, TREND, VOLATILE


class EMACrossParams(StrategyParams):
    timeframe: str = Field("12m", description="Signal timeframe (the thesis was formed on 12m)")
    fast: int = Field(9, description="Fast EMA", json_schema_extra=opt(5, 21, 1))
    slow: int = Field(21, description="Slow EMA", json_schema_extra=opt(13, 55, 1))
    adx_min: float = Field(
        20, description="Minimum ADX(14) for a trending market", json_schema_extra=opt(15, 30, 5)
    )
    slope_bars: int = Field(
        3,
        description="Slow EMA must have risen/fallen over this many bars",
        json_schema_extra=opt(1, 6, 1),
    )
    rsi_margin: float = Field(
        3.0,
        description="RSI(14) must be beyond 50 by this much in the trade direction",
        json_schema_extra=opt(0, 10, 1),
    )
    confirm_atr: float = Field(
        0.15,
        description="Close must clear the slow EMA by this many ATR",
        json_schema_extra=opt(0.0, 0.5, 0.05),
    )
    rvol_min: float = Field(
        1.0,
        description="Minimum relative volume (ignored when the series has none)",
        json_schema_extra=opt(0.5, 1.5, 0.25),
    )
    swing_bars: int = Field(
        6, description="Swing look-back for the structural stop", json_schema_extra=opt(3, 12, 1)
    )
    stop_buffer_atr: float = Field(0.2, description="Stop buffer beyond the structure, in ATR")
    stop_atr_max: float = Field(
        4.0, description="Skip setups whose structural stop exceeds this many ATR"
    )
    exit_on_cross: bool = Field(True, description="Exit on the opposite EMA cross")
    target_r: float = Field(
        3.0,
        description="Profit target in R (0 = ride with the trail)",
        json_schema_extra=opt(0.0, 4.0, 0.5),
    )
    trail_atr_mult: float = Field(
        2.0, description="Trailing stop distance k x ATR(n)", json_schema_extra=opt(0.0, 3.0, 0.5)
    )
    trail_activate_r: float = Field(
        1.5, description="Trail only after this many R", json_schema_extra=opt(0.5, 2.5, 0.5)
    )
    min_risk_pct: float = Field(0.15, description="Friction floor on the initial risk, % of price")


class EMACross(Strategy):
    name = "ema_cross"
    label = "EMA Cross (trend + momentum + volume)"
    description = (
        "Fast/slow EMA cross taken only in a trending market with RSI and relative volume "
        "agreeing; one entry per cross, structural stop, exit on the opposite cross."
    )
    regimes = (TREND, NEUTRAL, VOLATILE)
    Params = EMACrossParams

    def raw_signals(self, f: pd.DataFrame) -> pd.DataFrame:
        p: EMACrossParams = self.p  # type: ignore[assignment]
        c, atr = f["close"], f["atr"]
        ef, es = ind.ema(c, p.fast), ind.ema(c, p.slow)
        above = ef > es
        cross_up = above & ~above.shift(1, fill_value=False)
        cross_dn = ~above & above.shift(1, fill_value=False)
        slope = es - es.shift(p.slope_bars)
        adx_ok = f["adx"] >= p.adx_min
        rv = f["rvol"]
        vol_ok = (rv >= p.rvol_min) | rv.isna()
        long_c = (
            above
            & adx_ok
            & (slope > 0)
            & (f["rsi"] >= 50 + p.rsi_margin)
            & (c >= es + p.confirm_atr * atr)
            & vol_ok
        )
        short_c = (
            ~above
            & adx_ok
            & (slope < 0)
            & (f["rsi"] <= 50 - p.rsi_margin)
            & (c <= es - p.confirm_atr * atr)
            & vol_ok
        )
        long_entry = first_since(long_c, cross_up)
        short_entry = first_since(short_c, cross_dn)

        swing_lo = f["low"].rolling(p.swing_bars, min_periods=1).min()
        swing_hi = f["high"].rolling(p.swing_bars, min_periods=1).max()
        buf = p.stop_buffer_atr * atr
        out = pd.DataFrame(index=f.index)
        out["long_entry"], out["short_entry"] = long_entry, short_entry
        out["long_stop"] = np.minimum(es, swing_lo) - buf
        out["short_stop"] = np.maximum(es, swing_hi) + buf
        out["long_exit"] = cross_dn if p.exit_on_cross else False
        out["short_exit"] = cross_up if p.exit_on_cross else False
        out["ema_fast"], out["ema_slow"] = ef, es
        sep = ((ef - es) / atr).fillna(0)
        out["pl_price_long"] = (sep / 1.0).clip(0, 1)
        out["pl_price_short"] = (-sep / 1.0).clip(0, 1)
        out["pl_momentum_long"] = 0.5 * ((f["adx"] - 15) / 20).clip(0, 1) + 0.5 * (
            (f["rsi"] - 50) / 20
        ).clip(0, 1)
        out["pl_momentum_short"] = 0.5 * ((f["adx"] - 15) / 20).clip(0, 1) + 0.5 * (
            (50 - f["rsi"]) / 20
        ).clip(0, 1)
        out["pl_volatility_long"] = out["pl_volatility_short"] = vol_pillar(f["atr_pct"])
        out["pl_volume_long"] = out["pl_volume_short"] = volume_pillar(rv)
        out["long_score"] = np.nan
        out["short_score"] = np.nan
        return out

    def _reasons(self, f, i, side):
        row = f.iloc[i]
        word = "above" if side == "long" else "below"
        return (
            [
                f"EMA{self.p.fast} crossed {word} EMA{self.p.slow} ({row['ema_fast']:.2f} vs {row['ema_slow']:.2f})",
                f"ADX {row['adx']:.0f} (trending), RSI {row['rsi']:.0f}",
            ],
            [],
        )

    def implies(self, side: str) -> str:
        return (
            "Fresh momentum leg with the trend; stay while price holds above the slow EMA."
            if side == "long"
            else "Fresh momentum leg with the downtrend; stay while price holds below the slow EMA."
        )
