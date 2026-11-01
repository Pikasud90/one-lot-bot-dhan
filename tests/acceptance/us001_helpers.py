"""Offline harness shared by the US-001 acceptance tests (the paper book).

Nothing here touches the network, a broker, `current_settings()` or the wall clock. The
paper book is reached only through its public contract (ADR-001 + US-001): the package
is imported lazily inside `make_book`, so a missing `onelot.paper` makes each test fail on
its own instead of breaking collection of the whole file.

Conventions used by every test:

* a Thursday (`DAY`) is the trading day; `at(h, m, s)` builds an aware IST datetime;
* alerts are built like `Desk._raise_signal` builds them (`make_alert`), as plain JSON;
* bars are tz-aware IST frames at the desk fine resolution (`minute_bars`);
* money is asserted in integer paise; expected fills come from an exact `Fraction` oracle
  or, for options, from `lab.execution.execute_trade` itself (the differential oracle).
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import datetime as dt
import functools
import importlib
import json
import math
import shutil
import socket
import sqlite3
import tempfile
from collections.abc import Callable, Iterator, Sequence
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Engine, event, select, text

from onelot.clock import IST
from onelot.config import BacktestCfg, CostConfig
from onelot.costs import CostModel
from onelot.data.universe import InstrumentSpec
from onelot.store import init_database

ROOT = Path(__file__).resolve().parents[2]

DAY = dt.date(2026, 10, 29)  # a Thursday
FRIDAY = dt.date(2026, 10, 30)
MONDAY = dt.date(2026, 11, 2)

LOT = 30  # BANKNIFTY_OPT lot in the harness specs
NIFTY_LOT = 65
OPT_P0 = 450.0  # option premium at the alert (and what the fake pricer returns at U0)
OPT_U0 = 54000.0  # underlying at the alert

BT = BacktestCfg(slippage_pct=0.03, option_slippage_ticks=2, option_spread_pct=0.006)


def at(h: int, m: int, s: int = 0, us: int = 0, day: dt.date = DAY) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, h, m, s, us, tzinfo=IST)


CREATED = at(10, 5, 3)  # the worked example: the alert is raised at 10:05:03 IST
TTL = dt.timedelta(minutes=10)
EXPIRES = CREATED + TTL


# ── instruments and costs ─────────────────────────────────────────────────────────────
def _stock(key: str) -> InstrumentSpec:
    return InstrumentSpec(key, f"{key} (1 share)", "STOCK", key, 1, 0.05, "equity_intraday", 0.0)


def _option(key: str, series: str, lot: int) -> InstrumentSpec:
    return InstrumentSpec(
        key,
        f"{series} options (1 lot = {lot})",
        "OPTIONS",
        series,
        lot,
        0.05,
        "index_options",
        100.0,
    )


SPECS: dict[str, InstrumentSpec] = {k: _stock(k) for k in ("SBIN", "RELIANCE", "HDFCBANK", "INFY")}
SPECS["BANKNIFTY_OPT"] = _option("BANKNIFTY_OPT", "BANKNIFTY", LOT)
SPECS["NIFTY_OPT"] = _option("NIFTY_OPT", "NIFTY", NIFTY_LOT)


@functools.lru_cache(maxsize=1)
def _cost_cfg() -> CostConfig:
    return CostConfig.load(ROOT / "config" / "costs.yaml")


_cost_cfg()  # parsed once at import, never inside a test's window


def cost_for_specs(specs: dict[str, InstrumentSpec]) -> Callable[[str], CostModel]:
    """The injected `cost_for(instrument_key)`: the real cost table for that product."""

    def cost_for(instrument_key: str) -> CostModel:
        return CostModel(_cost_cfg().product(specs[instrument_key].product))

    return cost_for


cost_for = cost_for_specs(SPECS)


# ── the fake option pricer: (option dict, underlying, ts) -> premium ──────────────────
def make_pricer(
    p0: float = OPT_P0, u0: float = OPT_U0, delta: float = 0.65
) -> Callable[[Any, float, Any], float | None]:
    """Linear in the underlying, independent of strike/expiry/time, and equal to `p0` at
    `u0`: whatever constant basis a book derives at the alert, it is exactly zero."""
    calls: list[tuple[dict, float, Any]] = []

    def premium_at(option: Any, underlying: float, ts: Any) -> float | None:
        calls.append((dict(option), float(underlying), ts))
        sign = 1.0 if option["opt_type"] == "CE" else -1.0
        return max(0.05, p0 + sign * delta * (float(underlying) - u0))

    premium_at.calls = calls  # type: ignore[attr-defined]
    return premium_at


# ── building the book ─────────────────────────────────────────────────────────────────
def paper_book_class() -> type:
    return importlib.import_module("onelot.paper.book").PaperBook


def make_book(
    engine: Engine,
    *,
    enabled: bool = True,
    feed_delay: int = 15,
    bt: BacktestCfg | None = None,
    specs: dict[str, InstrumentSpec] | None = None,
    premium_at: Callable | None = None,
    clock: Callable[[], dt.datetime] | None = None,
    cls: type | None = None,
    reconcile_days: int | None = None,
):
    from onelot.config import PaperCfg

    specs = specs if specs is not None else SPECS
    extra = {} if reconcile_days is None else {"reconcile_days": reconcile_days}
    return (cls or paper_book_class())(
        engine,
        PaperCfg(enabled=enabled, feed_delay_minutes=feed_delay, **extra),
        bt or BT,
        specs,
        cost_for_specs(specs),
        premium_at or make_pricer(),
        clock=clock or (lambda: at(10, 30)),
    )


def new_db(tmp_path: Path, name: str = "us001.db", *, make_global: bool = False):
    d = init_database(f"sqlite:///{tmp_path / name}")
    if make_global:  # the Desk reads the process-wide database in a few places
        from onelot.data.db import use_database

        use_database(d)
    return d


@contextlib.contextmanager
def temp_book(**kw) -> Iterator[tuple[Any, Engine]]:
    """A throw-away database plus a book on it (for property tests: one per example)."""
    with tempfile.TemporaryDirectory() as folder:
        d = init_database(f"sqlite:///{folder}/prop.db")
        try:
            yield make_book(d.engine, **kw), d.engine
        finally:
            d.dispose()


def dispose_db(d, *, was_global: bool = False) -> None:
    d.dispose()
    if was_global:
        from onelot.data.db import get_database

        get_database.__globals__["_db"] = None


def block_network(monkeypatch) -> None:
    def refuse(*_a, **_k):
        raise AssertionError("US-001 tests must never touch the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    try:
        monkeypatch.setattr("yfinance.download", refuse)
    except (ImportError, AttributeError):  # pragma: no cover - yfinance absent
        pass


# ── alerts, built like Desk._raise_signal builds them ─────────────────────────────────
def make_alert(
    alert_id: str = "A1",
    *,
    kind: str = "STOCK",
    direction: str = "long",
    created: dt.datetime | None = None,
    ttl_min: float = 10,
    expires: dt.datetime | None = None,
    source: str | None = "dhan",
    resolution: str = "1m",
    delayed_flag: bool | None = None,
    entry: float | None = None,
    stop: float | None = None,
    target: float | None = None,
    status: str = "PENDING",
    risk_allowed: bool = True,
    score: float = 61.5,
    label: str = "medium",
    instrument_key: str | None = None,
    series_key: str | None = None,
    lot: int | None = None,
    eco_qty: int | None = None,
    premium: float | None = OPT_P0,
    iv: float | None = 0.16,
    drop_iv: bool = False,
    expiry: str = "2026-11-24",
    strike: float | None = None,
    strategy: str = "ema_cross",
    timeframe: str = "10m",
) -> dict[str, Any]:
    created = created or CREATED
    expires = expires or created + dt.timedelta(minutes=ttl_min)
    long_ = direction == "long"
    if kind == "STOCK":
        instrument_key = instrument_key or "SBIN"
        series_key = series_key or instrument_key
        entry = 1000.0 if entry is None else entry
        stop = (entry * 0.99 if long_ else entry * 1.01) if stop is None else stop
        target = (entry * 1.02 if long_ else entry * 0.98) if target is None else target
        side = "BUY" if long_ else "SELL"
        economics = {
            "kind": "STOCK",
            "side": side,
            "qty": 1 if eco_qty is None else eco_qty,
            "unit_label": "1 share",
            "underlying_entry": entry,
            "underlying_stop": stop,
            "underlying_target": target,
            "entry_price": entry,
            "stop_price": stop,
            "target_price": target,
            "outlay": entry,
            "risk_inr": round(abs(entry - stop) + 1.2, 2),
            "reward_inr": round(abs(target - entry) - 1.2, 2),
            "costs_inr": 1.2,
            "rr_net": 1.5,
            "r_target": 2.0,
            "option": {},
            "synthetic": False,
            "notes": [],
        }
    else:
        instrument_key = instrument_key or "BANKNIFTY_OPT"
        series_key = series_key or ("NIFTY" if instrument_key == "NIFTY_OPT" else "BANKNIFTY")
        lot = (NIFTY_LOT if instrument_key == "NIFTY_OPT" else LOT) if lot is None else lot
        entry = OPT_U0 if entry is None else entry
        stop = (entry - 150.0 if long_ else entry + 150.0) if stop is None else stop
        target = (entry + 300.0 if long_ else entry - 300.0) if target is None else target
        opt = "CE" if long_ else "PE"
        side = f"BUY_{opt}"
        economics = {
            "kind": "INDEX",
            "side": side,
            "qty": lot if eco_qty is None else eco_qty,
            "unit_label": f"1 lot ({lot})",
            "underlying_entry": entry,
            "underlying_stop": stop,
            "underlying_target": target,
            "entry_price": premium,
            "stop_price": round((premium or 0.0) - 97.5, 2),
            "target_price": round((premium or 0.0) + 195.0, 2),
            "outlay": round((premium or 0.0) * lot, 2),
            "risk_inr": 2925.0,
            "reward_inr": 5850.0,
            "costs_inr": 61.0,
            "rr_net": 1.9,
            "r_target": 2.0,
            "option": {
                "strike": entry if strike is None else strike,
                "opt_type": opt,
                "expiry": expiry,
                "delta": 0.65,
                "iv": iv,
                "premium": premium,
                "lot": lot,
            },
            "synthetic": True,
            "notes": ["premium is a Black-76 estimate off India VIX"],
        }
        if drop_iv:
            del economics["option"]["iv"]
    is_yahoo = str(source or "").lower().startswith("yahoo")
    return {
        "id": alert_id,
        "created_at": created.isoformat(),
        "expires_at": expires.isoformat(),
        "instrument_key": instrument_key,
        "series_key": series_key,
        "label": series_key,
        "kind": kind,
        "side": side,
        "direction": direction,
        "strategy": strategy,
        "strategy_label": strategy.replace("_", " "),
        "timeframe": timeframe,
        "bar_ts": (created - dt.timedelta(minutes=1)).isoformat(),
        "strength": 60.0,
        "confidence": {
            "score": score,
            "label": label,
            "parts": {"signal_strength": 60.0, "backtest_evidence": 50.0, "regime_fit": 70.0},
        },
        "horizon": "intraday",
        "data": {
            "source": source,
            "resolution": resolution,
            "delayed": is_yahoo if delayed_flag is None else delayed_flag,
            "delay_note": "Yahoo bars run ~15 minutes behind the exchange"
            if is_yahoo
            else "live exchange feed",
            "bar_age_sec": 14.0,
        },
        "reasons": ["test"],
        "economics": economics,
        "text": f"{side} 1 unit {series_key}",
        "status": status,
        "risk_allowed": risk_allowed,
        "risk_reason": "ok" if risk_allowed else "MANUAL HALT: test",
        "outcome": None,
        "manage": {
            "target_r": 2.0,
            "trail_atr_mult": 1.5,
            "trail_activate_r": 1.0,
            "max_bars": 0,
            "exit_time": "15:15",
            "atr": 4.2,
            "tf_min": 10,
            "horizon": "intraday",
        },
    }


def open_trade(book, alert: dict | None = None, now: dt.datetime | None = None, **kw):
    """on_alert with a private copy of the alert (the Desk passes a deep copy too)."""
    alert = alert if alert is not None else make_alert(**kw)
    when = now or dt.datetime.fromisoformat(alert["created_at"])
    return book.on_alert(copy.deepcopy(alert), when)


# ── bars ──────────────────────────────────────────────────────────────────────────────
def bars_at(
    stamps: Sequence[dt.datetime], opens: Sequence[float], *, spread: float = 0.5
) -> pd.DataFrame:
    o = np.asarray(opens, dtype=float)
    return pd.DataFrame(
        {
            "open": o,
            "high": o + spread,
            "low": o - spread,
            "close": o + spread / 5,
            "volume": np.full(len(o), 1000.0),
        },
        index=pd.DatetimeIndex(list(stamps)),
    )


def minute_bars(
    start: dt.datetime, opens: Sequence[float], step: int = 1, *, spread: float = 0.5
) -> pd.DataFrame:
    stamps = [start + dt.timedelta(minutes=step * i) for i in range(len(opens))]
    return bars_at(stamps, opens, spread=spread)


def standard_bars(opens: Sequence[float] = (998.0, 1000.0, 1001.0)) -> pd.DataFrame:
    """1-minute bars from 10:05: the worked example's frame (first eligible bar: 10:06)."""
    return minute_bars(at(10, 5), opens)


class Feed:
    """The `bars(series_key)` callable the desk hands to step(); counts its calls."""

    def __init__(self, **frames: pd.DataFrame | None) -> None:
        self.frames: dict[str, pd.DataFrame | None] = dict(frames)
        self.calls: list[str] = []

    def __call__(self, series_key: str) -> pd.DataFrame | None:
        self.calls.append(series_key)
        return self.frames.get(series_key)


def session_frame(
    day: dt.date, opens: Sequence[float] | None = None, step: int = 1
) -> pd.DataFrame:
    """A full NSE session of bars, 09:15 .. 15:29 (the last bar opens 15:29 at 1m)."""
    start = at(9, 15, day=day)
    n = (375 // step) if opens is None else len(opens)
    return minute_bars(start, opens if opens is not None else np.full(n, 1000.0), step)


# ── reading the book's tables (public contract: onelot.store.schema) ──────────────────
def _tables():
    schema = importlib.import_module("onelot.store.schema")
    return schema.paper_trades, schema.paper_events


def trade_rows(engine: Engine) -> list[dict]:
    trades, _ = _tables()
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(trades).order_by(trades.c.id)).mappings()]


def trade_row(engine: Engine, alert_id: str) -> dict:
    trades, _ = _tables()
    with engine.connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(select(trades).where(trades.c.alert_id == alert_id)).mappings()
        ]
    assert len(rows) == 1, f"expected exactly one paper_trades row for {alert_id}, got {len(rows)}"
    return rows[0]


def event_rows(engine: Engine, alert_id: str | None = None) -> list[dict]:
    trades, events = _tables()
    stmt = select(events).order_by(events.c.id)
    if alert_id is not None:
        stmt = (
            select(events)
            .join(trades, trades.c.id == events.c.trade_id)
            .where(trades.c.alert_id == alert_id)
            .order_by(events.c.id)
        )
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings()]


def count_rows(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())


def typeof(engine: Engine, column: str, alert_id: str) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text(f"SELECT typeof({column}) FROM paper_trades WHERE alert_id = :a"),
                {"a": alert_id},
            ).scalar_one()
        )


def insert_desk_signal(engine: Engine, alert: dict, status: str | None = None) -> None:
    """A desk_signals row exactly as `Desk._raise_signal` writes it."""
    from onelot.data.schema import desk_signals

    created = dt.datetime.fromisoformat(alert["created_at"])
    with engine.begin() as conn:
        conn.execute(
            desk_signals.insert().values(
                id=alert["id"],
                created_at=created,
                expires_at=dt.datetime.fromisoformat(alert["expires_at"]),
                instrument_key=alert["instrument_key"],
                series_key=alert["series_key"],
                kind=alert["kind"],
                side=alert["side"],
                strategy=alert["strategy"],
                timeframe=alert["timeframe"],
                bar_ts=dt.datetime.fromisoformat(alert["bar_ts"]),
                status=status or alert["status"],
                payload=alert,
            )
        )


def val(x: Any) -> Any:
    """Plain value of a str-Enum member or a str."""
    return getattr(x, "value", x)


def same_instant(a: dt.datetime | None, b: dt.datetime) -> bool:
    return a is not None and a.tzinfo is not None and a == b


def blob(obj: Any) -> str:
    """Lower-cased, separator-normalised JSON text of an event detail / row, for 'records why'."""
    s = json.dumps(obj, default=str).lower()
    return s.replace("-", "_").replace(" ", "_")


# ── oracles ───────────────────────────────────────────────────────────────────────────
def to_paise(x: float) -> int:
    """Price -> integer paise once, Decimal, ROUND_HALF_EVEN (ADR-001 amendment 5)."""
    return int(Decimal(str(float(x))).scaleb(2).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def expected_stock_entry(
    open_price: float, direction: str, slippage_pct: float = 0.03, tick_paise: int = 5
) -> int:
    """Exact: open in paise, slippage_pct is a PERCENT, then snap against the trader
    (buy -> up, sell -> down) to the tick, all in rational arithmetic."""
    o = to_paise(open_price)
    f = Fraction(str(slippage_pct)) / 100
    if direction == "long":
        return tick_paise * math.ceil(o * (1 + f) / tick_paise)
    return tick_paise * math.floor(o * (1 - f) / tick_paise)


def oracle_option_fill(
    *,
    bt: BacktestCfg,
    spec: InstrumentSpec,
    opt_type: str,
    underlying_open: float,
    fill_ts: dt.datetime,
    pricer: Callable[[Any, float, Any], float | None],
    underlying_stop: float | None = None,
) -> tuple[int, int]:
    """(entry_paise, risk_paise) that `lab.execution.execute_trade` books for the same entry.

    Entry is the premium it pays; risk is its `risk_inr`: (entry fill - the stop exit fill) x
    lot, where the stop fill is round_to_tick(stop_fair*(1-spread) - ticks*tick, down) floored
    at one tick (the Manager's decision: paper R and Lab R are comparable)."""
    from onelot.lab.execution import execute_trade
    from onelot.lab.simulator import Leg, SimTrade

    class Pricer:
        def iv_for(self, ts):
            return 0.16

        def premium(self, spot, k, ts, expiry, opt):
            return pricer({"opt_type": opt}, spot, ts)

        def delta(self, spot, k, ts, expiry, opt):
            return 0.65

    sign = 1 if opt_type == "CE" else -1
    stop = float(underlying_open) - 150.0 * sign if underlying_stop is None else underlying_stop
    ts = pd.Timestamp(fill_ts)
    t = SimTrade(
        symbol="BANKNIFTY",
        strategy="ema_cross",
        side=sign,
        signal_ts=ts - pd.Timedelta(minutes=1),
        entry_ts=ts,
        entry_price=float(underlying_open),
        stop_initial=float(stop),
        risk_ps=abs(float(underlying_open) - float(stop)),
        target=None,
        strength=60.0,
        atr=100.0,
        legs=[Leg(ts + pd.Timedelta(minutes=5), float(underlying_open), 1.0, "test")],
    )
    row = execute_trade(
        t, spec, CostModel(_cost_cfg().product(spec.product)), bt, bt.slippage_pct, Pricer()
    )
    return to_paise(row["detail"]["premium_entry"]), to_paise(row["risk_inr"])


def oracle_option_entry_paise(**kw: Any) -> int:
    """The premium `lab.execution.execute_trade` pays for the same entry, in paise."""
    return oracle_option_fill(**kw)[0]


def lab_fair_premium(
    *,
    strike: float,
    opt_type: str,
    expiry: str,
    underlying: float,
    ts: dt.datetime,
    iv: float,
    rate: float | None = None,
) -> float:
    """An independent Black-76 premium from the Lab's own pricer (constant IV = `iv`)."""
    from onelot.lab.execution import OptionPricer, expiry_datetime

    extra = {} if rate is None else {"risk_free_rate": rate}
    bt = BacktestCfg(option_iv_source="constant", option_iv_constant=iv, **extra)
    return float(
        OptionPricer(bt, None).premium(
            float(underlying),
            float(strike),
            pd.Timestamp(ts),
            expiry_datetime(dt.date.fromisoformat(expiry)),
            opt_type,
        )
    )


# ── a real Engine over a throw-away project root (never started) ──────────────────────
@contextlib.contextmanager
def real_engine(tmp_path: Path, **paper_overrides: Any) -> Iterator[Any]:
    """A real `onelot.engine.Engine` (mock broker, never started) whose project root is a
    copy of config/ in `tmp_path`, so its SQLite file, pid file and costs.yaml are all
    throw-away. `paper_overrides` replace fields of the config's `paper:` section."""
    from onelot.config import AppConfig, PaperCfg, Secrets
    from onelot.engine import Engine

    root = tmp_path / "root"
    shutil.copytree(ROOT / "config", root / "config")
    cfg = AppConfig.load(project_root=root)
    if paper_overrides:
        cfg = cfg.model_copy(
            update={"paper": PaperCfg(**{**cfg.paper.model_dump(), **paper_overrides})}
        )
    eng = Engine(cfg, Secrets(), use_mock=True)  # registers itself as the process-wide DB
    try:
        yield eng
    finally:
        eng.db.dispose()
        from onelot.data.db import get_database

        get_database.__globals__["_db"] = None


# ── corrupt rows and a locked database ────────────────────────────────────────────────
def raw_set_payload(engine: Engine, signal_id: str, payload_text: str | None) -> None:
    """Overwrite a desk_signals payload with raw text (not JSON-encoded by SQLAlchemy)."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE desk_signals SET payload = :p WHERE id = :i"),
            {"p": payload_text, "i": signal_id},
        )


def short_timeout_engine(db_path: Path | str, seconds: float) -> Engine:
    """An engine on the same SQLite file as `Database` builds, but with a SHORT busy timeout
    (the real one is 30 s). Same pragmas otherwise: WAL, synchronous NORMAL."""
    from sqlalchemy import create_engine

    eng = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": float(seconds)},
    )

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute(f"PRAGMA busy_timeout={int(seconds * 1000)}")
        cur.close()

    return eng


@contextlib.contextmanager
def write_lock(db_path: Path | str) -> Iterator[None]:
    """Hold SQLite's single write lock from another connection (BEGIN IMMEDIATE) until the
    block exits: every other writer waits for its busy timeout; readers (WAL) are unaffected."""
    conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield
    finally:
        try:
            conn.execute("ROLLBACK")
        finally:
            conn.close()


# ── failure injection ─────────────────────────────────────────────────────────────────
@contextlib.contextmanager
def failing_sql(
    engine: Engine, table: str, when: Callable[[str, Any], bool] | None = None
) -> Iterator[dict[str, int]]:
    """Make every INSERT/UPDATE/REPLACE that targets `table` raise, until the block exits.
    Works at the cursor, so it is independent of how the book structures its transaction.
    `when(statement, parameters)` narrows which statements fail (default: all of them)."""
    state = {"hits": 0}

    def boom(conn, cursor, statement, parameters, context, executemany):
        s = statement.lstrip().lower()
        if s.split(None, 1)[0] in ("insert", "update", "replace") and table in s:
            if when is None or when(s, parameters):
                state["hits"] += 1
                raise RuntimeError(f"injected failure on {table}")

    event.listen(engine, "before_cursor_execute", boom)
    try:
        yield state
    finally:
        event.remove(engine, "before_cursor_execute", boom)


def snapshot_tables(engine: Engine) -> dict[str, list[dict]]:
    return {"trades": trade_rows(engine), "events": event_rows(engine)}


# ── the Desk harness (offline; mirrors the US-028 acceptance harness) ─────────────────
BAR_MIN = 2
KEYS = ("BANKNIFTY", "SBIN")


def session_bars(base: float, seed: int, end: pd.Timestamp, days: int = 12) -> pd.DataFrame:
    """`days` NSE sessions of 2-minute bars (09:15..15:29) ending at the latest weekday on or
    before `end`; only bars that have closed by `end` exist. Deterministic for a seed."""
    rng = np.random.default_rng(seed)
    day, sessions = end.normalize(), []
    while len(sessions) < days:
        if day.weekday() < 5:
            sessions.append(day)
        day -= pd.Timedelta(days=1)
    idx: list[pd.Timestamp] = []
    for d in sorted(sessions):
        idx.extend(
            pd.date_range(d + pd.Timedelta(hours=9, minutes=15), periods=188, freq=f"{BAR_MIN}min")
        )
    idx = pd.DatetimeIndex([t for t in idx if t + pd.Timedelta(minutes=BAR_MIN) <= end])
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
        index=idx,
    )


def feed_end() -> pd.Timestamp:
    """Yesterday 15:30 IST: a full set of closed sessions whatever the wall clock says."""
    return (pd.Timestamp.now(tz=IST).normalize() - pd.Timedelta(days=1)) + pd.Timedelta(
        hours=15, minutes=30
    )


def desk_frames() -> dict[str, pd.DataFrame]:
    end = feed_end()
    return {
        "BANKNIFTY": session_bars(54000.0, seed=11, end=end),
        "SBIN": session_bars(950.0, seed=12, end=end),
    }


class OfflineSource:
    """A bar source with no network: fixed frames, a settable provenance per series."""

    delayed = False
    resolution = f"{BAR_MIN}m"
    primary = None
    last_error = None

    def __init__(self, frames: dict[str, pd.DataFrame], served: dict[str, str] | None = None):
        self.name = "offline"
        self.frames = frames
        self.served_by = dict(served if served is not None else dict.fromkeys(frames, "dhan"))

    def fetch(self, specs, days, since=None):
        return {k: self.frames[k] for k in specs if k in self.frames}


class FakeNotifier:
    def __init__(self) -> None:
        from onelot.notify.base import Notification

        self._cls = Notification
        self.sent: list[Any] = []

    def send(self, n, dedup_key=None):
        self.sent.append(n)
        return {"fake": True}

    def info(self, title, body="", dedup_key=None):
        return self.send(self._cls(title=title, body=body))


class OpenClock:
    """A session window that is always open with entries allowed."""

    class W:
        def __init__(self, now):
            self.open_at = now - dt.timedelta(hours=1)
            self.close_at = now + dt.timedelta(hours=3)
            self.mandatory_squareoff_at = now + dt.timedelta(hours=2)

        def is_open(self, ts):
            return True

        def entries_allowed(self, ts):
            return True

        def past_squareoff(self, ts):
            return False

    def current_window(self, ts=None):
        return OpenClock.W(ts or dt.datetime.now(tz=IST))


class ClosedClock(OpenClock):
    """The market is shut: no window is open, no entries."""

    class W(OpenClock.W):
        def is_open(self, ts):
            return False

        def entries_allowed(self, ts):
            return False

    def current_window(self, ts=None):
        return ClosedClock.W(ts or dt.datetime.now(tz=IST))


def small_desk_cfg():
    from onelot.desk.config import DeskConfig, DeskInstrumentCfg, StrategySlotCfg

    return DeskConfig(
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


def new_desk(
    app_cfg, db, *, paper=None, notifier=None, session_clock=None, served=None, frames=None
):
    """A Desk over an OfflineSource. `paper` is passed through the public `paper=` kwarg only
    when given, so a build without it exercises the pre-US-001 constructor call."""
    from onelot.clock import now_ist
    from onelot.desk import Desk
    from onelot.risk.governor import RiskGovernor
    from onelot.store.repo import Repo

    repo = Repo(db)
    gov = RiskGovernor(
        app_cfg.risk, app_cfg.account.capital_inr, app_cfg.mode, now_ist().date().isoformat(), repo
    )
    notifier = notifier or FakeNotifier()
    kwargs: dict[str, Any] = {"repo": repo, "desk_cfg": small_desk_cfg()}
    if paper is not None:
        kwargs["paper"] = paper
    desk = Desk(db.engine, app_cfg, notifier, gov, session_clock or OpenClock(), **kwargs)
    desk.source = OfflineSource(frames or desk_frames(), served)
    desk.fine_minutes = BAR_MIN
    return desk, notifier


def raise_alert(desk, key: str, *, now: dt.datetime | None = None, direction: str = "long") -> dict:
    """Raise an alert on `key` through Desk._raise_signal and return the new pending alert.
    With `now` inside the bar history the alert is created mid-session so later bars exist."""
    from onelot.desk.tracker import Verdict

    t = desk.trackers[key]
    if now is None:
        now = dt.datetime.now(tz=IST)
        bar_ts = t.bars.index[-1]
    else:
        bar_ts = t.bars.index[t.bars.index <= now][-1]
    close = float(t.bars["close"].loc[bar_ts])
    stop = close * 0.995 if direction == "long" else close * 1.005
    v = Verdict(direction, "test", bar_ts.to_pydatetime(), stop, close, 60.0, ["test"], True)
    before = set(desk.signals)
    desk._raise_signal(t, t.slots[0], v, now)
    new = [s for sid, s in desk.signals.items() if sid not in before]
    assert len(new) == 1, f"harness: no alert was raised for {key} ({t.last_error})"
    return new[0]


def mid_session(desk, key: str) -> dt.datetime:
    """An alert time (bar open + 3 s) 40 fine bars before the end of the held bars."""
    return desk.trackers[key].bars.index[-41].to_pydatetime() + dt.timedelta(seconds=3)


def desk_book(
    app_cfg, engine, *, enabled: bool = True, cls: type | None = None, clock: Callable | None = None
):
    """A real PaperBook for a Desk: real specs and costs, a constant fake pricer."""
    from onelot.data.universe import load_specs

    _, instruments = load_specs()
    return make_book(
        engine,
        enabled=enabled,
        bt=app_cfg.backtest,
        specs=instruments,
        premium_at=lambda o, u, ts: 100.0,
        clock=clock or (lambda: dt.datetime.now(tz=IST)),
        cls=cls,
    )


@dataclasses.dataclass
class StepCall:
    now: Any
    bars: Any
    strategy_exit: Any


class SpyPaper:
    """A stand-in PaperBook that records how the Desk drives it. `log` is shared with the
    test so call order against other desk activity can be asserted."""

    def __init__(self, engine=None, notifier=None, log: list[str] | None = None) -> None:
        self.engine = engine
        self.notifier = notifier
        self.log = log if log is not None else []
        self.alert_calls: list[dict[str, Any]] = []
        self.step_calls: list[StepCall] = []

    def on_alert(self, alert, now):
        seen: dict[str, Any] = {"alert": alert, "now": now}
        if self.engine is not None:
            from onelot.data.schema import desk_signals

            with self.engine.connect() as conn:
                row = (
                    conn.execute(select(desk_signals).where(desk_signals.c.id == alert["id"]))
                    .mappings()
                    .first()
                )
            seen["desk_row_status"] = None if row is None else row["status"]
        if self.notifier is not None:
            seen["notifications_sent"] = len(self.notifier.sent)
        self.alert_calls.append(seen)
        self.log.append("on_alert")
        return None

    def step(self, now, bars, strategy_exit=None):
        self.step_calls.append(StepCall(now, bars, strategy_exit))
        self.log.append("step")
        return []

    def open_trades(self):
        return []

    def status(self):
        return {"opened": 0, "filled": 0, "no_fill": 0, "closed": 0, "hook_errors": 0}
