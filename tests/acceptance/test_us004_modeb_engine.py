"""US-004 mode B: the Engine's side of the Telegram wiring, beyond the happy path.

  * quiet hours: the session callable the Engine hands the channel is open exactly during
    market hours on a trading day (the close is inclusive), is closed on weekends and NSE
    holidays, and reads a datetime in any zone as the IST instant it is;
  * Telegram is built FIRST, so a start-up CRITICAL (a clock problem) reaches the phone;
  * a Telegram start-up failure never stops the Engine: the dashboard and the toast carry on
    and the warning is recorded.

`Engine._in_session` is private but is exactly the callable Engine.start gives the channel
(there is no other way to ask what the production wiring considers "in session").
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any

import pytest
from us003_helpers import (
    install_network_guard,
    telegram,
    visible,
)
from us004_helpers import (
    alert_at,
    alert_id,
    make_engine,
    signal_notification,
    wait_until,
)

from onelot.clock import IST, now_ist


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"only the injected transport may be used: {attempts}"


@pytest.fixture
def engine_factory(tmp_path, monkeypatch):
    built: list[Any] = []

    def build(**kwargs: Any) -> Any:
        rig = make_engine(tmp_path / f"engine{len(built)}", monkeypatch, **kwargs)
        built.append(rig)
        return rig

    yield build
    for rig in built:
        try:
            rig.engine.stop()
        except Exception:  # noqa: BLE001
            pass


def ist(day: str, clock: str) -> dt.datetime:
    return dt.datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=IST)


# the NSE calendar in config/nse_holidays.yaml: Mon 2026-11-02 trades, Sat 11-07 and Sun 11-08
# do not, Tue 2026-11-10 (Diwali Balipratipada) is a holiday
@pytest.mark.parametrize(
    "when, expected",
    [
        (ist("2026-11-02", "09:14:59"), False),
        (ist("2026-11-02", "09:15:00"), True),
        (ist("2026-11-02", "12:00:00"), True),
        (ist("2026-11-02", "15:29:59"), True),
        (ist("2026-11-02", "15:30:00"), True),  # the close is inclusive (SessionWindow.is_open)
        (ist("2026-11-02", "15:30:01"), False),
        (ist("2026-11-02", "00:00:00"), False),
        (ist("2026-11-02", "23:59:59"), False),
        (ist("2026-11-07", "11:00:00"), False),  # Saturday
        (ist("2026-11-08", "11:00:00"), False),  # Sunday
        (ist("2026-11-10", "11:00:00"), False),  # an NSE holiday is quiet
        (ist("2026-11-11", "11:00:00"), True),  # the next day trades again
        (dt.datetime(2026, 11, 2, 3, 45, 0, tzinfo=dt.timezone.utc), True),  # 09:15 IST
        (dt.datetime(2026, 11, 2, 3, 44, 59, tzinfo=dt.timezone.utc), False),  # 09:14:59 IST
        (dt.datetime(2026, 11, 1, 22, 0, 0, tzinfo=dt.timezone.utc), False),  # 03:30 IST Mon
        (dt.datetime(2026, 11, 2, 10, 0, 0), True),  # naive means IST
    ],
)
def test_the_engine_session_callable_is_open_exactly_during_market_hours(
    engine_factory, when, expected
):
    engine = engine_factory().engine

    assert engine._in_session(when) is expected


def test_a_startup_critical_reaches_the_phone_because_telegram_starts_first(
    engine_factory, monkeypatch
):
    monkeypatch.setattr(
        "onelot.engine.assert_clock_sane", lambda tolerance: (False, "clock drift of 99 s")
    )
    rig = engine_factory()

    rig.engine.start()
    delivered = wait_until(lambda: rig.rec.calls >= 1)
    rig.engine.stop()

    assert delivered, "the start-up CRITICAL never reached the phone"
    first = visible(rig.rec.texts()[0])
    assert "Clock problem" in first and "99 s" in first


def test_a_telegram_start_failure_never_stops_the_engine(engine_factory, monkeypatch):
    def refuse(self: Any) -> None:
        raise RuntimeError("cannot start a thread")

    monkeypatch.setattr(telegram().TelegramOutbox, "start", refuse)
    rig = engine_factory()
    before = set(threading.enumerate())

    rig.engine.start()  # must not raise

    assert rig.engine.status.running is True
    assert any("Telegram" in w for w in rig.engine.status.warnings), rig.engine.status.warnings
    now = now_ist()
    result = rig.engine.notifier.send(signal_notification(alert_at(now, id=alert_id(1)), now))
    assert result["dashboard"] is True
    assert [t for t in rig.worker_threads(before) if t.name == "telegram-outbox"] == []
    rig.engine.stop()
    assert rig.engine.status.running is False


def test_a_second_stop_after_telegram_was_running_is_harmless(engine_factory):
    rig = engine_factory()
    rig.engine.start()
    rig.engine.stop()

    rig.engine.stop()

    assert rig.engine.status.running is False
