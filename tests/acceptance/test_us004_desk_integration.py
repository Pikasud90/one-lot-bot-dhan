"""US-004 through the REAL Desk: the payloads it sends are what the outbox must carry.

The Desk is driven exactly as tests/desk/test_desk.py drives it (synthetic bars, no network,
no broker); only the Notifier is real here, with the Telegram channel on a file-based outbox and
a fake clock. The Desk calls Notifier.send while it holds its own RLock, so this also proves the
channel's work happens inside that call without HTTP.

  * a risk-blocked, Yahoo-delayed SIGNAL arrives at the phone as BLOCKED and delayed, and silent;
  * a live, allowed SIGNAL arrives audible, with no tags;
  * an EXIT advice the Desk raises on every poll becomes 1 message + 3 reminders (AC5, real
    payloads).
"""

from __future__ import annotations

import datetime as dt
import zlib
from typing import Any

import numpy as np
import pandas as pd
import pytest
from us003_helpers import install_network_guard, telegram, visible
from us004_helpers import FakeClock, build_notifier, find_key, rig_factory

from onelot.clock import IST, now_ist
from onelot.config import AppConfig
from onelot.desk import Desk
from onelot.desk.config import DeskConfig, DeskInstrumentCfg, StrategySlotCfg
from onelot.desk.data import COLUMNS, CompositeBars
from onelot.desk.tracker import Verdict
from onelot.risk.governor import RiskGovernor
from onelot.store import init_database
from onelot.store.repo import Repo


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"only the injected transport may be used: {attempts}"


@pytest.fixture
def make_rig(tmp_path):
    with rig_factory(tmp_path) as build:
        yield build


# ========================= a compact copy of the Desk test harness ======================
def _synthetic(
    days: int = 12, base: float = 1000.0, seed: int = 3, end: Any = None
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    end = end or pd.Timestamp.now(tz=IST).floor("2min")
    day, sessions = end.normalize(), []
    while len(sessions) < days:
        if day.weekday() < 5:
            sessions.append(day)
        day -= pd.Timedelta(days=1)
    idx: list[pd.Timestamp] = []
    for d in sorted(sessions):
        idx.extend(pd.date_range(d + pd.Timedelta(hours=9, minutes=15), periods=188, freq="2min"))
    index = pd.DatetimeIndex([t for t in idx if t + pd.Timedelta(minutes=2) <= end])
    n = len(index)
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
        index=index,
    )


def _yesterday_close() -> pd.Timestamp:
    return (pd.Timestamp.now(tz=IST).normalize() - pd.Timedelta(days=1)) + pd.Timedelta(
        hours=15, minutes=30
    )


class _Bars:
    delayed = False
    resolution = "2m"
    last_error = None

    def __init__(self, name: str, salt: int, empty_for: set[str] | None = None) -> None:
        self.name = name
        self.salt = salt
        self.empty_for = empty_for or set()

    def fetch(self, specs: Any, days: int, since: Any = None) -> dict[str, pd.DataFrame]:
        end = _yesterday_close()
        return {
            k: pd.DataFrame(columns=COLUMNS)
            if k in self.empty_for
            else _synthetic(seed=(zlib.crc32(k.encode()) + self.salt) % 1000, end=end)
            for k in specs
        }


class _OpenClock:
    class W:
        def __init__(self, now: dt.datetime) -> None:
            self.open_at = now - dt.timedelta(hours=1)
            self.close_at = now + dt.timedelta(hours=3)
            self.mandatory_squareoff_at = now + dt.timedelta(hours=2)

        def is_open(self, ts: Any) -> bool:
            return True

        def entries_allowed(self, ts: Any) -> bool:
            return True

        def past_squareoff(self, ts: Any) -> bool:
            return False

    def current_window(self, ts: dt.datetime | None = None) -> Any:
        return _OpenClock.W(ts or now_ist())


def _desk_config() -> DeskConfig:
    return DeskConfig(
        poll_seconds=60,
        history_days=10,
        source="yahoo",
        signal_valid_minutes=10,
        context=(),
        indices=(),
        stocks=(
            DeskInstrumentCfg("SBIN", "SBIN", "STOCK", (StrategySlotCfg("ema_cross", "10m"),)),
        ),
    )


def build_desk(tmp_path: Any, notifier: Any, *, delayed: bool, blocked: bool, monkeypatch: Any):
    db = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    cfg = AppConfig.load()
    repo = Repo(db)
    governor = RiskGovernor(
        cfg.risk, cfg.account.capital_inr, cfg.mode, now_ist().date().isoformat(), repo
    )
    desk = Desk(
        db.engine, cfg, notifier, governor, _OpenClock(), repo=repo, desk_cfg=_desk_config()
    )
    primary = _Bars("dhan", 0, empty_for={"SBIN"} if delayed else set())
    desk.source = CompositeBars(primary, _Bars("yahoo", 1))
    desk.fine_minutes = 2
    monkeypatch.setattr(desk, "_maybe_switch_source", lambda: None)
    desk.poll()
    if blocked:
        governor.manual_panic_halt()
    return desk, db


def raise_signal(desk: Any, now: dt.datetime) -> dict[str, Any]:
    t = desk.trackers["SBIN"]
    close = float(t.bars["close"].iloc[-1])
    verdict = Verdict(
        "long", "test", t.bars.index[-1].to_pydatetime(), close * 0.99, close, 60.0, [], True
    )
    desk._raise_signal(t, t.slots[0], verdict, now)
    return next(iter(desk.signals.values()))


# ========================= tests ========================================================
def test_a_blocked_and_delayed_signal_reaches_the_phone_tagged_and_silent(
    make_rig, tmp_path, monkeypatch
):
    tg = telegram()
    clock = FakeClock(now_ist())
    rig = make_rig(clock=clock)
    notifier, _ = build_notifier(rig)
    desk, db = build_desk(tmp_path, notifier, delayed=True, blocked=True, monkeypatch=monkeypatch)
    try:
        sig = raise_signal(desk, clock())
        assert rig.rec.calls == 0, "the Desk's call only queues"
        stored = rig.row()["payload"]
        assert find_key(stored, "risk_allowed") is False
        assert find_key(find_key(stored, "data"), "delayed") is True

        rig.drain()

        (text,) = rig.rec.texts()
        assert text == tg.format_alert(sig, clock())
        assert text.startswith("<b>[BLOCKED by risk] ") and "<b>delayed ~15m</b>" in text
        assert rig.rec.silent() == [True], "a risk-blocked signal is sent silent"
    finally:
        db.dispose()


def test_a_live_allowed_signal_reaches_the_phone_untagged_and_audible(
    make_rig, tmp_path, monkeypatch
):
    tg = telegram()
    clock = FakeClock(now_ist())
    rig = make_rig(clock=clock)
    notifier, _ = build_notifier(rig)
    desk, db = build_desk(tmp_path, notifier, delayed=False, blocked=False, monkeypatch=monkeypatch)
    try:
        sig = raise_signal(desk, clock())

        rig.drain()

        (text,) = rig.rec.texts()
        assert text == tg.format_alert(sig, clock())
        assert "BLOCKED" not in text and "delayed" not in text and "Data live" in text
        assert rig.rec.silent() == [False]
    finally:
        db.dispose()


def test_an_exit_the_desk_raises_on_every_poll_is_one_message_and_three_reminders(
    make_rig, tmp_path, monkeypatch
):
    clock = FakeClock(now_ist())
    rig = make_rig(clock=clock)
    notifier, _ = build_notifier(rig)
    desk, db = build_desk(tmp_path, notifier, delayed=False, blocked=False, monkeypatch=monkeypatch)
    try:
        sig = raise_signal(desk, clock())
        rig.drain()  # the SIGNAL message goes out first
        pid = desk.acknowledge(sig["id"], "TAKE")["position_id"]
        position = desk.positions[pid]
        tracker = desk.trackers["SBIN"]
        # price falls through the stop: the advice is STOP on every poll from now on
        tracker.bars.loc[tracker.bars.index[-1], "close"] = position["stop_underlying"] - 1.0
        clock.advance(60)
        origin = clock()

        for minute in range(0, 31):  # the Desk polls once a minute for half an hour
            clock.set(origin + dt.timedelta(minutes=minute))
            desk._manage_positions(clock(), _OpenClock().current_window(clock()))
            rig.drain()

        assert desk.positions[pid]["exit_advice"] == "STOP"
        exits = [
            (at - origin).total_seconds()
            for at, text in zip(rig.rec.times, rig.rec.texts(), strict=True)
            if visible(text).startswith("EXIT: SELL SBIN")  # US-003 decision 7: closing action
        ]
        assert exits == [0.0, 300.0, 600.0, 900.0], (
            "1 EXIT + 3 reminders, 5 minutes apart, while the Desk keeps raising it"
        )
        assert len([r for r in rig.rows() if r["kind"] == "exit"]) == 4
    finally:
        db.dispose()
