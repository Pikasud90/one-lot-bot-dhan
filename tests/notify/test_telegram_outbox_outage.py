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
