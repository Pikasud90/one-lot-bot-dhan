"""US-087 AC1: the Dhan token reminder ladder, over a simulated clock.

The decision function is pure; the step adds the memory (SQLite) and the words. No network.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from onelot.clock import IST
from onelot.notify.base import Notification, NotificationLevel
from onelot.notify.token_reminders import (
    STAGE_MINUTES,
    Reminder,
    ReminderMemory,
    ReminderState,
    TokenReminderStep,
    decide,
    reminder_text,
    when_text,
)
from onelot.store import init_database

E = dt.datetime(2026, 11, 25, 21, 31, tzinfo=IST)  # a Wednesday
E2 = E + dt.timedelta(days=1)


def at(day: int, hh: int, mm: int, ss: int = 0) -> dt.datetime:
    return dt.datetime(2026, 11, day, hh, mm, ss, tzinfo=IST)


def weekday(d: dt.date) -> bool:
    return d.weekday() < 5


class Rig:
    """A step with a database, a readable expiry, and a list of what was sent."""

    def __init__(self, path: Path, expiry: dt.datetime | None = E) -> None:
        self.db = init_database(f"sqlite:///{path}")
        self.expiry = expiry
        self.sent: list[Notification] = []
        self.self_renew = False
        self.trading = weekday
        self.step = self.build()

    def build(self) -> TokenReminderStep:
        """A NEW step on the same database: what a restart does."""
        return TokenReminderStep(
            ReminderMemory(self.db.engine),
            self.sent.append,
            lambda: self.expiry,
            lambda d: self.trading(d),
            lambda: self.self_renew,
        )

    def run(self, start: dt.datetime, end: dt.datetime, every_s: int = 60) -> list[tuple[str, str]]:
        """Look every `every_s` seconds from start to end; (time, title) of each message."""
        out: list[tuple[str, str]] = []
        now = start
        while now <= end:
            before = len(self.sent)
            self.step.look(now)
            for n in self.sent[before:]:
                out.append((now.strftime("%d %H:%M"), n.title))
            now += dt.timedelta(seconds=every_s)
        return out


@pytest.fixture
def rig(tmp_path: Path):
    r = Rig(tmp_path / "t.db")
    yield r
    r.db.dispose()


# ── the stages, each once ───────────────────────────────────────────────────────
def test_every_stage_fires_once_in_order_over_a_whole_evening(rig: Rig) -> None:
    got = rig.run(at(25, 19, 0), at(25, 22, 30))
    assert got == [
        ("25 20:31", "Dhan token: 60 minutes left"),
        ("25 21:01", "Dhan token: 30 minutes left"),
        ("25 21:16", "Dhan token: 15 minutes left"),
        ("25 21:26", "Dhan token: 5 minutes left"),
        ("25 21:31", "Dhan token expired"),
    ]


def test_each_message_is_a_warning_of_kind_token_with_its_own_key(rig: Rig) -> None:
    rig.run(at(25, 20, 0), at(25, 21, 40))
    assert {n.level for n in rig.sent} == {NotificationLevel.WARNING}
    assert {n.kind for n in rig.sent} == {"token"}
    keys = [n.dedup_key for n in rig.sent]
    assert len(set(keys)) == len(keys) == 5
    assert keys == [f"token:2026-11-25T21:31+05:30:{stage}" for stage in (60, 30, 15, 5)] + [
        "token:2026-11-25T21:31+05:30:expired"
    ]


@pytest.mark.parametrize(
    "start, titles",
    [
        (at(25, 21, 28), ["Dhan token: 3 minutes left", "Dhan token expired"]),
        (at(25, 21, 31), ["Dhan token expired"]),
        (at(25, 22, 5), ["Dhan token expired"]),
    ],
)
def test_a_start_late_in_the_ladder_sends_only_what_is_still_ahead(
    rig: Rig, start: dt.datetime, titles: list[str]
) -> None:
    assert [t for _, t in rig.run(start, at(25, 22, 30))] == titles


def test_a_start_at_23_minutes_left_says_23_then_15_5_expired(rig: Rig) -> None:
    got = rig.run(at(25, 21, 8), at(25, 21, 40))
    assert [t for _, t in got] == [
        "Dhan token: 23 minutes left",
        "Dhan token: 15 minutes left",
        "Dhan token: 5 minutes left",
        "Dhan token expired",
    ]
    assert "23 minutes left" in rig.sent[0].title


def test_the_minutes_are_the_true_time_left_rounded_up(rig: Rig) -> None:
    rig.step.look(at(25, 21, 8, 30))  # 22 min 30 s left
    assert rig.sent[0].title == "Dhan token: 23 minutes left"
    r2 = Rig(Path(rig.db.engine.url.database).with_name("t2.db"))
    r2.step.look(at(25, 21, 30, 10))  # 50 s left
    assert r2.sent[0].title == "Dhan token: 1 minute left"
    r2.db.dispose()


# ── a restart repeats nothing ────────────────────────────────────────────────────
def test_a_restart_repeats_nothing_and_continues_the_ladder(rig: Rig) -> None:
    first = rig.run(at(25, 20, 30), at(25, 21, 2))
    assert [t for _, t in first] == ["Dhan token: 60 minutes left", "Dhan token: 30 minutes left"]
    rig.step = rig.build()  # a restart: a new object, the same database
    assert rig.step.look(at(25, 21, 3)) == []
    again = rig.run(at(25, 21, 3), at(25, 21, 40))
    assert [t for _, t in again] == [
        "Dhan token: 15 minutes left",
        "Dhan token: 5 minutes left",
        "Dhan token expired",
    ]
    rig.step = rig.build()
    assert rig.run(at(25, 21, 41), at(25, 22, 0)) == []  # expired is not told twice


def test_the_memory_is_in_the_database_not_in_the_object(rig: Rig) -> None:
    rig.step.look(at(25, 20, 40))
    state = ReminderMemory(rig.db.engine).load()
    assert state.expiry == E and state.sent == frozenset({60})


# ── a renewal re-arms and announces ──────────────────────────────────────────────
def test_a_renewal_announces_once_and_arms_the_ladder_again(rig: Rig) -> None:
    rig.run(at(25, 20, 30), at(25, 21, 10))  # 60 and 30 sent
    rig.expiry = E2  # renewed at 21:12: valid until 21:31 the next day
    got = rig.run(at(25, 21, 11), at(25, 21, 20))
    assert [t for _, t in got] == ["Dhan token renewed: valid until 21:31 on 26 Nov"]
    assert rig.run(at(25, 21, 21), at(26, 20, 29)) == []  # a day of quiet
    again = rig.run(at(26, 20, 30), at(26, 21, 40))
    assert [t for _, t in again] == [
        "Dhan token: 60 minutes left",
        "Dhan token: 30 minutes left",
        "Dhan token: 15 minutes left",
        "Dhan token: 5 minutes left",
        "Dhan token expired",
    ]


def test_a_renewal_after_expiry_announces_and_stops_the_morning_reminder(rig: Rig) -> None:
    rig.run(at(25, 21, 0), at(25, 21, 40))
    rig.expiry = E2
    got = rig.run(at(26, 8, 0), at(26, 9, 30))
    assert [t for _, t in got] == ["Dhan token renewed: valid until 21:31 on 26 Nov"]


def test_the_first_ever_reading_is_not_announced_as_a_renewal(rig: Rig) -> None:
    rig.step.look(at(25, 10, 0))
    assert rig.sent == []


# ── an unreadable expiry ─────────────────────────────────────────────────────────
def test_an_unreadable_expiry_is_reported_once_then_again_only_after_a_good_reading(
    rig: Rig,
) -> None:
    rig.expiry = None
    got = rig.run(at(25, 10, 0), at(25, 10, 30))
    assert [t for _, t in got] == ["Cannot read when the Dhan token expires"]
    rig.expiry = E
    assert rig.run(at(25, 10, 31), at(25, 10, 40)) == []  # readable again, no announcement
    rig.expiry = None
    again = rig.run(at(25, 11, 0), at(25, 11, 10))
    assert len(again) == 1


def test_an_expiry_that_becomes_unreadable_after_it_passed_counts_as_expired(rig: Rig) -> None:
    rig.step.look(at(25, 21, 0))  # 31 min left: stage 60 (the only due stage not sent)
    rig.sent.clear()
    rig.expiry = None  # the profile can no longer be fetched: the token ran out
    rig.step.look(at(25, 21, 32))
    assert [n.title for n in rig.sent] == ["Dhan token expired"]


# ── 08:30 while still expired ────────────────────────────────────────────────────
def test_the_morning_reminder_comes_once_per_trading_day_while_expired(rig: Rig) -> None:
    rig.run(at(25, 21, 0), at(25, 21, 40))  # 60.., expired
    rig.sent.clear()
    got = rig.run(at(26, 8, 0), at(26, 11, 0))  # Thursday
    assert got == [("26 08:30", "Dhan token is still expired")]
    assert "before 09:15" in rig.sent[0].body
    fri = rig.run(at(27, 8, 0), at(27, 11, 0))
    assert fri == [("27 08:30", "Dhan token is still expired")]


def test_no_morning_reminder_on_a_holiday_or_a_weekend(rig: Rig) -> None:
    rig.run(at(25, 21, 0), at(25, 21, 40))
    rig.sent.clear()
    rig.trading = lambda d: d != dt.date(2026, 11, 26)  # Thursday is a holiday
    assert rig.run(at(26, 8, 0), at(26, 10, 0)) == []
    rig.trading = weekday
    assert rig.run(at(28, 8, 0), at(28, 10, 0)) == []  # Saturday
    assert rig.run(at(29, 8, 0), at(29, 10, 0)) == []  # Sunday
    assert len(rig.run(at(30, 8, 0), at(30, 10, 0))) == 1  # Monday


def test_no_morning_reminder_while_the_token_is_valid(rig: Rig) -> None:
    rig.expiry = E2
    rig.step.look(at(25, 12, 0))
    assert rig.run(at(26, 8, 0), at(26, 9, 0)) == []


def test_no_second_morning_message_when_expired_was_told_that_morning(rig: Rig) -> None:
    rig.expiry = at(26, 8, 45)  # expires in the morning, after 08:30
    rig.run(at(26, 8, 40), at(26, 9, 0))
    assert [n.title for n in rig.sent] == ["Dhan token: 5 minutes left", "Dhan token expired"]


def test_the_morning_reminder_survives_a_restart_without_repeating(rig: Rig) -> None:
    rig.run(at(25, 21, 0), at(25, 21, 40))
    rig.run(at(26, 8, 0), at(26, 8, 40))
    n = len(rig.sent)
    rig.step = rig.build()
    assert rig.run(at(26, 8, 41), at(26, 9, 14)) == []
    assert len(rig.sent) == n


# ── US-097: a desk that first looks after 09:15 still sends the day's reminder ───────
def test_a_first_look_at_10_30_on_a_trading_day_sends_one_reminder(rig: Rig) -> None:
    rig.run(at(25, 21, 0), at(25, 21, 40))  # expired on Wednesday evening
    rig.sent.clear()
    rig.step = rig.build()  # the desk was stopped overnight and started at 10:30
    got = rig.run(at(26, 10, 30), at(26, 15, 0))
    assert got == [("26 10:30", "Dhan token is still expired")]
    assert "market is open" in rig.sent[0].body
    assert "before 09:15" not in rig.sent[0].body
    assert rig.sent[0].dedup_key == f"token:{E.isoformat(timespec='minutes')}:morning:2026-11-26"


def test_a_second_look_the_same_day_sends_no_second_reminder(rig: Rig) -> None:
    rig.run(at(25, 21, 0), at(25, 21, 40))
    rig.sent.clear()
    rig.step.look(at(26, 10, 30))
    rig.step = rig.build()  # a restart at noon
    assert rig.run(at(26, 12, 0), at(26, 15, 29)) == []
    assert len(rig.sent) == 1


def test_a_late_first_look_on_a_non_trading_day_sends_none(rig: Rig) -> None:
    rig.run(at(25, 21, 0), at(25, 21, 40))
    rig.sent.clear()
    rig.trading = lambda d: weekday(d) and d != dt.date(2026, 11, 26)
    assert rig.run(at(26, 10, 30), at(26, 12, 0)) == []
    assert rig.run(at(28, 10, 30), at(28, 12, 0)) == []  # Saturday


def test_no_reminder_after_the_close(rig: Rig) -> None:
    rig.run(at(25, 21, 0), at(25, 21, 40))
    rig.sent.clear()
    rig.step = rig.build()
    assert rig.run(at(26, 15, 30), at(26, 18, 0)) == []


# ── the loop that hosts it ticks faster than a minute ─────────────────────────────
def test_looking_again_within_55_seconds_does_nothing(rig: Rig) -> None:
    rig.step.look(at(25, 21, 0))
    rig.expiry = None  # would be reported as unreadable if it looked again
    assert rig.step.look(at(25, 21, 0, 30)) == []
    assert not any("Cannot read" in n.title for n in rig.sent)
    rig.step.look(at(25, 21, 1))  # a minute on: it looks
    assert any("Cannot read" in n.title for n in rig.sent)


# ── the pure function, directly ──────────────────────────────────────────────────
def test_decide_is_pure_and_does_not_mutate_its_input() -> None:
    s0 = ReminderState()
    s1, out = decide(s0, E, at(25, 21, 20), True)
    assert s0 == ReminderState() and [r.stage for r in out] == [15]
    assert s1.sent == frozenset({60, 30, 15})
    s2, out2 = decide(s1, E, at(25, 21, 21), True)
    assert out2 == [] and s2 == s1


def test_the_stages_are_60_30_15_5() -> None:
    assert STAGE_MINUTES == (60, 30, 15, 5)


# ── the words ────────────────────────────────────────────────────────────────────
def test_the_text_names_the_time_in_ist_and_what_to_do() -> None:
    title, body = reminder_text(Reminder("stage", 30, 23, "k"), E, False)
    assert title == "Dhan token: 23 minutes left"
    assert "21:31 IST on 25 Nov" in body
    assert "Settings, Dhan card: Renew, or paste a new token." in body
    assert "by itself" not in body


def test_the_text_says_the_desk_will_try_when_it_can_renew_itself() -> None:
    for kind in ("stage", "expired", "morning"):
        _, body = reminder_text(Reminder(kind, 5, 5, "k"), E, True)  # type: ignore[arg-type]
        assert "try to renew it by itself" in body


def test_the_time_is_shown_in_ist_whatever_zone_it_came_in() -> None:
    utc = dt.datetime(2026, 11, 25, 16, 1, tzinfo=dt.UTC)
    assert when_text(utc) == "21:31 on 25 Nov"


def test_no_text_carries_anything_token_like(rig: Rig) -> None:
    rig.run(at(25, 20, 0), at(25, 21, 40))
    for n in rig.sent:
        blob = f"{n.title} {n.body} {n.payload}"
        assert "eyJ" not in blob and "access" not in blob.lower()
