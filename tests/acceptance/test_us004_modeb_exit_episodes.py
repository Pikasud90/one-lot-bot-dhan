"""US-004 mode B: EXIT episodes across restarts, advice changes and the 60-minute gap.

The channel keeps the reminder budget in memory (a restart starts a fresh one, accepted) but
the numbering of EXIT rows ('{position_id}#n', one row per (alert_id, kind)) must continue
from the table: a restarted channel that began at #1 again would have its first EXIT swallowed
as a duplicate of an old row, and the phone would never hear it.

A new episode starts when the advice changes, or the same advice returns after 60 minutes
without being raised (measured from the last poll that raised it; a poll that raises it every
minute never lets an episode end).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from us003_helpers import install_network_guard, visible
from us004_helpers import (
    SessionProbe,
    build_notifier,
    exit_notification,
    rig_factory,
)

from onelot.config import NotifyCfg
from onelot.notify.telegram_channel import TelegramChannel

PID = "pos000000001"


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"only the injected transport may be used: {attempts}"


@pytest.fixture
def make_rig(tmp_path):
    with rig_factory(tmp_path) as build:
        yield build


def raise_exit(rig: Any, notifier: Any, offset_s: float, origin: dt.datetime, advice: str = "STOP"):
    rig.clock.set(origin + dt.timedelta(seconds=offset_s))
    result = notifier.send(
        exit_notification(PID, 1000.0 + offset_s, rig.clock(), advice),
        dedup_key=f"desk:exit:{PID}:{advice}",
    )
    rig.drain()
    return result


def exit_ids(rig: Any) -> list[str]:
    return [r["alert_id"] for r in rig.rows() if r["kind"] == "exit"]


# ========================= restarts =====================================================
def test_a_restarted_channel_continues_the_exit_numbering_and_its_first_message_is_heard(
    make_rig,
):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    t0 = rig.clock()
    raise_exit(rig, notifier, 0, t0)
    raise_exit(rig, notifier, 300, t0)
    assert exit_ids(rig) == [f"{PID}#1", f"{PID}#2"]

    notifier2, _ = build_notifier(rig)  # the process restarted: a new channel, same table
    raise_exit(rig, notifier2, 360, t0)

    assert exit_ids(rig) == [f"{PID}#1", f"{PID}#2", f"{PID}#3"]
    assert len(rig.rec.texts()) == 3, "the first EXIT after a restart must reach the phone"


def test_a_restarted_channel_on_a_second_outbox_object_continues_the_numbering_too(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    t0 = rig.clock()
    raise_exit(rig, notifier, 0, t0)
    other, rec2 = rig.another_outbox()
    channel = TelegramChannel(
        rig.client, other, NotifyCfg(os_toast=False), SessionProbe(True), clock=rig.clock
    )
    rig.clock.set(t0 + dt.timedelta(seconds=90))

    assert channel.send(_notifier_ready(exit_notification(PID, 5.0, rig.clock()))) is True
    rig.drain(other)

    assert exit_ids(rig) == [f"{PID}#1", f"{PID}#2"] and rec2.calls == 1


def _notifier_ready(n: Any) -> Any:
    """What Notifier.send does before a channel sees a notification (kind and dedup_key)."""
    from onelot.notify.manager import dedup_key_for

    n.kind = n.level.value.lower()
    n.dedup_key = dedup_key_for(n)
    return n


# ========================= the 60-minute gap ============================================
def exhaust_the_budget(rig: Any, notifier: Any, origin: dt.datetime) -> None:
    for minute in range(16):  # raised every minute: 4 messages, then nothing due
        raise_exit(rig, notifier, minute * 60, origin)
    assert rig.rec.offsets(origin) == [0.0, 300.0, 600.0, 900.0]


@pytest.mark.parametrize(
    "gap_s, fresh",
    [(3599, False), (3600, True), (3601, True), (7200, True), (1, False), (1799, False)],
)
def test_the_same_advice_back_after_an_hour_without_it_is_a_new_episode_exactly_at_60_minutes(
    make_rig, gap_s, fresh
):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    t0 = rig.clock()
    exhaust_the_budget(rig, notifier, t0)
    last_raised = 900  # the last poll that raised it

    result = raise_exit(rig, notifier, last_raised + gap_s, t0)

    assert result.get("telegram") is True, "a reminder that is not due is success, not failure"
    assert len(rig.rec.texts()) == (5 if fresh else 4), f"gap {gap_s} s, fresh={fresh}"


def test_a_new_episode_has_its_own_three_reminders(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    t0 = rig.clock()
    exhaust_the_budget(rig, notifier, t0)
    back = 900 + 3600

    for minute in range(0, 31):
        raise_exit(rig, notifier, back + minute * 60, t0)

    offsets = rig.rec.offsets(t0)
    assert offsets == [0.0, 300.0, 600.0, 900.0] + [back + s for s in (0.0, 300.0, 600.0, 900.0)]
    assert len(set(exit_ids(rig))) == 8, "eight messages, eight rows, no collisions"


def test_while_the_advice_keeps_being_raised_the_budget_is_never_renewed(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    t0 = rig.clock()

    for minute in range(0, 3 * 60 + 1):  # three hours, raised every minute
        raise_exit(rig, notifier, minute * 60, t0)

    assert rig.rec.offsets(t0) == [0.0, 300.0, 600.0, 900.0]


# ========================= a changing advice ============================================
def test_each_change_of_advice_is_a_fresh_first_message_with_its_own_reminders(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    t0 = rig.clock()
    plan = {0: "STOP", 1: "STOP", 2: "TARGET", 3: "TARGET", 4: "STOP", 5: "STOP"}

    for minute in range(0, 26):
        raise_exit(rig, notifier, minute * 60, t0, plan.get(minute, "STOP"))

    assert rig.rec.offsets(t0) == [0.0, 120.0, 240.0, 540.0, 840.0, 1140.0]
    reasons = [visible(t).split("\n")[1] for t in rig.rec.texts()]
    assert reasons == ["Reason: STOP", "Reason: TARGET"] + ["Reason: STOP"] * 4
