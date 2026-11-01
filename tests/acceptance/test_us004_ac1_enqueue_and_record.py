"""US-004 AC1: a SIGNAL becomes ONE short INSERT of a queued row; the response is recorded.

  * TelegramChannel.send performs ONE short INSERT: no HTTP, no Notifier call-back;
  * the row is durable (committed, visible from another connection) before the HTTP call;
  * after the response the row has message_id, sent_at from the injected clock and
    latency_ms = round((sent_at - alert_created_at) in ms) clamped at 0;
  * p95 is the nearest-rank percentile over the last 100 sent rows.

The AC text says the row is "queued when the HTTP handler runs", while AC3 says the worker
claims it (status sending) and commits BEFORE the HTTP call. Both cannot hold at the same
instant, so this file pins the part that is unambiguous: the row exists and is committed when
the handler runs (queued or sending), and is queued right after send() returns. AC3's file pins
the claim. See "AC clarity notes" in the test report.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from typing import Any

import pytest
from sqlalchemy import inspect
from us003_helpers import (
    TOKEN,
    install_network_guard,
    ok_response,
    tg_error,
)
from us004_helpers import (
    alert_at,
    alert_id,
    build_notifier,
    can_write,
    holds_alert,
    probe,
    read_rows,
    rig_factory,
    signal_notification,
    sql_statements,
    when,
)

from onelot.notify.manager import Notifier
from onelot.store import init_database

MINUTES_10 = dt.timedelta(minutes=10)
CONTRACT_COLUMNS = {
    "id",
    "dedup_key",
    "kind",
    "alert_id",
    "payload",
    "status",
    "attempts",
    "created_at",
    "alert_created_at",
    "expires_at",
    "next_attempt_at",
    "claimed_at",
    "silent",
    "sent_at",
    "message_id",
    "latency_ms",
    "last_error",
}


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"the outbox must only use the injected transport: {attempts}"


@pytest.fixture
def make_rig(tmp_path):
    with rig_factory(tmp_path) as build:
        yield build


# ========================= the table ====================================================
def test_the_telegram_outbox_table_has_every_contract_column(tmp_path):
    db = init_database(f"sqlite:///{tmp_path / 'schema.db'}")
    try:
        names = {c["name"] for c in inspect(db.engine).get_columns("telegram_outbox")}
        assert CONTRACT_COLUMNS <= names, f"missing columns: {sorted(CONTRACT_COLUMNS - names)}"
    finally:
        db.dispose()


def test_dedup_key_is_unique_in_the_schema(tmp_path):
    db = init_database(f"sqlite:///{tmp_path / 'schema.db'}")
    try:
        insp = inspect(db.engine)
        unique = [u["column_names"] for u in insp.get_unique_constraints("telegram_outbox")]
        unique += [i["column_names"] for i in insp.get_indexes("telegram_outbox") if i["unique"]]
        assert ["dedup_key"] in unique, f"no unique constraint on dedup_key: {unique}"
        pk = insp.get_pk_constraint("telegram_outbox")["constrained_columns"]
        assert pk == ["id"]
    finally:
        db.dispose()


# ========================= send() = one short INSERT ====================================
def test_a_signal_becomes_one_queued_row_and_makes_no_http_call(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    alert = alert_at(rig.clock(), id=alert_id(1))

    result = notifier.send(signal_notification(alert, rig.clock()))

    assert result == {"dashboard": True, "telegram": True}
    assert rig.rec.calls == 0, "TelegramChannel.send must not make an HTTP call"
    (row,) = rig.rows()
    assert row["status"] == "queued"
    assert row["kind"] == "alert"
    assert row["alert_id"] == alert["id"]
    assert row["dedup_key"], "the dedup key comes from Notifier.send"
    assert row["attempts"] == 0
    assert holds_alert(row["payload"], alert), "the whole alert is stored to be formatted later"
    assert when(row["alert_created_at"]) == dt.datetime.fromisoformat(alert["created_at"])
    assert when(row["expires_at"]) == dt.datetime.fromisoformat(alert["expires_at"])
    assert when(row["created_at"]) == rig.clock()
    assert not row["silent"]
    assert row["sent_at"] is None and row["message_id"] is None and row["latency_ms"] is None
    due = when(row["next_attempt_at"])
    assert due is None or due <= rig.clock(), "a new row is due at once"


def test_channel_send_executes_exactly_one_insert_and_nothing_else(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    alert = alert_at(rig.clock(), id=alert_id(2))

    with sql_statements(rig.db.engine) as statements:
        notifier.send(signal_notification(alert, rig.clock()))

    inserts = [s for s in statements if s.lstrip().upper().startswith("INSERT")]
    others = [s for s in statements if s not in inserts]
    assert len(inserts) == 1 and "telegram_outbox" in inserts[0], statements
    assert others == [], f"send() must be a single INSERT, it also ran: {others}"


def test_channel_send_never_calls_back_into_the_notifier(make_rig, monkeypatch):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    titles: list[str] = []
    real_send = Notifier.send

    def spy(self: Notifier, n: Any, dedup_key: str | None = None) -> Any:
        titles.append(n.title)
        return real_send(self, n, dedup_key)

    monkeypatch.setattr(Notifier, "send", spy)
    alert = alert_at(rig.clock(), id=alert_id(3))

    notifier.send(signal_notification(alert, rig.clock()))
    rig.drain()

    assert len(titles) == 1, f"the channel or the worker called the Notifier again: {titles}"


def test_the_row_is_committed_and_no_transaction_is_open_when_the_http_handler_runs(make_rig):
    seen: dict[str, Any] = {}

    def respond(request):
        seen["rows"] = probe(rig.path)  # another connection: only committed data is visible
        seen["writable"] = can_write(rig.path)  # another connection can take the write lock
        return ok_response(555)

    rig = make_rig(respond)
    notifier, _ = build_notifier(rig)
    alert = alert_at(rig.clock(), id=alert_id(4))
    notifier.send(signal_notification(alert, rig.clock()))

    rig.drain()

    assert len(seen["rows"]) == 1, "the row must exist (committed) before the HTTP call"
    assert seen["rows"][0][0] in ("queued", "sending")
    assert seen["writable"] is True, "no transaction may span the HTTP call"
    assert rig.statuses() == ["sent"]


# ========================= what the response records ====================================
@pytest.mark.parametrize(
    "created_before_send_s, expected_ms",
    [
        (0.0, 0),
        (1.2344, 1234),
        (1.2346, 1235),
        (4.2, 4200),
        (59.9996, 60000),
        (3600.0, 3_600_000),
    ],
)
def test_after_the_response_the_row_records_message_id_sent_at_and_latency(
    make_rig, created_before_send_s, expected_ms
):
    rig = make_rig(lambda request: ok_response(777))
    send_time = rig.clock()
    alert = alert_at(send_time, id=alert_id(5), minutes=120)
    created = send_time - dt.timedelta(seconds=created_before_send_s)

    rig.outbox.enqueue(
        dedup_key=f"alert:{alert['id']}",
        kind="alert",
        alert_id=alert["id"],
        payload=alert,
        alert_created_at=created,
        expires_at=send_time + dt.timedelta(hours=2),
    )
    assert rig.outbox.drain_once() == 1

    (row,) = rig.rows()
    assert row["status"] == "sent"
    assert row["message_id"] == 777
    assert when(row["sent_at"]) == send_time, "sent_at comes from the injected clock"
    assert row["latency_ms"] == expected_ms
    assert row["attempts"] == 1
    assert not row["last_error"]


@pytest.mark.parametrize("skew_s", [0.0004, 2.5, 3600.0])
def test_latency_is_clamped_at_zero_when_the_alert_is_dated_after_the_send(make_rig, skew_s):
    rig = make_rig()
    send_time = rig.clock()
    alert = alert_at(send_time, id=alert_id(6))
    rig.outbox.enqueue(
        dedup_key=f"alert:{alert['id']}",
        kind="alert",
        alert_id=alert["id"],
        payload=alert,
        alert_created_at=send_time + dt.timedelta(seconds=skew_s),
        expires_at=send_time + MINUTES_10,
    )

    rig.drain()

    (row,) = rig.rows()
    assert row["status"] == "sent" and row["latency_ms"] == 0


@pytest.mark.parametrize("n", [1, 2, 20, 21, 100, 101, 150])
def test_p95_is_the_nearest_rank_over_the_last_100_sent(make_rig, n):
    """Old rows are huge outliers: they must fall out of the window. Latencies are distinct so
    interpolation (numpy-style) cannot pass for nearest-rank."""
    rig = make_rig()
    older = max(0, n - 100)
    latencies = [600_000 + i * 1000 if i < older else ((i * 37) % 101 + 1) * 100 for i in range(n)]
    for i, latency_ms in enumerate(latencies):
        rig.clock.advance(2.0)  # past the 1 s spacing: every message is sent at `now`
        now = rig.clock()
        alert = alert_at(now, id=alert_id(i), minutes=30)
        rig.outbox.enqueue(
            dedup_key=f"alert:{alert['id']}",
            kind="alert",
            alert_id=alert["id"],
            payload=alert,
            alert_created_at=now - dt.timedelta(milliseconds=latency_ms),
            expires_at=now + dt.timedelta(minutes=30),
        )
        rig.drain()

    assert [r["latency_ms"] for r in rig.rows()] == latencies
    window = sorted(latencies[-100:])
    expected = window[math.ceil(0.95 * len(window)) - 1]
    assert rig.outbox.status()["p95_latency_ms"] == expected


def test_p95_ignores_failed_rows(make_rig):
    outcomes = iter([tg_error(403, "Forbidden: bot was blocked"), ok_response(1), ok_response(2)])
    rig = make_rig(lambda request: next(outcomes))
    for i in range(3):
        rig.clock.advance(2.0)
        now = rig.clock()
        alert = alert_at(now, id=alert_id(i))
        rig.outbox.enqueue(
            dedup_key=f"alert:{alert['id']}",
            kind="alert",
            alert_id=alert["id"],
            payload=alert,
            alert_created_at=now - dt.timedelta(milliseconds=[9_000_000, 300, 100][i]),
            expires_at=now + MINUTES_10,
        )
        rig.drain()

    assert rig.statuses() == ["failed", "sent", "sent"]
    assert rig.outbox.status()["p95_latency_ms"] == 300


# ========================= status() =====================================================
STATUS_KEYS = {
    "configured",
    "last_ok_at",
    "queue_depth",
    "last_error",
    "p95_latency_ms",
    "enqueued",
    "sent",
    "failed",
}


def test_status_of_a_fresh_outbox(make_rig):
    rig = make_rig()
    st = rig.outbox.status()
    assert STATUS_KEYS <= set(st), f"missing: {sorted(STATUS_KEYS - set(st))}"
    assert st["configured"] is True
    assert st["queue_depth"] == 0 and st["enqueued"] == 0 and st["sent"] == 0
    assert st["failed"] == 0
    assert st["last_ok_at"] is None and st["last_error"] is None


def test_status_counts_enqueued_and_sent_separately_and_tracks_the_last_success(make_rig):
    rig = make_rig()
    for i in range(3):
        rig.enqueue_alert(alert_at(rig.clock(), id=alert_id(i)))
    st = rig.outbox.status()
    assert (st["enqueued"], st["queue_depth"], st["sent"]) == (3, 3, 0)

    rig.drain()

    st = rig.outbox.status()
    assert (st["enqueued"], st["queue_depth"], st["sent"], st["failed"]) == (3, 0, 3, 0)
    assert when(st["last_ok_at"]) == rig.rec.times[-1], "last_ok_at is the last confirmed send"


def test_status_reports_a_failed_row_and_its_scrubbed_error(make_rig):
    rig = make_rig(lambda request: tg_error(400, f"Bad Request: chat not found {TOKEN}"))
    rig.enqueue_alert(alert_at(rig.clock(), id=alert_id(1)))

    rig.drain()

    st = rig.outbox.status()
    assert st["failed"] == 1 and st["sent"] == 0 and st["queue_depth"] == 0
    assert "HTTP 400" in st["last_error"] and "chat not found" in st["last_error"]
    assert TOKEN not in st["last_error"]


def test_status_is_json_serialisable_as_it_stands(make_rig):
    rig = make_rig()
    rig.enqueue_alert(alert_at(rig.clock(), id=alert_id(1)))
    rig.drain()
    # no default=str: the API returns this dict as it is
    assert json.loads(json.dumps(rig.outbox.status()))["sent"] == 1


def test_a_second_connection_sees_the_same_rows_the_outbox_wrote(make_rig):
    rig = make_rig()
    rig.enqueue_alert(alert_at(rig.clock(), id=alert_id(1)))
    assert [r["status"] for r in read_rows(rig.db.engine)] == ["queued"]
    assert probe(rig.path) == [("queued", "alert", alert_id(1))]
