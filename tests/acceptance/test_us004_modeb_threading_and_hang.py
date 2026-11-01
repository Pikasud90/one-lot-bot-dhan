"""US-004 mode B: threads, locks, hung networks and crashes around the outbox worker.

  * the worker never takes the Desk lock, and the Desk can notify under its lock while the
    worker is stuck inside an HTTP call (no transaction spans the call);
  * one hung request holds ONE helper thread and consumes no retry; status() does not wait for
    it; stop() does not hang on it;
  * a result that could not be recorded re-queues the row once at the next drain;
  * a Desk poll loop and the worker share one SQLite file without a single error;
  * an unavailable client leaves the queue untouched;
  * concurrent EXIT advice for one position queues one message.

Real threads appear here, so waits are on events with generous timeouts; nothing asserts how
long anything took. The outbox's `send_deadline_s` keyword (default 15 s) is shortened so a
"hung" request does not make the test wait.
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any

import pytest
from sqlalchemy import event
from us003_helpers import install_network_guard, ok_response, visible
from us004_helpers import (
    RESEND,
    FakeClock,
    OpenClock,
    alert_at,
    alert_id,
    build_desk,
    build_notifier,
    critical_notification,
    exit_notification,
    raise_signal,
    rig_factory,
    wait_until,
    when,
)

from onelot.clock import now_ist
from onelot.notify import telegram_outbox


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"only the injected transport may be used: {attempts}"


@pytest.fixture
def make_rig(tmp_path):
    with rig_factory(tmp_path) as build:
        yield build


def helper_threads(before: set[threading.Thread]) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t not in before and t.is_alive()]


# ========================= the Desk lock ================================================
def test_the_default_send_deadline_is_fifteen_seconds():
    assert telegram_outbox.SEND_DEADLINE_S == 15.0


def test_drain_once_completes_while_another_thread_holds_the_desk_lock(make_rig, monkeypatch):
    clock = FakeClock(now_ist())
    rig = make_rig(clock=clock)
    notifier, _ = build_notifier(rig)
    desk = build_desk(rig.db, notifier, monkeypatch=monkeypatch)
    raise_signal(desk, clock())  # queued; nothing sent yet
    result: list[int] = []
    finished = threading.Event()

    def worker() -> None:
        result.append(rig.outbox.drain_once())
        finished.set()

    thread = threading.Thread(target=worker, daemon=True)
    with desk._lock:  # the Desk is in the middle of a poll
        thread.start()
        completed = finished.wait(30)
    thread.join(30)

    assert completed, "the worker waited for the Desk lock"
    assert result == [1] and rig.statuses() == ["sent"]


def test_the_running_worker_delivers_while_the_desk_lock_is_held(make_rig, monkeypatch):
    clock = FakeClock(now_ist())
    rig = make_rig(clock=clock)
    notifier, _ = build_notifier(rig)
    desk = build_desk(rig.db, notifier, monkeypatch=monkeypatch)
    rig.outbox.start()
    with desk._lock:
        raise_signal(desk, clock())  # the Desk's own call, under its lock
        delivered = wait_until(lambda: rig.rec.calls >= 1)
    rig.outbox.stop()

    assert delivered, "the worker thread could not deliver while the Desk held its lock"
    assert rig.statuses() == ["sent"]


def test_the_desk_can_notify_under_its_lock_while_the_worker_is_stuck_in_http(
    make_rig, monkeypatch
):
    entered, release = threading.Event(), threading.Event()

    def stuck(request: Any) -> Any:
        entered.set()
        release.wait(60)
        return ok_response(1)

    clock = FakeClock(now_ist())
    rig = make_rig(stuck, clock=clock, send_deadline_s=60)  # the worker really is in the call
    notifier, _ = build_notifier(rig)
    desk = build_desk(rig.db, notifier, monkeypatch=monkeypatch)
    rig.enqueue_alert(alert_at(clock(), id=alert_id(1)))
    rig.outbox.start()
    try:
        assert entered.wait(30), "the worker never reached the HTTP call"
        outcome: list[Any] = []
        finished = threading.Event()

        def poll() -> None:
            with desk._lock:  # as Desk.poll does
                sig = raise_signal(desk, clock())
                outcome.append(notifier.send(critical_notification("Feed slow", "x", clock())))
                outcome.append(sig["id"])
            finished.set()

        thread = threading.Thread(target=poll, daemon=True)
        thread.start()
        completed = finished.wait(30)
    finally:
        release.set()
        rig.outbox.stop()

    assert completed, "the Desk's notify blocked behind the worker's HTTP call"
    assert outcome[0].get("telegram") is True


# ========================= a hung transport =============================================
def test_a_hung_request_holds_one_thread_and_consumes_no_retry(make_rig):
    release, started = threading.Event(), threading.Event()
    calls: list[int] = []

    def hang(request: Any) -> Any:
        calls.append(1)
        started.set()
        release.wait(60)
        return ok_response(5)

    rig = make_rig(hang, send_deadline_s=0.2)
    t0 = rig.clock()
    rig.enqueue_alert(alert_at(t0, id=alert_id(1), minutes=120))
    before = set(threading.enumerate())
    try:
        assert rig.outbox.drain_once() == 1  # gives up after the deadline

        row = rig.row()
        assert row["status"] == "queued" and row["attempts"] == 1
        assert "no response within" in (row["last_error"] or "")
        assert when(row["next_attempt_at"]) == t0 + dt.timedelta(seconds=1)  # the normal backoff
        assert len(helper_threads(before)) == 1, "one hung request is one helper thread"
        for _ in range(10):  # an hour of fake time and ten drains: nothing starts
            rig.clock.advance(360)
            assert rig.outbox.drain_once() == 0
        assert len(calls) == 1, "a new request started while the first was still hung"
        assert rig.row()["attempts"] == 1, "a retry was consumed while the network was hung"
        assert len(helper_threads(before)) == 1, "one helper per retry is a thread leak"
    finally:
        release.set()

    assert wait_until(lambda: not helper_threads(before)), "the helper never ended"
    rig.drain()  # the network is back: the row is retried, once
    assert len(calls) == 2 and rig.row()["attempts"] == 2 and rig.statuses() == ["sent"]


def test_status_and_the_notifier_status_do_not_wait_for_a_hung_request(make_rig):
    entered, release = threading.Event(), threading.Event()

    def stuck(request: Any) -> Any:
        entered.set()
        release.wait(60)
        return ok_response(1)

    clock = FakeClock(now_ist())
    rig = make_rig(stuck, clock=clock, send_deadline_s=60)
    notifier, channel = build_notifier(rig)
    rig.enqueue_alert(alert_at(clock(), id=alert_id(1)))
    rig.outbox.start()
    answers: dict[str, Any] = {}
    done = threading.Event()

    def ask() -> None:
        answers["outbox"] = rig.outbox.status()
        answers["channel"] = channel.status()
        answers["notifier"] = notifier.status()
        done.set()

    try:
        assert entered.wait(30)
        thread = threading.Thread(target=ask, daemon=True)
        thread.start()
        answered = done.wait(30)
    finally:
        release.set()
        rig.outbox.stop()

    assert answered, "status() waited for the in-flight HTTP call"
    assert answers["outbox"]["queue_depth"] == 1, "the row in flight still counts as queued"


def test_stop_returns_even_if_a_request_is_hung(make_rig):
    entered, release = threading.Event(), threading.Event()

    def stuck(request: Any) -> Any:
        entered.set()
        release.wait(60)
        return ok_response(1)

    rig = make_rig(stuck, send_deadline_s=0.3)
    rig.enqueue_alert(alert_at(rig.clock(), id=alert_id(1)))
    before = set(threading.enumerate())
    rig.outbox.start()
    try:
        assert entered.wait(30)
        workers = [t for t in helper_threads(before) if t.name == "telegram-outbox"]
        rig.outbox.stop()
    finally:
        release.set()

    assert workers and not any(t.is_alive() for t in workers), "stop() left the worker running"


# ========================= a result that could not be recorded ==========================
def test_a_result_that_could_not_be_recorded_is_requeued_once_at_the_next_drain(make_rig):
    rig = make_rig()
    alert = alert_at(rig.clock(), id=alert_id(1))
    rig.enqueue_alert(alert)
    armed = {"on": True}

    def fail_the_final_update(conn: Any, cursor: Any, statement: str, parameters: Any, *rest: Any):
        if (
            armed["on"]
            and statement.lstrip().upper().startswith("UPDATE")
            and "sent" in (tuple(parameters) if parameters else ())
        ):
            armed["on"] = False
            raise RuntimeError("disk full")

    event.listen(rig.db.engine, "before_cursor_execute", fail_the_final_update)
    try:
        with pytest.raises(RuntimeError):
            rig.outbox.drain_once()  # the HTTP call happened, the update did not
    finally:
        event.remove(rig.db.engine, "before_cursor_execute", fail_the_final_update)
    assert rig.rec.calls == 1 and rig.statuses() == ["sending"], "the row is stuck mid-send"

    rig.clock.advance(2)
    rig.drain()

    assert rig.rec.calls == 2, "the orphaned row must be queued again, not lost"
    assert visible(rig.rec.texts()[1]).lstrip().startswith(RESEND)
    assert rig.statuses() == ["sent"]
    rig.clock.advance(60)
    rig.drain()
    assert rig.rec.calls == 2, "re-queued once, then settled: nothing more happens"


# ========================= heavy contention: a Desk poll loop plus the worker ===========
def test_a_desk_poll_loop_and_the_worker_share_one_sqlite_file_without_errors(
    make_rig, monkeypatch
):
    clock = FakeClock(now_ist())
    rig = make_rig(clock=clock)
    notifier, _ = build_notifier(rig)
    desk = build_desk(rig.db, notifier, monkeypatch=monkeypatch)  # the SAME file as the outbox
    sig = raise_signal(desk, clock())
    rig.drain()  # the SIGNAL goes out first
    pid = desk.acknowledge(sig["id"], "TAKE")["position_id"]
    tracker = desk.trackers["SBIN"]
    tracker.bars.loc[tracker.bars.index[-1], "close"] = desk.positions[pid]["stop_underlying"] - 1
    polls = 120
    errors: list[BaseException] = []
    stop_reading = threading.Event()

    def poller() -> None:
        try:
            for k in range(polls):
                with desk._lock:  # the Desk holds its lock while it writes and notifies
                    desk._manage_positions(clock(), OpenClock().current_window(clock()))
                    notifier.send(critical_notification(f"Feed {k}", "slow", clock()))
                clock.advance(30)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:  # the dashboard polling status
        try:
            while not stop_reading.is_set():
                notifier.status()
                rig.outbox.status()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    rig.outbox.start()
    read = threading.Thread(target=reader, daemon=True)
    poll = threading.Thread(target=poller, daemon=True)
    read.start()
    poll.start()
    poll.join(300)
    settled = wait_until(
        lambda: not [s for s in rig.statuses() if s in ("queued", "sending")], timeout=120
    )
    stop_reading.set()
    read.join(30)
    rig.outbox.stop()

    assert not errors, f"a thread failed: {errors[:3]}"
    assert settled, "rows were left unsent"
    rows = rig.rows()
    critical = [r for r in rows if r["kind"] == "critical"]
    exits = [r for r in rows if r["kind"] == "exit"]
    assert len(critical) == polls and all(r["status"] == "sent" for r in critical)
    # 1 EXIT + 3 reminders were queued. The worker runs concurrently, so a reminder queued
    # while an older one is still unsent supersedes it (decision B): each row is either sent or
    # superseded, the newest is always sent, and nothing is sent twice.
    assert len(exits) == 4 and exits[-1]["status"] == "sent"
    for row in exits:
        assert row["status"] == "sent" or (
            row["status"] == "skipped" and "superseded" in (row["last_error"] or "")
        ), row
    sent_exits = [r for r in exits if r["status"] == "sent"]
    assert rig.rec.calls == 1 + len(critical) + len(sent_exits), "every sent row sent once"
    titles = [t for t in (visible(x) for x in rig.rec.texts()) if "Feed " in t]
    assert len(titles) == len(set(titles)) == polls


# ========================= no client, many threads ======================================
def test_with_an_unavailable_client_nothing_is_sent_and_nothing_is_lost(make_rig):
    rig = make_rig(token="", chat="")  # not configured
    rig.enqueue_alert(alert_at(rig.clock(), id=alert_id(1)))

    assert rig.outbox.drain_once() == 0
    rig.clock.advance(5)
    assert rig.outbox.drain_once() == 0

    assert rig.rec.calls == 0 and rig.statuses() == ["queued"]
    status = rig.outbox.status()
    assert status["configured"] is False and status["queue_depth"] == 1


def test_sixteen_threads_raising_the_same_exit_queue_one_message(make_rig):
    clock = FakeClock(now_ist())
    rig = make_rig(clock=clock)
    notifier, _ = build_notifier(rig)
    barrier = threading.Barrier(16)
    results: list[dict[str, bool]] = []
    errors: list[BaseException] = []

    def poll() -> None:
        try:
            barrier.wait(30)
            results.append(notifier.send(exit_notification("pos000000001", -740.0, clock())))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=poll, daemon=True) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    assert not errors, errors
    assert all(r.get("telegram") is True for r in results) and len(results) == 16
    assert [r["kind"] for r in rig.rows()] == ["exit"], "one first EXIT, however many threads"


def test_three_hundred_queued_messages_are_all_delivered_in_order(make_rig):
    rig = make_rig()
    now = rig.clock()
    for i in range(300):
        assert rig.outbox.enqueue(
            dedup_key=f"critical:{i}",
            kind="critical",
            alert_id=f"c{i:04d}",
            payload={"title": f"Event {i:04d}", "body": ""},
            alert_created_at=now,
            expires_at=None,
        )

    rig.drain(limit=400)

    titles = [visible(t).split("\n")[0] for t in rig.rec.texts()]
    assert titles == [f"CRITICAL {chr(0x00B7)} Event {i:04d}" for i in range(300)]
    assert set(rig.statuses()) == {"sent"}
    assert all(
        b - a >= dt.timedelta(seconds=1) - dt.timedelta(microseconds=1)
        for a, b in zip(rig.rec.times, rig.rec.times[1:], strict=False)
    )
