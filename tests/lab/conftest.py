"""Fixtures for the Backtest Lab tests: synthetic NSE-session bars and a scratch database."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def make_intraday(
    days: int = 5,
    seed: int = 7,
    start: str = "2026-09-01",
    drift: float = 0.0,
    vol: float = 0.0015,
    freq_min: int = 5,
    base: float = 1000.0,
    with_volume: bool = True,
) -> pd.DataFrame:
    """Synthetic NSE-session OHLCV bars (09:15-15:30 IST) as a random walk."""
    rng = np.random.default_rng(seed)
    idx = []
    day = pd.Timestamp(start, tz="Asia/Kolkata")
    while len({d.date() for d in idx}) < days:
        if day.weekday() < 5:
            idx.extend(
                pd.date_range(
                    day + pd.Timedelta(hours=9, minutes=15),
                    periods=375 // freq_min,
                    freq=f"{freq_min}min",
                )
            )
        day += pd.Timedelta(days=1)
    n = len(idx)
    rets = rng.normal(drift, vol, n)
    close = base * np.exp(np.cumsum(rets))
    open_ = np.r_[base, close[:-1]]
    spread = np.abs(rng.normal(0, vol, n)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = rng.integers(10_000, 50_000, n).astype(float) if with_volume else np.zeros(n)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=pd.DatetimeIndex(idx, name="ts"),
    )


@pytest.fixture
def bars5():
    return make_intraday()


@pytest.fixture
def lab_engine(tmp_path):
    from onelot.data.db import get_database, use_database
    from onelot.store import init_database

    db = init_database(f"sqlite:///{tmp_path / 'lab.db'}")
    use_database(db)
    yield db.engine
    db.dispose()
    get_database.__globals__["_db"] = None
