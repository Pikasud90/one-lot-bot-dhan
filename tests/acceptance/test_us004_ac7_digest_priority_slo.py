"""US-004 AC7: a burst is ordered by confidence, capped at 8 individual messages and digested;
messages are formatted when they are sent; the latency SLO.

  * more than 8 queued: the top 8 by confidence go individually at 1 message/s and the rest as
    ONE digest;
  * "time left" is computed when the message is actually sent (after any pacing wait), so it is
    never stale;
  * p95 alert-to-sent is at most 10 s when 8 or fewer are queued.

An alert's confidence is `payload["confidence"]["score"]`. The `<code>xxxxxx</code>` short id in a
message tells which alert it is. The digest's wording is not specified; the tests only require
that it says how many alerts it covers.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from us003_helpers import install_network_guard, telegram, visible
from us004_helpers import (
    alert_at,
    alert_id,
    build_notifier,
    code_ids,
    critical_notification,
    exit_notification,
    mentions_count,
    rig_factory,
    signal_notification,
)

SCORES = [55.0, 91.0, 63.0, 78.0, 50.0, 88.0, 70.0, 95.0, 60.0, 82.0, 66.0, 74.0]  # shuffled


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"only the injected transport may be used: {attempts}"


@pytest.fixture
def make_rig(tmp_path):
    with rig_factory(tmp_path) as build:
        yield build


def queue_burst(rig: Any, count: int) -> list[dict[str, Any]]:
    """`count` alerts, all raised at the same instant, enqueued in a shuffled order."""
    now = rig.clock()
    alerts = [alert_at(now, id=alert_id(i), score=SCORES[i]) for i in range(count)]
    for alert in alerts:
        assert rig.enqueue_alert(alert)
    return alerts


def by_confidence(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(alerts, key=lambda a: a["confidence"]["score"], reverse=True)


def gaps(times: list[dt.datetime]) -> list[float]:
    return [(b - a).total_seconds() for a, b in zip(times, times[1:], strict=False)]


# ========================= more than 8 queued ===========================================
def test_more_than_eight_queued_sends_the_top_eight_by_confidence_then_one_digest(make_rig):
    rig = make_rig()
    alerts = queue_burst(rig, 12)
    ranked = by_confidence(alerts)

    rig.drain()

    texts = rig.rec.texts()
    assert len(texts) == 9, f"expected 8 alerts + 1 digest, got {len(texts)} messages"
    first_eight = [code_ids(t) for t in texts[:8]]
    assert first_eight == [[a["id"][:6]] for a in ranked[:8]], (
        "the 8 individual messages must be the 8 highest confidences, highest first"
    )
    assert mentions_count(texts[8], 4), f"the digest must say it covers 4 alerts: {texts[8]!r}"
    assert all(g >= 1.0 - 1e-9 for g in gaps(rig.rec.times)), "1 message/s includes the digest"

    rows = {r["alert_id"]: r for r in rig.rows() if r["kind"] == "alert"}
    assert all(rows[a["id"]]["status"] == "sent" for a in ranked[:8])
    assert all(rows[a["id"]]["status"] in ("skipped", "sent") for a in ranked[8:]), (
        "the folded alerts are settled, not left queued"
    )
    digests = [r for r in rig.rows() if r["kind"] == "digest"]
    assert len(digests) == 1 and digests[0]["status"] == "sent"
    assert rig.outbox.status()["queue_depth"] == 0


def test_exactly_eight_queued_are_all_sent_individually_with_no_digest(make_rig):
    rig = make_rig()
    ranked = by_confidence(queue_burst(rig, 8))

    rig.drain()

    texts = rig.rec.texts()
    assert [code_ids(t) for t in texts] == [[a["id"][:6]] for a in ranked]
    assert [r["kind"] for r in rig.rows()].count("digest") == 0
    assert all(g >= 1.0 - 1e-9 for g in gaps(rig.rec.times))


def test_nine_queued_is_over_the_limit_so_the_ninth_goes_into_a_digest(make_rig):
    rig = make_rig()
    ranked = by_confidence(queue_burst(rig, 9))

    rig.drain()

    texts = rig.rec.texts()
    assert [code_ids(t) for t in texts[:8]] == [[a["id"][:6]] for a in ranked[:8]]
    assert len(texts) == 9
    assert [r["kind"] for r in rig.rows()].count("digest") == 1


def test_a_new_burst_after_the_first_is_ordered_again(make_rig):
    rig = make_rig()
    queue_burst(rig, 3)
    rig.drain()
    rig.clock.advance(60)
    now = rig.clock()
    later = [alert_at(now, id=alert_id(20 + i), score=s) for i, s in enumerate([40.0, 99.0, 70.0])]
    for alert in later:
        rig.enqueue_alert(alert)

    rig.drain()

    tail = rig.rec.texts()[3:]
    assert [code_ids(t) for t in tail] == [[a["id"][:6]] for a in by_confidence(later)]


def test_exit_and_critical_go_first_and_only_signal_alerts_are_capped_and_digested(make_rig):
    """Decided: EXIT and CRITICAL are never folded into a digest and go first; the top-8 cap and
    the digest count SIGNAL alerts only. 10 alerts + EXIT + CRITICAL = EXIT, CRITICAL, the 8
    best alerts, then one digest for the other 2 (11 messages)."""
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    now = rig.clock()
    alerts = []
    for i in range(10):
        alert = alert_at(now, id=alert_id(i), score=SCORES[i])
        alerts.append(alert)
        notifier.send(signal_notification(alert, now))
    notifier.send(exit_notification("pos000000001", -740.0, now))
    notifier.send(critical_notification("Dhan token expired", "renew it", now))
    ranked = by_confidence(alerts)

    rig.drain()

    texts = rig.rec.texts()
    plain = [visible(t) for t in texts]
    assert len(texts) == 11, f"expected 2 + 8 + 1 messages, got {len(texts)}"
    first_two = plain[:2]
    assert any(p.startswith("EXIT ") for p in first_two), "EXIT goes first"
    assert any("Dhan token expired" in p for p in first_two), "CRITICAL goes first"
    assert [code_ids(t) for t in texts[2:10]] == [[a["id"][:6]] for a in ranked[:8]]
    assert mentions_count(texts[10], 2), f"the digest covers the 2 other alerts: {texts[10]!r}"
    assert all(g >= 1.0 - 1e-9 for g in gaps(rig.rec.times))


# ========================= formatted at send time =======================================
def test_time_left_is_computed_when_the_message_is_sent_not_when_it_was_queued(make_rig):
    tg = telegram()
    rig = make_rig()
    t0 = rig.clock()
    alert = alert_at(t0, id=alert_id(1), minutes=10)
    rig.enqueue_alert(alert)
    rig.clock.advance(180)  # three minutes in the queue (Telegram was slow)
    sent_at = rig.clock()

    rig.drain()

    (text,) = rig.rec.texts()
    assert text == tg.format_alert(alert, sent_at)
    assert "7m left" in text and "10m left" not in text, text


def test_time_left_is_computed_after_the_one_second_pacing_wait(make_rig):
    """Both alerts expire exactly 600 s after t0: at t0 the first reads '10m left', one second
    later the second must read '9m left' - formatted at ITS send time, not when claimed."""
    tg = telegram()
    rig = make_rig()
    t0 = rig.clock()
    high = alert_at(t0, id=alert_id(1), score=90, minutes=10)
    low = alert_at(t0, id=alert_id(2), score=40, minutes=10)
    rig.enqueue_alert(low)
    rig.enqueue_alert(high)

    rig.drain()

    texts, times = rig.rec.texts(), rig.rec.times
    assert (times[1] - times[0]).total_seconds() == pytest.approx(1.0)
    assert texts == [tg.format_alert(high, times[0]), tg.format_alert(low, times[1])]
    assert "10m left" in texts[0] and "9m left" in texts[1]


# ========================= the latency SLO ==============================================
def test_p95_alert_to_sent_is_within_ten_seconds_with_eight_queued(make_rig):
    rig = make_rig()
    queue_burst(rig, 8)

    rig.drain()

    st = rig.outbox.status()
    assert sorted(r["latency_ms"] for r in rig.rows()) == [i * 1000 for i in range(8)]
    assert st["p95_latency_ms"] == 7000, "nearest rank 8 of 8: the slowest of the eight"
    assert st["p95_latency_ms"] <= 10_000


def test_the_first_alert_of_a_burst_has_zero_latency_because_nothing_waits_for_it(make_rig):
    rig = make_rig()
    alerts = queue_burst(rig, 3)

    rig.drain()

    best = by_confidence(alerts)[0]
    row = next(r for r in rig.rows() if r["alert_id"] == best["id"])
    assert row["latency_ms"] == 0
