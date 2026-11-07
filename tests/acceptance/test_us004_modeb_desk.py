"""US-004 mode B through the REAL Desk (synthetic bars, no network):

* a stock SELL (a short) is intraday only, whatever stock_horizon says (ADR-001 amendment 8):
  the alert, its management rules and its square-off say so; a long stock still follows
  stock_horizon; the phone's EXIT for a short names the closing BUY ("cover short");
* the symbol the phone shows is the tradable one (universe.yaml trading_symbol: MM -> M&M),
  HTML-escaped on the wire and plain on the screen, on the alert and on the EXIT; an older
  alert without trading_symbol still opens a position that falls back to the series key.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from us003_helpers import DOT, install_network_guard, visible
from us004_helpers import (
    FakeClock,
    OpenClock,
    build_desk,
    build_notifier,
    raise_signal,
    rig_factory,
)

from onelot.clock import IST, now_ist
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


def position(desk: Any, pid: str) -> dict:
    """A taken position, open or closed. Since owner feedback OF-5 an alert without a paper
    trade closes on its one exit advice, so a closed one is read back from its stored row."""
    if pid in desk.positions:
        return desk.positions[pid]
    from sqlalchemy import select

    from onelot.data.schema import desk_positions

    with desk.engine.connect() as conn:
        return conn.execute(
            select(desk_positions.c.payload).where(desk_positions.c.id == pid)
        ).scalar_one()


def fall_through_stop(desk: Any, key: str, pid: str, rise: bool = False) -> None:
    """Move the last bar through the position's stop (up for a short, down for a long)."""
    tracker = desk.trackers[key]
    stop = desk.positions[pid]["stop_underlying"]
    tracker.bars.loc[tracker.bars.index[-1], "close"] = stop + (1.0 if rise else -1.0)


# ========================= a stock short is intraday ====================================
@pytest.mark.parametrize(
    "direction, stock_horizon, expected",
    [
        ("short", "swing", "intraday"),  # the fix: a short is never carried overnight
        ("short", "intraday", "intraday"),
        ("long", "swing", "swing"),  # a long still follows stock_horizon
        ("long", "intraday", "intraday"),
    ],
)
def test_a_stock_short_is_intraday_whatever_stock_horizon_says(
    make_rig, tmp_path, monkeypatch, direction, stock_horizon, expected
):
    clock = FakeClock(now_ist())
    rig = make_rig(clock=clock)
    notifier, _ = build_notifier(rig)
    db = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    try:
        desk = build_desk(db, notifier, monkeypatch=monkeypatch, stock_horizon=stock_horizon)

        sig = raise_signal(desk, clock(), direction=direction)

        assert sig["side"] == ("SELL" if direction == "short" else "BUY")
        assert sig["horizon"] == expected and sig["manage"]["horizon"] == expected
        assert ("n/a (swing" in sig["text"]) is (expected == "swing"), sig["text"]
    finally:
        db.dispose()


def test_a_swing_config_still_squares_a_short_off_at_the_exit_time_but_not_a_long(
    make_rig, tmp_path, monkeypatch
):
    clock = FakeClock(now_ist())
    rig = make_rig(clock=clock)
    notifier, _ = build_notifier(rig)
    db = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    try:
        desk = build_desk(db, notifier, monkeypatch=monkeypatch, stock_horizon="swing")
        short = raise_signal(desk, clock(), direction="short")
        short_pid = desk.acknowledge(short["id"], "TAKE")["position_id"]
        after_close = dt.datetime.combine(now_ist().date(), dt.time(15, 20), tzinfo=IST)

        desk._manage_positions(after_close, OpenClock().current_window(after_close))

        assert position(desk, short_pid)["exit_advice"] == "SQUARE_OFF"
        assert short_pid not in desk.positions  # OF-5: its one exit closed it
        long = raise_signal(desk, clock(), direction="long")
        long_pid = desk.acknowledge(long["id"], "TAKE")["position_id"]
        desk._manage_positions(after_close, OpenClock().current_window(after_close))
        assert position(desk, long_pid)["exit_advice"] != "SQUARE_OFF", "a swing long is carried"
    finally:
        db.dispose()


def test_the_phone_tells_the_owner_to_buy_back_a_stock_short_and_to_sell_a_long(
    make_rig, tmp_path, monkeypatch
):
    clock = FakeClock(now_ist())
    rig = make_rig(clock=clock)
    notifier, _ = build_notifier(rig)
    db = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    try:
        desk = build_desk(db, notifier, monkeypatch=monkeypatch)
        short = raise_signal(desk, clock(), direction="short")
        rig.drain()
        pid = desk.acknowledge(short["id"], "TAKE")["position_id"]
        fall_through_stop(desk, "SBIN", pid, rise=True)  # a short loses when the price rises
        clock.advance(60)

        desk._manage_positions(clock(), OpenClock().current_window(clock()))
        rig.drain()

        alert_text, exit_text = (visible(t).split("\n")[0] for t in rig.rec.texts())
        # a short in the (swing) desk config still reads "intraday short" on the phone
        assert alert_text == f"SELL SBIN {DOT} 1 share {DOT} intraday short"
        assert exit_text == f"EXIT: BUY SBIN {DOT} 1 share {DOT} cover short"
        assert position(desk, pid)["exit_advice"] == "STOP"
    finally:
        db.dispose()


def test_the_phone_tells_the_owner_to_sell_a_stock_long_when_it_exits(
    make_rig, tmp_path, monkeypatch
):
    clock = FakeClock(now_ist())
    rig = make_rig(clock=clock)
    notifier, _ = build_notifier(rig)
    db = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    try:
        desk = build_desk(db, notifier, monkeypatch=monkeypatch)
        sig = raise_signal(desk, clock())
        rig.drain()
        pid = desk.acknowledge(sig["id"], "TAKE")["position_id"]
        fall_through_stop(desk, "SBIN", pid)
        clock.advance(60)

        desk._manage_positions(clock(), OpenClock().current_window(clock()))
        rig.drain()

        assert visible(rig.rec.texts()[1]).split("\n")[0] == f"EXIT: SELL SBIN {DOT} 1 share"
    finally:
        db.dispose()


# ========================= the symbol the owner types into Dhan =========================
def test_a_symbol_with_an_ampersand_is_shown_as_the_trading_symbol_and_escaped_on_the_wire(
    make_rig, tmp_path, monkeypatch
):
    clock = FakeClock(now_ist())
    rig = make_rig(clock=clock)
    notifier, _ = build_notifier(rig)
    db = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    try:
        desk = build_desk(db, notifier, monkeypatch=monkeypatch, key="MM")
        sig = raise_signal(desk, clock(), key="MM")
        rig.drain()
        pid = desk.acknowledge(sig["id"], "TAKE")["position_id"]
        fall_through_stop(desk, "MM", pid)
        clock.advance(60)
        desk._manage_positions(clock(), OpenClock().current_window(clock()))
        rig.drain()

        assert sig["series_key"] == "MM" and sig["trading_symbol"] == "M&M"
        assert position(desk, pid)["trading_symbol"] == "M&M"
        alert, exit_ = rig.rec.texts()
        assert alert.split("\n")[0] == f"<b>BUY M&amp;M {DOT} 1 share {DOT} delivery</b>"
        assert exit_.split("\n")[0] == f"<b>EXIT: SELL M&amp;M {DOT} 1 share</b>"
        assert visible(alert).split("\n")[0] == f"BUY M&M {DOT} 1 share {DOT} delivery"
    finally:
        db.dispose()


def test_an_older_alert_without_trading_symbol_still_opens_a_position_that_falls_back(
    make_rig, tmp_path, monkeypatch
):
    clock = FakeClock(now_ist())
    rig = make_rig(clock=clock)
    notifier, _ = build_notifier(rig)
    db = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    try:
        desk = build_desk(db, notifier, monkeypatch=monkeypatch)
        sig = raise_signal(desk, clock())
        desk.signals[sig["id"]].pop("trading_symbol")  # an alert stored before US-004

        pid = desk.acknowledge(sig["id"], "TAKE")["position_id"]

        assert desk.positions[pid]["trading_symbol"] == "SBIN"
    finally:
        db.dispose()


# ========================= decision E: an intraday position from an earlier session =====
SESSION_OVER = "session over: record the broker square-off"


def open_position(desk: Any, clock: FakeClock, rig: Any, direction: str, key: str = "SBIN") -> str:
    sig = raise_signal(desk, clock(), key=key, direction=direction)
    rig.drain()
    pid = desk.acknowledge(sig["id"], "TAKE")["position_id"]
    fall_through_stop(desk, key, pid, rise=(direction == "short"))
    return pid


def next_morning(desk: Any, pid: str, days: int = 1) -> dt.datetime:
    """10:00 IST `days` after the IST date the position was opened on."""
    opened = dt.datetime.fromisoformat(desk.positions[pid]["opened_at"]).astimezone(IST)
    return dt.datetime.combine(opened.date() + dt.timedelta(days=days), dt.time(10, 0), tzinfo=IST)


def info_titles(notifier: Any) -> list[str]:
    return [i["title"] for i in notifier.dashboard.recent() if i["level"] == "INFO"]


def test_an_intraday_position_from_an_earlier_session_raises_no_exit_and_says_session_over(
    make_rig, tmp_path, monkeypatch
):
    clock = FakeClock(now_ist())
    rig = make_rig(clock=clock)
    notifier, _ = build_notifier(rig)
    db = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    try:
        desk = build_desk(db, notifier, monkeypatch=monkeypatch)
        pid = open_position(
            desk, clock, rig, "short"
        )  # a short is intraday: the broker squares it off
        morning = next_morning(desk, pid)
        calls_before = rig.rec.calls

        for minute in range(3):  # three polls the next morning, price through the stop
            moment = morning + dt.timedelta(minutes=minute)
            clock.set(moment)
            desk._manage_positions(moment, OpenClock().current_window(moment))
            rig.drain()

        position = desk.positions[pid]
        assert position["exit_advice"] is None, "no trading advice for a squared-off position"
        assert position["hold_check"] == SESSION_OVER
        assert [r for r in rig.rows() if r["kind"] == "exit"] == [], "no EXIT row, no reminders"
        assert rig.rec.calls == calls_before, "nothing reaches the phone"
        told = [t for t in info_titles(notifier) if t.startswith("Session over")]
        assert len(told) == 1, f"the owner is told once, not {len(told)} times"
    finally:
        db.dispose()


def test_the_session_over_note_is_not_repeated_after_a_restart(make_rig, tmp_path, monkeypatch):
    clock = FakeClock(now_ist())
    rig = make_rig(clock=clock)
    notifier, _ = build_notifier(rig)
    db = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    try:
        desk = build_desk(db, notifier, monkeypatch=monkeypatch)
        pid = open_position(desk, clock, rig, "short")
        morning = next_morning(desk, pid)
        desk._manage_positions(morning, OpenClock().current_window(morning))
        assert len([t for t in info_titles(notifier) if t.startswith("Session over")]) == 1

        restarted = build_desk(db, notifier, monkeypatch=monkeypatch)  # a new Desk, same file
        assert pid in restarted.positions
        restarted._manage_positions(morning, OpenClock().current_window(morning))

        assert len([t for t in info_titles(notifier) if t.startswith("Session over")]) == 1
        assert restarted.positions[pid]["exit_advice"] is None
    finally:
        db.dispose()


def test_a_swing_position_from_an_earlier_session_is_managed_as_before(
    make_rig, tmp_path, monkeypatch
):
    clock = FakeClock(now_ist())
    rig = make_rig(clock=clock)
    notifier, _ = build_notifier(rig)
    db = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    try:
        desk = build_desk(db, notifier, monkeypatch=monkeypatch, stock_horizon="swing")
        pid = open_position(desk, clock, rig, "long")  # a swing long is carried overnight
        assert desk.positions[pid]["horizon"] == "swing"
        morning = next_morning(desk, pid)
        clock.set(morning)

        desk._manage_positions(morning, OpenClock().current_window(morning))
        rig.drain()

        assert position(desk, pid)["exit_advice"] == "STOP"
        assert [r["kind"] for r in rig.rows() if r["kind"] == "exit"] == ["exit"]
        assert "session_over" not in position(desk, pid)
        assert not [t for t in info_titles(notifier) if t.startswith("Session over")]
    finally:
        db.dispose()


@pytest.mark.parametrize(
    "days, advice_expected",
    [(0, True), (1, False), (3, False)],
)
def test_the_session_over_rule_starts_with_the_next_ist_date(
    make_rig, tmp_path, monkeypatch, days, advice_expected
):
    clock = FakeClock(now_ist())
    rig = make_rig(clock=clock)
    notifier, _ = build_notifier(rig)
    db = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    try:
        desk = build_desk(db, notifier, monkeypatch=monkeypatch)
        pid = open_position(desk, clock, rig, "short")
        opened = dt.datetime.fromisoformat(desk.positions[pid]["opened_at"]).astimezone(IST)
        # 23:59:59 on the opening date is still that session's date; 00:00:00 is the next one
        moment = (
            dt.datetime.combine(opened.date(), dt.time(23, 59, 59), tzinfo=IST)
            if days == 0
            else dt.datetime.combine(
                opened.date() + dt.timedelta(days=days), dt.time(0, 0, 0), tzinfo=IST
            )
        )
        clock.set(moment)

        desk._manage_positions(moment, OpenClock().current_window(moment))

        assert (position(desk, pid)["exit_advice"] == "STOP") is advice_expected
    finally:
        db.dispose()
