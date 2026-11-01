"""US-004 mode B: edges of the Notifier changes (AC4) that a story test would not think of.

* replace_channel with an unknown name adds the channel; add_channel with a known name
  replaces it, so a name is never listed twice;
* a channel whose status() or accepts() raises is isolated: the other channels, the other
  entries and the stats all keep working, and only the exception CLASS is logged or stored;
* a preset kind or dedup_key is kept; NOTHING_DUE counts nowhere; a deferred channel counts
  enqueued, never sent;
* add_channel from one thread while others send never raises (the stats entry always exists
  before the channel can be called).
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import pytest
from us003_helpers import NOW, TOKEN, install_network_guard
from us004_helpers import RecordingChannel, bare_notifier

from onelot.notify.base import NOTHING_DUE, Notification, NotificationLevel

L = NotificationLevel


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"only the injected transport may be used: {attempts}"


def info(title: str = "x") -> Notification:
    return Notification(title=title, body="", level=L.INFO, created_at=NOW)


class Scripted:
    """A channel whose behaviour each test chooses."""

    available = True

    def __init__(self, name: str, send: Any = True, deferred: bool = False) -> None:
        self.name = name
        self._send = send
        self.deferred = deferred
        self.seen: list[Notification] = []

    def send(self, n: Notification) -> Any:
        self.seen.append(n)
        if isinstance(self._send, Exception):
            raise self._send
        return self._send


def test_replace_channel_with_an_unknown_name_adds_the_channel_at_the_end():
    notifier = bare_notifier()
    notifier.add_channel(RecordingChannel("toast"))
    fresh = RecordingChannel("telegram")

    notifier.replace_channel("no-such-channel", fresh)

    assert [c.name for c in notifier.channels] == ["dashboard", "toast", "telegram"]
    assert "telegram" in notifier.stats
    assert notifier.send(info()) == {"dashboard": True, "toast": True, "telegram": True}


def test_add_channel_with_an_existing_name_replaces_it_and_keeps_one_entry():
    notifier = bare_notifier()
    old, new = RecordingChannel("toast"), RecordingChannel("toast")
    notifier.add_channel(old)
    notifier.send(info())

    notifier.add_channel(new)
    result = notifier.send(info())

    assert [c.name for c in notifier.channels] == ["dashboard", "toast"]
    assert result == {"dashboard": True, "toast": True}
    assert len(old.sent) == 1 and len(new.sent) == 1
    assert [c["name"] for c in notifier.status()["channels"]] == ["dashboard", "toast"]


def test_a_channel_whose_status_raises_does_not_break_the_notifier_status(caplog):
    notifier = bare_notifier()
    flaky = RecordingChannel("flaky")
    flaky.status = lambda: (_ for _ in ()).throw(RuntimeError(f"db locked {TOKEN}"))  # type: ignore[method-assign]
    notifier.add_channel(flaky)
    notifier.add_channel(RecordingChannel("steady", own_status={"queue_depth": 2}))

    with caplog.at_level(logging.DEBUG):
        status = notifier.status()

    entries = {c["name"]: c for c in status["channels"]}
    assert set(entries) == {"dashboard", "flaky", "steady"}, "every channel keeps its entry"
    assert {"sent", "failed", "available"} <= set(entries["flaky"])
    assert entries["steady"]["queue_depth"] == 2
    assert json.loads(json.dumps(status))
    assert TOKEN not in caplog.text, "only the exception class may be logged"
    assert "RuntimeError" in caplog.text


def test_a_channel_whose_accepts_raises_is_a_failure_and_the_others_still_deliver(caplog):
    notifier = bare_notifier()

    class BadFilter(RecordingChannel):
        def accepts(self, n: Notification) -> bool:
            raise ValueError(f"cannot decide {TOKEN}")

    bad, good = BadFilter("bad"), RecordingChannel("good")
    notifier.add_channel(bad)
    notifier.add_channel(good)

    with caplog.at_level(logging.DEBUG):
        result = notifier.send(info())

    assert result == {"dashboard": True, "bad": False, "good": True}
    assert bad.sent == [] and len(good.sent) == 1
    assert notifier.stats["bad"].failed == 1 and TOKEN not in caplog.text


def test_only_the_exception_class_is_stored_and_logged_when_a_channel_raises(caplog):
    notifier = bare_notifier()
    notifier.add_channel(Scripted("boom", RuntimeError(f"INSERT ... VALUES ('{TOKEN}')")))

    with caplog.at_level(logging.DEBUG):
        result = notifier.send(info())

    assert result["boom"] is False and result["dashboard"] is True
    stats = notifier.stats["boom"]
    assert stats.failed == 1 and stats.last_error == "RuntimeError"
    assert TOKEN not in caplog.text and TOKEN not in json.dumps(notifier.status())


def test_a_preset_kind_and_dedup_key_are_kept():
    notifier = bare_notifier()
    spy = RecordingChannel("spy")
    notifier.add_channel(spy)
    n = Notification(
        title="x",
        body="",
        level=L.SIGNAL,
        signal_id="s1",
        created_at=NOW,
        kind="custom-kind",
        dedup_key="custom-key",
    )

    notifier.send(n)

    assert (n.kind, n.dedup_key) == ("custom-kind", "custom-key")
    assert spy.sent[0] is n


def test_nothing_due_is_success_and_counts_nowhere():
    notifier = bare_notifier()
    quiet = Scripted("quiet", NOTHING_DUE, deferred=True)
    notifier.add_channel(quiet)

    result = notifier.send(info())

    assert result["quiet"] is True
    stats = notifier.stats["quiet"]
    assert (stats.sent, stats.failed, stats.enqueued) == (0, 0, 0)
    assert stats.last_sent_at is None


def test_a_deferred_channel_counts_enqueued_never_sent():
    notifier = bare_notifier()
    notifier.add_channel(Scripted("queue", True, deferred=True))

    notifier.send(info())
    notifier.send(info())

    stats = notifier.stats["queue"]
    assert (stats.enqueued, stats.sent, stats.failed) == (2, 0, 0)
    assert stats.last_sent_at is None
    entry = next(c for c in notifier.status()["channels"] if c["name"] == "queue")
    assert (entry["enqueued"], entry["sent"]) == (2, 0)


def test_adding_channels_while_other_threads_send_never_raises():
    notifier = bare_notifier()
    errors: list[BaseException] = []
    stop = threading.Event()

    def sender() -> None:
        try:
            while not stop.is_set():
                notifier.send(info())
                notifier.status()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=sender, daemon=True) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        for i in range(60):
            notifier.add_channel(RecordingChannel(f"late{i}"))
            if i % 3 == 0:
                notifier.replace_channel(f"late{i}", RecordingChannel(f"late{i}"))
    finally:
        stop.set()
        for t in threads:
            t.join(30)

    assert not errors, errors[:3]
    assert len(notifier.channels) == 61
    assert all(c.name in notifier.stats for c in notifier.channels)
