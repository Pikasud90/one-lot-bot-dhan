"""S11 - CPR (Central Pivot Range) breakout.

Floor-trader pivots from the PRIOR session: P = (H+L+C)/3, BC = (H+L)/2, TC = 2P - BC.
A narrow CPR (TC-BC small relative to price) marks a session that is more likely to
trend. The first decisive close above the range top (long) / below the range bottom
(short), with ADX and relative-volume agreement, is the entry; the stop sits on the
other side of the range (ATR-floored); exits are R-based with an ATR trail.

Lineage: Mark Fisher, *The Logical Trader* (2002, "pivot range"); Frank Ochoa, *Secrets
of a Pivot Boss* (2010); the many public "CPR" Pine scripts on TradingView. Indian
retail's favourite intraday level set - which is exactly why it must be backtested
rather than believed.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
from pydantic import Field

from .base import Strategy, StrategyParams, first_in_day, opt, vol_pillar, volume_pillar
from .regime import NEUTRAL, TREND, VOLATILE


class CPRParams(StrategyParams):
    cpr_width_max_pct: float = Field(
        0.5,
        description="Trade only when (TC-BC)/P is at most this % (narrow CPR = trend day)",
        json_schema_extra=opt(0.2, 1.0, 0.1),
    )
    buffer_atr: float = Field(
        0.1,
        description="Close must clear the range edge by this many ATR(14)",
        json_schema_extra=opt(0.0, 0.3, 0.05),
    )
    adx_min: float = Field(15, description="Minimum ADX(14)", json_schema_extra=opt(10, 25, 5))
    rvol_min: float = Field(
        1.0,
        description="Minimum relative volume (ignored when the series has none)",
        json_schema_extra=opt(0.8, 1.5, 0.1),
    )
    stop_mode: Literal["cpr", "atr"] = Field(
        "cpr", description="Stop at the far side of the CPR, or close -/+ stop_atr x ATR"
    )
    stop_atr: float = Field(
        1.0,
        description="ATR stop distance when stop_mode = atr",
        json_schema_extra=opt(0.5, 2.0, 0.25),
    )
    one_per_day: bool = Field(True, description="Only the first breakout of the session per side")
    stop_atr_max: float = Field(
        4.0,
        description="Skip setups whose initial risk exceeds this many ATR(14)",
        json_schema_extra=opt(2.0, 6.0, 0.5),
    )
    target_r: float = Field(
        2.5, description="Profit target in R", json_schema_extra=opt(0.0, 4.0, 0.5)
    )
    trail_atr_mult: float = Field(
        2.0, description="Trailing stop k x ATR (0 = off)", json_schema_extra=opt(0.0, 3.0, 0.5)
    )
    max_trades_per_day: int = Field(
        2, description="Per symbol per day", json_schema_extra=opt(1, 3, 1)
    )


class CPRBreakout(Strategy):
    name = "cpr_breakout"
    label = "CPR Breakout"
    description = (
        "Narrow prior-day Central Pivot Range + decisive close beyond it with ADX and volume "
        "agreement; stop on the far side of the range."
    )
    regimes = (TREND, NEUTRAL, VOLATILE)
    Params = CPRParams

    def raw_signals(self, f: pd.DataFrame) -> pd.DataFrame:
        p: CPRParams = self.p  # type: ignore[assignment]
        c, atr = f["close"], f["atr"]
        h, l, pc = f["pd_high"], f["pd_low"], f["pd_close"]
        pivot = (h + l + pc) / 3.0
        bc = (h + l) / 2.0
        tc = 2.0 * pivot - bc
        top, bot = np.maximum(tc, bc), np.minimum(tc, bc)
        top, bot = pd.Series(top, index=f.index), pd.Series(bot, index=f.index)
        width_pct = (top - bot) / pivot * 100.0
        narrow = width_pct <= p.cpr_width_max_pct
        trend_ok = f["adx"] >= p.adx_min
        vol_ok = (f["rvol"] >= p.rvol_min) | f["rvol"].isna()
        up_edge, dn_edge = top + p.buffer_atr * atr, bot - p.buffer_atr * atr
        cross_up = (c > up_edge) & (c.shift(1) <= up_edge.shift(1))
        cross_dn = (c < dn_edge) & (c.shift(1) >= dn_edge.shift(1))
        long_raw = cross_up & narrow & trend_ok & vol_ok & ~f["new_day"]
        short_raw = cross_dn & narrow & trend_ok & vol_ok & ~f["new_day"]
        if p.one_per_day:
            long_raw, short_raw = (
                first_in_day(long_raw.fillna(False)),
                first_in_day(short_raw.fillna(False)),
            )
        out = pd.DataFrame(index=f.index)
        out["long_entry"], out["short_entry"] = long_raw.fillna(False), short_raw.fillna(False)
        if p.stop_mode == "cpr":
            out["long_stop"] = np.minimum(bot, c - 0.5 * atr)
            out["short_stop"] = np.maximum(top, c + 0.5 * atr)
        else:
            out["long_stop"] = c - p.stop_atr * atr
            out["short_stop"] = c + p.stop_atr * atr
        out["cpr_top"], out["cpr_bot"], out["cpr_width_pct"] = top, bot, width_pct
        out["pl_price_long"] = ((c - top) / atr).clip(0, 1)
        out["pl_price_short"] = ((bot - c) / atr).clip(0, 1)
        out["pl_momentum_long"] = out["pl_momentum_short"] = (f["adx"] / 40.0).clip(0, 1)
        out["pl_volatility_long"] = out["pl_volatility_short"] = vol_pillar(f["atr_pct"])
        out["pl_volume_long"] = out["pl_volume_short"] = volume_pillar(f["rvol"])
        out["long_score"] = np.nan
        out["short_score"] = np.nan
        return out

    def _reasons(self, f, i, side):
        row = f.iloc[i]
        edge = "top" if side == "long" else "bottom"
        return [
            f"Narrow CPR ({row['cpr_width_pct']:.2f}% of price) - trend-day setup",
            f"Close broke the CPR {edge} ({row['cpr_bot']:.2f}-{row['cpr_top']:.2f}) with ADX {row['adx']:.0f}",
        ], []

    def implies(self, side: str) -> str:
        return (
            "A narrow pivot range breaking upward usually means the session trends; "
            "the far side of the range is the invalidation."
            if side == "long"
            else "A narrow pivot range breaking downward usually means the session trends; "
            "the far side of the range is the invalidation."
        )
