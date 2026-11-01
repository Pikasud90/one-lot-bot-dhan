"""US-004 AC2: rate limits, 1 s pacing, retries, expiry and the outage digest.

  * a 429 with retry_after R puts the row back with next_attempt_at = now + max(R, 1) and
    drain_once returns WITHOUT sleeping;
  * the only sleep is the 1.000 s spacing between sends, read off the fake clock;
  * a transient failure retries after min(60, 2^(attempts-1)) s, no jitter, until expires_at;
  * at or after expires_at the original is skipped and ONE missed note per alert is enqueued;
  * after a long outage ONE digest is sent instead of N notes.

drain_once is called through ``rig.drain()`` (until it returns 0), so these hold whether one
call handles one due row or every due row. Times are the fake clock's, never the wall clock's.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest
from us003_helpers import (
    install_network_guard,
    json_response,
    ok_response,
    telegram,
    tg_error,
)
from us004_helpers import (
    FakeClock,
    alert_at,
    alert_id,
    mentions_count,
    rig_factory,
    scripted,
    when,
)

SECONDS = dt.timedelta(seconds=1)


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"the outbox must only use the injected transport: {attempts}"


@pytest.fixture
def make_rig(tmp_path):
    with rig_factory(tmp_path) as build:
        yield build


# ========================= 429: reschedule, never sleep =================================
@pytest.mark.parametrize(
    "retry_after, delay_s",
    [(3, 3.0), (0, 1.0), (0.2, 1.0), (1, 1.0), (2.5, 2.5), (30, 30.0)],
)
def test_a_429_reschedules_at_now_plus_max_retry_after_and_one_and_does_not_sleep(
    make_rig, retry_after, delay_s
):
    too_many = tg_error(
        429,
        f"Too Many Requests: retry after {retry_after}",
        parameters={"retry_after": retry_after},
    )
    rig = make_rig(scripted(too_many, ok_response(9)))
    t0 = rig.clock()
    rig.enqueue_alert(alert_at(t0, id=alert_id(1), minutes=30))

    rig.outbox.drain_once()

    assert rig.rec.calls == 1
    row = rig.row()
    assert row["status"] == "queued", "a rate-limited row waits in the queue"
    assert when(row["next_attempt_at"]) == t0 + dt.timedelta(seconds=delay_s)
    assert sum(rig.clock.sleeps) == 0, "drain_once must return, not sleep, on a 429"
    assert rig.clock() == t0
    # nothing is due until next_attempt_at
    assert rig.outbox.drain_once() == 0 and rig.rec.calls == 1
    rig.clock.advance(delay_s - 0.001)
    assert rig.outbox.drain_once() == 0 and rig.rec.calls == 1
    rig.clock.advance(0.001)
    rig.drain()
    assert rig.rec.calls == 2 and rig.statuses() == ["sent"]
    assert sum(rig.clock.sleeps) == 0


def test_a_429_without_retry_after_still_waits_at_least_one_second(make_rig):
    rig = make_rig(scripted(tg_error(429, "Too Many Requests"), ok_response(9)))
    t0 = rig.clock()
    rig.enqueue_alert(alert_at(t0, id=alert_id(1), minutes=30))

    rig.drain()

    wait = (when(rig.row()["next_attempt_at"]) - t0).total_seconds()
    assert 1.0 <= wait <= 60.0, f"waited {wait} s"
    assert rig.rec.calls == 1 and sum(rig.clock.sleeps) == 0


# ========================= spacing: the only sleep ======================================
def test_a_lone_message_is_sent_at_once_without_any_sleep(make_rig):
    rig = make_rig()
    t0 = rig.clock()
    rig.enqueue_alert(alert_at(t0, id=alert_id(1)))

    assert rig.outbox.drain_once() == 1

    assert rig.rec.offsets(t0) == [0.0]
    assert sum(rig.clock.sleeps) == 0


def test_queued_messages_go_out_exactly_one_second_apart_on_the_fake_clock(make_rig):
    rig = make_rig()
    t0 = rig.clock()
    for i in range(4):
        rig.enqueue_alert(alert_at(t0, id=alert_id(i), score=60 + i))

    rig.drain()

    offsets = rig.rec.offsets(t0)
    assert len(offsets) == 4 and offsets[0] == 0.0, "the first message is not delayed"
    gaps = [b - a for a, b in zip(offsets, offsets[1:], strict=False)]
    assert all(abs(g - 1.0) < 1e-6 for g in gaps), f"gaps {gaps}"
    assert all(0 <= s <= 1.0 + 1e-6 for s in rig.clock.sleeps), rig.clock.sleeps


def test_spacing_also_holds_between_two_separate_drains(make_rig):
    rig = make_rig()
    rig.enqueue_alert(alert_at(rig.clock(), id=alert_id(1)))
    rig.drain()
    rig.clock.advance(0.4)
    rig.enqueue_alert(alert_at(rig.clock(), id=alert_id(2)))

    rig.drain()

    gap = (rig.rec.times[1] - rig.rec.times[0]).total_seconds()
    assert gap >= 1.0 - 1e-9, f"two sends {gap} s apart"
    assert all(0 <= s <= 1.0 + 1e-6 for s in rig.clock.sleeps), rig.clock.sleeps


def test_spacing_holds_when_the_http_call_itself_takes_time(make_rig):
    clock = FakeClock()

    def slow(request: httpx.Request) -> httpx.Response:
        clock.advance(0.3)  # the call takes 0.3 s of fake time
        return ok_response(1)

    rig = make_rig(slow, clock=clock)
    for i in range(3):
        rig.enqueue_alert(alert_at(clock(), id=alert_id(i), score=60 + i))

    rig.drain()

    starts = rig.rec.times
    assert all(
        (b - a).total_seconds() >= 1.0 - 1e-9 for a, b in zip(starts, starts[1:], strict=False)
    )
    assert all(0 <= s <= 1.0 + 1e-6 for s in clock.sleeps), clock.sleeps


# ========================= transient: 2^(attempts-1), capped at 60, no jitter ==========
TRANSIENT = {
    "http_500": lambda: tg_error(500, "Internal Server Error"),
    "http_502": lambda: tg_error(502, "Bad Gateway"),
    "network_error": lambda: httpx.ConnectError("connection refused"),
    "ok_false_200": lambda: json_response(200, {"ok": False, "description": "try later"}),
}


@pytest.mark.parametrize("failure", list(TRANSIENT))
def test_transient_failures_retry_after_min_60_2_pow_attempts_minus_1_seconds_without_jitter(
    make_rig, failure
):
    delays = [1, 2, 4, 8, 16, 32, 60, 60, 60]
    rig = make_rig(scripted(*[TRANSIENT[failure]] * len(delays), lambda: ok_response(4242)))
    rig.enqueue_alert(alert_at(rig.clock(), id=alert_id(1), minutes=30))  # 30 min > 243 s of waits

    for k, delay in enumerate(delays, start=1):
        attempted_at = rig.clock()
        rig.drain()
        assert rig.rec.calls == k, f"attempt {k} did not happen"
        row = rig.row()
        assert row["status"] == "queued" and row["attempts"] == k
        assert when(row["next_attempt_at"]) - attempted_at == dt.timedelta(seconds=delay), (
            f"after failure {k} the next attempt must be {delay} s away"
        )
        rig.clock.advance(delay - 0.001)  # one millisecond early: nothing happens
        rig.drain()
        assert rig.rec.calls == k, f"retry {k + 1} came early"
        rig.clock.advance(0.001)

    rig.drain()  # the 10th attempt succeeds
    row = rig.row()
    assert row["status"] == "sent" and row["attempts"] == len(delays) + 1
    assert row["message_id"] == 4242
    assert sum(rig.clock.sleeps) == 0, "back-off is a schedule, never a sleep"


def test_a_failing_row_does_not_block_the_next_one_and_spacing_still_holds(make_rig):
    rig = make_rig(scripted(tg_error(500, "boom"), ok_response(1)))
    t0 = rig.clock()
    rig.enqueue_alert(alert_at(t0, id=alert_id(1), score=90, minutes=30))
    rig.enqueue_alert(alert_at(t0, id=alert_id(2), score=50, minutes=30))

    rig.drain()

    assert rig.row(alert_id=alert_id(2))["status"] == "sent", "a failing row must not block"
    times = rig.rec.times
    assert len(times) >= 2
    assert all(
        (b - a).total_seconds() >= 1.0 - 1e-9 for a, b in zip(times, times[1:], strict=False)
    )


# ========================= expiry: skipped + ONE missed note ============================
@pytest.mark.parametrize(
    "offset_s, still_sent",
    [(-1.0, True), (-0.000001, True), (0.0, False), (1.0, False), (7200.0, False)],
)
def test_at_or_after_expires_at_the_original_is_skipped_and_one_missed_note_goes_out(
    make_rig, offset_s, still_sent
):
    tg = telegram()
    rig = make_rig()
    alert = alert_at(rig.clock(), id=alert_id(1), minutes=10)
    rig.enqueue_alert(alert)
    expires = dt.datetime.fromisoformat(alert["expires_at"])
    rig.clock.set(expires + dt.timedelta(seconds=offset_s))
    now = rig.clock()

    rig.drain()

    if still_sent:
        assert rig.rec.texts() == [tg.format_alert(alert, now)]
        assert rig.statuses() == ["sent"]
        return
    assert rig.rec.texts() == [tg.format_missed(alert, now)], "only the missed note is sent"
    assert rig.row(kind="alert")["status"] == "skipped"
    missed = rig.row(kind="missed")
    assert missed["alert_id"] == alert["id"] and missed["status"] == "sent"


def test_a_transient_failure_is_retried_only_until_expires_at(make_rig):
    tg = telegram()
    # the five attempts at the original fail; the missed note (6th request) then goes through
    rig = make_rig(scripted(*[lambda: tg_error(500, "boom")] * 5, ok_response))
    t0 = rig.clock()
    alert = alert_at(t0, id=alert_id(1), ttl=dt.timedelta(seconds=20))
    rig.enqueue_alert(alert)

    for step in (0, 1, 2, 4, 8):  # attempts at t0, +1, +3, +7, +15
        rig.clock.advance(step)
        rig.drain()
    assert rig.rec.calls == 5 and rig.statuses() == ["queued"]  # next retry would be at +31 s

    rig.clock.set(t0 + dt.timedelta(seconds=19.999))
    rig.drain()
    assert rig.rec.calls == 5, "not due and not expired: nothing happens"

    rig.clock.set(t0 + dt.timedelta(seconds=20))  # expires_at, still before the next retry
    now = rig.clock()
    rig.drain()

    assert rig.rec.calls == 6, "one more request: the missed note, not the original"
    assert rig.rec.texts()[-1] == tg.format_missed(alert, now)
    assert rig.row(kind="alert")["status"] == "skipped"
    rig.clock.advance(3600)
    rig.drain()
    assert rig.rec.calls == 6, "the skipped original is never retried"


def test_only_one_missed_note_is_ever_made_per_alert_even_across_a_restart(make_rig):
    rig = make_rig()
    alert = alert_at(rig.clock(), id=alert_id(1))
    rig.enqueue_alert(alert)
    rig.clock.advance(3600)

    rig.drain()
    for _ in range(3):
        rig.clock.advance(60)
        rig.outbox.drain_once()
    restarted, rec2 = rig.another_outbox()
    rig.drain(restarted)

    assert [r["kind"] for r in rig.rows()].count("missed") == 1
    assert rig.rec.calls == 1 and rec2.calls == 0


# ========================= a long outage: one digest, not N notes =======================
def test_after_a_long_outage_one_digest_is_sent_instead_of_n_missed_notes(make_rig):
    rig = make_rig()
    t0 = rig.clock()
    for i in range(12):
        rig.enqueue_alert(alert_at(t0, id=alert_id(i), score=50 + i, minutes=10))
    rig.clock.advance(2 * 3600)  # the phone was unreachable for two hours

    rig.drain()

    texts = rig.rec.texts()
    assert len(texts) == 1, f"{len(texts)} messages for 12 expired alerts"
    assert mentions_count(texts[0], 12), f"the digest must say how many it covers: {texts[0]!r}"
    rows = rig.rows()
    assert [r["status"] for r in rows if r["kind"] == "alert"] == ["skipped"] * 12
    assert not [r for r in rows if r["kind"] == "missed" and r["status"] == "sent"]
    digests = [r for r in rows if r["kind"] == "digest"]
    assert len(digests) == 1 and digests[0]["status"] == "sent"
    assert rig.outbox.status()["queue_depth"] == 0


def test_eight_expired_alerts_get_eight_missed_notes_and_no_digest(make_rig):
    tg = telegram()
    rig = make_rig()
    t0 = rig.clock()
    alerts = [alert_at(t0, id=alert_id(i), score=50 + i) for i in range(8)]
    for alert in alerts:
        rig.enqueue_alert(alert)
    rig.clock.advance(2 * 3600)
    now = rig.clock()

    rig.drain()

    texts = rig.rec.texts()
    assert len(texts) == 8
    assert sorted(texts) == sorted(tg.format_missed(a, now) for a in alerts)
    kinds = [r["kind"] for r in rig.rows()]
    assert kinds.count("digest") == 0 and kinds.count("missed") == 8


def test_nine_expired_alerts_get_one_digest_and_no_missed_notes(make_rig):
    rig = make_rig()
    t0 = rig.clock()
    for i in range(9):
        rig.enqueue_alert(alert_at(t0, id=alert_id(i), score=50 + i))
    rig.clock.advance(2 * 3600)

    rig.drain()

    texts = rig.rec.texts()
    assert len(texts) == 1 and mentions_count(texts[0], 9), texts
    assert [r["kind"] for r in rig.rows() if r["status"] == "sent"] == ["digest"]


def test_a_single_expired_alert_still_gets_its_own_missed_note_not_a_digest(make_rig):
    tg = telegram()
    rig = make_rig()
    alert = alert_at(rig.clock(), id=alert_id(1))
    rig.enqueue_alert(alert)
    rig.clock.advance(2 * 3600)
    now = rig.clock()

    rig.drain()

    assert rig.rec.texts() == [tg.format_missed(alert, now)]
    assert [r["kind"] for r in rig.rows()].count("digest") == 0
