"""US-004 AC4: what the Notifier and the Telegram channel promise each other.

  * Notification carries kind and dedup_key, set by Notifier.send from signal_id, position_id
    and level;
  * a channel may define accepts(n): only SIGNAL, EXIT and CRITICAL reach Telegram, INFO,
    WARNING and the self-test never do;
  * add_channel / replace_channel work under the Notifier lock and create the stats entry
    (no KeyError);
  * status() merges each channel's own status();
  * a message counts as sent only when Telegram confirmed it; enqueued is a separate count.

Nothing is asserted about the exact `kind` or `dedup_key` strings: only that they are derived
from the right inputs, stable for the same alert and different for different ones.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from typing import Any

import pytest
from us003_helpers import (
    NOW,
    TOKEN,
    assert_well_formed,
    install_network_guard,
    tg_error,
    visible,
)
from us004_helpers import (
    RecordingChannel,
    SessionProbe,
    alert_at,
    alert_id,
    bare_notifier,
    build_notifier,
    critical_notification,
    exit_payload,
    rig_factory,
    scripted,
    signal_notification,
)

from onelot.config import NotifyCfg
from onelot.notify.base import Notification, NotificationLevel

L = NotificationLevel


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"only the injected transport may be used: {attempts}"


@pytest.fixture
def make_rig(tmp_path):
    with rig_factory(tmp_path) as build:
        yield build


def telegram_entry(notifier: Any) -> dict[str, Any]:
    return next(c for c in notifier.status()["channels"] if c["name"] == "telegram")


def one_of_each_level(rig: Any) -> list[Notification]:
    """INFO, WARNING, SIGNAL, EXIT and CRITICAL notifications as the Desk and Engine make them."""
    now = rig.clock()
    return [
        Notification(title="Monitoring SBIN", body="re-checked every poll", level=L.INFO),
        Notification(title="Feed slow", body="bars are 40 s old", level=L.WARNING),
        signal_notification(alert_at(now, id=alert_id(1)), now),
        Notification(
            title="EXIT SBIN - STOP",
            body="close it",
            level=L.EXIT,
            position_id="pos000000001",
            created_at=now,
            payload=exit_payload("pos000000001", -740.0),
        ),
        critical_notification("Dhan token expired", "renew it", now),
    ]


# ========================= Notification fields ==========================================
def test_notification_gains_kind_and_dedup_key_with_neutral_defaults():
    n = Notification(title="t", body="b")
    assert n.kind == "generic"
    assert n.dedup_key is None
    assert {"kind", "dedup_key"} <= {f.name for f in dataclasses.fields(Notification)}


def test_notifier_send_sets_dedup_key_from_signal_id_position_id_and_level():
    spy = RecordingChannel("spy")
    notifier = bare_notifier()
    notifier.add_channel(spy)

    def make(level: L, **kw: Any) -> Notification:
        return Notification(title="x", body="", level=level, created_at=NOW, **kw)

    sig = make(L.SIGNAL, signal_id="sig-aaa111")
    sig_again = make(L.SIGNAL, signal_id="sig-aaa111")
    sig_other = make(L.SIGNAL, signal_id="sig-bbb222")
    ex = make(L.EXIT, position_id="pos-ccc333")
    ex_again = make(L.EXIT, position_id="pos-ccc333")
    ex_other = make(L.EXIT, position_id="pos-ddd444")
    same_id_signal = make(L.SIGNAL, signal_id="shared-id")
    same_id_exit = make(L.EXIT, position_id="shared-id")
    for n in (sig, sig_again, sig_other, ex, ex_again, ex_other, same_id_signal, same_id_exit):
        notifier.send(n)

    assert isinstance(sig.dedup_key, str) and "sig-aaa111" in sig.dedup_key
    assert sig.dedup_key == sig_again.dedup_key, "the same signal always maps to the same key"
    assert sig.dedup_key != sig_other.dedup_key
    assert isinstance(ex.dedup_key, str) and "pos-ccc333" in ex.dedup_key
    assert ex.dedup_key == ex_again.dedup_key
    assert ex.dedup_key != ex_other.dedup_key
    assert same_id_signal.dedup_key != same_id_exit.dedup_key, "the level is part of the key"
    # the channel gets the very object the Notifier annotated
    assert spy.sent[0].dedup_key == sig.dedup_key


def test_notifier_send_marks_the_kind_of_signal_exit_and_critical_notifications():
    notifier = bare_notifier()
    sig = Notification(title="s", body="", level=L.SIGNAL, signal_id="s1", created_at=NOW)
    ex = Notification(title="e", body="", level=L.EXIT, position_id="p1", created_at=NOW)
    crit = Notification(title="c", body="", level=L.CRITICAL, created_at=NOW)
    for n in (sig, ex, crit):
        notifier.send(n)

    kinds = [sig.kind, ex.kind, crit.kind]
    assert "generic" not in kinds, kinds
    assert len(set(kinds)) == 3, f"each level needs its own kind: {kinds}"


def test_two_criticals_never_share_a_dedup_key_so_neither_is_lost():
    """A CRITICAL has no signal or position id. If its key were built from the level alone the
    second 'Clock problem' of the week would be swallowed as a duplicate of the first."""
    notifier = bare_notifier()
    first = critical_notification("Clock problem", "drift 3 s", NOW)
    second = critical_notification("Clock problem", "drift 9 s", NOW.replace(hour=14))
    third = critical_notification("Dhan token expired", "renew", NOW)
    for n in (first, second, third):
        notifier.send(n)

    keys = [first.dedup_key, second.dedup_key, third.dedup_key]
    assert all(isinstance(k, str) and k for k in keys), keys
    assert len(set(keys)) == 3, f"distinct criticals share a dedup key: {keys}"


# ========================= accepts() ====================================================
def test_the_channel_accepts_exactly_signal_exit_and_critical(make_rig):
    rig = make_rig()
    spy = RecordingChannel("spy")
    notifier, channel = build_notifier(rig, extra=(spy,))
    for n in one_of_each_level(rig):
        notifier.send(n)
    rows_before = len(rig.rows())

    verdict = {n.level.value: channel.accepts(n) for n in spy.sent}

    assert verdict == {
        "INFO": False,
        "WARNING": False,
        "SIGNAL": True,
        "EXIT": True,
        "CRITICAL": True,
    }
    assert all(type(v) is bool for v in verdict.values())
    assert len(rig.rows()) == rows_before and rig.rec.calls == 0, "accepts() has no side effects"


def test_only_signal_exit_and_critical_reach_the_outbox(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    results = [notifier.send(n) for n in one_of_each_level(rig)]

    assert [r.get("telegram") is True for r in results] == [False, False, True, True, True]
    kinds = [r["kind"] for r in rig.rows()]
    assert kinds == ["alert", "exit", "critical"], kinds  # outbox kinds (decided: 'critical')
    assert rig.rec.calls == 0


def test_a_message_that_telegram_does_not_get_is_not_a_telegram_failure(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)

    notifier.info("Monitoring SBIN", "re-checked every poll")
    notifier.warning("Feed slow", "bars are 40 s old")

    stats = notifier.stats["telegram"]
    assert (stats.sent, stats.failed) == (0, 0)
    entry = telegram_entry(notifier)
    assert (entry["sent"], entry["failed"], entry["enqueued"]) == (0, 0, 0)
    assert rig.rows() == [] and rig.rec.calls == 0


def test_the_startup_self_test_never_reaches_telegram(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)

    notifier.self_test()
    rig.drain()

    assert rig.rows() == [] and rig.rec.calls == 0


def test_the_configured_levels_decide_what_is_accepted(make_rig):
    rig = make_rig()
    notifier, channel = build_notifier(rig, levels=["SIGNAL"])
    spy = RecordingChannel("spy")
    notifier.add_channel(spy)
    for n in one_of_each_level(rig):
        notifier.send(n)

    assert [r["kind"] for r in rig.rows()] == ["alert"], "only SIGNAL is configured"
    assert [channel.accepts(n) for n in spy.sent] == [False, False, True, False, False]


# ========================= CRITICAL on the phone ========================================
def test_a_critical_reaches_the_phone_with_its_title_and_is_never_silent(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig, in_session=SessionProbe(False))  # outside market hours

    result = notifier.send(critical_notification("Dhan token expired", "Renew it", rig.clock()))
    rig.drain()

    assert result["telegram"] is True
    (text,) = rig.rec.texts()
    assert "Dhan token expired" in visible(text)
    assert_well_formed(text)
    assert rig.rec.silent() == [False], "CRITICAL is never silent (ADR-002 amendment 9)"


def test_the_convenience_critical_call_is_delivered_too(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)

    result = notifier.critical("Clock problem", "local and IST differ by 3 s")
    rig.drain()

    assert result["telegram"] is True
    assert "Clock problem" in visible(rig.rec.texts()[0])


def test_two_criticals_with_the_same_title_are_both_delivered(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    notifier.send(critical_notification("Clock problem", "first", rig.clock()))
    rig.clock.advance(3600)
    notifier.send(critical_notification("Clock problem", "second", rig.clock()))

    rig.drain()

    assert len(rig.rec.texts()) == 2, "the second CRITICAL was swallowed as a duplicate"


def test_html_in_a_critical_title_is_escaped(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    notifier.send(critical_notification("Feed <dead> & stale", "x < y", rig.clock()))

    rig.drain()

    (text,) = rig.rec.texts()
    assert_well_formed(text)
    assert "Feed <dead> & stale" in visible(text)


def test_a_critical_with_a_huge_body_is_cut_to_fit_telegrams_message_limit(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    notifier.send(critical_notification("Engine halted", "x" * 10_000, rig.clock()))

    rig.drain()

    (text,) = rig.rec.texts()
    assert len(text) <= 4096, "Telegram rejects longer messages: the alert would be lost"
    assert "Engine halted" in visible(text)
    assert rig.statuses() == ["sent"]


# ========================= add_channel / replace_channel ================================
def test_add_channel_registers_the_channel_and_its_stats_entry():
    notifier = bare_notifier()
    late = RecordingChannel("late")

    notifier.add_channel(late)

    assert [c.name for c in notifier.channels] == ["dashboard", "late"]
    assert "late" in notifier.stats
    result = notifier.send(Notification(title="x", body="", level=L.INFO, created_at=NOW))
    assert result == {"dashboard": True, "late": True}, "no KeyError for the new channel"
    assert notifier.stats["late"].sent == 1
    assert "late" in [c["name"] for c in notifier.status()["channels"]]


def test_replace_channel_swaps_the_channel_and_keeps_the_stats_entry_usable():
    notifier = bare_notifier()
    old, new = RecordingChannel("toast"), RecordingChannel("toast")
    notifier.add_channel(old)

    notifier.replace_channel("toast", new)
    notifier.send(Notification(title="x", body="", level=L.INFO, created_at=NOW))

    assert [c.name for c in notifier.channels] == ["dashboard", "toast"]
    assert new in notifier.channels and old not in notifier.channels
    assert len(new.sent) == 1 and old.sent == []
    assert "toast" in notifier.stats


def test_replace_channel_with_a_renamed_channel_creates_its_stats_entry():
    notifier = bare_notifier()
    notifier.add_channel(RecordingChannel("toast"))

    notifier.replace_channel("toast", RecordingChannel("toast2"))
    result = notifier.send(Notification(title="x", body="", level=L.INFO, created_at=NOW))

    assert result == {"dashboard": True, "toast2": True}
    assert "toast2" in notifier.stats


@pytest.mark.parametrize("operation", ["add", "replace"])
def test_add_and_replace_channel_take_the_notifier_lock(operation):
    notifier = bare_notifier()
    notifier.add_channel(RecordingChannel("toast"))
    fresh = RecordingChannel("fresh" if operation == "add" else "toast")
    done = threading.Event()

    def run() -> None:
        if operation == "add":
            notifier.add_channel(fresh)
        else:
            notifier.replace_channel("toast", fresh)
        done.set()

    worker = threading.Thread(target=run, daemon=True)
    with notifier._lock:  # the lock named in the contract: held by someone else
        worker.start()
        assert not done.wait(0.3), f"{operation}_channel did not wait for the Notifier lock"
        assert fresh not in notifier.channels
    assert done.wait(20), f"{operation}_channel never finished after the lock was released"
    worker.join(20)
    assert fresh in notifier.channels


# ========================= status() merges the channel's own status =====================
def test_status_merges_each_channels_own_status_into_its_entry():
    notifier = bare_notifier()
    notifier.add_channel(
        RecordingChannel("spy", own_status={"queue_depth": 3, "p95_latency_ms": 1234, "note": "x"})
    )

    channels = {c["name"]: c for c in notifier.status()["channels"]}

    spy = channels["spy"]
    assert (spy["queue_depth"], spy["p95_latency_ms"], spy["note"]) == (3, 1234, "x")
    base = {"name", "available", "sent", "failed", "last_error", "last_sent_at"}
    assert base <= set(spy), "the Notifier's own fields stay"
    assert "queue_depth" not in channels["dashboard"], "a channel without status() is unchanged"
    assert base <= set(channels["dashboard"])
    assert json.loads(json.dumps(notifier.status()))  # still plain JSON


def test_the_telegram_channel_reports_the_outbox_status(make_rig):
    rig = make_rig()
    notifier, channel = build_notifier(rig)
    notifier.send(signal_notification(alert_at(rig.clock(), id=alert_id(1)), rig.clock()))

    own = channel.status()
    entry = telegram_entry(notifier)

    keys = {"configured", "last_ok_at", "queue_depth", "last_error", "p95_latency_ms"}
    assert keys <= set(own) and keys <= set(entry)
    assert (entry["queue_depth"], entry["enqueued"]) == (1, 1)
    assert json.loads(json.dumps(own))


def test_channel_name_and_availability(make_rig):
    rig = make_rig()
    notifier, channel = build_notifier(rig)
    assert channel.name == "telegram"
    assert channel.available is True
    from us003_helpers import telegram

    dead = telegram().TelegramChannel(
        telegram().TelegramClient("", ""), rig.outbox, NotifyCfg(), SessionProbe(), clock=rig.clock
    )
    assert dead.available is False


# ========================= sent counts only on confirmation =============================
def test_stats_count_a_message_as_sent_only_when_telegram_confirmed_it(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    for i in range(3):
        notifier.send(signal_notification(alert_at(rig.clock(), id=alert_id(i)), rig.clock()))

    entry = telegram_entry(notifier)
    assert entry["sent"] == 0, "queued is not sent"
    assert entry["enqueued"] == 3 and entry["queue_depth"] == 3
    assert entry["last_sent_at"] is None
    assert notifier.stats["telegram"].sent == 0 and notifier.stats["telegram"].last_sent_at is None

    rig.drain()

    entry = telegram_entry(notifier)
    assert entry["sent"] == 3 and entry["enqueued"] == 3 and entry["queue_depth"] == 0
    assert entry["failed"] == 0


def test_a_message_telegram_rejected_is_never_counted_as_sent(make_rig):
    rig = make_rig(scripted(tg_error(403, f"Forbidden: bot was blocked by the user {TOKEN}")))
    notifier, _ = build_notifier(rig)
    notifier.send(signal_notification(alert_at(rig.clock(), id=alert_id(1)), rig.clock()))

    rig.drain()

    entry = telegram_entry(notifier)
    assert entry["sent"] == 0 and entry["failed"] == 1 and entry["enqueued"] == 1
    assert "HTTP 403" in entry["last_error"] and TOKEN not in entry["last_error"]
