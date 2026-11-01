"""US-004 mode B: the Manager's review-round decisions (A) to (D), (F) and (G), end to end.

  (A) EXIT and CRITICAL texts carry the time of the EVENT; delivered more than 60 s after it
      they add " · sent late HH:MM". SIGNAL stays formatted at send time.
  (B) a newer queued EXIT supersedes the older unsent EXITs for the same position (skipped,
      'superseded'): an outage gives the phone one EXIT, not a burst of stale ones.
  (C) missed notes that pile up while the worker drains through an outage become ONE
      "Missed N alerts" digest (more than 8); up to 8 stay individual notes.
  (D) p95 counts alert, exit and critical rows only.
  (F) every CRITICAL is its own row; the worker skips a queued one whose title (spacing
      normalised, case kept) matches one SENT less than 30 minutes ago: a sliding window from the
      last message the phone got; a failed CRITICAL never silences the next.
  (G) the worker paces BEFORE it claims: a failure in the wait leaves the row queued.

Outages are a transport that answers 502; the worker "drains throughout" by running
drain_once every simulated half minute.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import pytest
from us003_helpers import DOT, install_network_guard, ok_response, telegram, tg_error, visible
from us004_helpers import (
    RESEND,
    FakeClock,
    Outage,
    alert_at,
    alert_id,
    build_notifier,
    code_ids,
    critical_notification,
    exit_notification,
    exit_payload,
    rig_factory,
    scripted,
    signal_notification,
)

from onelot.notify.telegram_outbox import (
    SENT_LATE_AFTER,
    format_critical,
)

PID = "pos000000001"
DIGEST_LINE = re.compile(r"^Missed (\d+) alerts?\b")
NOTE_LINE = re.compile(rf"^Missed {DOT} ")  # format_missed: one alert, "Missed · BUY ..."


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"only the injected transport may be used: {attempts}"


@pytest.fixture
def make_rig(tmp_path):
    with rig_factory(tmp_path) as build:
        yield build


def late_suffix(moment: dt.datetime) -> str:
    return f" {DOT} sent late {moment.astimezone(moment.tzinfo).strftime('%H:%M')}"


# ========================= (A) the event time and "sent late" ===========================
@pytest.mark.parametrize(
    "delay_s, late",
    [(0.0, False), (59.999, False), (60.0, False), (60.001, True), (61.0, True), (3600.0, True)],
)
def test_an_exit_shows_the_event_time_and_says_sent_late_only_after_sixty_seconds(
    make_rig, delay_s, late
):
    tg = telegram()
    rig = make_rig()
    t0 = rig.clock()
    notifier, _ = build_notifier(rig)
    notifier.send(exit_notification(PID, 1234.0, t0))
    rig.clock.advance(delay_s)
    sent_at = rig.clock()

    rig.drain()

    (text,) = rig.rec.texts()
    expected = tg.format_exit(exit_payload(PID, 1234.0), t0)
    assert text == expected + (late_suffix(sent_at) if late else "")
    assert f"Time {t0.strftime('%H:%M:%S')} IST" in text, "the Time line is the event's"


@pytest.mark.parametrize(
    "delay_s, late", [(0.0, False), (60.0, False), (60.001, True), (900.0, True)]
)
def test_a_critical_shows_the_event_time_and_says_sent_late_only_after_sixty_seconds(
    make_rig, delay_s, late
):
    rig = make_rig()
    t0 = rig.clock()
    notifier, _ = build_notifier(rig)
    notifier.send(critical_notification("Dhan token expired", "renew it", t0))
    rig.clock.advance(delay_s)
    sent_at = rig.clock()

    rig.drain()

    (text,) = rig.rec.texts()
    expected = format_critical({"title": "Dhan token expired", "body": "renew it"}, t0)
    assert text == expected + (late_suffix(sent_at) if late else "")


def test_the_sent_late_threshold_is_sixty_seconds():
    assert SENT_LATE_AFTER == dt.timedelta(seconds=60)


def test_a_signal_is_still_formatted_at_send_time_and_never_says_sent_late(make_rig):
    tg = telegram()
    rig = make_rig()
    t0 = rig.clock()
    alert = alert_at(t0, id=alert_id(1), minutes=30)
    notifier, _ = build_notifier(rig)
    notifier.send(signal_notification(alert, t0))
    rig.clock.advance(600)  # ten minutes late
    sent_at = rig.clock()

    rig.drain()

    (text,) = rig.rec.texts()
    assert text == tg.format_alert(alert, sent_at), "the time left is the current one"
    assert "sent late" not in text


# ========================= (A)+(B) an outage while the EXIT advice is raised ============
def test_an_outage_while_exit_reminders_are_raised_gives_one_exit_with_its_event_time(make_rig):
    """The advice is raised every minute for 20 minutes while Telegram is down: four EXIT rows
    are queued (the first and 3 reminders). When it is back the phone gets ONE message, the
    newest, stamped with its own event time and 'sent late', not four stale ones."""
    tg = telegram()
    outage = Outage()
    rig = make_rig(outage)
    notifier, _ = build_notifier(rig)
    t0 = rig.clock()
    last_event = None
    for minute in range(0, 21):
        rig.clock.set(t0 + dt.timedelta(minutes=minute))
        result = notifier.send(exit_notification(PID, 1000.0 + minute, rig.clock()))
        assert result.get("telegram") is True
        if minute == 15:
            last_event = (exit_payload(PID, 1000.0 + minute), rig.clock())  # the 4th message
        rig.drain()  # the worker drains throughout; every attempt fails

    assert outage.delivered == [] and "sent" not in rig.statuses()
    outage.down = False
    rig.clock.advance(61)
    recovered_at = rig.clock()
    rig.drain()

    exits = [r for r in rig.rows() if r["kind"] == "exit"]
    assert [r["status"] for r in exits] == ["skipped", "skipped", "skipped", "sent"]
    assert all("superseded" in (r["last_error"] or "") for r in exits[:3])
    (text,) = outage.delivered
    payload, event_at = last_event
    assert text == tg.format_exit(payload, event_at) + late_suffix(recovered_at)
    assert rig.outbox.status()["queue_depth"] == 0


# ========================= (B) superseded EXITs =========================================
def test_a_newer_exit_supersedes_the_older_unsent_ones_for_the_same_position_only(make_rig):
    outage = Outage()
    rig = make_rig(outage)
    notifier, _ = build_notifier(rig)
    t0 = rig.clock()
    for second, pid, pnl in [
        (0, "posAAAAAAAAA", 111.0),
        (10, "posBBBBBBBBB", 222.0),
        (300, "posAAAAAAAAA", 333.0),  # A's reminder
        (310, "posBBBBBBBBB", 444.0),  # B's reminder (B's first one is 5 min old)
    ]:
        rig.clock.set(t0 + dt.timedelta(seconds=second))
        notifier.send(exit_notification(pid, pnl, rig.clock()))
        rig.drain()
    outage.down = False
    rig.clock.advance(61)

    rig.drain()

    texts = outage.delivered
    assert len(texts) == 2, "one EXIT per position"
    assert any("+333" in visible(t) for t in texts) and any("+444" in visible(t) for t in texts)
    assert not any("+111" in visible(t) or "+222" in visible(t) for t in texts)


def test_a_different_advice_for_the_same_position_also_supersedes_the_unsent_one(make_rig):
    outage = Outage()
    rig = make_rig(outage)
    notifier, _ = build_notifier(rig)
    t0 = rig.clock()
    for second, advice in [(0, "STOP"), (60, "TARGET")]:
        rig.clock.set(t0 + dt.timedelta(seconds=second))
        notifier.send(exit_notification(PID, 500.0, rig.clock(), advice))
        rig.drain()
    outage.down = False
    rig.clock.advance(61)

    rig.drain()

    (text,) = outage.delivered
    assert "Reason: TARGET" in visible(text) and "STOP" not in visible(text)


def test_exits_that_were_already_sent_are_never_touched_by_a_later_one(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    t0 = rig.clock()
    notifier.send(exit_notification(PID, 100.0, t0))
    rig.drain()
    rig.clock.advance(300)
    notifier.send(exit_notification(PID, 200.0, rig.clock()))
    rig.drain()

    assert [r["status"] for r in rig.rows() if r["kind"] == "exit"] == ["sent", "sent"]
    assert len(rig.rec.texts()) == 2


# ========================= (C) missed notes while the worker drains =====================
def drain_through_outage(rig: Any, minutes: int, per_minute: int = 2) -> None:
    for _ in range(minutes * per_minute):
        rig.drain()
        rig.clock.advance(60 / per_minute)


@pytest.mark.parametrize("count", [8, 9])
def test_up_to_eight_missed_notes_stay_individual_and_nine_become_one_digest(make_rig, count):
    outage = Outage()
    rig = make_rig(outage)
    alerts = []
    for minute in range(count):  # one alert a minute, valid 5 minutes, the worker drains
        alert = alert_at(rig.clock(), id=alert_id(minute), score=40.0 + minute, minutes=5)
        alerts.append(alert)
        rig.enqueue_alert(alert)
        drain_through_outage(rig, 1)
    drain_through_outage(rig, 10)  # everything has expired by now
    outage.down = False
    rig.clock.advance(120)  # past the retry back-off (at most 60 s) of the notes that failed

    rig.drain(limit=100)

    plain = [visible(t).split("\n")[0] for t in outage.delivered]
    digests = [m for m in (DIGEST_LINE.match(p) for p in plain) if m]
    notes = [p for p in plain if NOTE_LINE.match(p)]
    if count <= 8:
        assert len(notes) == count and not digests, plain
    else:
        assert len(digests) == 1 and int(digests[0].group(1)) == count and not notes, plain
    assert len(plain) == (count if count <= 8 else 1)


def test_draining_throughout_a_two_hour_outage_gives_one_digest_and_a_bounded_queue(make_rig):
    outage = Outage()
    rig = make_rig(outage)
    alerts, deepest = [], 0
    for minute in range(120):  # an alert a minute for two hours, each valid 5 minutes
        alert = alert_at(rig.clock(), id=alert_id(minute), score=40.0 + minute % 50, minutes=5)
        alerts.append(alert)
        rig.enqueue_alert(alert)
        drain_through_outage(rig, 1)
        deepest = max(deepest, rig.outbox.status()["queue_depth"])
    assert deepest <= 12, f"the queue grew to {deepest} rows during the outage (notes piling up)"
    outage.down = False
    rig.clock.advance(45)
    recovered_at = rig.clock()
    expired = [a for a in alerts if dt.datetime.fromisoformat(a["expires_at"]) <= recovered_at]
    alive = [a for a in alerts if a not in expired]

    rig.drain(limit=100)

    plain = [visible(t) for t in outage.delivered]
    first_lines = [p.split("\n")[0] for p in plain]
    digests = [m for m in (DIGEST_LINE.match(p) for p in first_lines) if m]
    assert len(digests) == 1, f"one digest, not {len(digests)}: {first_lines}"
    assert int(digests[0].group(1)) == len(expired)
    assert not [p for p in first_lines if NOTE_LINE.match(p)], "no individual missed notes"
    assert len(plain) == 1 + len(alive), "the digest plus the alerts still alive"
    assert len(expired) >= 100


# ========================= (D) p95 counts alert, exit and critical only ================
def test_p95_ignores_the_notes_the_worker_makes_itself(make_rig):
    rig = make_rig()
    # three alerts delivered 1, 2 and 3 s after they were raised
    for i, latency in enumerate((1000, 2000, 3000)):
        rig.clock.advance(5)
        now = rig.clock()
        alert = alert_at(now, id=alert_id(i), minutes=30)
        rig.outbox.enqueue(
            dedup_key=f"alert:{i}",
            kind="alert",
            alert_id=alert["id"],
            payload=alert,
            alert_created_at=now - dt.timedelta(milliseconds=latency),
            expires_at=now + dt.timedelta(minutes=30),
        )
        rig.drain()
    # and twenty notes the outbox sends within 0 ms of making them (in batches of at most
    # eight, so none of them is folded into a digest: that is decision C, tested above)
    note_alert = alert_at(rig.clock(), id=alert_id(99), minutes=30)
    for batch in (range(0, 8), range(8, 16), range(16, 20)):
        for n in batch:
            rig.outbox.enqueue(
                dedup_key=f"missed:{n}",
                kind="missed",
                alert_id=f"m{n:03d}",
                payload=note_alert,
                alert_created_at=rig.clock(),
                expires_at=None,
            )
        rig.drain(limit=100)

    assert len(rig.rec.texts()) == 23 and rig.statuses().count("sent") == 23
    assert rig.outbox.status()["p95_latency_ms"] == 3000, "only alert, exit and critical count"


def test_p95_counts_exit_and_critical_rows(make_rig):
    rig = make_rig()
    now = rig.clock()
    rig.outbox.enqueue(
        dedup_key="critical:slow",
        kind="critical",
        alert_id="c1",
        payload={"title": "Feed dead", "body": ""},
        alert_created_at=now - dt.timedelta(seconds=9),
        expires_at=None,
    )
    rig.drain()

    assert rig.outbox.status()["p95_latency_ms"] == 9000


# ========================= (F) one CRITICAL per title per 30 minutes ====================
# Every CRITICAL is its own row (key = notification id); the WORKER skips a queued CRITICAL whose
# title (spacing normalised, case kept) matches one SENT less than 30 minutes ago. The window is
# sliding, from the last message the phone actually got; a failed CRITICAL never silences the next.
def at(rig: Any, hh: int, mm: int, ss: int = 0) -> dt.datetime:
    moment = rig.clock().replace(hour=hh, minute=mm, second=ss, microsecond=0)
    rig.clock.set(moment)
    return moment


def criticals(rig: Any) -> list[dict[str, Any]]:
    return [r for r in rig.rows() if r["kind"] == "critical"]


def test_the_same_critical_title_within_30_minutes_is_sent_once_and_the_repeats_are_skipped(
    make_rig,
):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    results = []
    for hh, mm, ss in [(10, 0, 30), (10, 1, 30), (10, 10, 0), (10, 29, 59)]:
        moment = at(rig, hh, mm, ss)
        results.append(notifier.send(critical_notification("UNMANAGED position", "x", moment)))
        rig.drain()

    assert all(r == {"dashboard": True, "telegram": True} for r in results), "never a failure"
    assert len(rig.rec.texts()) == 1
    rows = criticals(rig)
    assert [r["status"] for r in rows] == ["sent", "skipped", "skipped", "skipped"]
    assert all("repeat" in (r["last_error"] or "") for r in rows[1:])


def test_a_new_message_needs_30_minutes_from_the_last_one_that_was_sent(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    plan = [
        ((10, 5, 0), "sent"),
        ((10, 31, 0), "skipped"),  # 26 minutes after the 10:05 send
        ((10, 35, 0), "sent"),  # exactly 30 minutes after it (the skipped 10:31 does not count)
        ((11, 2, 0), "skipped"),  # 27 minutes after the 10:35 send
        ((11, 5, 0), "sent"),
    ]
    for (hh, mm, ss), _expected in plan:
        notifier.send(critical_notification("UNMANAGED position", "x", at(rig, hh, mm, ss)))
        rig.drain()

    assert [r["status"] for r in criticals(rig)] == [status for _, status in plan]
    assert len(rig.rec.texts()) == 3


@pytest.mark.parametrize(
    "gap_s, sent",
    [
        (1, False),
        (60.0, False),
        (1799.999, False),
        (1800.0, True),
        (1800.001, True),
        (7200.0, True),
    ],
)
def test_the_repeat_window_is_exactly_30_minutes_from_the_last_send(make_rig, gap_s, sent):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    first = at(rig, 10, 5)
    notifier.send(critical_notification("Feed dead", "x", first))
    rig.drain()
    rig.clock.set(first + dt.timedelta(seconds=gap_s))
    notifier.send(critical_notification("Feed dead", "x", rig.clock()))

    rig.drain()

    assert [r["status"] for r in criticals(rig)] == ["sent", "sent" if sent else "skipped"]


def test_different_critical_titles_each_reach_the_phone(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    moment = at(rig, 10, 5)
    for title in ("Clock problem", "Dhan token expired", "Feed dead"):
        notifier.send(critical_notification(title, "x", moment))
    rig.drain()

    assert len(rig.rec.texts()) == 3


def test_the_title_comparison_ignores_spacing_but_not_case(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    moment = at(rig, 10, 5)
    for title in ("Clock  problem", " Clock problem ", "Clock\tproblem", "clock problem"):
        notifier.send(critical_notification(title, "x", moment))
    rig.drain()

    plain = [visible(t).split("\n")[0] for t in rig.rec.texts()]
    assert len(plain) == 2, plain  # "Clock problem" (any spacing) and "clock problem"


def test_a_repeated_critical_is_success_not_a_channel_failure(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    moment = at(rig, 10, 5)
    results = [notifier.send(critical_notification("Clock problem", "x", moment)) for _ in range(3)]
    rig.drain()

    assert all(r["telegram"] is True for r in results)
    assert notifier.stats["telegram"].failed == 0
    assert [r["status"] for r in criticals(rig)] == ["sent", "skipped", "skipped"]
    assert rig.outbox.status()["failed"] == 0 and rig.outbox.status()["queue_depth"] == 0


def test_a_failed_critical_never_silences_the_next_one(make_rig):
    rig = make_rig(scripted(tg_error(403, "Forbidden: bot was blocked by the user"), ok_response))
    notifier, _ = build_notifier(rig)
    notifier.send(critical_notification("Dhan token expired", "x", at(rig, 10, 5)))
    rig.drain()
    assert [r["status"] for r in criticals(rig)] == ["failed"]

    notifier.send(critical_notification("Dhan token expired", "x", at(rig, 10, 10)))
    rig.drain()

    assert [r["status"] for r in criticals(rig)] == ["failed", "sent"]


def test_a_critical_that_is_still_failing_is_not_a_sent_one(make_rig):
    """A transient failure leaves the first row queued; it does not count as 'sent'."""
    outage = Outage()
    rig = make_rig(outage)
    notifier, _ = build_notifier(rig)
    notifier.send(critical_notification("Feed dead", "x", at(rig, 10, 5)))
    rig.drain()

    assert outage.delivered == [] and [r["status"] for r in criticals(rig)] == ["queued"]


def test_after_an_outage_the_oldest_of_a_run_of_repeats_goes_and_the_rest_are_skipped(make_rig):
    """A legacy engine raises 'UNMANAGED' every minute. Forty minutes of outage queue forty
    rows; on recovery the phone gets the OLDEST (with its own event time and 'sent late'), a
    different title raised meanwhile gets through, and the other thirty-nine are skipped."""
    outage = Outage()
    rig = make_rig(outage)
    notifier, _ = build_notifier(rig)
    start = at(rig, 10, 0)
    first_event = None
    for minute in range(40):
        moment = start + dt.timedelta(minutes=minute)
        rig.clock.set(moment)
        notifier.send(critical_notification("UNMANAGED position", f"poll {minute}", moment))
        if minute == 3:
            notifier.send(critical_notification("Clock problem", "drift", moment))
        first_event = first_event or moment
        rig.drain()
    assert outage.delivered == []
    outage.down = False
    rig.clock.advance(120)  # past the retry back-off (at most 60 s) of the rows that kept failing
    recovered_at = rig.clock()

    rig.drain(limit=200)
    rig.drain(limit=200)

    assert len(outage.delivered) == 2, [visible(t).split("\n")[0] for t in outage.delivered]
    unmanaged, clock_problem = outage.delivered
    assert unmanaged == format_critical(
        {"title": "UNMANAGED position", "body": "poll 0"}, first_event
    ) + late_suffix(recovered_at)
    assert "Clock problem" in visible(clock_problem)
    statuses = [r["status"] for r in criticals(rig) if "UNMANAGED" in str(r["payload"])]
    assert statuses.count("sent") == 1 and statuses.count("skipped") == 39
    assert statuses[0] == "sent", "the oldest is the one that goes"
    assert rig.outbox.status()["queue_depth"] == 0


# ========================= (G) pace before claiming =====================================
class BrokenSleep(FakeClock):
    broken = True

    def sleep(self, seconds: float) -> None:
        if self.broken:
            raise RuntimeError("the pacing wait was interrupted")
        super().sleep(seconds)


def test_a_failure_in_the_pacing_wait_leaves_the_row_queued_and_never_resent(make_rig):
    clock = BrokenSleep()
    rig = make_rig(clock=clock)
    rig.enqueue_alert(alert_at(clock(), id=alert_id(1)))
    assert rig.outbox.drain_once() == 1  # the first message needs no wait
    rig.enqueue_alert(alert_at(clock(), id=alert_id(2)))

    with pytest.raises(RuntimeError):
        rig.outbox.drain_once()  # the wait for the 1 s spacing blows up

    assert rig.statuses() == ["sent", "queued"], "not left in 'sending'"
    clock.broken = False
    rig.drain()
    assert rig.rec.calls == 2 and rig.statuses() == ["sent", "sent"]
    assert not visible(rig.rec.texts()[1]).lstrip().startswith(RESEND)
    assert code_ids(rig.rec.texts()[1]) == [alert_id(2)[:6]]


def test_the_readme_states_the_final_quiet_and_late_rules():
    from us004_helpers import REPO

    readme = " ".join((REPO / "README.md").read_text().split())
    assert "SIGNAL and EXIT arrive silently" in readme
    assert "CRITICAL always sounds" in readme
    assert "sent late" in readme and "Missed N alerts" in readme


# ========================= a digest that cannot be stored never loses its alerts ==========
def test_a_digest_that_cannot_be_stored_never_leaves_alerts_skipped_and_unreported(make_rig):
    """The digest's dedup key is 'digest:burst:{lowest folded row id}'. Plant a row that owns
    that key: the insert collides. The alerts that were about to be folded must then stay
    queued (or be covered by a digest), never end 'skipped' with nobody told."""
    rig = make_rig()
    now = rig.clock()
    for i in range(9):  # nine queued alerts: the ninth (lowest confidence, row 9) is folded
        rig.enqueue_alert(alert_at(now, id=alert_id(i), score=90.0 - i))
    rig.outbox.enqueue(
        dedup_key="digest:burst:9",
        kind="test",
        alert_id="planted",
        payload={"title": "planted"},
        alert_created_at=now,
        expires_at=None,
    )

    for _ in range(3):  # whatever the worker does about it, it must not lose the alert
        try:
            rig.drain(limit=40)
        except Exception:  # noqa: BLE001 - raising is allowed, silently skipping is not
            pass

    rows = rig.rows()
    skipped = [r for r in rows if r["kind"] == "alert" and r["status"] == "skipped"]
    digests = [r for r in rows if r["kind"] == "digest"]
    assert not skipped or digests, f"{len(skipped)} alert(s) skipped and no digest tells the owner"
