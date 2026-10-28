"""S14 - Turtle Soup (failed-breakout reversal).

Raschke & Connors, *Street Smarts* (1996): when price makes a new N-bar low that takes
out a prior low set at least a few bars earlier, and then closes back above that prior
low, the breakout has failed and the trapped breakout traders fuel a snap-back. Stop
just beyond the new extreme; modest R target and a time stop (it is a mean-reversion
trade, not a trend trade). Mirror for shorts. The intraday version here uses N bars of
the signal timeframe instead of the original 20 daily bars.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field

from .base import Strategy, StrategyParams, opt, vol_pillar, volume_pillar
from .regime import NEUTRAL, RANGE, VOLATILE


class TSParams(StrategyParams):
    lookback: int = Field(20, description="N-bar low/high that must be broken", json_schema_extra=opt(10, 40, 5))
    min_prior_age: int = Field(4, description="The prior extreme must be at least this many bars old",
                               json_schema_extra=opt(2, 8, 1))
    min_break_atr: float = Field(0.1, description="New extreme must exceed the prior one by this many ATR(14)",
                                 json_schema_extra=opt(0.0, 0.5, 0.05))
    stop_buffer_atr: float = Field(0.1, description="Stop beyond the new extreme by this many ATR(14)",
                                   json_schema_extra=opt(0.0, 0.3, 0.05))
    target_r: float = Field(1.5, description="Profit target in R", json_schema_extra=opt(0.5, 3.0, 0.5))
    trail_atr_mult: float = Field(0.0, description="Trailing stop k x ATR (0 = off)", json_schema_extra=opt(0.0, 2.0, 0.5))
    max_bars: int = Field(24, description="Time stop in bars", json_schema_extra=opt(6, 36, 6))
    regime_filter: bool = Field(True, description="Only in range / neutral / volatile regimes (not strong trends)")
    max_trades_per_day: int = Field(2, description="Per symbol per day", json_schema_extra=opt(1, 3, 1))


class TurtleSoup(Strategy):
    name = "turtle_soup"
    label = "Turtle Soup (failed breakout)"
    description = ("Fade a breakout that fails: a new N-bar extreme beyond an older one that closes back "
                   "inside; stop beyond the extreme, small target, time stop.")
    regimes = (RANGE, NEUTRAL, VOLATILE)
    Params = TSParams

    def raw_signals(self, f: pd.DataFrame) -> pd.DataFrame:
        p: TSParams = self.p  # type: ignore[assignment]
        c, h, l, atr = f["close"], f["high"], f["low"], f["atr"]
        n = int(p.lookback)
        prior_low = l.shift(1).rolling(n).min()
        prior_high = h.shift(1).rolling(n).max()
        # age of the prior extreme in bars (position inside the window, from the right)
        low_age = n - l.shift(1).rolling(n).apply(np.argmin, raw=True) - 1
        high_age = n - h.shift(1).rolling(n).apply(np.argmax, raw=True) - 1
        new_low = l < prior_low - p.min_break_atr * atr
        new_high = h > prior_high + p.min_break_atr * atr
        long_raw = new_low & (low_age >= p.min_prior_age) & (c > prior_low) & ~f["new_day"]
        short_raw = new_high & (high_age >= p.min_prior_age) & (c < prior_high) & ~f["new_day"]
        long_raw = (long_raw & ~long_raw.shift(1, fill_value=False)).fillna(False)
        short_raw = (short_raw & ~short_raw.shift(1, fill_value=False)).fillna(False)
        out = pd.DataFrame(index=f.index)
        out["long_entry"], out["short_entry"] = long_raw.astype(bool), short_raw.astype(bool)
        out["long_stop"] = l - p.stop_buffer_atr * atr
        out["short_stop"] = h + p.stop_buffer_atr * atr
        out["ts_prior_low"], out["ts_prior_high"] = prior_low, prior_high
        depth_l = ((prior_low - l) / atr).clip(0, 1)
        depth_s = ((h - prior_high) / atr).clip(0, 1)
        out["pl_price_long"], out["pl_price_short"] = depth_l, depth_s
        out["pl_momentum_long"] = ((30 - f["rsi"]) / 20 + 0.5).clip(0, 1)
        out["pl_momentum_short"] = ((f["rsi"] - 70) / 20 + 0.5).clip(0, 1)
        out["pl_volatility_long"] = out["pl_volatility_short"] = vol_pillar(f["atr_pct"])
        out["pl_volume_long"] = out["pl_volume_short"] = volume_pillar(f["rvol"])
        out["long_score"] = np.nan
        out["short_score"] = np.nan
        return out

    def _reasons(self, f, i, side):
        row = f.iloc[i]
        if side == "long":
            return [f"New {self.p.lookback}-bar low below {row['ts_prior_low']:.2f}, then a close back above it",
                    "Breakdown traders are trapped; snap-back expected"], []
        return [f"New {self.p.lookback}-bar high above {row['ts_prior_high']:.2f}, then a close back below it",
                "Breakout traders are trapped; snap-back expected"], []

    def implies(self, side: str) -> str:
        return ("A failed breakdown; the trade is only right if price never revisits the new low."
                if side == "long" else
                "A failed breakout; the trade is only right if price never revisits the new high.")
