"""Builders for the wide-list tests (US-099): synthetic daily bars, a fake broker that never
leaves the process, a recording notifier and a wide desk on a throw-away database."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import create_engine

from onelot.clock import IST, SessionClock, TradingCalendar
from onelot.config import CostConfig, SessionCfg
from onelot.delivery.money import delivery_model, depository_paise
from onelot.delivery.rule import PullbackParams
from onelot.delivery.universe import UniverseStock
from onelot.delivery.wide import StockDays, WideDesk
from onelot.store.schema import METADATA

ROOT = Path(__file__).resolve().parents[2]
COSTS = CostConfig.load(ROOT / "config" / "costs.yaml")
#: weekdays only (no holiday list: no year is covered, so no hole test applies)
CAL = TradingCalendar({}, verified=True)
START = dt.date(2024, 1, 1)
BASE = 240  # sessions of steady rise before anything happens


def weekdays(start: dt.date, n: int) -> list[dt.date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def at(day: dt.date, hhmm: str = "18:00") -> dt.datetime:
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(h, m), tzinfo=IST)


def closes(path: Sequence[tuple[int, float]], n: int, rise: float = 0.003) -> np.ndarray:
    """Closes of `n` sessions: a steady rise of `rise` a session from 100, then from each
    (session, daily change) of `path` on, that change a session (a drop of 10% is one
    session of -0.10 followed by another change)."""
    c = np.empty(n)
    c[0] = 100.0
    step = {i: g for i, g in path}
    g = rise
    for i in range(1, n):
        if i in step:
            g = step[i]
        c[i] = c[i - 1] * (1 + g)
    return c


def frame(dates: Sequence[dt.date], c: np.ndarray) -> pd.DataFrame:
    """Daily bars stamped 09:15 IST: each opens at the close before (the first at its close)."""
    o = np.r_[c[0], c[:-1]]
    idx = pd.DatetimeIndex([dt.datetime.combine(d, dt.time(9, 15), tzinfo=IST) for d in dates])
    return pd.DataFrame(
        {"open": o, "high": np.maximum(o, c), "low": np.minimum(o, c), "close": c, "volume": 1e5},
        index=idx,
    )


def drop_then(day: int, depth: float, after: float, n: int) -> np.ndarray:
    """A rise, one session down by `depth` on session `day`, then `after` a session."""
    return closes([(day, -depth), (day + 1, after)], n)


class Broker:
    """A fake broker: serves each stock's daily bars up to `published`; records each request.
    `fail(key, start)` says whether a request raises; nothing leaves the process."""

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames = frames
        self.published: dt.date | None = None
        self.calls: list[tuple[str, dt.date, dt.date]] = []
        self.fail: Callable[[str, dt.date], bool] = lambda key, start: False
        self.clock: FakeClock | None = None
        self.times: list[float] = []

    def fetch(self, stock: UniverseStock, start: dt.date, end: dt.date) -> pd.DataFrame:
        self.calls.append((stock.key, start, end))
        if self.clock is not None:
            self.times.append(self.clock.monotonic())
        if self.fail(stock.key, start):
            raise ConnectionError(f"{stock.key}: refused")
        f = self.frames[stock.key]
        d = pd.DatetimeIndex(f.index).tz_convert(IST).date
        last = min(end, self.published or end)
        return f[(d >= start) & (d <= last)]


class Recorder:
    """A notifier that keeps every notification it is given and accepts it."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    def send(self, n: Any, dedup_key: str | None = None) -> dict[str, bool]:
        self.sent.append(n)
        return {"recorder": True}

    def titles(self) -> list[str]:
        return [n.title for n in self.sent]


class FakeClock:
    """monotonic() and sleep() for the pacing test: time moves only when slept."""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def stock(key: str, group: str = "new") -> UniverseStock:
    return UniverseStock(key, f"{key} Ltd", key, "1", "NSE_EQ", group)


def nifty_of(dates: Sequence[dt.date]) -> Callable[[dt.datetime], tuple[list[dt.date], np.ndarray]]:
    """NIFTY rising every session (above its SMA200 from the 200th): what the round sees at
    `now` is its sessions up to the round's own."""
    c = closes([], len(dates), 0.002) * 100

    def get(now: dt.datetime) -> tuple[list[dt.date], np.ndarray]:
        day = now.astimezone(IST).date()
        k = sum(1 for d in dates if d <= day)
        return list(dates[:k]), c[:k]

    return get


def wide_desk(
    stocks: Sequence[UniverseStock],
    dates: Sequence[dt.date],
    *,
    engine: Any = None,
    path: Path | None = None,
    **kw: Any,
) -> WideDesk:
    eng = engine or create_engine(f"sqlite:///{path}" if path else "sqlite://")
    METADATA.create_all(eng)  # the store's tables (bars, delivery_trades) as the app has them
    kw.setdefault("nifty", nifty_of(dates))
    kw.setdefault("background", False)
    kw.setdefault("wall", lambda: dt.datetime(2000, 1, 1, tzinfo=IST))  # never 09:00 yet
    return WideDesk(
        eng, stocks, PullbackParams(), delivery_model(COSTS), depository_paise(COSTS), CAL,
        SessionClock(SessionCfg(), CAL), **kw,
    )  # fmt: skip


def source_of(frames: dict[str, pd.DataFrame]) -> Callable[..., dict[str, StockDays]]:
    days = {k: StockDays.of(k, f) for k, f in frames.items()}
    return lambda keys: {k: days[k] for k in keys if k in days}
