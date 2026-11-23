"""Fixtures for the Simulator (US-075): synthetic one-minute bars, an in-memory bar source,
forced signals and a scratch database. No network, no real data."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from onelot.clock import IST
from onelot.sim import engine as sim_engine
from onelot.sim.manager import SimManager, default_deps

MIN = pd.Timedelta(minutes=1)


def trading_days(start: str, days: int) -> list[dt.date]:
    out: list[dt.date] = []
    d = dt.date.fromisoformat(start)
    while len(out) < days:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def day_index(day: dt.date) -> pd.DatetimeIndex:
    t0 = pd.Timestamp(dt.datetime.combine(day, dt.time(9, 15)), tz=IST)
    return pd.date_range(t0, periods=375, freq="1min")


def flat_minutes(start: str, days: int, level: float, half: float, volume: float = 0.0):
    """Every minute open = close = `level`, high/low `half` away: every true range is
    2 x half, so ATR(14) on any timeframe is exactly 2 x half."""
    idx = pd.DatetimeIndex(np.concatenate([day_index(d) for d in trading_days(start, days)]))
    n = len(idx)
    return pd.DataFrame(
        {
            "open": np.full(n, level),
            "high": np.full(n, level + half),
            "low": np.full(n, level - half),
            "close": np.full(n, level),
            "volume": np.full(n, volume),
        },
        index=idx.rename("ts"),
    )


def walk_minutes(start: str, days: int, base: float, vol: float, seed: int, volume: bool):
    idx = pd.DatetimeIndex(np.concatenate([day_index(d) for d in trading_days(start, days)]))
    rng = np.random.default_rng(seed)
    n = len(idx)
    close = base * np.exp(np.cumsum(rng.normal(0, vol, n)))
    open_ = np.r_[base, close[:-1]]
    spread = np.abs(rng.normal(0, vol, n)) * close
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + spread,
            "low": np.minimum(open_, close) - spread,
            "close": close,
            "volume": rng.integers(1000, 5000, n).astype(float) if volume else np.zeros(n),
        },
        index=idx.rename("ts"),
    )


def set_bar(df: pd.DataFrame, ts: str, o: float, h: float, lo: float, c: float) -> None:
    t = pd.Timestamp(ts, tz=IST)
    df.loc[t, ["open", "high", "low", "close"]] = [o, h, lo, c]


class MemBars:
    """A `BarSource` over frames in memory."""

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames = frames

    def minute_bars(self, key: str, start: Any, end: Any) -> pd.DataFrame:
        df = self.frames.get(key)
        if df is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        if start is not None:
            df = df[df.index >= pd.Timestamp(start)]
        if end is not None:
            df = df[df.index < pd.Timestamp(end)]
        return df

    def daily_bars(self, key: str) -> pd.DataFrame | None:
        return None

    def minute_range(self, key: str):
        df = self.frames.get(key)
        if df is None or df.empty:
            return None
        return pd.Timestamp(df.index[0]), pd.Timestamp(df.index[-1])


@pytest.fixture
def db_engine(tmp_path: Path):
    from onelot.store import init_database

    db = init_database(f"sqlite:///{tmp_path / 'sim.db'}")
    yield db.engine
    db.dispose()


def make_manager(engine, bars, tmp_path: Path, **kw) -> SimManager:
    deps = kw.pop("deps", None) or default_deps(
        kw.get("desk"), option_store_path=tmp_path / "no_store.db"
    )
    return SimManager(engine, deps=deps, bars=bars, **kw)


@pytest.fixture
def force(monkeypatch):
    """Force the strategy's signals: `force(long=[...], short=[...], long_exit=[...],
    short_exit=[...])`, each a list of candle START times ("2025-02-03 10:10"). Every other
    candle has no signal. The strategy's frame is still built the Lab's way."""
    real = sim_engine.build_signals

    def apply(**cols: list[str]) -> None:
        def fake(*args: Any, **kwargs: Any):
            f, candles = real(*args, **kwargs)
            if f is None:
                return f, candles
            f = f.copy()
            for side in ("long", "short"):
                f[f"{side}_entry"] = False
                f[f"{side}_exit"] = False
            for name, stamps in cols.items():
                col = name.replace("long", "long_entry") if name == "long" else name
                col = "short_entry" if name == "short" else col
                for s in stamps:
                    t = pd.Timestamp(s, tz=IST)
                    if t in f.index:
                        f.loc[t, col] = True
            return f, candles

        monkeypatch.setattr(sim_engine, "build_signals", fake)

    return apply
