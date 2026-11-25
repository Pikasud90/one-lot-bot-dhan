"""A small fixture store for the Volume Watch: three stocks and two indices over 70 sessions,
with a market-wide spike on the last session. No network, no real data."""

from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from onelot.clock import IST
from onelot.desk.config import DeskConfig, DeskInstrumentCfg, StrategySlotCfg, VolumeCfg

from .helpers import sessions

BASKET = ("SBIN", "INFY", "TCS")
ABSENT = "ITC"  # in the basket, never any bar
DAYS = sessions("2025-03-03", 70)
SPIKE_DAY = DAYS[-1]
SPIKE_COLS = range(100, 105)  # 10:55 to 10:59: 30 times the usual turnover
HALF_DAY = DAYS[30]  # 60 bars only: never a baseline


def make_bars(seed: int, base: float, spike: bool = True, days=DAYS) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    frames = []
    for d in days:
        n = 60 if d == HALF_DAY else 375
        t0 = pd.Timestamp(dt.datetime.combine(d, dt.time(9, 15)), tz=IST)
        idx = pd.date_range(t0, periods=n, freq="1min", name="ts")
        close = base * np.exp(np.cumsum(rng.normal(0, 0.0008, n)))
        open_ = np.r_[base, close[:-1]]
        sp = np.abs(rng.normal(0, 0.0006, n)) * close
        vol = rng.lognormal(np.log(2000), 0.3, n)
        if spike and d == SPIKE_DAY:
            vol[list(SPIKE_COLS)] *= 30
            close[list(SPIKE_COLS)] = close[99] * (1 - 0.002 * np.arange(1, 6))
        if d == HALF_DAY:
            vol *= 50
        frames.append(
            pd.DataFrame(
                {
                    "open": open_,
                    "high": np.maximum(open_, close) + sp,
                    "low": np.minimum(open_, close) - sp,
                    "close": close,
                    "volume": vol,
                },
                index=idx,
            )  # fmt: skip
        )
        base = float(close[-1])
    return pd.concat(frames)


def index_from(stocks: dict[str, pd.DataFrame], weights: dict[str, float], level: float):
    rets = pd.DataFrame({k: v["close"].pct_change().fillna(0.0) for k, v in stocks.items()})
    r = sum(rets[k] * w for k, w in weights.items())
    close = level * np.exp(np.log1p(r).cumsum())
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 0.0},
        index=close.index,
    )


@pytest.fixture(scope="session")
def frames() -> dict[str, pd.DataFrame]:
    st = {k: make_bars(i + 1, 500.0 + 300 * i) for i, k in enumerate(BASKET)}
    st["NIFTY"] = index_from(
        {k: st[k] for k in BASKET}, {"SBIN": 0.2, "INFY": 0.5, "TCS": 0.3}, 22000
    )
    st["BANKNIFTY"] = index_from({"SBIN": st["SBIN"]}, {"SBIN": 1.0}, 48000)
    return st


def write_bars(path: Path, frames: dict[str, pd.DataFrame]) -> None:
    """Rows straight into `bars` (the store's own layout: bar start, naive UTC): `save_bars`
    is too slow for 130,000 rows in a test."""
    import sqlite3

    con = sqlite3.connect(path)
    try:
        for k, df in frames.items():
            ts = pd.DatetimeIndex(df.index).tz_convert("UTC").strftime("%Y-%m-%d %H:%M:%S.%f")
            rows = zip(ts, [k] * len(df), ["1m"] * len(df), *(df[c].astype(float).tolist()
                       for c in ("open", "high", "low", "close", "volume")), strict=True)  # fmt: skip
            con.executemany(
                "INSERT INTO bars (ts, series_key, resolution, open, high, low, close, volume, "
                "source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 't')",
                rows,
            )
        con.commit()
    finally:
        con.close()


@pytest.fixture(scope="session")
def store_template(tmp_path_factory, frames) -> Path:
    from onelot.store import init_database

    path = tmp_path_factory.mktemp("volstore") / "vol.db"
    db = init_database(f"sqlite:///{path}")
    db.dispose()
    write_bars(path, frames)
    return path


@pytest.fixture
def store(tmp_path: Path, store_template: Path):
    import shutil

    from sqlalchemy import create_engine

    path = tmp_path / "vol.db"
    shutil.copy(store_template, path)
    engine = create_engine(f"sqlite:///{path}")
    yield engine
    engine.dispose()


def desk_cfg(**volume) -> DeskConfig:
    slot = (StrategySlotCfg("ema_cross", "10m"),)
    return DeskConfig(
        indices=(
            DeskInstrumentCfg("NIFTY", "NIFTY_OPT", "INDEX", slot),
            DeskInstrumentCfg("BANKNIFTY", "BANKNIFTY_OPT", "INDEX", slot),
        ),
        stocks=tuple(DeskInstrumentCfg(k, k, "STOCK", slot) for k in (*BASKET, ABSENT)),
        volume=dataclasses.replace(VolumeCfg(rule="clv"), **volume),
    )


def at(day: dt.date, hhmm: str) -> dt.datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(h, m), IST)


class Recorder:
    """A Notifier that records what it is given."""

    def __init__(self) -> None:
        self.sent: list = []

    def send(self, n, dedup_key=None):
        self.sent.append(n)
        return {"recorder": True}
