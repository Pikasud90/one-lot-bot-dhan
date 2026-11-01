"""US-004 AC5: an EXIT advice that persists is not a message per poll.

The Desk re-evaluates every open position on every poll and calls Notifier.send(EXIT) each
time the advice still holds (see Desk._manage_positions). The phone must get the FIRST EXIT at
once and then at most 3 reminders, 5 minutes apart, for as long as the Desk keeps raising it;
when the position is closed the Desk stops raising it and the phone goes quiet.

Each "poll" below does exactly what the Desk does: Notifier.send(EXIT notification with
payload {"advice", "position"}) with the position's current P&L. The Notifier's own 20 s window
is off (see us004_helpers.build_notifier) so that the channel's behaviour is what is measured.
Poll cadences divide 300 s so "5 minutes after the previous message" and "5 minutes steps from
the first" give the same schedule (an ambiguity noted in the report).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from us003_helpers import install_network_guard, telegram, visible
from us004_helpers import (
    build_notifier,
    exit_notification,
    exit_payload,
    rig_factory,
)


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"only the injected transport may be used: {attempts}"


@pytest.fixture
def make_rig(tmp_path):
    with rig_factory(tmp_path) as build:
        yield build


def pnl(poll_index: int) -> float:
    """Distinct and comma-grouped (+1,000 ...) so a stale payload is visible in the text."""
    return 1000.0 + poll_index


def run_polls(
    rig: Any,
    notifier: Any,
    pid: str,
    offsets: list[float],
    origin: dt.datetime | None = None,
    advice: str = "STOP",
) -> dict[float, dict[str, Any]]:
    """Raise the EXIT advice for `pid` at each offset (seconds after `origin`), draining the
    outbox after every poll. Returns the event raised at each offset."""
    origin = origin or rig.clock()
    raised: dict[float, dict[str, Any]] = {}
    for k, offset in enumerate(offsets):
        rig.clock.set(origin + dt.timedelta(seconds=offset))
        notifier.send(
            exit_notification(pid, pnl(k), rig.clock(), advice),
            dedup_key=f"desk:exit:{pid}:{advice}",  # the key Desk passes
        )
        raised[offset] = exit_payload(pid, pnl(k), advice)
        rig.drain()
    return raised


@pytest.mark.parametrize(
    "every_s, total_s",
    [(1, 1000), (5, 3600), (20, 3600), (30, 3600), (60, 3600)],
)
def test_the_phone_gets_the_first_exit_then_three_reminders_five_minutes_apart_and_no_more(
    make_rig, every_s, total_s
):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    t0 = rig.clock()

    run_polls(rig, notifier, "pos000000001", list(range(0, total_s + 1, every_s)), t0)

    assert rig.rec.offsets(t0) == [0.0, 300.0, 600.0, 900.0], (
        f"polling every {every_s} s for {total_s} s must give 1 EXIT + 3 reminders"
    )


def test_the_boundaries_of_the_five_minute_interval(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    t0 = rig.clock()

    run_polls(
        rig,
        notifier,
        "pos000000001",
        [0, 150, 299, 300, 301, 598, 599, 600, 899, 900, 901, 1499, 1500, 3600],
        t0,
    )

    assert rig.rec.offsets(t0) == [0.0, 300.0, 600.0, 900.0]


def test_the_first_exit_is_the_plain_formatted_exit_sent_immediately(make_rig):
    tg = telegram()
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    t0 = rig.clock()

    raised = run_polls(rig, notifier, "pos000000001", [0], t0)

    assert rig.rec.texts() == [tg.format_exit(raised[0], t0)]
    assert visible(rig.rec.texts()[0]).startswith("EXIT ")


def test_every_reminder_carries_the_position_as_of_the_poll_that_triggered_it(make_rig):
    tg = telegram()
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    t0 = rig.clock()

    raised = run_polls(rig, notifier, "pos000000001", list(range(0, 1201, 60)), t0)

    for text, at in zip(rig.rec.texts(), rig.rec.times, strict=True):
        event = raised[(at - t0).total_seconds()]
        assert tg.format_exit(event, at) in text, (
            "a reminder must show the current P&L, not a stale one"
        )
    assert len(rig.rec.texts()) == 4


def test_when_the_desk_stops_raising_the_advice_the_phone_goes_quiet(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    t0 = rig.clock()

    run_polls(rig, notifier, "pos000000001", list(range(0, 421, 60)), t0)  # closed at 7 minutes
    rig.clock.advance(2 * 3600)
    rig.drain()
    rig.clock.advance(2 * 3600)
    rig.drain()

    assert rig.rec.offsets(t0) == [0.0, 300.0]


def test_two_open_positions_each_get_their_own_first_exit_and_reminders(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    t0 = rig.clock()
    for second in range(0, 1201, 30):
        rig.clock.set(t0 + dt.timedelta(seconds=second))
        notifier.send(exit_notification("posAAAAAAAAA", pnl(second), rig.clock()))
        if second >= 30:  # the second position's advice starts 30 s later
            notifier.send(exit_notification("posBBBBBBBBB", pnl(second), rig.clock()))
        rig.drain()

    # A: 0, 300, 600, 900.  B: the first poll at 30 s, then 330, 630, 930.
    assert rig.rec.offsets(t0) == [0.0, 30.0, 300.0, 330.0, 600.0, 630.0, 900.0, 930.0]


def test_a_different_position_is_not_silenced_by_the_first_ones_reminder_budget(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    t0 = rig.clock()
    run_polls(rig, notifier, "posAAAAAAAAA", list(range(0, 1801, 60)), t0)  # A used up its 4
    assert len(rig.rec.texts()) == 4

    run_polls(rig, notifier, "posBBBBBBBBB", [0], t0 + dt.timedelta(seconds=1800))

    assert len(rig.rec.texts()) == 5, "B's first EXIT must go out at once"


def test_a_change_of_advice_for_the_same_position_starts_a_fresh_reminder_budget(make_rig):
    """Decided: STOP at 0 and 60 s, then the advice becomes TARGET. The new advice is a first
    message at once (120 s) and has its own 3 reminders (420, 720, 1020 s)."""
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    t0 = rig.clock()
    pid = "pos000000001"
    for k in range(0, 61):  # one poll a minute for an hour
        advice = "STOP" if k < 2 else "TARGET"
        rig.clock.set(t0 + dt.timedelta(minutes=k))
        notifier.send(
            exit_notification(pid, pnl(k), rig.clock(), advice),
            dedup_key=f"desk:exit:{pid}:{advice}",
        )
        rig.drain()

    assert rig.rec.offsets(t0) == [0.0, 120.0, 420.0, 720.0, 1020.0]
    plain = [visible(t) for t in rig.rec.texts()]
    assert "STOP" in plain[0] and all("TARGET" in p for p in plain[1:])


def test_exit_rows_are_durable_and_each_message_is_its_own_row(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)

    run_polls(rig, notifier, "pos000000001", list(range(0, 1201, 60)), rig.clock())

    exits = [r for r in rig.rows() if r["kind"] == "exit"]
    assert len(exits) == 4 and all(r["status"] == "sent" for r in exits)
    assert len({(r["alert_id"], r["kind"]) for r in exits}) == 4, "one row per (alert_id, kind)"
    assert len({r["dedup_key"] for r in exits}) == 4
