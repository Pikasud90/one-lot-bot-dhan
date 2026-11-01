"""Fixtures for the paper-book unit tests (US-001).

A real SQLite file per test, hand-built instrument specs, desk-shaped alert dicts
(the dict `Desk._raise_signal` builds) and fine-resolution bar frames. Hermetic: no
network, no `current_settings()`, a fixed clock.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Engine, Table, create_engine, event, select

from onelot.clock import IST
from onelot.config import BacktestCfg, CostConfig, PaperCfg
from onelot.costs import CostModel
from onelot.data.universe import InstrumentSpec
from onelot.store import init_database

THU = dt.date(2026, 10, 29)  # an ordinary Thursday session
FRI = dt.date(2026, 10, 30)
MON = dt.date(2026, 11, 2)  # the session after FRI

# Pinned explicitly: slippage_pct is a PERCENT, option_spread_pct a FRACTION.
BT = BacktestCfg(slippage_pct=0.03, option_spread_pct=0.006, option_slippage_ticks=2)

SPECS: dict[str, InstrumentSpec] = {
    "RELIANCE": InstrumentSpec(
        "RELIANCE", "Reliance Industries (1 share)", "STOCK", "RELIANCE", 1, 0.05, "equity_intraday"
    ),
    "BANKNIFTY_OPT": InstrumentSpec(
        "BANKNIFTY_OPT",
        "NIFTY BANK options (1 lot = 30)",
        "OPTIONS",
        "BANKNIFTY",
        30,
        0.05,
        "index_options",
        100.0,
        extra={"underlying_kind": "INDEX"},
    ),
}


def ist(d: dt.date, hh: int, mm: int, ss: int = 0) -> dt.datetime:
    return dt.datetime.combine(d, dt.time(hh, mm, ss), tzinfo=IST)


def make_alert(
    kind: str = "STOCK",
    direction: str = "long",
    *,
    alert_id: str = "a1b2c3d4e5f6",
    source: str | None = "dhan",
    resolution: str = "1m",
    created: dt.datetime | None = None,
    ttl_min: int = 10,
    status: str = "PENDING",
    risk_allowed: bool = True,
    stop: float | None = None,
) -> dict[str, Any]:
    """A desk alert as `Desk._raise_signal` builds it (stock: 1 share; index: 1 lot CE/PE)."""
    created = created or ist(THU, 10, 5, 3)
    sign = 1 if direction == "long" else -1
    if kind == "STOCK":
        entry = 1000.00
        stop = stop if stop is not None else entry - sign * 5.0
        target = entry + sign * 10.0
        side = "BUY" if sign > 0 else "SELL"
        instrument, series, option, qty = "RELIANCE", "RELIANCE", {}, 1
        price, stop_px, target_px = entry, stop, target
    else:
        entry = 45210.35
        stop = stop if stop is not None else entry - sign * 100.0
        target = entry + sign * 200.0
        opt = "CE" if sign > 0 else "PE"
        side = f"BUY_{opt}"
        instrument, series, qty = "BANKNIFTY_OPT", "BANKNIFTY", 30
        option = {
            "strike": 45000.0 if sign > 0 else 45400.0,
            "opt_type": opt,
            "expiry": "2026-11-24",
            "delta": 0.65,
            "iv": 0.1532,
            "premium": 312.34,
            "lot": 30,
        }
        price, stop_px, target_px = 312.34, 255.1, 430.2
    return {
        "id": alert_id,
        "created_at": created.isoformat(),
        "expires_at": (created + dt.timedelta(minutes=ttl_min)).isoformat(),
        "instrument_key": instrument,
        "series_key": series,
        "label": series,
        "kind": kind,
        "side": side,
        "direction": direction,
        "strategy": "ema_cross",
        "strategy_label": "EMA cross",
        "timeframe": "5m",
        "bar_ts": (created - dt.timedelta(minutes=5, seconds=3)).isoformat(),
        "strength": 0.7,
        "confidence": {"score": 62.5, "label": "medium", "parts": {"signal": 70.0}},
        "horizon": "intraday",
        "data": {
            "source": source,
            "resolution": resolution,
            "delayed": source is not None and source.startswith("yahoo"),
            "delay_note": "live exchange feed",
            "bar_age_sec": 3.0,
        },
        "reasons": ["EMA9 crossed EMA21"],
        "economics": {
            "kind": kind,
            "side": side,
            "qty": qty,
            "unit_label": "1 share" if kind == "STOCK" else "1 lot (30)",
            "underlying_entry": entry,
            "underlying_stop": stop,
            "underlying_target": target,
            "entry_price": price,
            "stop_price": stop_px,
            "target_price": target_px,
            "outlay": price * qty,
            "risk_inr": 512.0,
            "reward_inr": 900.0,
            "costs_inr": 41.2,
            "rr_net": 1.76,
            "r_target": 2.0,
            "option": option,
            "synthetic": kind == "INDEX",
            "notes": [],
        },
        "text": "BUY ...",
        "status": status,
        "risk_allowed": risk_allowed,
        "risk_reason": None if risk_allowed else "daily loss limit reached",
        "outcome": None,
        "manage": {
            "target_r": 2.0,
            "trail_atr_mult": 0.0,
            "trail_activate_r": 1.0,
            "max_bars": 0,
            "exit_time": "15:15",
            "atr": 4.2,
            "tf_min": 5,
            "horizon": "intraday",
        },
    }


def make_bars(start: dt.datetime, opens: list[float], res_min: int = 1) -> pd.DataFrame:
    """Bars at `res_min` spacing from `start` (bar OPEN times, tz-aware IST)."""
    idx = pd.date_range(start, periods=len(opens), freq=f"{res_min}min")
    o = np.asarray(opens, dtype=float)
    return pd.DataFrame(
        {"open": o, "high": o + 1.0, "low": o - 1.0, "close": o + 0.5, "volume": 1000.0},
        index=idx,
    )


def table_rows(engine: Engine, table: Table) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(table)).mappings().all()]


class FakePricer:
    """`premium_at(option, underlying, ts)` returning a fixed fair premium; records calls."""

    def __init__(self, fair: float | None = 312.34) -> None:
        self.fair = fair
        self.calls: list[tuple[dict[str, Any], float, dt.datetime]] = []

    def __call__(
        self, option: Mapping[str, Any], underlying: float, ts: dt.datetime
    ) -> float | None:
        self.calls.append((dict(option), float(underlying), ts))
        return self.fair


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    db = init_database(f"sqlite:///{tmp_path / 'paper.db'}")
    yield db.engine
    db.dispose()


@pytest.fixture
def pricer() -> FakePricer:
    return FakePricer()


@pytest.fixture
def make_book(engine: Engine, cost_cfg: CostConfig, pricer: FakePricer) -> Callable[..., Any]:
    """Build a PaperBook on the test database; keyword overrides for cfg/bt/pricer."""
    from onelot.paper.book import PaperBook

    models = {k: CostModel(cost_cfg.product(s.product)) for k, s in SPECS.items()}

    def _make(
        enabled: bool = True,
        feed_delay_minutes: int = 15,
        bt: BacktestCfg = BT,
        premium_at: Callable[..., float | None] | None = None,
        reconcile_days: int = 3,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> PaperBook:
        cfg = PaperCfg(
            enabled=enabled, feed_delay_minutes=feed_delay_minutes, reconcile_days=reconcile_days
        )
        return PaperBook(
            engine,
            cfg,
            bt,
            SPECS,
            models.__getitem__,
            premium_at or pricer,
            clock=clock or (lambda: ist(THU, 10, 8)),
        )

    return _make


def store_desk_signal(engine: Engine, alert: Mapping[str, Any], payload: Any = ...) -> None:
    """A desk_signals row as Desk._raise_signal writes it (payload = the alert dict)."""
    from onelot.store.schema import desk_signals

    with engine.begin() as conn:
        conn.execute(
            desk_signals.insert().values(
                id=alert["id"],
                created_at=dt.datetime.fromisoformat(alert["created_at"]),
                expires_at=dt.datetime.fromisoformat(alert["expires_at"]),
                instrument_key=alert["instrument_key"],
                series_key=alert["series_key"],
                kind=alert["kind"],
                side=alert["side"],
                strategy=alert["strategy"],
                timeframe=alert["timeframe"],
                bar_ts=dt.datetime.fromisoformat(alert["bar_ts"]),
                status=alert["status"],
                payload=alert if payload is ... else payload,
            )
        )


# ── a busy SQLite database (US-001 review round 1: BUG-1, BUG-2) ──────────────────
BUSY = 0.2  # seconds: the busy timeout of the `busy` book's engine


def short_engine(path: Path) -> Engine:
    eng = create_engine(
        f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": BUSY}
    )

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_conn, _rec):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute(f"PRAGMA busy_timeout={int(BUSY * 1000)}")
        cur.close()

    return eng


@contextlib.contextmanager
def write_lock(path: Path) -> Iterator[None]:
    """Hold SQLite's write lock from another connection until the block exits."""
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield
    finally:
        conn.execute("ROLLBACK")
        conn.close()


@pytest.fixture
def busy(tmp_path, cost_cfg):
    """(db file, a PaperBook on a short-busy-timeout engine, that engine)."""
    from onelot.config import PaperCfg
    from onelot.costs import CostModel
    from onelot.paper.book import PaperBook

    path = tmp_path / "busy.db"
    init_database(f"sqlite:///{path}").dispose()  # the full schema, WAL
    eng = short_engine(path)
    models = {k: CostModel(cost_cfg.product(s.product)) for k, s in SPECS.items()}
    book = PaperBook(
        eng, PaperCfg(), BT, SPECS, models.__getitem__, FakePricer(), clock=lambda: ist(THU, 10, 8)
    )
    try:
        yield path, book, eng
    finally:
        eng.dispose()
