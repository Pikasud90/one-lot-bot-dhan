"""US-002 mode B: properties of the whole book (hypothesis) and multi-instance regressions.

Every property runs the REAL PaperBook on a throw-away SQLite file per example, over the same
tick-grid integer paths as the AC7 truncation tests (gaps, wicks, either direction, trail on or
off, with or without a target, a time stop, a strategy exit, a session that runs into 15:15):

  P1  differential: the book's close (reason, time, exit price, gross) equals `manage_step` on
      the same bars with the rules read from the alert BY THE TEST, priced by the exact stock-exit
      oracle; and nothing is swallowed (`hook_errors` stays 0)
  P2  restart: a NEW book on the same database at every chunk boundary books exactly the events
      of one book that never stopped (trailed stop, pending exit and cursor all survive)
  P3  data time: a delayed (Yahoo) trade stepped 15 minutes of wall time later books exactly what
      a live trade books (same bars, same exits)
  P4  idempotence: stepping again with the same now and bars writes nothing

and the regression for two PaperBook instances sharing one database (see BUG-2 below).
"""

from __future__ import annotations

import copy
import datetime as dt
from collections import Counter

import pytest
import us001_helpers as H
import us002_helpers as U
import us023_helpers as W
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from test_us002_ac7_truncation import cases
from test_us002_book_ac6_ac7_ac8 import alert_for

MIN = dt.timedelta(minutes=1)
PROP = settings(
    max_examples=60,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=list(HealthCheck),
)
KEYS = ("status", "close_reason", "entry_paise", "exit_paise", "gross_paise", "exit_ts")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


def frame_of(case):
    return U.pbars(case.start - MIN, [U.PRE_BAR, *case.rows])


def callback(case):
    return lambda trade, now: case.strategy_exit(now)


def row_keys(engine):
    row = U.row_of(engine, "A1")
    return {k: row[k] for k in KEYS}


def strip(rows):
    return [{k: v for k, v in r.items() if k != "updated_at"} for r in rows]


def oracle_rules(alert):
    """ManageRules read out of the alert's manage block here, not by the book."""
    m = alert["manage"]
    hh, mm = (int(x) for x in m["exit_time"].split(":"))
    return U.manage().ManageRules(
        target_r=m["target_r"],
        trail_atr_mult=m["trail_atr_mult"],
        trail_activate_r=m["trail_activate_r"],
        atr=m["atr"],
        max_bars=m["max_bars"],
        tf_min=m["tf_min"],
        exit_time=dt.time(hh, mm),
        tick_paise=5,
    )


class TestTheBookIsManageStepPlusPricing:
    @PROP
    @given(case=cases())
    def test_P1_the_books_close_is_manage_steps_close_priced_by_the_oracle(self, case):
        alert = alert_for(case)
        frame = frame_of(case)
        with H.temp_book() as (book, engine):
            H.open_trade(book, copy.deepcopy(alert))
            events = book.step(U.end_of(frame), H.Feed(SBIN=frame), callback(case))
            row = U.row_of(engine, "A1")
            assert book.status()["hook_errors"] == 0, "the book swallowed an exception"
            assert row["status"] in ("OPEN", "CLOSED"), row["status"]
            eco = alert["economics"]
            target = eco["underlying_target"]
            state = U.mk_state(
                case.direction,
                entry=row["entry_paise"],
                stop=H.to_paise(eco["underlying_stop"]),
                target=None if target is None else H.to_paise(target),
                opened_at=row["fill_bar_ts"],
            )
            _, expected = U.manage().manage_step(
                state, frame, oracle_rules(alert), U.end_of(frame), case.strategy_exit()
            )
            want = U.close_of(expected)
            got = U.close_of(events)
            if want is None:
                assert got is None and row["status"] == "OPEN"
                return
            assert (got.reason, got.price, got.at) == (want.reason, want.price, want.at)
            level = U.expected_stock_exit(want.price, case.direction, want.reason)
            assert row["status"] == "CLOSED" and row["close_reason"] == want.reason
            assert row["exit_paise"] == level and H.same_instant(row["exit_ts"], want.at)
            assert row["gross_paise"] == U.expected_gross(row["entry_paise"], level, case.direction)
            costs = W.expected_costs(  # US-023 books the two legs' charges with the close
                W.model_for("SBIN"), "STOCK", case.direction, row["entry_paise"], level, 1, True
            )
            assert (row["costs_paise"], row["net_paise"]) == (costs, row["gross_paise"] - costs)
            moved = [e for e in events if str(e.kind) == "STOP_MOVED"]
            assert U.canon(moved) == U.canon(e for e in expected if str(e.kind) == "STOP_MOVED")

    @PROP
    @given(case=cases())
    def test_P4_stepping_again_with_the_same_now_and_bars_writes_nothing(self, case):
        frame = frame_of(case)
        with H.temp_book() as (book, engine):
            H.open_trade(book, copy.deepcopy(alert_for(case)))
            book.step(U.end_of(frame), H.Feed(SBIN=frame), callback(case))
            before = (strip(H.trade_rows(engine)), H.event_rows(engine))
            assert book.step(U.end_of(frame), H.Feed(SBIN=frame), callback(case)) == []
            assert (strip(H.trade_rows(engine)), H.event_rows(engine)) == before
            assert book.status()["hook_errors"] == 0


class TestRestartAtEveryChunkBoundary:
    @PROP
    @given(case=cases(), sizes=st.lists(st.integers(1, 7), min_size=1, max_size=5))
    def test_P2_a_new_book_each_time_books_what_one_book_that_never_stopped_books(
        self, case, sizes
    ):
        alert = alert_for(case)
        frame = frame_of(case)
        end = U.end_of(frame)
        with H.temp_book() as (steady, steady_engine):
            H.open_trade(steady, copy.deepcopy(alert))
            expected = steady.step(end, H.Feed(SBIN=frame), callback(case))
            want = row_keys(steady_engine)
        n = len(case.rows)
        with H.temp_book() as (first, engine):
            H.open_trade(first, copy.deepcopy(alert))
            booked, done, book, swallowed = [], 0, first, 0
            for size in (*sizes, n):
                done = min(done + size, n)
                now = case.start + done * MIN  # the close of the done-th bar after the fill bar
                booked += book.step(now, H.Feed(SBIN=frame), callback(case))
                swallowed += book.status()["hook_errors"]
                book = H.make_book(engine)  # the process restarted
            assert U.canon(booked, with_trade=True) == U.canon(expected, with_trade=True)
            assert row_keys(engine) == want
            assert swallowed == 0


class TestDataTime:
    @PROP
    @given(case=cases())
    def test_P3_a_delayed_trade_stepped_the_feed_delay_later_books_what_a_live_trade_books(
        self, case
    ):
        frame = frame_of(case)
        end = U.end_of(frame)
        live_alert = alert_for(case)
        delayed_alert = copy.deepcopy(live_alert)
        delayed_alert["data"].update(source="yahoo", delayed=True)
        with H.temp_book() as (live, live_engine):
            H.open_trade(live, copy.deepcopy(live_alert))
            a = live.step(end, H.Feed(SBIN=frame), callback(case))
            live_keys = row_keys(live_engine)
        with H.temp_book() as (delayed, delayed_engine):
            H.open_trade(delayed, copy.deepcopy(delayed_alert))
            b = delayed.step(end + 15 * MIN, H.Feed(SBIN=frame), callback(case))
            delayed_keys = row_keys(delayed_engine)
        management = lambda evs: [e for e in evs if str(e.kind) in ("STOP_MOVED", "CLOSED")]  # noqa: E731
        assert U.canon(management(a), with_trade=True) == U.canon(management(b), with_trade=True)
        assert live_keys == delayed_keys


class TestTwoBooksOnOneDatabase:
    """BUG-2. `_commit` guards an update with the trade's status and grade, which is enough to
    book a CLOSE once (the status changes) but not a STOP_MOVED: the trade stays OPEN, so a
    second PaperBook whose in-memory copy is stale walks the same bars again and books the same
    STOP_MOVED events again."""

    ROWS = [
        (100000, 101200, 100100, 101100),
        (101100, 101300, 100800, 101150),
        (101150, 101400, 100900, 101300),
    ]

    def test_a_book_with_a_stale_view_never_re_books_a_management_event(self, tmp_path):
        db = H.new_db(tmp_path)
        try:
            alert = U.make_alert("A1", manage={"trail_atr_mult": 1.5, "atr": 4.0})
            frame = U.story(self.ROWS)
            first = H.make_book(db.engine)
            H.open_trade(first, alert)
            first.step(H.at(10, 7), H.Feed(SBIN=frame))  # fills and manages bar 1
            stale = H.make_book(db.engine)  # loads the OPEN trade as it is NOW
            first.step(U.end_of(frame), H.Feed(SBIN=frame))  # the first book moves on
            stale.step(U.end_of(frame), H.Feed(SBIN=frame))  # the stale one walks the same bars
            stored = [(str(H.val(e["kind"])), e["ts"]) for e in H.event_rows(db.engine, "A1")]
            duplicates = {k: n for k, n in Counter(stored).items() if n > 1}
            assert not duplicates, f"events booked twice: {duplicates}"
            assert Counter(k for k, _ in stored)["STOP_MOVED"] == 3
        finally:
            db.dispose()

    def test_a_stale_book_never_moves_the_cursor_back(self, tmp_path):
        db = H.new_db(tmp_path)
        try:
            alert = U.make_alert("A1", manage={"trail_atr_mult": 1.5, "atr": 4.0})
            full = U.story(self.ROWS)
            first = H.make_book(db.engine)
            H.open_trade(first, alert)
            first.step(H.at(10, 7), H.Feed(SBIN=full))
            stale = H.make_book(db.engine)
            first.step(U.end_of(full), H.Feed(SBIN=full))
            ahead = U.row_of(db.engine, "A1")["last_bar_ts"]
            stale.step(H.at(10, 8), H.Feed(SBIN=full))  # an OLDER data horizon than the DB holds
            assert U.row_of(db.engine, "A1")["last_bar_ts"] >= ahead
        finally:
            db.dispose()

    def test_the_close_is_still_booked_exactly_once_by_whichever_book_gets_there_first(
        self, tmp_path
    ):
        db = H.new_db(tmp_path)
        try:
            alert = U.make_alert("A1")
            frame = U.story([(100000, 100100, 99000, 99500)])
            a, b = H.make_book(db.engine), H.make_book(db.engine)
            H.open_trade(a, alert)
            U.step_book(a, frame)
            U.step_book(b, frame)
            assert len(U.stored_close_events(db.engine)) == 1
        finally:
            db.dispose()
