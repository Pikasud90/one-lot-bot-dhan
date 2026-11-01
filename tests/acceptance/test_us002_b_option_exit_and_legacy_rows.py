"""US-002 mode B: the option exit leg when repricing fails, and rows written before US-002.

Index option exit (Manager scope decision, story notes): the close is judged on the UNDERLYING;
the exit premium is `option_exit_paise(premium_at(option, underlying close level, close ts))`
with the option frozen at the alert. When that premium is unavailable (the pricer returns None,
raises, or answers something that is not a positive finite number) the close is STILL booked,
final, with exit_paise / gross_paise / costs_paise / net_paise all NULL (never a silent zero) and
a PRICING_UNAVAILABLE event for the exit leg, stamped with the close time.

Rows written by the US-001 book have no management record in their plan (`plan["position"]`).
They must still be managed from their fill, for a stock and for an option (whose underlying open
is read back from the FILLED event).
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
import us001_helpers as H
import us002_helpers as U
from sqlalchemy import text

PRE = (5399000, 5399500, 5398500, 5399000)
FILL = (5400000, 5401000, 5399000, 5400500)
TARGET_BAR = (5400500, 5431000, 5400000, 5430000)  # trades through 54300.00
FILL_TS = H.at(10, 6)
Q = (100000, 100100, 99900, 100000)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


def failing_after_the_fill(bad):
    """A pricer that prices the fill bar (entry and the stop leg of the risk) and then fails."""
    good = H.make_pricer()
    calls = []

    def premium_at(option, underlying, ts):
        calls.append((dict(option), float(underlying), ts))
        if ts > FILL_TS:
            if isinstance(bad, Exception):
                raise bad
            return bad
        return good(option, underlying, ts)

    premium_at.calls = calls
    return premium_at


def run_option(db, pricer, rows, *, direction="long", now=None, alert=None):
    alert = alert or U.make_alert("O1", kind="INDEX", direction=direction)
    book = H.make_book(db.engine, premium_at=pricer)
    H.open_trade(book, alert)
    frame = U.pbars(H.at(10, 5), [PRE, *rows])
    events = U.step_book(book, frame, now, series="BANKNIFTY")
    return book, U.row_of(db.engine, "O1"), events


BAD_PRICES = [
    pytest.param(None, id="none"),
    pytest.param(RuntimeError("pricer down"), id="raises"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(float("inf"), id="inf"),
    pytest.param(0.0, id="zero"),
    pytest.param(-5.0, id="negative"),
    pytest.param("n/a", id="string"),
    pytest.param(True, id="bool"),
]


class TestAnOptionThatCannotBeRepricedAtTheExitStillCloses:
    @pytest.mark.parametrize("bad", BAD_PRICES)
    def test_the_close_is_booked_with_null_money_and_a_pricing_event(self, db, bad):
        book, row, events = run_option(db, failing_after_the_fill(bad), [FILL, TARGET_BAR])
        assert row["status"] == "CLOSED" and row["close_reason"] == "TARGET"
        assert row["entry_paise"] == 45280, "the entry premium of the fill is untouched"
        for column in ("exit_paise", "gross_paise", "costs_paise", "net_paise"):
            assert row[column] is None, f"{column} must be NULL, never a silent zero"
        assert H.same_instant(row["exit_ts"], H.at(10, 7))
        unavailable = [e for e in events if str(e.kind) == "PRICING_UNAVAILABLE"]
        assert len(unavailable) == 1 and unavailable[0].detail["leg"] == "exit"
        assert U.mentions(unavailable[0].detail, 5430000), "the underlying level it tried"
        assert unavailable[0].ts == U.close_of(events).at == H.at(10, 7)
        assert book.status()["closed"] == 1 and book.status()["hook_errors"] == 0
        assert book.open_trades() == []

    def test_both_events_are_stored_so_a_restart_still_knows_why_there_is_no_price(self, db):
        run_option(db, failing_after_the_fill(None), [FILL, TARGET_BAR])
        stored = {str(H.val(e["kind"])): e for e in H.event_rows(db.engine, "O1")}
        assert {"OPENED", "FILLED", "CLOSED", "PRICING_UNAVAILABLE"} <= set(stored)
        assert stored["PRICING_UNAVAILABLE"]["detail"]["leg"] == "exit"
        assert stored["CLOSED"]["detail"]["reason"] == "TARGET"

    def test_the_close_is_final_a_pricer_that_recovers_never_reopens_or_re_books_it(self, db):
        pricer = failing_after_the_fill(None)
        book, row, _ = run_option(db, pricer, [FILL, TARGET_BAR])
        before = (H.trade_rows(db.engine), H.event_rows(db.engine))
        later = U.pbars(H.at(10, 5), [PRE, FILL, TARGET_BAR, (5430000, 5431000, 5429000, 5430500)])
        assert U.step_book(book, later, H.at(10, 30), series="BANKNIFTY") == []
        assert (H.trade_rows(db.engine), H.event_rows(db.engine)) == before
        reborn = H.make_book(db.engine, premium_at=H.make_pricer())
        assert U.step_book(reborn, later, H.at(10, 30), series="BANKNIFTY") == []
        assert U.row_of(db.engine, "O1")["exit_paise"] is None

    @pytest.mark.parametrize(
        "rows, reason",
        [
            ([FILL, (5400500, 5400600, 5384000, 5390000)], "STOP"),
            ([FILL, (5370000, 5371000, 5369000, 5370500)], "STOP"),  # a gap through the stop
        ],
        ids=["stop", "gap-stop"],
    )
    def test_a_stop_close_with_an_unpriceable_option_is_booked_the_same_way(self, db, rows, reason):
        book, row, events = run_option(db, failing_after_the_fill(None), rows)
        assert row["status"] == "CLOSED" and row["close_reason"] == reason
        assert row["exit_paise"] is None and row["gross_paise"] is None
        assert "PRICING_UNAVAILABLE" in U.kinds(events) and book.status()["hook_errors"] == 0

    def test_a_buy_pe_target_close_is_booked_the_same_way(self, db):
        rows = [(5400000, 5400500, 5399500, 5399800), (5399800, 5399900, 5369000, 5370500)]
        _, row, events = run_option(db, failing_after_the_fill(None), rows, direction="short")
        assert row["close_reason"] == "TARGET" and row["exit_paise"] is None
        assert "PRICING_UNAVAILABLE" in U.kinds(events)

    def test_the_session_over_fallback_close_of_an_option_is_booked_the_same_way(self, db):
        """The feed stops at 10:08; at 15:40 the session is over and the option cannot be priced."""
        quiet = (5400000, 5400500, 5399500, 5400200)
        rows = [FILL, quiet, quiet]
        book, row, events = run_option(db, failing_after_the_fill(None), rows, now=H.at(15, 40))
        assert row["status"] == "CLOSED" and row["close_reason"] == "SESSION_END"
        assert row["exit_paise"] is None and row["gross_paise"] is None
        assert "PRICING_UNAVAILABLE" in U.kinds(events) and book.status()["hook_errors"] == 0

    def test_a_stock_never_asks_the_option_pricer_at_all(self, db):
        def explode(option, underlying, ts):
            raise AssertionError("a stock trade must never reach premium_at")

        book = H.make_book(db.engine, premium_at=explode)
        H.open_trade(book, U.make_alert("A1"))
        U.step_book(book, U.story([Q, (100000, 100100, 99000, 99500)]))
        row = U.row_of(db.engine, "A1")
        assert row["status"] == "CLOSED" and row["exit_paise"] == 98970
        assert book.status()["hook_errors"] == 0


class TestTheExitLegIsPricedAtTheUnslippedUnderlyingLevelAndTheCloseTime:
    def last_exit_call(self, pricer):
        return pricer.calls[-1]

    def test_a_target_is_priced_at_exactly_the_target_level_at_the_bars_open_time(self, db):
        pricer = H.make_pricer()
        run_option(db, pricer, [FILL, TARGET_BAR])
        option, underlying, ts = self.last_exit_call(pricer)
        assert underlying == 54300.0 and ts == H.at(10, 7)
        assert option["opt_type"] == "CE" and option["premium"] == 450.0

    def test_a_gap_stop_is_priced_at_the_open_not_at_the_stop_nor_the_slipped_open(self, db):
        pricer = H.make_pricer()
        run_option(db, pricer, [FILL, (5370000, 5371000, 5369000, 5370500)])
        _, underlying, ts = self.last_exit_call(pricer)
        assert underlying == 53700.0 and ts == H.at(10, 7)

    def test_the_option_dict_handed_over_is_the_frozen_one_not_a_live_chain(self, db):
        pricer = H.make_pricer()
        alert = U.make_alert("O1", kind="INDEX", iv=0.21)
        run_option(db, pricer, [FILL, TARGET_BAR], alert=alert)
        option, _, _ = self.last_exit_call(pricer)
        assert (
            option["iv"] == 0.21
            and option["strike"] == 54000.0
            and option["expiry"] == "2026-11-24"
        )

    def test_one_exit_leg_is_priced_once(self, db):
        pricer = H.make_pricer()
        book, _, _ = run_option(db, pricer, [FILL, TARGET_BAR])
        seen = len(pricer.calls)
        U.step_book(book, U.pbars(H.at(10, 5), [PRE, FILL, TARGET_BAR]), series="BANKNIFTY")
        assert len(pricer.calls) == seen, "a closed trade is never repriced"


def strip_position(engine, alert_id):
    """Turn a row into what the US-001 book wrote: a plan with no management record."""
    with engine.begin() as conn:
        raw = conn.execute(
            text("SELECT plan FROM paper_trades WHERE alert_id = :a"), {"a": alert_id}
        ).scalar_one()
        plan = json.loads(raw) if isinstance(raw, str) else raw
        assert "position" in plan, "the book is expected to record its management state"
        plan.pop("position")
        conn.execute(
            text("UPDATE paper_trades SET plan = :p WHERE alert_id = :a"),
            {"p": json.dumps(plan), "a": alert_id},
        )


class TestRowsWrittenBeforeUs002:
    def test_an_open_stock_row_without_a_management_record_is_managed_from_its_fill(self, db):
        first = H.make_book(db.engine)
        H.open_trade(first, U.make_alert("A1"))
        first.step(H.at(10, 7), H.Feed(SBIN=U.story([Q])))
        strip_position(db.engine, "A1")
        book = H.make_book(db.engine)
        frame = U.story([Q, (100000, 100100, 99000, 99500)])
        book.step(U.end_of(frame), H.Feed(SBIN=frame))
        row = U.row_of(db.engine, "A1")
        assert row["status"] == "CLOSED" and row["close_reason"] == "STOP"
        assert row["exit_paise"] == 98970 and row["gross_paise"] == -1060
        assert book.status()["hook_errors"] == 0

    def test_an_open_option_row_without_a_record_reads_the_fill_open_from_its_event(self, db):
        first = H.make_book(db.engine)
        H.open_trade(first, U.make_alert("O1", kind="INDEX"))
        first.step(H.at(10, 7), H.Feed(BANKNIFTY=U.pbars(H.at(10, 5), [PRE, FILL])))
        strip_position(db.engine, "O1")
        book = H.make_book(db.engine)
        frame = U.pbars(H.at(10, 5), [PRE, FILL, TARGET_BAR])
        book.step(U.end_of(frame), H.Feed(BANKNIFTY=frame))
        row = U.row_of(db.engine, "O1")
        entry, exit_, gross = U.oracle_option_roundtrip(
            bt=H.BT,
            spec=H.SPECS["BANKNIFTY_OPT"],
            opt_type="CE",
            underlying_entry=54000.0,
            underlying_exit=54300.0,
            entry_ts=FILL_TS,
            exit_ts=H.at(10, 7),
            pricer=H.make_pricer(),
        )
        assert row["close_reason"] == "TARGET"
        assert (row["entry_paise"], row["exit_paise"], row["gross_paise"]) == (entry, exit_, gross)
        assert book.status()["hook_errors"] == 0

    def test_a_row_that_never_had_its_management_record_equals_one_that_always_had_it(
        self, db, tmp_path
    ):
        other = H.new_db(tmp_path, "other.db")
        try:
            rows = [Q, Q, (100000, 100100, 99000, 99500)]
            frame = U.story(rows)
            steady = H.make_book(other.engine)
            H.open_trade(steady, U.make_alert("A1"))
            steady.step(U.end_of(frame), H.Feed(SBIN=frame))

            legacy = H.make_book(db.engine)
            H.open_trade(legacy, U.make_alert("A1"))
            legacy.step(H.at(10, 8), H.Feed(SBIN=U.story(rows[:2])))  # fill + 2 quiet bars
            strip_position(db.engine, "A1")
            H.make_book(db.engine).step(U.end_of(frame), H.Feed(SBIN=frame))
            keys = ("status", "close_reason", "exit_paise", "gross_paise", "exit_ts")
            a, b = U.row_of(db.engine, "A1"), U.row_of(other.engine, "A1")
            assert {k: a[k] for k in keys} == {k: b[k] for k in keys}
        finally:
            other.dispose()

    def test_a_flat_trade_row_survives_a_json_round_trip_of_its_plan(self, db):
        """The management record lives in a JSON column: it must read back as plain JSON types."""
        book = H.make_book(db.engine)
        H.open_trade(book, U.make_alert("A1", manage={"trail_atr_mult": 1.5, "atr": 4.0}))
        frame = U.story([(100000, 101200, 100100, 101100), Q])
        book.step(U.end_of(frame), H.Feed(SBIN=frame))
        plan = U.row_of(db.engine, "A1")["plan"]
        position = plan["position"]
        json.dumps(position)  # no numpy / datetime object leaked into the column
        for key in (
            "entry_paise",
            "initial_stop_paise",
            "stop_paise",
            "bars_held",
            "sessions_held",
        ):
            assert type(position[key]) is int, key
        assert isinstance(position["opened_at"], str) and dt.datetime.fromisoformat(
            position["opened_at"]
        )
