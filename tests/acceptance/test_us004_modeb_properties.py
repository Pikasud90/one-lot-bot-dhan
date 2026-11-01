"""US-004 mode B: Hypothesis properties of the outbox (pacing, backoff, expiry, digest, dedup).

Each property builds a fresh file-based outbox on a fake clock per example, so an example is
a small deterministic story; the examples themselves are drawn by Hypothesis (derandomized, no
example database, no deadline). The invariants:

  * pacing: whatever the gaps between drains, two requests are never closer than 1.000 s,
    nothing is lost or sent twice, and order is first-in-first-out within a rank;
  * backoff: after the j-th transient failure the retry is exactly min(60, 2^(j-1)) s away;
    after a 429 it is exactly max(retry_after, 1) s away (hour cap aside);
  * expiry: an alert is sent iff now < expires_at; otherwise it is skipped, never sent, and
    its one missed note (or one digest when there are many) goes out;
  * burst: n queued alerts give the best min(n, 8) one by one, highest confidence first, and a
    digest of the rest iff n > 8; CRITICALs go first and are never folded;
  * latency: latency_ms is round(delta in ms), clamped at 0; p95 is the nearest rank;
  * dedup: the table behaves like a set keyed by dedup_key and by (alert_id, kind).
"""

from __future__ import annotations

import datetime as dt
import math
from fractions import Fraction
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st
from us003_helpers import (
    install_network_guard,
    json_response,
    ok_response,
    telegram,
    tg_error,
    visible,
)
from us004_helpers import (
    alert_at,
    alert_id,
    code_ids,
    mentions_count,
    rig_factory,
    scripted,
    when,
)

from onelot.notify.telegram_outbox import p95_nearest_rank

SETTINGS = settings(
    max_examples=25,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=list(HealthCheck),
)
EXPENSIVE = settings(SETTINGS, max_examples=15)
SECOND = dt.timedelta(seconds=1)


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"only the injected transport may be used: {attempts}"


@pytest.fixture
def make_rig(tmp_path):
    with rig_factory(tmp_path) as build:
        yield build


def critical(rig: Any, i: int, title: str | None = None) -> None:
    rig.outbox.enqueue(
        dedup_key=f"critical:{i}",
        kind="critical",
        alert_id=f"c{i:04d}",
        payload={"title": title or f"Event {i:04d}", "body": ""},
        alert_created_at=rig.clock(),
        expires_at=None,
    )


# ========================= pacing =======================================================
@SETTINGS
@given(gaps_ms=st.lists(st.integers(0, 3500), min_size=1, max_size=10))
def test_property_two_requests_are_never_closer_than_one_second_and_none_is_lost(make_rig, gaps_ms):
    rig = make_rig()
    t0 = rig.clock()
    for i, gap in enumerate(gaps_ms):
        critical(rig, i)
        rig.drain()
        rig.clock.advance(gap / 1000)

    times = rig.rec.times
    assert len(times) == len(gaps_ms), "every message is sent exactly once"
    assert times[0] == t0, "an idle chat is not made to wait"
    assert all(b - a >= SECOND for a, b in zip(times, times[1:], strict=False)), times
    assert all(0 <= s <= 1.0 + 1e-9 for s in rig.clock.sleeps), rig.clock.sleeps
    titles = [visible(t).split("\n")[0] for t in rig.rec.texts()]
    assert titles == [f"CRITICAL {chr(0x00B7)} Event {i:04d}" for i in range(len(gaps_ms))]


# ========================= backoff and rate limits ======================================
@SETTINGS
@given(
    failures=st.integers(1, 14),
    slack_ms=st.lists(st.integers(0, 3000), min_size=14, max_size=14),
)
def test_property_the_nth_transient_failure_retries_exactly_min_60_2_pow_n_minus_1_s_later(
    make_rig, failures, slack_ms
):
    rig = make_rig(scripted(*[lambda: tg_error(502, "bad gateway")] * failures, ok_response))
    rig.enqueue_alert(alert_at(rig.clock(), id=alert_id(1), minutes=24 * 60))

    for n in range(1, failures + 1):
        attempted = rig.clock()
        rig.drain()
        row = rig.row()
        assert row["status"] == "queued" and row["attempts"] == n
        delay = dt.timedelta(seconds=min(60, 2 ** (n - 1)))
        assert when(row["next_attempt_at"]) - attempted == delay, f"failure {n}"
        rig.clock.advance(delay.total_seconds() + slack_ms[n - 1] / 1000)

    rig.drain()
    row = rig.row()
    assert row["status"] == "sent" and row["attempts"] == failures + 1
    assert sum(rig.clock.sleeps) == 0, "back-off is a schedule, never a sleep"


@SETTINGS
@given(retry_after=st.integers(0, 4000).map(lambda q: q / 4))  # 0 .. 1000 s in quarters
def test_property_a_429_retries_exactly_max_retry_after_1_seconds_later(make_rig, retry_after):
    too_many = tg_error(
        429, "Too Many Requests", parameters={"retry_after": retry_after}
    )  # a JSON number: 3 or 3.25
    rig = make_rig(scripted(too_many, ok_response))
    t0 = rig.clock()
    rig.enqueue_alert(alert_at(t0, id=alert_id(1), minutes=24 * 60))

    rig.outbox.drain_once()

    row = rig.row()
    assert row["status"] == "queued"
    assert when(row["next_attempt_at"]) - t0 == dt.timedelta(seconds=max(retry_after, 1.0))
    assert sum(rig.clock.sleeps) == 0 and rig.clock() == t0
    rig.clock.advance(max(retry_after, 1.0))
    rig.drain()
    assert rig.statuses() == ["sent"]


# ========================= expiry =======================================================
@SETTINGS
@example(ttl_s=1, offset_us=-1)
@example(ttl_s=1, offset_us=0)
@example(ttl_s=600, offset_us=1)
@given(ttl_s=st.integers(1, 7200), offset_us=st.integers(-3_000_000, 3_000_000))
def test_property_an_alert_is_sent_iff_now_is_before_expires_at(make_rig, ttl_s, offset_us):
    tg = telegram()
    rig = make_rig()
    alert = alert_at(rig.clock(), id=alert_id(1), ttl=dt.timedelta(seconds=ttl_s))
    rig.enqueue_alert(alert)
    expires = dt.datetime.fromisoformat(alert["expires_at"])
    rig.clock.set(expires + dt.timedelta(microseconds=offset_us))
    now = rig.clock()

    rig.drain()
    rig.clock.advance(30)
    rig.drain()  # settling twice changes nothing

    texts = rig.rec.texts()
    if offset_us < 0:
        assert texts == [tg.format_alert(alert, now)]
        assert rig.statuses() == ["sent"]
    else:
        assert texts == [tg.format_missed(alert, now)], "only the one missed note"
        assert sorted(r["kind"] for r in rig.rows()) == ["alert", "missed"]
        assert rig.row(kind="alert")["status"] == "skipped"


@SETTINGS
@example(count=8, outage_s=601)
@example(count=9, outage_s=601)
@example(count=1, outage_s=601)
@given(count=st.integers(1, 20), outage_s=st.integers(601, 90_000))
def test_property_expired_alerts_get_one_note_each_up_to_eight_and_one_digest_above(
    make_rig, count, outage_s
):
    rig = make_rig()
    for i in range(count):
        rig.enqueue_alert(alert_at(rig.clock(), id=alert_id(i), score=40.0 + i, minutes=10))
    rig.clock.advance(outage_s)

    rig.drain(limit=200)

    texts = rig.rec.texts()
    kinds = [r["kind"] for r in rig.rows()]
    assert [r["status"] for r in rig.rows() if r["kind"] == "alert"] == ["skipped"] * count
    if count > 8:
        assert len(texts) == 1 and kinds.count("digest") == 1 and kinds.count("missed") == 0
        assert mentions_count(texts[0], count), texts[0]
    else:
        assert len(texts) == count and kinds.count("missed") == count
        assert kinds.count("digest") == 0
        assert len(set(texts)) == count, "each missed note names its own alert"


# ========================= a burst ======================================================
@EXPENSIVE
@example(scores=list(range(8)), criticals=0)
@example(scores=list(range(9)), criticals=1)
@example(scores=list(range(20)), criticals=3)
@given(
    scores=st.lists(st.integers(0, 100), min_size=1, max_size=20, unique=True),
    criticals=st.integers(0, 3),
)
def test_property_a_burst_sends_the_best_eight_in_order_and_digests_the_rest(
    make_rig, scores, criticals
):
    rig = make_rig()
    now = rig.clock()
    alerts = [alert_at(now, id=alert_id(i), score=float(s)) for i, s in enumerate(scores)]
    for alert in alerts:
        rig.enqueue_alert(alert)
    for c in range(criticals):
        critical(rig, c)
    ranked = sorted(alerts, key=lambda a: -a["confidence"]["score"])

    rig.drain(limit=100)

    texts = rig.rec.texts()
    plain = [visible(t) for t in texts]
    crit_at = [i for i, p in enumerate(plain) if p.startswith("CRITICAL")]
    best = ranked[:8]
    middle = texts[criticals : criticals + len(best)]
    assert crit_at == list(range(criticals)), "CRITICALs go first and are never folded"
    assert [code_ids(t) for t in middle] == [[a["id"][:6]] for a in best], "best 8, highest first"
    extra = len(alerts) - len(best)
    digests = [r for r in rig.rows() if r["kind"] == "digest"]
    assert len(digests) == (1 if extra else 0)
    assert len(texts) == criticals + len(best) + (1 if extra else 0)
    if extra:
        assert mentions_count(texts[-1], extra), texts[-1]
    status = {r["alert_id"]: r["status"] for r in rig.rows() if r["kind"] == "alert"}
    assert [status[a["id"]] for a in best] == ["sent"] * len(best)
    assert [status[a["id"]] for a in ranked[8:]] == ["skipped"] * extra
    assert not [s for s in rig.statuses() if s in ("queued", "sending")]
    assert all(b - a >= SECOND for a, b in zip(rig.rec.times, rig.rec.times[1:], strict=False))


# ========================= latency and p95 ==============================================
@SETTINGS
@given(offset_us=st.integers(-10_000_000, 10_000_000_000))
def test_property_latency_ms_is_the_rounded_delay_clamped_at_zero(make_rig, offset_us):
    assume(offset_us % 1000 != 500)  # a tie: half-up (the module's rule) or half-even is a choice
    rig = make_rig()
    sent_at = rig.clock()
    alert = alert_at(sent_at, id=alert_id(1), minutes=24 * 60 * 400)
    rig.outbox.enqueue(
        dedup_key=f"alert:{alert['id']}",
        kind="alert",
        alert_id=alert["id"],
        payload=alert,
        alert_created_at=sent_at - dt.timedelta(microseconds=offset_us),
        expires_at=sent_at + dt.timedelta(days=400),
    )

    rig.drain()

    expected = max(0, round(Fraction(offset_us, 1000)))
    assert rig.row()["latency_ms"] == expected


@settings(max_examples=300, deadline=None, derandomize=True, database=None)
@given(values=st.lists(st.integers(0, 10**9), min_size=1, max_size=300))
def test_property_p95_is_the_smallest_value_with_at_least_95_percent_at_or_below(values):
    result = p95_nearest_rank(values)
    needed = math.ceil(0.95 * len(values))
    assert result in values
    assert sum(v <= result for v in values) >= needed
    assert sum(v < result for v in values) < needed, "a smaller value would already do"


def test_p95_of_nothing_is_none():
    assert p95_nearest_rank([]) is None


# ========================= dedup ========================================================
OPS = st.lists(
    st.tuples(
        st.sampled_from([None, "a", "b", "c"]),
        st.sampled_from(["alert", "exit", "critical", "test"]),
        st.sampled_from(["k1", "k2", "k3", "k4", "k5", "k6"]),
    ),
    min_size=1,
    max_size=25,
)


@SETTINGS
@given(ops=OPS)
def test_property_the_table_is_a_set_keyed_by_dedup_key_and_by_alert_id_and_kind(make_rig, ops):
    rig = make_rig()
    keys: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    model: list[tuple[str, str | None, str]] = []
    for alert_id_, kind, key in ops:
        assert (
            rig.outbox.enqueue(
                dedup_key=key,
                kind=kind,
                alert_id=alert_id_,
                payload={"title": key, "body": ""},
                alert_created_at=rig.clock(),
                expires_at=None,
            )
            is True
        ), "a duplicate is never a failure"
        clash = key in keys or (alert_id_ is not None and (alert_id_, kind) in pairs)
        if not clash:
            keys.add(key)
            if alert_id_ is not None:
                pairs.add((alert_id_, kind))
            model.append((key, alert_id_, kind))

    stored = [(r["dedup_key"], r["alert_id"], r["kind"]) for r in rig.rows()]
    assert stored == model, "the first row wins; later duplicates change nothing"


@SETTINGS
@given(
    first=st.integers(0, 4),
    ok_after=st.booleans(),
)
def test_property_a_message_is_never_sent_twice_whatever_the_failures_before_it(
    make_rig, first, ok_after
):
    """After any run of transient failures a row is sent at most once: once `sent`, further
    drains and re-enqueues of the same alert never send it again."""
    steps = [lambda: json_response(200, {"ok": False, "description": "try later"})] * first
    rig = make_rig(scripted(*steps, ok_response))
    alert = alert_at(rig.clock(), id=alert_id(1), minutes=24 * 60)
    rig.enqueue_alert(alert)
    for _ in range(first + 1):
        rig.drain()
        rig.clock.advance(120)
    calls = rig.rec.calls
    if ok_after:
        rig.enqueue_alert(alert)  # a duplicate enqueue
    for _ in range(3):
        rig.clock.advance(3600)
        rig.drain()

    assert rig.statuses() == ["sent"] and rig.rec.calls == calls == first + 1
