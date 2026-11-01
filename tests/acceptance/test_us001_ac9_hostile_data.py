"""US-001 AC9: hostile data and concurrency.

Given None or an empty DataFrame (as Desk._reset_bars leaves), a NaN open, duplicate or
unsorted bars, 1-minute and 2-minute spacing mixed in one frame, or tz-naive timestamps,
then no exception escapes step; mixed spacing grades the trade delayed; numpy integers are
stored as SQL integers (typeof entry_paise is integer); and the same alert id from two
threads yields one row without a leaked IntegrityError.

Where the contract leaves the choice open (what to do with an unusable bar), the tests accept
every defensible behaviour and reject the indefensible one: a price that never traded.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import threading

import numpy as np
import pandas as pd
import pytest
import us001_helpers as H
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

PROP = settings(
    max_examples=30,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=list(HealthCheck),
)
NAN = float("nan")
INF = float("inf")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


def stepped(db, frame, now=None, alert=None, **kw):
    book = H.make_book(db.engine, **kw)
    alert = alert or H.make_alert("A1")
    H.open_trade(book, alert)
    events = book.step(now or H.at(10, 10), H.Feed(**{alert["series_key"]: frame}))
    return book, H.trade_row(db.engine, alert["id"]), events


def assert_a_real_price_or_none(row, good_open, good_ts):
    """No fill on an unusable bar. Either the trade waits / closes NO_FILL with no price, or it
    fills on a later GOOD bar at that bar's own open."""
    if row["entry_paise"] is None:
        assert H.val(row["status"]) in ("PENDING_FILL", "NO_FILL")
        assert row["exit_paise"] is None
    else:
        assert H.val(row["status"]) == "OPEN"
        assert row["entry_paise"] == H.expected_stock_entry(good_open, "long")
        assert H.same_instant(row["fill_bar_ts"], good_ts)


class TestNoneAndEmptyFrames:
    @pytest.mark.parametrize(
        "frame",
        [
            None,
            pd.DataFrame(),
            pd.DataFrame(columns=["open", "high", "low", "close", "volume"]),
            pd.DataFrame(index=pd.DatetimeIndex([], tz="Asia/Kolkata")),
            H.standard_bars().drop(columns=["open"]),
            H.standard_bars().iloc[0:0],
        ],
        ids=["none", "bare", "columns-only", "empty-index", "no-open-column", "zero-rows"],
    )
    def test_no_exception_and_the_trade_keeps_waiting(self, db, frame):
        _, row, events = stepped(db, frame)
        assert events == []
        assert H.val(row["status"]) == "PENDING_FILL" and row["entry_paise"] is None

    def test_a_series_that_flips_between_empty_and_full_across_polls_still_fills_once(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1")
        for frame in (None, pd.DataFrame(), H.standard_bars(), None, pd.DataFrame()):
            book.step(H.at(10, 10), H.Feed(SBIN=frame))
        assert [H.val(e["kind"]) for e in H.event_rows(db.engine, "A1")] == ["OPENED", "FILLED"]
        assert H.trade_row(db.engine, "A1")["entry_paise"] == 100030

    @pytest.mark.parametrize("volume", ["missing", "nan", "zero"])
    def test_a_volumeless_series_such_as_an_index_still_fills(self, db, volume):
        frame = H.minute_bars(H.at(10, 5), [54000.0, 54000.0, 54001.0])
        if volume == "missing":
            frame = frame.drop(columns=["volume"])
        else:
            frame["volume"] = NAN if volume == "nan" else 0.0
        alert = H.make_alert("O1", kind="INDEX")
        _, row, _ = stepped(db, frame, alert=alert)
        assert H.val(row["status"]) == "OPEN" and row["entry_paise"] == 45280


class TestBadPrices:
    @pytest.mark.parametrize(
        "bad", [NAN, INF, -INF, 0.0, -5.0], ids=["nan", "inf", "-inf", "0", "-5"]
    )
    def test_an_unusable_open_on_the_first_eligible_bar_is_never_a_fill_price(self, db, bad):
        frame = H.minute_bars(H.at(10, 5), [998.0, 1000.0, 1002.0, 1003.0])
        frame.iloc[1, frame.columns.get_loc("open")] = bad  # only the open is bad (10:06)
        assert frame["high"].notna().all() and frame["close"].notna().all()
        _, row, _ = stepped(db, frame)
        assert_a_real_price_or_none(row, 1002.0, H.at(10, 7))

    def test_a_whole_bar_of_nan_is_not_a_fill(self, db):
        frame = H.minute_bars(H.at(10, 5), [998.0, 1000.0, 1002.0, 1003.0])
        frame.iloc[1] = NAN
        _, row, _ = stepped(db, frame)
        assert_a_real_price_or_none(row, 1002.0, H.at(10, 7))

    def test_a_frame_of_nothing_but_nan_leaves_the_trade_waiting(self, db):
        frame = H.minute_bars(H.at(10, 5), [NAN] * 6)
        _, row, events = stepped(db, frame)
        assert H.val(row["status"]) in ("PENDING_FILL", "NO_FILL") and row["entry_paise"] is None
        assert all(H.val(e.kind) != "FILLED" for e in events)

    def test_a_nan_in_an_irrelevant_bar_changes_nothing(self, db):
        frame = H.minute_bars(H.at(10, 5), [NAN, 1000.0, NAN, 1001.0])  # 10:05 and 10:07
        _, row, _ = stepped(db, frame)
        assert H.val(row["status"]) == "OPEN" and row["entry_paise"] == 100030


class TestDuplicateAndUnsortedBars:
    def test_a_duplicated_timestamp_fills_once_at_one_of_its_opens(self, db):
        stamps = [H.at(10, 5), H.at(10, 6), H.at(10, 6), H.at(10, 7)]
        frame = H.bars_at(stamps, [998.0, 1000.0, 1005.0, 1001.0])
        _, row, events = stepped(db, frame)
        assert [H.val(e.kind) for e in events] == ["FILLED"]
        assert H.same_instant(row["fill_bar_ts"], H.at(10, 6))
        assert row["entry_paise"] in (
            H.expected_stock_entry(1000.0, "long"),
            H.expected_stock_entry(1005.0, "long"),
        )
        assert H.count_rows(db.engine, "paper_events") == 2

    def test_unsorted_rows_give_the_same_fill_as_sorted_rows(self, db):
        opens = [998.0, 1000.0, 1001.0, 1002.0, 1003.0]
        frame = H.minute_bars(H.at(10, 5), opens)
        shuffled = frame.iloc[[3, 0, 4, 2, 1]]
        _, a, _ = stepped(db, frame, alert=H.make_alert("S1"))
        _, b, _ = stepped(db, shuffled, alert=H.make_alert("U1"))
        for col in ("status", "entry_paise", "fill_bar_ts", "entry_ts", "close_reason"):
            assert a[col] == b[col], col
        assert b["entry_paise"] == 100030

    def test_unsorted_and_duplicated_together_do_not_break_the_book(self, db):
        stamps = [H.at(10, 7), H.at(10, 5), H.at(10, 6), H.at(10, 7), H.at(10, 6)]
        frame = H.bars_at(stamps, [1001.0, 998.0, 1000.0, 1002.0, 1000.0])
        _, row, events = stepped(db, frame)
        assert [H.val(e.kind) for e in events] == ["FILLED"]
        assert H.same_instant(row["fill_bar_ts"], H.at(10, 6))

    @pytest.mark.parametrize("kind", ["sorted", "unsorted", "duplicated"])
    def test_step_never_changes_the_callers_frame(self, db, kind):
        stamps = [H.at(10, 5), H.at(10, 6), H.at(10, 7)]
        opens = [998.0, 1000.0, 1001.0]
        if kind == "unsorted":
            stamps, opens = stamps[::-1], opens[::-1]
        if kind == "duplicated":
            stamps, opens = [*stamps, H.at(10, 6)], [*opens, 1005.0]
        frame = H.bars_at(stamps, opens)
        frozen = frame.copy(deep=True)
        stepped(db, frame)
        pd.testing.assert_frame_equal(frame, frozen)
        assert frame.index.equals(frozen.index)

    @given(
        perm=st.permutations(range(8)),
        opens=st.lists(st.integers(99_500, 100_500), min_size=8, max_size=8),
        direction=st.sampled_from(["long", "short"]),
    )
    @PROP
    def test_property_row_order_never_changes_the_outcome(self, perm, opens, direction):
        frame = H.minute_bars(H.at(10, 3), [o / 100 for o in opens])  # 10:03 .. 10:10
        far = 10.0 if direction == "long" else 100_000.0
        alert = H.make_alert("P1", direction=direction, stop=far)
        outcomes = []
        for candidate in (frame, frame.iloc[list(perm)]):
            with H.temp_book() as (book, engine):
                H.open_trade(book, alert)
                book.step(H.at(10, 12), H.Feed(SBIN=candidate))
                row = H.trade_row(engine, "P1")
                outcomes.append(
                    (H.val(row["status"]), row["entry_paise"], row["fill_bar_ts"], row["entry_ts"])
                )
        assert outcomes[0] == outcomes[1]
        assert outcomes[0][0] == "OPEN"


class TestMixedSpacing:
    def mixed(self, start=None):
        one = H.minute_bars(start or H.at(10, 5), [998.0, 1000.0, 1001.0, 1002.0])  # 10:05 .. 10:08
        two = H.minute_bars(H.at(10, 10), [1003.0, 1004.0, 1005.0], step=2)  # 10:10, 10:12, 10:14
        return pd.concat([one, two])

    def test_one_and_two_minute_bars_in_one_frame_grade_the_trade_delayed(self, db):
        _, row, _ = stepped(db, self.mixed(), now=H.at(10, 20))
        assert H.val(row["grade"]) == "delayed"
        assert H.val(H.trade_row(db.engine, "A1")["grade"]) == "delayed"

    def test_the_mixed_frame_does_not_stop_the_book_doing_its_job(self, db):
        book, row, events = stepped(db, self.mixed(), now=H.at(10, 20))
        assert H.val(row["status"]) in ("OPEN", "NO_FILL", "PENDING_FILL")
        if H.val(row["status"]) == "OPEN":
            assert row["entry_paise"] == 100030 and H.same_instant(row["fill_bar_ts"], H.at(10, 6))
        assert book.step(H.at(10, 20), H.Feed(SBIN=self.mixed())) == []

    def test_the_downgrade_is_kept_for_a_trade_that_was_live_when_it_opened(self, db):
        book = H.make_book(db.engine)
        trade = H.open_trade(book, alert_id="A1", source="dhan")
        assert H.val(trade.grade) == "live"
        book.step(H.at(10, 20), H.Feed(SBIN=self.mixed()))
        assert H.val(H.trade_row(db.engine, "A1")["grade"]) == "delayed"
        assert [H.val(t.grade) for t in book.open_trades()] == ["delayed"]

    @pytest.mark.parametrize(
        "frame, source, resolution, grade",
        [
            (H.minute_bars(H.at(10, 5), [998.0, 1000.0, 1001.0]), "dhan", "1m", "live"),
            (H.minute_bars(H.at(10, 4), [998.0, 1000.0, 1001.0], step=2), "yahoo", "2m", "delayed"),
            (H.minute_bars(H.at(10, 4), [998.0, 1000.0, 1001.0], step=2), "dhan", "2m", "live"),
        ],
        ids=["dhan-1m", "yahoo-2m", "dhan-2m"],
    )
    def test_uniform_spacing_never_changes_the_grade(self, db, frame, source, resolution, grade):
        alert = H.make_alert("A1", source=source, resolution=resolution)
        _, row, _ = stepped(db, frame, now=H.at(10, 20), alert=alert)
        assert H.val(row["grade"]) == grade

    def test_a_frame_spanning_two_sessions_is_not_mixed_spacing(self, db):
        """Desk frames hold days of bars; the overnight hole is not a second resolution."""
        yesterday = H.minute_bars(H.at(15, 20, day=H.DAY - dt.timedelta(days=1)), [999.0] * 10)
        today = H.minute_bars(H.at(10, 0), [999.0] * 6 + [1000.0, 1001.0, 1002.0])
        _, row, _ = stepped(db, pd.concat([yesterday, today]), now=H.at(10, 20))
        assert H.val(row["grade"]) == "live"
        assert H.val(row["status"]) == "OPEN" and row["entry_paise"] == 100030


class TestNaiveTimestamps:
    def test_a_frame_indexed_by_naive_timestamps_is_ignored_without_an_exception(self, db):
        frame = H.standard_bars()
        frame.index = frame.index.tz_localize(None)
        assert frame.index.tz is None
        book, row, events = stepped(db, frame)
        assert events == []
        assert H.val(row["status"]) == "PENDING_FILL" and row["entry_paise"] is None
        # the rejected frame did not poison the trade: aware bars fill it afterwards
        events = book.step(H.at(10, 10), H.Feed(SBIN=H.standard_bars()))
        assert [H.val(e.kind) for e in events] == ["FILLED"]

    def test_a_naive_now_is_rejected_not_guessed(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1")
        try:
            events = book.step(H.at(10, 10).replace(tzinfo=None), H.Feed(SBIN=H.standard_bars()))
        except (ValueError, TypeError):
            events = []  # refusing is a valid way to reject
        assert events == []
        row = H.trade_row(db.engine, "A1")
        assert H.val(row["status"]) == "PENDING_FILL" and row["entry_paise"] is None

    def test_a_frame_mixing_naive_and_aware_stamps_does_not_escape_either(self, db):
        frame = H.standard_bars()
        frame.index = pd.Index([*frame.index[:2], frame.index[2].tz_localize(None)], dtype=object)
        _, row, _ = stepped(db, frame)
        assert row["entry_paise"] is None or row["entry_paise"] == 100030


class TestNumpyValuesAreStoredAsSqlIntegers:
    INT_COLS = ("entry_paise", "risk_paise", "qty", "resolution_min")

    @pytest.mark.parametrize("dtype", ["int64", "int32", "float32", "float64"])
    def test_whatever_numpy_dtype_the_bars_carry_the_columns_are_integer_typed(self, db, dtype):
        raw = np.array([998, 1000, 1001], dtype=dtype)
        frame = H.minute_bars(H.at(10, 5), raw.astype(float)).astype(
            {"open": dtype, "high": dtype, "low": dtype, "close": dtype}
        )
        assert frame["open"].dtype == np.dtype(dtype)
        _, row, events = stepped(db, frame)
        assert row["entry_paise"] == 100030
        assert H.typeof(db.engine, "entry_paise", "A1") == "integer"
        for col in self.INT_COLS:
            assert H.typeof(db.engine, col, "A1") in ("integer", "null"), col
        assert isinstance(row["entry_paise"], int) and not isinstance(row["entry_paise"], bool)
        json.dumps(events[0].detail)  # no numpy scalar leaked into the event payload

    def test_an_inexact_float32_open_is_rounded_to_paise_once(self, db):
        frame = H.minute_bars(H.at(10, 5), [998.0, 1000.1, 1001.0]).astype("float32")
        open_as_stored = float(frame["open"].iloc[1])  # 1000.0999755859375
        _, row, _ = stepped(db, frame)
        assert row["entry_paise"] == H.expected_stock_entry(open_as_stored, "long")
        assert H.typeof(db.engine, "entry_paise", "A1") == "integer"

    def test_numpy_index_options_columns_are_integers_too(self, db):
        frame = H.minute_bars(H.at(10, 5), [54000.0, 54000.0, 54001.0]).astype("float32")
        alert = H.make_alert("O1", kind="INDEX")
        _, row, _ = stepped(db, frame, alert=alert)
        assert row["entry_paise"] == 45280 and row["qty"] == H.LOT
        for col in ("entry_paise", "risk_paise", "qty"):
            assert H.typeof(db.engine, col, "O1") == "integer", col

    def test_a_no_fill_row_stores_integer_zero_costs_and_net(self, db):
        alert = H.make_alert("A1", stop=1005.0)
        _, row, _ = stepped(db, H.standard_bars(), alert=alert)
        assert H.val(row["status"]) == "NO_FILL"
        assert H.typeof(db.engine, "costs_paise", "A1") == "integer"
        assert H.typeof(db.engine, "net_paise", "A1") == "integer"
        assert H.typeof(db.engine, "entry_paise", "A1") == "null"
        assert H.typeof(db.engine, "exit_paise", "A1") == "null"


class TestConcurrency:
    @staticmethod
    def race(books, alerts, copies_per_book=3):
        errors: list[BaseException] = []
        results: dict[str, list] = {}
        lock = threading.Lock()
        for alert in alerts:
            barrier = threading.Barrier(len(books) * copies_per_book)

            def worker(book, alert=alert, barrier=barrier):
                try:
                    barrier.wait(timeout=30)
                    trade = book.on_alert(copy.deepcopy(alert), H.CREATED)
                    with lock:
                        results.setdefault(alert["id"], []).append(trade)
                except BaseException as exc:  # noqa: BLE001 - the test reports everything
                    with lock:
                        errors.append(exc)

            threads = [
                threading.Thread(target=worker, args=(book,))
                for book in books
                for _ in range(copies_per_book)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
        return errors, results

    def test_the_same_alert_id_from_many_threads_yields_one_row_and_no_error(self, db):
        book = H.make_book(db.engine)
        alerts = [H.make_alert(f"T{i}") for i in range(10)]
        errors, results = self.race([book], alerts, copies_per_book=6)
        assert errors == []
        for alert in alerts:
            trades = results[alert["id"]]
            assert len(trades) == 6 and all(t is not None for t in trades)
            assert len({t.id for t in trades}) == 1
        assert len(H.trade_rows(db.engine)) == 10
        assert H.count_rows(db.engine, "paper_events") == 10  # one OPENED each
        assert book.status()["opened"] == 10

    def test_two_books_on_one_database_race_without_leaking_an_integrity_error(self, db):
        """The in-process lock cannot help here: the UNIQUE index decides, and the loser must
        turn its IntegrityError into 'return the existing trade'."""
        books = [H.make_book(db.engine), H.make_book(db.engine)]
        alerts = [H.make_alert(f"D{i}") for i in range(10)]
        errors, results = self.race(books, alerts, copies_per_book=2)
        assert errors == [], [f"{type(e).__name__}: {e}" for e in errors]
        for alert in alerts:
            trades = results[alert["id"]]
            assert len(trades) == 4 and all(t is not None for t in trades)
            assert len({t.id for t in trades}) == 1
        assert len(H.trade_rows(db.engine)) == 10
        assert H.count_rows(db.engine, "paper_events") == 10

    def test_open_trades_and_status_can_be_read_while_the_book_writes(self, db):
        book = H.make_book(db.engine)
        stop = threading.Event()
        errors: list[BaseException] = []

        def reader():
            while not stop.is_set():
                try:
                    book.open_trades()
                    book.status()
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                    return

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for i in range(40):
                H.open_trade(book, alert_id=f"R{i}")
            book.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars()))
        finally:
            stop.set()
            thread.join(timeout=30)
        assert errors == []
        assert len(book.open_trades()) == 40
        assert book.status()["opened"] == 40 and book.status()["filled"] == 40
