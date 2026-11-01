"""US-004 review round 1: what the phone gets after an outage, and what the SLO measures.

Manager decisions (A)-(D): EXIT and CRITICAL texts carry the time of the event, flagged when
they are sent late; a newer EXIT for a position supersedes its older unsent ones; missed notes
that pile up are folded into one digest; p95 counts alert, exit and critical rows only.

Same rig as test_telegram_outbox: a file-based tmp SQLite, a fake clock whose sleep advances
it, httpx.MockTransport. No network, no real sleeping.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

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
