"""US-001 AC3: when the trade cannot fill honestly it closes NO_FILL, and no price is invented.

* the fill bar opens beyond the stop                          -> NO_FILL, reason GAP_THROUGH_STOP
* no eligible bar, and the DATA has advanced to a bar opening
  after the deadline (expires_at, + the feed delay if delayed)  -> NO_FILL, reason NO_FILL ("expired")
* the session has closed (never a fill on a later day: an alert at 15:29 on a Friday is
  NO_FILL, never a Monday fill)                               -> NO_FILL, reason NO_FILL ("session_closed")
* NO_FILL rows have NULL entry and exit, zero costs and zero net, and are excluded from the
  filled / closed counts;
* while waiting, nothing is invented: no bars, None, an empty frame, or data that has not
  reached the deadline all leave the trade PENDING_FILL, however far the wall clock moves.

TTL and NO_FILL are judged on DATA time, not wall time (ADR-001 amendment 3).
"""

from __future__ import annotations

import pandas as pd
import pytest
import us001_helpers as H


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


def run(db, alert, frame, now, **book_kw):
    book = H.make_book(db.engine, **book_kw)
    H.open_trade(book, alert)
    events = book.step(now, H.Feed(**{alert["series_key"]: frame}))
    return book, H.trade_row(db.engine, alert["id"]), events


def friday_then_monday(friday_from=(15, 20), friday_bars=10, monday_bars=6):
    friday = H.minute_bars(H.at(*friday_from, day=H.FRIDAY), [1000.0] * friday_bars)
    monday = H.minute_bars(H.at(9, 15, day=H.MONDAY), [1000.0] * monday_bars)
    return pd.concat([friday, monday])


FRIDAY_1529 = H.at(15, 29, 3, day=H.FRIDAY)
MONDAY_0920 = H.at(9, 20, day=H.MONDAY)


def gap_case():
    alert = H.make_alert("N1", stop=990.0)
    return (
        alert,
        H.minute_bars(H.at(10, 5), [1000.0, 985.0, 986.0]),
        H.at(10, 8),
        "GAP_THROUGH_STOP",
        ("gap_through_stop",),
    )


def expired_case():
    alert = H.make_alert("N1")
    return alert, H.minute_bars(H.at(10, 16), [1000.0] * 3), H.at(10, 30), "NO_FILL", ("expired",)


def friday_case():
    alert = H.make_alert("N1", created=FRIDAY_1529)
    return alert, friday_then_monday(), MONDAY_0920, "NO_FILL", ("session_closed", "expired")


def friday_long_ttl_case():
    alert = H.make_alert("N1", created=FRIDAY_1529, expires=H.at(12, 0, day=H.MONDAY))
    return alert, friday_then_monday(), MONDAY_0920, "NO_FILL", ("session_closed",)


def session_over_case():
    friday = H.minute_bars(H.at(15, 20, day=H.FRIDAY), [1000.0] * 10)  # the last bar opens 15:29
    alert = H.make_alert("N1", created=FRIDAY_1529)
    return alert, friday, H.at(15, 31, day=H.FRIDAY), "NO_FILL", ("session_closed",)


CASES = [
    pytest.param(gap_case, id="gap-through-stop"),
    pytest.param(expired_case, id="deadline-passed"),
    pytest.param(friday_case, id="friday-1529-with-monday-bars"),
    pytest.param(friday_long_ttl_case, id="friday-1529-ttl-reaching-monday"),
    pytest.param(session_over_case, id="session-closed-no-later-bar-yet"),
]


class TestGapThroughTheStop:
    @pytest.mark.parametrize(
        "direction, stop, fill_open, gaps",
        [
            ("long", 990.0, 985.0, True),
            ("long", 990.0, 989.95, True),
            ("long", 990.0, 995.0, False),
            ("short", 1010.0, 1015.0, True),
            ("short", 1010.0, 1010.05, True),
            ("short", 1010.0, 1005.0, False),
        ],
    )
    def test_a_stock_whose_fill_bar_opens_beyond_its_stop_never_fills(
        self, db, direction, stop, fill_open, gaps
    ):
        alert = H.make_alert("A1", direction=direction, stop=stop)
        frame = H.minute_bars(H.at(10, 5), [1000.0, fill_open, fill_open])
        _, row, events = run(db, alert, frame, H.at(10, 8))
        if gaps:
            assert H.val(row["status"]) == "NO_FILL"
            assert H.val(row["close_reason"]) == "GAP_THROUGH_STOP"
            assert [H.val(e.kind) for e in events] == ["NO_FILL"]
        else:
            assert H.val(row["status"]) == "OPEN" and row["close_reason"] is None

    @pytest.mark.parametrize(
        "direction, stop, fill_open, gaps",
        [
            ("long", 53850.0, 53800.0, True),
            ("long", 53850.0, 53900.0, False),
            ("short", 54150.0, 54200.0, True),  # BUY_PE: the stop is ABOVE the underlying
            ("short", 54150.0, 54100.0, False),
        ],
    )
    def test_an_index_option_is_judged_on_the_underlying_open_against_the_underlying_stop(
        self, db, direction, stop, fill_open, gaps
    ):
        alert = H.make_alert("O1", kind="INDEX", direction=direction, stop=stop)
        frame = H.minute_bars(H.at(10, 5), [54000.0, fill_open, fill_open])
        _, row, _ = run(db, alert, frame, H.at(10, 8))
        assert H.val(row["status"]) == ("NO_FILL" if gaps else "OPEN")
        if gaps:
            assert H.val(row["close_reason"]) == "GAP_THROUGH_STOP"

    def test_only_the_fill_bar_is_judged_against_the_stop(self, db):
        """The 10:05 bar (open <= created_at) gaps below the stop but is not the fill bar; the
        later 10:07 bar gaps too, but the trade already filled at 10:06."""
        alert = H.make_alert("A1", stop=990.0)
        frame = H.minute_bars(H.at(10, 5), [980.0, 1000.0, 970.0])
        _, row, events = run(db, alert, frame, H.at(10, 8))
        # filled at 10:06 on the FILLED event and the entry columns (the 10:07 gap below the stop
        # then stops the open trade out in the same step: US-002), never a NO_FILL
        assert len(H.fills(events)) == 1
        H.assert_filled(row, at_ts=H.at(10, 6), entry=100030)
        assert "NO_FILL" not in H.kinds_of(db.engine, "A1")
        assert row["close_reason"] != "GAP_THROUGH_STOP"

    def test_the_reason_is_written_on_the_event(self, db):
        _, _, (event,) = run(db, *gap_case()[:3])
        assert H.val(event.kind) == "NO_FILL"
        assert "gap_through_stop" in H.blob(event.detail)


class TestGapThroughTheTarget:
    """US-002 decision 3: a fill bar that opens AT or BEYOND the alert's target is NO_FILL,
    reason GAP_THROUGH_TARGET (its target would sit behind the entry). One tick inside fills."""

    @pytest.mark.parametrize(
        "direction, target, fill_open, gaps",
        [
            ("long", 1020.0, 1020.0, True),  # AT the target
            ("long", 1020.0, 1020.05, True),
            ("long", 1020.0, 1100.0, True),
            ("long", 1020.0, 1019.95, False),  # one tick inside: fills
            ("short", 980.0, 980.0, True),
            ("short", 980.0, 979.95, True),
            ("short", 980.0, 900.0, True),
            ("short", 980.0, 980.05, False),
        ],
    )
    def test_a_stock_whose_fill_bar_opens_at_or_beyond_its_target_never_fills(
        self, db, direction, target, fill_open, gaps
    ):
        alert = H.make_alert("A1", direction=direction, target=target)
        frame = H.minute_bars(H.at(10, 5), [1000.0, fill_open, fill_open])
        book, row, events = run(db, alert, frame, H.at(10, 8))
        if gaps:
            assert (H.val(row["status"]), H.val(row["close_reason"])) == (
                "NO_FILL",
                "GAP_THROUGH_TARGET",
            )
            assert row["entry_paise"] is None and row["exit_paise"] is None
            assert row["costs_paise"] == 0 and row["net_paise"] == 0
            assert [H.val(e.kind) for e in events] == ["NO_FILL"]
            assert "gap_through_target" in H.blob(events[0].detail)
            assert book.open_trades() == [] and book.status()["no_fill"] == 1
            assert book.status()["filled"] == 0
        else:
            assert len(H.fills(events)) == 1
            H.assert_filled(row, at_ts=H.at(10, 6))

    @pytest.mark.parametrize(
        "direction, target, fill_open, gaps",
        [
            ("long", 54300.0, 54300.0, True),
            ("long", 54300.0, 54299.95, False),
            ("short", 53700.0, 53700.0, True),  # BUY_PE: the target is BELOW the underlying
            ("short", 53700.0, 53700.05, False),
        ],
    )
    def test_an_index_option_is_judged_on_the_underlying_open_against_the_underlying_target(
        self, db, direction, target, fill_open, gaps
    ):
        alert = H.make_alert("O1", kind="INDEX", direction=direction, target=target)
        frame = H.minute_bars(H.at(10, 5), [54000.0, fill_open, fill_open])
        _, row, events = run(db, alert, frame, H.at(10, 8))
        if gaps:
            assert (H.val(row["status"]), H.val(row["close_reason"])) == (
                "NO_FILL",
                "GAP_THROUGH_TARGET",
            )
            assert row["entry_paise"] is None and "FILLED" not in H.kinds_of(db.engine, "O1")
        else:
            H.assert_filled(row, at_ts=H.at(10, 6))

    def test_an_alert_without_a_target_has_nothing_to_gap_through(self, db):
        alert = H.make_alert("A1")
        alert["economics"]["underlying_target"] = alert["economics"]["target_price"] = None
        frame = H.minute_bars(H.at(10, 5), [1000.0, 5000.0, 5000.0])  # a huge open: still a fill
        _, row, events = run(db, alert, frame, H.at(10, 8))
        assert len(H.fills(events)) == 1
        H.assert_filled(row, at_ts=H.at(10, 6))

    def test_the_stop_gap_wins_when_the_open_is_beyond_the_stop_and_the_target_is_inverted(
        self, db
    ):
        """A long open below the stop is GAP_THROUGH_STOP even if it is also 'beyond' a target
        that was (wrongly) placed below it: the stop is the first test."""
        alert = H.make_alert("A1", stop=990.0, target=985.0)
        frame = H.minute_bars(H.at(10, 5), [1000.0, 980.0, 980.0])
        _, row, _ = run(db, alert, frame, H.at(10, 8))
        assert H.val(row["close_reason"]) == "GAP_THROUGH_STOP"


class TestDeadlineAndSession:
    def test_the_window_end_is_exclusive_after_expires_at_for_a_live_alert(self, db):
        alert = H.make_alert("A1", created=H.at(10, 5))  # expires 10:15:00
        frame = H.minute_bars(H.at(10, 16), [1000.0, 1001.0])  # first bar opens after expires_at
        _, row, events = run(db, alert, frame, H.at(10, 30))
        assert H.val(row["status"]) == "NO_FILL" and H.val(row["close_reason"]) == "NO_FILL"
        assert [H.val(e.kind) for e in events] == ["NO_FILL"]
        assert "expired" in H.blob(events[0].detail)

    def test_an_alert_at_1529_on_a_friday_never_fills_on_monday(self, db):
        alert, frame, now, *_ = friday_case()
        _, row, events = run(db, alert, frame, now)
        assert H.val(row["status"]) == "NO_FILL" and H.val(row["close_reason"]) == "NO_FILL"
        assert row["entry_paise"] is None
        assert [H.val(e.kind) for e in events] == ["NO_FILL"]

    def test_a_ttl_that_reaches_monday_still_cannot_fill_on_monday(self, db):
        """The same-session rule is separate from the TTL: expires_at says Monday 12:00 and the
        Monday 09:15 bar is inside it, but it belongs to another session date."""
        alert, frame, now, *_ = friday_long_ttl_case()
        _, row, events = run(db, alert, frame, now)
        assert H.val(row["status"]) == "NO_FILL" and row["entry_paise"] is None
        assert "session" in H.blob(events[0].detail)

    def test_a_delayed_alert_cannot_fill_on_monday_either(self, db):
        alert = H.make_alert("N1", created=FRIDAY_1529, source="yahoo", resolution="2m")
        _, row, _ = run(db, alert, friday_then_monday(), MONDAY_0920)
        assert H.val(row["status"]) == "NO_FILL" and row["entry_paise"] is None

    def test_a_frame_holding_only_the_next_session_cannot_fill_a_friday_alert(self, db):
        alert = H.make_alert("N1", created=FRIDAY_1529, source="yahoo")
        monday_only = H.minute_bars(H.at(9, 15, day=H.MONDAY), [1000.0] * 6)
        _, row, _ = run(db, alert, monday_only, MONDAY_0920)
        assert H.val(row["status"]) == "NO_FILL" and row["entry_paise"] is None

    def test_once_the_data_has_reached_the_session_end_with_no_later_bar_it_closes_no_fill(
        self, db
    ):
        """15:29:03 alert: the last session bar (15:29) opened before it, so no bar can ever
        fill it on that date. Data has reached 15:30; the session has closed."""
        alert, frame, now, *_ = session_over_case()
        _, row, events = run(db, alert, frame, now)
        assert H.val(row["status"]) == "NO_FILL" and row["entry_paise"] is None
        assert [H.val(e.kind) for e in events] == ["NO_FILL"]

    def test_a_late_arriving_delayed_feed_is_not_cut_off_by_the_wall_clock(self, db):
        """Yahoo data is ~15 minutes behind. Created Friday 15:20:03, it is 15:36 on the wall
        but the data stops at 15:10: the 15:22..15:29 bars are still to come, so the trade must
        wait (NO_FILL is judged on data time) and then fill on the first of them."""
        created = H.at(15, 20, 3, day=H.FRIDAY)
        alert = H.make_alert("N1", created=created, source="yahoo", resolution="2m")
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        behind = H.minute_bars(H.at(15, 0, day=H.FRIDAY), [1000.0] * 6, step=2)  # 15:00 .. 15:10
        assert book.step(H.at(15, 36, day=H.FRIDAY), H.Feed(SBIN=behind)) == []
        assert H.val(H.trade_row(db.engine, "N1")["status"]) == "PENDING_FILL"
        caught_up = H.minute_bars(H.at(15, 0, day=H.FRIDAY), [1000.0] * 15, step=2)  # .. 15:28
        events = book.step(H.at(15, 45, day=H.FRIDAY), H.Feed(SBIN=caught_up))
        row = H.trade_row(db.engine, "N1")
        # filled on the first late bar; the same step may also close it (session over: US-002)
        assert len(H.fills(events)) == 1
        H.assert_filled(row, at_ts=H.at(15, 22, day=H.FRIDAY))


class TestNoPriceIsInventedWhileWaiting:
    @pytest.mark.parametrize(
        "frame",
        [
            None,
            pd.DataFrame(),
            pd.DataFrame(columns=["open", "high", "low", "close", "volume"]),
            H.minute_bars(H.at(10, 0), [1000.0] * 6),  # ends at 10:05, not after created_at
        ],
        ids=["none", "empty-no-columns", "empty-with-columns", "only-older-bars"],
    )
    @pytest.mark.parametrize(
        "now", [H.at(10, 6), H.at(10, 40), H.at(13, 0)], ids=["soon", "late", "much-later"]
    )
    def test_without_newer_data_the_trade_waits_however_far_the_wall_clock_moves(
        self, db, frame, now
    ):
        _, row, events = run(db, H.make_alert("A1"), frame, now)
        assert events == []
        assert H.val(row["status"]) == "PENDING_FILL"
        for col in ("entry_paise", "exit_paise", "fill_bar_ts", "entry_ts", "close_reason"):
            assert row[col] is None, col

    def test_a_waiting_trade_is_still_pending_in_the_open_trade_list(self, db):
        book, row, _ = run(db, H.make_alert("A1"), None, H.at(11, 0))
        assert [t.id for t in book.open_trades()] == [row["id"]]
        assert H.val(book.open_trades()[0].status) == "PENDING_FILL"
        assert book.open_trades()[0].entry_paise is None


class TestNoFillRows:
    @pytest.mark.parametrize("case", CASES)
    def test_a_no_fill_row_has_no_prices_and_zero_costs_and_zero_net(self, db, case):
        alert, frame, now, reason, _ = case()
        _, row, _ = run(db, alert, frame, now)
        assert H.val(row["status"]) == "NO_FILL"
        assert H.val(row["close_reason"]) == reason
        assert row["entry_paise"] is None and row["exit_paise"] is None
        assert row["costs_paise"] == 0 and row["net_paise"] == 0
        assert row["gross_paise"] in (None, 0)

    @pytest.mark.parametrize("case", CASES)
    def test_the_no_fill_event_says_why(self, db, case):
        alert, frame, now, _, why = case()
        _, row, events = run(db, alert, frame, now)
        (event,) = events
        assert event.trade_id == row["id"] and H.val(event.kind) == "NO_FILL"
        assert any(w in H.blob(event.detail) for w in why), H.blob(event.detail)
        stored = [e for e in H.event_rows(db.engine, "N1") if H.val(e["kind"]) == "NO_FILL"]
        assert len(stored) == 1 and stored[0]["trade_id"] == row["id"]

    @pytest.mark.parametrize("case", CASES)
    def test_a_no_fill_is_final_and_not_listed_as_open(self, db, case):
        alert, frame, now, _, _ = case()
        book, row, _ = run(db, alert, frame, now)
        assert book.open_trades() == []
        later = now + pd.Timedelta(hours=1)
        assert book.step(later, H.Feed(**{alert["series_key"]: frame})) == []
        assert H.trade_row(db.engine, "N1") == row
        assert [H.val(e["kind"]) for e in H.event_rows(db.engine, "N1")] == ["OPENED", "NO_FILL"]

    @pytest.mark.parametrize("case", [gap_case, expired_case])
    def test_back_filled_bars_inside_the_window_cannot_revive_a_no_fill(self, db, case):
        alert, frame, now, _, _ = case()
        book, row, _ = run(db, alert, frame, now)
        revived = H.minute_bars(H.at(10, 0), [1000.0] * 40)  # 10:00 .. 10:39, all inside the TTL
        assert book.step(H.at(11, 0), H.Feed(SBIN=revived)) == []
        assert H.trade_row(db.engine, "N1") == row

    def test_filled_and_no_fill_trades_are_counted_apart(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="F1", stop=990.0)
        H.open_trade(book, alert_id="G1", stop=1005.0, instrument_key="RELIANCE")
        book.step(
            H.at(10, 8),
            H.Feed(SBIN=H.standard_bars(), RELIANCE=H.standard_bars((998.0, 1000.0, 1001.0))),
        )
        status = book.status()
        assert H.val(H.trade_row(db.engine, "F1")["status"]) == "OPEN"
        assert H.val(H.trade_row(db.engine, "G1")["status"]) == "NO_FILL"
        assert status["opened"] == 2 and status["filled"] == 1
        assert status["no_fill"] == 1 and status["closed"] == 0
        assert [t.alert_id for t in book.open_trades()] == ["F1"]
