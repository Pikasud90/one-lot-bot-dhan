"""Intraday time-series momentum: the first half-hour predicts the last half-hour.

Evidence: Gao, Han, Li & Zhou (2018, J. Financial Economics) on the S&P 500 ETF,
and Li (2020, "Intraday Time Series Momentum: International Evidence") across
12 equity markets. The first 30 minutes' return (and, secondarily, the
return of the half-hour before the last) positively predicts the last
half-hour, more so on volatile and high-volume days. It is a late-session,
index-level effect, which is why it is offered for index futures/options
rather than single stocks.

Rule (closed bars only):
    at the bar that closes at `entry_time` (default 14:45), go with the sign
    of the opening return (09:15 open -> `open_window` close), optionally
    requiring the prior half-hour to agree; exit at the square-off.
    `min_open_move` in ATR filters flat opens, where the signal is weakest.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
from pydantic import Field

from ..indicators import core as ind
from .base import Strategy, StrategyParams, decision_times, opt, vol_pillar
from .regime import NEUTRAL, RANGE, TREND, VOLATILE


class IMParams(StrategyParams):
    entry_start: str = Field("14:30", description="Earliest decision time")
    entry_end: str = Field("15:00", description="Latest decision time")
    entry_time: str = Field("14:45", description="Decide at the bar that CLOSES at this time (HH:MM)")
    open_window_min: int = Field(30, description="Opening window in minutes", json_schema_extra=opt(15, 60, 15))
    require_prior_agree: bool = Field(False, description="Also require the half-hour before entry to agree")
    min_open_move_atr: float = Field(0.5, description="Opening move must exceed this many ATR(14)",
                                     json_schema_extra=opt(0.0, 1.5, 0.25))
    stop_atr: float = Field(1.0, description="Stop distance in ATR(14)", json_schema_extra=opt(0.5, 2.0, 0.25))
    target_r: float = Field(0.0, description="Target in R (0 = hold to square-off)",
                            json_schema_extra=opt(0.0, 3.0, 0.5))
    trail_atr_mult: float = Field(0.0, description="Trail (0 = off)")
    max_trades_per_day: int = Field(1, description="One per day by construction")
    regime_filter: bool = Field(False, description="Not used")
    market_filter: bool = Field(False, description="Not used (it IS the market)")
    min_risk_pct: float = Field(0.1, description="Friction floor")


class IntradayMomentum(Strategy):
    name = "intraday_momentum"
    label = "Intraday momentum (first 30m -> last 30m)"
    description = ("Trade the last half-hour in the direction of the first half-hour's move "
                   "(Gao-Han-Li-Zhou 2018; international evidence Li 2020). Index-level effect.")
    regimes = (TREND, NEUTRAL, RANGE, VOLATILE)
    Params = IMParams

    def raw_signals(self, f: pd.DataFrame) -> pd.DataFrame:
        p: IMParams = self.p  # type: ignore[assignment]
        c = f["close"]
        key = ind.session_key(f.index)
        tf_min = max(1, int(round((f.index[1] - f.index[0]).total_seconds() / 60))) if len(f) > 1 else 5
        n_open = max(1, p.open_window_min // tf_min)
        day_open = f["open"].groupby(key).transform("first")
        # close of the opening window, carried forward through the session
        open_close = c.where(f["bar_no"] == n_open - 1).groupby(key).transform("max")
        open_ret = (open_close - day_open)
        atr = f["atr"]
        t = decision_times(f.index, p.timeframe)
        h, m = p.entry_time.split(":")
        at_entry = pd.Series(t == dt.time(int(h), int(m)), index=f.index)
        strong = open_ret.abs() >= p.min_open_move_atr * atr
        if p.require_prior_agree:
            n_half = max(1, 30 // tf_min)
            prior = c - c.shift(n_half)
            agree_up, agree_dn = prior > 0, prior < 0
        else:
            agree_up = agree_dn = pd.Series(True, index=f.index)
        long_entry = at_entry & strong & (open_ret > 0) & agree_up
        short_entry = at_entry & strong & (open_ret < 0) & agree_dn
        out = pd.DataFrame(index=f.index)
        out["long_entry"], out["short_entry"] = long_entry, short_entry
        out["long_stop"] = c - p.stop_atr * atr
        out["short_stop"] = c + p.stop_atr * atr
        out["open_ret"] = open_ret
        size = (open_ret.abs() / atr).fillna(0)
        out["pl_price_long"] = out["pl_price_short"] = (size / 2.0).clip(0, 1)
        out["pl_momentum_long"] = ((f["rsi"] - 50) / 20).clip(0, 1)
        out["pl_momentum_short"] = ((50 - f["rsi"]) / 20).clip(0, 1)
        out["pl_volatility_long"] = out["pl_volatility_short"] = vol_pillar(f["atr_pct"])
        out["pl_volume_long"] = out["pl_volume_short"] = 0.7
        out["long_score"] = np.nan
        out["short_score"] = np.nan
        return out

    def _reasons(self, f, i, side):
        row = f.iloc[i]
        return ([f"Opening {self.p.open_window_min}-minute move {row['open_ret']:+.2f} points",
                 "Late-session continuation of the opening move (intraday time-series momentum)"], [])

    def implies(self, side: str) -> str:
        return ("The opening direction tends to reassert into the close; hold to the square-off."
                if side == "long" else
                "The opening sell-off tends to resume into the close; hold to the square-off.")
