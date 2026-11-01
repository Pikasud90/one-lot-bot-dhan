"""US-004 AC4, developer tests: the Notifier's channel hooks beyond the acceptance suite.

Covers the unhappy paths: a filter or a status() that raises, an explicit dedup key or kind
kept as the caller set it, replace_channel for a name that is not there, and the deferred
(queue-backed) channel whose `True` means "enqueued", never "sent".
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

from onelot.clock import IST
from onelot.config import NotifyCfg
from onelot.notify.base import Notification, NotificationLevel
from onelot.notify.manager import Notifier

NOW = dt.datetime(2026, 11, 2, 10, 0, tzinfo=IST)
L = NotificationLevel


class Spy:
    available = True

    def __init__(self, name: str = "spy", ok: bool = True) -> None:
        self.name = name
        self.ok = ok
        self.sent: list[Notification] = []

    def send(self, n: Notification) -> bool:
        self.sent.append(n)
        return self.ok


def notifier() -> Notifier:
    return Notifier(SimpleNamespace(notify=NotifyCfg(os_toast=False)), dedup_seconds=0.0)


def note(level: L = L.INFO, **kw: Any) -> Notification:
    return Notification(title="t", body="b", level=level, created_at=NOW, **kw)


def test_an_explicit_dedup_key_and_kind_are_kept():
    n = note(L.SIGNAL, signal_id="s1")
    n.dedup_key, n.kind = "custom:key", "custom"
    notifier().send(n)
    assert (n.dedup_key, n.kind) == ("custom:key", "custom")


def test_info_and_warning_get_a_level_kind_and_a_key_too():
    info, warning = note(L.INFO), note(L.WARNING, signal_id="s9")
    nf = notifier()
    nf.send(info)
    nf.send(warning)
    assert info.kind == "info" and info.dedup_key == f"info:{info.notification_id}"
    assert warning.kind == "warning" and warning.dedup_key == "warning:s9"


def test_a_channel_that_declines_is_left_out_of_the_result_and_the_stats():
    spy = Spy()
    spy.accepts = lambda n: n.level is L.SIGNAL  # type: ignore[attr-defined]
    nf = notifier()
    nf.add_channel(spy)

    result = nf.send(note(L.INFO))

    assert result == {"dashboard": True}
    assert spy.sent == [] and (nf.stats["spy"].sent, nf.stats["spy"].failed) == (0, 0)


def test_a_filter_that_raises_counts_as_a_failure_never_a_silent_drop():
    spy = Spy()

    def broken(n: Notification) -> bool:
        raise ValueError("bad filter")

    spy.accepts = broken  # type: ignore[attr-defined]
    nf = notifier()
    nf.add_channel(spy)

    result = nf.send(note(L.SIGNAL, signal_id="s1"))

    assert result == {"dashboard": True, "spy": False}
    assert nf.stats["spy"].failed == 1 and "ValueError" in (nf.stats["spy"].last_error or "")


def test_a_deferred_channel_counts_enqueued_not_sent():
    spy = Spy("queue")
    spy.deferred = True  # type: ignore[attr-defined]
    nf = notifier()
    nf.add_channel(spy)

    nf.send(note(L.SIGNAL, signal_id="s1"))

    st = nf.stats["queue"]
    assert (st.sent, st.enqueued, st.last_sent_at) == (0, 1, None)
    entry = next(c for c in nf.status()["channels"] if c["name"] == "queue")
    assert entry["sent"] == 0 and entry["enqueued"] == 1


def test_a_status_that_raises_leaves_the_notifier_status_intact():
    spy = Spy()

    def broken() -> dict[str, Any]:
        raise RuntimeError("db gone")

    spy.status = broken  # type: ignore[attr-defined]
    nf = notifier()
    nf.add_channel(spy)

    entry = next(c for c in nf.status()["channels"] if c["name"] == "spy")

    assert entry["name"] == "spy" and entry["sent"] == 0


def test_the_channels_own_keys_win_but_the_name_stays():
    spy = Spy()
    spy.status = lambda: {"sent": 7, "name": "impostor"}  # type: ignore[attr-defined]
    nf = notifier()
    nf.add_channel(spy)

    names = [c["name"] for c in nf.status()["channels"]]
    entry = next(c for c in nf.status()["channels"] if c["name"] == "spy")

    assert names == ["dashboard", "spy"] and entry["sent"] == 7


def test_replace_channel_for_an_unknown_name_adds_the_channel():
    nf = notifier()
    fresh = Spy("telegram")

    nf.replace_channel("telegram", fresh)

    assert [c.name for c in nf.channels] == ["dashboard", "telegram"]
    assert "telegram" in nf.stats


def test_add_channel_with_a_taken_name_replaces_rather_than_duplicates():
    nf = notifier()
    first, second = Spy("toast"), Spy("toast")
    nf.add_channel(first)

    nf.add_channel(second)

    assert [c for c in nf.channels if c.name == "toast"] == [second]


SECRET_SQL = 'INSERT INTO telegram_outbox (payload) VALUES (\'{"risk_reason": "halt"}\')'


class Raising(Spy):
    def send(self, n: Notification) -> bool:
        raise RuntimeError(SECRET_SQL)


def test_a_channel_failure_keeps_and_logs_only_the_exception_class(caplog):
    """An exception's text can quote SQL, parameters or the payload: never store or log it."""
    nf = notifier()
    nf.add_channel(Raising("db"))

    with caplog.at_level("DEBUG"):
        result = nf.send(note(L.SIGNAL, signal_id="s1"))

    assert result == {"dashboard": True, "db": False}
    assert nf.stats["db"].last_error == "RuntimeError"
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in logged and "INSERT" not in logged and "risk_reason" not in logged


def test_a_status_failure_logs_only_the_exception_class(caplog):
    spy = Spy()

    def broken() -> dict[str, Any]:
        raise RuntimeError(SECRET_SQL)

    spy.status = broken  # type: ignore[attr-defined]
    nf = notifier()
    nf.add_channel(spy)

    with caplog.at_level("DEBUG"):
        nf.status()

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in logged and "INSERT" not in logged


def test_nothing_due_is_a_success_that_no_counter_sees():
    from onelot.notify.base import NOTHING_DUE

    spy = Spy("queue")
    spy.deferred = True  # type: ignore[attr-defined]
    spy.send = lambda n: NOTHING_DUE  # type: ignore[method-assign]
    nf = notifier()
    nf.add_channel(spy)

    result = nf.send(note(L.EXIT, position_id="p1"))

    assert result == {"dashboard": True, "queue": True}
    assert type(result["queue"]) is bool
    st = nf.stats["queue"]
    assert (st.sent, st.enqueued, st.failed) == (0, 0, 0)
