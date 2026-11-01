"""US-004 mode B: the payloads the outbox really meets.

* the legacy Bank Nifty engine (Engine._emit_signal and Engine._manage) sends SIGNAL and EXIT
  through the same Notifier with payloads shaped by Signal.as_dict() and ExitDecision.as_dict(),
  not by the Desk: the phone must still be told WHAT to trade (BUG-1 below);
* odd but legal values in a Desk payload (numpy scalars, NaN, datetimes, a huge junk key);
* bad input to enqueue is refused loudly and leaves no row; naive datetimes mean IST.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import pytest
from us003_helpers import (
    install_network_guard,
    telegram,
    visible,
)
from us004_helpers import (
    alert_at,
    alert_id,
    build_notifier,
    rig_factory,
    when,
)

from onelot.clock import IST
from onelot.notify.base import Notification, NotificationLevel


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"only the injected transport may be used: {attempts}"


@pytest.fixture
def make_rig(tmp_path):
    with rig_factory(tmp_path) as build:
        yield build


# ========================= the legacy engine's payloads =================================
def legacy_signal_notification(now: dt.datetime) -> Notification:
    """What Notifier.signal(sig) builds for the Bank Nifty engine: title, body and the
    Signal.as_dict() payload (it has signal_id, underlying, strike, opt_type ... and none of
    the Desk's series_key, side, kind, economics, confidence or data)."""
    payload = {
        "signal_id": "5a1d0e2c9b47",
        "created_at": now.isoformat(),
        "expires_at": (now + dt.timedelta(minutes=2)).isoformat(),
        "direction": "long",
        "underlying": "BANKNIFTY",
        "expiry": "2026-11-26",
        "strike": 52000.0,
        "opt_type": "CE",
        "security_id": "55012",
        "quote": {"bid": 120.0, "ask": 125.5, "ltp": 123.0, "spread": 5.5},
        "entry_eff": 126.0,
        "stop_level": 98.0,
        "target_level": 160.0,
        "lots": 1,
        "qty": 30,
        "lot_size": 30,
        "rr_net": 1.12,
        "regime": {"regime": "TRENDING_UP"},
        "ivp": {"mode": "off"},
        "rsi": 55.0,
        "warnings": [],
    }
    return Notification(
        title="BUY BANKNIFTY 52000 CE x1 @ ~126.00",
        body="BUY BANKNIFTY 11-26 52000 CE  x1 lot (30 qty)\nEntry ~126.00 (ask 125.50)",
        level=NotificationLevel.SIGNAL,
        signal_id=payload["signal_id"],
        ttl_seconds=120,
        created_at=now,
        payload=payload,
    )


def legacy_exit_notification(now: dt.datetime) -> Notification:
    """What Notifier.exit_alert(position, decision) builds: the ExitDecision.as_dict() payload
    has an action, a reason and a message, and NO position."""
    payload = {
        "action": "EXIT",
        "reason": "STOP",
        "message": "Underlying through the stop: close the position at the market.",
        "hold_confidence": 0.2,
        "trail_stop": None,
        "trail_active": False,
        "high_water": 131.0,
        "unrealised_r": -1.04,
        "unrealised_net": -812.5,
        "details": {},
    }
    return Notification(
        title=f"EXIT 52000 CE {chr(0x2014)} STOP",
        body=str(payload["message"]),
        level=NotificationLevel.EXIT,
        position_id="9c3e5d7f1a20",
        created_at=now,
        payload=payload,
    )


def test_a_legacy_engine_signal_is_queued_and_delivered_without_an_error(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)

    result = notifier.send(legacy_signal_notification(rig.clock()))
    rig.drain()

    assert result == {"dashboard": True, "telegram": True}
    assert len(rig.rec.texts()) == 1 and rig.statuses() == ["sent"]


def test_BUG1_a_legacy_engine_signal_tells_the_phone_what_to_trade(make_rig):
    """The text must name the instrument, strike and option type the dashboard and the toast
    show. Today the Desk formatter gets a payload with none of its fields and sends
    '? ?', 'Entry n/a', 'id <empty>': an alert the owner cannot act on."""
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    notifier.send(legacy_signal_notification(rig.clock()))

    rig.drain()

    (text,) = rig.rec.texts()
    shown = visible(text)
    assert "BANKNIFTY" in shown and "52000" in shown and "CE" in shown, text


def test_BUG1_a_legacy_engine_exit_tells_the_phone_which_position_and_why(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    notifier.send(legacy_exit_notification(rig.clock()))

    rig.drain()

    (text,) = rig.rec.texts()
    shown = visible(text)
    assert shown.startswith("EXIT") and "52000 CE" in shown and "STOP" in shown, text


# ========================= odd values in a Desk payload =================================
def test_numpy_scalars_in_a_payload_are_stored_as_plain_json_and_the_message_is_unchanged(
    make_rig,
):
    tg = telegram()
    rig = make_rig()
    plain = alert_at(rig.clock(), id=alert_id(1), blocked=True, delayed=True)
    rich = alert_at(rig.clock(), id=alert_id(2), blocked=True, delayed=True)
    rich["risk_allowed"] = np.bool_(False)
    rich["data"]["delayed"] = np.bool_(True)
    rich["confidence"]["score"] = np.float64(71.6)
    rich["economics"]["qty"] = np.int64(1)
    rich["economics"]["entry_price"] = np.float64(812.4)
    assert rig.outbox.enqueue(
        dedup_key="alert:rich",
        kind="alert",
        alert_id=rich["id"],
        payload=rich,
        alert_created_at=rig.clock(),
        expires_at=rig.clock() + dt.timedelta(minutes=10),
    )

    rig.drain()

    (text,) = rig.rec.texts()
    expected = tg.format_alert(plain, rig.clock()).replace(alert_id(1)[:6], alert_id(2)[:6])
    assert text == expected, "numpy values must read exactly like the plain ones"
    assert text.startswith("<b>[BLOCKED by risk] ") and "<b>delayed ~15m</b>" in text


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -float("inf"), 10**400, "high"])
def test_a_confidence_score_that_is_not_a_finite_number_never_blocks_delivery(make_rig, score):
    rig = make_rig()
    alert = alert_at(rig.clock(), id=alert_id(1))
    alert["confidence"]["score"] = score
    assert rig.enqueue_alert(alert) is True

    rig.drain()

    assert rig.statuses() == ["sent"], rig.rows()[0].get("last_error")


def test_datetime_objects_in_a_payload_read_like_iso_strings(make_rig):
    tg = telegram()
    rig = make_rig()
    as_text = alert_at(rig.clock(), id=alert_id(1))
    as_objects = alert_at(rig.clock(), id=alert_id(1))
    as_objects["created_at"] = dt.datetime.fromisoformat(as_text["created_at"])
    as_objects["expires_at"] = dt.datetime.fromisoformat(as_text["expires_at"])
    rig.outbox.enqueue(
        dedup_key="alert:objects",
        kind="alert",
        alert_id=as_objects["id"],
        payload=as_objects,
        alert_created_at=as_objects["created_at"],
        expires_at=as_objects["expires_at"],
    )

    rig.drain()

    assert rig.rec.texts() == [tg.format_alert(as_text, rig.clock())]


def test_a_two_megabyte_junk_key_is_stored_but_never_reaches_the_phone(make_rig):
    from us003_helpers import assert_budget

    tg = telegram()
    rig = make_rig()
    alert = alert_at(rig.clock(), id=alert_id(1))
    clean = tg.format_alert(alert, rig.clock())
    alert["archive"] = ["x" * 1000] * 2000
    alert["ledger"] = {"balance": 1234567.89, "holdings": ["SBIN"]}
    assert rig.enqueue_alert(alert) is True

    rig.drain()

    (text,) = rig.rec.texts()
    assert text == clean, "only the whitelisted fields are ever rendered"
    assert_budget(text)
    assert "1234567" not in text and "holdings" not in text


# ========================= bad input to enqueue =========================================
def enqueue_args(rig: Any, **over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "dedup_key": "k",
        "kind": "alert",
        "alert_id": "a1",
        "payload": {"title": "x"},
        "alert_created_at": rig.clock(),
        "expires_at": None,
    }
    args.update(over)
    return args


@pytest.mark.parametrize(
    "bad, error",
    [
        ({"kind": "place_order"}, ValueError),
        ({"kind": ""}, ValueError),
        ({"dedup_key": ""}, ValueError),
        ({"payload": ["not", "a", "mapping"]}, TypeError),
        ({"payload": None}, TypeError),
    ],
)
def test_bad_input_to_enqueue_raises_and_leaves_no_row(make_rig, bad, error):
    rig = make_rig()

    with pytest.raises(error):
        rig.outbox.enqueue(**enqueue_args(rig, **bad))

    assert rig.rows() == []


def test_a_naive_datetime_means_ist(make_rig):
    rig = make_rig()
    naive_created = dt.datetime(2026, 11, 2, 10, 27, 3)
    naive_expires = dt.datetime(2026, 11, 2, 10, 37, 3)
    rig.outbox.enqueue(
        **enqueue_args(rig, alert_created_at=naive_created, expires_at=naive_expires)
    )

    (row,) = rig.rows()

    assert when(row["alert_created_at"]) == naive_created.replace(tzinfo=IST)
    assert when(row["expires_at"]) == naive_expires.replace(tzinfo=IST)


def test_a_utc_datetime_is_the_same_instant_as_its_ist_twin(make_rig):
    rig = make_rig()
    ist = dt.datetime(2026, 11, 2, 10, 27, 3, tzinfo=IST)
    utc = ist.astimezone(dt.timezone.utc)
    rig.outbox.enqueue(**enqueue_args(rig, alert_created_at=utc, expires_at=None))

    assert when(rig.rows()[0]["alert_created_at"]) == ist
