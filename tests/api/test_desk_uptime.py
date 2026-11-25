"""US-097 points 1 and 2 through the REAL Engine and app: `GET /api/desk/uptime`, and the desk's
start and 09:05 messages on the phone (kind `desk`, forwarded like `token` and `volume`), against
the fake Telegram on 127.0.0.1. Without Telegram keys nothing leaves the computer."""

from __future__ import annotations

import datetime as dt
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from onelot.clock import IST, now_ist
from onelot.desk.uptime import UptimeRecorder
from onelot.engine import DESK_START_SETTLE, Engine
from onelot.store.schema import telegram_outbox

from .fake_telegram import FakeTelegram
from .test_telegram_phone import save_keys, settle


def at(day: int, hhmm: str) -> dt.datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return dt.datetime(2026, 11, day, h, m, tzinfo=IST)


def fake_desk(engine: Engine, source: str = "dhan+yahoo") -> None:
    engine.desk = SimpleNamespace(source=SimpleNamespace(name=source))  # type: ignore[assignment]


def test_the_route_gives_the_cover_per_trading_day(engine: Engine, client) -> None:
    rec = UptimeRecorder(engine.db.engine, engine.session_clock, engine.calendar.is_trading_day)
    today = now_ist().date()
    d = today - dt.timedelta(days=1)
    while not engine.calendar.is_trading_day(d):
        d -= dt.timedelta(days=1)
    start = dt.datetime.combine(d, dt.time(11, 1), IST)
    now = start
    while now <= dt.datetime.combine(d, dt.time(15, 40), IST):
        rec.beat(now)
        now += dt.timedelta(minutes=1)
    r = client.get("/api/desk/uptime?days=30")
    assert r.status_code == 200
    days = {x["day"]: x for x in r.json()["days"]}
    got = days[d.isoformat()]
    assert got["ran_from"] == "11:01" and got["ran_to"] == "15:30" and got["started_late"] is True
    assert 0.6 < got["share"] < 0.9 and got["missed"] is False and got["known"] is True
    assert all(engine.calendar.is_trading_day(dt.date.fromisoformat(k)) for k in days)
    assert client.get("/api/desk/uptime?days=0").status_code == 422


def test_the_start_message_goes_out_a_minute_after_the_start_with_the_three_facts(
    engine: Engine, client, fake: FakeTelegram, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_keys(client)
    fake_desk(engine)
    monkeypatch.setattr(engine, "token_expiry", lambda: at(25, "21:31"))
    monkeypatch.setattr(engine.calendar, "is_trading_day", lambda d: True)
    engine._desk_messages._read_expiry = engine.token_expiry  # type: ignore[attr-defined]
    engine._desk_messages.is_trading_day = engine.calendar.is_trading_day
    start = at(25, "10:57")
    engine._desk_started(start)
    engine._desk_heartbeat(start + dt.timedelta(seconds=30))  # not yet
    time.sleep(0.3)
    assert fake.sent == []
    engine._desk_heartbeat(start + DESK_START_SETTLE)
    engine._desk_heartbeat(start + DESK_START_SETTLE + dt.timedelta(minutes=1))  # once
    settle(engine, fake, 1)
    text = fake.sent[0]
    assert "Desk started 10:57. Token valid until 21:31. Feed: live." in text
    assert "Started late: alerts before 10:57 were missed." in text


def test_the_morning_check_reaches_the_phone_once(
    engine: Engine, client, fake: FakeTelegram, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_keys(client)
    fake_desk(engine, "yahoo")
    monkeypatch.setattr(engine, "token_expiry", lambda: at(25, "21:31"))
    engine._desk_messages._read_expiry = engine.token_expiry  # type: ignore[attr-defined]
    engine._desk_messages.is_trading_day = lambda d: True
    for hhmm in ("09:04", "09:05", "09:06", "09:10"):
        engine._desk_heartbeat(at(25, hhmm))
    settle(engine, fake, 1)
    assert "Desk up at 09:05. Token valid until 21:31. Feed: delayed" in fake.sent[0]


def test_kind_desk_is_forwarded_and_a_repeat_of_its_key_is_not(
    engine: Engine, client, fake: FakeTelegram
) -> None:
    from onelot.notify.base import Notification, NotificationLevel

    save_keys(client)
    for _ in range(2):
        engine.notifier.send(
            Notification(title="Desk is up", body="Desk up at 09:05.", kind="desk",
                         level=NotificationLevel.WARNING, dedup_key="desk:morning:2026-11-25")
        )  # fmt: skip
    settle(engine, fake, 1)
    assert "Desk up at 09:05." in fake.sent[0]


def test_in_mock_mode_without_keys_nothing_leaves_the_computer(
    engine: Engine, fake: FakeTelegram
) -> None:
    fake_desk(engine)
    engine._desk_started(at(25, "09:02"))
    engine._desk_heartbeat(at(25, "09:02") + DESK_START_SETTLE)
    engine._desk_messages.is_trading_day = lambda d: True
    engine._desk_heartbeat(at(25, "09:06"))
    shown = [n["title"] for n in engine.notifier.dashboard.recent()]
    assert "Desk started" in shown and "Desk is up" in shown  # the page has them
    with engine.db.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(telegram_outbox)).scalar() == 0
    assert fake.requests == [] and engine.telegram_client is None


def test_without_a_desk_there_is_no_heartbeat_and_no_message(engine: Engine) -> None:
    engine.desk = None
    engine._desk_started(at(25, "09:02"))
    engine._desk_heartbeat(at(25, "09:06"))
    assert engine._desk_messages.sent == []
    assert engine._uptime.writes == 0


def test_a_failing_heartbeat_never_raises(engine: Engine, monkeypatch) -> None:
    fake_desk(engine)

    def boom(now):
        raise RuntimeError("disk full")

    monkeypatch.setattr(engine._uptime, "beat", boom)
    monkeypatch.setattr(engine._desk_messages, "look", boom)
    engine._desk_started(at(25, "09:02"))
    engine._desk_heartbeat(at(25, "09:06"))  # no exception
