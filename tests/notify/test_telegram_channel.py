"""US-004 review round 1, the channel side: CRITICAL repeats (decision F) and the EXIT episode
memory (decision G).

(F, refined) a CRITICAL whose title matches one SENT to the phone less than 30 minutes ago is
not sent again: a sliding window from the last sent one, decided by the worker when the row
comes up (send() stays one INSERT, the dashboard still shows every one). This replaces decision
3's "two same-title CRITICALs both arrive"; a repeat 30 minutes or more after the last sent
one arrives.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from onelot.config import NotifyCfg
from onelot.notify.base import Notification, NotificationLevel
from onelot.notify.manager import Notifier
from onelot.notify.telegram_channel import TelegramChannel

from .test_telegram_outbox import T0, Rig, error, ok

L = NotificationLevel


@pytest.fixture
def make_rig(tmp_path: Path) -> Iterator[Callable[..., Rig]]:
    made: list[Rig] = []

    def build(respond: Callable[[httpx.Request], httpx.Response] = lambda r: ok()) -> Rig:
        rig = Rig(tmp_path / f"c{len(made)}.db", respond)
        made.append(rig)
        return rig

    yield build
    for rig in made:
        rig.outbox.stop()
        rig.db.dispose()


def critical(title: str, at: dt.datetime, body: str = "") -> Notification:
    return Notification(title=title, body=body, level=L.CRITICAL, created_at=at)


def channel_of(rig: Rig) -> TelegramChannel:
    return TelegramChannel(rig.client, rig.outbox, NotifyCfg(), lambda m: True, clock=rig.clock)


def raise_at(rig: Rig, channel: TelegramChannel, minute: float, title: str = "Feed down") -> None:
    """Raise the CRITICAL at T0 + `minute` and let the worker drain."""
    rig.clock.now = T0 + dt.timedelta(minutes=minute)
    channel.send(critical(title, rig.clock()))
    rig.drain()


def sent_minutes(rig: Rig) -> list[float]:
    return [
        (r["sent_at"] - T0).total_seconds() / 60
        for r in rig.rows()
        if r["kind"] == "critical" and r["status"] == "sent"
    ]


# ========================= (F) a sliding 30 minutes from the last SENT one ===============
@pytest.mark.parametrize(("again", "sent"), [(29.99, [0.0]), (30, [0.0, 30.0])])
def test_a_same_title_critical_within_30_minutes_of_the_last_sent_one_is_skipped(
    make_rig, again, sent
):
    rig = make_rig()
    channel = channel_of(rig)

    raise_at(rig, channel, 0)
    raise_at(rig, channel, again)

    assert sent_minutes(rig) == sent
    if len(sent) == 1:
        assert rig.rows()[-1]["status"] == "skipped"
        assert rig.rows()[-1]["last_error"] == "repeat within 30 minutes"


def test_the_window_slides_from_the_last_sent_one_not_from_the_last_raised(make_rig):
    """A legacy alarm raised every minute for two hours reaches the phone every 30 minutes:
    each repeat is measured from the last one that was SENT."""
    rig = make_rig()
    channel = channel_of(rig)

    for minute in range(121):
        raise_at(rig, channel, minute, "UNMANAGED position")

    assert sent_minutes(rig) == [0.0, 30.0, 60.0, 90.0, 120.0]


def test_another_title_is_another_alarm_and_spacing_is_not_a_difference(make_rig):
    rig = make_rig()
    channel = channel_of(rig)

    raise_at(rig, channel, 0, "Feed down")
    raise_at(rig, channel, 1, "Feed  down ")  # the same title
    raise_at(rig, channel, 2, "feed down")  # case is kept: another title
    raise_at(rig, channel, 3, "Clock problem")

    assert sent_minutes(rig) == [0.0, 2.0, 3.0]


def test_a_critical_that_failed_does_not_silence_the_next_one(make_rig):
    replies = iter([error(400, "Bad Request: chat not found"), ok()])
    rig = make_rig(lambda r: next(replies))
    channel = channel_of(rig)

    raise_at(rig, channel, 0)
    raise_at(rig, channel, 5)

    assert [r["status"] for r in rig.rows()] == ["failed", "sent"]


def test_an_outage_delivers_the_repeated_alarm_once(make_rig):
    down = [True]

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502) if down[0] else ok()

    rig = make_rig(respond)
    channel = channel_of(rig)
    for minute in range(60):  # raised every minute while Telegram is unreachable
        rig.clock.now = T0 + dt.timedelta(minutes=minute)
        channel.send(critical("UNMANAGED position", rig.clock()))
        rig.outbox.drain_once()  # one pass a minute: every attempt fails
    down[0] = False
    rig.clock.now = T0 + dt.timedelta(minutes=61)

    rig.drain()

    statuses = [r["status"] for r in rig.rows()]
    assert statuses.count("sent") == 1 and statuses.count("skipped") == 59
    text = json.loads(rig.requests[-1].read())["text"]
    assert "Time 10:00:00 IST" in text and text.endswith(" · sent late 11:01"), text


def test_the_dashboard_still_shows_every_critical(make_rig):
    rig = make_rig()
    notifier = Notifier(SimpleNamespace(notify=NotifyCfg(os_toast=False)), dedup_seconds=0.0)
    notifier.add_channel(channel_of(rig))

    for minute in (1, 9, 31):
        rig.clock.now = T0 + dt.timedelta(minutes=minute)
        assert notifier.send(critical("UNMANAGED position", rig.clock()))["telegram"] is True
        rig.drain()

    assert len(rig.requests) == 2, "10:09 repeats the 10:01 alarm; 10:31 is 30 min after it"
    assert len(notifier.dashboard.recent()) == 3


def test_each_critical_is_its_own_row_with_the_notification_id_in_its_key():
    from onelot.notify.manager import dedup_key_for

    a = critical("Feed down", T0)
    b = critical("Feed down", T0)
    assert dedup_key_for(a) == f"critical:{a.notification_id}" != dedup_key_for(b)


# ========================= (G) the EXIT episode memory is pruned =========================
def exit_for(pid: str, at: dt.datetime) -> Notification:
    return Notification(
        title="EXIT",
        body="",
        level=L.EXIT,
        position_id=pid,
        created_at=at,
        payload={"advice": "STOP", "position": {"series_key": "SBIN", "kind": "STOCK"}},
    )


def test_episodes_older_than_the_gap_are_forgotten(make_rig):
    """A closed position is never raised again: its episode must not stay in memory for the
    life of the process. Anything not raised for the episode gap (60 min) would start a fresh
    budget anyway, so it is dropped."""
    rig = make_rig()
    channel = TelegramChannel(rig.client, rig.outbox, NotifyCfg(), lambda m: True, clock=rig.clock)
    for i in range(50):
        channel.send(exit_for(f"p{i}", T0))
    assert len(channel._episodes) == 50

    rig.clock.advance(59 * 60)
    channel.send(exit_for("p0", rig.clock()))  # still raised: kept
    rig.clock.advance(60)
    channel.send(exit_for("new", rig.clock()))

    assert sorted(channel._episodes) == ["new", "p0"]


def test_a_pruned_position_raised_again_starts_a_fresh_budget(make_rig):
    rig = make_rig()
    channel = TelegramChannel(rig.client, rig.outbox, NotifyCfg(), lambda m: True, clock=rig.clock)
    for minute in range(31):
        rig.clock.now = T0 + dt.timedelta(minutes=minute)
        channel.send(exit_for("p1", rig.clock()))
    assert len(rig.rows()) == 4

    rig.clock.now = T0 + dt.timedelta(minutes=90)
    channel.send(exit_for("other", rig.clock()))  # prunes p1
    channel.send(exit_for("p1", rig.clock()))

    assert [r["alert_id"] for r in rig.rows()][-1] == "p1#5"
