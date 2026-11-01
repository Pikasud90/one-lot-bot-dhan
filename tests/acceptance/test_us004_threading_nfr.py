"""US-004 threading NFRs (ADR-002 amendment 6) that can be observed from outside.

  * the worker never calls the Notifier (it would re-enter the desk's lock chain), on any path:
    delivery, rate limit, permanent failure, expiry, digest;
  * drain_once never calls the real time.sleep: the only wait is the injected sleep that keeps
    1.000 s between sends.

Not observable here and left to review: "the worker never takes the desk lock".
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from us003_helpers import install_network_guard, ok_response, tg_error
from us004_helpers import alert_at, alert_id, rig_factory, scripted

from onelot.notify.manager import Notifier


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"only the injected transport may be used: {attempts}"


@pytest.fixture
def make_rig(tmp_path):
    with rig_factory(tmp_path) as build:
        yield build


def busy_queue(rig: Any) -> None:
    """Every path at once: a burst over 8 (digest), one alert that will have expired, a failure."""
    now = rig.clock()
    for i in range(10):
        rig.enqueue_alert(alert_at(now, id=alert_id(i), score=50.0 + i))
    rig.enqueue_alert(alert_at(now, id=alert_id(50), score=99.0, minutes=0.05))  # 3 s of life


def test_the_worker_never_calls_back_into_the_notifier(make_rig, monkeypatch):
    rig = make_rig(
        scripted(
            tg_error(429, "slow down", parameters={"retry_after": 2}),
            tg_error(500, "boom"),
            tg_error(403, "Forbidden: bot was blocked by the user"),
            ok_response,
        )
    )
    calls: list[str] = []
    monkeypatch.setattr(Notifier, "send", lambda self, n, dedup_key=None: calls.append(n.title))
    busy_queue(rig)
    rig.clock.advance(10)  # the 3-second alert has expired by the time the worker looks

    for _ in range(6):
        rig.drain()
        rig.clock.advance(120)

    assert rig.rec.calls > 0, "the scenario never reached the transport"
    assert calls == [], f"the worker called the Notifier: {calls}"


def test_drain_once_never_calls_the_real_sleep(make_rig, monkeypatch):
    rig = make_rig(
        scripted(
            tg_error(429, "slow", parameters={"retry_after": 1}), tg_error(500, "boom"), ok_response
        )
    )
    real_sleeps: list[float] = []
    with monkeypatch.context() as patched:
        patched.setattr(time, "sleep", lambda seconds: real_sleeps.append(seconds))
        busy_queue(rig)
        for _ in range(6):
            rig.drain()
            rig.clock.advance(120)

    assert real_sleeps == [], f"drain_once slept for real: {real_sleeps}"
    assert rig.clock.sleeps, "the 1 s spacing must have gone through the injected sleep"
    assert all(0 <= s <= 1.0 + 1e-6 for s in rig.clock.sleeps), rig.clock.sleeps
