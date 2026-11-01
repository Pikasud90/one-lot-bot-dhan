"""Candle construction with strict closed-bar semantics.

*** INVARIANT I2 / v1 defect C4 ***
v1 said the engine "evaluates every 5 minutes" without ever saying WHEN. If
the strategy reads the in-progress bar, every signal repaints: the bar it fired
on keeps changing after the fact, so the backtest measures a bar that never
existed at decision time. That single ambiguity invalidates any performance
number a system produces.

Here a bar is `closed` only once wall-clock time has passed its close boundary
plus a settle delay for late prints, and `closed_bars()` is the ONLY accessor
the strategy is allowed to use.

*** v1 defect B4 ***
EMA(21), RSI(14), ATR and ADX(14) need 20-30 bars. A session starts with zero.
`seed_from_history()` backfills prior sessions so indicators are live at 09:15
instead of producing NaN (or worse, garbage) until ~10:30.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

import numpy as np

from ..broker.base import Bar
from ..clock import bar_close_time, bar_open_time, to_ist

log = logging.getLogger(__name__)


@dataclass
class WorkingBar:
    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    tick_count: int = 0

    def update(self, price: float, volume_delta: float | None) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        if volume_delta is not None:
            self.volume = (self.volume or 0.0) + volume_delta
        self.tick_count += 1

    def freeze(self) -> Bar:
        return Bar(
            ts=self.ts,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )


class CandleSeries:
    """A single instrument's bars at one interval.

    `has_volume` is explicit rather than inferred: an index legitimately has no
    volume, and storing 0 for it is what let v1 compute VWAP on 0/0 (defect B3).
    """

    def __init__(
        self,
        instrument_key: str,
        interval_min: int,
        session_open: dt.datetime,
        has_volume: bool = True,
        max_bars: int = 2000,
    ):
        self.instrument_key = instrument_key
        self.interval_min = interval_min
        self.session_open = session_open
        self.has_volume = has_volume
        self.max_bars = max_bars
        self._closed: list[Bar] = []
        self._working: WorkingBar | None = None
        self._last_cum_volume: float | None = None
        self._seeded = 0

    # ── construction ──────────────────────────────────────────────────────
    def seed_from_history(self, bars: list[Bar]) -> int:
        """Backfill closed bars from historical data (warm-up)."""
        added = 0
        for b in sorted(bars, key=lambda x: x.ts):
            ts = to_ist(b.ts)
            if self._closed and ts <= self._closed[-1].ts:
                continue
            vol = b.volume if self.has_volume else None
            self._closed.append(Bar(ts, b.open, b.high, b.low, b.close, vol))
            added += 1
        self._trim()
        self._seeded += added
        return added

    def on_tick(
        self, ts: dt.datetime, price: float, cumulative_volume: float | None = None
    ) -> list[Bar]:
        """Feed a tick. Returns bars that closed as a result of this tick."""
        ts = to_ist(ts)
        if price is None or not np.isfinite(price) or price <= 0:
            return []

        vol_delta = None
        if self.has_volume and cumulative_volume is not None:
            if self._last_cum_volume is not None and cumulative_volume >= self._last_cum_volume:
                vol_delta = cumulative_volume - self._last_cum_volume
            self._last_cum_volume = cumulative_volume

        b_open = bar_open_time(ts, self.interval_min, self.session_open)
        closed_now: list[Bar] = []

        if self._working is None:
            self._working = WorkingBar(
                b_open, price, price, price, price, 0.0 if self.has_volume else None
            )
        elif b_open > self._working.ts:
            closed_now.append(self._close_working())
            self._working = WorkingBar(
                b_open, price, price, price, price, 0.0 if self.has_volume else None
            )

        self._working.update(price, vol_delta)
        return closed_now

    def mark_closed_through(self, now: dt.datetime, settle_delay_sec: float = 0.0) -> list[Bar]:
        """Close the working bar on TIME rather than on tick arrival.

        Necessary because a bar must close even if no trade prints in the next
        interval — otherwise a quiet period silently stalls the strategy.
        """
        if self._working is None:
            return []
        now = to_ist(now)
        close_at = bar_close_time(self._working.ts, self.interval_min)
        if now >= close_at + dt.timedelta(seconds=settle_delay_sec):
            return [self._close_working()]
        return []

    def _close_working(self) -> Bar:
        assert self._working is not None
        bar = self._working.freeze()
        self._working = None
        if not self._closed or bar.ts > self._closed[-1].ts:
            self._closed.append(bar)
            self._trim()
        return bar

    def _trim(self) -> None:
        if len(self._closed) > self.max_bars:
            self._closed = self._closed[-self.max_bars :]

    # ── access (strategy may ONLY use closed bars) ────────────────────────
    @property
    def closed_bars(self) -> list[Bar]:
        return list(self._closed)

    @property
    def count(self) -> int:
        return len(self._closed)

    @property
    def forming_bar(self) -> Bar | None:
        """Display only. Passing this to the strategy is the repainting bug."""
        return self._working.freeze() if self._working else None

    def latest_closed(self) -> Bar | None:
        return self._closed[-1] if self._closed else None

    def arrays(self, limit: int | None = None) -> dict[str, np.ndarray]:
        bars = self._closed[-limit:] if limit else self._closed
        vol = (
            np.array([b.volume if b.volume is not None else np.nan for b in bars])
            if self.has_volume
            else np.full(len(bars), np.nan)
        )
        return {
            "ts": np.array([b.ts for b in bars], dtype=object),
            "open": np.array([b.open for b in bars], dtype=float),
            "high": np.array([b.high for b in bars], dtype=float),
            "low": np.array([b.low for b in bars], dtype=float),
            "close": np.array([b.close for b in bars], dtype=float),
            "volume": vol,
        }

    def session_arrays(self, session_date: dt.date) -> dict[str, np.ndarray]:
        """Only today's bars — VWAP is anchored to the session open, not to
        whatever happens to be in the buffer."""
        bars = [b for b in self._closed if b.ts.date() == session_date]
        vol = (
            np.array([b.volume if b.volume is not None else np.nan for b in bars])
            if self.has_volume
            else np.full(len(bars), np.nan)
        )
        return {
            "high": np.array([b.high for b in bars], dtype=float),
            "low": np.array([b.low for b in bars], dtype=float),
            "close": np.array([b.close for b in bars], dtype=float),
            "volume": vol,
        }

    def warmup_status(self, required_bars: int) -> tuple[bool, str]:
        n = self.count
        if n >= required_bars:
            return True, f"{n} closed bars ({self._seeded} seeded)"
        return False, (
            f"warm-up incomplete: {n}/{required_bars} closed bars — "
            f"signals blocked until indicators are defined"
        )


@dataclass
class CandleBook:
    """All series the engine tracks, keyed by instrument."""

    session_open: dt.datetime
    interval_min: int = 5
    series: dict[str, CandleSeries] = field(default_factory=dict)

    def ensure(self, key: str, has_volume: bool, interval_min: int | None = None) -> CandleSeries:
        iv = interval_min or self.interval_min
        k = f"{key}@{iv}"
        if k not in self.series:
            self.series[k] = CandleSeries(key, iv, self.session_open, has_volume)
        return self.series[k]

    def get(self, key: str, interval_min: int | None = None) -> CandleSeries | None:
        return self.series.get(f"{key}@{interval_min or self.interval_min}")

    def roll_session(self, new_session_open: dt.datetime) -> None:
        self.session_open = new_session_open
        for s in self.series.values():
            s.session_open = new_session_open
            s._last_cum_volume = None
            s._working = None
