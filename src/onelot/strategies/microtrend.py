"""The owner's options rule (US-068): the micro trend of seven timeframes, read on 3-minute bars.

"We check the micro trend of 5, 10, 15, 30, 60 minutes and 4 hours, and then the 3-minute
candles generate the alert when all the micro trends align on the call side or the put side.
The exit is as soon as the micro trend reverses in at least 3 of the timeframes."

- Trend of a timeframe: the direction of Supertrend(14, 1.7) on its CLOSED bars, built from the
  one-minute bars (`indicators/mtf.py`). The ladder is 3m (the trigger), 5m, 10m, 15m, 30m,
  60m and 4h; a 4h bar runs 09:15 to 13:15 and 13:15 to the close. A timeframe with too few
  bars has no reading (`mtf.WARMUP_FACTOR` x 14 of its bars), and then nothing fires.
- Entry: at a 3-minute close where all seven point the same way and did not at the previous
  3-minute close (the alignment is new). Up: a call; down: a put (one lot, the desk's contract).
- Exit (`long_exit` / `short_exit`): at the first 3-minute close where at least `exit_count`
  (3) of the seven point against the trade. The desk hands it to the paper book as the
  strategy's exit, filled at the first fine bar at or after that close; otherwise the session
  end. No target.
- Stop: the paper book needs a level, so the alert carries the far side of the 60-minute
  Supertrend line (the line minus `stop_buffer_atr` x ATR(14) of the 60-minute bars for a call,
  plus for a put). It is a backstop for a crash, not the rule's exit.

Evidence, not advice: a backtest of this exact rule over five years on five indices lost a
little on modelled premiums (see the slot's `evidence` in `config/desk.yaml`).
"""

from __future__ import annotations

import numpy as np
import pandas as pd  # type: ignore[import-untyped]
from pydantic import Field

from ..indicators import mtf
from . import mtf_support as ms
from .base import Context, Strategy, StrategyParams
from .regime import NEUTRAL, RANGE, TREND, VOLATILE

#: the owner's micro trend ladder, the trigger first ("240m" is the 4-hour bar)
MICRO_TIMEFRAMES: tuple[str, ...] = ("3m", "5m", "10m", "15m", "30m", "60m", "240m")
_WORD = {1: "up", -1: "down", 0: "no reading"}


def label(tf: str) -> str:
    """How a timeframe is shown: 240m is 4h."""
    return "4h" if tf == "240m" else tf


class MicrotrendParams(StrategyParams):
    timeframe: str = Field("3m", description="The trigger: decisions at the close of 3m bars")
    st_period: int = Field(mtf.ST_PERIOD, description="Supertrend ATR period, every timeframe")
    st_mult: float = Field(mtf.ST_MULT, description="Supertrend multiplier, every timeframe")
    exit_count: int = Field(
        3, ge=1, le=7, description="Exit when this many of the 7 trends point against the trade"
    )
    stop_buffer_atr: float = Field(
        3.0,
        ge=0.0,
        description="The backstop: this many ATR(14) of the 60m bars beyond the 60m line",
    )
    # the line decides the stop's distance: the shared ATR and percentage limits are opened up
    stop_atr_min: float = Field(0.0, description="Minimum initial risk in ATR(14) units (off)")
    stop_atr_max: float = Field(1000.0, description="Maximum initial risk in ATR(14) (off)")
    min_risk_pct: float = Field(0.05, description="Friction floor: risk at least this % of price")
    max_risk_pct: float = Field(10.0, description="Skip a setup whose stop is further than this %")
    target_r: float = Field(0.0, description="No target: the reversal count exits")
    trail_atr_mult: float = Field(0.0, description="No ATR trail")
    regime_filter: bool = Field(False, description="Off: the rule reads no regime")
    min_strength: float = Field(0.0, description="Off: the rule has no strength score")


class Microtrend(Strategy):
    name = "microtrend"
    label = "Micro trend (owner's options rule)"
    description = (
        "Supertrend(14, 1.7) on 3m, 5m, 10m, 15m, 30m, 60m and 4h: a call or a put when all "
        "seven newly point the same way at a 3-minute close; out when 3 of the 7 turn against."
    )
    regimes = (TREND, NEUTRAL, RANGE, VOLATILE)
    Params = MicrotrendParams
    multi_timeframe = True
    ladder_timeframes = MICRO_TIMEFRAMES

    def features(self, df: pd.DataFrame, ctx: Context | None = None) -> pd.DataFrame:
        ctx = ctx or Context()
        f = super().features(df, ctx)
        p: MicrotrendParams = self.p  # type: ignore[assignment]
        ladder = ms.ladder_for(df, ctx, p.timeframe, p.st_period, p.st_mult, MICRO_TIMEFRAMES)
        times = ms.decision_ns(pd.DatetimeIndex(f.index), p.timeframe)
        cols = ms.ladder_columns(ladder, times)
        cols.update(ms.trail_columns(ladder, "60m", times))
        return pd.concat([f, pd.DataFrame(cols, index=f.index)], axis=1)

    def raw_signals(self, f: pd.DataFrame) -> pd.DataFrame:
        p: MicrotrendParams = self.p  # type: ignore[assignment]
        dirs = {tf: f[f"mtf_{tf}"].to_numpy() for tf in MICRO_TIMEFRAMES}
        aligned = mtf.alignment(dirs, MICRO_TIMEFRAMES)
        before = np.r_[np.int8(0), aligned[:-1]]  # the previous 3-minute close
        up, down = f["mtf_up"].to_numpy(), f["mtf_down"].to_numpy()
        line = f["mtf_trail_line"].to_numpy(float)
        buffer = p.stop_buffer_atr * f["mtf_trail_atr"].to_numpy(float)
        out = pd.DataFrame(index=f.index)
        for sign, side, against in ((1, "long", down), (-1, "short", up)):
            with np.errstate(invalid="ignore"):
                stop = line - sign * buffer
            out[f"{side}_entry"] = (aligned == sign) & (before != sign) & np.isfinite(stop)
            out[f"{side}_stop"] = stop
            out[f"{side}_exit"] = against >= p.exit_count
            out[f"{side}_score"] = np.nan  # no strength of its own: the neutral 50
        return out

    def _reasons(self, f: pd.DataFrame, i: int, side: str) -> tuple[list[str], list[str]]:
        p: MicrotrendParams = self.p  # type: ignore[assignment]
        row = f.iloc[i]
        sign = 1 if side == "long" else -1
        state = {tf: int(row[f"mtf_{tf}"]) for tf in MICRO_TIMEFRAMES}
        reasons = [
            f"Supertrend({p.st_period}, {p.st_mult:g}): "
            + ", ".join(f"{label(tf)} {_WORD[state[tf]]}" for tf in MICRO_TIMEFRAMES)
        ]
        blockers: list[str] = []
        against = [tf for tf in MICRO_TIMEFRAMES if state[tf] != sign]
        if against:
            blockers.append(
                f"Not all {_WORD[sign]}: "
                + ", ".join(f"{label(tf)} {_WORD[state[tf]]}" for tf in against)
            )
        else:
            reasons.append(
                f"All 7 timeframes newly {_WORD[sign]}: "
                f"{'BUY a call' if sign == 1 else 'BUY a put'}; out when "
                f"{p.exit_count} of 7 turn against"
            )
        return reasons, blockers
