"""S4 - Volatility Squeeze Breakout (Bollinger inside Keltner -> release + Donchian breakout).

Compression (BB inside KC for several bars) stores energy; the release with a Donchian breakout
in the direction of momentum and above-average volume is the trigger. Generalises ref 7's
"consolidation breakout" with an objective, volatility-normalised definition of consolidation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field

from ..indicators import core as ind
from .base import Strategy, StrategyParams, opt, vol_pillar, volume_pillar
from .regime import NEUTRAL, RANGE, TREND, VOLATILE


class SqueezeParams(StrategyParams):
    bb_len: int = Field(20, description="Bollinger length", json_schema_extra=opt(14, 30, 2))
    bb_k: float = Field(2.0, description="Bollinger std-dev multiplier")
    kc_mult: float = Field(
        1.5, description="Keltner ATR multiplier", json_schema_extra=opt(1.0, 2.0, 0.25)
    )
    min_squeeze_bars: int = Field(
        6,
        description="Minimum bars in squeeze before a release counts",
        json_schema_extra=opt(3, 15, 2),
    )
    release_window: int = Field(
        3,
        description="Breakout must come within this many bars of the release",
        json_schema_extra=opt(1, 6, 1),
    )
    dc_len: int = Field(
        20, description="Donchian breakout length", json_schema_extra=opt(10, 30, 5)
    )
    mom_len: int = Field(12, description="Momentum length", json_schema_extra=opt(8, 20, 2))
    rvol_min: float = Field(
        1.1, description="Minimum relative volume", json_schema_extra=opt(0.8, 2.0, 0.2)
    )
    stop_buffer_atr: float = Field(
        0.1, description="Stop beyond the squeeze range by this many ATR"
    )
    regime_filter: bool = Field(
        False, description="Only trade in the regimes this strategy is designed for"
    )


class SqueezeBreakout(Strategy):
    name = "squeeze"
    label = "Volatility Squeeze Breakout"
    description = "Enter when a Bollinger-inside-Keltner squeeze releases with a Donchian breakout and volume."
    regimes = (RANGE, NEUTRAL, TREND, VOLATILE)
    Params = SqueezeParams

    def raw_signals(self, f: pd.DataFrame) -> pd.DataFrame:
        p: SqueezeParams = self.p  # type: ignore[assignment]
        h, l, c = f["high"], f["low"], f["close"]
        bb = ind.bollinger(c, p.bb_len, p.bb_k)
        kc = ind.keltner(h, l, c, p.bb_len, p.kc_mult, p.bb_len)
        sq = (bb["bb_upper"] < kc["kc_upper"]) & (bb["bb_lower"] > kc["kc_lower"])
        run = sq.astype(int).groupby((~sq).cumsum().to_numpy()).cumsum()
        released_len = run.shift(1).where(sq.shift(1, fill_value=False) & ~sq, 0).fillna(0)
        recent_release = (
            released_len.rolling(p.release_window, min_periods=1).max() >= p.min_squeeze_bars
        )
        dc = ind.donchian(h, l, p.dc_len, exclude_current=True)
        mid = (
            (h.rolling(p.mom_len).max() + l.rolling(p.mom_len).min()) / 2 + ind.sma(c, p.mom_len)
        ) / 2
        mom = ind.linreg_slope(c - mid, p.mom_len)
        vol_ok = f["rvol"] >= p.rvol_min
        long_entry = (
            recent_release
            & (c > dc["dc_upper"])
            & (mom > 0)
            & (mom > mom.shift(1))
            & (c > f["vwap"])
            & vol_ok
        )
        short_entry = (
            recent_release
            & (c < dc["dc_lower"])
            & (mom < 0)
            & (mom < mom.shift(1))
            & (c < f["vwap"])
            & vol_ok
        )
        # one signal per release
        long_entry = long_entry & ~long_entry.shift(1, fill_value=False).rolling(
            p.release_window, min_periods=1
        ).max().astype(bool)
        short_entry = short_entry & ~short_entry.shift(1, fill_value=False).rolling(
            p.release_window, min_periods=1
        ).max().astype(bool)
        look = p.min_squeeze_bars + p.release_window
        buf = p.stop_buffer_atr * f["atr"]
        out = pd.DataFrame(index=f.index)
        out["long_entry"], out["short_entry"] = long_entry, short_entry
        out["long_stop"] = l.rolling(look, min_periods=1).min() - buf
        out["short_stop"] = h.rolling(look, min_periods=1).max() + buf
        out["squeeze_on"], out["mom"] = sq, mom
        brk_up = ((c - dc["dc_upper"]) / f["atr"]).clip(0, 2)
        brk_dn = ((dc["dc_lower"] - c) / f["atr"]).clip(0, 2)
        out["pl_price_long"] = (1 - (brk_up - 0.4).abs() / 1.6).clip(0, 1)
        out["pl_price_short"] = (1 - (brk_dn - 0.4).abs() / 1.6).clip(0, 1)
        mom_n = (mom / f["atr"]).fillna(0)
        out["pl_momentum_long"] = (mom_n * 5).clip(0, 1)
        out["pl_momentum_short"] = (-mom_n * 5).clip(0, 1)
        # for a squeeze, *low* prior volatility is good: reward a low ATR percentile
        out["pl_volatility_long"] = out["pl_volatility_short"] = 0.5 * vol_pillar(
            f["atr_pct"]
        ) + 0.5 * (1 - f["atr_pct"].fillna(50) / 100)
        out["pl_volume_long"] = out["pl_volume_short"] = volume_pillar(f["rvol"])
        out["long_score"] = np.nan
        out["short_score"] = np.nan
        return out

    def _reasons(self, f, i, side):
        word = "upside" if side == "long" else "downside"
        return (
            [
                f"Volatility squeeze (Bollinger inside Keltner) of {self.p.min_squeeze_bars}+ bars just released",
                f"{self.p.dc_len}-bar Donchian {word} breakout with momentum agreeing",
            ],
            [],
        )

    def implies(self, side: str) -> str:
        return (
            "Compression resolved upward; expansion moves after squeezes tend to travel 1-2x the squeeze range."
            if side == "long"
            else "Compression resolved downward; expansion moves after squeezes tend to travel 1-2x the squeeze range."
        )
