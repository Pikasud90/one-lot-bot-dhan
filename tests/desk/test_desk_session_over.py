"""US-004 review round 1, decision (E): an intraday position from an earlier session.

The broker squares an intraday position off at the close. One the owner never recorded as
closed must not raise trading advice the next day (an EXIT on the phone would tell him to
trade a position he no longer has). It raises no EXIT and no reminders; it says once, as an
INFO (which never reaches Telegram), that the session is over and the broker's square-off
should be recorded. A swing position is carried on purpose and is managed as before.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import update
from test_desk import OpenClock
from test_desk_stock_short_intraday import _desk, _raise

from onelot.clock import IST, now_ist
from onelot.data.schema import desk_positions
from onelot.desk import Desk
from onelot.notify.base import NotificationLevel
from onelot.store import init_database

SESSION_OVER = "session over: record the broker square-off"


@pytest.fixture
def db(tmp_path):
    from onelot.data.db import get_database, use_database

    d = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    use_database(d)
    yield d
    d.dispose()
    get_database.__globals__["_db"] = None


def _taken(desk: Desk, direction: str) -> str:
    sig = _raise(desk, direction)
    return desk.acknowledge(sig["id"], "TAKE")["position_id"]


def _through_the_stop(desk: Desk, pid: str) -> None:
    p = desk.positions[pid]
    beyond = 1.0 if p["direction"] == "short" else -1.0
    bars = desk.trackers["SBIN"].bars
    bars.loc[bars.index[-1], "close"] = p["stop_underlying"] + beyond


def _opened_yesterday(desk: Desk, pid: str) -> None:
    """Move the position's opening to 10:00 yesterday, in memory and in its row (a restart
    reads opened_at from the column)."""
    yesterday = now_ist().date() - dt.timedelta(days=1)
    opened = dt.datetime.combine(yesterday, dt.time(10, 0), tzinfo=IST)
    desk.positions[pid]["opened_at"] = opened.isoformat()
    with desk.engine.begin() as conn:
        conn.execute(
            update(desk_positions).where(desk_positions.c.id == pid).values(opened_at=opened)
        )


def _manage(desk: Desk, at: dt.datetime) -> list:
    """Manage once at `at`; the notifications it produced."""
    before = len(desk.notifier.sent)
    desk._manage_positions(at, OpenClock().current_window(at))
    return desk.notifier.sent[before:]


def _today(hh: int, mm: int) -> dt.datetime:
    return dt.datetime.combine(now_ist().date(), dt.time(hh, mm), tzinfo=IST)


def test_an_intraday_position_from_an_earlier_session_raises_no_exit(cfg, db):
    desk = _desk(cfg, db)
    pid = _taken(desk, "short")  # a stock short: intraday only
    _opened_yesterday(desk, pid)
    _through_the_stop(desk, pid)

    sent = _manage(desk, _today(10, 0)) + _manage(desk, _today(10, 1))

    p = desk.positions[pid]
    assert p["exit_advice"] is None
    assert p["hold_check"] == SESSION_OVER
    assert [n.level for n in sent] == [NotificationLevel.INFO], "one INFO, never an EXIT"
    assert "square-off" in sent[0].title + sent[0].body
    assert p["session_over"] is True


def test_the_session_over_note_is_said_once_even_after_a_restart(cfg, db):
    desk = _desk(cfg, db)
    pid = _taken(desk, "short")
    _opened_yesterday(desk, pid)
    assert [n.level for n in _manage(desk, _today(10, 0))] == [NotificationLevel.INFO]

    restarted = _desk(cfg, db)  # a new process: the position comes back from the database
    sent = restarted.notifier.sent + _manage(restarted, _today(11, 0))

    assert pid in restarted.positions
    assert [n for n in sent if "square-off" in n.title + n.body] == []
    assert restarted.positions[pid]["hold_check"] == SESSION_OVER


def _closed(desk: Desk, pid: str) -> dict:
    """The journal row of a position the desk closed."""
    return next(j for j in desk.journal() if j["id"] == pid)


def test_the_same_session_intraday_position_still_gets_its_exit(cfg, db):
    desk = _desk(cfg, db)
    pid = _taken(desk, "short")
    _through_the_stop(desk, pid)

    sent = _manage(desk, now_ist()) + _manage(desk, now_ist())

    # OF-5: no paper book here, so the desk's advice is the alert's one exit: told once,
    # and the position closes at the desk's price (never re-advised every poll)
    assert pid not in desk.positions
    assert [n.level for n in sent].count(NotificationLevel.EXIT) == 1
    row = _closed(desk, pid)
    assert row["exit_reason"] == "STOP" and row["exit_source"] == "desk"


def test_a_swing_long_from_an_earlier_session_is_managed_as_before(cfg, db):
    desk = _desk(cfg, db)
    pid = _taken(desk, "long")  # stock BUY: stock_horizon swing
    _opened_yesterday(desk, pid)
    _through_the_stop(desk, pid)

    sent = _manage(desk, _today(10, 0))

    assert pid not in desk.positions
    assert [n.level for n in sent].count(NotificationLevel.EXIT) == 1
    assert _closed(desk, pid)["exit_reason"] == "STOP"


def test_the_note_also_covers_a_position_the_broker_did_not_close(cfg, db):
    """The broker squares off an intraday (MIS) position, but not one bought NRML (an option
    held past the close): the note must be right either way, and say the desk stops watching."""
    desk = _desk(cfg, db)
    pid = _taken(desk, "short")
    _opened_yesterday(desk, pid)

    (note,) = _manage(desk, _today(10, 0))

    assert desk.positions[pid]["hold_check"] == SESSION_OVER, "the card text is unchanged"
    assert "If the broker squared it off at the close" in note.body
    assert (
        "If you still hold it, the desk no longer watches it: close it on your terminal, "
        "then record the exit with Closed." in note.body
    )
    assert "session; the broker squared it off" not in note.body, "never claims it was closed"
