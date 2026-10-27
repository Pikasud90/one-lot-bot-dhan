"""S2 - Opening Range Breakout (ORB) with VWAP, volume, gap and range-width filters.

The first N minutes define the opening range (OR). A bar that closes beyond the OR (first breakout
of the day only) in the direction of VWAP, on above-normal volume, is the trigger. Days with a huge
gap or an OR that is too narrow/too wide relative to daily ATR are skipped. Ref 7's breakout/gap
fill logic informs the stop handling.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
from pydantic import Field

from ..indicators import core as ind
from .base import (
    Strategy,
    StrategyParams,
    decision_times,
    first_in_day,
    opt,
    vol_pillar,
    volume_pillar,
)
from .regime import NEUTRAL, RANGE, TREND, VOLATILE


class ORBParams(StrategyParams):
    entry_start: str = Field("09:30", description="No new entries before (HH:MM)")
    or_minutes: int = Field(15, description="Opening range length in minutes", json_schema_extra=opt(15, 45, 15))
    min_or_atr: float = Field(0.15, description="Skip if OR width < this x daily ATR", json_schema_extra=opt(0.05, 0.4, 0.05))
    max_or_atr: float = Field(0.9, description="Skip if OR width > this x daily ATR", json_schema_extra=opt(0.5, 1.5, 0.1))
    max_gap_pct: float = Field(2.5, description="Skip days that gap more than this %", json_schema_extra=opt(1.0, 4.0, 0.5))
    rvol_min: float = Field(1.2, description="Minimum relative volume on the breakout bar", json_schema_extra=opt(0.8, 2.0, 0.2))
    last_entry: str = Field("11:30", description="No ORB entries after (HH:MM)")
    stop_mode: str = Field("mid", description="'mid' = OR midpoint, 'opposite' = other side of the OR, "
                           "'datr' = stop_datr x prior-day ATR from the entry")
    stop_datr: float = Field(0.1, description="Stop distance as a fraction of daily ATR (stop_mode='datr')",
                             json_schema_extra=opt(0.05, 0.3, 0.05))
    direction_mode: str = Field("breakout", description="'breakout' = trade the first OR break; "
                                "'first_candle' = trade the direction of the opening-range candle at its close")
    or_rvol_min: float = Field(0.0, description="'Stock in play' filter: OR volume vs its 10-day average (0 = off)",
                               json_schema_extra=opt(0.0, 3.0, 0.5))
    buffer_atr: float = Field(0.05, description="Stop buffer in ATR(14)")
    target_r: float = Field(1.5, description="Profit target in R multiples (0 = no fixed target)",
                            json_schema_extra=opt(0.0, 4.0, 0.5))


class OpeningRangeBreakout(Strategy):
    name = "orb"
    label = "Opening Range Breakout"
    description = "Trade the first decisive break of the opening range, with VWAP, volume, gap and range filters."
    regimes = (TREND, NEUTRAL, VOLATILE, RANGE)  # the OR itself defines the setup
    Params = ORBParams

    def raw_signals(self, f: pd.DataFrame) -> pd.DataFrame:
        p: ORBParams = self.p  # type: ignore[assignment]
        c = f["close"]
        key = ind.session_key(f.index)
        tf_min = max(1, int(round((f.index[1] - f.index[0]).total_seconds() / 60))) if len(f) > 1 else 5
        n_or = max(1, p.or_minutes // tf_min)
        in_or = f["bar_no"] < n_or
        or_high = f["high"].where(in_or).groupby(key).transform("max")
        or_low = f["low"].where(in_or).groupby(key).transform("min")
        or_width = or_high - or_low
        d_atr = f["d_atr"]
        width_ok = (or_width >= p.min_or_atr * d_atr) & (or_width <= p.max_or_atr * d_atr) | d_atr.isna()
        day_open = f["open"].groupby(key).transform("first")
        gap = (day_open / f["pd_close"] - 1).abs() * 100
        gap_ok = (gap <= p.max_gap_pct) | gap.isna()
        h, m = p.last_entry.split(":")
        before_last = decision_times(f.index, p.timeframe) <= dt.time(int(h), int(m))
        # "stock in play": opening-range volume vs its average over the previous 10 sessions
        or_vol_day = f["volume"].where(in_or).groupby(key).sum()
        or_rvol_day = or_vol_day / or_vol_day.shift(1).rolling(10, min_periods=3).mean()
        or_rvol = pd.Series(pd.Series(or_rvol_day).reindex(key).to_numpy(), index=f.index)
        in_play = (or_rvol >= p.or_rvol_min) | (p.or_rvol_min <= 0)
        ready = ~in_or & before_last & width_ok & gap_ok & in_play
        vol_ok = f["rvol"] >= p.rvol_min

        if p.direction_mode == "first_candle":
            or_open = f["open"].groupby(key).transform("first")
            at_or_close = f["bar_no"] == n_or - 1
            ok = at_or_close & width_ok & gap_ok & in_play
            long_entry = ok & (c > or_open)
            short_entry = ok & (c < or_open)
        else:
            long_raw = ready & (c > or_high) & (c > f["vwap"]) & vol_ok
            short_raw = ready & (c < or_low) & (c < f["vwap"]) & vol_ok
            # first breakout per day per side (a later re-break after a failure is not taken)
            brk_up = (c > or_high) & ready
            brk_dn = (c < or_low) & ready
            long_entry = first_in_day(brk_up) & long_raw
            short_entry = first_in_day(brk_dn) & short_raw

        mid = (or_high + or_low) / 2
        buf = p.buffer_atr * f["atr"]
        out = pd.DataFrame(index=f.index)
        out["long_entry"], out["short_entry"] = long_entry, short_entry
        if p.stop_mode == "datr":
            dist = (p.stop_datr * d_atr).fillna(2 * f["atr"])
            out["long_stop"], out["short_stop"] = c - dist, c + dist
        else:
            out["long_stop"] = (mid if p.stop_mode == "mid" else or_low) - buf
            out["short_stop"] = (mid if p.stop_mode == "mid" else or_high) + buf
        out["or_rvol"] = or_rvol
        out["or_high"], out["or_low"] = or_high, or_low
        # pillars
        brk_up_atr = ((c - or_high) / f["atr"]).clip(0, 2)
        brk_dn_atr = ((or_low - c) / f["atr"]).clip(0, 2)
        rel_w = (or_width / d_atr).fillna(0.4)
        out["pl_price_long"] = 0.6 * (1 - (brk_up_atr - 0.5).abs() / 1.5).clip(0, 1) + 0.4 * (1 - (rel_w - 0.4).abs() / 0.6).clip(0, 1)
        out["pl_price_short"] = 0.6 * (1 - (brk_dn_atr - 0.5).abs() / 1.5).clip(0, 1) + 0.4 * (1 - (rel_w - 0.4).abs() / 0.6).clip(0, 1)
        out["pl_momentum_long"] = ((f["rsi"] - 50) / 25).clip(0, 1)
        out["pl_momentum_short"] = ((50 - f["rsi"]) / 25).clip(0, 1)
        out["pl_volatility_long"] = out["pl_volatility_short"] = vol_pillar(f["atr_pct"])
        out["pl_volume_long"] = out["pl_volume_short"] = volume_pillar(f["rvol"])
        out["long_score"] = np.nan
        out["short_score"] = np.nan
        return out

    def _reasons(self, f, i, side):
        row = f.iloc[i]
        oh, ol = row.get("or_high"), row.get("or_low")
        if side == "long":
            return [f"Closed above the {self.p.or_minutes}-min opening range high {oh:.2f} (range {ol:.2f}-{oh:.2f})"], []
        return [f"Closed below the {self.p.or_minutes}-min opening range low {ol:.2f} (range {ol:.2f}-{oh:.2f})"], []

    def implies(self, side: str) -> str:
        return ("Opening balance resolved upward with volume; momentum often extends while the breakout level holds."
                if side == "long" else
                "Opening balance resolved downward with volume; selling often extends while the breakdown level holds.")
