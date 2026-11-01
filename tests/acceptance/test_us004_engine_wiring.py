"""US-004 Engine wiring (Manager clarifications) and AC3 "Engine.stop stops and joins the worker
before db.dispose".

The REAL Engine runs on a throw-away project root (see us004_helpers.make_engine): the broker,
feed, regime load, Desk and its loops are replaced by no-ops, `TelegramClient.from_env()` still
reads the (monkeypatched) environment but hands back a client on a MockTransport. What is left
is what the story changes.

The worker is a real thread here, driven by the real clock, so these tests wait for it with
generous timeouts and assert no durations.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import pytest
from us003_helpers import (
    CHAT,
    TOKEN,
    capture_logs,
    install_network_guard,
    leaked_forms,
    record_blob,
    telegram,
)
from us004_helpers import (
    alert_at,
    alert_id,
    make_engine,
    probe,
    signal_notification,
    wait_until,
)

from onelot.clock import now_ist


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"only the injected transport may be used: {attempts}"


@pytest.fixture
def engine_factory(tmp_path, monkeypatch):
    built: list[Any] = []

    def build(**kwargs: Any) -> Any:
        rig = make_engine(tmp_path / f"engine{len(built)}", monkeypatch, **kwargs)
        built.append(rig)
        return rig

    yield build
    for rig in built:  # a failing test must not leave a worker thread behind
        try:
            rig.engine.stop()
        except Exception:  # noqa: BLE001
            pass


def channel_named(engine: Any, name: str) -> Any:
    return next((c for c in engine.notifier.channels if c.name == name), None)


# ========================= configured ===================================================
@pytest.mark.parametrize("flag", [True, None], ids=["notify.telegram: true", "shipped default"])
def test_the_engine_adds_the_telegram_channel_and_starts_its_worker_when_the_client_is_available(
    engine_factory, flag
):
    rig = engine_factory(telegram_flag=flag)
    before = set(threading.enumerate())

    rig.engine.start()

    channel = channel_named(rig.engine, "telegram")
    assert channel is not None, "no telegram channel after Engine.start()"
    assert isinstance(channel, telegram().TelegramChannel) and channel.available is True
    assert rig.from_env_calls, "the Engine must build its client with TelegramClient.from_env()"
    assert len(rig.worker_threads(before)) == 1, "exactly one outbox worker thread"
    assert rig.rec.calls == 0, "the startup self-test must not reach the phone"
    assert probe(rig.db_path) == [], "and must not leave a row behind"
    assert [c.name for c in rig.engine.notifier.channels][0] == "dashboard"


def test_an_alert_raised_through_the_engines_notifier_reaches_the_phone(engine_factory):
    rig = engine_factory()
    with capture_logs() as handler:
        rig.engine.start()
        now = now_ist()
        alert = alert_at(now, id=alert_id(1))  # real clock: the worker uses the real clock
        result = rig.engine.notifier.send(signal_notification(alert, now))
        # a missing channel must fail here, not after a 20 s wait for a worker that is not there
        delivered = result.get("telegram") is True and wait_until(lambda: rig.rec.calls >= 1)
        rig.engine.stop()

    assert result.get("telegram") is True and result["dashboard"] is True
    assert delivered, "the worker never sent the queued alert"
    assert alert["id"][:6] in rig.rec.texts()[0]
    assert [row[0] for row in probe(rig.db_path)] == ["sent"]
    assert leaked_forms(record_blob(handler.records)) == [], "the token reached a log record"


# ========================= not configured ===============================================
@pytest.mark.parametrize(
    "token, chat",
    [(None, None), ("not-a-bot-token", CHAT), (TOKEN, None), (None, CHAT)],
    ids=["nothing set", "malformed token", "token only", "chat id only"],
)
def test_without_a_usable_client_the_engine_adds_nothing_and_logs_one_info_line(
    engine_factory, caplog, token, chat
):
    rig = engine_factory(token=token, chat=chat)
    before = set(threading.enumerate())

    with caplog.at_level(logging.INFO):
        rig.engine.start()

    assert channel_named(rig.engine, "telegram") is None
    notes = [r for r in caplog.records if "telegram not configured" in r.getMessage().lower()]
    assert len(notes) == 1, f"expected exactly one 'Telegram not configured' line, got {len(notes)}"
    assert notes[0].levelno == logging.INFO
    assert rig.worker_threads(before) == [], "no worker without a client"
    blob = record_blob(caplog.records)
    assert leaked_forms(blob) == [] and "not-a-bot-token" not in blob
    now = now_ist()
    result = rig.engine.notifier.send(signal_notification(alert_at(now, id=alert_id(1)), now))
    assert result == {"dashboard": True}, "alerts still work with only the dashboard"
    rig.engine.stop()


# ========================= stop ordering ================================================
def test_engine_stop_stops_and_joins_the_worker_before_the_database_is_disposed(
    engine_factory, monkeypatch
):
    rig = engine_factory()
    before = set(threading.enumerate())
    rig.engine.start()
    workers = rig.worker_threads(before)
    assert workers, "the worker did not start"
    order: list[str] = []
    real_dispose = rig.engine.db.dispose
    real_stop = telegram().TelegramOutbox.stop

    def dispose() -> None:
        alive = sum(t.is_alive() for t in workers)
        order.append(f"dispose(alive={alive})")
        real_dispose()

    def stop(self: Any) -> None:
        order.append("outbox.stop")
        real_stop(self)
        order.append("outbox.stopped")

    monkeypatch.setattr(rig.engine.db, "dispose", dispose)
    monkeypatch.setattr(telegram().TelegramOutbox, "stop", stop)

    rig.engine.stop()

    assert "outbox.stopped" in order, f"Engine.stop never stopped the outbox: {order}"
    first_dispose = next(i for i, step in enumerate(order) if step.startswith("dispose"))
    assert order.index("outbox.stopped") < first_dispose, f"the database went first: {order}"
    assert order[first_dispose] == "dispose(alive=0)", "the worker must be joined, not just told"
    assert not any(t.is_alive() for t in workers)


def test_engine_stop_works_when_telegram_was_never_configured(engine_factory):
    rig = engine_factory(token=None, chat=None)
    rig.engine.start()

    rig.engine.stop()

    assert rig.engine.status.running is False
