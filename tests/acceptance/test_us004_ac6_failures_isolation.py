"""US-004 AC6: failures stay inside Telegram, and the token never leaves the client.

* a permanent failure (400, 401, 403, 404): the row is `failed` with the scrubbed detail, no
  endless retry, and the dashboard and OS-toast channels still deliver the same alert;
* an enqueue that raises: the Notifier result is {dashboard: True, telegram: False} and
  nothing else breaks;
* the token never appears in outbox rows, in any status JSON, in a repr or in a log.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import OperationalError
from us003_helpers import (
    TOKEN,
    capture_logs,
    install_network_guard,
    leaked_forms,
    ok_response,
    record_blob,
    telegram,
    tg_error,
)
from us004_helpers import (
    RecordingChannel,
    alert_at,
    alert_id,
    build_notifier,
    rig_factory,
    scripted,
    signal_notification,
)


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"only the injected transport may be used: {attempts}"


@pytest.fixture
def make_rig(tmp_path):
    with rig_factory(tmp_path) as build:
        yield build


def raise_alert(rig: Any, notifier: Any, i: int = 1) -> tuple[dict[str, Any], dict[str, bool]]:
    alert = alert_at(rig.clock(), id=alert_id(i))
    return alert, notifier.send(signal_notification(alert, rig.clock()))


# ========================= permanent failures ===========================================
@pytest.mark.parametrize(
    "status, description",
    [
        (400, "Bad Request: chat not found"),
        (401, "Unauthorized"),
        (403, "Forbidden: bot was blocked by the user"),
        (404, "Not Found"),
    ],
)
def test_a_permanent_failure_fails_the_row_with_a_scrubbed_detail_and_never_retries(
    make_rig, status, description
):
    echo = f"{description} (token {TOKEN}, url /bot{TOKEN}/sendMessage)"
    rig = make_rig(scripted(tg_error(status, echo)))
    rig.enqueue_alert(alert_at(rig.clock(), id=alert_id(1)))

    rig.drain()

    row = rig.row()
    assert row["status"] == "failed" and row["attempts"] == 1
    detail = row["last_error"]
    assert f"HTTP {status}" in detail and description in detail, "the detail is kept"
    assert leaked_forms(detail) == [], "and scrubbed"
    assert rig.outbox.status()["failed"] == 1 and rig.outbox.status()["queue_depth"] == 0

    for _ in range(3):  # three days of drains: no retry, not even a missed note
        rig.clock.advance(24 * 3600)
        rig.drain()
    assert rig.rec.calls == 1, "a permanent failure must not be retried"
    assert rig.statuses() == ["failed"]


def test_after_a_permanent_failure_the_dashboard_and_the_toast_still_get_every_alert(make_rig):
    rig = make_rig(scripted(tg_error(403, "Forbidden: bot was blocked by the user")))
    toast = RecordingChannel("macos_toast")
    notifier, _ = build_notifier(rig, extra=(toast,))
    results = []
    for i in range(2):
        results.append(raise_alert(rig, notifier, i)[1])
        rig.drain()  # Telegram rejects the first one, and the second
        rig.clock.advance(2)

    assert results == [{"dashboard": True, "macos_toast": True, "telegram": True}] * 2
    shown = {item["notification_id"] for item in notifier.dashboard.recent()}
    assert shown == {n.notification_id for n in toast.sent} and len(shown) == 2
    assert rig.statuses() == ["failed", "failed"]


def test_a_failing_telegram_never_slows_or_blocks_the_other_channels(make_rig):
    """send() only queues: the rejection happens later, in the worker, not in the Desk's call."""
    rig = make_rig(scripted(tg_error(401, "Unauthorized")))
    toast = RecordingChannel("macos_toast")
    notifier, _ = build_notifier(rig, extra=(toast,))

    _, result = raise_alert(rig, notifier)

    assert rig.rec.calls == 0, "no HTTP happened inside Notifier.send"
    assert result == {"dashboard": True, "macos_toast": True, "telegram": True}


# ========================= an enqueue that raises =======================================
def test_an_enqueue_that_raises_gives_dashboard_true_and_telegram_false(make_rig, monkeypatch):
    rig = make_rig()
    toast = RecordingChannel("macos_toast")
    notifier, _ = build_notifier(rig, extra=(toast,))

    def locked(self: Any, **kwargs: Any) -> bool:
        raise OperationalError("INSERT INTO telegram_outbox", {}, Exception("database is locked"))

    with monkeypatch.context() as patched:
        patched.setattr(telegram().TelegramOutbox, "enqueue", locked)
        _, result = raise_alert(rig, notifier, 1)

    assert result == {"dashboard": True, "macos_toast": True, "telegram": False}
    assert notifier.stats["telegram"].failed == 1
    assert len(notifier.dashboard.recent()) == 1 and len(toast.sent) == 1, "nothing else breaks"
    # ... and the next alert, with the database healthy again, is queued normally
    _, again = raise_alert(rig, notifier, 2)
    assert again == {"dashboard": True, "macos_toast": True, "telegram": True}
    assert len(rig.rows()) == 1


def test_with_only_the_dashboard_the_result_is_exactly_dashboard_true_telegram_false(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    with rig.db.engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE telegram_outbox")  # a real database error, not a mock

    _, result = raise_alert(rig, notifier)

    assert result == {"dashboard": True, "telegram": False}
    assert len(notifier.dashboard.recent()) == 1


def test_an_enqueue_that_reports_false_is_a_channel_failure_too(make_rig, monkeypatch):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    with monkeypatch.context() as patched:
        patched.setattr(telegram().TelegramOutbox, "enqueue", lambda self, **kwargs: False)
        _, result = raise_alert(rig, notifier)

    assert result == {"dashboard": True, "telegram": False}
    assert notifier.stats["telegram"].failed == 1


# ========================= the token never leaves the client ============================
LEAKY = {
    "permanent_echo": [lambda: tg_error(401, f"Unauthorized: {TOKEN} at /bot{TOKEN}/sendMessage")],
    "transient_echo": [lambda: tg_error(502, f"Bad Gateway: /bot{TOKEN}/sendMessage")],
    "network_error": [
        lambda: httpx.ConnectError(f"failed for https://api.telegram.org/bot{TOKEN}/sendMessage")
    ],
    "rate_limited_echo": [
        lambda: tg_error(429, f"Slow down /bot{TOKEN}/", parameters={"retry_after": 2}),
        ok_response,
    ],
}


@pytest.mark.parametrize("scenario", list(LEAKY))
def test_the_token_never_appears_in_rows_status_json_reprs_or_logs(make_rig, scenario):
    rig = make_rig(scripted(*LEAKY[scenario]))
    notifier, channel = build_notifier(rig)

    with capture_logs() as handler:  # a bare handler: no redaction filter to hide behind
        raise_alert(rig, notifier)
        for _ in range(4):
            rig.drain()
            rig.clock.advance(400)

    rows = rig.rows()
    assert rows, "the scenario must have produced a row"
    surfaces = {
        "rows": json.dumps(rows, default=str),
        "outbox status": json.dumps(rig.outbox.status()),
        "channel status": json.dumps(channel.status()),
        "notifier status": json.dumps(notifier.status()),
        "reprs": " ".join(
            [repr(rig.outbox), str(rig.outbox), repr(channel), str(channel), repr(rig.client)]
        ),
        "logs": record_blob(handler.records),
    }
    for name, blob in surfaces.items():
        assert leaked_forms(blob) == [], f"the token leaked into {name} ({scenario})"
