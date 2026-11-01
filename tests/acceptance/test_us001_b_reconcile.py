"""US-001 mode B: startup reconciliation, its horizon, and corrupt desk_signals rows.

Constructing a PaperBook books a trade for every desk_signals row that has none (a crash
between the desk's insert and the hook; an alert raised before any book existed). Manager
decision (f): bounded, only rows raised since MIDNIGHT IST `paper.reconcile_days` calendar
days before the clock (default 3, which covers a weekend: a Monday start books Friday).
A booked row is stamped with the ALERT's created_at, so it fills exactly as if the hook had
run. One unreadable row (corrupt JSON, missing fields, naive times ...) is logged and
skipped; it never stops the start and never blocks its neighbours.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time

import pandas as pd
import pytest
import us001_helpers as H
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from onelot.clock import IST
from onelot.config import PaperCfg

PROP = settings(
    max_examples=40,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=list(HealthCheck),
)
MON_0900 = H.at(9, 0, day=H.MONDAY)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


def raise_row(engine, alert_id, created, **kw):
    alert = H.make_alert(alert_id, created=created, **kw)
    H.insert_desk_signal(engine, alert)
    return alert


def booked_after_start(engine, *, clock, days):
    H.make_book(engine, clock=lambda: clock, reconcile_days=days)
    return {r["alert_id"] for r in H.trade_rows(engine)}


def midnight_ist(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(0), tzinfo=IST)


class TestHorizon:
    @pytest.mark.parametrize(
        "days, created, booked",
        [
            (3, H.at(23, 59, 59, day=dt.date(2026, 10, 29)), False),  # Thu, before the horizon
            (3, H.at(0, 0, 0, day=H.FRIDAY), True),  # Fri 00:00:00, the horizon itself
            (3, H.at(15, 29, 3, day=H.FRIDAY), True),  # the Friday 15:29 alert
            (3, H.at(8, 59, 59, day=H.MONDAY), True),
            (0, H.at(23, 59, 59, day=dt.date(2026, 11, 1)), False),  # Sunday night
            (0, H.at(0, 0, 0, day=H.MONDAY), True),
            (1, H.at(23, 59, 59, day=dt.date(2026, 10, 31)), False),
            (1, H.at(0, 0, 0, day=dt.date(2026, 11, 1)), True),
            (10, H.at(0, 0, 0, day=dt.date(2026, 10, 23)), True),
            (10, H.at(23, 59, 59, day=dt.date(2026, 10, 22)), False),
        ],
        ids=[
            "3d-thu-before",
            "3d-fri-midnight",
            "3d-fri-1529",
            "3d-mon-early",
            "0d-sun-night",
            "0d-mon-midnight",
            "1d-sat-night",
            "1d-sun-midnight",
            "10d-in",
            "10d-out",
        ],
    )
    def test_only_rows_since_midnight_ist_that_many_days_back_are_booked(
        self, db, days, created, booked
    ):
        raise_row(db.engine, "R1", created)
        assert (booked_after_start(db.engine, clock=MON_0900, days=days) == {"R1"}) is booked

    @pytest.mark.parametrize(
        "clock, days, created, booked",
        [
            (
                dt.datetime(2028, 3, 1, 9, 0, tzinfo=IST),
                2,
                dt.datetime(2028, 2, 28, 0, 0, 1, tzinfo=IST),
                True,
            ),
            (
                dt.datetime(2028, 3, 1, 9, 0, tzinfo=IST),
                2,
                dt.datetime(2028, 2, 27, 23, 59, 59, tzinfo=IST),
                False,
            ),
            (
                dt.datetime(2027, 1, 1, 9, 0, tzinfo=IST),
                3,
                dt.datetime(2026, 12, 29, 0, 0, tzinfo=IST),
                True,
            ),
            (
                dt.datetime(2027, 1, 1, 9, 0, tzinfo=IST),
                3,
                dt.datetime(2026, 12, 28, 23, 59, 59, tzinfo=IST),
                False,
            ),
            (
                dt.datetime(2026, 11, 2, 0, 0, 1, tzinfo=IST),
                0,
                dt.datetime(2026, 11, 2, 0, 0, tzinfo=IST),
                True,
            ),
        ],
        ids=["leap-day-in", "leap-day-out", "year-end-in", "year-end-out", "just-after-midnight"],
    )
    def test_calendar_arithmetic_across_month_year_and_leap_day(
        self, db, clock, days, created, booked
    ):
        raise_row(db.engine, "R1", created)
        assert (booked_after_start(db.engine, clock=clock, days=days) == {"R1"}) is booked

    @given(
        days=st.integers(0, 10),
        minutes_back=st.integers(0, 14 * 24 * 60),
        seconds=st.integers(0, 59),
    )
    @PROP
    def test_property_booked_iff_created_at_or_after_the_midnight_horizon(
        self, days, minutes_back, seconds
    ):
        created = MON_0900 - dt.timedelta(minutes=minutes_back, seconds=seconds)
        horizon = midnight_ist(MON_0900.date() - dt.timedelta(days=days))
        with H.temp_book() as (_book, engine):
            raise_row(engine, "P1", created)
            booked = booked_after_start(engine, clock=MON_0900, days=days)
        assert (booked == {"P1"}) is (created >= horizon)

    def test_the_default_horizon_is_three_days(self, db):
        H.make_book(db.engine)  # PaperCfg default
        assert PaperCfg().reconcile_days == 3
        thursday = raise_row(db.engine, "OLD", H.at(23, 59, 59, day=dt.date(2026, 10, 29)))
        friday = raise_row(db.engine, "NEW", H.at(0, 0, 1, day=H.FRIDAY))
        H.make_book(db.engine, clock=lambda: MON_0900)
        assert {r["alert_id"] for r in H.trade_rows(db.engine)} == {friday["id"]}
        assert thursday["id"] not in {r["alert_id"] for r in H.trade_rows(db.engine)}

    def test_the_horizon_does_not_depend_on_the_clock_being_in_ist(self, db):
        """Midnight IST, even when the clock hands back UTC."""
        raise_row(db.engine, "IN", H.at(0, 30, day=H.FRIDAY))  # Fri 00:30 IST = Thu 19:00 UTC
        raise_row(db.engine, "OUT", H.at(23, 30, day=dt.date(2026, 10, 29)))
        utc_clock = MON_0900.astimezone(dt.timezone.utc)  # Mon 03:30 UTC
        H.make_book(db.engine, clock=lambda: utc_clock, reconcile_days=3)
        assert {r["alert_id"] for r in H.trade_rows(db.engine)} == {"IN"}

    def test_older_alerts_stay_out_on_every_restart_until_the_horizon_is_widened(self, db):
        raise_row(db.engine, "OLD", MON_0900 - dt.timedelta(days=8))
        raise_row(db.engine, "NEW", MON_0900 - dt.timedelta(hours=20))
        assert booked_after_start(db.engine, clock=MON_0900, days=3) == {"NEW"}
        assert booked_after_start(db.engine, clock=MON_0900, days=3) == {"NEW"}
        assert booked_after_start(db.engine, clock=MON_0900, days=14) == {"NEW", "OLD"}
        assert len(H.trade_rows(db.engine)) == 2  # and nothing was booked twice

    def test_the_horizon_is_a_config_field_and_must_not_be_negative(self):
        assert PaperCfg(reconcile_days=0).reconcile_days == 0
        assert PaperCfg(reconcile_days=30).reconcile_days == 30
        with pytest.raises(ValidationError):
            PaperCfg(reconcile_days=-1)
        with pytest.raises(ValidationError):
            PaperCfg(reconcile_days="soon")  # type: ignore[arg-type]

    def test_a_disabled_book_reconciles_nothing_whatever_the_horizon(self, db):
        raise_row(db.engine, "R1", MON_0900 - dt.timedelta(hours=3))
        H.make_book(db.engine, enabled=False, clock=lambda: MON_0900, reconcile_days=30)
        assert H.count_rows(db.engine, "paper_trades") == 0


class TestAMondayStartBooksFridaysAlerts:
    def friday_and_monday_bars(self):
        friday = H.minute_bars(H.at(10, 0, day=H.FRIDAY), [1000.0] * 30)  # 10:00 .. 10:29
        late = H.minute_bars(H.at(15, 20, day=H.FRIDAY), [1000.0] * 10)  # .. 15:29
        monday = H.minute_bars(H.at(9, 15, day=H.MONDAY), [1000.0] * 6)
        return pd.concat([friday, late, monday])

    def test_the_friday_1529_alert_is_booked_pending_and_closes_no_fill_never_filling_monday(
        self, db
    ):
        alert = raise_row(db.engine, "F1", H.at(15, 29, 3, day=H.FRIDAY))
        book = H.make_book(db.engine, clock=lambda: MON_0900)
        row = H.trade_row(db.engine, alert["id"])
        assert H.val(row["status"]) == "PENDING_FILL"
        assert H.same_instant(row["created_at"], H.at(15, 29, 3, day=H.FRIDAY))
        events = book.step(H.at(9, 20, day=H.MONDAY), H.Feed(SBIN=self.friday_and_monday_bars()))
        row = H.trade_row(db.engine, alert["id"])
        assert [H.val(e.kind) for e in events] == ["NO_FILL"]
        assert H.val(row["status"]) == "NO_FILL" and row["entry_paise"] is None
        assert [H.val(e["kind"]) for e in H.event_rows(db.engine, "F1")] == ["OPENED", "NO_FILL"]

    def test_a_friday_alert_the_hook_never_booked_fills_from_fridays_stored_bars(self, db):
        """Reconciliation is 'as if the hook had run': the desk keeps 70 days of 1-minute bars, so
        the Friday 10:06 bar is still there and is the fill, not a Monday bar."""
        alert = raise_row(db.engine, "F2", H.at(10, 5, 3, day=H.FRIDAY))
        book = H.make_book(db.engine, clock=lambda: MON_0900)
        frame = self.friday_and_monday_bars()
        frame.loc[H.at(10, 6, day=H.FRIDAY), "open"] = 1003.35
        events = book.step(H.at(9, 20, day=H.MONDAY), H.Feed(SBIN=frame))
        row = H.trade_row(db.engine, alert["id"])
        assert [H.val(e.kind) for e in events] == ["FILLED"]
        assert H.same_instant(row["fill_bar_ts"], H.at(10, 6, day=H.FRIDAY))
        assert row["entry_paise"] == H.expected_stock_entry(1003.35, "long")

    def test_a_weekend_of_alerts_is_all_booked_in_creation_order(self, db):
        stamps = [
            H.at(9, 40, day=H.FRIDAY),
            H.at(11, 5, day=H.FRIDAY),
            H.at(14, 50, day=H.FRIDAY),
        ]
        for i, ts in enumerate(stamps):
            raise_row(db.engine, f"W{i}", ts)
        book = H.make_book(db.engine, clock=lambda: MON_0900)
        assert [t.alert_id for t in book.open_trades()] == ["W0", "W1", "W2"]
        assert book.status()["opened"] == 3

    def test_many_rows_are_booked_in_reasonable_time(self, db):
        for i in range(300):
            raise_row(db.engine, f"M{i:03d}", MON_0900 - dt.timedelta(minutes=i + 1))
        started = time.perf_counter()
        book = H.make_book(db.engine, clock=lambda: MON_0900)
        elapsed = time.perf_counter() - started
        assert len(book.open_trades()) == 300
        assert elapsed < 20.0, f"reconciling 300 rows took {elapsed:.1f}s"


def _good(alert_id, **kw):
    return H.make_alert(alert_id, created=H.at(10, 5, 3), **kw)


def _tamper(**changes):
    def build(alert_id):
        a = _good(alert_id)
        for key, value in changes.items():
            a[key] = value
        return json.dumps(a)

    return build


def _tamper_eco(**changes):
    def build(alert_id):
        a = _good(alert_id, kind="INDEX")
        a["economics"].update(changes)
        return json.dumps(a)

    return build


def _tamper_option(**changes):
    def build(alert_id):
        a = _good(alert_id, kind="INDEX")
        a["economics"]["option"].update(changes)
        return json.dumps(a)

    return build


CORRUPT = {
    "not-json": lambda i: "not json {",
    "truncated-json": lambda i: json.dumps(_good(i))[:60],
    "empty-text": lambda i: "",
    "json-null": lambda i: "null",
    "json-list": lambda i: "[1, 2, 3]",
    "json-number": lambda i: "42",
    "json-string": lambda i: '"just a string"',
    "double-encoded": lambda i: json.dumps(json.dumps(_good(i))),
    "empty-object": lambda i: "{}",
    "sql-null": lambda i: None,
    "no-economics": _tamper(economics=None),
    "economics-is-a-list": _tamper(economics=[1, 2]),
    "bad-kind": _tamper(kind="FUTURES"),
    "bad-direction": _tamper(direction="sideways"),
    "no-instrument": _tamper(instrument_key=None),
    "no-series": _tamper(series_key=""),
    "naive-created": _tamper(created_at="2026-10-29T10:05:03"),
    "naive-expires": _tamper(expires_at="2026-10-29T10:15:03"),
    "garbage-created": _tamper(created_at="yesterday-ish"),
    "expires-before-created": _tamper(expires_at="2026-10-29T10:00:00+05:30"),
    "stop-missing": _tamper_eco(underlying_stop=None),
    "stop-text": _tamper_eco(underlying_stop="high"),
    "entry-missing": _tamper_eco(underlying_entry=None),
    "option-lot-zero": _tamper_option(lot=0),
    "option-lot-text": _tamper_option(lot="thirty"),
    "option-lot-fraction": _tamper_option(lot=7.5),
}


class TestACorruptDeskSignalIsSkippedNotFatal:
    @pytest.mark.parametrize("name", list(CORRUPT))
    def test_the_bad_row_is_skipped_its_neighbours_are_booked_and_the_start_survives(
        self, db, caplog, name
    ):
        for alert_id in ("G1", "G2"):
            raise_row(db.engine, alert_id, H.at(10, 5, 3))
        raise_row(db.engine, "BAD", H.at(10, 5, 3))
        H.raw_set_payload(db.engine, "BAD", CORRUPT[name]("BAD"))
        with caplog.at_level(logging.WARNING):
            book = H.make_book(db.engine, clock=lambda: H.at(10, 30))  # must not raise
        booked = {r["alert_id"] for r in H.trade_rows(db.engine)}
        assert booked == {"G1", "G2"}, f"{name}: {booked}"
        assert [t.alert_id for t in book.open_trades()] == ["G1", "G2"]
        assert book.status()["opened"] == 2 and book.status()["hook_errors"] == 0
        warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("BAD" in m for m in warned), f"{name}: the skipped row was not logged: {warned}"
        # a second start copes with the same row again, and books nothing new
        H.make_book(db.engine, clock=lambda: H.at(10, 30))
        assert {r["alert_id"] for r in H.trade_rows(db.engine)} == {"G1", "G2"}

    def test_several_bad_rows_in_a_row_never_stop_the_good_ones_between_them(self, db):
        order = ["G0", "B1", "B2", "G1", "B3", "G2"]
        for i, alert_id in enumerate(order):
            raise_row(db.engine, alert_id, H.at(10, 0, 0) + dt.timedelta(minutes=i))
        for alert_id, name in (("B1", "not-json"), ("B2", "json-null"), ("B3", "bad-kind")):
            H.raw_set_payload(db.engine, alert_id, CORRUPT[name](alert_id))
        book = H.make_book(db.engine, clock=lambda: H.at(10, 30))
        assert [t.alert_id for t in book.open_trades()] == ["G0", "G1", "G2"]

    def test_a_payload_without_an_id_is_booked_under_the_rows_id(self, db):
        raise_row(db.engine, "R1", H.at(10, 5, 3))
        stored = _good("R1")
        del stored["id"]
        H.raw_set_payload(db.engine, "R1", json.dumps(stored))
        H.make_book(db.engine, clock=lambda: H.at(10, 30))
        assert H.trade_row(db.engine, "R1")["alert_id"] == "R1"

    def test_a_payload_that_names_another_alert_is_booked_under_the_rows_id(self, db, caplog):
        """desk_signals.id is the join key. A payload carrying a different id (here one that already
        HAS a trade) must neither collide with it nor move it."""
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="TAKEN")  # TAKEN already has its paper trade
        before = H.trade_row(db.engine, "TAKEN")
        raise_row(db.engine, "R2", H.at(10, 5, 3))
        confused = _good("TAKEN")
        H.raw_set_payload(db.engine, "R2", json.dumps(confused))
        with caplog.at_level(logging.WARNING):
            H.make_book(db.engine, clock=lambda: H.at(10, 30))
        assert H.trade_row(db.engine, "TAKEN") == before
        assert H.trade_row(db.engine, "R2")["alert_id"] == "R2"
        assert any("R2" in r.getMessage() for r in caplog.records)

    def test_a_row_of_the_wrong_shape_does_not_poison_the_database_for_later_starts(self, db):
        raise_row(db.engine, "BAD", H.at(10, 5, 3))
        H.raw_set_payload(db.engine, "BAD", "{{{")
        for _ in range(3):
            book = H.make_book(db.engine, clock=lambda: H.at(10, 30))
            assert book.open_trades() == []
        raise_row(db.engine, "LATER", H.at(10, 6, 3))
        book = H.make_book(db.engine, clock=lambda: H.at(10, 30))
        assert [t.alert_id for t in book.open_trades()] == ["LATER"]
        assert [r["alert_id"] for r in H.trade_rows(db.engine)] == ["LATER"]

    def test_a_database_without_a_desk_signals_table_is_a_clean_start(self, tmp_path):
        """A paper-only database (no desk tables yet) has nothing to reconcile and must not fail."""
        from sqlalchemy import create_engine

        from onelot.store.schema import paper_events, paper_trades

        engine = create_engine(f"sqlite:///{tmp_path / 'paper_only.db'}", future=True)
        paper_trades.metadata.create_all(engine, tables=[paper_trades, paper_events])
        try:
            book = H.make_book(engine)
            assert book.open_trades() == [] and book.status()["opened"] == 0
        finally:
            engine.dispose()
