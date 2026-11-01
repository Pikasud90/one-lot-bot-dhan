"""US-001 mode B, cycle 2: the behaviour the review-round-1 fixes added.

* the 15:30 cap: for an alert raised before the close, no bar that opens at or after 15:30
  ever fills it (a closing print, post-close bars, a delayed window or a long TTL cannot
  change that); an alert raised after the close (a special session) is bounded by its date;
* OPENED says who booked the trade and when: `detail.opened_by` (on_alert | reconcile) and
  `detail.booked_at` (wall time, IST); the event's ts stays the alert's created_at;
* the retry set: an on_alert that failed at run time (a locked or broken database) is kept as
  a deep copy (bounded: 500, the oldest dropped with a WARNING) and booked by the next step,
  oldest first, at the alert's own time, exactly once, even against a concurrent reconcile or a
  second book;
* `retire_untracked(series_keys, now)`: a waiting trade on a series the desk does not track
  gets no bars, so it closes NO_FILL ("series_untracked", wall clock) once past its deadline
  (+ the feed delay if delayed); the Desk tells the book after construction and after reload;
* paper_events.trade_id is a real foreign key; a PRICING_UNAVAILABLE insert is guarded like
  every other write (a trade another book already moved is not touched).
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import threading
import time

import pytest
import us001_helpers as H
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from onelot.clock import IST
from onelot.config import AppConfig

TIMEOUT = 0.4  # busy timeout of the short-timeout engines (production: 30 s)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


@pytest.fixture
def desk_db(tmp_path):
    d = H.new_db(tmp_path, "desk.db", make_global=True)
    yield d
    H.dispose_db(d, was_global=True)


def opened_event(engine, alert_id):
    (event,) = [e for e in H.event_rows(engine, alert_id) if H.val(e["kind"]) == "OPENED"]
    return event


def fail_on_alert(book, engine, alerts, now=H.CREATED):
    """on_alert for each alert while every INSERT into paper_trades fails (a runtime failure)."""
    with H.failing_sql(engine, "paper_trades"):
        for alert in alerts:
            with pytest.raises(RuntimeError):
                book.on_alert(copy.deepcopy(alert), now)


# ── the 15:30 cap ─────────────────────────────────────────────────────────────────────
def stamps(*hhmm, day=H.DAY):
    return [H.at(h, m, day=day) for h, m in hhmm]


CAP_CASES = [
    # (id, resolution, created, source, ttl, bar opens, now, outcome, fill bar); outcome FILLED
    # means the entry columns are set (the same step may already have managed it to a close)
    ("1m-fills-the-last-session-bar", 1, H.at(15, 28, 30), "dhan", 10,
     stamps((15, 28), (15, 29), (15, 30), (15, 31)), H.at(15, 35), "FILLED", H.at(15, 29)),
    ("1m-hole-at-1529-never-fills-the-1530-print", 1, H.at(15, 28, 30), "dhan", 10,
     stamps((15, 28), (15, 30), (15, 31)), H.at(15, 35), "NO_FILL", None),
    ("1m-1529-alert-and-post-close-bars", 1, H.at(15, 29, 3), "dhan", 10,
     stamps((15, 29), (15, 30), (15, 31), (15, 32)), H.at(15, 40), "NO_FILL", None),
    ("1m-alert-in-the-last-microsecond", 1, H.at(15, 29, 59, 999999), "dhan", 10,
     stamps((15, 29), (15, 30), (15, 31)), H.at(15, 40), "NO_FILL", None),
    ("2m-fills-the-last-session-bar", 2, H.at(15, 27, 3), "dhan", 10,
     stamps((15, 26), (15, 28), (15, 30)), H.at(15, 40), "FILLED", H.at(15, 28)),
    ("2m-1528-alert-and-post-close-bars", 2, H.at(15, 28, 3), "dhan", 10,
     stamps((15, 28), (15, 30), (15, 32)), H.at(15, 40), "NO_FILL", None),
    ("delayed-window-cannot-reach-past-the-close", 2, H.at(15, 28, 3), "yahoo", 10,
     stamps((15, 28), (15, 30), (15, 32)), H.at(15, 50), "NO_FILL", None),
    ("long-ttl-cannot-reach-past-the-close", 1, H.at(15, 29, 3), "dhan", 120,
     stamps((15, 29), (15, 30), (15, 31)), H.at(16, 0), "NO_FILL", None),
]  # fmt: skip


class TestTheCloseCap:
    @pytest.mark.parametrize(
        "res, created, source, ttl, opens, now, status, fill_at",
        [c[1:] for c in CAP_CASES],
        ids=[c[0] for c in CAP_CASES],
    )
    def test_a_bar_opening_at_or_after_1530_never_fills_an_alert_raised_before_the_close(
        self, db, res, created, source, ttl, opens, now, status, fill_at
    ):
        alert = H.make_alert(
            "A1", created=created, source=source, ttl_min=ttl, resolution=f"{res}m"
        )
        # this class tests the 15:30 cap: the strategy's exit_time is moved to the close so that
        # it cannot be what refuses the late bars (a first bar at or after exit_time 15:15 is
        # NO_FILL 'session_end', US-002 review 1 MINOR-2; see TestTheExitTimeCap below)
        alert["manage"]["exit_time"] = "15:30"
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        book.step(now, H.Feed(SBIN=H.bars_at(opens, [1000.0 + i for i in range(len(opens))])))
        row = H.trade_row(db.engine, "A1")
        if status == "FILLED":  # the last session bar: filled, then SESSION_END closes it at once
            H.assert_filled(row, at_ts=fill_at)
            assert H.kinds_of(db.engine, "A1").count("FILLED") == 1
            return
        assert H.val(row["status"]) == status, row
        assert row["entry_paise"] is None and row["fill_bar_ts"] is None
        (event,) = [e for e in H.event_rows(db.engine, "A1") if H.val(e["kind"]) == "NO_FILL"]
        assert "session_closed" in H.blob(event["detail"])

    def test_an_alert_raised_after_the_close_is_bounded_by_its_date_not_by_1530(self, db):
        """A special session (evening Muhurat): the alert is not 'before the close', so the cap
        does not apply and its first later bar of the same date fills."""
        created = H.at(18, 20, 3)
        alert = H.make_alert("A1", created=created)
        alert["manage"]["exit_time"] = "23:00"  # the special session's own square-off
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        frame = H.minute_bars(H.at(18, 19), [1000.0, 1001.0, 1002.0])
        book.step(H.at(18, 30), H.Feed(SBIN=frame))
        row = H.trade_row(db.engine, "A1")
        H.assert_filled(row, at_ts=H.at(18, 21))  # (closed on the spot: the session is over)

    def test_the_same_late_alert_with_the_default_exit_time_is_no_fill_session_end(self, db):
        """With the desk's default exit_time (15:15) an evening alert has no bar before it."""
        alert = H.make_alert("A1", created=H.at(18, 20, 3))
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        frame = H.minute_bars(H.at(18, 19), [1000.0, 1001.0, 1002.0])
        events = book.step(H.at(18, 30), H.Feed(SBIN=frame))
        row = H.trade_row(db.engine, "A1")
        assert H.val(row["status"]) == "NO_FILL" and row["entry_paise"] is None
        assert [H.val(e.kind) for e in events] == ["NO_FILL"]
        assert "session_end" in H.blob(events[0].detail)

    def test_the_last_session_bars_still_fill_when_the_frame_also_holds_post_close_bars(self, db):
        frame = H.minute_bars(H.at(15, 20), [1000.0] * 20)  # 15:20 .. 15:39
        book = H.make_book(db.engine)
        alert = H.make_alert("A1", created=H.at(15, 27, 3))
        alert["manage"]["exit_time"] = "15:30"  # the 15:30 cap is under test, not exit_time
        H.open_trade(book, alert)
        events = book.step(H.at(15, 45), H.Feed(SBIN=frame))
        assert len(H.fills(events)) == 1  # the step may also close it: session over (US-002)
        H.assert_filled(H.trade_row(db.engine, "A1"), at_ts=H.at(15, 28))


class TestTheExitTimeCap:
    """US-002 review 1 MINOR-2: the Lab never fills at or after its square-off minute, so a first
    eligible bar opening at or after the strategy's exit_time (manage.exit_time, default 15:15)
    on the alert's date is NO_FILL, reason 'session_end', stamped with that bar's open."""

    @pytest.mark.parametrize(
        "exit_time, created, first_open, fills",
        [
            ("15:15", H.at(15, 10, 3), H.at(15, 11), True),
            ("15:15", H.at(15, 14, 3), H.at(15, 15), False),  # opens AT exit_time
            ("15:15", H.at(15, 14, 59), H.at(15, 15), False),
            ("15:15", H.at(15, 13, 3), H.at(15, 14), True),  # the last bar before it
            ("14:45", H.at(14, 40, 3), H.at(14, 41), True),
            ("14:45", H.at(14, 44, 3), H.at(14, 45), False),  # a custom exit_time is honoured
        ],
    )
    def test_a_first_bar_at_or_after_exit_time_never_fills(
        self, db, exit_time, created, first_open, fills
    ):
        alert = H.make_alert("A1", created=created)
        alert["manage"]["exit_time"] = exit_time
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        frame = H.minute_bars(first_open - dt.timedelta(minutes=1), [1000.0] * 6)
        events = book.step(first_open + dt.timedelta(minutes=10), H.Feed(SBIN=frame))
        row = H.trade_row(db.engine, "A1")
        if fills:
            H.assert_filled(row, at_ts=first_open)
            return
        assert (H.val(row["status"]), H.val(row["close_reason"])) == ("NO_FILL", "NO_FILL")
        assert row["entry_paise"] is None and row["costs_paise"] == 0 == row["net_paise"]
        assert [H.val(e.kind) for e in events] == ["NO_FILL"]
        assert "session_end" in H.blob(events[0].detail)
        assert H.same_instant(events[0].ts, first_open)

    def test_only_the_FIRST_eligible_bar_decides(self, db):
        """A bar before exit_time that is not eligible does not rescue the trade: the first
        eligible bar (after created_at) is at exit_time."""
        alert = H.make_alert("A1", created=H.at(15, 14, 30))
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        frame = H.minute_bars(H.at(15, 13), [1000.0] * 5)  # 15:13 .. 15:17
        book.step(H.at(15, 25), H.Feed(SBIN=frame))
        assert H.val(H.trade_row(db.engine, "A1")["status"]) == "NO_FILL"

    def test_an_alert_without_a_manage_block_uses_the_desks_default_exit_time(self, db):
        alert = H.make_alert("A1", created=H.at(15, 14, 3))
        del alert["manage"]
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        book.step(H.at(15, 25), H.Feed(SBIN=H.minute_bars(H.at(15, 14), [1000.0] * 6)))
        assert H.val(H.trade_row(db.engine, "A1")["status"]) == "NO_FILL"

    def test_an_index_option_is_judged_the_same_way(self, db):
        alert = H.make_alert("O1", kind="INDEX", created=H.at(15, 14, 3))
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        events = book.step(
            H.at(15, 25), H.Feed(BANKNIFTY=H.minute_bars(H.at(15, 14), [54000.0] * 6))
        )
        assert H.val(H.trade_row(db.engine, "O1")["status"]) == "NO_FILL"
        assert "session_end" in H.blob(events[0].detail)


# ── opened_by and booked_at ───────────────────────────────────────────────────────────
class TestOpenedSaysWhoBookedAndWhen:
    def test_an_alert_booked_by_the_hook_says_on_alert_and_the_wall_time_of_the_call(self, db):
        book = H.make_book(db.engine)
        call_time = H.at(10, 5, 4, 500000)
        book.on_alert(H.make_alert("A1"), call_time)
        event = opened_event(db.engine, "A1")
        assert event["detail"]["opened_by"] == "on_alert"
        booked = dt.datetime.fromisoformat(event["detail"]["booked_at"])
        assert booked.tzinfo is not None and booked == call_time
        assert booked.utcoffset() == dt.timedelta(hours=5, minutes=30)  # IST
        assert H.same_instant(event["ts"], H.CREATED)  # the event stays at the alert's own time

    def test_a_reconciled_trade_says_reconcile_and_is_visibly_late(self, db):
        H.insert_desk_signal(db.engine, H.make_alert("R1"))
        book = H.make_book(db.engine, clock=lambda: H.at(10, 47, 30))
        event = opened_event(db.engine, "R1")
        assert event["detail"]["opened_by"] == "reconcile"
        booked = dt.datetime.fromisoformat(event["detail"]["booked_at"])
        assert booked == H.at(10, 47, 30) and booked > H.CREATED + dt.timedelta(minutes=40)
        assert H.same_instant(event["ts"], H.CREATED)
        assert book.status()["opened"] == 1

    def test_a_retried_alert_says_on_alert_booked_at_the_retry_time(self, db):
        book = H.make_book(db.engine)
        fail_on_alert(book, db.engine, [H.make_alert("A1")])
        book.step(H.at(10, 8), H.Feed(SBIN=None))
        event = opened_event(db.engine, "A1")
        assert event["detail"]["opened_by"] == "on_alert"
        assert dt.datetime.fromisoformat(event["detail"]["booked_at"]) == H.at(10, 8)
        assert H.same_instant(event["ts"], H.CREATED)
        assert H.same_instant(H.trade_row(db.engine, "A1")["created_at"], H.CREATED)

    def test_every_booking_path_through_the_real_desk_is_labelled(self, desk_db):
        cfg = AppConfig.load()
        book = H.desk_book(cfg, desk_db.engine)
        d, _ = H.new_desk(cfg, desk_db, paper=book)
        d.poll()
        now = H.mid_session(d, "SBIN")
        alert = H.raise_alert(d, "SBIN", now=now)
        event = opened_event(desk_db.engine, alert["id"])
        assert event["detail"]["opened_by"] == "on_alert"
        assert dt.datetime.fromisoformat(event["detail"]["booked_at"]) == now

    def test_the_label_does_not_change_what_the_trade_is(self, db):
        book = H.make_book(db.engine)
        H.insert_desk_signal(db.engine, H.make_alert("R1"))
        reconciled = H.make_book(db.engine, clock=lambda: H.at(10, 30))
        direct = H.open_trade(book, alert_id="D1")
        (r1,) = [t for t in reconciled.open_trades() if t.alert_id == "R1"]
        for field in ("qty", "grade", "product", "created_at", "expires_at", "side"):
            assert getattr(r1, field) == getattr(direct, field), field


# ── the retry set ─────────────────────────────────────────────────────────────────────
class TestAFailedOnAlertIsRetriedByTheNextStep:
    def test_the_failure_is_raised_to_the_caller_and_the_next_step_books_and_fills_it(self, db):
        book = H.make_book(db.engine)
        alert = H.make_alert("A1")
        fail_on_alert(book, db.engine, [alert])
        assert H.count_rows(db.engine, "paper_trades") == 0 and book.open_trades() == []
        events = book.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars()))
        row = H.trade_row(db.engine, "A1")
        assert [H.val(e.kind) for e in events] == ["FILLED"]
        assert H.val(row["status"]) == "OPEN" and row["entry_paise"] == 100030
        assert H.same_instant(row["created_at"], H.CREATED)  # the alert's time, not the step's
        assert H.same_instant(row["fill_bar_ts"], H.at(10, 6))
        assert [H.val(e["kind"]) for e in H.event_rows(db.engine, "A1")] == ["OPENED", "FILLED"]
        assert book.status()["opened"] == 1 and book.status()["hook_errors"] == 0

    def test_the_retry_does_not_need_any_bars(self, db):
        book = H.make_book(db.engine)
        fail_on_alert(book, db.engine, [H.make_alert("A1")])
        assert book.step(H.at(10, 8), H.Feed(SBIN=None)) == []
        assert H.val(H.trade_row(db.engine, "A1")["status"]) == "PENDING_FILL"
        assert [t.alert_id for t in book.open_trades()] == ["A1"]

    def test_the_book_kept_its_own_copy_of_the_failed_alert(self, db):
        """The caller's very dict is handed in (no copy made by the test) and changed afterwards."""
        book = H.make_book(db.engine)
        alert = H.make_alert("A1", entry=1000.0, stop=990.0)
        with H.failing_sql(db.engine, "paper_trades"), pytest.raises(RuntimeError):
            book.on_alert(alert, H.CREATED)
        alert["economics"]["underlying_stop"] = alert["economics"]["stop_price"] = 1100.0
        alert["economics"]["option"]["lot"] = 99
        alert["instrument_key"] = alert["series_key"] = "RELIANCE"
        alert["strategy"] = "mutated"
        alert["data"]["source"] = "yahoo"
        book.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars((995.0, 995.0, 996.0))))
        row = H.trade_row(db.engine, "A1")
        assert H.val(row["status"]) == "OPEN"  # against the ORIGINAL stop of 990
        assert row["instrument_key"] == "SBIN" and row["strategy"] == "ema_cross"
        assert H.val(row["grade"]) == "live" and row["data_source"] == "dhan"

    def test_a_retried_alert_is_booked_once_and_never_retried_again(self, db):
        book = H.make_book(db.engine)
        fail_on_alert(book, db.engine, [H.make_alert("A1")])
        feed = H.Feed(SBIN=H.standard_bars())
        book.step(H.at(10, 8), feed)
        for later in (H.at(10, 9), H.at(10, 20), H.at(11, 0)):
            assert book.step(later, feed) == []
        assert [H.val(e["kind"]) for e in H.event_rows(db.engine, "A1")] == ["OPENED", "FILLED"]
        assert len(H.trade_rows(db.engine)) == 1 and book.status()["opened"] == 1

    def test_a_later_successful_on_alert_for_the_same_alert_clears_it_from_the_retry_set(self, db):
        book = H.make_book(db.engine)
        alert = H.make_alert("A1")
        fail_on_alert(book, db.engine, [alert])
        assert H.open_trade(book, alert) is not None  # the database recovered; the desk re-raised
        events = book.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars()))
        assert [H.val(e.kind) for e in events] == ["FILLED"]
        assert [H.val(e["kind"]) for e in H.event_rows(db.engine, "A1")] == ["OPENED", "FILLED"]
        opened = opened_event(db.engine, "A1")
        assert dt.datetime.fromisoformat(opened["detail"]["booked_at"]) == H.CREATED  # the hook's

    def test_the_retries_are_booked_oldest_failure_first(self, db):
        book = H.make_book(db.engine)
        order = ["X3", "X1", "X2", "X0"]
        fail_on_alert(book, db.engine, [H.make_alert(i) for i in order])
        book.step(H.at(10, 8), H.Feed(SBIN=None))
        assert [r["alert_id"] for r in H.trade_rows(db.engine)] == order  # ids ascend with booking

    def test_an_alert_the_book_can_never_record_is_refused_and_not_kept(self, db):
        book = H.make_book(db.engine)
        bad = H.make_alert("BAD")
        bad["kind"] = "FUTURES"
        with pytest.raises(ValueError):
            book.on_alert(bad, H.CREATED)
        assert book.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars())) == []
        assert H.count_rows(db.engine, "paper_trades") == 0
        assert book.status()["hook_errors"] == 0

    def test_a_disabled_book_keeps_and_retries_nothing(self, db):
        book = H.make_book(db.engine, enabled=False)
        assert book.on_alert(H.make_alert("A1"), H.CREATED) is None
        book.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars()))
        assert H.count_rows(db.engine, "paper_trades") == 0

    def test_retried_alerts_of_every_kind_are_booked_in_their_failure_order(self, db):
        book = H.make_book(db.engine)
        alerts = [
            H.make_alert("S1"),
            H.make_alert("O1", kind="INDEX"),
            H.make_alert("S2", instrument_key="RELIANCE"),
        ]
        fail_on_alert(book, db.engine, alerts)
        book.step(H.at(10, 8), H.Feed(SBIN=None))
        assert [r["alert_id"] for r in H.trade_rows(db.engine)] == ["S1", "O1", "S2"]


class TestTheRetrySetIsBounded:
    def test_beyond_500_the_oldest_are_dropped_with_a_warning_and_a_restart_still_books_them(
        self, db, caplog
    ):
        book = H.make_book(db.engine, clock=lambda: H.at(10, 30))
        alerts = [H.make_alert(f"C{i:03d}") for i in range(505)]
        for alert in alerts:
            H.insert_desk_signal(db.engine, alert)  # the desk stored them all
        with caplog.at_level(logging.WARNING):
            fail_on_alert(book, db.engine, alerts)
        dropped = [r.getMessage() for r in caplog.records if "retry set full" in r.getMessage()]
        assert len(dropped) == 5
        for i in range(5):
            assert any(f"C{i:03d}" in m for m in dropped), f"C{i:03d} was not named as dropped"
        book.step(H.at(10, 8), H.Feed(SBIN=None))
        booked = [r["alert_id"] for r in H.trade_rows(db.engine)]
        assert booked == [f"C{i:03d}" for i in range(5, 505)]  # the newest 500, oldest first
        assert book.status()["opened"] == 500
        # the five that were dropped are not lost: the next start reconciles them
        restarted = H.make_book(db.engine, clock=lambda: H.at(10, 30))
        assert restarted.status()["opened"] == 5
        assert len(H.trade_rows(db.engine)) == 505
        for i in range(5):
            assert opened_event(db.engine, f"C{i:03d}")["detail"]["opened_by"] == "reconcile"

    def test_exactly_500_failures_lose_nothing(self, db, caplog):
        book = H.make_book(db.engine)
        with caplog.at_level(logging.WARNING):
            fail_on_alert(book, db.engine, [H.make_alert(f"E{i:03d}") for i in range(500)])
        assert not [r for r in caplog.records if "retry set full" in r.getMessage()]
        book.step(H.at(10, 8), H.Feed(SBIN=None))
        assert len(H.trade_rows(db.engine)) == 500

    def test_a_repeated_failure_of_the_same_alert_takes_one_place(self, db):
        book = H.make_book(db.engine)
        alert = H.make_alert("A1")
        fail_on_alert(book, db.engine, [alert] * 3)
        book.step(H.at(10, 8), H.Feed(SBIN=None))
        assert [r["alert_id"] for r in H.trade_rows(db.engine)] == ["A1"]
        assert book.status()["opened"] == 1


class TestARetryNeverDoubleBooks:
    def test_concurrent_retry_reconcile_and_a_repeated_on_alert_book_each_alert_once(self, db):
        errors: list[BaseException] = []
        opened_total = 0
        for round_no in range(5):
            book1 = H.make_book(db.engine, clock=lambda: H.at(10, 30))
            alerts = [H.make_alert(f"K{round_no}-{i:02d}") for i in range(12)]
            for alert in alerts:
                H.insert_desk_signal(db.engine, alert)  # reconcile can see them
            fail_on_alert(book1, db.engine, alerts)  # ... and book1 holds them for retry
            holder: dict[str, object] = {}
            barrier = threading.Barrier(3)

            def guarded(fn, barrier):
                def run():
                    try:
                        barrier.wait(timeout=30)
                        fn()
                    except BaseException as exc:  # noqa: BLE001 - reported below
                        errors.append(exc)

                return run

            def retry(book1=book1):
                book1.step(H.at(10, 8), H.Feed(SBIN=None))

            def restart(holder=holder):
                holder["book2"] = H.make_book(db.engine, clock=lambda: H.at(10, 30))

            def again(book1=book1, alerts=alerts):
                for alert in alerts:
                    book1.on_alert(copy.deepcopy(alert), H.CREATED)

            threads = [
                threading.Thread(target=guarded(f, barrier)) for f in (retry, restart, again)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
            assert errors == [], [f"{type(e).__name__}: {e}" for e in errors]
            ids = {a["id"] for a in alerts}
            rows = [r for r in H.trade_rows(db.engine) if r["alert_id"] in ids]
            assert sorted(r["alert_id"] for r in rows) == sorted(ids)
            for alert_id in ids:
                kinds = [H.val(e["kind"]) for e in H.event_rows(db.engine, alert_id)]
                assert kinds.count("OPENED") == 1, (alert_id, kinds)
                assert opened_event(db.engine, alert_id)["detail"]["opened_by"] in (
                    "on_alert",
                    "reconcile",
                )
            booked_here = book1.status()["opened"] + holder["book2"].status()["opened"]  # type: ignore[attr-defined]
            assert booked_here == 12, "an alert was counted as opened by two books"
            opened_total += booked_here
        assert opened_total == 60

    def test_two_books_retrying_the_same_failed_alerts_at_once_book_them_once(self, db):
        book1, book2 = H.make_book(db.engine), H.make_book(db.engine)
        alerts = [H.make_alert(f"T{i:02d}") for i in range(10)]
        fail_on_alert(book1, db.engine, alerts)
        fail_on_alert(book2, db.engine, alerts)
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def go(book):
            try:
                barrier.wait(timeout=30)
                book.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars()))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=go, args=(b,)) for b in (book1, book2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert errors == [], [f"{type(e).__name__}: {e}" for e in errors]
        assert len(H.trade_rows(db.engine)) == 10
        assert H.count_rows(db.engine, "paper_events") == 10 + 10  # one OPENED, one FILLED each
        assert book1.status()["opened"] + book2.status()["opened"] == 10
        assert book1.status()["filled"] + book2.status()["filled"] == 10
        assert {H.val(r["status"]) for r in H.trade_rows(db.engine)} == {"OPEN"}


class TestARetryUnderALockedDatabase:
    @pytest.fixture
    def env(self, tmp_path):
        db = H.new_db(tmp_path, "busy.db")
        short = H.short_timeout_engine(tmp_path / "busy.db", TIMEOUT)
        try:
            yield tmp_path / "busy.db", db, short
        finally:
            short.dispose()
            db.dispose()

    def test_an_on_alert_that_hit_a_held_lock_is_booked_by_a_step_after_the_lock_clears(self, env):
        path, db, short = env
        book = H.make_book(short)
        with H.write_lock(path), pytest.raises(Exception, match="locked"):
            book.on_alert(H.make_alert("A1"), H.CREATED)
        assert H.count_rows(db.engine, "paper_trades") == 0
        events = book.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars()))
        assert [H.val(e.kind) for e in events] == ["FILLED"]
        assert [H.val(e["kind"]) for e in H.event_rows(db.engine, "A1")] == ["OPENED", "FILLED"]

    def test_a_step_under_a_still_held_lock_stops_at_the_first_failure_and_keeps_them_all(
        self, env, caplog
    ):
        path, db, short = env
        book = H.make_book(short)
        with H.write_lock(path):
            for i in range(4):
                with pytest.raises(Exception, match="locked"):
                    book.on_alert(H.make_alert(f"A{i}"), H.CREATED)
        with H.write_lock(path), caplog.at_level(logging.ERROR):
            started = time.perf_counter()
            events = book.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars()))
            elapsed = time.perf_counter() - started
        assert events == [] and H.count_rows(db.engine, "paper_trades") == 0
        assert elapsed < 1.5 * TIMEOUT + 1.0, f"retrying 4 alerts took {elapsed:.2f}s on one lock"
        assert book.status()["hook_errors"] == 1
        locked = [
            r
            for r in caplog.records
            if r.levelno >= logging.ERROR and "retrying alert" in r.getMessage()
        ]
        assert len(locked) == 1, [r.getMessage() for r in locked]
        events = book.step(H.at(10, 9), H.Feed(SBIN=H.standard_bars()))
        assert [r["alert_id"] for r in H.trade_rows(db.engine)] == ["A0", "A1", "A2", "A3"]
        assert [H.val(e.kind) for e in events] == ["FILLED"] * 4


# ── retire_untracked ──────────────────────────────────────────────────────────────────
def waiting_on_reliance(book, alert_id="R1", **kw):
    return H.open_trade(book, alert_id=alert_id, instrument_key="RELIANCE", **kw)


class TestRetireUntracked:
    @pytest.mark.parametrize(
        "source, now, retired",
        [
            ("dhan", H.at(10, 15, 3), False),  # expires_at 10:15:03 itself: not past it
            ("dhan", H.at(10, 15, 3, 1), True),
            ("dhan", H.at(10, 10), False),
            ("yahoo", H.at(10, 15, 4), False),  # delayed: + the 15-minute feed delay
            ("yahoo", H.at(10, 30, 3), False),
            ("yahoo", H.at(10, 30, 3, 1), True),
        ],
    )
    def test_a_waiting_trade_on_an_untracked_series_retires_once_past_its_deadline(
        self, db, source, now, retired
    ):
        book = H.make_book(db.engine)
        waiting_on_reliance(book, source=source)
        events = book.retire_untracked({"SBIN"}, now)
        row = H.trade_row(db.engine, "R1")
        assert (H.val(row["status"]) == "NO_FILL") is retired
        assert [H.val(e.kind) for e in events] == (["NO_FILL"] if retired else [])

    def test_the_retirement_is_a_no_fill_that_says_why_and_that_the_clock_was_the_wall(self, db):
        book = H.make_book(db.engine)
        waiting_on_reliance(book)
        now = H.at(10, 40, 7)
        (event,) = book.retire_untracked({"SBIN"}, now)
        row = H.trade_row(db.engine, "R1")
        assert (H.val(row["status"]), row["close_reason"]) == ("NO_FILL", "NO_FILL")
        assert row["entry_paise"] is None and row["exit_paise"] is None
        assert row["costs_paise"] == 0 and row["net_paise"] == 0 and row["gross_paise"] == 0
        assert H.val(event.kind) == "NO_FILL" and event.trade_id == row["id"]
        assert event.ts == now  # the wall time: there are no bars, so no data time
        assert event.detail["reason"] == "series_untracked"
        assert event.detail["series_key"] == "RELIANCE" and event.detail["clock"] == "wall"
        assert dt.datetime.fromisoformat(event.detail["deadline"]) == H.EXPIRES
        stored = [e for e in H.event_rows(db.engine, "R1") if H.val(e["kind"]) == "NO_FILL"]
        assert len(stored) == 1 and stored[0]["detail"]["reason"] == "series_untracked"
        assert book.open_trades() == []
        status = book.status()
        assert (status["no_fill"], status["filled"], status["closed"]) == (1, 0, 0)

    def test_the_deadline_of_a_delayed_trade_includes_the_configured_feed_delay(self, db):
        for delay, now, retired in (
            (0, H.at(10, 15, 4), True),
            (5, H.at(10, 20, 3), False),
            (5, H.at(10, 20, 4), True),
        ):
            book = H.make_book(db.engine, feed_delay=delay)
            alert_id = f"D{delay}-{now.minute}-{now.second}"
            waiting_on_reliance(book, alert_id=alert_id, source="yahoo")
            book.retire_untracked({"SBIN"}, now)
            assert (H.val(H.trade_row(db.engine, alert_id)["status"]) == "NO_FILL") is retired

    def test_retiring_twice_does_nothing_more(self, db):
        book = H.make_book(db.engine)
        waiting_on_reliance(book)
        assert len(book.retire_untracked({"SBIN"}, H.at(11, 0))) == 1
        before = H.snapshot_tables(db.engine)
        assert book.retire_untracked({"SBIN"}, H.at(11, 5)) == []
        assert book.step(H.at(11, 10), H.Feed(SBIN=None)) == []
        assert H.snapshot_tables(db.engine) == before

    def test_a_tracked_series_is_never_retired_however_overdue(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="S1", instrument_key="SBIN")
        assert book.retire_untracked({"SBIN", "RELIANCE"}, H.at(15, 0)) == []
        assert H.val(H.trade_row(db.engine, "S1")["status"]) == "PENDING_FILL"
        # data time still rules: with no later bar it keeps waiting at 16:00 too
        assert book.step(H.at(16, 0), H.Feed(SBIN=H.minute_bars(H.at(10, 0), [1000.0] * 6))) == []

    def test_an_open_trade_on_an_untracked_series_is_left_alone(self, db):
        book = H.make_book(db.engine)
        waiting_on_reliance(book)
        book.step(H.at(10, 8), H.Feed(RELIANCE=H.standard_bars()))
        assert H.val(H.trade_row(db.engine, "R1")["status"]) == "OPEN"
        assert book.retire_untracked({"SBIN"}, H.at(16, 0)) == []
        assert H.val(H.trade_row(db.engine, "R1")["status"]) == "OPEN"

    def test_until_the_desk_says_which_series_it_tracks_only_data_time_applies(self, db):
        book = H.make_book(db.engine)
        waiting_on_reliance(book)
        assert book.step(H.at(16, 0), H.Feed(RELIANCE=None)) == []  # nothing told: it just waits
        assert H.val(H.trade_row(db.engine, "R1")["status"]) == "PENDING_FILL"

    def test_every_step_retires_what_became_overdue_since_the_desk_last_spoke(self, db):
        book = H.make_book(db.engine)
        waiting_on_reliance(book)
        assert book.retire_untracked({"SBIN"}, H.at(10, 10)) == []  # not overdue yet
        events = book.step(H.at(10, 16), H.Feed(SBIN=None, RELIANCE=None))
        assert [H.val(e.kind) for e in events] == ["NO_FILL"]
        assert events[0].detail["reason"] == "series_untracked"
        assert events[0].ts == H.at(10, 16)

    def test_a_reload_replaces_the_tracked_set_it_does_not_add_to_it(self, db):
        book = H.make_book(db.engine)
        waiting_on_reliance(book, "R1")
        H.open_trade(book, alert_id="S1", instrument_key="SBIN")
        late = H.at(11, 0)
        assert book.retire_untracked({"SBIN", "RELIANCE"}, late) == []  # both tracked
        retired = book.retire_untracked({"SBIN"}, late)  # RELIANCE dropped by a reload
        assert [e.trade_id for e in retired] == [H.trade_row(db.engine, "R1")["id"]]
        assert H.val(H.trade_row(db.engine, "S1")["status"]) == "PENDING_FILL"
        # a series that is tracked again after a reload is no longer retired
        waiting_on_reliance(book, "R2", created=H.at(10, 50, 3))  # expires 11:00:03
        assert book.retire_untracked({"SBIN", "RELIANCE"}, H.at(12, 0)) == []
        assert H.val(H.trade_row(db.engine, "R2")["status"]) == "PENDING_FILL"
        again = book.retire_untracked({"SBIN"}, H.at(12, 0))  # dropped by a second reload
        assert [e.trade_id for e in again] == [H.trade_row(db.engine, "R2")["id"]]

    def test_a_restart_forgets_the_set_until_the_desk_tells_the_new_book_again(self, db):
        waiting_on_reliance(H.make_book(db.engine))
        restarted = H.make_book(db.engine)
        assert restarted.step(H.at(12, 0), H.Feed(RELIANCE=None)) == []  # not told yet
        events = restarted.retire_untracked({"SBIN"}, H.at(12, 0))
        assert [H.val(e.kind) for e in events] == ["NO_FILL"]
        assert H.val(H.trade_row(db.engine, "R1")["status"]) == "NO_FILL"

    def test_an_empty_tracked_set_retires_every_overdue_waiting_trade(self, db):
        book = H.make_book(db.engine)
        waiting_on_reliance(book, "R1")
        H.open_trade(book, alert_id="S1", instrument_key="SBIN")
        events = book.retire_untracked(set(), H.at(12, 0))
        assert sorted(H.val(r["status"]) for r in H.trade_rows(db.engine)) == ["NO_FILL", "NO_FILL"]
        assert len(events) == 2

    def test_a_naive_now_is_rejected(self, db):
        with pytest.raises(ValueError):
            H.make_book(db.engine).retire_untracked({"SBIN"}, H.at(12, 0).replace(tzinfo=None))

    def test_a_locked_database_is_survived_and_the_trade_waits_for_the_next_call(self, tmp_path):
        db = H.new_db(tmp_path, "busy.db")
        short = H.short_timeout_engine(tmp_path / "busy.db", TIMEOUT)
        try:
            book = H.make_book(short)
            waiting_on_reliance(book)
            with H.write_lock(tmp_path / "busy.db"):
                assert book.retire_untracked({"SBIN"}, H.at(12, 0)) == []  # must not raise
            assert H.val(H.trade_row(db.engine, "R1")["status"]) == "PENDING_FILL"
            assert book.status()["hook_errors"] == 1
            assert len(book.retire_untracked({"SBIN"}, H.at(12, 1))) == 1
        finally:
            short.dispose()
            db.dispose()


class RetireSpy(H.SpyPaper):
    """A stand-in book that also records retire_untracked (the Desk's duck-typed call)."""

    def __init__(self, *a, fail: bool = False, **kw):
        super().__init__(*a, **kw)
        self.retire_calls: list[tuple[set[str], dt.datetime]] = []
        self.fail = fail

    def retire_untracked(self, series_keys, now):
        self.retire_calls.append((set(series_keys), now))
        if self.fail:
            raise RuntimeError("retire exploded")
        return []


class TestTheDeskTellsTheBookWhichSeriesItTracks:
    def test_it_does_so_after_construction_and_again_after_a_reload(self, desk_db):
        cfg = AppConfig.load()
        spy = RetireSpy()
        before = dt.datetime.now(tz=IST)
        d, _ = H.new_desk(cfg, desk_db, paper=spy)
        ((keys, now),) = spy.retire_calls
        assert keys == {"BANKNIFTY", "SBIN"} == set(d.trackers)
        assert now.tzinfo is not None and abs((now - before).total_seconds()) < 30
        assert d.hook_errors == 0
        d.reload()  # the watchlist is re-read from desk.yaml
        assert len(spy.retire_calls) == 2
        assert spy.retire_calls[1][0] == set(d.trackers) and len(d.trackers) > 2

    def test_a_failure_of_the_call_is_counted_and_the_desk_still_starts(self, desk_db):
        spy = RetireSpy(fail=True)
        d, notifier = H.new_desk(AppConfig.load(), desk_db, paper=spy)
        assert d.hook_errors == 1
        d.poll()  # still polls
        assert d.polls == 1

    def test_a_book_without_the_method_is_left_alone(self, desk_db):
        d, _ = H.new_desk(AppConfig.load(), desk_db, paper=H.SpyPaper())
        assert d.hook_errors == 0

    def test_no_book_means_nothing_to_tell(self, desk_db):
        d, _ = H.new_desk(AppConfig.load(), desk_db)
        assert d.paper is None and d.hook_errors == 0

    def test_a_real_book_retires_an_overdue_trade_on_a_series_the_desk_does_not_track(
        self, desk_db
    ):
        """The desk here tracks BANKNIFTY and SBIN only. A waiting RELIANCE trade from an earlier
        run (long past its deadline by the wall clock) is closed when the Desk is built."""
        cfg = AppConfig.load()
        book = H.desk_book(cfg, desk_db.engine)
        trade = waiting_on_reliance(book)
        assert H.val(trade.status) == "PENDING_FILL"
        d, _ = H.new_desk(cfg, desk_db, paper=book)
        row = H.trade_row(desk_db.engine, "R1")
        assert H.val(row["status"]) == "NO_FILL" and row["close_reason"] == "NO_FILL"
        (event,) = [e for e in H.event_rows(desk_db.engine, "R1") if H.val(e["kind"]) == "NO_FILL"]
        assert event["detail"]["reason"] == "series_untracked"
        assert d.hook_errors == 0 and book.status()["no_fill"] == 1
        d.poll()
        assert d.snapshot()["paper"]["no_fill"] == 1

    def test_a_trade_on_a_tracked_series_is_not_touched_by_the_desks_call(self, desk_db):
        cfg = AppConfig.load()
        book = H.desk_book(cfg, desk_db.engine)
        H.open_trade(book, alert_id="S1", instrument_key="SBIN")
        H.new_desk(cfg, desk_db, paper=book)
        assert H.val(H.trade_row(desk_db.engine, "S1")["status"]) == "PENDING_FILL"


# ── foreign key and the guarded PRICING_UNAVAILABLE ───────────────────────────────────
class TestEventsReferenceTheirTrade:
    def test_trade_id_is_a_foreign_key_to_paper_trades(self, db):
        (fk,) = sa_inspect(db.engine).get_foreign_keys("paper_events")
        assert fk["constrained_columns"] == ["trade_id"]
        assert fk["referred_table"] == "paper_trades" and fk["referred_columns"] == ["id"]

    def test_an_event_for_a_trade_that_does_not_exist_is_refused(self, db):
        with pytest.raises(IntegrityError), db.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO paper_events (trade_id, ts, kind, detail) "
                    "VALUES (424242, '2026-10-29 04:35:03', 'FILLED', '{}')"
                )
            )

    def test_a_trade_that_has_events_cannot_be_deleted_out_from_under_them(self, db):
        H.open_trade(H.make_book(db.engine), alert_id="A1")
        with pytest.raises(IntegrityError), db.engine.begin() as conn:
            conn.execute(text("DELETE FROM paper_trades"))
        assert H.count_rows(db.engine, "paper_trades") == 1

    def test_every_flow_leaves_the_database_with_no_orphan_events(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="F1")
        H.open_trade(book, alert_id="G1", stop=1005.0, instrument_key="RELIANCE")
        H.open_trade(book, alert_id="U1", instrument_key="HDFCBANK")
        H.open_trade(book, alert_id="T1", kind="INDEX")
        fail_on_alert(book, db.engine, [H.make_alert("Q1", instrument_key="INFY")])
        H.insert_desk_signal(db.engine, H.make_alert("R9", instrument_key="INFY"))
        book.retire_untracked({"SBIN", "RELIANCE", "BANKNIFTY", "INFY"}, H.at(12, 0))
        book.step(
            H.at(10, 8),
            H.Feed(
                SBIN=H.standard_bars(),
                RELIANCE=H.standard_bars(),
                BANKNIFTY=H.minute_bars(H.at(10, 5), [54000.0] * 3),
                INFY=H.standard_bars(),
            ),
        )
        H.make_book(db.engine, clock=lambda: H.at(10, 30))
        with db.engine.connect() as conn:
            assert conn.execute(text("PRAGMA foreign_key_check")).all() == []
        assert H.count_rows(db.engine, "paper_events") >= 8


class TestAPricingUnavailableWriteIsGuarded:
    def test_a_trade_another_book_already_closed_gets_no_stray_event_and_is_adopted(self, db):
        """Book B still sees the trade waiting and hits an unpriceable fill bar; book A already
        closed it NO_FILL. B's PRICING_UNAVAILABLE must not be booked against the closed trade."""
        from onelot.paper.factory import build_paper_book

        cfg = AppConfig.load()
        a = build_paper_book(cfg, db.engine, clock=lambda: H.at(10, 30))
        alert = H.make_alert("I1", kind="INDEX", iv=None, drop_iv=True, premium=None)
        H.open_trade(a, alert)  # no IV, no premium: it can never be priced
        b = build_paper_book(cfg, db.engine, clock=lambda: H.at(10, 30))
        assert [t.alert_id for t in b.open_trades()] == ["I1"]
        late = H.minute_bars(H.at(10, 16), [H.OPT_U0] * 3)  # window (10:15:03) over
        assert [H.val(e.kind) for e in a.step(H.at(10, 30), H.Feed(BANKNIFTY=late))] == ["NO_FILL"]
        early = H.minute_bars(H.at(10, 5), [H.OPT_U0 - 5, H.OPT_U0, H.OPT_U0 + 1])
        assert b.step(H.at(10, 8), H.Feed(BANKNIFTY=early)) == []  # stale view: nothing booked
        kinds = [H.val(e["kind"]) for e in H.event_rows(db.engine, "I1")]
        assert kinds == ["OPENED", "NO_FILL"], kinds
        assert H.val(H.trade_row(db.engine, "I1")["status"]) == "NO_FILL"
        assert b.open_trades() == []  # adopted the stored row
