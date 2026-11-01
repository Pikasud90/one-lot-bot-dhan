"""The Desk drives a paper book (US-001 AC8) — developer tests for T4.

    Desk(..., paper: PaperBook | None = None)
    _raise_signal: desk_signals insert -> paper.on_alert(copy.deepcopy(sig), now) -> notifier.send
    poll:          ... _manage_positions -> paper.step(now, bars) -> _audit_archive -> snapshot

The Desk owns `hook_errors: int` (every on_alert / step exception, Manager decision 5);
snapshot["paper"] is the book's status() plus the Desk's own count under
"desk_hook_errors", shown separately (Manager decision (d)); None without a book. Nothing
a book does can stop an alert, its notification or the rest of the poll.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

from onelot.clock import IST, now_ist
from onelot.config import AppConfig
from onelot.data.schema import desk_signals
from onelot.desk import Desk
from onelot.desk.config import DeskConfig, DeskInstrumentCfg, StrategySlotCfg
from onelot.desk.tracker import Verdict
from onelot.notify.base import Notification
from onelot.risk.governor import RiskGovernor
from onelot.store import init_database
from onelot.store.repo import Repo

STATUS = {"opened": 3, "filled": 2, "no_fill": 1, "closed": 0, "hook_errors": 4}


def synthetic(base: float, seed: int, days: int = 6) -> pd.DataFrame:
    """`days` sessions of 2-minute bars ending yesterday 15:30 IST (only closed bars)."""
    rng = np.random.default_rng(seed)
    end = pd.Timestamp.now(tz=IST).normalize() - pd.Timedelta(days=1) + pd.Timedelta(hours=15.5)
    day, sessions = end.normalize(), []
    while len(sessions) < days:
        if day.weekday() < 5:
            sessions.append(day)
        day -= pd.Timedelta(days=1)
    idx = [
        t
        for d in sorted(sessions)
        for t in pd.date_range(d + pd.Timedelta(hours=9.25), periods=188, freq="2min")
        if t + pd.Timedelta(minutes=2) <= end
    ]
    close = base * np.exp(np.cumsum(rng.normal(0.0, 0.0012, len(idx))))
    open_ = np.r_[base, close[:-1]]
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) * 1.001,
            "low": np.minimum(open_, close) * 0.999,
            "close": close,
            "volume": 1000.0,
        },
        index=pd.DatetimeIndex(idx),
    )


class Source:
    name, delayed, resolution, primary, last_error = "offline", False, "2m", None, None

    def __init__(self) -> None:
        self.served_by = {"BANKNIFTY": "dhan", "SBIN": "dhan"}
        self.frames = {"BANKNIFTY": synthetic(54000.0, 11), "SBIN": synthetic(950.0, 12)}

    def fetch(self, specs, days, since=None):
        return {k: self.frames[k] for k in specs if k in self.frames}


class Notifier:
    def __init__(self, log: list[Any]) -> None:
        self.log, self.sent = log, []

    def send(self, n, dedup_key=None):
        self.log.append("send")
        self.sent.append(n)
        return {"fake": True}

    def info(self, title, body="", dedup_key=None):
        return self.send(Notification(title=title, body=body))


class Clock:
    """An always-open session, or (open=False) a shut market."""

    def __init__(self, open_: bool = True) -> None:
        self.open_ = open_

    def current_window(self, ts=None):
        now, is_open = ts or now_ist(), self.open_

        class W:
            open_at = now - dt.timedelta(hours=1)
            close_at = now + dt.timedelta(hours=3)
            mandatory_squareoff_at = now + dt.timedelta(hours=2)

            def is_open(self, _ts):
                return is_open

            def entries_allowed(self, _ts):
                return is_open

            def past_squareoff(self, _ts):
                return False

        return W()


class Book:
    """A recording stand-in for PaperBook; the first N on_alert / step calls raise."""

    def __init__(self, engine=None, log=None, fail_alert=0, fail_step=0, fail_status=False):
        self.engine, self.log = engine, log if log is not None else []
        self.fail_alert, self.fail_step, self.fail_status = fail_alert, fail_step, fail_status
        self.alerts: list[dict[str, Any]] = []
        self.steps: list[tuple[dt.datetime, Any]] = []
        self.strategy_exits: list[Any] = []

    def on_alert(self, alert, now):
        row = None
        if self.engine is not None:
            with self.engine.connect() as conn:
                stmt = select(desk_signals.c.status).where(desk_signals.c.id == alert["id"])
                row = conn.execute(stmt).scalar_one_or_none()
        self.log.append(("on_alert", row))
        self.alerts.append({"alert": alert, "now": now})
        if self.fail_alert:
            self.fail_alert -= 1
            raise RuntimeError(f"paper on_alert exploded for {alert['id']}")

    def step(self, now, bars, strategy_exit=None):
        self.log.append("step")
        self.steps.append((now, bars))
        self.strategy_exits.append(strategy_exit)
        if self.fail_step:
            self.fail_step -= 1
            raise RuntimeError("paper step exploded")
        return []

    def open_trades(self):
        return []

    def status(self):
        if self.fail_status:
            raise RuntimeError("status exploded")
        return dict(STATUS)


@pytest.fixture
def cfg() -> AppConfig:
    return AppConfig.load()


@pytest.fixture
def db(tmp_path):
    from onelot.data.db import get_database, use_database

    d = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    use_database(d)
    yield d
    d.dispose()
    get_database.__globals__["_db"] = None


def make_desk(cfg, db, *, paper=None, log=None, clock=None):
    log = log if log is not None else []
    repo = Repo(db)
    gov = RiskGovernor(
        cfg.risk, cfg.account.capital_inr, cfg.mode, now_ist().date().isoformat(), repo
    )
    desk_cfg = DeskConfig(
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
            DeskInstrumentCfg("SBIN", "SBIN", "STOCK", (StrategySlotCfg("ema_cross", "10m"),)),
        ),
    )
    kwargs: dict[str, Any] = {"repo": repo, "desk_cfg": desk_cfg}
    if paper is not None:
        kwargs["paper"] = paper
    notifier = Notifier(log)
    d = Desk(db.engine, cfg, notifier, gov, clock or Clock(), **kwargs)
    d.source = Source()
    d.fine_minutes = 2
    d.poll()
    assert not d.signals, "harness: a stray signal fired on the first poll"
    log.clear()
    return d, notifier


def raise_alert(d: Desk, key: str = "SBIN") -> tuple[dict[str, Any], dt.datetime]:
    t = d.trackers[key]
    now = t.bars.index[-41].to_pydatetime() + dt.timedelta(seconds=3)
    bar_ts = t.bars.index[t.bars.index <= now][-1]
    close = float(t.bars["close"].loc[bar_ts])
    v = Verdict("long", "test", bar_ts.to_pydatetime(), close * 0.995, close, 60.0, ["t"], True)
    d._raise_signal(t, t.slots[0], v, now)
    (sig,) = [s for s in d.signals.values() if s["instrument_key"] == t.instrument.key]
    return sig, now


# ── the Desk's own counter and snapshot block ────────────────────────────────────


def test_a_desk_without_a_book_has_no_paper_block_and_no_hook_errors(cfg, db):
    d, _ = make_desk(cfg, db)
    assert d.paper is None and d.hook_errors == 0
    assert d.snapshot()["paper"] is None


def test_the_snapshot_shows_the_book_status_and_the_desk_hook_errors_separately(cfg, db):
    d, _ = make_desk(cfg, db, paper=Book())
    assert d.snapshot()["paper"] == STATUS | {"desk_hook_errors": 0}
    d.paper.fail_step = 1
    d.poll()  # the failed step is in THIS poll's snapshot: step runs before the rebuild
    assert d.hook_errors == 1
    assert d.snapshot()["paper"] == STATUS | {"desk_hook_errors": 1}


def test_a_book_whose_status_raises_cannot_break_the_snapshot(cfg, db):
    d, _ = make_desk(cfg, db, paper=Book(fail_status=True))
    d.poll()
    snap = d.snapshot()
    assert snap["updated_at"] is not None and d.polls == 2
    assert snap["paper"]["desk_hook_errors"] == d.hook_errors


# ── on_alert ─────────────────────────────────────────────────────────────────────


def test_on_alert_gets_a_deep_copy_after_the_insert_and_before_the_notification(cfg, db):
    log: list[Any] = []
    book = Book(engine=db.engine, log=log)
    d, notifier = make_desk(cfg, db, paper=book, log=log)

    sig, now = raise_alert(d)

    assert log == [("on_alert", "PENDING"), "send"]
    (call,) = book.alerts
    assert call["now"] == now
    assert call["alert"] == sig and call["alert"] is not sig
    assert call["alert"]["economics"] is not sig["economics"]
    assert notifier.sent[-1].signal_id == sig["id"]


def test_a_failing_on_alert_is_counted_and_logged_with_the_alert_id(cfg, db, caplog):
    d, notifier = make_desk(cfg, db, paper=Book(fail_alert=1))
    with caplog.at_level(logging.ERROR, logger="onelot.desk.desk"):
        sig, _ = raise_alert(d)  # must not raise
    assert sig["id"] in d.signals and d.archive[0]["id"] == sig["id"]
    assert notifier.sent[-1].signal_id == sig["id"]
    assert d.hook_errors == 1
    assert any(sig["id"] in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)


# ── step ─────────────────────────────────────────────────────────────────────────


def test_step_runs_after_manage_positions_with_the_trackers_bars_by_series_key(
    cfg, db, monkeypatch
):
    log: list[Any] = []
    book = Book(log=log)
    d, _ = make_desk(cfg, db, paper=book, log=log)
    monkeypatch.setattr(d, "_manage_positions", lambda now, window: log.append("manage"))

    d.poll()

    assert log == ["manage", "step"]
    now, bars = book.steps[-1]
    assert now.tzinfo is not None
    assert bars("SBIN") is d.trackers["SBIN"].bars
    assert bars("BANKNIFTY") is d.trackers["BANKNIFTY"].bars
    assert bars("NO_SUCH_SERIES") is None


class PaperTrade:
    """The fields of onelot.paper.book.PaperTrade the desk's callback reads."""

    def __init__(self, direction: str = "long", series_key: str = "SBIN") -> None:
        self.series_key, self.direction = series_key, direction
        self.strategy, self.timeframe = "ema_cross", "10m"


@pytest.mark.parametrize(
    ("flags", "direction", "reason"),
    [
        ({"long_exit": True}, "long", "SIGNAL_EXIT"),
        ({"short_entry": True}, "long", "OPPOSITE_SIGNAL"),
        ({"short_exit": True}, "short", "SIGNAL_EXIT"),
        ({"long_entry": True}, "short", "OPPOSITE_SIGNAL"),
        ({"long_entry": True}, "long", None),  # the strategy still says long: HOLD
        ({}, "long", None),
    ],
)
def test_the_book_is_told_the_strategys_exit_on_its_last_closed_bar(
    cfg, db, flags, direction, reason
):
    """US-002: the paper book hears the same exit_advice the desk shows for a manual position,
    as a plain (reason, close time of that strategy bar) tuple: the desk never imports the
    paper package. Read-only: the slot is not touched."""
    book = Book()
    d, _ = make_desk(cfg, db, paper=book)
    slot = d.trackers["SBIN"].slots[0]
    slot.last_flags = dict(flags)
    ask = book.strategy_exits[-1]
    answer = ask(PaperTrade(direction), now_ist())
    if reason is None:
        assert answer is None
    else:
        assert answer == (reason, slot.last_bar_ts + dt.timedelta(minutes=10))
    assert slot.last_flags == flags


def test_the_strategy_exit_is_none_for_an_unknown_series_or_strategy(cfg, db):
    book = Book()
    d, _ = make_desk(cfg, db, paper=book)
    d.trackers["SBIN"].slots[0].last_flags = {"long_exit": True}
    ask = book.strategy_exits[-1]
    assert ask(PaperTrade(series_key="NOPE"), now_ist()) is None
    other = PaperTrade()
    other.strategy = "orb"
    assert ask(other, now_ist()) is None


def test_step_runs_when_the_market_is_shut_and_nothing_is_held(cfg, db):
    log: list[Any] = []
    d, _ = make_desk(cfg, db, paper=Book(log=log), log=log, clock=Clock(open_=False))
    d.poll()
    d.poll()
    assert log == ["step", "step"]


def test_failing_steps_are_counted_and_every_poll_finishes(cfg, db):
    d, _ = make_desk(cfg, db, paper=Book(fail_step=2))
    for _ in range(3):
        version = d.version
        d.poll()  # must not raise
        assert d.version > version
    assert d.hook_errors == 2 and d.polls == 4


# ── review round 1, MINOR-4: the book learns which series the desk tracks ─────────


class TrackingBook(Book):
    def __init__(self, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.told: list[tuple[set[str], dt.datetime]] = []

    def retire_untracked(self, series_keys, now):
        self.told.append((set(series_keys), now))
        if self.fail:
            raise RuntimeError("retire exploded")
        return []


def test_the_desk_tells_the_book_its_series_once_built_and_after_a_reload(cfg, db, monkeypatch):
    book = TrackingBook()
    d, _ = make_desk(cfg, db, paper=book)
    assert [keys for keys, _ in book.told] == [{"BANKNIFTY", "SBIN"}]
    assert book.told[0][1].tzinfo is not None

    monkeypatch.setattr("onelot.desk.desk.load_desk_config", lambda: d.cfg)
    d.reload()
    assert [keys for keys, _ in book.told] == [{"BANKNIFTY", "SBIN"}] * 2
    assert d.hook_errors == 0


def test_a_failing_retire_untracked_is_counted_and_never_stops_the_desk(cfg, db):
    d, _ = make_desk(cfg, db, paper=TrackingBook(fail=True))
    assert d.hook_errors == 1 and d.polls == 1
