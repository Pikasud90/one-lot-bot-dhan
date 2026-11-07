"""US-004 through the REAL Desk: the payloads it sends are what the outbox must carry.

The Desk is driven exactly as tests/desk/test_desk.py drives it (synthetic bars, no network,
no broker); only the Notifier is real here, with the Telegram channel on a file-based outbox and
a fake clock. The Desk calls Notifier.send while it holds its own RLock, so this also proves the
channel's work happens inside that call without HTTP.

  * a Yahoo-delayed SIGNAL arrives at the phone delayed, never BLOCKED (OF-5: no risk governor
    on alerts), and audible;
  * a live, allowed SIGNAL arrives audible, with no tags;
  * an EXIT advice the Desk raises on every poll becomes 1 message + 3 reminders (AC5, real
    payloads).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from us003_helpers import install_network_guard, telegram, visible
from us004_helpers import (
    FakeClock,
    OpenClock,
    build_desk,
    build_notifier,
    find_key,
    raise_signal,
    rig_factory,
)

from onelot.clock import now_ist
from onelot.store import init_database


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"only the injected transport may be used: {attempts}"


@pytest.fixture
def make_rig(tmp_path):
    with rig_factory(tmp_path) as build:
        yield build


# ========================= tests ========================================================
def test_a_delayed_signal_reaches_the_phone_tagged_and_unblocked_when_the_governor_says_no(
    make_rig, tmp_path, monkeypatch
):
    tg = telegram()
    clock = FakeClock(now_ist())
    rig = make_rig(clock=clock)
    notifier, _ = build_notifier(rig)
    db = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    desk = build_desk(db, notifier, delayed=True, blocked=True, monkeypatch=monkeypatch)
    try:
        sig = raise_signal(desk, clock())
        assert rig.rec.calls == 0, "the Desk's call only queues"
        stored = rig.row()["payload"]
        # owner feedback OF-5: no risk governor on alerts, so never blocked, never muted
        assert find_key(stored, "risk_allowed") is True
        assert find_key(find_key(stored, "data"), "delayed") is True

        rig.drain()

        (text,) = rig.rec.texts()
        assert text == tg.format_alert(sig, clock())
        assert "BLOCKED" not in text and "<b>delayed ~15m</b>" in text
        assert rig.rec.silent() == [False]
    finally:
        db.dispose()


def test_a_live_allowed_signal_reaches_the_phone_untagged_and_audible(
    make_rig, tmp_path, monkeypatch
):
    tg = telegram()
    clock = FakeClock(now_ist())
    rig = make_rig(clock=clock)
    notifier, _ = build_notifier(rig)
    db = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    desk = build_desk(db, notifier, delayed=False, blocked=False, monkeypatch=monkeypatch)
    try:
        sig = raise_signal(desk, clock())

        rig.drain()

        (text,) = rig.rec.texts()
        assert text == tg.format_alert(sig, clock())
        assert "BLOCKED" not in text and "delayed" not in text and "Data live" in text
        assert rig.rec.silent() == [False]
    finally:
        db.dispose()


def test_an_exit_is_one_message_and_the_position_closes_whatever_the_desk_polls(
    make_rig, tmp_path, monkeypatch
):
    clock = FakeClock(now_ist())
    rig = make_rig(clock=clock)
    notifier, _ = build_notifier(rig)
    db = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    desk = build_desk(db, notifier, delayed=False, blocked=False, monkeypatch=monkeypatch)
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
            desk._manage_positions(clock(), OpenClock().current_window(clock()))
            rig.drain()

        # owner feedback OF-5: one exit per entry, never repeated. With no paper trade the
        # desk's advice is that exit; it closes the position, so later polls say nothing.
        assert pid not in desk.positions and position["exit_advice"] == "STOP"
        exits = [
            (at - origin).total_seconds()
            for at, text in zip(rig.rec.times, rig.rec.texts(), strict=True)
            if visible(text).startswith("EXIT: SELL SBIN")  # US-003 decision 7: closing action
        ]
        assert exits == [0.0], "one EXIT, no reminders"
        assert len([r for r in rig.rows() if r["kind"] == "exit"]) == 1
    finally:
        db.dispose()
