"""US-002 AC6, AC7 and AC8 through the book: `PaperBook.step`.

AC6  stepping again is empty and changes nothing; catching up in one step (or after a restart)
     equals watching bar by bar; event times are bar or exit times, never `now`; a bar is
     eligible iff its open + resolution <= now; booked events are final and store the bar
     prices they used, and a bar the trade has seen is never evaluated again.
AC7  truncation: every booked event up to t is unchanged when the bars after t are removed.
AC8  45 open trades on 30,000-row frames: `bars()` is called once per series and the cursor
     keeps a step with nothing new cheap; the Desk is not modified by this story.

Everything is driven through `H.make_book` (US-001 harness) and the public contract; the trade
rows are read back from SQLite exactly as a restart would see them.
"""

from __future__ import annotations

import ast
import copy
import datetime as dt
import time
from pathlib import Path

import pandas as pd
import pytest
import us001_helpers as H
import us002_helpers as U
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from test_us002_ac7_truncation import cases

MIN = dt.timedelta(minutes=1)
Q = (100000, 100100, 99900, 100000)
TRAIL = {"trail_atr_mult": 1.5, "atr": 4.0}
TRAIL_THEN_STOP = [
    (100000, 101200, 100100, 101100),
    (101100, 101300, 100800, 101150),
    (101150, 101400, 100900, 101300),
    (101300, 101350, 101000, 101050),
    (101000, 101100, 100700, 100800),
    (100800, 100900, 100600, 100700),
]
SIGNAL_ROWS = [Q, Q, Q, Q, (100200, 100300, 100100, 100250), (100300, 100400, 100200, 100350)]
PROP = settings(
    max_examples=12,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=list(HealthCheck),
)
DESK_PY = Path(H.ROOT) / "src" / "onelot" / "desk" / "desk.py"
SIGNAL = lambda trade, now: (  # noqa: E731
    U.strategy_exit("SIGNAL_EXIT", H.at(10, 10)) if now >= H.at(10, 10) else None
)


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
    """What a step must leave alone: every event row and every trade row but `updated_at`."""
    trades = [{k: v for k, v in r.items() if k != "updated_at"} for r in H.trade_rows(engine)]
    return {"trades": trades, "events": H.event_rows(engine)}


def final_row(engine, alert_id="A1"):
    keys = ("status", "close_reason", "entry_paise", "exit_paise", "gross_paise", "risk_paise")
    row = U.row_of(engine, alert_id)
    return {
        **{k: row[k] for k in keys},
        "exit_ts": row["exit_ts"],
        "last_bar_ts": row["last_bar_ts"],
    }


def watch(book, frame, alert, se=None):
    """One step as every bar closes: a desk that never missed a poll."""
    events = []
    for ts in frame.index:
        feed = H.Feed(**{alert["series_key"]: frame})
        now = (ts + MIN).to_pydatetime()
        events += book.step(now, feed) if se is None else book.step(now, feed, se)
    return events


# ── AC6 ───────────────────────────────────────────────────────────────────────────────
class TestIdempotence:
    def test_a_second_step_with_the_same_now_and_bars_is_empty_and_changes_nothing(self, db):
        book = H.make_book(db.engine)
        alert = U.opened(book, U.make_alert("A1", manage=TRAIL))
        frame = U.story(TRAIL_THEN_STOP)
        first = U.step_book(book, frame)
        assert U.close_of(first) is not None
        before = frozen(db.engine)
        assert U.step_book(book, frame) == []
        assert frozen(db.engine) == before and alert["id"] == "A1"

    def test_the_same_holds_for_a_trade_that_is_still_open(self, db):
        book = H.make_book(db.engine)
        U.opened(book, U.make_alert("A1"))
        frame = U.story([Q, Q, Q])
        U.step_book(book, frame)
        before = frozen(db.engine)
        assert U.step_book(book, frame) == []
        assert frozen(db.engine) == before and U.row_of(db.engine)["status"] == "OPEN"

    def test_a_second_book_on_the_same_database_does_not_book_the_close_twice(self, db):
        a, b = H.make_book(db.engine), H.make_book(db.engine)
        U.opened(a, U.make_alert("A1"))
        frame = U.story([(100000, 100100, 99000, 99500)])
        U.step_book(a, frame)
        assert U.step_book(b, frame) == []
        assert len(U.stored_close_events(db.engine)) == 1


class TestCatchUp:
    def books(self, db, db2, alert):
        a, b = H.make_book(db.engine), H.make_book(db2.engine)
        H.open_trade(a, copy.deepcopy(alert))
        H.open_trade(b, copy.deepcopy(alert))
        return a, b

    @pytest.mark.parametrize(
        "rows, manage, se",
        [
            (TRAIL_THEN_STOP, TRAIL, None),
            (SIGNAL_ROWS, {}, SIGNAL),
            ([Q, Q, (100500, 102010, 100400, 101800), Q], {}, None),
            (U.flat(100000, 8), {"max_bars": 3, "tf_min": 1}, None),
        ],
        ids=["trail-then-trailed-stop", "signal-exit", "target", "time-stop"],
    )
    def test_one_late_step_books_the_same_events_and_row_as_watching_every_bar(
        self, db, db2, rows, manage, se
    ):
        alert = U.make_alert("A1", manage=manage)
        frame = U.story(rows)
        once, watched = self.books(db, db2, alert)
        a = once.step(U.end_of(frame), H.Feed(SBIN=frame), se) if se else U.step_book(once, frame)
        b = watch(watched, frame, alert, se)
        assert U.canon(a, with_trade=True) == U.canon(b, with_trade=True)
        assert final_row(db.engine) == final_row(db2.engine)
        assert [(e["kind"], e["ts"]) for e in H.event_rows(db.engine)] == [
            (e["kind"], e["ts"]) for e in H.event_rows(db2.engine)
        ]

    def test_a_restart_in_the_middle_changes_nothing(self, db, db2):
        """Book A runs to 10:09 and 'dies'; a NEW book on the same database finishes. The
        trailed stop that A had moved (100500 -> 100700) must survive in the database."""
        alert = U.make_alert("A1", manage=TRAIL)
        frame = U.story(TRAIL_THEN_STOP)
        steady = H.make_book(db2.engine)
        H.open_trade(steady, copy.deepcopy(alert))
        expected = U.step_book(steady, frame)

        first = H.make_book(db.engine)
        H.open_trade(first, copy.deepcopy(alert))
        early = U.step_book(first, frame, now=H.at(10, 9))  # bars 10:06 .. 10:08 managed
        assert U.close_of(early) is None
        del first
        second = H.make_book(db.engine)  # the process restarted
        late = U.step_book(second, frame)
        assert U.canon(early + late, with_trade=True) == U.canon(expected, with_trade=True)
        row = U.row_of(db.engine)
        assert row["close_reason"] == "TRAIL_STOP"
        assert row["exit_paise"] == U.expected_stock_exit(100700, "long", "TRAIL_STOP") == 100665

    def test_a_restart_while_the_exit_bar_is_still_forming_still_exits_at_its_open(self, db):
        alert = U.make_alert("A1")
        frame = U.story(SIGNAL_ROWS)
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        book.step(H.at(10, 10, 30), H.Feed(SBIN=frame), SIGNAL)  # exit seen, bar 10:10 forming
        assert U.row_of(db.engine)["status"] == "OPEN"
        reborn = H.make_book(db.engine)
        reborn.step(H.at(10, 12), H.Feed(SBIN=frame), SIGNAL)
        assert U.row_of(db.engine)["close_reason"] == "SIGNAL_EXIT"

    def test_a_disabled_book_still_manages_the_trades_it_already_has(self, db):
        """`paper.enabled: false` stops NEW trades only (PaperCfg); an open one runs to its close."""
        alert = U.make_alert("A1")
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        U.step_book(book, U.story([Q]), H.at(10, 8))
        assert U.row_of(db.engine)["status"] == "OPEN"
        off = H.make_book(db.engine, enabled=False)
        U.step_book(off, U.story([Q, (100000, 100100, 99000, 99500)]), H.at(10, 9))
        assert U.row_of(db.engine)["close_reason"] == "STOP"


class TestEventTimesAreDataTimes:
    def test_the_row_and_every_event_carry_bar_or_exit_times_never_now(self, db):
        """CLOSED and the row's exit_ts carry the OPEN of the deciding bar, STOP_MOVED the bar
        CLOSE at which the new stop takes effect (Manager decision 4); never the wall time."""
        alert = U.make_alert("A1", manage=TRAIL)
        frame = U.story(TRAIL_THEN_STOP)
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        now = H.at(11, 47, 13)
        events = U.step_book(book, frame, now=now)
        opens = set(frame.index)
        closes = {ts + MIN for ts in frame.index}
        assert events
        for e in events:
            assert e.ts != now, f"{e.kind} stamped with now"
            if str(e.kind) == "CLOSED":
                assert e.ts in opens, f"CLOSED must carry its bar's OPEN time, got {e.ts}"
            elif str(e.kind) == "STOP_MOVED":
                assert e.ts in closes, f"STOP_MOVED must carry a bar CLOSE time, got {e.ts}"
            else:
                assert e.ts in opens | closes
        row = U.row_of(db.engine)
        assert row["exit_ts"] != now and row["exit_ts"] in opens
        for e in H.event_rows(db.engine):
            if str(H.val(e["kind"])) != "OPENED":
                assert e["ts"] != now and e["ts"] in opens | closes

    def test_a_forming_bar_is_not_used_and_the_closed_one_is(self, db):
        book = H.make_book(db.engine)
        U.opened(book, U.make_alert("A1"))
        frame = U.story([Q, (100000, 100100, 99000, 99500)])
        book.step(H.at(10, 7, 59), H.Feed(SBIN=frame))
        assert U.row_of(db.engine)["status"] == "OPEN"
        book.step(H.at(10, 8), H.Feed(SBIN=frame))
        assert U.row_of(db.engine)["status"] == "CLOSED"


class TestBookedEventsAreFinal:
    def stopped_book(self, db):
        book = H.make_book(db.engine)
        U.opened(book, U.make_alert("A1"))
        frame = U.story([(100050, 100100, 98900, 99500)])
        U.step_book(book, frame)
        return book, frame

    def test_the_stored_close_event_holds_the_bar_prices_it_used(self, db):
        self.stopped_book(db)
        (stored,) = U.stored_close_events(db.engine)
        for price in (100050, 100100, 98900, 99500):
            assert U.mentions(stored["detail"], price), f"{price} missing from {stored['detail']}"

    def test_a_feed_that_revises_the_closing_bar_cannot_change_the_row_or_the_events(self, db):
        book, _ = self.stopped_book(db)
        before = frozen(db.engine)
        revised = U.story([(100050, 100100, 99900, 100000), Q, Q])
        assert U.step_book(book, revised, now=H.at(10, 20)) == []
        assert frozen(db.engine) == before

    def test_a_bar_the_trade_already_saw_is_never_evaluated_again(self, db):
        book = H.make_book(db.engine)
        U.opened(book, U.make_alert("A1"))
        U.step_book(book, U.story([Q, Q]), now=H.at(10, 8))
        before = frozen(db.engine)
        revised = U.story([Q, (100000, 100100, 98000, 98500), Q])  # 10:07 rewritten with a dip
        events = U.step_book(book, revised, now=H.at(10, 9))
        assert U.row_of(db.engine)["status"] == "OPEN" and U.closes_of(events) == []
        assert before["trades"][0]["status"] == "OPEN"

    def test_a_restart_does_not_re_walk_bars_it_already_booked(self, db):
        book = H.make_book(db.engine)
        U.opened(book, U.make_alert("A1"))
        U.step_book(book, U.story([Q, Q]), now=H.at(10, 8))
        reborn = H.make_book(db.engine)
        revised = U.story([Q, (100000, 100100, 98000, 98500), Q])
        U.step_book(reborn, revised, now=H.at(10, 9))
        assert U.row_of(db.engine)["status"] == "OPEN"


class TestHostileAndAwkwardFrames:
    def test_a_nan_bar_never_wedges_the_trade_a_later_good_bar_still_closes_it(self, db):
        book = H.make_book(db.engine)
        U.opened(book, U.make_alert("A1"))
        nan = float("nan")
        frame = U.story([Q, Q, (100000, 100100, 99000, 99500)])
        frame.iloc[2] = [nan, nan, nan, nan, 0.0]  # 10:07 is garbage
        book.step(U.end_of(frame), H.Feed(SBIN=frame))  # never raises
        row = U.row_of(db.engine)
        assert (
            row["status"] == "CLOSED"
            and row["close_reason"] == "STOP"
            and row["exit_paise"] == U.expected_stock_exit(99000, "long", "STOP")
        )

    def test_the_callers_frame_is_never_modified(self, db):
        book = H.make_book(db.engine)
        U.opened(book, U.make_alert("A1", manage=TRAIL))
        frame = U.story(TRAIL_THEN_STOP)
        before = frame.copy(deep=True)
        U.step_book(book, frame)
        pd.testing.assert_frame_equal(frame, before)

    def test_no_bars_for_the_series_leaves_the_open_trade_alone(self, db):
        book = H.make_book(db.engine)
        U.opened(book, U.make_alert("A1"))
        U.step_book(book, U.story([Q]), now=H.at(10, 8))
        before = frozen(db.engine)
        assert book.step(H.at(10, 30), H.Feed(SBIN=None)) == []
        assert book.step(H.at(10, 30), H.Feed(SBIN=U.story([Q]).iloc[:0])) == []
        assert frozen(db.engine) == before

    def test_a_naive_now_is_rejected_not_guessed(self, db):
        book = H.make_book(db.engine)
        U.opened(book, U.make_alert("A1"))
        with pytest.raises(ValueError):
            book.step(dt.datetime(2026, 10, 29, 10, 30), H.Feed(SBIN=U.story([Q])))

    def test_two_trades_on_one_series_are_managed_independently(self, db):
        book = H.make_book(db.engine)
        U.opened(book, U.make_alert("L1"))
        U.opened(book, U.make_alert("S1", direction="short"))
        frame = U.story([Q, (100000, 100100, 99000, 99500)])  # stops the long, not the short
        book.step(U.end_of(frame), H.Feed(SBIN=frame))
        assert U.row_of(db.engine, "L1")["close_reason"] == "STOP"
        assert U.row_of(db.engine, "S1")["status"] == "OPEN", (
            "the dip stops the long, not the short"
        )


# ── AC7 at the book level ─────────────────────────────────────────────────────────────
def alert_for(case):
    alert = U.make_alert(
        "A1",
        direction=case.direction,
        created=case.start - dt.timedelta(seconds=57),
        entry=case.rows[0][0] / 100.0,
        stop=case.state_kw["stop"] / 100.0,
        manage={**case.rules_kw, "exit_time": "15:15"},
    )
    target = case.state_kw["target"]
    alert["economics"]["underlying_target"] = None if target is None else target / 100.0
    return alert


class TestTruncationThroughTheBook:
    @PROP
    @given(case=cases(), cut=st.integers(0, 29))
    def test_every_booked_event_up_to_t_survives_cutting_the_bars_after_t(self, case, cut):
        m = min(cut, len(case.rows) - 1)
        t = case.start + m * MIN
        alert = alert_for(case)
        end = case.start + len(case.rows) * MIN
        se = lambda trade, now: case.strategy_exit(now)  # noqa: E731

        def booked(rows):
            frame = U.pbars(case.start - MIN, [U.PRE_BAR, *rows])
            with H.temp_book() as (book, _engine):
                H.open_trade(book, copy.deepcopy(alert))
                return book.step(end, H.Feed(SBIN=frame), se)

        full = booked(case.rows)
        cut_events = booked(case.rows[: m + 1])
        assert U.canon(U.upto(full, t), with_trade=True) == U.canon(
            U.upto(cut_events, t), with_trade=True
        )


# ── AC8 ───────────────────────────────────────────────────────────────────────────────
class TestScale:
    def test_bars_is_called_once_per_series_even_with_waiting_and_open_trades_together(self, db):
        book = H.make_book(db.engine)
        U.opened(book, U.make_alert("OPEN1"))
        U.opened(book, U.make_alert("OPEN2"))
        U.step_book(book, U.story([Q, Q]), now=H.at(10, 8))
        U.opened(book, U.make_alert("WAIT1", created=H.at(10, 20, 3)))  # waits for 10:21
        U.opened(book, U.make_alert("OTHER", instrument_key="RELIANCE"))
        feed = H.Feed(SBIN=U.story([Q, Q, Q]), RELIANCE=U.story([Q]))
        book.step(H.at(10, 30), feed)
        assert sorted(feed.calls) == ["RELIANCE", "SBIN"], feed.calls

    def test_forty_five_open_trades_on_30000_row_frames_are_managed_inside_the_budget(self, db):
        specs = U.many_specs(9)
        stamps = U.history_stamps(80)
        assert len(stamps) == 30_000
        frame = H.bars_at(stamps, [1000.0] * len(stamps))
        series = [f"S{i}" for i in range(9)]
        book = H.make_book(db.engine, specs=specs)
        for i in range(45):
            U.opened(book, U.make_alert(f"P{i}", instrument_key=series[i % 9]))

        feed = H.Feed(**dict.fromkeys(series, frame))
        started = time.perf_counter()
        filled = book.step(H.at(12, 0), feed)
        fill_s = time.perf_counter() - started
        assert sorted(feed.calls) == sorted(series), "bars() once per series"
        assert len(filled) == 45 and {U.kinds([e])[0] for e in filled} == {"FILLED"}

        feed = H.Feed(**dict.fromkeys(series, frame))
        started = time.perf_counter()
        closed = book.step(H.at(15, 40), feed)
        close_s = time.perf_counter() - started
        assert sorted(feed.calls) == sorted(series), "bars() once per series"
        assert len(U.closes_of(closed)) == 45
        assert {U.close_of([e]).reason for e in U.closes_of(closed)} == {"SESSION_END"}

        feed = H.Feed(**dict.fromkeys(series, frame))
        started = time.perf_counter()
        again = book.step(H.at(15, 40), feed)
        quiet_s = time.perf_counter() - started
        assert again == [] and len(feed.calls) == len(set(feed.calls))
        rows = H.trade_rows(db.engine)
        assert {H.val(r["status"]) for r in rows} == {"CLOSED"} and len(rows) == 45
        # the 15:15 open 1000.00, slipped 0.03 % against the seller: 999.70
        assert {r["exit_paise"] for r in rows} == {
            U.expected_stock_exit(100000, "long", "SESSION_END")
        }
        assert {r["exit_paise"] for r in rows} == {99970}
        assert fill_s < 3.0 and close_s < 3.0 and quiet_s < 1.5, (fill_s, close_s, quiet_s)

    def test_a_step_with_nothing_new_does_not_rescan_the_history(self, db):
        """Per-trade cursor: with 45 trades still open and no new bar, a step is near free."""
        specs = U.many_specs(9)
        frame = H.bars_at(U.history_stamps(80), [1000.0] * 30_000)
        book = H.make_book(db.engine, specs=specs)
        for i in range(45):
            U.opened(book, U.make_alert(f"P{i}", instrument_key=f"S{i % 9}"))
        feed = H.Feed(**{f"S{i}": frame for i in range(9)})
        book.step(H.at(12, 0), feed)
        started = time.perf_counter()
        events = book.step(H.at(12, 0), H.Feed(**{f"S{i}": frame for i in range(9)}))
        elapsed = time.perf_counter() - started
        assert events == [] and elapsed < 1.5, elapsed

    def test_the_desk_never_imports_the_paper_books_manager(self):
        """Guard for AC8, narrowed when the desk adopted onelot/desk/rules.py (the paper test,
        2026-11-08: `pick_expiry`): the desk still never imports the paper book's manager."""
        tree = ast.parse(DESK_PY.read_text(encoding="utf-8"))
        mentioned = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mentioned.add((node.module or "").split(".")[-1])
                mentioned.update(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                mentioned.update(a.name.split(".")[-1] for a in node.names)
        assert "manage" not in mentioned and "manage_step" not in mentioned
