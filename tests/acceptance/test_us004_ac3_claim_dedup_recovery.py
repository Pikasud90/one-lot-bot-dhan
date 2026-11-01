"""US-004 AC3: claim, commit, THEN call; at-least-once recovery; one row per (alert_id, kind);
duplicate enqueue is not a failure; the worker thread starts once and stops cleanly.

  * the worker claims a row with UPDATE ... WHERE status is queued, commits, makes the HTTP
    call and only then updates;
  * rows found in `sending` at startup are re-queued once and carry a resend marker (ADR-002
    amendment 1: delivery is at-least-once, a duplicate on the phone is acceptable, a lost alert
    is not);
  * at most one row per (alert_id, kind); a duplicate enqueue returns True;
  * starting the worker twice is a no-op; stop() joins it.

(Engine.stop stopping and joining the worker before db.dispose is in test_us004_engine_wiring.)
The "crash" is simulated by putting a row into `sending` through the contract table, which is
exactly what a process killed between the claim and the response leaves behind.
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any

import pytest
from sqlalchemy import update
from us003_helpers import (
    CHAT,
    TOKEN,
    install_network_guard,
    make_http,
    ok_response,
    telegram,
    visible,
)
from us004_helpers import (
    RESEND,
    TimedRecorder,
    alert_at,
    alert_id,
    build_notifier,
    can_write,
    outbox_table,
    probe,
    read_rows,
    rig_factory,
    signal_notification,
    wait_until,
    when,
)

from onelot.store import init_database


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"the outbox must only use the injected transport: {attempts}"


@pytest.fixture
def make_rig(tmp_path):
    with rig_factory(tmp_path) as build:
        yield build


def _enqueue(rig: Any, i: int = 1, **kw: Any) -> dict[str, Any]:
    alert = alert_at(rig.clock(), id=alert_id(i), **kw)
    rig.enqueue_alert(alert)
    return alert


def _crash_after_claim(rig: Any) -> None:
    """Leave the only row as a killed process would: claimed (`sending`), response unknown."""
    table = outbox_table()
    with rig.db.engine.begin() as conn:
        conn.execute(update(table).values(status="sending", claimed_at=rig.clock(), attempts=1))


# ========================= claim -> commit -> HTTP -> update ============================
def test_the_row_is_claimed_and_committed_before_the_http_call(make_rig):
    seen: dict[str, Any] = {}

    def respond(request):
        seen["rows"] = probe(rig.path, "SELECT status, attempts FROM telegram_outbox")
        seen["claimed"] = probe(rig.path, "SELECT claimed_at FROM telegram_outbox")
        return ok_response(1)

    rig = make_rig(respond)
    _enqueue(rig)

    rig.drain()

    assert [r[0] for r in seen["rows"]] == ["sending"], (
        "when the HTTP call starts, ANOTHER connection must already see the claim (status "
        f"sending): {seen['rows']}"
    )
    assert seen["claimed"][0][0] is not None, "claimed_at is set by the claim"
    assert rig.statuses() == ["sent"], "and only after the response is it updated"


def test_no_transaction_is_open_while_the_http_call_is_in_flight(make_rig):
    seen: dict[str, bool] = {}

    def respond(request):
        seen["writable"] = can_write(rig.path)
        return ok_response(1)

    rig = make_rig(respond)
    _enqueue(rig)

    rig.drain()

    assert seen["writable"] is True, "the worker kept a write transaction open across the HTTP call"


def test_a_row_claimed_by_one_worker_is_never_sent_by_a_second_one(make_rig):
    entered, release = threading.Event(), threading.Event()

    def slow(request):
        entered.set()
        release.wait(30)
        return ok_response(1)

    rig = make_rig(slow)
    second, rec2 = rig.another_outbox()  # built BEFORE the claim: it is a competing worker
    _enqueue(rig)
    first = threading.Thread(target=rig.outbox.drain_once, daemon=True)
    first.start()
    assert entered.wait(30), "the first worker never reached the HTTP call"

    outcome: list[int] = []
    other = threading.Thread(target=lambda: outcome.append(second.drain_once()), daemon=True)
    other.start()
    other.join(10)
    blocked = other.is_alive()
    release.set()
    first.join(30)

    assert not blocked, "the second worker hung: the first one holds a lock across its HTTP call"
    assert outcome == [0] and rec2.calls == 0, "the claimed row must not be sent twice"
    assert rig.rec.calls == 1 and rig.statuses() == ["sent"]


# ========================= restart: sending -> queued once, resend marker ===============
def test_a_row_left_in_sending_is_resent_after_a_restart_with_the_resend_marker(make_rig):
    tg = telegram()
    rig = make_rig()
    alert = _enqueue(rig)
    _crash_after_claim(rig)
    assert rig.statuses() == ["sending"]

    restarted, rec2 = rig.another_outbox()
    restarted.start()
    assert wait_until(lambda: "sending" not in rig.statuses()), "the stale row was never re-queued"
    restarted.stop()
    rig.drain(restarted)

    assert rec2.calls == 1, "re-queued ONCE: the alert is delivered again exactly one time"
    text = rec2.texts()[0]
    assert visible(text).lstrip().startswith(RESEND), f"no resend marker: {text!r}"
    assert tg.format_alert(alert, rig.clock()) in text, "the resent text is the formatted alert"
    assert rig.statuses() == ["sent"]
    assert rig.rec.calls == 0, "the process that crashed made no further call"


def test_the_resend_marker_is_added_once_not_stacked(make_rig):
    rig = make_rig()
    _enqueue(rig)
    _crash_after_claim(rig)
    restarted, rec2 = rig.another_outbox()
    restarted.start()
    assert wait_until(lambda: "sending" not in rig.statuses())
    restarted.stop()
    rig.drain(restarted)

    assert rec2.texts()[0].count(RESEND) == 1


def test_a_row_left_in_sending_whose_alert_has_since_expired_gets_a_missed_note(make_rig):
    tg = telegram()
    rig = make_rig()
    alert = _enqueue(rig, minutes=10)
    _crash_after_claim(rig)
    rig.clock.advance(2 * 3600)  # the machine was off for two hours

    restarted, rec2 = rig.another_outbox()
    restarted.start()
    assert wait_until(lambda: "sending" not in rig.statuses())
    restarted.stop()
    rig.drain(restarted)

    assert rec2.texts() == [tg.format_missed(alert, rig.clock())], "no stale alert is re-sent"
    assert rig.row(kind="alert")["status"] == "skipped"


def test_a_queued_alert_survives_a_real_restart_and_is_sent_once(make_rig):
    rig = make_rig()
    alert = _enqueue(rig)
    rig.db.dispose()  # the process ends

    db2 = init_database(f"sqlite:///{rig.path}")  # a new process opens the same file
    rec2 = TimedRecorder(None, rig.clock)
    client2 = telegram().TelegramClient(TOKEN, CHAT, http=make_http(rec2))
    outbox2 = telegram().TelegramOutbox(db2.engine, client2, clock=rig.clock, sleep=rig.clock.sleep)
    try:
        for _ in range(3):
            outbox2.drain_once()
        rows = read_rows(db2.engine)
    finally:
        outbox2.stop()
        db2.dispose()

    assert [r["status"] for r in rows] == ["sent"] and rec2.calls == 1
    assert rows[0]["alert_id"] == alert["id"]


# ========================= one row per (alert_id, kind) =================================
def test_a_duplicate_enqueue_returns_true_and_keeps_one_row(make_rig):
    rig = make_rig()
    alert = alert_at(rig.clock(), id=alert_id(1))

    assert rig.enqueue_alert(alert) is True
    assert rig.enqueue_alert(alert) is True

    assert len(rig.rows()) == 1
    assert rig.outbox.status()["enqueued"] == 1, "a duplicate is not a new message"


def test_the_same_alert_id_and_kind_with_another_dedup_key_is_still_one_row(make_rig):
    rig = make_rig()
    alert = alert_at(rig.clock(), id=alert_id(1))

    assert rig.enqueue_alert(alert, key="alert:first") is True
    assert rig.enqueue_alert(alert, key="alert:second") is True

    assert len(rig.rows()) == 1


def test_the_same_alert_id_with_a_different_kind_gets_its_own_row(make_rig):
    rig = make_rig()
    alert = alert_at(rig.clock(), id=alert_id(1))
    rig.enqueue_alert(alert)

    assert rig.outbox.enqueue(
        dedup_key="exit:one",
        kind="exit",
        alert_id=alert["id"],
        payload={"advice": "STOP", "position": {"id": "p1"}},
        alert_created_at=rig.clock(),
        expires_at=None,
    )

    assert sorted(r["kind"] for r in rig.rows()) == ["alert", "exit"]


def test_rows_without_an_alert_id_are_told_apart_by_their_dedup_key(make_rig):
    rig = make_rig()

    def enqueue(key: str) -> bool:
        return rig.outbox.enqueue(
            dedup_key=key,
            kind="test",
            alert_id=None,
            payload={"title": key},
            alert_created_at=rig.clock(),
            expires_at=None,
        )

    assert enqueue("a") is True and enqueue("b") is True and enqueue("a") is True
    assert sorted(r["dedup_key"] for r in rig.rows()) == ["a", "b"]


def test_an_alert_that_was_already_sent_is_not_sent_again_by_a_duplicate_enqueue(make_rig):
    rig = make_rig()
    alert = alert_at(rig.clock(), id=alert_id(1))
    rig.enqueue_alert(alert)
    rig.drain()

    assert rig.enqueue_alert(alert) is True
    rig.drain()

    assert rig.rec.calls == 1 and rig.statuses() == ["sent"]


def test_a_duplicate_through_the_channel_is_not_a_channel_failure(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    alert = alert_at(rig.clock(), id=alert_id(1))

    first = notifier.send(signal_notification(alert, rig.clock()))
    again = notifier.send(signal_notification(alert, rig.clock()))  # same signal, new object

    assert first == again == {"dashboard": True, "telegram": True}
    assert len(rig.rows()) == 1
    assert notifier.stats["telegram"].failed == 0


def test_twelve_threads_enqueueing_the_same_alert_leave_one_row_and_all_get_true(make_rig):
    rig = make_rig()
    alert = alert_at(rig.clock(), id=alert_id(1))
    barrier = threading.Barrier(12)
    results: list[bool] = []
    errors: list[BaseException] = []

    def work() -> None:
        try:
            barrier.wait(30)
            results.append(rig.enqueue_alert(alert))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, daemon=True) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    assert not errors, errors
    assert results == [True] * 12
    assert len(rig.rows()) == 1


def test_many_threads_enqueueing_different_alerts_lose_none(make_rig):
    rig = make_rig()
    barrier = threading.Barrier(24)
    results: list[bool] = []
    errors: list[BaseException] = []

    def work(i: int) -> None:
        try:
            barrier.wait(30)
            results.append(rig.enqueue_alert(alert_at(rig.clock(), id=alert_id(i))))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    assert not errors, errors
    assert results == [True] * 24
    assert len(rig.rows()) == 24


# ========================= the worker thread ============================================
def _new_threads(before: set[threading.Thread]) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t not in before and t.is_alive()]


def test_starting_the_worker_twice_starts_one_thread(make_rig):
    rig = make_rig()
    before = set(threading.enumerate())

    rig.outbox.start()
    rig.outbox.start()
    started = _new_threads(before)
    rig.outbox.stop()

    assert len(started) == 1, f"{len(started)} threads after two start() calls: {started}"
    assert not any(t.is_alive() for t in started), "stop() must join the worker"


def test_the_worker_thread_delivers_a_queued_alert_exactly_once_even_if_started_twice(make_rig):
    rig = make_rig()
    _enqueue(rig)

    rig.outbox.start()
    rig.outbox.start()
    assert wait_until(lambda: rig.rec.calls >= 1), "the worker never delivered the alert"
    rig.outbox.stop()

    assert rig.rec.calls == 1 and rig.statuses() == ["sent"]


def test_a_row_enqueued_while_the_worker_runs_is_delivered(make_rig):
    rig = make_rig()
    rig.outbox.start()
    _enqueue(rig, 1)
    assert wait_until(lambda: rig.rec.calls >= 1)
    _enqueue(rig, 2)
    assert wait_until(lambda: rig.rec.calls >= 2)
    rig.outbox.stop()

    assert rig.statuses() == ["sent", "sent"]


def test_stop_joins_the_worker_and_is_safe_to_repeat_or_to_call_without_start(make_rig):
    rig = make_rig()
    rig.outbox.stop()  # never started
    before = set(threading.enumerate())
    rig.outbox.start()
    started = _new_threads(before)
    assert started

    rig.outbox.stop()
    rig.outbox.stop()

    assert not any(t.is_alive() for t in started)


def test_the_first_attempt_time_is_the_injected_clock_not_the_wall_clock(make_rig):
    rig = make_rig()
    far = dt.datetime(2031, 3, 4, 9, 30, tzinfo=rig.clock().tzinfo)
    rig.clock.set(far)
    _enqueue(rig)

    rig.drain()

    (row,) = rig.rows()
    assert when(row["sent_at"]) == far and when(row["created_at"]) == far
