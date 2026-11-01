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
    DOT,
    assert_well_formed,
    install_network_guard,
    telegram,
    visible,
)
from us004_helpers import (
    Outage,
    alert_at,
    alert_id,
    build_notifier,
    mentions_count,
    rig_factory,
    signal_notification,
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


def test_a_legacy_engine_signal_tells_the_phone_what_to_trade(make_rig):
    """The text must name the instrument, strike and option type the dashboard and the toast
    show (it was '? ?', 'Entry n/a', 'id <empty>': an alert the owner could not act on)."""
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    notifier.send(legacy_signal_notification(rig.clock()))

    rig.drain()

    (text,) = rig.rec.texts()
    shown = visible(text)
    assert "BANKNIFTY" in shown and "52000" in shown and "CE" in shown, text


def test_a_legacy_engine_exit_tells_the_phone_which_position_and_why(make_rig):
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


# ========================= legacy payloads: the plain notes (BUG-1 regression) ===========
DASH = chr(0x2014)


def legacy_texts(rig: Any) -> list[str]:
    return [visible(t) for t in rig.rec.texts()]


def test_a_legacy_signal_is_the_title_the_toast_shows_then_the_body_then_the_event_time(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    n = legacy_signal_notification(rig.clock())
    notifier.send(n)
    t0 = rig.clock()

    rig.drain()

    (raw,) = rig.rec.texts()
    assert_well_formed(raw)
    lines = visible(raw).split("\n")
    assert lines[0] == "BUY BANKNIFTY 52000 CE x1 @ ~126.00"
    assert "Entry ~126.00 (ask 125.50)" in lines[1]
    assert lines[-1] == f"Time {t0.strftime('%H:%M:%S')} IST"
    assert raw.startswith("<b>BUY BANKNIFTY 52000 CE x1 @ ~126.00</b>")
    for junk in ("? ?", "n/a", "<code></code>", "Data live"):
        assert junk not in raw, f"Desk placeholder {junk!r} in a legacy note"


def test_a_legacy_exit_is_the_title_the_toast_shows_the_reason_and_the_event_time(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    notifier.send(legacy_exit_notification(rig.clock()))
    t0 = rig.clock()

    rig.drain()

    (raw,) = rig.rec.texts()
    assert_well_formed(raw)
    lines = visible(raw).split("\n")
    assert lines[0] == f"EXIT 52000 CE {DASH} STOP"
    assert "close the position at the market" in lines[1]
    assert lines[-1] == f"Time {t0.strftime('%H:%M:%S')} IST"


@pytest.mark.parametrize("delay_s, late", [(60.0, False), (60.001, True)])
def test_a_legacy_note_says_sent_late_only_after_sixty_seconds(make_rig, delay_s, late):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    notifier.send(legacy_exit_notification(rig.clock()))
    t0 = rig.clock()
    rig.clock.advance(delay_s)

    rig.drain()

    (raw,) = rig.rec.texts()
    expected_tail = f"Time {t0.strftime('%H:%M:%S')} IST" + (
        f" {DOT} sent late {rig.clock().strftime('%H:%M')}" if late else ""
    )
    assert visible(raw).split("\n")[-1] == expected_tail


def test_html_in_a_legacy_title_and_a_huge_body_cannot_break_the_message(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    n = legacy_signal_notification(rig.clock())
    n.title = "BUY <b>BANKNIFTY</b> & Co <script>"
    n.body = "x < y & z " * 2000
    notifier.send(n)

    rig.drain()

    (raw,) = rig.rec.texts()
    assert_well_formed(raw)
    assert len(raw) <= 4096
    assert "BUY <b>BANKNIFTY</b> & Co <script>" in visible(raw).split("\n")[0]
    assert rig.statuses() == ["sent"]


def test_a_legacy_signal_keeps_its_two_minute_life_and_then_becomes_a_plain_missed_note(
    make_rig,
):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    n = legacy_signal_notification(rig.clock())
    notifier.send(n)
    rig.clock.advance(120)  # its expires_at, the Telegram outage was longer than the signal

    rig.drain()

    (raw,) = rig.rec.texts()
    assert raw.startswith("<b>Missed</b>")
    shown = visible(raw)
    assert "BUY BANKNIFTY 52000 CE x1 @ ~126.00" in shown and "expired 10:29 IST" in shown
    assert "Entry" not in shown, "a missed note never carries the actionable details"
    assert [r["status"] for r in rig.rows() if r["kind"] == "alert"] == ["skipped"]


def test_legacy_exit_reminders_follow_the_same_five_minute_rule(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    t0 = rig.clock()
    for second in range(0, 1201, 20):  # the guardian loop raises it every 20 s
        rig.clock.set(t0 + dt.timedelta(seconds=second))
        notifier.send(legacy_exit_notification(rig.clock()))
        rig.drain()

    assert rig.rec.offsets(t0) == [0.0, 300.0, 600.0, 900.0]
    assert all(t.startswith("<b>EXIT 52000 CE") for t in rig.rec.texts())


def test_an_outage_leaves_one_legacy_exit_the_newest_with_its_event_time(make_rig):
    outage = Outage()
    rig = make_rig(outage)
    notifier, _ = build_notifier(rig)
    t0 = rig.clock()
    for second in range(0, 1201, 60):
        rig.clock.set(t0 + dt.timedelta(seconds=second))
        notifier.send(legacy_exit_notification(rig.clock()))
        rig.drain()
    outage.down = False
    rig.clock.advance(61)
    recovered = rig.clock()

    rig.drain()

    (raw,) = outage.delivered
    event = t0 + dt.timedelta(seconds=900)  # the fourth message of the episode
    assert visible(raw).split("\n")[-1] == (
        f"Time {event.strftime('%H:%M:%S')} IST {DOT} sent late {recovered.strftime('%H:%M')}"
    )
    assert [r["status"] for r in rig.rows() if r["kind"] == "exit"] == [
        "skipped",
        "skipped",
        "skipped",
        "sent",
    ]


def test_ten_legacy_signals_at_once_are_eight_notes_and_a_digest_that_names_the_rest(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    now = rig.clock()
    for i in range(10):
        n = legacy_signal_notification(now)
        n.signal_id = f"legacy{i:06d}"
        n.title = f"BUY BANKNIFTY {52000 + 100 * i} CE x1 @ ~126.00"
        n.payload = {**n.payload, "signal_id": n.signal_id}
        notifier.send(n)

    rig.drain(limit=50)

    plain = legacy_texts(rig)
    assert len(plain) == 9
    assert [p.split("\n")[0] for p in plain[:8]] == [
        f"BUY BANKNIFTY {52000 + 100 * i} CE x1 @ ~126.00" for i in range(8)
    ]
    digest = plain[8]
    assert mentions_count(digest, 2)
    assert "52800 CE" in digest and "52900 CE" in digest, "the digest names the folded signals"


def test_a_legacy_note_is_silent_outside_the_session_like_any_other_signal(make_rig):
    from us004_helpers import SessionProbe

    rig = make_rig()
    notifier, _ = build_notifier(rig, in_session=SessionProbe(False))
    notifier.send(legacy_signal_notification(rig.clock()))
    notifier.send(legacy_exit_notification(rig.clock()))

    rig.drain()

    assert rig.rec.silent() == [True, True]


def test_a_desk_alert_and_a_legacy_signal_in_one_queue_each_keep_their_own_format(make_rig):
    tg = telegram()
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    now = rig.clock()
    desk_alert = alert_at(now, id=alert_id(1), score=90.0)
    notifier.send(legacy_signal_notification(now))
    notifier.send(signal_notification(desk_alert, now))

    rig.drain()

    texts = rig.rec.texts()
    assert tg.format_alert(desk_alert, rig.rec.times[0]) in texts[0:1], "the Desk alert goes first"
    assert texts[1].startswith("<b>BUY BANKNIFTY 52000 CE x1 @ ~126.00</b>")


# ========================= a legacy SIGNAL note shows its validity like a desk alert ======
def valid_till_line(raw: str) -> str:
    return next(line for line in visible(raw).split("\n") if line.startswith("Valid till"))


@pytest.mark.parametrize("delay_s", [0, 1, 59, 60, 61, 90, 119])
def test_a_legacy_signal_note_has_the_desk_alerts_valid_till_line_at_send_time(make_rig, delay_s):
    tg = telegram()
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    now = rig.clock()
    legacy = legacy_signal_notification(now)  # valid for 2 minutes
    notifier.send(legacy)
    rig.clock.advance(delay_s)
    sent_at = rig.clock()
    twin = alert_at(now, id=alert_id(1), ttl=dt.timedelta(minutes=2))  # a desk alert, same life

    rig.drain()

    (raw,) = rig.rec.texts()
    lines = visible(raw).split("\n")
    assert valid_till_line(raw) == valid_till_line(tg.format_alert(twin, sent_at))
    assert lines[-2].startswith("Valid till") and lines[-1].startswith("Time "), lines


def test_a_legacy_exit_note_has_no_valid_till_line(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    notifier.send(legacy_exit_notification(rig.clock()))

    rig.drain()

    assert "Valid till" not in visible(rig.rec.texts()[0])


def test_a_legacy_signal_note_without_a_deadline_never_expires_and_reads_valid_till_na(make_rig):
    rig = make_rig()
    notifier, _ = build_notifier(rig)
    n = legacy_signal_notification(rig.clock())
    n.payload = {k: v for k, v in n.payload.items() if k != "expires_at"}
    n.ttl_seconds = None
    notifier.send(n)
    rig.clock.advance(7200)

    rig.drain()

    (raw,) = rig.rec.texts()
    assert valid_till_line(raw) == "Valid till n/a", "an unknown deadline is shown as unknown"
    assert [r["status"] for r in rig.rows() if r["kind"] == "alert"] == ["sent"]
