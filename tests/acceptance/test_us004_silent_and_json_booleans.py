"""US-004 Manager clarifications: silent delivery and JSON booleans through the store.

* a risk-blocked SIGNAL is sent silent (disable_notification);
* quiet outside session means silent outside market hours, NEVER dropped; CRITICAL is never
  silent; notify.telegram_quiet_outside_session switches the rule off;
* `in_session` is asked about the channel's clock at send time;
* the formatter is applied at send time from the stored alert, and JSON booleans survive the
  store/reload (risk_allowed False keeps the BLOCKED prefix, data.delayed True keeps the tag).
"""

from __future__ import annotations

from typing import Any

import pytest
from us003_helpers import install_network_guard, telegram
from us004_helpers import (
    SessionProbe,
    alert_at,
    alert_id,
    build_notifier,
    critical_notification,
    exit_notification,
    find_key,
    rig_factory,
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


def notification_for(level: str, rig: Any, blocked: bool) -> Any:
    now = rig.clock()
    if level == "SIGNAL":
        return signal_notification(alert_at(now, id=alert_id(1), blocked=blocked), now)
    if level == "EXIT":
        return exit_notification("pos000000001", -740.0, now)
    return critical_notification("Dhan token expired", "renew it", now)


# level, risk-blocked, inside the session, notify.telegram_quiet_outside_session, expect silent
SILENCE = [
    ("SIGNAL", False, True, None, False),
    ("SIGNAL", True, True, None, True),  # blocked: silent even in session
    ("SIGNAL", False, False, None, True),  # quiet outside session is on by default
    ("SIGNAL", False, False, True, True),
    ("SIGNAL", False, False, False, False),  # the switch is off: audible after hours
    ("SIGNAL", True, False, False, True),  # blocked stays silent whatever the switch says
    ("EXIT", False, True, None, False),
    ("EXIT", False, False, None, True),
    ("EXIT", False, False, False, False),
    ("CRITICAL", False, True, None, False),
    ("CRITICAL", False, False, None, False),  # CRITICAL is never silent
    ("CRITICAL", False, False, True, False),
]


@pytest.mark.parametrize("level, blocked, in_session, quiet, expect_silent", SILENCE)
def test_silent_delivery_rules_and_nothing_is_ever_dropped(
    make_rig, level, blocked, in_session, quiet, expect_silent
):
    rig = make_rig()
    notifier, _ = build_notifier(rig, in_session=SessionProbe(in_session), quiet=quiet)

    result = notifier.send(notification_for(level, rig, blocked))
    rig.drain()

    assert result["telegram"] is True
    assert len(rig.rec.texts()) == 1, "quiet means silent, never dropped"
    assert rig.rec.silent() == [expect_silent]
    assert bool(rig.row()["silent"]) is expect_silent, "the row records the decision"


@pytest.mark.parametrize("level", ["SIGNAL", "EXIT"])
def test_in_session_is_asked_about_the_channel_clock_at_send_time(make_rig, level):
    rig = make_rig()
    probe = SessionProbe(True)
    notifier, _ = build_notifier(rig, in_session=probe)
    sent_at = rig.clock()

    notifier.send(notification_for(level, rig, blocked=False))

    assert probe.calls, "the channel never asked whether the market is open"
    assert probe.calls[-1] == sent_at


@pytest.mark.parametrize("silent", [True, False])
def test_the_silent_flag_of_a_row_reaches_telegram_as_disable_notification(make_rig, silent):
    rig = make_rig()
    rig.enqueue_alert(alert_at(rig.clock(), id=alert_id(1)), silent=silent)

    rig.drain()

    assert rig.rec.silent() == [silent]


@pytest.mark.parametrize(
    "blocked, delayed", [(True, True), (True, False), (False, True), (False, False)]
)
def test_json_booleans_survive_the_store_so_the_formatter_still_tags_the_message(
    make_rig, blocked, delayed
):
    tg = telegram()
    rig = make_rig()
    alert = alert_at(rig.clock(), id=alert_id(1), blocked=blocked, delayed=delayed)
    rig.enqueue_alert(alert)

    stored = rig.row()["payload"]
    assert find_key(stored, "risk_allowed") is (not blocked), "risk_allowed must stay a boolean"
    assert find_key(find_key(stored, "data"), "delayed") is delayed, "data.delayed must too"
    rig.drain()

    (text,) = rig.rec.texts()
    assert text.startswith("<b>[BLOCKED by risk] ") is blocked
    assert ("<b>delayed ~15m</b>" in text) is delayed
    assert text == tg.format_alert(alert, rig.clock()), "formatted from the stored alert"


def test_a_blocked_and_delayed_alert_keeps_both_tags_after_a_restart(make_rig):
    tg = telegram()
    rig = make_rig()
    alert = alert_at(rig.clock(), id=alert_id(1), blocked=True, delayed=True)
    rig.enqueue_alert(alert)
    restarted, rec2 = rig.another_outbox()  # a new worker reads the row back from the file

    rig.drain(restarted)

    (text,) = rec2.texts()
    assert text == tg.format_alert(alert, rig.clock())
    assert text.startswith("<b>[BLOCKED by risk] ") and "delayed ~15m" in text
