"""US-004 review round 1, the channel side: CRITICAL repeats (decision F) and the EXIT episode
memory (decision G).

(F) the same CRITICAL title within one 30-minute bucket reaches the phone once: its dedup key
is the title plus the bucket, not the notification id (this replaces decision 3's "two
same-title CRITICALs both arrive"; a repeat in a later bucket still arrives).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from onelot.clock import IST
from onelot.config import NotifyCfg
from onelot.notify.base import Notification, NotificationLevel
from onelot.notify.manager import Notifier, dedup_key_for
from onelot.notify.telegram_channel import TelegramChannel

from .test_telegram_outbox import T0, Rig, ok

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


# ========================= (F) the CRITICAL key: title + 30-minute bucket ================
def at(hh: int, mm: int, ss: int = 0) -> dt.datetime:
    return dt.datetime(2026, 11, 2, hh, mm, ss, tzinfo=IST)


@pytest.mark.parametrize(
    ("first", "second", "same"),
    [
        (at(10, 0), at(10, 29, 59), True),
        (at(10, 30), at(10, 59, 59), True),
        (at(10, 29, 59), at(10, 30), False),  # a bucket edge: a new bucket, sent again
        (at(10, 0), at(11, 0), False),
    ],
)
def test_a_critical_key_is_its_title_and_a_thirty_minute_bucket(first, second, same):
    a, b = critical("Feed down", first), critical("Feed down", second)

    assert (dedup_key_for(a) == dedup_key_for(b)) is same
    assert dedup_key_for(a).startswith("critical:")


def test_the_bucket_is_the_same_instant_in_any_zone():
    utc = at(10, 5).astimezone(dt.timezone.utc)
    assert dedup_key_for(critical("Feed down", utc)) == dedup_key_for(
        critical("Feed down", at(10, 5))
    )


def test_different_titles_never_share_a_key_and_spacing_is_not_a_difference():
    base = dedup_key_for(critical("Feed down", at(10, 0)))

    assert dedup_key_for(critical("Feed  down ", at(10, 1))) == base
    assert dedup_key_for(critical("Feed up", at(10, 1))) != base
    assert dedup_key_for(critical("feed down", at(10, 1))) != base, "case is kept"


def test_a_long_title_still_gives_a_short_key():
    key = dedup_key_for(critical("x" * 5000, at(10, 0)))
    assert len(key) <= 64


def test_a_same_title_critical_within_the_bucket_reaches_the_phone_once(make_rig):
    rig = make_rig()
    notifier = Notifier(SimpleNamespace(notify=NotifyCfg(os_toast=False)), dedup_seconds=0.0)
    notifier.add_channel(
        TelegramChannel(rig.client, rig.outbox, NotifyCfg(), lambda m: True, clock=rig.clock)
    )

    first = notifier.send(critical("UNMANAGED position", T0 + dt.timedelta(minutes=1)))
    repeat = notifier.send(critical("UNMANAGED position", T0 + dt.timedelta(minutes=9)))
    later = notifier.send(critical("UNMANAGED position", T0 + dt.timedelta(minutes=31)))
    rig.drain()

    assert first["telegram"] is repeat["telegram"] is later["telegram"] is True
    assert len(rig.requests) == 2, "the 10:09 repeat is the same alarm; 10:31 is a new bucket"
    assert len(notifier.dashboard.recent()) == 3, "the dashboard still shows every one"


def test_the_channel_uses_the_same_key_without_the_notifier(make_rig):
    rig = make_rig()
    channel = TelegramChannel(rig.client, rig.outbox, NotifyCfg(), lambda m: True, clock=rig.clock)

    channel.send(critical("Feed down", T0))
    channel.send(critical("Feed down", T0 + dt.timedelta(minutes=5)))

    assert len(rig.rows()) == 1
