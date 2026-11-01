"""US-002 mode B: real-feed-shaped frames and the delayed Yahoo 2-minute path.

* a frame whose LAST bar is still forming (the Dhan 1-minute feed returns it with partial
  OHLC): the book must never act on it, however the partial range looks, never write while it
  forms, and decide on its FINAL print once it has closed;
* frame shapes real feeds produce (float32 columns, extra columns, no volume, a UTC index, a
  re-sent last bar, unsorted rows) give the answer of the plain frame;
* Yahoo 2-minute frames (delayed grade, resolution 2m, bars at 09:15 + 2k minutes): the trade
  is managed on DATA time (`now` minus `paper.feed_delay_minutes`), events carry bar times,
  the session end is the first 2-minute open at or after exit_time, and the session-over
  fallback needs the DATA to reach 15:30.

Prices are paise; the worked trade is the usual long SBIN (entry 100030, stop 99000, target
102000, tick 5) unless a test says otherwise.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest
import us001_helpers as H
import us002_helpers as U

Q = (100000, 100100, 99900, 100000)
MIN = dt.timedelta(minutes=1)
TRAIL = {"trail_atr_mult": 1.5, "atr": 4.0}
BAR1 = (100000, 101200, 100100, 101100)


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


def frozen(engine):
    trades = [{k: v for k, v in r.items() if k != "updated_at"} for r in H.trade_rows(engine)]
    return {"trades": trades, "events": H.event_rows(engine)}


def keys_of(engine, alert_id="A1"):
    row = U.row_of(engine, alert_id)
    return {
        k: row[k]
        for k in ("status", "close_reason", "entry_paise", "exit_paise", "gross_paise", "exit_ts")
    }


class TestAFormingLastBar:
    def book(self, db, **kw):
        book = H.make_book(db.engine)
        H.open_trade(book, U.make_alert("A1", **kw))
        return book

    def poll(self, book, frame, now):
        return book.step(now, H.Feed(SBIN=frame))

    def test_a_partial_range_that_already_touches_the_stop_is_not_a_stop(self, db):
        book = self.book(db)
        forming = (100000, 100100, 98900, 99950)  # 10:08 so far: the low is below 990.00
        frame = U.story([Q, Q, forming])
        self.poll(book, frame, H.at(10, 8, 30))
        assert U.row_of(db.engine)["status"] == "OPEN"
        assert book.status()["hook_errors"] == 0

    def test_a_partial_range_through_the_target_is_not_a_target(self, db):
        book = self.book(db)
        frame = U.story([Q, Q, (100000, 102010, 99950, 102000)])
        self.poll(book, frame, H.at(10, 8, 59))
        assert U.row_of(db.engine)["status"] == "OPEN"

    def test_polls_inside_the_forming_bar_write_nothing_however_its_range_grows(self, db):
        book = self.book(db, manage=TRAIL)
        self.poll(book, U.story([BAR1, Q, (100000, 100100, 99900, 100000)]), H.at(10, 8, 5))
        before = frozen(db.engine)
        for second, low in ((20, 99800), (40, 99000), (58, 98000)):
            grown = U.story([BAR1, Q, (100000, 100100, low, 99950)])
            assert self.poll(book, grown, H.at(10, 8, second)) == []
        assert frozen(db.engine) == before

    def test_once_it_closes_the_final_print_decides_not_the_partial_one(self, db):
        book = self.book(db)
        self.poll(book, U.story([Q, Q, (100000, 100100, 98900, 99950)]), H.at(10, 8, 30))
        final = U.story([Q, Q, (100000, 100100, 99800, 100050)])  # the dip was revised away
        self.poll(book, final, H.at(10, 9, 2))
        row = U.row_of(db.engine)
        assert row["status"] == "OPEN"
        assert H.same_instant(row["last_bar_ts"], H.at(10, 8)), "the closed bar is now seen"

    def test_and_a_final_print_that_does_touch_closes_on_the_final_values(self, db):
        book = self.book(db)
        self.poll(book, U.story([Q, Q, (100000, 100100, 99800, 100050)]), H.at(10, 8, 30))
        final = U.story([Q, Q, (100000, 100100, 98900, 99500)])
        self.poll(book, final, H.at(10, 9, 2))
        row = U.row_of(db.engine)
        assert row["close_reason"] == "STOP" and H.same_instant(row["exit_ts"], H.at(10, 8))
        (stored,) = U.stored_close_events(db.engine)
        assert U.mentions(stored["detail"], 98900), "the event stores the FINAL low"

    def test_a_forming_bar_that_opened_through_the_stop_waits_for_its_close_then_gaps(self, db):
        book = self.book(db)
        frame = U.story([Q, Q, (98500, 98700, 98400, 98600)])
        self.poll(book, frame, H.at(10, 8, 20))
        assert U.row_of(db.engine)["status"] == "OPEN"
        events = self.poll(book, frame, H.at(10, 9))
        close = U.close_of(events)
        assert (close.reason, close.price, close.gap) == ("STOP", 98500, True)

    def test_the_session_end_bar_forming_is_not_yet_the_exit_bar(self, db):
        book = self.book(db, created=H.at(14, 57, 3))
        rows = [(100000 + 10 * i, 100100 + 10 * i, 99900, 100000 + 10 * i) for i in range(19)]
        frame = U.pbars(H.at(14, 57), [U.PRE_BAR, *rows])  # last bar 15:15, forming at 15:15:30
        self.poll(book, frame, H.at(15, 15, 30))
        assert U.row_of(db.engine)["status"] == "OPEN"
        self.poll(book, frame, H.at(15, 16))
        row = U.row_of(db.engine)
        assert row["close_reason"] == "SESSION_END" and H.same_instant(row["exit_ts"], H.at(15, 15))

    def test_an_option_series_with_a_forming_bar_and_no_volume_is_judged_the_same_way(self, db):
        pre = (5399000, 5399500, 5398500, 5399000)
        fill = (5400000, 5401000, 5399000, 5400500)
        book = H.make_book(db.engine)
        H.open_trade(book, U.make_alert("O1", kind="INDEX"))
        frame = U.pbars(H.at(10, 5), [pre, fill, (5400500, 5431000, 5384000, 5400000)])
        frame["volume"] = 0.0  # an index: no volume; and the last bar is forming
        book.step(H.at(10, 7, 40), H.Feed(BANKNIFTY=frame))
        assert U.row_of(db.engine, "O1")["status"] == "OPEN"
        assert book.status()["hook_errors"] == 0

    def test_a_forming_bar_with_nan_extremes_is_skipped_and_costs_nothing(self, db):
        book = self.book(db)
        frame = U.story([Q, Q, Q])
        frame.iloc[-1, frame.columns.get_loc("high")] = np.nan
        frame.iloc[-1, frame.columns.get_loc("low")] = np.nan
        self.poll(book, frame, H.at(10, 8, 30))
        assert U.row_of(db.engine)["status"] == "OPEN" and book.status()["hook_errors"] == 0


class TestFrameShapesOfRealFeeds:
    ROWS = [Q, BAR1, (101100, 101300, 100500, 101250)]

    def run(self, db, frame, **kw):
        book = H.make_book(db.engine)
        H.open_trade(book, U.make_alert("A1", manage=TRAIL, **kw))
        book.step(U.end_of(frame), H.Feed(SBIN=frame))
        assert book.status()["hook_errors"] == 0
        return keys_of(db.engine)

    def baseline(self, db):
        return self.run(db, U.story(self.ROWS))

    def test_the_baseline_is_a_trail_stop(self, db):
        keys = self.baseline(db)
        assert keys["close_reason"] == "TRAIL_STOP"
        assert keys["exit_paise"] == U.expected_stock_exit(100500, "long", "TRAIL_STOP")

    def test_float32_columns_on_the_quarter_rupee_grid_give_the_same_answer(self, db, db2):
        base = self.baseline(db)
        frame = U.story(self.ROWS)
        for col in ("open", "high", "low", "close", "volume"):
            frame[col] = frame[col].astype("float32")
        assert self.run(db2, frame) == base

    def test_extra_columns_and_a_missing_volume_column_change_nothing(self, db, db2):
        base = self.baseline(db)
        frame = U.story(self.ROWS).drop(columns="volume")
        frame["oi"] = 1234.0
        frame["vwap"] = 1000.0
        frame["atr"] = 99.0  # a column named like the ATR must never feed the trail
        assert self.run(db2, frame) == base

    def test_a_utc_indexed_frame_gives_the_same_answer(self, db, db2):
        base = self.baseline(db)
        assert self.run(db2, U.to_utc(U.story(self.ROWS))) == base

    def test_a_re_sent_last_bar_uses_its_latest_revision(self, db, db2):
        base = self.baseline(db)
        frame = U.story(self.ROWS)
        stale = frame.iloc[[-1]].copy()
        stale[["high", "low"]] = [1001.10, 1000.90]  # an earlier, quieter print of the same bar
        resent = pd.concat([frame.iloc[:-1], stale, frame.iloc[[-1]]])  # the revision comes last
        assert self.run(db2, resent) == base

    def test_unsorted_rows_give_the_same_answer(self, db, db2):
        base = self.baseline(db)
        frame = U.story(self.ROWS)
        assert self.run(db2, frame.iloc[[3, 1, 0, 2]]) == base

    def test_whole_rupee_integer_columns_are_read_exactly(self, db):
        stamps = [H.at(10, 5), H.at(10, 6), H.at(10, 7)]
        frame = pd.DataFrame(
            {
                "open": np.array([998, 1000, 1000], dtype="int64"),
                "high": np.array([999, 1002, 1002], dtype="int64"),
                "low": np.array([997, 989, 989], dtype="int64"),
                "close": np.array([998, 995, 995], dtype="int64"),
                "volume": np.array([1000, 1000, 1000], dtype="int64"),
            },
            index=pd.DatetimeIndex(stamps),
        )
        book = H.make_book(db.engine)
        H.open_trade(book, U.make_alert("A1"))
        book.step(U.end_of(frame), H.Feed(SBIN=frame))
        row = U.row_of(db.engine)
        assert row["close_reason"] == "STOP" and row["exit_paise"] == 98970
        assert H.typeof(db.engine, "exit_paise", "A1") == "integer"


# ── Yahoo: delayed grade, 2-minute bars on the NSE grid (09:15 + 2k) ───────────────────
def nse_2m(overrides=None, *, last=dt.time(15, 28), first=dt.time(9, 15), price=100000):
    """A day of 2-minute bars from 09:15 on its own grid; `overrides` maps 'HH:MM' -> a row."""
    overrides = overrides or {}
    start = H.at(first.hour, first.minute)
    stamps, rows = [], []
    t = start
    while t.time() <= last:
        stamps.append(t)
        rows.append(overrides.get(t.strftime("%H:%M"), (price, price + 100, price - 100, price)))
        t += dt.timedelta(minutes=2)
    return U.pbars_at(stamps, rows)


def yahoo_alert(alert_id="A1", **kw):
    manage = kw.pop("manage", None)
    return U.make_alert(alert_id, source="yahoo", resolution="2m", manage=manage, **kw)


class TestYahooTwoMinuteFrames:
    def book(self, db, alert=None, **kw):
        book = H.make_book(db.engine, **kw)
        H.open_trade(book, alert or yahoo_alert())
        return book

    def test_the_trade_is_graded_delayed_and_fills_at_the_first_two_minute_open_after_the_alert(
        self, db
    ):
        book = self.book(db)
        book.step(H.at(10, 25), H.Feed(SBIN=nse_2m()))
        row = U.row_of(db.engine)
        assert row["grade"] == "delayed" and row["resolution_min"] == 2
        assert H.same_instant(row["fill_bar_ts"], H.at(10, 7)) and row["entry_paise"] == 100030

    def test_a_stop_bar_is_booked_only_once_its_data_time_has_come(self, db):
        """The bar opening 10:13 (closing 10:15) dips through the stop. Data time = wall - 15 min,
        so it is reachable from wall 10:30, though the frame already holds it (and the whole
        day) long before."""
        book = self.book(db)
        frame = nse_2m({"10:13": (100000, 100100, 98900, 99500)})
        book.step(H.at(10, 29, 59), H.Feed(SBIN=frame))
        assert U.row_of(db.engine)["status"] == "OPEN"
        events = book.step(H.at(10, 30), H.Feed(SBIN=frame))
        row = U.row_of(db.engine)
        assert row["close_reason"] == "STOP" and H.same_instant(row["exit_ts"], H.at(10, 13))
        assert row["exit_paise"] == 98970 and row["gross_paise"] == 98970 - 100030
        assert U.close_of(events).at == H.at(10, 13) != H.at(10, 30)

    def test_a_target_bar_on_the_two_minute_grid_exits_at_exactly_the_target(self, db):
        book = self.book(db)
        frame = nse_2m({"10:11": (100500, 102010, 100400, 101800)})
        book.step(H.at(10, 45), H.Feed(SBIN=frame))
        row = U.row_of(db.engine)
        assert row["close_reason"] == "TARGET" and row["exit_paise"] == 102000
        assert H.same_instant(row["exit_ts"], H.at(10, 11))

    def test_a_trail_arms_on_a_two_minute_close_and_acts_from_the_next_bar(self, db):
        book = self.book(db, yahoo_alert(manage=TRAIL))
        frame = nse_2m({"10:07": BAR1, "10:09": (101100, 101300, 100500, 101250)})
        book.step(H.at(10, 40), H.Feed(SBIN=frame))
        row = U.row_of(db.engine)
        assert row["close_reason"] == "TRAIL_STOP" and H.same_instant(row["exit_ts"], H.at(10, 9))
        assert row["exit_paise"] == U.expected_stock_exit(100500, "long", "TRAIL_STOP")

    def test_the_time_stop_counts_strategy_bars_not_two_minute_bars(self, db):
        """tf_min 10, max_bars 2: twenty minutes from the 10:07 fill, so the 10:27 open exits."""
        book = self.book(db, yahoo_alert(manage={"max_bars": 2, "tf_min": 10}))
        frame = nse_2m({"10:27": (100300, 100400, 100200, 100350)})
        book.step(H.at(11, 5), H.Feed(SBIN=frame))
        row = U.row_of(db.engine)
        assert row["close_reason"] == "TIME_STOP" and H.same_instant(row["exit_ts"], H.at(10, 27))
        assert row["exit_paise"] == U.expected_stock_exit(100300, "long", "TIME_STOP")

    def test_the_session_end_is_the_first_two_minute_open_from_15_15(self, db):
        book = self.book(db)
        frame = nse_2m({"15:15": (100150, 100250, 100050, 100200)})
        book.step(H.at(15, 45), H.Feed(SBIN=frame))
        row = U.row_of(db.engine)
        assert row["close_reason"] == "SESSION_END" and H.same_instant(row["exit_ts"], H.at(15, 15))
        assert row["exit_paise"] == U.expected_stock_exit(100150, "long", "SESSION_END")

    def test_the_session_end_is_not_reachable_before_its_data_time(self, db):
        book = self.book(db)
        frame = nse_2m()
        book.step(H.at(15, 30), H.Feed(SBIN=frame))  # data time 15:15: the 15:15 bar is forming
        assert U.row_of(db.engine)["status"] == "OPEN"
        book.step(H.at(15, 32), H.Feed(SBIN=frame))  # data time 15:17: it has closed
        assert U.row_of(db.engine)["close_reason"] == "SESSION_END"

    def test_a_feed_that_stops_at_15_08_is_closed_only_when_the_data_reaches_the_session_end(
        self, db
    ):
        """The last bar opens 15:07 (closing 15:09). The session is over in DATA time at 15:30,
        that is wall 15:45; the price is the last close seen."""
        book = self.book(db)
        rows = {"15:07": (100000, 100100, 99900, 100123)}
        frame = nse_2m(rows, last=dt.time(15, 7))
        book.step(H.at(15, 44), H.Feed(SBIN=frame))
        assert U.row_of(db.engine)["status"] == "OPEN"
        events = book.step(H.at(15, 45), H.Feed(SBIN=frame))
        row = U.row_of(db.engine)
        assert row["close_reason"] == "SESSION_END"
        assert row["exit_paise"] == U.expected_stock_exit(100123, "long", "SESSION_END")
        assert U.close_of(events).at in (H.at(15, 7), H.at(15, 9))

    def test_a_first_step_on_the_next_morning_still_closes_the_session(self, db):
        book = self.book(db)
        frame = nse_2m({"15:15": (100150, 100250, 100050, 100200)})
        next_day = nse_2m({"09:15": (90000, 90100, 89900, 90000)})
        next_day.index = next_day.index + pd.Timedelta(days=1)
        both = pd.concat([frame, next_day])
        book.step(H.at(9, 40, day=H.FRIDAY), H.Feed(SBIN=both))
        row = U.row_of(db.engine)
        assert row["close_reason"] == "SESSION_END" and H.same_instant(row["exit_ts"], H.at(15, 15))
        assert row["exit_paise"] == U.expected_stock_exit(100150, "long", "SESSION_END")

    def test_a_fill_bar_opening_at_the_target_is_no_fill_on_the_two_minute_grid_too(self, db):
        book = self.book(db)
        frame = nse_2m({"10:07": (102000, 102100, 101900, 102050)})
        book.step(H.at(10, 25), H.Feed(SBIN=frame))
        row = U.row_of(db.engine)
        assert row["status"] == "NO_FILL" and row["entry_paise"] is None

    def test_a_yahoo_option_trade_is_managed_the_same_way(self, db):
        alert = U.make_alert("O1", kind="INDEX", source="yahoo", resolution="2m")
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        stamps = [H.at(10, 5) + dt.timedelta(minutes=2 * i) for i in range(12)]
        rows = [(5400000, 5400500, 5399500, 5400000)] * 12
        rows[3] = (5400500, 5431000, 5400000, 5430000)  # 10:11 trades through 54300
        book.step(H.at(10, 45), H.Feed(BANKNIFTY=U.pbars_at(stamps, rows)))
        row = U.row_of(db.engine, "O1")
        assert row["close_reason"] == "TARGET" and H.same_instant(row["exit_ts"], H.at(10, 11))
        assert row["exit_paise"] is not None and row["gross_paise"] > 0
        assert book.status()["hook_errors"] == 0

    def test_a_zero_feed_delay_makes_a_yahoo_trade_manage_on_wall_time(self, db):
        book = self.book(db, feed_delay=0)
        frame = nse_2m({"10:13": (100000, 100100, 98900, 99500)})
        book.step(H.at(10, 14, 59), H.Feed(SBIN=frame))
        assert U.row_of(db.engine)["status"] == "OPEN"
        book.step(H.at(10, 15), H.Feed(SBIN=frame))
        assert U.row_of(db.engine)["close_reason"] == "STOP"
