"""US-023 AC2: `PaperLedger.totals` sums closed trades exactly, in integer paise.

    src/onelot/paper/ledger.py
    (frozen=True) class LedgerTotals: trades, wins, losses, gross_paise, costs_paise, net_paise
    class PaperLedger: __init__(engine); totals(*, grade=None, since=None, until=None) -> LedgerTotals
                       over the CLOSED rows whose net is not NULL

`totals(grade=None).net_paise` equals a Decimal-oracle sum EXACTLY (no float is ever summed),
does not depend on the order the trades were booked, and the SQL type of every paise column is
`integer`. Two sources of truth are used: the real book closing six trades (the oracle re-derives
each trade's money from its entry and exit), and rows inserted straight into `paper_trades`, so
a test can state any sequence of closed trades, including sums far beyond 2**53 where a float
sum is visibly wrong.

Windows (`since`, `until`) are tested on trades whose every timestamp is the same instant and
that sit well inside or outside the window, so the answer does not depend on WHICH timestamp
the ledger filters on; the one place it does (a trade exactly on a boundary) is pinned
separately in test_us023_pinned_proposals.py.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import inspect
from decimal import Decimal

import pytest
import us001_helpers as H
import us002_helpers as U
import us023_helpers as W

from onelot.clock import IST


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


def at(day_offset: int, hour: int = 10, minute: int = 0) -> dt.datetime:
    day = W.IST_DAY + dt.timedelta(days=day_offset)
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=IST)


class TestLedgerTotalsIsAFrozenValueWithTheContractFields:
    def test_it_is_a_frozen_dataclass_of_six_ints_in_the_contract_order(self):
        cls = W.ledger_module().LedgerTotals
        assert dataclasses.is_dataclass(cls)
        assert cls.__dataclass_params__.frozen is True
        assert [f.name for f in dataclasses.fields(cls)] == list(W.LEDGER_FIELDS)
        assert all(f.type in (int, "int") for f in dataclasses.fields(cls))

    def test_it_cannot_be_changed_after_it_is_built(self):
        cls = W.ledger_module().LedgerTotals
        totals = cls(trades=1, wins=1, losses=0, gross_paise=10, costs_paise=2, net_paise=8)
        with pytest.raises(dataclasses.FrozenInstanceError):
            totals.net_paise = 9  # type: ignore[misc]
        assert totals == cls(1, 1, 0, 10, 2, 8) and hash(totals) == hash(cls(1, 1, 0, 10, 2, 8))

    def test_totals_takes_keyword_only_grade_since_until_defaulting_to_none(self):
        parameters = inspect.signature(W.ledger_module().PaperLedger.totals).parameters
        assert list(parameters)[1:] == ["grade", "since", "until"]
        for name in ("grade", "since", "until"):
            assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
            assert parameters[name].default is None

    def test_the_ledger_is_built_from_an_engine_alone(self, db):
        ledger = W.make_ledger(db.engine)
        assert isinstance(ledger, W.ledger_module().PaperLedger)
        assert list(inspect.signature(W.ledger_module().PaperLedger).parameters) == ["engine"]


class TestAnEmptyLedger:
    def test_no_trades_is_six_plain_zeros_not_nones(self, db):
        totals = W.make_ledger(db.engine).totals()
        assert W.as_dict(totals) == dict.fromkeys(W.LEDGER_FIELDS, 0)
        assert all(type(getattr(totals, name)) is int for name in W.LEDGER_FIELDS)


class TestTotalsOverTheBooksOwnClosedTrades:
    """Six real closes (three winners, three losers; stock long and short; option CE and PE)."""

    def test_net_equals_the_decimal_oracle_sum_of_each_trades_net(self, db):
        _, rows = W.close_many(db.engine)
        money = [W.money_of(r) for r in rows]
        totals = W.make_ledger(db.engine).totals(grade=None)
        assert totals.net_paise == int(sum(Decimal(net) for _, _, net in money))
        assert totals.gross_paise == int(sum(Decimal(g) for g, _, _ in money))
        assert totals.costs_paise == int(sum(Decimal(c) for _, c, _ in money))
        assert totals.net_paise == totals.gross_paise - totals.costs_paise

    def test_it_counts_six_trades_three_wins_three_losses(self, db):
        W.close_many(db.engine)
        totals = W.make_ledger(db.engine).totals()
        assert (totals.trades, totals.wins, totals.losses) == (6, 3, 3)

    def test_it_agrees_with_the_columns_the_book_wrote(self, db):
        _, rows = W.close_many(db.engine)
        totals = W.make_ledger(db.engine).totals()
        assert totals.gross_paise == sum(r["gross_paise"] for r in rows)
        assert totals.costs_paise == sum(r["costs_paise"] for r in rows)
        assert totals.net_paise == sum(r["net_paise"] for r in rows)

    def test_totals_are_plain_python_ints(self, db):
        W.close_many(db.engine)
        totals = W.make_ledger(db.engine).totals()
        assert all(type(getattr(totals, name)) is int for name in W.LEDGER_FIELDS)

    def test_reading_the_ledger_writes_nothing(self, db):
        W.close_many(db.engine)
        before = H.snapshot_tables(db.engine)
        W.make_ledger(db.engine).totals()
        W.make_ledger(db.engine).totals(grade="live")
        assert H.snapshot_tables(db.engine) == before

    def test_a_ledger_over_a_book_sees_a_trade_closed_after_it_was_built(self, db):
        ledger = W.make_ledger(db.engine)
        assert ledger.totals().trades == 0
        W.close_many(db.engine)
        assert ledger.totals().trades == 6, "it reads the database each time, never a snapshot"


class TestEverySqlColumnIsAnInteger:
    def test_typeof_is_integer_for_every_paise_column_of_a_closed_row(self, db):
        W.close_many(db.engine)
        for column in W.PAISE_COLUMNS:
            assert W.typeofs(db.engine, column) == ["integer"] * 6, column

    def test_costs_and_net_are_stored_as_integers_not_reals_for_odd_prices(self, db):
        """INFY at 1650.35: a float leg total would be stored as a REAL if it were not rounded."""
        W.run_stock(db.engine, key="INFY", entry=1650.35, hit="stop")
        for column in ("costs_paise", "net_paise", "gross_paise"):
            assert W.typeofs(db.engine, column) == ["integer"], column

    def test_an_unpriced_closes_money_is_null_not_zero_or_text(self, db):
        """Whatever is not known stays SQL NULL; there is no 'real', 'text' or fake zero."""
        W.close_many(db.engine)
        book = W.make_book(db.engine)
        alert = W.stock_alert("X1")
        H.open_trade(book, alert)
        frame = H.minute_bars(H.at(10, 5), [1000.0, 1000.0])
        frame["high"] = frame["low"] = frame["close"] = float("nan")
        book.step(H.at(10, 8), H.Feed(SBIN=frame))
        book.step(H.at(15, 30), H.Feed())
        row = U.row_of(db.engine, "X1")
        assert row["status"] == "CLOSED" and row["exit_paise"] is None
        kinds = {c: W.typeofs(db.engine, c)[-1] for c in W.PAISE_COLUMNS}
        assert kinds["exit_paise"] == kinds["gross_paise"] == "null"
        assert kinds["costs_paise"] == kinds["net_paise"] == "null"
        assert kinds["entry_paise"] == kinds["risk_paise"] == "integer"


class TestWhichRowsCount:
    def seed(self, db):
        t = at(1)
        items = [
            W.booked("C1", 5000, 120, ts=t),
            W.booked("C2", -3000, 100, ts=t),
            W.booked("C3", 700, 90, ts=t),
            # not trades the ledger sums:
            W.Booked("OPEN1", "OPEN", None, None, None, "live", t),
            W.Booked("PEND1", "PENDING_FILL", None, None, None, "live", t),
            W.Booked("NOFILL1", "NO_FILL", 0, 0, 0, "live", t),  # zero money, never a trade
            W.Booked("UNPRICED1", "CLOSED", None, None, None, "live", t),  # exit unpriced: no net
        ]
        W.insert_booked(db.engine, items)
        return items

    def test_only_closed_rows_with_a_net_are_summed(self, db):
        items = self.seed(db)
        totals = W.make_ledger(db.engine).totals()
        assert W.as_dict(totals) == W.oracle_totals(items)
        assert (totals.trades, totals.wins, totals.losses) == (3, 2, 1)
        assert totals.net_paise == (5000 - 120) + (-3000 - 100) + (700 - 90)

    def test_a_no_fill_is_not_a_trade_even_though_its_money_is_zero(self, db):
        self.seed(db)
        assert W.make_ledger(db.engine).totals().trades == 3

    def test_a_closed_row_whose_exit_was_never_priced_is_left_out_not_counted_as_zero(self, db):
        W.insert_booked(db.engine, [W.Booked("U1", "CLOSED", None, None, None, "live", at(1))])
        assert W.as_dict(W.make_ledger(db.engine).totals()) == dict.fromkeys(W.LEDGER_FIELDS, 0)


class TestWinsAndLosses:
    def test_a_win_is_net_above_zero_not_gross_above_zero(self, db):
        W.insert_booked(
            db.engine,
            [
                W.booked("W1", 100, 40, ts=at(1)),  # gross +100, net +60: win
                W.booked("L1", 100, 140, ts=at(1)),  # gross +100, net -40: LOSS
                W.booked("L2", -5, 10, ts=at(1)),  # loss
            ],
        )
        totals = W.make_ledger(db.engine).totals()
        assert (totals.trades, totals.wins, totals.losses) == (3, 1, 2)

    def test_wins_and_losses_split_the_trades(self, db):
        items = [W.booked(f"T{i}", g, 25, ts=at(1)) for i, g in enumerate([500, -500, 26, -26, 30])]
        W.insert_booked(db.engine, items)
        totals = W.make_ledger(db.engine).totals()
        assert totals.wins + totals.losses == totals.trades == 5
        assert (totals.wins, totals.losses) == (3, 2)


class TestFilters:
    def seed(self, db):
        items = [
            W.booked("A", 1000, 100, ts=at(0), grade="live"),
            W.booked("B", 2000, 100, ts=at(1), grade="delayed"),
            W.booked("C", -500, 100, ts=at(1), grade="live"),
            W.booked("D", 400, 100, ts=at(2), grade="delayed"),
            W.booked("E", -700, 100, ts=at(3), grade="live"),
        ]
        W.insert_booked(db.engine, items)
        return items

    @pytest.mark.parametrize("grade", ["live", "delayed"])
    def test_grade_keeps_only_that_grade(self, db, grade):
        items = self.seed(db)
        totals = W.make_ledger(db.engine).totals(grade=grade)
        assert W.as_dict(totals) == W.oracle_totals(items, grade=grade)
        assert totals.trades == (3 if grade == "live" else 2)

    def test_grade_none_is_every_grade(self, db):
        items = self.seed(db)
        assert W.as_dict(W.make_ledger(db.engine).totals(grade=None)) == W.oracle_totals(items)
        assert W.make_ledger(db.engine).totals().trades == 5

    def test_the_two_grades_add_up_to_the_whole(self, db):
        self.seed(db)
        ledger = W.make_ledger(db.engine)
        live, delayed, whole = (
            ledger.totals(grade="live"),
            ledger.totals(grade="delayed"),
            ledger.totals(),
        )
        for name in W.LEDGER_FIELDS:
            assert getattr(live, name) + getattr(delayed, name) == getattr(whole, name), name

    @pytest.mark.parametrize(
        "since, until",
        [
            (at(1, 0, 0), None),
            (None, at(2, 0, 0)),
            (at(1, 0, 0), at(3, 0, 0)),
            (at(2, 0, 0), at(2, 23, 59)),
            (at(4, 0, 0), None),
            (None, at(0, 0, 0)),
        ],
    )
    def test_the_window_keeps_the_trades_inside_it(self, db, since, until):
        items = self.seed(db)
        totals = W.make_ledger(db.engine).totals(since=since, until=until)
        assert W.as_dict(totals) == W.oracle_totals(items, since=since, until=until)

    def test_grade_and_window_combine(self, db):
        items = self.seed(db)
        totals = W.make_ledger(db.engine).totals(grade="live", since=at(1), until=at(3))
        assert W.as_dict(totals) == W.oracle_totals(items, grade="live", since=at(1), until=at(3))
        assert (totals.trades, totals.net_paise) == (1, -600)

    def test_a_window_with_no_trades_is_zero(self, db):
        self.seed(db)
        totals = W.make_ledger(db.engine).totals(since=at(10), until=at(11))
        assert W.as_dict(totals) == dict.fromkeys(W.LEDGER_FIELDS, 0)

    def test_the_window_is_judged_on_instants_not_on_the_timezone_it_is_written_in(self, db):
        """00:10 IST on the 29th is 18:40 UTC on the 28th: a window opening at 00:00 IST on the
        29th must keep it and one opening at 00:00 IST on the 30th must not, whether `since` is
        written in IST or in UTC. A date taken in the wrong zone gets this wrong."""
        just_after_midnight = dt.datetime.combine(dt.date(2026, 10, 29), dt.time(0, 10), tzinfo=IST)
        just_before_midnight = dt.datetime.combine(
            dt.date(2026, 10, 28), dt.time(23, 50), tzinfo=IST
        )
        items = [
            W.booked("EARLY", 900, 100, ts=just_before_midnight),
            W.booked("LATE", 1100, 100, ts=just_after_midnight),
        ]
        W.insert_booked(db.engine, items)
        midnight_ist = dt.datetime.combine(dt.date(2026, 10, 29), dt.time(0, 0), tzinfo=IST)
        ledger = W.make_ledger(db.engine)
        for since in (midnight_ist, midnight_ist.astimezone(dt.timezone.utc)):
            totals = ledger.totals(since=since)
            assert (totals.trades, totals.net_paise) == (1, 1000), since
        next_midnight = midnight_ist + dt.timedelta(days=1)
        for until in (next_midnight, next_midnight.astimezone(dt.timezone.utc)):
            totals = ledger.totals(until=until)
            assert totals.trades == 2, until


class TestTheLedgerRefusesWhatItCannotAnswer:
    """Manager decision 4: an unknown grade and a window with since > until are refused (a
    ValueError), never answered with an empty total; an EMPTY window [t, t) is just zero."""

    @pytest.mark.parametrize("grade", ["Live", "LIVE", "Delayed", "paper", "", " live", "yahoo"])
    def test_a_grade_the_book_never_writes_is_a_value_error(self, db, grade):
        W.insert_booked(db.engine, [W.booked("W1", 970, 107, ts=at(1))])
        with pytest.raises(ValueError, match="grade"):
            W.make_ledger(db.engine).totals(grade=grade)

    def test_the_two_real_grades_and_none_are_accepted(self, db):
        W.insert_booked(db.engine, [W.booked("W1", 970, 107, ts=at(1))])
        ledger = W.make_ledger(db.engine)
        for grade in ("live", "delayed", None):
            ledger.totals(grade=grade)

    @pytest.mark.parametrize("gap_minutes", [1, 60, 24 * 60])
    def test_since_after_until_is_a_value_error(self, db, gap_minutes):
        W.insert_booked(db.engine, [W.booked("W1", 970, 107, ts=at(1))])
        until = at(1, 10, 0)
        since = until + dt.timedelta(minutes=gap_minutes)
        with pytest.raises(ValueError, match="since"):
            W.make_ledger(db.engine).totals(since=since, until=until)

    def test_an_empty_window_is_zero_not_an_error(self, db):
        """[t, t) holds nothing: even a trade exactly at t is outside it."""
        W.insert_booked(db.engine, [W.booked("W1", 970, 107, ts=at(1, 10, 0))])
        totals = W.make_ledger(db.engine).totals(since=at(1, 10, 0), until=at(1, 10, 0))
        assert W.as_dict(totals) == dict.fromkeys(W.LEDGER_FIELDS, 0)

    def test_the_same_window_in_two_timezones_is_still_the_same_instant(self, db):
        """An inverted window is judged on instants: 10:00 IST and 04:30 UTC are the same t."""
        W.insert_booked(db.engine, [W.booked("W1", 970, 107, ts=at(1, 10, 0))])
        t_ist = at(1, 10, 0)
        t_utc = t_ist.astimezone(dt.timezone.utc)
        totals = W.make_ledger(db.engine).totals(since=t_ist, until=t_utc)
        assert totals.trades == 0

    def test_a_refused_call_changes_nothing(self, db):
        W.insert_booked(db.engine, [W.booked("W1", 970, 107, ts=at(1))])
        before = H.snapshot_tables(db.engine)
        ledger = W.make_ledger(db.engine)
        with pytest.raises(ValueError):
            ledger.totals(grade="bogus")
        assert H.snapshot_tables(db.engine) == before
        assert ledger.totals().trades == 1, "and the ledger still answers"


class TestTheSumIsExact:
    def test_a_float_sum_would_lose_the_unit_above_2_to_the_53(self, db):
        """2**60 + 1 - 2**60 + 3: a float sum gives 0 (or 4); the exact sum is 4 and 3 + 1."""
        big = 2**60
        items = [
            W.booked("B1", big + 1, 0, ts=at(1)),
            W.booked("B2", -big, 0, ts=at(1)),
            W.booked("B3", 3, 0, ts=at(1)),
        ]
        W.insert_booked(db.engine, items)
        totals = W.make_ledger(db.engine).totals()
        assert totals.net_paise == 4 == int(sum(Decimal(i.net or 0) for i in items))
        assert totals.gross_paise == 4 and totals.costs_paise == 0
        assert type(totals.net_paise) is int

    def test_a_thousand_one_paisa_trades_sum_to_exactly_a_thousand_paise(self, db):
        W.insert_booked(db.engine, [W.booked(f"P{i:04d}", 2, 1, ts=at(1)) for i in range(1000)])
        totals = W.make_ledger(db.engine).totals()
        assert (totals.trades, totals.wins, totals.losses) == (1000, 1000, 0)
        assert (totals.gross_paise, totals.costs_paise, totals.net_paise) == (2000, 1000, 1000)

    def test_net_is_gross_minus_costs_over_any_book(self, db):
        items = [
            W.booked(f"N{i}", g, c, ts=at(1 + i % 3))
            for i, (g, c) in enumerate([(123456789012345, 987), (-98765432109876, 5), (7, 3)])
        ]
        W.insert_booked(db.engine, items)
        totals = W.make_ledger(db.engine).totals()
        assert totals.net_paise == totals.gross_paise - totals.costs_paise
        assert W.as_dict(totals) == W.oracle_totals(items)


class TestTheOrderTradesWereBookedIsIrrelevant:
    def test_two_orders_of_the_same_trades_give_the_same_totals(self, db, tmp_path):
        items = [
            W.booked(f"R{i}", g, c, ts=at(1 + i % 3), grade="live" if i % 2 else "delayed")
            for i, (g, c) in enumerate([(5000, 120), (-3000, 100), (700, 90), (-90, 1), (250, 40)])
        ]
        W.insert_booked(db.engine, items)
        other = H.new_db(tmp_path, "reversed.db")
        try:
            W.insert_booked(other.engine, list(reversed(items)))
            first, second = W.make_ledger(db.engine), W.make_ledger(other.engine)
            for kwargs in (
                {},
                {"grade": "live"},
                {"since": at(2)},
                {"grade": "delayed", "until": at(2)},
            ):
                assert first.totals(**kwargs) == second.totals(**kwargs), kwargs
        finally:
            other.dispose()
