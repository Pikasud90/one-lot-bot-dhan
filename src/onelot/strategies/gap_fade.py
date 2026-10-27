"""S6 - Relative Gap Fade (found by the research process, validated on a 2022-26 holdout).

Evidence (15 large caps, daily bars 2015-2026; see docs/research/RESEARCH_REPORT.md):
stocks that open >= 0.75% above NIFTY's own opening gap drift DOWN from open to close
(+0.28-0.32% short P&L per trade before costs, t ~ 4.5 in both the 2015-21 exploration period
and the untouched 2022-26 holdout, positive in 11 of 12 years). Roughly 70% of that drift happens
after the first continuous trade, i.e. it is capturable from an alert at the first bar close.
This is consistent with the well-documented Indian-equity pattern of positive overnight and weak
intraday returns (overnight optimism partially reverses during the session).

Trade: at the close of the first bar (09:20 on 5m bars) short the stock if its relative gap is large
(optionally only if the first bar is red / below VWAP - i.e. the gap is already failing); stop a
fraction of daily ATR above; hold to the intraday square-off. The mirror long side (buy relative
gap-downs) worked before 2022 but NOT in the holdout, so it is disabled by default.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pydantic import Field

from ..indicators import core as ind
from .base import Strategy, StrategyParams, opt, vol_pillar, volume_pillar


class GapFadeParams(StrategyParams):
    allow_long: bool = Field(False, description="Also buy relative gap-DOWNS (failed the 2022-26 holdout; off by default)")
    allow_short: bool = Field(True, description="Short relative gap-ups (the validated side)")
    entry_start: str = Field("09:15", description="No new entries before (HH:MM)")
    entry_end: str = Field("09:45", description="Only act on the opening bars")
    min_rel_gap: float = Field(0.75, description="Stock gap minus NIFTY gap, in % (>= for shorts, <= -x for longs)",
                               json_schema_extra=opt(0.5, 1.5, 0.25))
    min_gap: float = Field(0.3, description="Stock's own gap must also be at least this % in the trade's direction",
                           json_schema_extra=opt(0.0, 1.0, 0.25))
    max_gap: float = Field(4.0, description="Skip extreme gaps (news/results days) above this %")
    confirm: str = Field("none", description="'none' | 'red_bar' (first bar closes against the gap) | 'below_vwap'")
    entry_bar: int = Field(0, description="Decide at the close of this bar of the day (0 = first bar)",
                           json_schema_extra=opt(0, 2, 1))
    stop_datr: float = Field(0.75, description="Stop distance as a fraction of the prior day's ATR",
                             json_schema_extra=opt(0.5, 1.5, 0.25))
    target_r: float = Field(0.0, description="Profit target in R (0 = hold to square-off; the drift runs into the close)",
                            json_schema_extra=opt(0.0, 3.0, 0.5))
    trail_atr_mult: float = Field(0.0, description="Trailing stop distance k (0 = off)", json_schema_extra=opt(0.0, 3.0, 0.5))
    min_risk_pct: float = Field(0.5, description="Friction floor: initial risk at least this % of price")
    max_risk_pct: float = Field(3.0, description="Skip setups whose initial risk exceeds this % of price")
    stop_atr_max: float = Field(50.0, description="Intraday-ATR cap disabled (stop is daily-ATR based)")
    market_filter: bool = Field(False, description="Not used: the signal is already relative to NIFTY")
    regime_filter: bool = Field(False, description="Not used")
    max_trades_per_day: int = Field(1, description="Per symbol per day")


class GapFade(Strategy):
    name = "gap_fade"
    label = "Relative Gap Fade"
    description = ("Short stocks that open well above NIFTY's own gap and hold to the close - the intraday "
                   "reversal of overnight optimism (validated on 2015-21 and an untouched 2022-26 holdout).")
    Params = GapFadeParams

    def raw_signals(self, f: pd.DataFrame) -> pd.DataFrame:
        p: GapFadeParams = self.p  # type: ignore[assignment]
        c, o = f["close"], f["open"]
        key = ind.session_key(f.index)
        day_open = o.groupby(key).transform("first")
        gap = (day_open / f["pd_close"] - 1) * 100
        mgap = f["mkt_gap"] if "mkt_gap" in f.columns else pd.Series(0.0, index=f.index)
        rel = gap - mgap.fillna(0.0)
        at_bar = f["bar_no"] == p.entry_bar
        sane = gap.abs() <= p.max_gap
        if p.confirm == "red_bar":
            conf_s, conf_l = c < day_open, c > day_open
        elif p.confirm == "below_vwap":
            conf_s, conf_l = c < f["vwap"], c > f["vwap"]
        else:
            conf_s = conf_l = pd.Series(True, index=f.index)
        short_entry = at_bar & sane & (rel >= p.min_rel_gap) & (gap >= p.min_gap) & conf_s
        long_entry = at_bar & sane & (rel <= -p.min_rel_gap) & (gap <= -p.min_gap) & conf_l
        dist = (p.stop_datr * f["d_atr"]).fillna(3 * f["atr"])
        out = pd.DataFrame(index=f.index)
        out["long_entry"], out["short_entry"] = long_entry, short_entry
        out["long_stop"], out["short_stop"] = c - dist, c + dist
        out["gap_pct"], out["rel_gap_pct"] = gap, rel
        size = (rel.abs() - p.min_rel_gap) / 1.0
        out["pl_price_short"] = (0.5 + size).clip(0, 1)
        out["pl_price_long"] = (0.5 + size).clip(0, 1)
        out["pl_momentum_short"] = (0.5 + 0.5 * (c < day_open)).astype(float)
        out["pl_momentum_long"] = (0.5 + 0.5 * (c > day_open)).astype(float)
        out["pl_volatility_short"] = out["pl_volatility_long"] = vol_pillar(f["atr_pct"])
        out["pl_volume_short"] = out["pl_volume_long"] = volume_pillar(f["rvol"])
        out["long_score"] = np.nan
        out["short_score"] = np.nan
        return out

    def _reasons(self, f, i, side):
        row = f.iloc[i]
        g, r = row.get("gap_pct", np.nan), row.get("rel_gap_pct", np.nan)
        word = "above" if side == "short" else "below"
        return ([f"Opened {g:+.2f}% vs yesterday's close; {r:+.2f}% {word} NIFTY's own gap",
                 "Large relative gaps in these large caps have tended to fade into the close (2015-2026 study)"], [])

    def implies(self, side: str) -> str:
        return ("Overnight enthusiasm has priced the stock well above the market; the session historically gives part "
                "of it back. Hold to the square-off unless the stop is hit." if side == "short" else
                "Overnight pessimism has priced the stock well below the market; historically part of it recovers "
                "(weaker evidence since 2022).")
