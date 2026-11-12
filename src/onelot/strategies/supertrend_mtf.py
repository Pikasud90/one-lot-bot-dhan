"""Supertrend on several timeframes at once: the micro trend (the owner's rule, 2026-11-11).

The rule. One Supertrend setting, period 14 and multiplier 1.7, on six timeframes: 1, 5, 15,
30 and 60 minutes and the day. When the timeframes that are asked to agree all point the same
way the micro trend is clear, and a trade is taken in that direction. The Supertrend line of
one timeframe is the stop from the first minute and the trail afterwards: it follows the
market, only ever in the trade's favour, and the trade ends when the market crosses it.

How it reads the market, with no look-ahead. The strategy decides at the close of each of its
own bars (5-minute bars by default, so it looks every five minutes). At that moment it reads,
for every timeframe, the last bar that has CLOSED: a 30-minute bar still forming says nothing
yet, and today's daily bar is not used until tomorrow. See `indicators/mtf.py`.

Entry. The "entry timeframe" (`trend_tf`) gives the trend a trade belongs to. One entry per
trend of that timeframe (or one per trend per session with `reenter_daily`), at the first
decision inside the entry window where the gate agrees, the price is on the right side of the
trail line and the optional filters pass (how long ago the trend turned, and the ADX, RSI and
relative volume of the entry timeframe).

Stop and trail. `trail_tf` picks whose line is followed. With `stop_buffer_atr = 0` the line
itself is a resting stop: the first touch ends the trade. With a buffer the stop stands that
many ATRs beyond the line, and the trade ends at the next bar's open once a bar of the trail
timeframe has CLOSED beyond the line (the Supertrend has turned, which is how the indicator
itself changes colour); the buffered stop is then only the backstop for a fast move. The
2026-11-11 research found the close-based exit better than the touch on every structure
tested, and a backstop two ATRs away better than one closer (`mtf_lab_check.txt`), which is
why the default buffer is 2 and not 0. The stop in force only ever moves in the trade's
favour: the Lab and the paper book take a new level only when it is tighter than the last.

What the research said (evidence, not advice; `data/research/mtf_2026-11-11/NOTES.md`). On
five years of five index options and eighteen stocks the plain rule, every timeframe aligned
and held the same day, lost a little after costs. No setting has a verified positive result.
The form with the best same-day evidence adds a fresh 30-minute turn with strong ADX and RSI,
and is positive on modelled option premiums but not on the real premiums that could be checked.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd  # type: ignore[import-untyped]
from pydantic import Field

from ..indicators import core as ind
from ..indicators import mtf
from . import mtf_support as ms
from .base import Context, Strategy, StrategyParams, _hhmm, decision_times, opt
from .regime import NEUTRAL, RANGE, TREND, VOLATILE

_WORD = {1: "up", -1: "down", 0: "no reading"}


def _bars(n: int) -> str:
    return "1 bar" if n == 1 else f"{n} bars"


class STMtfParams(StrategyParams):
    timeframe: str = Field(
        "5m", description="How often it looks: decisions are made at the close of these bars"
    )
    st_period: int = Field(
        mtf.ST_PERIOD,
        description="Supertrend ATR period, the same on every timeframe",
        json_schema_extra=opt(7, 21, 1),
    )
    st_mult: float = Field(
        mtf.ST_MULT,
        description="Supertrend multiplier, the same on every timeframe",
        json_schema_extra=opt(1.0, 3.0, 0.1),
    )
    trend_tf: Literal["1m", "5m", "15m", "30m", "60m", "1d"] = Field(
        "30m",
        description="Entry timeframe: a trade belongs to one trend of its Supertrend "
        "(1m, 5m, 15m, 30m, 60m or 1d)",
    )
    gate: Literal["all", "slower", "faster", "daily", "trend"] = Field(
        "all",
        description="Which timeframes must agree: all = 1m, 5m, 15m, 30m, 60m and 1d; "
        "slower = the entry timeframe and every slower one; faster = it and every faster one; "
        "daily = it and the day; trend = it alone",
    )
    flip_window: int = Field(
        0,
        description="Enter only within this many entry-timeframe bars of the turn "
        "(0 = any time while the timeframes agree)",
        json_schema_extra=opt(0, 6, 1),
    )
    reenter_daily: bool = Field(
        False, description="Off: one entry per trend. On: one per trend in every session"
    )
    trend_adx_min: float = Field(
        0.0,
        description="Minimum ADX(14) of the entry timeframe (0 = off)",
        json_schema_extra=opt(0, 30, 5),
    )
    trend_rsi_min: float = Field(
        0.0,
        description="Longs need RSI(14) of the entry timeframe at or above this, shorts at or "
        "below 100 minus this (0 = off)",
        json_schema_extra=opt(0, 65, 5),
    )
    trend_rvol_min: float = Field(
        0.0,
        description="Minimum relative volume of the entry timeframe (0 = off; ignored when the "
        "series has no volume, as an index has none)",
        json_schema_extra=opt(0.0, 2.0, 0.5),
    )
    trail_tf: Literal["trend", "1m", "5m", "15m", "30m", "60m", "1d"] = Field(
        "trend",
        description="Whose Supertrend line is the stop and the trail: trend = the entry "
        "timeframe, or name one (60m, 1d ...)",
    )
    stop_buffer_atr: float = Field(
        2.0,
        description="How far beyond the line the stop stands, in ATR(14) of the trail timeframe. "
        "0 = the line itself is a resting stop (first touch exits). Above 0 = the trade exits "
        "when a bar of the trail timeframe closes beyond the line, and this stop is the backstop",
        json_schema_extra=opt(0.0, 3.0, 0.5),
    )
    exit_on_flip: bool = Field(
        True, description="Exit when the trail timeframe's Supertrend turns against the trade"
    )
    # the line decides the distance of the stop: the shared ATR and percentage limits are opened up
    stop_atr_min: float = Field(0.0, description="Minimum initial risk in ATR(14) units (0 = off)")
    stop_atr_max: float = Field(
        1000.0, description="Skip setups whose initial risk exceeds this many ATR(14) (off)"
    )
    min_risk_pct: float = Field(
        0.05, description="Friction floor: initial risk at least this % of price"
    )
    max_risk_pct: float = Field(
        5.0, description="Skip a setup whose line is further than this % of price away"
    )
    target_r: float = Field(
        0.0,
        description="Profit target in R multiples (0 = none: the line decides the exit)",
        json_schema_extra=opt(0.0, 4.0, 0.5),
    )
    trail_atr_mult: float = Field(
        0.0,
        description="Extra ATR trailing stop, k in k x ATR(n) (0 = off: the line is the trail)",
        json_schema_extra=opt(0.0, 3.0, 0.5),
    )
    cooldown_bars: int = Field(
        0, description="Bars to wait after an exit before re-entering the same symbol"
    )
    max_trades_per_day: int = Field(
        3, description="Per symbol per day", json_schema_extra=opt(1, 4, 1)
    )
    market_filter: bool = Field(
        False, description="Block longs when NIFTY is strongly bearish (and vice versa)"
    )
    regime_filter: bool = Field(
        False, description="Only trade in the regimes this strategy is designed for"
    )


class SupertrendMTF(Strategy):
    name = "supertrend_mtf"
    label = "Supertrend multi-timeframe (micro trend)"
    description = (
        "One Supertrend setting (14, 1.7) on 1m, 5m, 15m, 30m, 60m and 1d. A trade is taken "
        "when the chosen timeframes all point the same way; the Supertrend line of one of them "
        "is the stop and the trail."
    )
    regimes = (TREND, NEUTRAL, RANGE, VOLATILE)
    Params = STMtfParams
    multi_timeframe = True

    @property
    def trail_timeframe(self) -> str:
        p: STMtfParams = self.p  # type: ignore[assignment]
        return p.trend_tf if p.trail_tf == "trend" else p.trail_tf

    def features(self, df: pd.DataFrame, ctx: Context | None = None) -> pd.DataFrame:
        """The shared features plus the ladder as it stood at each bar's close.

        The ladder is read here and not in `_compute_features` because the shared feature cache
        is keyed by the data alone, and these columns also depend on this strategy's settings.
        """
        ctx = ctx or Context()
        f = super().features(df, ctx)
        p: STMtfParams = self.p  # type: ignore[assignment]
        ladder = ms.ladder_for(df, ctx, p.timeframe, p.st_period, p.st_mult)
        times = ms.decision_ns(pd.DatetimeIndex(f.index), p.timeframe)
        cols = ms.ladder_columns(ladder, times)
        cols.update(ms.trend_columns(ladder, p.trend_tf, times))
        cols.update(ms.trail_columns(ladder, self.trail_timeframe, times))
        return pd.concat([f, pd.DataFrame(cols, index=f.index)], axis=1)

    def raw_signals(self, f: pd.DataFrame) -> pd.DataFrame:
        p: STMtfParams = self.p  # type: ignore[assignment]
        n = len(f)
        dirs = {tf: f[f"mtf_{tf}"].to_numpy() for tf in mtf.LADDER_TIMEFRAMES}
        aligned = mtf.alignment(dirs, ms.gate_timeframes(p.gate, p.trend_tf))
        trail_dir = f["mtf_trail_dir"].to_numpy()
        line = f["mtf_trail_line"].to_numpy(float)
        close = f["close"].to_numpy(float)
        buffer = p.stop_buffer_atr * f["mtf_trail_atr"].to_numpy(float) if p.stop_buffer_atr else 0
        adx, rsi, rv = (f[k].to_numpy(float) for k in ("mtf_adx", "mtf_rsi", "mtf_rvol"))
        # the first allowed decision of each trend is taken, so the window is applied before picking
        t = decision_times(pd.DatetimeIndex(f.index), p.timeframe)
        allowed = (t >= _hhmm(p.entry_start)) & (t <= _hhmm(p.entry_end))
        if p.flip_window > 0:
            allowed &= f["mtf_since"].to_numpy() < p.flip_window
        with np.errstate(invalid="ignore"):
            if p.trend_adx_min > 0:
                allowed &= adx >= p.trend_adx_min
            if p.trend_rvol_min > 0:
                allowed &= (rv >= p.trend_rvol_min) | np.isnan(rv)
            rsi_ok = {
                1: (rsi >= p.trend_rsi_min) if p.trend_rsi_min > 0 else np.ones(n, dtype=bool),
                -1: (rsi <= 100 - p.trend_rsi_min) if p.trend_rsi_min > 0 else np.ones(n, bool),
            }
        groups = [f["mtf_seg"].to_numpy()]
        if p.reenter_daily:
            groups.append(np.asarray(ind.session_key(f.index)))
        out = pd.DataFrame(index=f.index)
        for sign, side in ((1, "long"), (-1, "short")):
            stop = np.where(trail_dir == sign, line - sign * buffer, np.nan)
            with np.errstate(invalid="ignore"):
                ok = (
                    (aligned == sign)
                    & (sign * (close - line) > 0)
                    & np.isfinite(stop)
                    & allowed
                    & rsi_ok[sign]
                )
            first = pd.Series(ok.astype(int), index=f.index).groupby(groups).cumsum() == 1
            out[f"{side}_entry"] = ok & first.to_numpy()
            out[f"{side}_stop"] = stop
            out[f"{side}_trail"] = stop  # the level the stop may stand at after this close
            out[f"{side}_exit"] = (trail_dir == -sign) if p.exit_on_flip else False
            # No strength of its own: under the "all" gate every entry would score 100 and the
            # desk would call an untested setting "high confidence". NaN leaves the neutral 50.
            out[f"{side}_score"] = np.nan
        return out

    def _reasons(self, f: pd.DataFrame, i: int, side: str) -> tuple[list[str], list[str]]:
        p: STMtfParams = self.p  # type: ignore[assignment]
        row = f.iloc[i]
        sign = 1 if side == "long" else -1
        way = _WORD[sign]
        state = {tf: int(row[f"mtf_{tf}"]) for tf in mtf.LADDER_TIMEFRAMES}
        gate = ms.gate_timeframes(p.gate, p.trend_tf)
        reasons = [
            f"Supertrend({p.st_period}, {p.st_mult:g}): "
            + ", ".join(f"{tf} {_WORD[state[tf]]}" for tf in mtf.LADDER_TIMEFRAMES)
        ]
        blockers: list[str] = []
        against = [tf for tf in gate if state[tf] != sign]
        if against:
            blockers.append(
                f"Not all {way} yet: " + ", ".join(f"{tf} {_WORD[state[tf]]}" for tf in against)
            )
            if any(state[tf] == 0 for tf in against):
                blockers.append(
                    "No reading means too few bars so far, or a feed that cannot give that "
                    "timeframe (a delayed 2-minute feed has no 1m, 5m or 15m bars)"
                )
        elif len(gate) > 1:
            reasons.append(f"All {len(gate)} timeframes that must agree point {way}")
        since = int(row["mtf_since"])
        if state[p.trend_tf] == sign and since < ms.NO_FLIP:
            # 0 = the last closed bar of the entry timeframe is the one the trend turned on
            when = "on its last closed bar" if since == 0 else f"{_bars(since)} of {p.trend_tf} ago"
            reasons.append(f"The {p.trend_tf} trend turned {way} {when}")
            if 0 < p.flip_window <= since:
                blockers.append(
                    f"The turn is not within the last {_bars(p.flip_window)} of {p.trend_tf}"
                )
        rsi_limit = p.trend_rsi_min if sign == 1 else 100 - p.trend_rsi_min
        # name, reading, filter on, the reading passes, what the filter asks for
        checks = (
            (
                "ADX",
                row["mtf_adx"],
                p.trend_adx_min > 0,
                lambda v: v >= p.trend_adx_min,
                "at least",
            ),
            (
                "RSI",
                row["mtf_rsi"],
                p.trend_rsi_min > 0,
                (lambda v: v >= rsi_limit) if sign == 1 else (lambda v: v <= rsi_limit),
                "at least" if sign == 1 else "at most",
            ),
            (
                "relative volume",
                row["mtf_rvol"],
                p.trend_rvol_min > 0,
                lambda v: v >= p.trend_rvol_min,
                "at least",
            ),
        )
        limits = {"ADX": p.trend_adx_min, "RSI": rsi_limit, "relative volume": p.trend_rvol_min}
        readings = [f"{name} {value:.1f}" for name, value, *_ in checks if not np.isnan(value)]
        if readings:
            reasons.append(f"{p.trend_tf} " + ", ".join(readings))
        for name, value, on, passes, word in checks:
            if on and not np.isnan(value) and not passes(value):
                blockers.append(
                    f"{p.trend_tf} {name} {value:.1f} does not pass the filter "
                    f"(needs {word} {limits[name]:g})"
                )
        trail_tf = self.trail_timeframe
        level = row["mtf_trail_line"]
        if int(row["mtf_trail_dir"]) == sign and not np.isnan(level):
            if p.stop_buffer_atr > 0:
                reasons.append(
                    f"Stop and trail: the {trail_tf} Supertrend line {level:.2f}. A {trail_tf} "
                    f"close beyond it ends the trade; the stop stands {p.stop_buffer_atr:g} ATR "
                    f"beyond it at {row[f'{side}_stop']:.2f}"
                )
            else:
                reasons.append(
                    f"Stop and trail: the {trail_tf} Supertrend line {level:.2f} (a touch exits)"
                )
            if (row["close"] - level) * sign <= 0:
                blockers.append(f"The price is already beyond the {trail_tf} line")
        else:
            blockers.append(f"The {trail_tf} Supertrend line is not on the {way} side")
        return reasons, blockers

    def implies(self, side: str) -> str:
        way = "up" if side == "long" else "down"
        return (
            f"The timeframes agree that the micro trend is {way}. The Supertrend line of the "
            "trail timeframe is the stop: it follows the market, never back, and the trade ends "
            "when the market crosses it."
        )
