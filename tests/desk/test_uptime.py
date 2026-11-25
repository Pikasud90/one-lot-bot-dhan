"""US-097 points 1 and 2: the desk's heartbeat, the cover per trading day, and the messages that
say it is up. A simulated clock and a throw-away database; no network."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import select

from onelot.clock import IST, SessionClock, TradingCalendar
from onelot.config import SessionCfg
from onelot.desk.uptime import (
    DeskMessages,
    UptimeRecorder,
    coverage,
    facts_text,
    feed_word,
    start_text,
)
from onelot.notify.base import Notification, NotificationLevel
from onelot.store import init_database
from onelot.store.schema import desk_uptime

HOLIDAY = dt.date(2026, 11, 10)  # a Tuesday made a holiday for the test
CAL = TradingCalendar({HOLIDAY: "Test holiday"}, verified=True)
CLOCK = SessionClock(SessionCfg(), CAL)


def at(day: int, hhmm: str, month: int = 11) -> dt.datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return dt.datetime(2026, month, day, h, m, tzinfo=IST)


@pytest.fixture
def db(tmp_path: Path):
    d = init_database(f"sqlite:///{tmp_path / 'u.db'}")
    yield d
    d.dispose()


def recorder(db) -> UptimeRecorder:
    return UptimeRecorder(db.engine, CLOCK, CAL.is_trading_day)


def rows(db) -> dict[str, dict]:
    with db.engine.connect() as conn:
        return {r.day: dict(r._mapping) for r in conn.execute(select(desk_uptime))}


def run(rec: UptimeRecorder, start: dt.datetime, end: dt.datetime, every_s: int = 60) -> int:
    """Call `beat` every `every_s` seconds (the housekeeping loop calls it every 10 s)."""
    writes, now = 0, start
    while now <= end:
        writes += rec.beat(now)
        now += dt.timedelta(seconds=every_s)
    return writes


# ── the heartbeat ─────────────────────────────────────────────────────────────
def test_beats_are_throttled_to_one_write_every_five_minutes(db) -> None:
    rec = recorder(db)
    assert rec.beat(at(23, "09:00")) is True
    assert rec.beat(at(23, "09:02")) is False
    assert rec.beat(at(23, "09:04") + dt.timedelta(seconds=59)) is False
    assert rec.beat(at(23, "09:05")) is True
    writes = run(rec, at(23, "09:05"), at(23, "10:05"), every_s=10)
    assert writes == 12  # 09:10, 09:15, ... 10:05
    r = rows(db)["2026-11-23"]
    assert r["beats"] == 14 and r["started_late"] is False


def test_a_restart_continues_the_days_row_and_a_gap_is_not_counted(db) -> None:
    rec = recorder(db)
    run(rec, at(23, "09:00"), at(23, "10:00"))
    first = rows(db)["2026-11-23"]
    assert first["session_minutes"] == pytest.approx(45.0)  # 09:15 to 10:00
    again = recorder(db)  # a restart at 11:00 (the desk was down from 10:00)
    run(again, at(23, "11:00"), at(23, "12:00"))
    r = rows(db)["2026-11-23"]
    assert r["beats"] == first["beats"] + 13
    assert r["first_seen"].astimezone(IST) == at(23, "09:00")
    assert r["last_seen"].astimezone(IST) == at(23, "12:00")
    assert r["session_minutes"] == pytest.approx(45.0 + 60.0)  # 10:00 to 11:00 is not covered
    assert r["started_late"] is False  # the day's first start was before the open


def test_a_first_start_after_09_15_on_a_trading_day_is_late_and_not_on_a_holiday(db) -> None:
    rec = recorder(db)
    rec.beat(at(23, "11:01"))
    rec.beat(at(HOLIDAY.day, "11:01"))
    rec.beat(at(22, "11:01"))  # a Sunday
    r = rows(db)
    assert r["2026-11-23"]["started_late"] is True
    assert r["2026-11-10"]["started_late"] is False and r["2026-11-22"]["started_late"] is False


# ── the cover per trading day: a hand-made month ───────────────────────────────────
def test_the_cover_of_a_month_with_a_holiday_a_missed_day_and_a_late_start(db) -> None:
    rec = recorder(db)
    for day in (3, 4, 5, 6, 9, 11, 12):  # whole sessions (10 Nov is the holiday)
        run(rec, at(day, "09:00"), at(day, "15:40"))
    # 13 Nov: missed. 16 Nov: started late at 11:01, ran to the close
    run(rec, at(16, "11:01"), at(16, "15:40"))
    got = {c.day.isoformat(): c for c in coverage(db.engine, CLOCK, CAL.is_trading_day,
                                                   at(16, "18:00"), days=16)}  # fmt: skip
    assert "2026-11-10" not in got  # the holiday is no session
    assert "2026-11-07" not in got and "2026-11-08" not in got  # a weekend neither
    full = got["2026-11-04"].as_dict()
    assert full["ran_from"] == "09:15" and full["ran_to"] == "15:30"
    assert full["share"] == pytest.approx(1.0, abs=0.01) and full["missed"] is False
    missed = got["2026-11-13"].as_dict()
    assert missed["missed"] is True and missed["share"] == 0 and missed["ran_from"] is None
    late = got["2026-11-16"].as_dict()
    assert late["started_late"] is True and late["ran_from"] == "11:01"
    assert late["share"] == pytest.approx((15 * 60 + 30 - (11 * 60 + 1)) / 375, abs=0.01)
    before = got["2026-11-02"].as_dict()  # before the first row ever: nothing can be said
    assert before["known"] is False and before["missed"] is False


def test_today_counts_the_session_so_far_and_is_not_finished(db) -> None:
    rec = recorder(db)
    run(rec, at(23, "09:00"), at(23, "12:15"))
    (today,) = [c for c in coverage(db.engine, CLOCK, CAL.is_trading_day, at(23, "12:15"), days=1)]
    d = today.as_dict()
    assert d["finished"] is False and d["missed"] is False
    assert d["share"] == pytest.approx(1.0, abs=0.01)


# ── the messages ─────────────────────────────────────────────────────────────
def test_the_start_message_names_the_time_the_token_and_the_feed() -> None:
    title, body = start_text(at(25, "09:02"), at(25, "21:31"), "live", late=False)
    assert title == "Desk started"
    assert body == "Desk started 09:02. Token valid until 21:31. Feed: live."


def test_a_late_start_adds_what_was_missed() -> None:
    title, body = start_text(at(23, "10:57"), at(23, "21:31"), "live", late=True)
    assert title == "Desk started late"
    assert body == (
        "Desk started 10:57. Token valid until 21:31. Feed: live. "
        "Started late: alerts before 10:57 were missed."
    )


def test_the_token_words_for_another_day_an_expired_and_an_unknown_token() -> None:
    assert "Token valid until 08:00 on 26 Nov." in facts_text(
        "x", at(25, "09:00"), at(26, "08:00"), "live"
    )
    assert "Token expired at 21:31 on 24 Nov." in facts_text(
        "x", at(25, "09:00"), at(24, "21:31"), "live"
    )
    assert "Token: expiry not known." in facts_text("x", at(25, "09:00"), None, "live")
    assert (
        feed_word("yahoo", True).startswith("delayed") and feed_word("dhan+yahoo", False) == "live"
    )


class Sent:
    def __init__(self) -> None:
        self.items: list[Notification] = []

    def __call__(self, n: Notification) -> None:
        self.items.append(n)


def messages(sent: Sent, expiry=None, feed="live") -> DeskMessages:
    return DeskMessages(sent, CLOCK, CAL.is_trading_day, lambda: expiry, lambda: feed)


def test_the_start_message_is_a_desk_warning_with_a_key_per_day() -> None:
    sent = Sent()
    m = messages(sent, at(25, "21:31"))
    key = m.started(at(25, "09:02"), at(25, "09:03"))
    (n,) = sent.items
    assert key == "desk:start:2026-11-25" and n.dedup_key == key
    assert n.kind == "desk" and n.level == NotificationLevel.WARNING
    assert n.body == "Desk started 09:02. Token valid until 21:31. Feed: live."


def test_a_start_after_09_15_on_a_trading_day_says_late_and_after_the_close_not() -> None:
    sent = Sent()
    m = messages(sent, at(23, "21:31"))
    m.started(at(23, "10:57"))
    m.started(at(23, "16:30"))
    m.started(at(HOLIDAY.day, "10:57"))
    late, evening, holiday = sent.items
    assert "Started late: alerts before 10:57 were missed." in late.body
    assert "Started late" not in evening.body and "Started late" not in holiday.body


def test_the_morning_check_comes_once_a_trading_day_between_09_05_and_09_15() -> None:
    sent = Sent()
    m = messages(sent, at(25, "21:31"))
    now = at(25, "08:50")
    while now <= at(25, "12:00"):
        m.look(now)
        now += dt.timedelta(seconds=10)
    (n,) = sent.items
    assert n.dedup_key == "desk:morning:2026-11-25" and n.kind == "desk"
    assert n.body == "Desk up at 09:05. Token valid until 21:31. Feed: live."
    for day in (HOLIDAY.day, 22):  # a holiday and a Sunday
        assert m.look(at(day, "09:06")) is None
    assert m.look(at(26, "09:20")) is None  # a desk that comes up after 09:15: the start says it
    assert len(sent.items) == 1
