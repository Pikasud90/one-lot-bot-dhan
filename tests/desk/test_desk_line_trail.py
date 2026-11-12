"""The desk's side of a strategy that trails its stop along its own line (the owner's
Supertrend rule, 2026-11-11): what the desk tells an alert, a taken position and the paper
book about that line, and how it keeps the stored days the trend ladder's daily timeframe
needs.

    _raise_signal        an alert of a line-trailing slot carries manage["line_trail"] = True
    _manage_positions    a taken position's stop follows the level after each closed bar
    _paper_trail_level   the paper book's question: (level in rupees, when its bar closed)
    _tell_paper_trail    the desk hands the book that question once, when it starts
    _load_daily_history  each tracker's stored days: once a day, asked again when it fails
    reload               a new configuration keeps the bars and the stored days

Offline: the feed is the harness's random walk and the paper book a recorder. Where a test
needs an exact level the slot's state (its last closed bar, the level its line allows) is
set by hand, the way the tracker leaves it after a bar; two tests let the real strategy
set it.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging

import pandas as pd
import pytest
from desk_harness import OpenClock, ShutClock, new_desk, small_cfg
from sqlalchemy import select

from onelot.clock import IST, now_ist
from onelot.data.resample import resample_bars
from onelot.data.schema import desk_positions
from onelot.data.store import save_bars
from onelot.desk import Desk
from onelot.desk.config import DeskConfig, DeskInstrumentCfg, StrategySlotCfg
from onelot.desk.desk import HISTORY_EMPTY_RETRY, HISTORY_RETRY
from onelot.desk.tracker import StrategySlot, Verdict
from onelot.notify.base import NotificationLevel
from onelot.paper.position import TrailLevel, rules_from_alert, trail_level_of
from onelot.store import init_database

LINE = StrategySlotCfg("supertrend_vol", "6m", {"line_trail": True})  # the line is the trail
PLAIN = StrategySlotCfg("ema_cross", "10m")  # a strategy with no line of its own
SIX = dt.timedelta(minutes=6)

NO_DAYS = "the store has no daily history for it: no 1d trend reading (run a backfill)"
UNREADABLE_DAYS = "the stored daily history could not be read: no 1d trend reading"


@pytest.fixture
def db(tmp_path):
    from onelot.data.db import get_database, use_database

    d = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    use_database(d)
    yield d
    d.dispose()
    get_database.__globals__["_db"] = None


def line_cfg() -> DeskConfig:
    """Two stocks, each with a slot that trails along its own line and one that does not."""
    stocks = tuple(DeskInstrumentCfg(k, k, "STOCK", (LINE, PLAIN)) for k in ("SBIN", "RELIANCE"))
    return small_cfg(indices=(), stocks=stocks, stock_horizon="swing")


def cold_desk(cfg, db, **kw) -> Desk:
    """A desk that has not polled yet."""
    desk, _, _ = new_desk(cfg, db, desk_cfg=line_cfg(), clock=ShutClock(), **kw)
    return desk


def warm_desk(cfg, db, **kw) -> Desk:
    """A desk that has polled once with the market shut: every slot has read its bars and
    nothing has been alerted."""
    desk = cold_desk(cfg, db, **kw)
    desk.poll()
    assert not desk.signals and not desk.positions
    return desk


def line_slot(desk: Desk, key: str = "SBIN") -> StrategySlot:
    return desk.trackers[key].slots[0]


def plain_slot(desk: Desk, key: str = "SBIN") -> StrategySlot:
    return desk.trackers[key].slots[1]


def on_day(desk: Desk, hh: int, mm: int, ss: int = 0) -> dt.datetime:
    """A moment of the last session the desk has bars of."""
    day = desk.trackers["SBIN"].bars.index[-1].date()
    return dt.datetime.combine(day, dt.time(hh, mm, ss), tzinfo=IST)


def say(slot: StrategySlot, bar_open: dt.datetime, *, long=None, short=None) -> None:
    """Leave the slot the way the tracker does after the bar that opened at `bar_open`: its
    line allows `long` / `short` as the stop of an open trade (None: the line is not on that
    side), and the strategy says nothing else about an open position."""
    slot.line_trail = True
    slot.last_trail = {"long": long, "short": short}
    slot.last_bar_ts = bar_open
    slot.last_flags = {}


def price_of(desk: Desk, key: str = "SBIN") -> float:
    return float(desk.trackers[key].last_price())


def set_price(desk: Desk, price: float, key: str = "SBIN") -> None:
    bars = desk.trackers[key].bars
    bars.loc[bars.index[-1], "close"] = price


def alert(desk: Desk, slot: StrategySlot, direction: str = "long", key: str = "SBIN") -> dict:
    """A fresh `direction` alert of `slot` on the last bar, its stop 1 % beyond the close."""
    t = desk.trackers[key]
    close = float(t.bars["close"].iloc[-1])
    stop = close * (0.99 if direction == "long" else 1.01)
    verdict = Verdict(
        direction, "test", t.bars.index[-1].to_pydatetime(), stop, close, 60.0, [], True
    )
    before = set(desk.signals)
    desk._raise_signal(t, slot, verdict, now_ist())
    (sid,) = set(desk.signals) - before
    return desk.signals[sid]


def taken(desk: Desk, slot: StrategySlot, direction: str = "long", *, opened: dt.datetime) -> dict:
    """The position of a taken alert of `slot`, opened at `opened`."""
    sig = alert(desk, slot, direction)
    pid = desk.acknowledge(sig["id"], "TAKE")["position_id"]
    position = desk.positions[pid]
    position["opened_at"] = opened.isoformat()
    return position


def manage(desk: Desk, at: dt.datetime) -> None:
    desk._manage_positions(at, OpenClock().current_window(at))


def between(position: dict, desk: Desk) -> float:
    """A level halfway between the position's stop and the price: nearer than the stop."""
    return round((position["stop_underlying"] + price_of(desk)) / 2, 2)


def closed_row(desk: Desk, pid: str) -> dict:
    return next(j for j in desk.journal() if j["id"] == pid)


def stored_position(desk: Desk, pid: str) -> dict:
    with desk.engine.connect() as conn:
        stmt = select(desk_positions.c.payload).where(desk_positions.c.id == pid)
        return dict(conn.execute(stmt).scalar_one())


class Book:
    """A recording paper book from before the line trail: it cannot be asked to follow one."""

    def __init__(self) -> None:
        self.alerts: list[dict] = []

    def on_alert(self, alert, now):
        self.alerts.append(alert)

    def step(self, now, bars, strategy_exit=None):
        return []

    def open_trades(self):
        return []

    def for_alert(self, alert_id):
        return None

    def status(self):
        return {}


class TrailBook(Book):
    """A recording paper book that takes the desk's trail question."""

    def __init__(self) -> None:
        super().__init__()
        self.followed: list = []

    def follow_trail(self, ask):
        self.followed.append(ask)


@dataclasses.dataclass
class Trade:
    """What the paper book shows the desk of one of its trades."""

    series_key: str = "SBIN"
    strategy: str = LINE.name
    timeframe: str = LINE.timeframe
    direction: str = "long"


# ── the alert ────────────────────────────────────────────────────────────────────────


def test_the_real_strategy_marks_its_slot_and_gives_the_level(cfg, db):
    """Nothing set by hand: after a poll the slot of a strategy that trails along its line
    holds the level that line allows, on the one side the line is on, below (above) the
    close of its bar."""
    desk = warm_desk(cfg, db)
    slot, plain = line_slot(desk), plain_slot(desk)

    assert slot.line_trail is True and plain.line_trail is False
    assert plain.trail_view() is None
    view = slot.trail_view()
    assert view is not None and view["timeframe"] == "6m"
    (side,) = [s for s in ("long", "short") if view[s] is not None]
    bar = resample_bars(desk.trackers["SBIN"].bars, "6m").loc[pd.Timestamp(slot.last_bar_ts)]
    if side == "long":
        assert view["long"] < bar["close"]
    else:
        assert view["short"] > bar["close"]


def test_an_alert_of_a_slot_that_trails_along_its_line_says_so(cfg, db):
    desk = warm_desk(cfg, db)

    sig = alert(desk, line_slot(desk))

    assert sig["manage"]["line_trail"] is True
    rules = rules_from_alert(sig, tick_paise=5, resolution=2, timeframe=sig["timeframe"])
    assert rules.line_trail is True


def test_an_alert_of_any_other_slot_does_not_carry_the_rule(cfg, db):
    desk = warm_desk(cfg, db)

    sig = alert(desk, plain_slot(desk))

    assert "line_trail" not in sig["manage"]
    rules = rules_from_alert(sig, tick_paise=5, resolution=2, timeframe=sig["timeframe"])
    assert rules.line_trail is False


def test_the_paper_book_is_given_the_rule_with_the_alert(cfg, db):
    book = Book()
    desk = warm_desk(cfg, db, paper=book)

    alert(desk, line_slot(desk))
    alert(desk, plain_slot(desk, "RELIANCE"), key="RELIANCE")

    with_line, without = book.alerts
    assert with_line["manage"]["line_trail"] is True
    assert "line_trail" not in without["manage"]


def test_the_rule_comes_back_with_the_alert_after_a_restart(cfg, db):
    desk = warm_desk(cfg, db)
    sig = alert(desk, line_slot(desk))

    again = cold_desk(cfg, db)  # a new process reads the pending alert from the database

    assert again.signals[sig["id"]]["manage"]["line_trail"] is True


# ── a taken position ─────────────────────────────────────────────────────────────────


def test_a_taken_positions_stop_moves_to_the_line_after_a_closed_bar(cfg, db):
    desk = warm_desk(cfg, db)
    slot = line_slot(desk)
    p = taken(desk, slot, opened=on_day(desk, 11, 5))
    first, level = p["stop_underlying"], between(p, desk)
    say(slot, on_day(desk, 11, 3), long=level)  # the 11:03 bar closes 11:09

    manage(desk, on_day(desk, 11, 10))

    assert first < level < price_of(desk)
    assert p["stop_underlying"] == level
    assert p["trail_active"] is True
    assert p["initial_stop_underlying"] == first, "the first stop stays the measure of risk"
    assert p["exit_advice"] is None and p["id"] in desk.positions
    assert stored_position(desk, p["id"])["stop_underlying"] == level


def test_the_stop_never_moves_back(cfg, db):
    desk = warm_desk(cfg, db)
    slot = line_slot(desk)
    p = taken(desk, slot, opened=on_day(desk, 11, 5))
    first, level = p["stop_underlying"], between(p, desk)
    say(slot, on_day(desk, 11, 3), long=level)
    manage(desk, on_day(desk, 11, 10))

    say(slot, on_day(desk, 11, 9), long=first + 0.01)  # the line stepped back a long way
    manage(desk, on_day(desk, 11, 16))

    assert p["stop_underlying"] == level and p["trail_active"] is True


def test_a_level_further_away_than_the_stop_is_not_taken(cfg, db):
    desk = warm_desk(cfg, db)
    slot = line_slot(desk)
    p = taken(desk, slot, opened=on_day(desk, 11, 5))
    first = p["stop_underlying"]
    say(slot, on_day(desk, 11, 3), long=round(first * 0.99, 2))

    manage(desk, on_day(desk, 11, 10))

    assert p["stop_underlying"] == first and p["trail_active"] is False


def test_a_short_positions_stop_comes_down_with_the_line(cfg, db):
    desk = warm_desk(cfg, db)
    slot = line_slot(desk)
    p = taken(desk, slot, "short", opened=on_day(desk, 11, 5))
    first, level = p["stop_underlying"], between(p, desk)
    say(slot, on_day(desk, 11, 3), short=level)

    manage(desk, on_day(desk, 11, 10))
    assert price_of(desk) < level < first
    assert p["stop_underlying"] == level and p["trail_active"] is True

    say(slot, on_day(desk, 11, 9), short=first)  # back up: not followed
    manage(desk, on_day(desk, 11, 16))
    assert p["stop_underlying"] == level
    assert p["exit_advice"] is None and p["id"] in desk.positions


def test_only_the_positions_own_side_of_the_line_is_followed(cfg, db):
    desk = warm_desk(cfg, db)
    slot = line_slot(desk)
    p = taken(desk, slot, opened=on_day(desk, 11, 5))
    first = p["stop_underlying"]
    say(slot, on_day(desk, 11, 3), short=between(p, desk))  # the line is on the other side

    manage(desk, on_day(desk, 11, 10))

    assert p["stop_underlying"] == first and p["trail_active"] is False


def test_a_level_from_a_bar_that_closed_before_the_position_opened_is_not_taken(cfg, db):
    """That level is the alert's first stop already; the stop moves with the first bar that
    closes after the position was opened."""
    desk = warm_desk(cfg, db)
    slot = line_slot(desk)
    p = taken(desk, slot, opened=on_day(desk, 11, 9, 30))  # the 11:03 bar closed at 11:09
    first, level = p["stop_underlying"], between(p, desk)

    say(slot, on_day(desk, 11, 3), long=level)
    manage(desk, on_day(desk, 11, 10))
    assert p["stop_underlying"] == first and p["trail_active"] is False

    say(slot, on_day(desk, 11, 9), long=level)  # closes 11:15, after the position opened
    manage(desk, on_day(desk, 11, 16))
    assert p["stop_underlying"] == level and p["trail_active"] is True


def test_the_stop_is_kept_to_the_paisa(cfg, db):
    desk = warm_desk(cfg, db)
    slot = line_slot(desk)
    p = taken(desk, slot, opened=on_day(desk, 11, 5))
    level = between(p, desk) + 0.004321
    say(slot, on_day(desk, 11, 3), long=level)

    manage(desk, on_day(desk, 11, 10))

    assert p["stop_underlying"] == round(level, 2) != level


def test_a_position_whose_alert_did_not_carry_the_rule_keeps_its_stop(cfg, db):
    """The alert's manage block is the record: a position raised without the line trail does
    not start following a line later (a reloaded configuration, say)."""
    desk = warm_desk(cfg, db)
    slot = plain_slot(desk)
    p = taken(desk, slot, opened=on_day(desk, 11, 5))
    first = p["stop_underlying"]
    assert "line_trail" not in p["manage"]
    say(slot, on_day(desk, 11, 3), long=between(p, desk))

    manage(desk, on_day(desk, 11, 14))

    assert p["stop_underlying"] == first and p["trail_active"] is False


@pytest.mark.parametrize("case", ["no closed bar yet", "the slot is gone", "the line is off"])
def test_without_a_level_to_follow_the_stop_stays(cfg, db, case):
    desk = warm_desk(cfg, db)
    slot = line_slot(desk)
    p = taken(desk, slot, opened=on_day(desk, 11, 5))
    first = p["stop_underlying"]
    say(slot, on_day(desk, 11, 3), long=between(p, desk))
    if case == "no closed bar yet":  # a slot built by a reload, before its first bar
        slot.last_bar_ts = None
    elif case == "the slot is gone":  # the configuration no longer runs this timeframe
        p["timeframe"] = "7m"
    else:
        slot.line_trail = False

    manage(desk, on_day(desk, 11, 10))

    assert p["stop_underlying"] == first and p["trail_active"] is False
    assert p["exit_advice"] is None and p["id"] in desk.positions


def test_a_price_through_the_trailed_stop_ends_the_position_as_a_trail_stop(cfg, db):
    desk = warm_desk(cfg, db)
    slot = line_slot(desk)
    p = taken(desk, slot, opened=on_day(desk, 11, 5))
    pid, level = p["id"], between(p, desk)
    say(slot, on_day(desk, 11, 3), long=level)
    manage(desk, on_day(desk, 11, 10))
    assert pid in desk.positions

    set_price(desk, level - 0.5)
    manage(desk, on_day(desk, 11, 11))

    # no paper book here, so the desk's own advice is the alert's one exit
    assert pid not in desk.positions
    assert closed_row(desk, pid)["exit_reason"] == "TRAIL_STOP"
    exits = [n for n in desk.notifier.sent if n.level == NotificationLevel.EXIT]
    assert len(exits) == 1 and "TRAIL STOP" in exits[0].title


def test_a_price_already_beyond_the_new_level_ends_it_in_the_same_pass(cfg, db):
    desk = warm_desk(cfg, db)
    slot = line_slot(desk)
    p = taken(desk, slot, opened=on_day(desk, 11, 5))
    pid, level = p["id"], between(p, desk)
    say(slot, on_day(desk, 11, 3), long=level)
    set_price(desk, level - 0.5)  # the price fell under the line's new level meanwhile

    manage(desk, on_day(desk, 11, 10))

    assert closed_row(desk, pid)["exit_reason"] == "TRAIL_STOP"


def test_a_price_through_a_stop_the_line_never_moved_is_a_plain_stop(cfg, db):
    desk = warm_desk(cfg, db)
    slot = line_slot(desk)
    p = taken(desk, slot, opened=on_day(desk, 11, 5))
    pid = p["id"]
    say(slot, on_day(desk, 11, 3), long=None)
    set_price(desk, p["stop_underlying"] - 0.5)

    manage(desk, on_day(desk, 11, 10))

    assert closed_row(desk, pid)["exit_reason"] == "STOP"


def test_the_trailed_stop_and_the_rule_survive_a_restart(cfg, db):
    desk = warm_desk(cfg, db)
    slot = line_slot(desk)
    p = taken(desk, slot, opened=on_day(desk, 11, 5))
    level = between(p, desk)
    say(slot, on_day(desk, 11, 3), long=level)
    manage(desk, on_day(desk, 11, 10))

    again = cold_desk(cfg, db)

    back = again.positions[p["id"]]
    assert back["stop_underlying"] == level and back["trail_active"] is True
    assert back["manage"]["line_trail"] is True


# ── the paper book's question ────────────────────────────────────────────────────────


def test_the_books_question_is_answered_with_the_level_and_when_its_bar_closed(cfg, db):
    desk = warm_desk(cfg, db)
    bar = on_day(desk, 11, 3)
    say(line_slot(desk), bar, long=987.65)

    answer = desk._paper_trail_level(Trade(), now_ist())

    assert answer == (987.65, bar + SIX)
    assert trail_level_of(answer) == TrailLevel(98_765, bar + SIX), "paise, once, in the book"


def test_the_real_line_is_what_the_book_is_told(cfg, db):
    desk = warm_desk(cfg, db)
    slot = line_slot(desk)
    view = slot.trail_view()
    (side,) = [s for s in ("long", "short") if view[s] is not None]
    other = "short" if side == "long" else "long"

    answer = desk._paper_trail_level(Trade(direction=side), now_ist())

    assert answer == (view[side], slot.last_bar_ts + SIX)
    assert trail_level_of(answer).level_paise == round(view[side] * 100)
    assert desk._paper_trail_level(Trade(direction=other), now_ist()) is None


@pytest.mark.parametrize(
    "trade",
    [
        Trade(series_key="INFY"),  # a series this desk does not track
        Trade(strategy="orb"),  # a strategy the instrument does not run
        Trade(timeframe="30m"),  # ... or not on that timeframe
        Trade(direction="short"),  # the line is not on that side
        Trade(strategy=PLAIN.name, timeframe=PLAIN.timeframe),  # a slot with no line
    ],
    ids=["series", "strategy", "timeframe", "side", "plain slot"],
)
def test_no_answer_for_a_trade_the_line_does_not_cover(cfg, db, trade):
    desk = warm_desk(cfg, db)
    say(line_slot(desk), on_day(desk, 11, 3), long=987.65)
    plain_slot(desk).line_trail, plain_slot(desk).last_trail = False, {}

    assert desk._paper_trail_level(trade, now_ist()) is None


def test_no_answer_before_the_slot_has_read_a_bar(cfg, db):
    desk = cold_desk(cfg, db)
    slot = line_slot(desk)
    slot.line_trail, slot.last_trail = True, {"long": 987.65, "short": None}

    assert slot.last_bar_ts is None
    assert desk._paper_trail_level(Trade(), now_ist()) is None


def test_the_book_is_handed_the_question_when_the_desk_starts(cfg, db):
    book = TrailBook()

    desk = cold_desk(cfg, db, paper=book)

    assert book.followed == [desk._paper_trail_level]
    assert desk.hook_errors == 0


def test_a_book_that_cannot_be_asked_is_left_alone(cfg, db):
    desk = cold_desk(cfg, db, paper=Book())

    assert desk.hook_errors == 0
    desk.poll()
    assert desk.hook_errors == 0


def test_a_book_that_refuses_the_question_does_not_stop_the_desk(cfg, db, caplog):
    class Refusing(TrailBook):
        def follow_trail(self, ask):
            raise RuntimeError("no trail today")

    with caplog.at_level(logging.ERROR):
        desk = cold_desk(cfg, db, paper=Refusing())

    assert desk.hook_errors == 1
    assert "follow_trail failed" in caplog.text
    desk.poll()  # and it runs
    assert desk.polls == 1


def test_after_a_reload_the_question_is_answered_from_the_new_slots(cfg, db, monkeypatch):
    book = TrailBook()
    desk = warm_desk(cfg, db, paper=book)
    (ask,) = book.followed
    bar = on_day(desk, 11, 3)
    say(line_slot(desk), bar, long=987.65)
    assert ask(Trade(), now_ist()) == (987.65, bar + SIX)

    monkeypatch.setattr("onelot.desk.desk.load_desk_config", lambda: desk.cfg)
    desk.reload()
    assert ask(Trade(), now_ist()) is None, "the new slot has not read a bar yet"

    desk.poll()
    view = line_slot(desk).trail_view()
    (side,) = [s for s in ("long", "short") if view[s] is not None]
    assert ask(Trade(direction=side), now_ist()) == (
        view[side],
        line_slot(desk).last_bar_ts + SIX,
    )
    assert book.followed == [ask], "handed over once: it reads whatever slots the desk has"


# ── the stored days for the ladder's daily timeframe ─────────────────────────────────

T0 = dt.datetime(2026, 11, 9, 9, 20, tzinfo=IST)
MIN = dt.timedelta(minutes=1)


def days(close: float = 1000.0, n: int = 3) -> pd.DataFrame:
    idx = pd.date_range("2026-11-02 09:15", periods=n, freq="B", tz="Asia/Kolkata", name="ts")
    return pd.DataFrame(
        {"open": close, "high": close + 5, "low": close - 5, "close": close, "volume": 1e5},
        index=idx,
    )


class Store:
    """Stands in for the stored days (`desk.history.daily_history`): what each series has,
    which reads fail, and every read made."""

    def __init__(self) -> None:
        self.days: dict[str, pd.DataFrame] = {}
        self.broken: set[str] = set()
        self.reads: list[tuple[str, dt.datetime]] = []

    def __call__(self, engine, series_key, now):
        self.reads.append((series_key, now))
        if series_key in self.broken:
            raise OSError(f"{series_key}: disk I/O error")
        return self.days.get(series_key, pd.DataFrame())

    def count(self, series_key: str) -> int:
        return sum(1 for key, _ in self.reads if key == series_key)


@pytest.fixture
def store(monkeypatch) -> Store:
    s = Store()
    monkeypatch.setattr("onelot.desk.desk.daily_history", s)
    return s


def test_each_tracker_reads_its_stored_days_once_a_day(cfg, db, store):
    desk = cold_desk(cfg, db)
    store.days = {"SBIN": days(950.0), "RELIANCE": days(1400.0)}

    desk._load_daily_history(T0)

    assert sorted(store.reads) == [("RELIANCE", T0), ("SBIN", T0)]
    for key, t in desk.trackers.items():
        assert t.daily_history is store.days[key]
        assert t.history_day == T0.date() and t.history_note is None

    desk._load_daily_history(T0 + 6 * 60 * MIN)  # the same day: not read again
    assert len(store.reads) == 2

    tomorrow = T0 + dt.timedelta(days=1)
    desk._load_daily_history(tomorrow)
    assert sorted(store.reads[2:]) == [("RELIANCE", tomorrow), ("SBIN", tomorrow)]
    assert desk.trackers["SBIN"].history_day == tomorrow.date()


def test_a_store_without_days_is_said_and_asked_again_an_hour_later(cfg, db, store):
    desk = cold_desk(cfg, db)
    sbin = desk.trackers["SBIN"]

    desk._load_daily_history(T0)

    assert sbin.daily_history is not None and sbin.daily_history.empty
    assert sbin.history_note == NO_DAYS
    assert sbin.history_day is None

    assert HISTORY_EMPTY_RETRY == dt.timedelta(hours=1)
    desk._load_daily_history(T0 + 59 * MIN)
    assert store.count("SBIN") == 1

    store.days["SBIN"] = days()  # a backfill ran meanwhile
    desk._load_daily_history(T0 + 60 * MIN)
    assert store.count("SBIN") == 2
    assert sbin.daily_history is store.days["SBIN"]
    assert sbin.history_note is None and sbin.history_day == T0.date()


def test_a_read_that_fails_is_logged_once_and_tried_again_ten_minutes_later(cfg, db, store, caplog):
    desk = cold_desk(cfg, db)
    sbin = desk.trackers["SBIN"]
    store.days = {"RELIANCE": days(1400.0)}
    store.broken = {"SBIN"}
    assert HISTORY_RETRY == dt.timedelta(minutes=10)

    with caplog.at_level(logging.ERROR):
        desk._load_daily_history(T0)
        desk._load_daily_history(T0 + 9 * MIN)  # too early
        desk._load_daily_history(T0 + 10 * MIN)  # asked again: it fails again

    assert store.count("SBIN") == 2
    assert sbin.daily_history is None and sbin.history_day is None
    assert sbin.history_note == UNREADABLE_DAYS
    said = [r.getMessage() for r in caplog.records if "could not be read" in r.getMessage()]
    assert said == ["desk: the stored daily history of SBIN could not be read"]
    # the other instrument was read once, whatever happened to this one
    assert store.count("RELIANCE") == 1
    assert desk.trackers["RELIANCE"].daily_history is store.days["RELIANCE"]

    store.broken, store.days["SBIN"] = set(), days()
    desk._load_daily_history(T0 + 20 * MIN)
    assert sbin.daily_history is store.days["SBIN"]
    assert sbin.history_note is None and sbin.history_day == T0.date()


def test_a_failed_read_keeps_the_days_already_held(cfg, db, store, caplog):
    desk = cold_desk(cfg, db)
    sbin = desk.trackers["SBIN"]
    store.days = {"SBIN": days(), "RELIANCE": days(1400.0)}
    desk._load_daily_history(T0)
    held = sbin.daily_history

    store.broken = {"SBIN"}
    tomorrow = T0 + dt.timedelta(days=1)
    with caplog.at_level(logging.ERROR):
        desk._load_daily_history(tomorrow)
        desk._load_daily_history(tomorrow + 10 * MIN)

    assert sbin.daily_history is held, "yesterday's reading is better than none"
    assert sbin.history_note is None
    assert sbin.history_day == T0.date(), "still due: it is asked again"
    assert store.count("SBIN") == 3
    assert len([r for r in caplog.records if "could not be read" in r.getMessage()]) == 1


def test_a_failure_after_a_recovery_is_logged_again(cfg, db, store, caplog):
    desk = cold_desk(cfg, db)
    store.broken = {"SBIN"}
    with caplog.at_level(logging.ERROR):
        desk._load_daily_history(T0)
        store.broken, store.days["SBIN"] = set(), days()
        desk._load_daily_history(T0 + 10 * MIN)  # recovered
        store.broken = {"SBIN"}
        desk._load_daily_history(T0 + dt.timedelta(days=1))  # a new failure, a new line

    assert len([r for r in caplog.records if "could not be read" in r.getMessage()]) == 2


def test_an_instrument_dropped_while_its_days_were_read_is_skipped(cfg, db, monkeypatch):
    """The store is read outside the desk's lock: a reload may drop an instrument meanwhile."""
    desk = cold_desk(cfg, db)
    frame = days()

    def read(engine, series_key, now):
        desk.trackers.pop("SBIN", None)
        return frame

    monkeypatch.setattr("onelot.desk.desk.daily_history", read)

    desk._load_daily_history(T0)

    assert "SBIN" not in desk._history_retry
    assert desk.trackers["RELIANCE"].daily_history is frame


def test_the_poll_reads_the_days_the_store_holds(cfg, db):
    """Through the real reader: daily bars in the store reach the tracker at the first poll,
    and an instrument without any says so."""
    last = pd.Timestamp.now(tz="Asia/Kolkata").normalize() - pd.Timedelta(days=1)
    idx = pd.bdate_range(end=last, periods=80) + pd.Timedelta(hours=9, minutes=15)
    close = 900.0 + pd.Series(range(80), index=idx, dtype=float)
    stored = pd.DataFrame(
        {"open": close, "high": close + 5, "low": close - 5, "close": close, "volume": 1e5}
    )
    assert save_bars(db.engine, "SBIN", "1d", stored, "dhan") == 80
    desk = cold_desk(cfg, db)

    desk.poll()

    sbin, reliance = desk.trackers["SBIN"], desk.trackers["RELIANCE"]
    assert sbin.history_note is None and sbin.history_day is not None
    assert list(sbin.daily_history["close"]) == list(close)
    assert reliance.daily_history.empty and reliance.history_note == NO_DAYS


# ── a reloaded configuration ─────────────────────────────────────────────────────────


def test_a_reload_keeps_the_bars_and_the_stored_days(cfg, db, store, monkeypatch):
    store.days = {"SBIN": days()}  # RELIANCE has none: asked again only in an hour
    desk = warm_desk(cfg, db)
    old = dict(desk.trackers)
    assert set(desk._history_retry) == {"RELIANCE"}
    monkeypatch.setattr("onelot.desk.desk.load_desk_config", lambda: desk.cfg)

    out = desk.reload()

    assert out["instruments"] == 2
    for key, was in old.items():
        t = desk.trackers[key]
        assert t is not was
        assert t.bars is was.bars and not t.bars.empty
        assert t.daily_history is was.daily_history
        assert t.history_note == was.history_note
        assert t.history_day is None, "read again at the next poll"
    assert desk.trackers["RELIANCE"].history_note == NO_DAYS
    assert desk._history_retry == {}

    before = len(store.reads)
    desk._load_daily_history(now_ist())  # a backfill run before the reload is picked up now
    assert sorted(key for key, _ in store.reads[before:]) == ["RELIANCE", "SBIN"]


def test_an_instrument_new_in_the_configuration_gets_its_days_at_the_next_poll(
    cfg, db, store, monkeypatch
):
    store.days = {"SBIN": days(), "RELIANCE": days(1400.0), "INFY": days(1500.0)}
    desk = warm_desk(cfg, db)
    more = desk.cfg.stocks + (DeskInstrumentCfg("INFY", "INFY", "STOCK", (LINE,)),)
    wider = dataclasses.replace(desk.cfg, stocks=more)
    monkeypatch.setattr("onelot.desk.desk.load_desk_config", lambda: wider)

    desk.reload()

    infy = desk.trackers["INFY"]
    assert infy.bars.empty and infy.daily_history is None and infy.history_day is None

    desk._load_daily_history(now_ist())
    assert infy.daily_history is store.days["INFY"] and infy.history_note is None
