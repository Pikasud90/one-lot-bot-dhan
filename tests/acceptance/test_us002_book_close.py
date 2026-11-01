"""US-002 through the public contract of the book: `PaperBook.step(now, bars, strategy_exit)`
feeds each OPEN trade through `manage_step` and books the close (AC1 to AC5 end to end).

A closed trade row says how it ended: status CLOSED, close_reason (STOP, TRAIL_STOP, TARGET,
SIGNAL_EXIT, OPPOSITE_SIGNAL, SESSION_END, TIME_STOP), exit_paise, exit_ts, and
gross_paise = sign x (exit - entry) x qty (story notes, Manager scope decisions):

* a STOCK exit mirrors the Lab (Manager decision 2, 2026-11-01, ADR-001 am. 11): the stop, a
  gap at the open, an exit at an open (strategy exit, time stop) and the session end are
  SLIPPED by `slippage_pct` (a PERCENT) against the trader and snapped to the tick against the
  trader (a long sells down, a short buys back up); a TARGET is a limit fill and is NOT slipped.
  `us002_helpers.expected_stock_exit` is the exact oracle; the headline numbers below are also
  written out by hand (long stop 99000 -> 98970, short stop 101000 -> 101035);
* an INDEX option exits at the Lab's exit fill, `fill.option_exit_paise(premium_at(option,
  underlying close level, close ts))` on the UNSLIPPED underlying level with the option frozen
  at the alert, and its P&L is LONG PREMIUM: a BUY_PE (direction short on the underlying)
  profits when the underlying FALLS. `lab.execution.execute_trade` is the differential oracle
  for that exit fill and the gross;
* CLOSED events and the row's exit_ts carry the OPEN time of the bar that decided the close;
* the levels are the ALERT's underlying_stop / underlying_target; a fill bar that opens at or
  beyond the alert target is NO_FILL, reason gap_through_target (Manager decision 3);
* costs_paise and net_paise are US-023 (pulled into Sprint 1 right after this story): until
  then they stay NULL, never a fake 0.

Worked stock: alert long SBIN 1000.00, stop 990.00, target 1020.00, 10:05:03; the fill bar is
10:06 and opens 1000.00, so entry_paise is 100030 (US-001) and one share is traded. Its short
twin fills at 99970.
"""

from __future__ import annotations

import datetime as dt

import pytest
import us001_helpers as H
import us002_helpers as U

Q = (100000, 100100, 99900, 100000)
MIN = dt.timedelta(minutes=1)
BAR1 = (100000, 101200, 100100, 101100)  # arms the 1.5 x 4.00 trail at 100500


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


def go(db, alert=None, rows=None, *, frame=None, now=None, se=None, book=None, **book_kw):
    """Open one trade, step the book once, return (book, closed-or-open row, events)."""
    book = book or H.make_book(db.engine, **book_kw)
    alert = alert or U.make_alert("A1")
    H.open_trade(book, alert)
    frame = frame if frame is not None else U.story(rows)
    events = U.step_book(book, frame, now, se, series=alert["series_key"])
    return book, U.row_of(db.engine, alert["id"]), events


def drift(n, step=10):
    return [(100000 + step * i, 100100 + step * i, 99900, 100000 + step * i) for i in range(n)]


def assert_closed(row, reason, level, direction="long", entry=None):
    """CLOSED with `reason`; exit_paise is the rule `level` slipped as the Lab does (a target is
    not slipped); gross is sign x (exit - entry) x 1 share."""
    entry = (U.ENTRY_L if direction == "long" else U.ENTRY_S) if entry is None else entry
    exit_ = U.expected_stock_exit(level, direction, reason)
    assert row["status"] == "CLOSED" and row["close_reason"] == reason, row
    assert row["entry_paise"] == entry and row["exit_paise"] == exit_, (row["exit_paise"], exit_)
    assert row["gross_paise"] == U.expected_gross(entry, exit_, direction)
    return exit_


class TestStockStop:
    def test_a_touch_of_the_stop_closes_the_trade_at_the_stop_slipped_against_the_trader(self, db):
        book, row, events = go(db, rows=[(100000, 100100, 99000, 99500)])
        assert assert_closed(row, "STOP", 99000) == 98970  # 990.00 x 0.9997 = 989.703 -> 989.70
        assert row["gross_paise"] == 98970 - U.ENTRY_L == -1060
        assert row["exit_ts"] == H.at(10, 6) and row["qty"] == 1

    def test_the_book_hands_back_the_close_and_forgets_the_trade(self, db):
        book, row, events = go(db, rows=[(100000, 100100, 99000, 99500)])
        assert [k for k in U.kinds(events) if k in ("FILLED", "CLOSED")] == ["FILLED", "CLOSED"]
        assert U.close_of(events).price == 99000, "the event keeps the RULE level, before slippage"
        assert row["exit_paise"] == 98970
        assert book.open_trades() == []
        assert book.status()["closed"] == 1 and book.status()["hook_errors"] == 0

    def test_the_close_is_persisted_as_an_event_with_the_rows_exit_time(self, db):
        _, row, events = go(db, rows=[(100000, 100100, 99000, 99500)])
        stored = U.stored_close_events(db.engine)
        assert len(stored) == 1 and H.same_instant(row["exit_ts"], U.close_of(events).at)
        assert (
            stored[0]["detail"]["reason"] == "STOP" and stored[0]["detail"]["price_paise"] == 99000
        )
        order = [str(H.val(e["kind"])) for e in H.event_rows(db.engine, "A1")]
        assert order[0] == "OPENED" and order.index("FILLED") < order.index("CLOSED")

    def test_a_gap_below_the_stop_exits_at_the_open_slipped(self, db):
        _, row, events = go(db, rows=[Q, (98500, 98700, 98000, 98600)])
        assert assert_closed(row, "STOP", 98500) == 98470
        assert row["gross_paise"] == 98470 - U.ENTRY_L == -1560
        assert H.same_instant(row["exit_ts"], H.at(10, 7))
        assert U.close_of(events).gap is True

    def test_stop_beats_target_on_the_same_bar(self, db):
        _, row, _ = go(db, rows=[(100000, 102100, 98900, 100000)])
        assert_closed(row, "STOP", 99000)

    def test_the_fill_bar_is_managed_and_an_earlier_bar_is_not(self, db):
        """The 10:05 bar (before the alert) dips below the stop; only the fill bar onward counts."""
        frame = U.pbars(H.at(10, 5), [(99000, 99000, 50000, 99000), Q, Q])
        _, row, _ = go(db, frame=frame)
        assert row["status"] == "OPEN" and row["close_reason"] is None


class TestStockTarget:
    def test_trading_through_the_target_exits_at_exactly_the_target_unslipped(self, db):
        _, row, _ = go(db, rows=[Q, (100500, 102010, 100400, 101800)])
        assert assert_closed(row, "TARGET", 102000) == 102000
        assert row["gross_paise"] == 102000 - U.ENTRY_L == 1970

    def test_reaching_the_target_exactly_leaves_the_trade_open(self, db):
        _, row, _ = go(db, rows=[Q, (100500, 102000, 100400, 101800)])
        assert row["status"] == "OPEN" and row["exit_paise"] is None
        assert row["gross_paise"] in (None, 0)

    def test_a_gap_up_through_the_target_still_exits_at_the_target_not_the_open(self, db):
        _, row, _ = go(db, rows=[Q, (103000, 103100, 102900, 103050)])
        assert assert_closed(row, "TARGET", 102000) == 102000

    def test_a_trade_without_a_target_is_never_closed_by_one(self, db):
        alert = U.make_alert("A1")
        alert["economics"]["underlying_target"] = None
        _, row, _ = go(db, alert, rows=[Q, (100500, 900000, 100400, 800000)])
        assert row["status"] == "OPEN"

    def test_the_target_is_the_alerts_underlying_target_not_one_derived_from_target_r(self, db):
        """underlying_target 1010.00 (the owner was told 1010), target_r 2.0 would say ~1020.90."""
        alert = U.make_alert("A1", target=1010.0)
        _, row, _ = go(db, alert, rows=[Q, (100500, 101010, 100400, 100900)])
        assert assert_closed(row, "TARGET", 101000) == 101000

    def test_the_stop_is_the_alerts_underlying_stop(self, db):
        alert = U.make_alert("A1", stop=995.0)
        _, row, _ = go(db, alert, rows=[Q, (100000, 100100, 99500, 99800)])
        assert assert_closed(row, "STOP", 99500) == U.expected_stock_exit(99500, "long", "STOP")


class TestShorts:
    def alert(self, **kw):
        return U.make_alert("A1", direction="short", **kw)

    def test_a_short_fills_at_the_sell_side_and_stops_on_a_touch_from_below_slipped_up(self, db):
        _, row, _ = go(db, self.alert(), rows=[(100000, 101000, 99900, 100500)])
        assert assert_closed(row, "STOP", 101000, "short") == 101035  # 1010.00 x 1.0003 -> 1010.35
        assert row["gross_paise"] == -(101035 - U.ENTRY_S) == -1065

    def test_a_short_gap_up_through_the_stop_exits_at_the_open_slipped_up(self, db):
        _, row, _ = go(db, self.alert(), rows=[Q, (101500, 101700, 101400, 101600)])
        assert assert_closed(row, "STOP", 101500, "short") == 101535

    def test_a_short_target_pays_a_positive_gross_unslipped(self, db):
        _, row, _ = go(db, self.alert(), rows=[Q, (99500, 99600, 97995, 98200)])
        assert assert_closed(row, "TARGET", 98000, "short") == 98000
        assert row["gross_paise"] == -(98000 - U.ENTRY_S) == 1970

    def test_a_short_that_trails_closes_trail_stop_slipped_up(self, db):
        alert = self.alert(manage={"trail_atr_mult": 1.5, "atr": 4.0})
        _, row, _ = go(
            db, alert, rows=[(100000, 100100, 98800, 98900), (98900, 99500, 98800, 99000)]
        )
        assert assert_closed(row, "TRAIL_STOP", 99500, "short") == 99530
        assert row["gross_paise"] == -(99530 - U.ENTRY_S) == 440


class TestTheTrailThroughTheAlertsFrozenManageBlock:
    def test_a_trailed_stop_closes_trail_stop_at_the_moved_level_slipped(self, db):
        alert = U.make_alert("A1", manage={"trail_atr_mult": 1.5, "atr": 4.0})
        _, row, _ = go(db, alert, rows=[BAR1, (101100, 101300, 100500, 101250)])
        assert assert_closed(row, "TRAIL_STOP", 100500) == 100465
        assert row["gross_paise"] == 100465 - U.ENTRY_L == 435

    def test_the_trail_does_not_act_inside_the_bar_that_arms_it(self, db):
        alert = U.make_alert("A1", manage={"trail_atr_mult": 1.5, "atr": 4.0})
        _, row, _ = go(db, alert, rows=[BAR1])
        assert row["status"] == "OPEN"

    @pytest.mark.parametrize("atr", [None, 0.0], ids=["none", "zero"])
    def test_an_alert_whose_atr_was_not_frozen_has_no_trail_and_no_live_fallback(self, db, atr):
        alert = U.make_alert("A1", manage={"trail_atr_mult": 1.5, "atr": atr})
        _, row, _ = go(db, alert, rows=[BAR1, (101100, 101200, 98900, 99000)])
        assert_closed(row, "STOP", 99000)

    def test_a_missing_multiple_means_no_trail(self, db):
        alert = U.make_alert("A1", manage={"trail_atr_mult": 0.0, "atr": 4.0})
        _, row, _ = go(db, alert, rows=[BAR1, (101100, 101200, 98900, 99000)])
        assert row["close_reason"] == "STOP"


class TestStrategyExitCallback:
    def rows(self):
        return [Q, Q, Q, Q, (100200, 100300, 100100, 100250), (100300, 100400, 100200, 100350)]

    def test_a_signal_exit_closes_at_the_first_fine_open_after_its_bar_slipped(self, db):
        seen = []

        def se(trade, now):
            seen.append((trade.alert_id, trade.status))
            return U.strategy_exit("SIGNAL_EXIT", H.at(10, 10)) if now >= H.at(10, 10) else None

        _, row, _ = go(db, rows=self.rows(), se=se)
        assert assert_closed(row, "SIGNAL_EXIT", 100200) == 100165  # 1002.00 x 0.9997 -> 1001.65
        assert row["gross_paise"] == 100165 - U.ENTRY_L == 135
        assert H.same_instant(row["exit_ts"], H.at(10, 10))
        assert seen and set(seen) == {("A1", "OPEN")}, "asked only for OPEN trades"

    def test_an_opposite_signal_closes_the_same_way(self, db):
        se = lambda trade, now: U.strategy_exit("OPPOSITE_SIGNAL", H.at(10, 10))  # noqa: E731
        _, row, _ = go(db, rows=self.rows(), se=se)
        assert_closed(row, "OPPOSITE_SIGNAL", 100200)

    def test_an_exit_due_at_the_open_is_not_pre_empted_by_a_dip_later_in_that_bar(self, db):
        """Manager decision 1: chronological bars. The 10:10 bar opens 1002.00, the exit is due
        there, and the bar later dips through the 990.00 stop: the trade already left at the open."""
        rows = [Q, Q, Q, Q, (100200, 100300, 98900, 99500)]
        se = lambda trade, now: U.strategy_exit("SIGNAL_EXIT", H.at(10, 10))  # noqa: E731
        _, row, _ = go(db, rows=rows, se=se)
        assert assert_closed(row, "SIGNAL_EXIT", 100200) == 100165

    def test_the_callback_is_never_asked_about_a_trade_that_is_not_open(self, db):
        asked = []
        se = lambda trade, now: asked.append(trade.status)  # noqa: E731
        book = H.make_book(db.engine)
        U.opened(book, U.make_alert("W1", created=H.at(10, 40, 3)))  # waiting: no bar yet
        U.opened(book, U.make_alert("C1", instrument_key="RELIANCE"))
        feed = H.Feed(SBIN=U.story([Q]), RELIANCE=U.story([(100000, 100100, 99000, 99500)]))
        book.step(U.end_of(U.story([Q])), feed, se)
        assert "PENDING_FILL" not in asked and "CLOSED" not in asked
        book.step(U.end_of(U.story([Q])), feed, se)
        assert "PENDING_FILL" not in asked and "CLOSED" not in asked
        assert [r for r in H.trade_rows(db.engine) if H.val(r["status"]) == "CLOSED"]

    def test_a_callback_that_raises_never_stops_the_other_trades_or_escapes(self, db):
        book = H.make_book(db.engine)
        U.opened(book, U.make_alert("B1"))
        U.opened(book, U.make_alert("B2", instrument_key="RELIANCE"))
        feed = H.Feed(SBIN=U.story([Q, Q]), RELIANCE=U.story([(100000, 100100, 99000, 99500)]))

        def se(trade, now):
            if trade.alert_id == "B1":
                raise RuntimeError("strategy blew up")
            return None

        book.step(H.at(10, 30), feed, se)  # must not raise
        assert U.row_of(db.engine, "B2")["status"] == "CLOSED"
        assert U.row_of(db.engine, "B1")["status"] in ("OPEN", "PENDING_FILL")


class TestSessionEnd:
    def late_alert(self, **kw):
        return U.make_alert("A1", created=H.at(14, 57, 3), **kw)

    def late_frame(self, n=23):
        return U.pbars(H.at(14, 57), [U.PRE_BAR, *drift(n)])

    def test_every_trade_closes_session_end_at_the_first_open_from_15_15_slipped(self, db):
        """Fill bar 14:58; the 15:15 bar is the 18th row: open 100000 + 17 x 10 = 100170."""
        _, row, _ = go(db, self.late_alert(), frame=self.late_frame())
        assert assert_closed(row, "SESSION_END", 100170) == 100135
        assert row["gross_paise"] == 100135 - U.ENTRY_L == 105
        assert H.same_instant(row["exit_ts"], H.at(15, 15))

    def test_a_swing_alert_is_not_carried_overnight_either(self, db):
        _, row, _ = go(db, self.late_alert(horizon="swing"), frame=self.late_frame())
        assert row["close_reason"] == "SESSION_END" and H.same_instant(row["exit_ts"], H.at(15, 15))

    def test_the_exit_time_comes_from_the_alerts_manage_block(self, db):
        alert = self.late_alert(manage={"exit_time": "15:05"})
        _, row, _ = go(db, alert, frame=self.late_frame())
        assert H.same_instant(row["exit_ts"], H.at(15, 5))
        assert assert_closed(row, "SESSION_END", 100000 + 10 * 7) == 100035

    def test_the_first_step_on_a_later_day_still_closes_at_the_sessions_exit(self, db):
        stamps = [H.at(14, 57) + MIN * i for i in range(33)]  # 14:57 .. 15:29
        stamps += [H.at(9, 15, day=H.FRIDAY) + MIN * i for i in range(10)]
        rows = [U.PRE_BAR, *drift(32)] + [(90000, 90100, 89900, 90000)] * 10
        frame = U.pbars_at(stamps, rows)
        _, row, _ = go(db, self.late_alert(), frame=frame, now=H.at(9, 30, day=H.FRIDAY))
        assert row["status"] == "CLOSED" and row["close_reason"] == "SESSION_END"
        assert H.same_instant(row["exit_ts"], H.at(15, 15))
        assert row["exit_paise"] == 100135, "the 15:15 open of the trading day, not Friday's gap"

    def test_a_feed_that_stops_before_exit_time_is_closed_at_the_last_close_after_the_session(
        self, db
    ):
        frame = U.pbars(H.at(14, 57), [U.PRE_BAR, *drift(6)])  # the last bar opens 15:03
        _, row, _ = go(db, self.late_alert(), frame=frame, now=H.at(15, 40))
        assert assert_closed(row, "SESSION_END", 100050) == 100015

    def test_nothing_closes_before_exit_time(self, db):
        _, row, _ = go(db, self.late_alert(), frame=U.pbars(H.at(14, 57), [U.PRE_BAR, *drift(10)]))
        assert row["status"] == "OPEN"


class TestTimeStop:
    def test_max_bars_closes_time_stop_at_the_next_open_slipped(self, db):
        alert = U.make_alert("A1", manage={"max_bars": 3, "tf_min": 1})
        _, row, _ = go(db, alert, rows=drift(8, step=20))
        assert H.same_instant(row["exit_ts"], H.at(10, 9))
        assert assert_closed(row, "TIME_STOP", 100060) == 100025
        assert row["gross_paise"] == 100025 - U.ENTRY_L == -5

    def test_max_bars_zero_never_closes_on_time(self, db):
        alert = U.make_alert("A1", manage={"max_bars": 0, "tf_min": 1})
        _, row, _ = go(db, alert, rows=U.flat(100000, 60))
        assert row["status"] == "OPEN"


class TestTheExitSlippageRule:
    """Manager decision 2, across every reason and both directions, against the exact oracle."""

    def test_the_slippage_comes_from_the_books_backtest_config_a_percent(self, db):
        """slippage_pct 0.10 (a PERCENT): 99000 x 0.999 = 98901 -> 98900 on the 5-paise tick."""
        from onelot.config import BacktestCfg

        bt = BacktestCfg(slippage_pct=0.10, option_slippage_ticks=2, option_spread_pct=0.006)
        _, row, _ = go(db, rows=[(100000, 100100, 99000, 99500)], bt=bt)
        assert row["exit_paise"] == U.expected_stock_exit(99000, "long", "STOP", 0.10) == 98900
        assert row["entry_paise"] == H.expected_stock_entry(1000.0, "long", 0.10)

    def test_zero_slippage_exits_at_the_rule_level(self, db):
        from onelot.config import BacktestCfg

        bt = BacktestCfg(slippage_pct=0.0, option_slippage_ticks=2, option_spread_pct=0.006)
        _, row, _ = go(db, rows=[(100000, 100100, 99000, 99500)], bt=bt)
        assert row["exit_paise"] == 99000 and row["gross_paise"] == 99000 - row["entry_paise"]


class TestAFillBarOpeningAtOrBeyondTheTarget:
    """Manager decision 3, widened by review round 1 (MAJOR-2): the owner was told stop and target
    as the alert's underlying levels. A stock whose fill bar opens AT or BEYOND the target (long:
    open >= T; short: open <= T), or whose SLIPPED entry is at or beyond it, is NO_FILL, reason
    gap_through_target (symmetric with gap_through_stop): it would be a 'target' behind the entry.
    The index rows below are judged on the underlying open alone (the entry is a premium)."""

    @pytest.mark.parametrize(
        "direction, fill_open, no_fill",
        [
            ("long", 1020.0, True),
            ("long", 1025.0, True),
            ("long", 1019.95, True),  # open below T, but the slipped entry 1020.25 is past it
            ("long", 1019.65, True),  # 1019.9559 -> 1020.00: the entry lands AT the target
            ("long", 1019.60, False),  # 1019.9058 -> 1019.95: one tick inside, fills
            ("long", 1000.0, False),
            ("short", 980.0, True),
            ("short", 975.0, True),
            ("short", 980.05, True),  # the slipped entry 979.75 is past the target
            ("short", 980.30, True),  # 980.0059 -> 980.00: AT the target
            ("short", 980.35, False),  # 980.0559 -> 980.05: one tick inside, fills
            ("short", 1000.0, False),
        ],
    )
    def test_a_stock_is_no_fill_when_its_fill_bar_opens_at_or_beyond_the_target(
        self, db, direction, fill_open, no_fill
    ):
        alert = U.make_alert(
            "A1", direction=direction
        )  # long: stop 990 target 1020; short: 1010 / 980
        frame = H.minute_bars(H.at(10, 5), [1000.0, fill_open, fill_open])
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        events = book.step(H.at(10, 8), H.Feed(SBIN=frame))
        row = U.row_of(db.engine, "A1")
        if no_fill:
            assert row["status"] == "NO_FILL" and row["entry_paise"] is None
            assert H.val(row["close_reason"]) in ("NO_FILL", "GAP_THROUGH_TARGET")
            assert row["exit_paise"] is None and row["gross_paise"] == 0
            assert [H.val(e.kind) for e in events] == ["NO_FILL"]
            assert "gap_through_target" in H.blob(events[0].detail)
            assert H.same_instant(events[0].ts, H.at(10, 6)), "stamped with the fill bar's open"
        else:
            assert row["status"] in ("OPEN", "CLOSED") and row["entry_paise"] is not None

    @pytest.mark.parametrize(
        "direction, fill_open, no_fill",
        [
            ("long", 54300.0, True),
            ("long", 54350.0, True),
            ("long", 54299.95, False),
            ("short", 53700.0, True),  # BUY_PE: the target is BELOW the underlying
            ("short", 53650.0, True),
            ("short", 53700.05, False),
        ],
    )
    def test_an_index_option_is_judged_on_the_underlying_open_against_the_underlying_target(
        self, db, direction, fill_open, no_fill
    ):
        alert = U.make_alert("O1", kind="INDEX", direction=direction)
        frame = H.minute_bars(H.at(10, 5), [54000.0, fill_open, fill_open])
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        events = book.step(H.at(10, 8), H.Feed(BANKNIFTY=frame))
        row = U.row_of(db.engine, "O1")
        assert (row["status"] == "NO_FILL") is no_fill
        if no_fill:
            assert "gap_through_target" in H.blob(events[0].detail)

    def test_the_stop_gap_rule_still_names_gap_through_stop(self, db):
        alert = U.make_alert("A1")
        frame = H.minute_bars(H.at(10, 5), [1000.0, 985.0, 985.0])
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        events = book.step(H.at(10, 8), H.Feed(SBIN=frame))
        assert U.row_of(db.engine, "A1")["close_reason"] == "GAP_THROUGH_STOP"
        assert "gap_through_stop" in H.blob(events[0].detail)

    def test_a_no_fill_for_the_target_is_final_and_never_managed(self, db):
        alert = U.make_alert("A1")
        frame = H.minute_bars(H.at(10, 5), [1000.0, 1025.0, 1025.0, 1030.0])
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        book.step(H.at(10, 9), H.Feed(SBIN=frame))
        before = U.row_of(db.engine, "A1")
        assert book.step(H.at(10, 12), H.Feed(SBIN=frame)) == []
        assert U.row_of(db.engine, "A1")["status"] == "NO_FILL" == before["status"]
        assert H.count_rows(db.engine, "paper_events") == 2  # OPENED + NO_FILL


class TestCostsAreUs023:
    def test_costs_and_net_stay_null_until_us023_never_a_fake_zero(self, db):
        """Manager scope decision. RETIRE this test in US-023, which books them."""
        _, row, _ = go(db, rows=[(100000, 100100, 99000, 99500)])
        assert row["costs_paise"] is None and row["net_paise"] is None
        assert row["gross_paise"] == -1060  # 98970 - 100030, the slipped exit

    def test_risk_paise_set_at_the_fill_is_untouched_by_the_close(self, db):
        book = H.make_book(db.engine)
        alert = U.opened(book, U.make_alert("A1"))
        U.step_book(book, U.story([Q]), H.at(10, 8))
        risk = U.row_of(db.engine)["risk_paise"]
        U.step_book(book, U.story([Q, (100000, 100100, 99000, 99500)]), H.at(10, 9))
        row = U.row_of(db.engine)
        assert row["status"] == "CLOSED" and row["risk_paise"] == risk == 1030
        assert alert["id"] == "A1"


class TestMoneyColumnsStayIntegers:
    def test_a_closed_rows_money_is_stored_as_sql_integers(self, db):
        go(db, rows=[(100000, 100100, 99000, 99500)])
        for column in ("entry_paise", "exit_paise", "gross_paise", "risk_paise"):
            assert H.typeof(db.engine, column, "A1") == "integer", column


class TestIndexOptionClosedOnTheUnderlyingPricedAsLongPremium:
    """exit_paise is the Lab's exit fill of the option at the underlying close level, and the P&L
    is long premium whichever way the alert's direction points."""

    PRE = (5399000, 5399500, 5398500, 5399000)

    def run(self, db, direction, rows, **kw):
        alert = U.make_alert("O1", kind="INDEX", direction=direction, **kw)
        pricer = H.make_pricer()
        frame = U.pbars(H.at(10, 5), [self.PRE, *rows])
        book = H.make_book(db.engine, premium_at=pricer)
        H.open_trade(book, alert)
        events = U.step_book(book, frame, series="BANKNIFTY")
        return U.row_of(db.engine, "O1"), events

    def oracle(self, direction, underlying_exit, exit_ts):
        opt = "CE" if direction == "long" else "PE"
        stop = 53850.0 if direction == "long" else 54150.0
        return U.oracle_option_roundtrip(
            bt=H.BT,
            spec=H.SPECS["BANKNIFTY_OPT"],
            opt_type=opt,
            underlying_entry=54000.0,
            underlying_exit=underlying_exit,
            entry_ts=H.at(10, 6),
            exit_ts=exit_ts,
            pricer=H.make_pricer(),
            stop=stop,
        )

    CE_FILL = (5400000, 5401000, 5399000, 5400500)
    PE_FILL = (5400000, 5400500, 5399500, 5399800)

    def test_a_buy_ce_target_exits_at_the_oracles_exit_fill_for_the_target_level(self, db):
        rows = [self.CE_FILL, (5400500, 5431000, 5400000, 5430000)]
        row, events = self.run(db, "long", rows)
        entry, exit_, gross = self.oracle("long", 54300.0, H.at(10, 7))
        assert row["close_reason"] == "TARGET" and row["status"] == "CLOSED"
        assert (row["entry_paise"], row["exit_paise"], row["gross_paise"]) == (entry, exit_, gross)
        assert gross > 0 and row["qty"] == H.LOT

    def test_a_buy_ce_stop_loses_premium(self, db):
        rows = [self.CE_FILL, (5400500, 5400600, 5384000, 5390000)]
        row, _ = self.run(db, "long", rows)
        entry, exit_, gross = self.oracle("long", 53850.0, H.at(10, 7))
        assert row["close_reason"] == "STOP"
        assert (row["entry_paise"], row["exit_paise"], row["gross_paise"]) == (entry, exit_, gross)
        assert gross < 0

    def test_a_buy_pe_target_is_a_PROFIT_when_the_underlying_falls(self, db):
        rows = [self.PE_FILL, (5399800, 5399900, 5369000, 5370500)]
        row, _ = self.run(db, "short", rows)
        entry, exit_, gross = self.oracle("short", 53700.0, H.at(10, 7))
        assert row["close_reason"] == "TARGET"
        assert (row["entry_paise"], row["exit_paise"], row["gross_paise"]) == (entry, exit_, gross)
        assert row["gross_paise"] > 0, "P&L is long premium: the put gained as the underlying fell"

    def test_a_buy_pe_stop_is_a_LOSS_when_the_underlying_rises(self, db):
        rows = [self.PE_FILL, (5399800, 5416000, 5399500, 5410000)]
        row, _ = self.run(db, "short", rows)
        entry, exit_, gross = self.oracle("short", 54150.0, H.at(10, 7))
        assert row["close_reason"] == "STOP"
        assert (row["entry_paise"], row["exit_paise"], row["gross_paise"]) == (entry, exit_, gross)
        assert row["gross_paise"] < 0

    def test_a_gap_through_the_option_stop_is_priced_at_the_open_level(self, db):
        rows = [self.CE_FILL, (5370000, 5371000, 5369000, 5370500)]
        row, _ = self.run(db, "long", rows)
        entry, exit_, gross = self.oracle("long", 53700.0, H.at(10, 7))
        assert row["close_reason"] == "STOP"
        assert (row["exit_paise"], row["gross_paise"]) == (exit_, gross)

    def test_a_session_end_close_is_priced_at_the_underlying_open_of_the_exit_bar(self, db):
        alert = U.make_alert("O1", kind="INDEX", created=H.at(14, 57, 3))
        stamps = [H.at(14, 57) + MIN * i for i in range(24)]
        rows = [self.PRE] + [
            (5400000 + 100 * i, 5400500 + 100 * i, 5399500, 5400000 + 100 * i) for i in range(23)
        ]
        frame = U.pbars_at(stamps, rows)
        book = H.make_book(db.engine, premium_at=H.make_pricer())
        H.open_trade(book, alert)
        U.step_book(book, frame, series="BANKNIFTY")
        row = U.row_of(db.engine, "O1")
        level = rows[1 + 17][0] / 100.0  # the 15:15 bar
        entry, exit_, gross = self.oracle("long", level, H.at(15, 15))
        assert row["close_reason"] == "SESSION_END"
        assert (row["exit_paise"], row["gross_paise"]) == (exit_, gross)

    def test_the_option_exit_is_a_whole_number_of_ticks_in_integer_paise(self, db):
        rows = [self.CE_FILL, (5400500, 5431000, 5400000, 5430000)]
        row, _ = self.run(db, "long", rows)
        assert row["exit_paise"] % 5 == 0 and H.typeof(db.engine, "exit_paise", "O1") == "integer"
