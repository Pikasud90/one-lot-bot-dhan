"""US-004 developer tests for the outbox and the channel: the boundaries and unhappy paths the
acceptance suite leaves open (rounding, caps, deadlines, restarts, bad input).

A file-based tmp SQLite, a fake clock whose sleep advances it, and httpx.MockTransport: no
network, no real sleeping, no wall-clock assertions.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from onelot.clock import IST
from onelot.config import NotifyCfg
from onelot.notify.base import NOTHING_DUE, Notification, NotificationLevel
from onelot.notify.telegram import SEP, TelegramClient
from onelot.notify.telegram_channel import TelegramChannel
from onelot.notify.telegram_outbox import (
    RESEND_MARKER,
    TelegramOutbox,
    format_critical,
    format_digest,
    latency_ms,
    p95_nearest_rank,
)
from onelot.store import init_database
from onelot.store.schema import telegram_outbox

TOKEN = "123456789:AAH-" + "x" * 35  # fake, the AC4 shape
CHAT = "-1001234567890"
T0 = dt.datetime(2026, 11, 2, 10, 0, tzinfo=IST)
L = NotificationLevel


class Clock:
    def __init__(self, start: dt.datetime = T0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += dt.timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)


def ok(message_id: int = 7) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})


def error(status: int, description: str, **extra: Any) -> httpx.Response:
    return httpx.Response(status, json={"ok": False, "description": description, **extra})


class Rig:
    def __init__(self, path: Path, respond: Callable[[httpx.Request], httpx.Response]) -> None:
        self.db = init_database(f"sqlite:///{path}")
        self.clock = Clock()
        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return respond(request)

        http = httpx.Client(transport=httpx.MockTransport(handler))
        self.client = TelegramClient(TOKEN, CHAT, http=http)
        self.outbox = TelegramOutbox(
            self.db.engine, self.client, clock=self.clock, sleep=self.clock.sleep
        )

    def alert(self, i: int = 1, *, minutes: float = 10, score: Any = 70.0) -> dict[str, Any]:
        return {
            "id": f"{i:06d}abcdef",
            "created_at": self.clock().isoformat(),
            "expires_at": (self.clock() + dt.timedelta(minutes=minutes)).isoformat(),
            "series_key": "SBIN",
            "kind": "STOCK",
            "side": "BUY",
            "risk_allowed": True,
            "confidence": {"score": score, "label": "fair"},
            "data": {"delayed": False},
            "economics": {"entry_price": 800.0, "stop_price": 790.0, "target_price": 820.0},
        }

    def enqueue(self, alert: dict[str, Any], **kw: Any) -> bool:
        return self.outbox.enqueue(
            dedup_key=f"alert:{alert['id']}",
            kind="alert",
            alert_id=alert["id"],
            payload=alert,
            alert_created_at=dt.datetime.fromisoformat(alert["created_at"]),
            expires_at=dt.datetime.fromisoformat(alert["expires_at"]),
            **kw,
        )

    def drain(self) -> int:
        total = 0
        for _ in range(100):
            done = self.outbox.drain_once()
            if not done:
                return total
            total += done
        raise AssertionError("drain_once never ran dry")

    def rows(self) -> list[dict[str, Any]]:
        with self.db.engine.connect() as conn:
            result = conn.execute(select(telegram_outbox).order_by(telegram_outbox.c.id))
            return [dict(r) for r in result.mappings()]


@pytest.fixture
def make_rig(tmp_path: Path) -> Iterator[Callable[..., Rig]]:
    made: list[Rig] = []

    def build(respond: Callable[[httpx.Request], httpx.Response] = lambda r: ok()) -> Rig:
        rig = Rig(tmp_path / f"o{len(made)}.db", respond)
        made.append(rig)
        return rig

    yield build
    for rig in made:
        rig.outbox.stop()
        rig.db.dispose()


# ========================= pure helpers ================================================
@pytest.mark.parametrize(
    "micros, expected",
    [(0, 0), (499, 0), (500, 1), (1_499, 1), (1_500, 2), (-1, 0), (-10_000_000, 0)],
)
def test_latency_rounds_half_up_on_whole_microseconds_and_never_goes_negative(micros, expected):
    created = T0
    assert latency_ms(created + dt.timedelta(microseconds=micros), created) == expected


@pytest.mark.parametrize(
    "values, expected",
    [([], None), ([5], 5), ([3, 1, 2], 3), (list(range(1, 21)), 19), (list(range(1, 101)), 95)],
)
def test_p95_is_the_nearest_rank(values, expected):
    assert p95_nearest_rank(values) == expected


def test_format_critical_escapes_cuts_and_keeps_the_title():
    text = format_critical({"title": "Feed <x> & y", "body": "z" * 20_000}, T0)
    assert text.startswith("<b>") and "Feed &lt;x&gt; &amp; y" in text
    assert len(text) <= 4096


def test_format_critical_survives_a_payload_without_fields():
    assert "<b>" in format_critical({}, T0)


def test_a_digest_lists_at_most_ten_alerts_and_says_how_many_it_covers():
    alerts = [{"id": f"{i:06d}zz", "series_key": "SBIN", "side": "BUY"} for i in range(30)]
    text = format_digest({"reason": "burst", "alerts": alerts}, T0)
    assert "30 more alerts" in text
    assert text.count("<code>") == 10 and "and 20 more" in text
    assert len(text) <= 4096


@pytest.mark.parametrize(
    ("score", "shown"),
    [(64.6, "Conf 64"), (99.99, "Conf 99"), (10**400, "Conf 100"), ("high", "Conf n/a")],
    ids=["64.6", "99.99", "10**400", "text"],
)
def test_a_digest_shows_the_same_floored_confidence_as_the_alert(score, shown):
    """US-003 BUG-4: one number everywhere (notify.base.shown_confidence), never rounded up
    across a label threshold, and a score beyond float range never raises."""
    alert = {"id": "000001zz", "series_key": "SBIN", "side": "BUY", "confidence": {"score": score}}
    text = format_digest({"reason": "burst", "alerts": [alert]}, T0)
    assert f"{SEP}{shown}{SEP}" in text


# ========================= enqueue: input checks =========================================
def test_an_unknown_kind_is_refused_before_any_sql(make_rig):
    rig = make_rig()
    with pytest.raises(ValueError, match="kind"):
        rig.outbox.enqueue(
            dedup_key="k",
            kind="bogus",  # type: ignore[arg-type]
            alert_id=None,
            payload={},
            alert_created_at=T0,
            expires_at=None,
        )
    assert rig.rows() == []


def test_non_json_values_are_stored_as_text_and_booleans_stay_booleans(make_rig):
    rig = make_rig()
    alert = rig.alert()
    alert["extra"] = {"when": T0, "flag": False}

    assert rig.enqueue(alert) is True

    stored = rig.rows()[0]["payload"]
    assert stored["extra"] == {"when": T0.isoformat(), "flag": False}
    assert stored["risk_allowed"] is True


def test_a_score_beyond_float_range_is_still_queued(make_rig):
    """US-003 BUG-5 class: an absurd int score must not make the INSERT raise (the phone
    would lose the alert); it is stored as is and ranked like an unreadable one."""
    rig = make_rig()

    assert rig.enqueue(rig.alert(score=10**400)) is True

    (row,) = rig.rows()
    assert row["confidence"] is None and row["payload"]["confidence"]["score"] == 10**400


@pytest.mark.parametrize(
    "score",
    [float("nan"), float("inf"), True, "high", None, 10**400],
    ids=["nan", "inf", "bool", "text", "none", "10**400"],
)
def test_a_burst_ranks_an_unreadable_confidence_last(make_rig, score):
    rig = make_rig()
    rig.enqueue(rig.alert(1, score=score))
    rig.enqueue(rig.alert(2, score=10.0))

    rig.drain()

    first_text = rig.requests[0].read().decode()
    assert "000002" in first_text


# ========================= 429, deadline, unavailable ===================================
def test_a_huge_retry_after_is_capped_at_an_hour(make_rig):
    rig = make_rig(lambda r: error(429, "slow", parameters={"retry_after": 999_999}))
    rig.enqueue(rig.alert(minutes=600))

    rig.outbox.drain_once()

    assert rig.rows()[0]["next_attempt_at"] == T0 + dt.timedelta(hours=1)


def test_a_429_pauses_every_row_not_only_the_one_that_hit_it(make_rig):
    replies = iter([error(429, "slow", parameters={"retry_after": 5}), ok(1), ok(2)])
    rig = make_rig(lambda r: next(replies))
    rig.enqueue(rig.alert(1, score=90))
    rig.enqueue(rig.alert(2, score=10))

    assert rig.outbox.drain_once() == 1
    assert rig.outbox.drain_once() == 0, "flood control applies to the whole chat"
    rig.clock.advance(5)
    rig.drain()

    assert [r["status"] for r in rig.rows()] == ["sent", "sent"]


def test_a_call_that_outlives_the_deadline_is_a_transient_failure(make_rig, tmp_path):
    release = threading.Event()

    def hang(request: httpx.Request) -> httpx.Response:
        release.wait(30)
        return ok()

    rig = make_rig(hang)
    quick = TelegramOutbox(
        rig.db.engine, rig.client, clock=rig.clock, sleep=rig.clock.sleep, send_deadline_s=0.05
    )
    rig.enqueue(rig.alert(minutes=30))
    try:
        assert quick.drain_once() == 1
    finally:
        release.set()

    row = rig.rows()[0]
    assert row["status"] == "queued" and row["attempts"] == 1
    assert "no response within" in row["last_error"]
    assert row["next_attempt_at"] == T0 + dt.timedelta(seconds=1)


def _join_send_helpers() -> None:
    for thread in threading.enumerate():
        if thread.name == "telegram-send":
            thread.join(10)


def test_a_hung_request_is_the_only_one_in_flight(make_rig):
    """A request that outlived its deadline keeps its helper thread until httpx gives up.
    No second request (and no second thread) starts while it lives, and no row is claimed
    in the meantime, so a hung network never piles up threads or burns attempts."""
    release = threading.Event()

    def hang(request: httpx.Request) -> httpx.Response:
        release.wait(30)
        return ok()

    rig = make_rig(hang)
    quick = TelegramOutbox(
        rig.db.engine, rig.client, clock=rig.clock, sleep=rig.clock.sleep, send_deadline_s=0.05
    )
    rig.enqueue(rig.alert(1, minutes=30, score=90))
    rig.enqueue(rig.alert(2, minutes=30, score=10))
    try:
        assert quick.drain_once() == 1
        rig.clock.advance(5)
        assert quick.drain_once() == 0, "nothing is claimed while the hung request lives"
        assert len(rig.requests) == 1
        assert [r["attempts"] for r in rig.rows()] == [1, 0]
        alive = [t for t in threading.enumerate() if t.name == "telegram-send"]
        assert len(alive) == 1
    finally:
        release.set()
    _join_send_helpers()

    assert quick.drain_once() == 1 and quick.drain_once() == 1
    assert [r["status"] for r in rig.rows()] == ["sent", "sent"]


def test_status_never_waits_for_a_request_in_flight(make_rig):
    """status() is read by the dashboard thread: it must not queue behind the drain lock
    while the worker waits on HTTP."""
    started, release = threading.Event(), threading.Event()

    def slow(request: httpx.Request) -> httpx.Response:
        started.set()
        release.wait(30)
        return ok()

    rig = make_rig(slow)
    rig.enqueue(rig.alert(minutes=30))
    drainer = threading.Thread(target=rig.outbox.drain_once)
    drainer.start()
    try:
        assert started.wait(10)
        status = rig.outbox.status()
        assert drainer.is_alive(), "status() returned while the request was in flight"
        assert status["queue_depth"] == 1 and status["last_error"] is None
    finally:
        release.set()
        drainer.join(10)
    _join_send_helpers()


def test_an_unavailable_client_claims_nothing(make_rig):
    rig = make_rig()
    rig.enqueue(rig.alert())
    dead = TelegramOutbox(
        rig.db.engine, TelegramClient("", ""), clock=rig.clock, sleep=rig.clock.sleep
    )

    assert dead.drain_once() == 0
    assert [r["status"] for r in rig.rows()] == ["queued"]
    assert dead.status()["configured"] is False


def test_an_alert_that_expires_during_the_pacing_wait_becomes_a_missed_note(make_rig):
    rig = make_rig()
    rig.enqueue(rig.alert(1, minutes=10))
    rig.drain()
    rig.clock.advance(0.5)
    second = rig.alert(2, minutes=0.25 / 60)  # 0.25 s of life: expires during the 0.5 s wait
    rig.enqueue(second)

    rig.drain()

    kinds = [(r["kind"], r["status"]) for r in rig.rows()]
    assert kinds == [("alert", "sent"), ("alert", "skipped"), ("missed", "sent")]


def test_a_payload_the_formatter_cannot_render_fails_the_row_instead_of_looping(
    make_rig, monkeypatch
):
    import onelot.notify.telegram_outbox as module

    def broken(alert: Any, now: Any) -> str:
        raise KeyError("x")

    monkeypatch.setattr(module, "format_alert", broken)
    rig = make_rig()
    rig.enqueue(rig.alert())

    rig.drain()

    row = rig.rows()[0]
    assert row["status"] == "failed" and "KeyError" in row["last_error"]
    assert rig.requests == []


def test_a_missed_note_and_a_digest_never_carry_the_resend_marker(make_rig):
    rig = make_rig()
    rig.enqueue(rig.alert(1, minutes=1))
    with rig.db.engine.begin() as conn:
        conn.execute(telegram_outbox.update().values(status="sending"))
    rig.clock.advance(3600)

    rig.outbox.start()
    rig.outbox.stop()
    rig.drain()

    (request,) = rig.requests
    assert RESEND_MARKER not in request.read().decode("utf-8")


# ========================= the channel ===================================================
def channel_for(rig: Rig, **cfg: Any) -> TelegramChannel:
    return TelegramChannel(
        rig.client, rig.outbox, NotifyCfg(**cfg), lambda moment: True, clock=rig.clock
    )


def exit_note(pid: str = "p1", advice: str = "STOP") -> Notification:
    return Notification(
        title="EXIT",
        body="",
        level=L.EXIT,
        position_id=pid,
        created_at=T0,
        payload={"advice": advice, "position": {"series_key": "SBIN", "kind": "STOCK"}},
        dedup_key=f"exit:{pid}",
    )


def test_exit_numbers_continue_after_a_restart_so_a_new_episode_is_not_swallowed(make_rig):
    rig = make_rig()
    first = channel_for(rig)
    assert first.send(exit_note()) is True
    restarted = channel_for(rig)  # a new process: the in-memory budget is gone

    assert restarted.send(exit_note()) is True

    assert [r["alert_id"] for r in rig.rows()] == ["p1#1", "p1#2"]


def raise_every_minute(channel: TelegramChannel, rig: Rig, minutes: int, **kw: Any) -> None:
    """The Desk's poll: the same advice raised once a minute, `minutes` + 1 times."""
    for k in range(minutes + 1):
        if k:
            rig.clock.advance(60)
        outcome = channel.send(exit_note(**kw))
        assert outcome is True or outcome is NOTHING_DUE, "a suppressed reminder is not a failure"


def test_an_exhausted_budget_stays_quiet_while_the_advice_persists(make_rig):
    """As decided: 1 message + 3 reminders per advice while the Desk keeps raising it, hours
    on end; a different advice (or a restart) opens a new budget."""
    rig = make_rig()
    channel = channel_for(rig)

    raise_every_minute(channel, rig, 4 * 60)

    assert len(rig.rows()) == 4
    channel.send(exit_note(advice="TARGET"))
    assert [r["alert_id"] for r in rig.rows()][-1] == "p1#5"


def test_an_advice_absent_for_an_hour_then_raised_again_starts_a_fresh_budget(make_rig):
    """Manager decision (1) after T1: the same advice back after at least 60 minutes without
    it is a new episode: a first message at once, then its own 3 reminders."""
    rig = make_rig()
    channel = channel_for(rig)
    raise_every_minute(channel, rig, 30)  # 0, 5, 10, 15 min; the last raise at 30 min
    assert len(rig.rows()) == 4

    rig.clock.now = T0 + dt.timedelta(minutes=90)  # absent 60 minutes
    raise_every_minute(channel, rig, 30)

    rows = rig.rows()
    assert [r["alert_id"] for r in rows[4:]] == ["p1#5", "p1#6", "p1#7", "p1#8"]
    assert [r["created_at"] for r in rows[4:]] == [
        T0 + dt.timedelta(minutes=m) for m in (90, 95, 100, 105)
    ]


@pytest.mark.parametrize(("gap_s", "rows"), [(3599, 4), (3600, 5)])
def test_the_absence_that_restarts_the_budget_is_at_least_sixty_minutes(make_rig, gap_s, rows):
    rig = make_rig()
    channel = channel_for(rig)
    raise_every_minute(channel, rig, 30)

    rig.clock.now = T0 + dt.timedelta(minutes=30, seconds=gap_s)
    channel.send(exit_note())

    assert len(rig.rows()) == rows


def test_a_shorter_absence_continues_the_same_budget(make_rig):
    """STOP raised at 0-5 min (2 messages), gone for 59 minutes, back at 64 min: the next two
    messages are the remaining reminders, then the phone goes quiet."""
    rig = make_rig()
    channel = channel_for(rig)
    raise_every_minute(channel, rig, 5)
    assert len(rig.rows()) == 2

    rig.clock.now = T0 + dt.timedelta(minutes=64)
    raise_every_minute(channel, rig, 60)

    assert [r["created_at"] for r in rig.rows()][2:] == [
        T0 + dt.timedelta(minutes=m) for m in (64, 69)
    ]


def test_a_session_probe_that_raises_never_mutes_an_alert(make_rig):
    rig = make_rig()

    def broken(moment: dt.datetime) -> bool:
        raise RuntimeError("calendar missing")

    channel = TelegramChannel(rig.client, rig.outbox, NotifyCfg(), broken, clock=rig.clock)

    assert channel.send(exit_note()) is True
    assert rig.rows()[0]["silent"] is False


def test_the_channel_declines_levels_outside_the_config_without_touching_the_db(make_rig):
    rig = make_rig()
    channel = channel_for(rig, telegram_levels=["EXIT"])
    signal = Notification(title="s", body="", level=L.SIGNAL, signal_id="s1", created_at=T0)

    assert channel.accepts(signal) is False and channel.accepts(exit_note()) is True
    assert channel.send(Notification(title="i", body="", level=L.INFO)) is False
    assert rig.rows() == []


def test_a_worker_whose_drain_keeps_failing_logs_the_streak_once(make_rig, monkeypatch, caplog):
    import time as real_time

    import onelot.notify.telegram_outbox as module

    rig = make_rig()
    calls: list[int] = []

    def broken() -> int:
        calls.append(1)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(module, "IDLE_WAIT_S", 0.005)
    monkeypatch.setattr(rig.outbox, "drain_once", broken)
    with caplog.at_level("WARNING", logger=module.__name__):
        rig.outbox.start()
        deadline = real_time.monotonic() + 20
        while len(calls) < 5 and real_time.monotonic() < deadline:
            real_time.sleep(0.01)
        rig.outbox.stop()

    assert len(calls) >= 5
    failures = [r for r in caplog.records if "drain failed" in r.getMessage()]
    assert len(failures) == 1, "one line per streak, not one per second"


def test_a_suppressed_exit_reminder_is_not_counted_as_enqueued(make_rig):
    from types import SimpleNamespace

    from onelot.notify.manager import Notifier

    rig = make_rig()
    notifier = Notifier(SimpleNamespace(notify=NotifyCfg(os_toast=False)), dedup_seconds=0.0)
    notifier.add_channel(channel_for(rig))

    for k in range(31):  # the Desk's poll, once a minute for half an hour
        if k:
            rig.clock.advance(60)
        assert notifier.send(exit_note()) == {"dashboard": True, "telegram": True}

    assert len(rig.rows()) == 4
    assert notifier.stats["telegram"].enqueued == 4, "27 polls queued nothing"
    assert notifier.stats["telegram"].failed == 0


def _notifier_with(channel: TelegramChannel) -> Any:
    from types import SimpleNamespace

    from onelot.notify.manager import Notifier

    notifier = Notifier(SimpleNamespace(notify=NotifyCfg(os_toast=False)), dedup_seconds=0.0)
    notifier.add_channel(channel)
    return notifier


def _signal_note(i: int, rig: Rig) -> Notification:
    alert = rig.alert(i)
    return Notification(
        title="s", body="", level=L.SIGNAL, signal_id=alert["id"], created_at=T0, payload=alert
    )


def _telegram_entry(notifier: Any) -> dict[str, Any]:
    return next(c for c in notifier.status()["channels"] if c["name"] == "telegram")


def test_enqueue_failures_stay_visible_in_the_merged_status(make_rig, monkeypatch):
    """The outbox's own failed/last_error describe delivery; a database error while queueing
    must not hide behind them as 'failed 0'."""
    from sqlalchemy.exc import OperationalError

    rig = make_rig()
    channel = channel_for(rig)
    notifier = _notifier_with(channel)
    real_enqueue = rig.outbox.enqueue

    def locked(**kwargs: Any) -> bool:
        raise OperationalError("INSERT INTO telegram_outbox", {"risk_reason": "x"}, Exception())

    monkeypatch.setattr(rig.outbox, "enqueue", locked)
    assert notifier.send(_signal_note(1, rig))["telegram"] is False
    monkeypatch.setattr(rig.outbox, "enqueue", lambda **kwargs: False)
    assert notifier.send(_signal_note(2, rig))["telegram"] is False

    entry = _telegram_entry(notifier)
    assert entry["enqueue_failed"] == 2 and entry["failed"] == 0
    assert entry["enqueue_last_error"] == "not queued"
    assert "INSERT" not in str(entry) and "risk_reason" not in str(entry)

    monkeypatch.setattr(rig.outbox, "enqueue", locked)
    notifier.send(_signal_note(3, rig))
    assert _telegram_entry(notifier)["enqueue_last_error"] == "OperationalError"

    monkeypatch.setattr(rig.outbox, "enqueue", real_enqueue)
    assert notifier.send(_signal_note(4, rig))["telegram"] is True
    entry = _telegram_entry(notifier)
    assert entry["enqueue_failed"] == 3, "the count is history"
    assert entry["enqueue_last_error"] is None, "a healthy enqueue clears the current error"


def test_enqueue_failures_are_reported_even_when_the_outbox_status_fails(make_rig):
    rig = make_rig()
    notifier = _notifier_with(channel_for(rig))
    with rig.db.engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE telegram_outbox")

    notifier.send(_signal_note(1, rig))

    entry = _telegram_entry(notifier)
    assert entry["enqueue_failed"] == 1 and entry["enqueue_last_error"] == "OperationalError"
    assert entry["status_error"] == "OperationalError"


def test_a_successful_send_clears_the_last_error(make_rig):
    replies = iter([error(500, "boom"), ok(1)])
    rig = make_rig(lambda r: next(replies))
    rig.enqueue(rig.alert(minutes=30))

    rig.drain()
    assert "HTTP 500" in rig.outbox.status()["last_error"]
    rig.clock.advance(1)
    rig.drain()

    status = rig.outbox.status()
    assert status["sent"] == 1 and status["last_error"] is None


def _fail_next_update_to(rig: Rig, status: str) -> list[str]:
    """Make the NEXT UPDATE that writes `status` raise, as a locked or vanished database
    would after the HTTP call already succeeded. Returns the list of statements it hit."""
    from sqlalchemy import event

    hit: list[str] = []

    def before(conn: Any, cursor: Any, statement: str, params: Any, *rest: Any) -> None:
        if not hit and statement.lstrip().startswith("UPDATE") and status in tuple(params):
            hit.append(statement)
            raise RuntimeError("database is locked")

    event.listen(rig.db.engine, "before_cursor_execute", before)
    return hit


@pytest.mark.parametrize("kind, marked", [("alert", True), ("missed", False)])
def test_a_row_whose_result_was_not_recorded_is_requeued_once_on_the_next_drain(
    make_rig, kind, marked
):
    """The HTTP call succeeded but recording it failed: the row is still 'sending' and no
    request is in flight. The next drain re-queues it (at-least-once) with the resend marker,
    as a restart would (never on a note about other rows)."""
    rig = make_rig()
    alert = rig.alert(minutes=30)
    rig.outbox.enqueue(
        dedup_key=f"{kind}:1",
        kind=kind,
        alert_id=alert["id"],
        payload=alert,
        alert_created_at=T0,
        expires_at=None,
    )
    hit = _fail_next_update_to(rig, "sent")

    with pytest.raises(RuntimeError):
        rig.outbox.drain_once()
    assert hit and [r["status"] for r in rig.rows()] == ["sending"]

    rig.drain()

    (row,) = rig.rows()
    assert row["status"] == "sent" and len(rig.requests) == 2
    second = rig.requests[1].read().decode("utf-8")
    assert (RESEND_MARKER in second) is marked
    rig.clock.advance(3600)
    assert rig.drain() == 0 and len(rig.requests) == 2, "re-queued once, not forever"


def test_another_outbox_never_requeues_a_row_it_did_not_claim(make_rig):
    rig = make_rig()
    rig.enqueue(rig.alert(minutes=30))
    hit = _fail_next_update_to(rig, "sent")
    with pytest.raises(RuntimeError):
        rig.outbox.drain_once()
    assert hit
    other = TelegramOutbox(rig.db.engine, rig.client, clock=rig.clock, sleep=rig.clock.sleep)

    assert other.drain_once() == 0

    assert [r["status"] for r in rig.rows()] == ["sending"], "only its claimer knows it is lost"


def _legacy_signal(ttl_s: int = 600) -> Notification:
    """What Notifier.signal() sends for the legacy Bank Nifty engine: Signal.as_dict() names
    no symbol or side, so the channel stores the title and body instead."""
    return Notification(
        title="BUY BANKNIFTY 52000 CE x1 @ ~126.00",
        body="Bank Nifty breakout; stop 98.00",
        level=L.SIGNAL,
        signal_id="legacy000001",
        ttl_seconds=ttl_s,
        created_at=T0,
        payload={"signal_id": "legacy000001", "strike": 52000.0, "opt_type": "CE"},
        dedup_key="signal:legacy000001",
    )


@pytest.mark.parametrize(
    "after_s, line",
    [
        (0, "Valid till 10:10 IST (10m left)"),
        (180, "Valid till 10:10 IST (7m left)"),  # formatted when sent, never stale
        (545, "Valid till 10:10 IST (expired)"),  # under a minute left reads expired
    ],
)
def test_a_legacy_signal_shows_its_validity_like_a_desk_alert(make_rig, after_s, line):
    rig = make_rig()
    channel = channel_for(rig)
    assert channel.send(_legacy_signal()) is True
    rig.clock.advance(after_s)

    rig.drain()

    (request,) = rig.requests
    text = __import__("json").loads(request.read())["text"]
    assert text.startswith("<b>BUY BANKNIFTY 52000 CE x1 @ ~126.00</b>")
    assert line in text.splitlines()
    assert text.splitlines()[-1].startswith("Time 10:00:00 IST"), "the event time stays last"


def test_a_legacy_exit_note_has_no_validity_line(make_rig):
    rig = make_rig()
    channel = channel_for(rig)
    legacy_exit = Notification(
        title="EXIT 52000 CE - STOP",
        body="close it",
        level=L.EXIT,
        position_id="legacy-pos-1",
        created_at=T0,
        payload={"reason": "STOP"},
        dedup_key="exit:legacy-pos-1",
    )
    assert channel.send(legacy_exit) is True

    rig.drain()

    (request,) = rig.requests
    assert "Valid till" not in __import__("json").loads(request.read())["text"]
