"""Run a Backtest Lab strategy on the live index bars to supply the DIRECTION.

`strategy.live.source: lab` swaps the built-in trend/momentum gates for a
registry strategy (ema_cross, supertrend_vol, ...). Exactly the same code
path the Lab backtested runs here, on the same closed bars, resampled to the
strategy's timeframe. Everything downstream -- contract selection, net R/R,
risk governor, Guardian -- is untouched, so the Lab strategy can only ever
say "which side, and where is the structural stop".
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..clock import IST
from ..config import LiveAlphaCfg
from ..data.resample import minutes_of, resample_bars
from ..strategies.registry import get_strategy

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LabVerdict:
    ce_fired: bool
    pe_fired: bool
    ce_stop_points: float | None
    pe_stop_points: float | None
    bar_ts: dt.datetime | None
    strategy: str
    timeframe: str
    reason: str = ""

    def fired(self, direction: str) -> bool:
        return self.ce_fired if direction == "CE" else self.pe_fired

    def stop_points(self, direction: str) -> float | None:
        return self.ce_stop_points if direction == "CE" else self.pe_stop_points

    def as_dict(self) -> dict:
        return {"strategy": self.strategy, "timeframe": self.timeframe, "ce": self.ce_fired,
                "pe": self.pe_fired, "ce_stop_points": self.ce_stop_points,
                "pe_stop_points": self.pe_stop_points,
                "bar_ts": self.bar_ts.isoformat() if self.bar_ts else None, "reason": self.reason}


class LabDirectionSource:
    def __init__(self, cfg: LiveAlphaCfg, bar_interval_min: int) -> None:
        self.cfg = cfg
        self.bar_interval_min = bar_interval_min
        self.tf_min = minutes_of(cfg.lab_timeframe)
        if self.tf_min % bar_interval_min != 0:
            raise ValueError(
                f"strategy.live.lab_timeframe={cfg.lab_timeframe} is not a multiple of "
                f"data.bar_interval_min={bar_interval_min}; set bar_interval_min to 1 for odd frames")
        self.strategy = get_strategy(cfg.lab_strategy, {**cfg.lab_params, "timeframe": cfg.lab_timeframe})
        self.name = cfg.lab_strategy
        self.timeframe = cfg.lab_timeframe

    def evaluate(self, ts: np.ndarray, o: np.ndarray, h: np.ndarray, l: np.ndarray,  # noqa: E741
                 c: np.ndarray) -> LabVerdict:
        n = len(c)
        if n < 60:
            return LabVerdict(False, False, None, None, None, self.name, self.timeframe,
                              f"only {n} bars; need 60")
        idx = pd.DatetimeIndex([pd.Timestamp(t).tz_convert(IST) if pd.Timestamp(t).tzinfo
                                else pd.Timestamp(t).tz_localize(IST) for t in ts])
        df = pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": 0.0}, index=idx)
        latest_close_at = idx[-1] + pd.Timedelta(minutes=self.bar_interval_min)
        if self.tf_min != self.bar_interval_min:
            df = resample_bars(df, self.cfg.lab_timeframe)
            # Only act on a COMPLETE resampled bar: its close must not be in the future.
            if df.index[-1] + pd.Timedelta(minutes=self.tf_min) > latest_close_at:
                df = df.iloc[:-1]
        if len(df) < 60:
            return LabVerdict(False, False, None, None, None, self.name, self.timeframe,
                              f"only {len(df)} {self.timeframe} bars; need 60")
        try:
            f = self.strategy.generate(df)
        except Exception as exc:  # noqa: BLE001
            log.exception("lab strategy %s failed on live bars", self.name)
            return LabVerdict(False, False, None, None, None, self.name, self.timeframe,
                              f"strategy error: {type(exc).__name__}")
        last = f.iloc[-1]
        bar_ts = f.index[-1].to_pydatetime()
        # The bar that just closed must BE the signal bar; an older cross is stale.
        fresh = (f.index[-1] + pd.Timedelta(minutes=self.tf_min)) == latest_close_at
        ce = bool(last.get("long_entry")) and fresh
        pe = bool(last.get("short_entry")) and fresh
        close = float(last["close"])
        ce_sp = float(close - last["long_stop"]) if ce and not np.isnan(last.get("long_stop", np.nan)) else None
        pe_sp = float(last["short_stop"] - close) if pe and not np.isnan(last.get("short_stop", np.nan)) else None
        reason = "" if (ce or pe) else ("bar not fresh" if not fresh else "no entry on the last bar")
        return LabVerdict(ce, pe, ce_sp if ce_sp and ce_sp > 0 else None,
                          pe_sp if pe_sp and pe_sp > 0 else None, bar_ts, self.name, self.timeframe, reason)
