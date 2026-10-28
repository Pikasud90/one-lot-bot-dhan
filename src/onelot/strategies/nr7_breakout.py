"""S12 - NR7 range contraction breakout.

Toby Crabel (*Day Trading with Short Term Price Patterns and Opening Range Breakout*,
1990): when yesterday's range was the narrowest of the last N (7) sessions, volatility
tends to expand today. Trade the first break of yesterday's high (long) or low (short)
with a small ATR buffer; ATR or mid-range stop; R target plus trail.
Public implementations: countless "NR7" / "NR4" Pine scripts and Python notebooks; the
pattern itself needs no indicator beyond the daily range.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
from pydantic import Field

from .base import Strategy, StrategyParams, first_in_day, opt, vol_pillar, volume_pillar
from .regime import NEUTRAL, RANGE, TREND


class NR7Params(StrategyParams):
    nr_n: int = Field(7, description="Yesterday's range must be the narrowest of the last N sessions",
                      json_schema_extra=opt(4, 10, 1))
    buffer_atr: float = Field(0.05, description="Break yesterday's high/low by this many ATR(14)",
                              json_schema_extra=opt(0.0, 0.3, 0.05))
    stop_mode: Literal["atr", "pd_mid"] = Field("atr", description="Stop: close -/+ stop_atr x ATR, or yesterday's mid-range")
    stop_atr: float = Field(1.0, description="ATR stop distance", json_schema_extra=opt(0.5, 2.0, 0.25))
    require_inside_day: bool = Field(False, description="Also require yesterday to be an inside day (NR7-ID)")
    rvol_min: float = Field(0.9, description="Minimum relative volume on the breakout bar (ignored without volume)",
                            json_schema_extra=opt(0.7, 1.5, 0.1))
    stop_atr_max: float = Field(4.0, description="Skip setups whose initial risk exceeds this many ATR(14)",
                                json_schema_extra=opt(2.0, 6.0, 0.5))
    target_r: float = Field(2.0, description="Profit target in R", json_schema_extra=opt(0.0, 4.0, 0.5))
    trail_atr_mult: float = Field(1.5, description="Trailing stop k x ATR (0 = off)", json_schema_extra=opt(0.0, 3.0, 0.5))
    max_trades_per_day: int = Field(1, description="Per symbol per day", json_schema_extra=opt(1, 2, 1))


class NR7Breakout(Strategy):
    name = "nr7_breakout"
    label = "NR7 Breakout"
    description = ("After the narrowest daily range of the last N sessions, trade the first break of "
                   "yesterday's high or low (Crabel's range-contraction breakout).")
    regimes = (TREND, NEUTRAL, RANGE)
    Params = NR7Params

    def raw_signals(self, f: pd.DataFrame) -> pd.DataFrame:
        p: NR7Params = self.p  # type: ignore[assignment]
        c, atr = f["close"], f["atr"]
        day = f.index.normalize()
        d = f.groupby(day).agg(high=("high", "max"), low=("low", "min"))
        rng = d["high"] - d["low"]
        nr_day = (rng == rng.rolling(p.nr_n).min()) & rng.rolling(p.nr_n).count().ge(p.nr_n)
        inside = (d["high"] < d["high"].shift(1)) & (d["low"] > d["low"].shift(1))
        flag = nr_day & (inside if p.require_inside_day else True)
        # Yesterday's flag applies to today's bars (shift by one session = no look-ahead).
        prior_flag = flag.shift(1, fill_value=False).reindex(day, fill_value=False).to_numpy()
        prior_flag = pd.Series(prior_flag, index=f.index)
        up_lvl, dn_lvl = f["pd_high"] + p.buffer_atr * atr, f["pd_low"] - p.buffer_atr * atr
        cross_up = (c > up_lvl) & (c.shift(1) <= up_lvl.shift(1))
        cross_dn = (c < dn_lvl) & (c.shift(1) >= dn_lvl.shift(1))
        vol_ok = (f["rvol"] >= p.rvol_min) | f["rvol"].isna()
        long_raw = first_in_day((cross_up & prior_flag & vol_ok & ~f["new_day"]).fillna(False))
        short_raw = first_in_day((cross_dn & prior_flag & vol_ok & ~f["new_day"]).fillna(False))
        out = pd.DataFrame(index=f.index)
        out["long_entry"], out["short_entry"] = long_raw, short_raw
        if p.stop_mode == "pd_mid":
            mid = (f["pd_high"] + f["pd_low"]) / 2.0
            out["long_stop"] = np.minimum(mid, c - 0.5 * atr)
            out["short_stop"] = np.maximum(mid, c + 0.5 * atr)
        else:
            out["long_stop"] = c - p.stop_atr * atr
            out["short_stop"] = c + p.stop_atr * atr
        out["nr_prior"] = prior_flag
        out["pl_price_long"] = ((c - f["pd_high"]) / atr).clip(0, 1)
        out["pl_price_short"] = ((f["pd_low"] - c) / atr).clip(0, 1)
        out["pl_momentum_long"] = ((f["rsi"] - 50) / 20).clip(0, 1)
        out["pl_momentum_short"] = ((50 - f["rsi"]) / 20).clip(0, 1)
        out["pl_volatility_long"] = out["pl_volatility_short"] = vol_pillar(f["atr_pct"])
        out["pl_volume_long"] = out["pl_volume_short"] = volume_pillar(f["rvol"])
        out["long_score"] = np.nan
        out["short_score"] = np.nan
        return out

    def _reasons(self, f, i, side):
        row = f.iloc[i]
        lvl = row["pd_high"] if side == "long" else row["pd_low"]
        return [f"Yesterday was an NR{self.p.nr_n} session (narrowest range of the last {self.p.nr_n})",
                f"Price broke yesterday's {'high' if side == 'long' else 'low'} {lvl:.2f}"], []

    def implies(self, side: str) -> str:
        return ("Range contraction tends to be followed by expansion; a break of yesterday's high is the "
                "classic trigger. Fails fast when the market stays dull." if side == "long" else
                "Range contraction tends to be followed by expansion; a break of yesterday's low is the "
                "classic trigger. Fails fast when the market stays dull.")
