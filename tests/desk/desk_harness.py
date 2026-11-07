"""A small offline Desk for the paper-test and fill-cost tests: fakes and builders only.

No network and no broker: the notifier is a recorder, the session is always open, the feed is a
synthetic random walk, and the "adapter" a test hands the desk is whatever fake it needs.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import zlib
from typing import Any

import numpy as np
import pandas as pd

from onelot.clock import IST, SessionWindow, now_ist
from onelot.desk import Desk
from onelot.desk.config import DeskConfig, DeskInstrumentCfg, StrategySlotCfg
from onelot.notify.base import Notification
from onelot.risk.governor import RiskGovernor
from onelot.store.repo import Repo


def synthetic(
    days: int = 12, freq_min: int = 2, base: float = 1000.0, seed: int = 3, end=None
) -> pd.DataFrame:
    """`days` NSE sessions of `freq_min`-minute bars (09:15..15:29) ending at the latest weekday
    on or before `end`; only bars that have closed by `end` exist."""
    rng = np.random.default_rng(seed)
    end = end if end is not None else yesterday_close()
    day, sessions = end.normalize(), []
    while len(sessions) < days:
        if day.weekday() < 5:
            sessions.append(day)
        day -= pd.Timedelta(days=1)
    idx: list[pd.Timestamp] = []
    per_day = 375 // freq_min
    for d in sorted(sessions):
        idx.extend(
            pd.date_range(
                d + pd.Timedelta(hours=9, minutes=15), periods=per_day, freq=f"{freq_min}min"
            )
        )
    idx = [t for t in idx if t + pd.Timedelta(minutes=freq_min) <= end]
    n = len(idx)
    close = base * np.exp(np.cumsum(rng.normal(0.0, 0.0012, n)))
    open_ = np.r_[base, close[:-1]]
    spread = np.abs(rng.normal(0, 0.0012, n)) * close
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + spread,
            "low": np.minimum(open_, close) - spread,
            "close": close,
            "volume": rng.integers(1000, 5000, n).astype(float),
        },
        index=pd.DatetimeIndex(idx),
    )


def yesterday_close() -> pd.Timestamp:
    """Yesterday 15:30 IST: a full set of closed sessions whatever the wall clock says."""
    return (pd.Timestamp.now(tz=IST).normalize() - pd.Timedelta(days=1)) + pd.Timedelta(
        hours=15, minutes=30
    )


def seed_of(key: str) -> int:
    return zlib.crc32(key.encode()) % 1000


class FakeSource:
    name, delayed, resolution = "fake", False, "2m"
    primary = None
    last_error = None

    def __init__(self) -> None:
        self.served_by: dict[str, str] = {}

    def fetch(self, specs, days, since=None):
        return {k: synthetic(seed=seed_of(k), end=yesterday_close()) for k in specs}


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def send(self, n, dedup_key=None):
        self.sent.append(n)
        return {"fake": True}

    def info(self, title, body="", dedup_key=None):
        return self.send(Notification(title=title, body=body))


def session_window(now: dt.datetime, *, open_: bool) -> SessionWindow:
    """The REAL `SessionWindow`, open around `now` (or long shut): its remaining boundaries are
    filled by field name, so this harness never spells the exchange's exit-time names."""
    hour = dt.timedelta(hours=1)
    if open_:
        times = {"open_at": now - hour, "close_at": now + 3 * hour,
                 "first_entry_at": now - hour, "no_new_entry_after": now + 2 * hour}  # fmt: skip
        other = now + 2 * hour
    else:
        times = {"open_at": now - 10 * hour, "close_at": now - 5 * hour,
                 "first_entry_at": now - 10 * hour, "no_new_entry_after": now - 6 * hour}  # fmt: skip
        other = now - 5 * hour
    values: dict[str, Any] = {"trade_date": now.date()}
    for f in dataclasses.fields(SessionWindow):
        if f.name != "trade_date":
            values[f.name] = times.get(f.name, other)
    return SessionWindow(**values)


class OpenClock:
    """A session clock whose window is always open with entries allowed."""

    def current_window(self, ts=None):
        return session_window(ts or now_ist(), open_=True)


class ShutClock:
    """The market is closed."""

    def current_window(self, ts=None):
        return session_window(ts or now_ist(), open_=False)


def small_cfg(**over: Any) -> DeskConfig:
    cfg = DeskConfig(
        poll_seconds=60,
        history_days=10,
        source="yahoo",
        signal_valid_minutes=10,
        context=(),
        indices=(
            DeskInstrumentCfg(
                "BANKNIFTY", "BANKNIFTY_OPT", "INDEX", (StrategySlotCfg("ema_cross", "10m"),)
            ),
        ),
        stocks=(
            DeskInstrumentCfg(
                "SBIN",
                "SBIN",
                "STOCK",
                (StrategySlotCfg("ema_cross", "10m"), StrategySlotCfg("supertrend_vol", "6m")),
            ),
        ),
    )
    return dataclasses.replace(cfg, **over) if over else cfg


def new_desk(app_cfg, db, *, desk_cfg: DeskConfig | None = None, clock=None, **kw):
    """(desk, notifier, governor) on the test database; extra keywords go to `Desk(...)`."""
    repo = Repo(db)
    gov = RiskGovernor(
        app_cfg.risk, app_cfg.account.capital_inr, app_cfg.mode, now_ist().date().isoformat(), repo
    )
    notifier = FakeNotifier()
    desk = Desk(
        db.engine,
        app_cfg,
        notifier,
        gov,
        clock or OpenClock(),
        repo=repo,
        desk_cfg=desk_cfg or small_cfg(),
        **kw,
    )
    desk.source = FakeSource()
    desk.fine_minutes = 2
    return desk, notifier, gov
