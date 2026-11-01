"""US-002 mode B: the strategy-exit state machine of the book, attacked.

The Desk answers `strategy_exit(trade, now)` from the strategy's LAST closed bar. That answer
changes under the book's feet: the callback may report an exit while its exit bar is still
forming and then say nothing (the strategy's next bar closed, or the flags were refreshed), a
trade may restart in between, a delayed (Yahoo) trade lives on DATA time (`now` minus
`paper.feed_delay_minutes`), and an answer may be stale or nonsense. Contract under test:

* an exit the callback reported once is REMEMBERED until its exit bar has closed, whatever the
  callback says later, and across a restart (live and delayed grade alike);
* the earliest reported exit wins;
* an exit whose strategy bar closed at or before the fill bar's open is stale: ignored;
* nothing closes before the data has reached the exit bar (no look-ahead), on a live trade
  against the wall clock and on a delayed trade against its data time;
* an answer that raises or makes no sense is counted in `hook_errors` and never stops the trade
  being managed; the desk's plain `(reason, bar_close_ts)` tuple is accepted.

Worked frame (paise, 1-minute bars, fill 10:06, entry 100030): 10:06 .. 10:09 quiet, the exit bar
10:10 opens 100200 (slipped long exit 100165), 10:11 opens 100300.
"""

from __future__ import annotations

import copy
import datetime as dt

import pytest
import us001_helpers as H
import us002_helpers as U

Q = (100000, 100100, 99900, 100000)
EXIT_BAR = (100200, 100300, 100100, 100250)
ROWS = [Q, Q, Q, Q, EXIT_BAR, (100300, 100400, 100200, 100350)]
MIN = dt.timedelta(minutes=1)
SLIPPED_OPEN = 100165  # 1002.00 sold: 1002.00 x 0.9997 = 1001.70 -> 1001.65 on the tick
TRAIL = {"trail_atr_mult": 1.5, "atr": 4.0}
BAR1 = (100000, 101200, 100100, 101100)  # closes +1.04 R: the trail arms at 100500


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


@pytest.fixture
def db2(tmp_path):
    d = H.new_db(tmp_path, "second.db")
    yield d
    d.dispose()


def reporting(reason="SIGNAL_EXIT", at=None):
    when = at or H.at(10, 10)
    return lambda trade, now: U.strategy_exit(reason, when)


def silent(trade, now):
    return None


def opened_book(db, alert=None, **book_kw):
    book = H.make_book(db.engine, **book_kw)
    H.open_trade(book, alert or U.make_alert("A1"))
    return book


def step(book, frame, now, callback=None):
    return book.step(now, H.Feed(SBIN=frame), callback)


class TestAPendingExitSurvivesTheCallbackStopping:
    def test_an_exit_reported_while_its_bar_forms_closes_at_its_open_though_the_callback_goes_quiet(
        self, db
    ):
        book = opened_book(db)
        frame = U.story(ROWS)
        step(book, frame, H.at(10, 10, 30), reporting())  # exit bar 10:10 is still forming
        assert U.row_of(db.engine)["status"] == "OPEN"
        events = step(book, frame, H.at(10, 12), silent)  # the strategy no longer says so
        row = U.row_of(db.engine)
        assert row["close_reason"] == "SIGNAL_EXIT" and H.same_instant(row["exit_ts"], H.at(10, 10))
        assert row["exit_paise"] == SLIPPED_OPEN and U.close_of(events).price == 100200
        assert book.status()["hook_errors"] == 0

    def test_the_same_across_a_restart_between_the_two_steps(self, db):
        frame = U.story(ROWS)
        first = opened_book(db)
        step(first, frame, H.at(10, 10, 30), reporting("OPPOSITE_SIGNAL"))
        del first
        reborn = H.make_book(db.engine)
        step(reborn, frame, H.at(10, 12), silent)
        row = U.row_of(db.engine)
        assert row["close_reason"] == "OPPOSITE_SIGNAL" and row["exit_paise"] == SLIPPED_OPEN

    def test_the_earliest_reported_exit_wins_over_a_later_one(self, db):
        book = opened_book(db)
        frame = U.story(ROWS)
        step(book, frame, H.at(10, 8, 30), reporting("OPPOSITE_SIGNAL", H.at(10, 8)))
        step(book, frame, H.at(10, 9, 30), reporting("SIGNAL_EXIT", H.at(10, 10)))
        step(book, frame, H.at(10, 12), silent)
        row = U.row_of(db.engine)
        assert row["close_reason"] == "OPPOSITE_SIGNAL"
        assert H.same_instant(row["exit_ts"], H.at(10, 8))

    def test_a_stop_before_the_exit_bar_still_closes_stop_and_the_pending_exit_dies_with_it(
        self, db
    ):
        book = opened_book(db)
        rows = [Q, Q, Q, (99800, 99900, 98900, 99000), EXIT_BAR]
        frame = U.story(rows)
        step(book, frame, H.at(10, 9, 30), reporting())  # pending: its bar 10:10 is ahead
        step(book, frame, H.at(10, 12), reporting())
        row = U.row_of(db.engine)
        assert row["close_reason"] == "STOP" and H.same_instant(row["exit_ts"], H.at(10, 9))
        assert len(U.stored_close_events(db.engine)) == 1

    def test_a_trade_closed_by_the_exit_is_never_closed_again(self, db):
        book = opened_book(db)
        frame = U.story(ROWS)
        step(book, frame, H.at(10, 12), reporting())
        before = (H.trade_rows(db.engine), H.event_rows(db.engine))
        later = U.story(ROWS + [(90000, 90100, 89900, 90000)])
        assert step(book, later, H.at(10, 30), reporting("OPPOSITE_SIGNAL", H.at(10, 12))) == []
        assert (H.trade_rows(db.engine), H.event_rows(db.engine)) == before


class TestAnExitOlderThanTheFill:
    @pytest.mark.parametrize(
        "at", [H.at(10, 5), H.at(10, 6)], ids=["before-the-fill", "at-the-fill-open"]
    )
    def test_a_signal_bar_that_closed_at_or_before_the_fill_bar_opened_is_ignored(self, db, at):
        """The strategy bar B closed before the trade existed: the desk still reports it (its
        flags are those of the last closed bar), the book must not act on it."""
        book = opened_book(db)
        frame = U.story(ROWS)
        step(book, frame, U.end_of(frame), reporting("SIGNAL_EXIT", at))
        row = U.row_of(db.engine)
        assert row["status"] == "OPEN" and row["close_reason"] is None
        assert book.status()["hook_errors"] == 0

    def test_a_stale_exit_is_not_remembered_either(self, db):
        book = opened_book(db)
        frame = U.story(ROWS)
        step(book, frame, H.at(10, 8), reporting("SIGNAL_EXIT", H.at(10, 5)))
        step(book, frame, H.at(10, 12), silent)
        assert U.row_of(db.engine)["status"] == "OPEN"

    def test_an_exit_one_second_after_the_fill_open_is_live_and_closes_at_the_next_open(self, db):
        book = opened_book(db)
        frame = U.story(ROWS)
        step(book, frame, U.end_of(frame), reporting("SIGNAL_EXIT", H.at(10, 6, 1)))
        row = U.row_of(db.engine)
        assert row["close_reason"] == "SIGNAL_EXIT" and H.same_instant(row["exit_ts"], H.at(10, 7))

    def test_a_day_old_report_never_closes_a_todays_trade(self, db):
        book = opened_book(db)
        frame = U.story(ROWS)
        yesterday = H.at(15, 30, day=dt.date(2026, 10, 28))
        step(book, frame, U.end_of(frame), reporting("OPPOSITE_SIGNAL", yesterday))
        assert U.row_of(db.engine)["status"] == "OPEN"


class TestNoLookAheadOnTheDataTime:
    def test_an_exit_whose_bar_closes_after_the_wall_clock_waits_for_it(self, db):
        """A live trade: the callback reports a strategy bar that closes at 10:30 while the step
        runs at 10:12. Nothing closes early; once 10:30 is past, the first open after it does."""
        book = opened_book(db)
        rows = [Q] * 40
        frame = U.story(rows)
        step(book, frame, H.at(10, 12), reporting("SIGNAL_EXIT", H.at(10, 30)))
        assert U.row_of(db.engine)["status"] == "OPEN"
        step(book, frame, H.at(10, 29), reporting("SIGNAL_EXIT", H.at(10, 30)))
        assert U.row_of(db.engine)["status"] == "OPEN"
        step(book, frame, H.at(10, 32), reporting("SIGNAL_EXIT", H.at(10, 30)))
        row = U.row_of(db.engine)
        assert row["close_reason"] == "SIGNAL_EXIT" and H.same_instant(row["exit_ts"], H.at(10, 30))
        assert book.status()["hook_errors"] == 0

    def test_a_delayed_trade_is_managed_on_data_time_not_wall_time(self, db):
        """Yahoo grade: data time is `now` minus 15 minutes. The exit bar 10:10 (closing 10:11
        in data time) is not reachable at wall 10:20 (data 10:05), and is at wall 10:27."""
        book = opened_book(db, U.make_alert("A1", source="yahoo"))
        frame = U.story(ROWS)
        step(book, frame, H.at(10, 20), reporting())
        assert U.row_of(db.engine)["status"] == "OPEN"
        step(book, frame, H.at(10, 25), reporting())  # data time 10:10: the exit bar is forming
        assert U.row_of(db.engine)["status"] == "OPEN"
        step(book, frame, H.at(10, 27), reporting())  # data time 10:12: it has closed
        row = U.row_of(db.engine)
        assert row["close_reason"] == "SIGNAL_EXIT" and H.same_instant(row["exit_ts"], H.at(10, 10))
        assert row["exit_paise"] == SLIPPED_OPEN and row["grade"] == "delayed"
        assert book.status()["hook_errors"] == 0

    def test_an_exit_ahead_of_data_time_is_not_lost_when_the_callback_stops_reporting(self, db):
        """BUG-1. The Desk reports the strategy's exit on its last closed bar; on a delayed feed
        the feed may be fresher than `feed_delay_minutes`, so the exit can be ahead of data time
        at the step that sees it. The live path remembers such an exit (it is `pending_exit`); the
        delayed path drops it, and when the strategy's next bar clears the flags the exit is
        gone for good: the trade runs on to its stop, target or session end instead."""
        book = opened_book(db, U.make_alert("A1", source="yahoo"))
        frame = U.story(ROWS)
        step(book, frame, H.at(10, 20), reporting())  # ahead of data time (10:05)
        step(book, frame, H.at(10, 40), silent)  # data time 10:25: long past the exit bar
        row = U.row_of(db.engine)
        assert row["close_reason"] == "SIGNAL_EXIT", row["close_reason"]
        assert H.same_instant(row["exit_ts"], H.at(10, 10))

    def test_a_shorter_feed_delay_moves_the_data_horizon_with_it(self, db):
        book = opened_book(db, U.make_alert("A1", source="yahoo"), feed_delay=5)
        frame = U.story([Q, (99800, 99900, 98900, 99000), Q, Q])
        step(book, frame, H.at(10, 12, 59))  # data time 10:07:59: bar 10:07 is still forming
        assert U.row_of(db.engine)["status"] == "OPEN"
        step(book, frame, H.at(10, 13))  # data time 10:08: it has closed
        assert U.row_of(db.engine)["close_reason"] == "STOP"

    def test_delayed_and_live_trades_book_the_same_events_when_wall_time_is_shifted_by_the_delay(
        self, db, db2
    ):
        frame = U.story(ROWS + [Q] * 3)
        live = opened_book(db, U.make_alert("A1", manage=TRAIL))
        delayed = opened_book(db2, U.make_alert("A1", manage=TRAIL, source="yahoo"))
        end = U.end_of(frame)
        a = step(live, frame, end, reporting())
        b = step(delayed, frame, end + 15 * MIN, reporting())
        assert U.canon(a, with_trade=True)[1:] == U.canon(b, with_trade=True)[1:]
        keys = ("close_reason", "exit_paise", "gross_paise", "exit_ts", "entry_paise")
        row_a, row_b = U.row_of(db.engine), U.row_of(db2.engine)
        assert {k: row_a[k] for k in keys} == {k: row_b[k] for k in keys}


class TestRestartMidTradeWithATrailedStopAndAPendingExit:
    def pending_then_restart(self, db, rows_after):
        """Bar 1 arms the trail (stop 100500); an exit is reported for the bar closing 10:09
        while that bar is forming; the process restarts; the feed then shows `rows_after`."""
        alert = U.make_alert("A1", manage=TRAIL)
        first = opened_book(db, alert)
        rows = [BAR1, (101100, 101300, 100800, 101150), (101150, 101400, 100900, 101300)]
        early = U.story(rows)
        step(first, early, H.at(10, 9, 30), reporting("SIGNAL_EXIT", H.at(10, 9)))  # 10:09 forming
        assert U.row_of(db.engine)["status"] == "OPEN"
        del first
        reborn = H.make_book(db.engine)
        late = U.story(rows + rows_after)
        events = step(reborn, late, U.end_of(late), silent)
        return reborn, events

    def test_the_pending_exit_closes_at_its_open_before_a_dip_inside_that_bar(self, db):
        reborn, events = self.pending_then_restart(db, [(101300, 101350, 100400, 100900)])
        row = U.row_of(db.engine)
        assert row["close_reason"] == "SIGNAL_EXIT" and H.same_instant(row["exit_ts"], H.at(10, 9))
        assert row["exit_paise"] == U.expected_stock_exit(101300, "long", "SIGNAL_EXIT")
        assert reborn.status()["hook_errors"] == 0

    def test_a_gap_through_the_trailed_stop_at_the_due_open_is_trail_stop_at_the_open(self, db):
        """The stop trailed to 100700 (bar 3 closed 101300); the exit bar opens at 100200, through
        it: same moment as the exit, and TRAIL_STOP outranks SIGNAL_EXIT. The trailed stop must
        have survived the restart, or this would close SIGNAL_EXIT (or nothing)."""
        reborn, _ = self.pending_then_restart(db, [(100200, 100300, 100100, 100250)])
        row = U.row_of(db.engine)
        assert row["close_reason"] == "TRAIL_STOP" and H.same_instant(row["exit_ts"], H.at(10, 9))
        assert row["exit_paise"] == U.expected_stock_exit(100200, "long", "TRAIL_STOP")

    def test_the_whole_story_equals_the_same_run_without_a_restart(self, db, db2):
        rows = [BAR1, (101100, 101300, 100800, 101150), (101150, 101400, 100900, 101300)]
        tail = [(101300, 101350, 100400, 100900), Q]
        frame = U.story(rows + tail)
        steady = opened_book(db2, U.make_alert("A1", manage=TRAIL))
        expected = step(steady, frame, U.end_of(frame), reporting("SIGNAL_EXIT", H.at(10, 9)))
        self.pending_then_restart(db, tail)
        keys = ("status", "close_reason", "exit_paise", "gross_paise", "exit_ts")
        row_a, row_b = U.row_of(db.engine), U.row_of(db2.engine)
        assert {k: row_a[k] for k in keys} == {k: row_b[k] for k in keys}
        assert U.close_of(expected).reason == "SIGNAL_EXIT"

    def test_two_restarts_in_a_row_keep_the_state(self, db):
        alert = U.make_alert("A1", manage=TRAIL)
        rows = [BAR1, (101100, 101300, 100800, 101150)]
        book = opened_book(db, alert)
        step(book, U.story(rows[:1]), H.at(10, 7))
        book = H.make_book(db.engine)
        step(book, U.story(rows), H.at(10, 8))
        book = H.make_book(db.engine)
        final = U.story([*rows, (101150, 101200, 100550, 100700)])  # touches the 100550 stop
        step(book, final, U.end_of(final))
        row = U.row_of(db.engine)
        assert row["close_reason"] == "TRAIL_STOP"
        assert row["exit_paise"] == U.expected_stock_exit(100550, "long", "TRAIL_STOP")


class TestWhatTheCallbackMayAnswer:
    def stopped_frame(self):
        return U.story([Q, (100000, 100100, 99000, 99500)])

    @pytest.mark.parametrize(
        "answer",
        [
            ("SQUARE_OFF", H.at(10, 7)),
            ("SIGNAL_EXIT", dt.datetime(2026, 10, 29, 10, 7)),
            "SIGNAL_EXIT",
            42,
            ("SIGNAL_EXIT",),
            ("STOP", H.at(10, 7)),
        ],
        ids=[
            "order-verb-reason",
            "naive-time",
            "bare-string",
            "number",
            "short-tuple",
            "stop-reason",
        ],
    )
    def test_nonsense_is_counted_never_raised_and_the_trade_is_still_managed(self, db, answer):
        book = opened_book(db)
        frame = self.stopped_frame()
        book.step(U.end_of(frame), H.Feed(SBIN=frame), lambda trade, now: answer)  # must not raise
        row = U.row_of(db.engine)
        assert row["close_reason"] == "STOP", "the stop is still honoured"
        assert book.status()["hook_errors"] >= 1

    def test_the_desks_plain_tuple_is_a_valid_answer(self, db):
        book = opened_book(db)
        frame = U.story(ROWS)
        answer = ("SIGNAL_EXIT", H.at(10, 10))
        book.step(U.end_of(frame), H.Feed(SBIN=frame), lambda trade, now: answer)
        row = U.row_of(db.engine)
        assert row["close_reason"] == "SIGNAL_EXIT" and row["exit_paise"] == SLIPPED_OPEN
        assert book.status()["hook_errors"] == 0

    def test_a_callback_that_always_raises_never_changes_what_the_rules_do(self, db, db2):
        frame = U.story([Q, BAR1, (101100, 101300, 100500, 101250)])

        def boom(trade, now):
            raise RuntimeError("strategy exploded")

        with_boom = opened_book(db, U.make_alert("A1", manage=TRAIL))
        without = opened_book(db2, U.make_alert("A1", manage=TRAIL))
        with_boom.step(U.end_of(frame), H.Feed(SBIN=frame), boom)
        without.step(U.end_of(frame), H.Feed(SBIN=frame))
        keys = ("status", "close_reason", "exit_paise", "gross_paise", "exit_ts")
        a, b = U.row_of(db.engine), U.row_of(db2.engine)
        assert {k: a[k] for k in keys} == {k: b[k] for k in keys}
        assert with_boom.status()["hook_errors"] >= 1 and without.status()["hook_errors"] == 0

    def test_the_callback_receives_a_snapshot_it_cannot_use_to_change_the_book(self, db):
        book = opened_book(db)
        frame = U.story(ROWS)

        def meddle(trade, now):
            try:
                trade.plan["alert"]["economics"]["underlying_stop"] = 1.0  # a fresh copy each time
            except Exception:  # noqa: BLE001
                pass
            return None

        step(book, frame, H.at(10, 8), meddle)
        step(book, frame, H.at(10, 9), meddle)
        assert U.row_of(db.engine)["plan"]["alert"]["economics"]["underlying_stop"] == 990.0
        assert copy.deepcopy(U.row_of(db.engine)["status"]) == "OPEN"
