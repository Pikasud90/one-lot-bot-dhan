"""US-004 review round 1: what the phone gets after an outage, and what the SLO measures.

Manager decisions (A)-(D): EXIT and CRITICAL texts carry the time of the event, flagged when
they are sent late; a newer EXIT for a position supersedes its older unsent ones; missed notes
that pile up are folded into one digest; p95 counts alert, exit and critical rows only.

Same rig as test_telegram_outbox: a file-based tmp SQLite, a fake clock whose sleep advances
it, httpx.MockTransport. No network, no real sleeping.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from onelot.notify.telegram import SEP, format_alert, format_exit
from onelot.notify.telegram_outbox import format_critical

from .test_telegram_outbox import T0, Rig, ok


@pytest.fixture
def make_rig(tmp_path: Path) -> Iterator[Callable[..., Rig]]:
    made: list[Rig] = []

    def build(respond: Callable[[httpx.Request], httpx.Response] = lambda r: ok()) -> Rig:
        rig = Rig(tmp_path / f"r{len(made)}.db", respond)
        made.append(rig)
        return rig

    yield build
    for rig in made:
        rig.outbox.stop()
        rig.db.dispose()


def enqueue_note(rig: Rig, key: str, **kw: Any) -> None:
    """A worker-style note ('test' kind): its latency is ~0 when drained at once."""
    rig.outbox.enqueue(
        dedup_key=key,
        kind="test",
        alert_id=None,
        payload={"title": key},
        alert_created_at=rig.clock(),
        expires_at=None,
        **kw,
    )


# ========================= (D) the SLO counts the alerts, not the notes ===================
def test_p95_counts_alert_exit_and_critical_rows_only(make_rig):
    rig = make_rig()
    rig.enqueue(rig.alert(1))
    rig.clock.advance(4)
    rig.drain()  # the alert: 4000 ms
    for i in range(20):  # twenty instant notes would pull the p95 down to 0
        rig.clock.advance(2)
        enqueue_note(rig, f"note{i}")
        rig.drain()

    status = rig.outbox.status()

    assert status["p95_latency_ms"] == 4000
    assert status["last_ok_at"] == rig.clock().isoformat(), "any confirmed send is 'last ok'"
    assert status["sent"] == 21


def test_p95_is_none_while_only_notes_were_sent(make_rig):
    rig = make_rig()
    enqueue_note(rig, "only")
    rig.drain()

    status = rig.outbox.status()

    assert status["p95_latency_ms"] is None and status["last_ok_at"] == T0.isoformat()


# ========================= (A) EXIT and CRITICAL show when the event happened =============
def exit_event(unrealized: float = 150.0) -> dict[str, Any]:
    position = {"series_key": "SBIN", "kind": "STOCK", "side": "BUY", "unrealized": unrealized}
    return {"advice": "STOP", "position": position}


def enqueue_event(rig: Rig, kind: str, payload: dict[str, Any], key: str = "p1#1") -> None:
    rig.outbox.enqueue(
        dedup_key=f"{kind}:{key}",
        kind=kind,  # type: ignore[arg-type]
        alert_id=key,
        payload=payload,
        alert_created_at=rig.clock(),
        expires_at=None,
    )


@pytest.mark.parametrize(
    ("kind", "payload", "formatter"),
    [
        ("exit", exit_event(), format_exit),
        ("critical", {"title": "Feed down", "body": "no ticks"}, format_critical),
    ],
    ids=["exit", "critical"],
)
@pytest.mark.parametrize(
    ("delay_s", "late"), [(0, None), (60, None), (61, "10:01"), (3 * 3600, "13:00")]
)
def test_an_event_message_carries_the_event_time_and_says_when_it_is_sent_late(
    make_rig, kind, payload, formatter, delay_s, late
):
    rig = make_rig()
    enqueue_event(rig, kind, payload)
    rig.clock.advance(delay_s)

    rig.drain()

    (request,) = rig.requests
    text = json.loads(request.read())["text"]
    expected = formatter(payload, T0)  # Time 10:00:00 IST: the event, not the delivery
    assert text == (expected if late is None else f"{expected}{SEP}sent late {late}")


def test_a_signal_alert_is_still_formatted_at_send_time(make_rig):
    """The alert's 'time left' must be current: (A) is for EXIT and CRITICAL only."""
    rig = make_rig()
    alert = rig.alert(minutes=10)
    rig.enqueue(alert)
    rig.clock.advance(120)

    rig.drain()

    assert json.loads(rig.requests[0].read())["text"] == format_alert(alert, T0 + _2_MIN)


_2_MIN = dt.timedelta(minutes=2)


# ========================= (B) a newer EXIT supersedes the older unsent ones ==============
class Switch:
    """A transport that answers 502 (transient) while down, then 200."""

    def __init__(self) -> None:
        self.down = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(502, json={"ok": False, "description": "Bad Gateway"})
        return ok()


def exit_rows(rig: Rig) -> list[tuple[str, str, str | None]]:
    return [
        (r["alert_id"], r["status"], r["last_error"]) for r in rig.rows() if r["kind"] == "exit"
    ]


def test_a_newer_exit_for_a_position_supersedes_its_older_unsent_ones(make_rig):
    switch = Switch()
    rig = make_rig(switch)
    enqueue_event(rig, "exit", exit_event(100.0), key="p1#1")
    rig.drain()  # fails: p1#1 waits for its retry
    rig.clock.advance(300)
    enqueue_event(rig, "exit", exit_event(-50.0), key="p1#2")

    rig.outbox.drain_once()

    assert exit_rows(rig)[0] == ("p1#1", "skipped", "superseded")
    switch.down = False
    rig.clock.advance(60)
    rig.drain()
    assert [s for _, s, _ in exit_rows(rig)] == ["skipped", "sent"]
    texts = [json.loads(r.read())["text"] for r in rig.requests]
    assert ["+100" in t for t in texts] == [True, False, False], "p1#1 is never tried again"
    assert "-50" in texts[-1]


def test_each_position_keeps_its_own_newest_exit(make_rig):
    rig = make_rig(Switch())
    for key in ("p1#1", "p2#1", "p1#2", "p2#2", "p3#1"):
        enqueue_event(rig, "exit", exit_event(), key=key)
        rig.clock.advance(1)

    rig.outbox.drain_once()

    status = {alert_id: s for alert_id, s, _ in exit_rows(rig)}
    assert status["p1#1"] == status["p2#1"] == "skipped"
    assert {status["p1#2"], status["p2#2"], status["p3#1"]} <= {"queued", "sending"}


def test_an_outage_gives_the_phone_one_exit_not_a_burst(make_rig):
    """The Desk raises STOP every minute for 30 minutes while Telegram is down: the channel
    queues the first EXIT and 3 reminders, and after recovery the phone gets one message,
    the newest, stamped with its own event time and flagged late."""
    from onelot.config import NotifyCfg
    from onelot.notify.base import Notification, NotificationLevel
    from onelot.notify.telegram_channel import TelegramChannel

    switch = Switch()
    rig = make_rig(switch)
    channel = TelegramChannel(
        rig.client, rig.outbox, NotifyCfg(), lambda moment: True, clock=rig.clock
    )
    for minute in range(31):
        rig.clock.now = T0 + dt.timedelta(minutes=minute)
        channel.send(
            Notification(
                title="EXIT",
                body="",
                level=NotificationLevel.EXIT,
                position_id="p1",
                created_at=rig.clock(),
                payload=exit_event(float(minute)),
                dedup_key="exit:p1",
            )
        )
        rig.outbox.drain_once()
    switch.down = False
    rig.clock.now = T0 + dt.timedelta(minutes=40)

    rig.drain()

    sent = [r for r in rig.rows() if r["status"] == "sent"]
    assert [r["alert_id"] for r in sent] == ["p1#4"]
    assert [s for _, s, _ in exit_rows(rig)] == ["skipped", "skipped", "skipped", "sent"]
    text = json.loads(rig.requests[-1].read())["text"]
    assert "Time 10:15:00 IST" in text and text.endswith(f"{SEP}sent late 10:40")


# ========================= (C) missed notes that pile up become one digest ================
def expire_one_by_one(rig: Rig, count: int) -> None:
    """`count` alerts, the i-th valid for i minutes, the worker draining once a minute while
    Telegram is down: each pass expires exactly one, as during a real outage."""
    for i in range(1, count + 1):
        rig.enqueue(rig.alert(i, minutes=i))
    for minute in range(1, count + 1):
        rig.clock.now = T0 + dt.timedelta(minutes=minute)
        rig.outbox.drain_once()


def queued_kinds(rig: Rig) -> list[str]:
    return [r["kind"] for r in rig.rows() if r["status"] in ("queued", "sending")]


def test_up_to_eight_missed_notes_stay_individual(make_rig):
    rig = make_rig(Switch())

    expire_one_by_one(rig, 8)

    assert queued_kinds(rig) == ["missed"] * 8


def test_a_ninth_missed_note_folds_them_all_into_one_digest(make_rig):
    switch = Switch()
    rig = make_rig(switch)
    expire_one_by_one(rig, 9)

    assert queued_kinds(rig) == ["digest"]
    switch.down = False
    rig.clock.advance(120)
    rig.drain()
    text = json.loads(rig.requests[-1].read())["text"]
    assert text.startswith("<b>Missed 9 alerts</b>")
    assert [r["last_error"] for r in rig.rows() if r["kind"] == "missed"] == [
        "folded into a digest"
    ] * 9


def test_a_two_hour_outage_gives_one_digest_and_the_live_alerts_not_a_note_each(make_rig):
    """The worker drains every second through a 2 h transport outage, one alert every 5
    minutes (each valid 10 minutes). After recovery: the alert still live and ONE digest of
    every alert that expired, never 23 separate notes (whichever is due first goes first:
    the rows are in their retry back-off, at most 60 s)."""
    switch = Switch()
    rig = make_rig(switch)
    outage = 2 * 3600
    for second in range(outage):
        rig.clock.now = max(rig.clock.now, T0 + dt.timedelta(seconds=second))
        if second % 300 == 0:
            rig.enqueue(rig.alert(second // 300 + 1, minutes=10))
        rig.outbox.drain_once()
    switch.down = False
    rig.clock.now = max(rig.clock.now, T0 + dt.timedelta(seconds=outage))
    tried_during_outage = len(rig.requests)

    for _ in range(120):  # longer than the longest back-off
        rig.clock.advance(1)
        rig.outbox.drain_once()

    after = [json.loads(r.read())["text"] for r in rig.requests[tried_during_outage:]]
    live = [r for r in rig.rows() if r["kind"] == "alert" and r["status"] == "sent"]
    expired = [r for r in rig.rows() if r["kind"] == "alert" and r["status"] == "skipped"]
    assert len(live) == 1 and len(expired) == 23
    assert len(after) == 2, after
    assert sorted(t.split("</b>")[0] for t in after) == [
        "<b>BUY SBIN · 1 share",
        "<b>Missed 23 alerts",
    ]
    assert queued_kinds(rig) == []


def test_the_missed_digest_lists_the_best_confidence_first_and_a_zero_is_a_score(make_rig):
    rig = make_rig(Switch())
    for i, score in enumerate([None, 0.0, 70.0, 10.0, 0.0, 90.0, 50.0, None, 30.0], start=1):
        rig.enqueue(rig.alert(i, minutes=i, score=score))
    for minute in range(1, 10):
        rig.clock.now = T0 + dt.timedelta(minutes=minute)
        rig.outbox.drain_once()

    (digest,) = [r for r in rig.rows() if r["kind"] == "digest"]

    scores = [a["confidence"]["score"] for a in digest["payload"]["alerts"]]
    assert scores == [90.0, 70.0, 50.0, 30.0, 10.0, 0.0, 0.0, None, None]


# ========================= (G) the spacing wait comes before the claim ===================
def test_the_spacing_wait_happens_before_the_row_is_claimed(make_rig):
    """A crash during the 1 s spacing wait must not leave a never-sent row in 'sending' (it
    would come back with the resend marker): the row is claimed only after the wait."""
    from onelot.notify.telegram_outbox import TelegramOutbox

    rig = make_rig()
    during_wait: list[list[str]] = []

    def sleep(seconds: float) -> None:
        during_wait.append([r["status"] for r in rig.rows()])
        rig.clock.sleep(seconds)

    outbox = TelegramOutbox(rig.db.engine, rig.client, clock=rig.clock, sleep=sleep)
    rig.enqueue(rig.alert(1))
    rig.enqueue(rig.alert(2))

    assert outbox.drain_once() == 1 and outbox.drain_once() == 1

    assert during_wait == [["sent", "queued"]]
    assert rig.clock.sleeps == [1.0]
