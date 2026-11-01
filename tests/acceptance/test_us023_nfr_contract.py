"""US-023 Manager NFRs and carry-overs.

    * A trade whose exit is unpriced (exit NULL) keeps costs/net NULL: never a fake zero.
    * NO_FILL rows keep zero costs/net (and are not trades for the ledger).
    * Backfill: CLOSED rows written by US-002 with costs/net NULL get them computed ONCE at startup
      (idempotent), so the Paper panel shows net for today.
    * Carry-over: `PaperTrade.from_row` never truncates non-integer money (unknown, i.e. None,
      instead).
    * Carry-over: `PaperBook._load_active` skips and counts a corrupt row instead of failing to
      start.

Rows "as US-002 wrote them" are made by closing real trades and then setting costs and net back
to NULL, so the fixture does not depend on how the new close books them.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest
import us001_helpers as H
import us002_helpers as U
import us023_helpers as W
from sqlalchemy import text

NAN, INF = float("nan"), float("inf")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


class TestAnUnpricedExitKeepsCostsAndNetNull:
    @pytest.mark.parametrize(
        "bad", [None, NAN, 0.0, RuntimeError("pricer down")], ids=["none", "nan", "zero", "raises"]
    )
    def test_an_option_whose_exit_cannot_be_priced_books_no_money_at_all(self, db, bad):
        pricer = W.SwitchablePricer(bad)
        book = W.make_book(db.engine, premium_at=pricer)
        alert = U.make_alert("O1", kind="INDEX", direction="long")
        H.open_trade(book, alert)
        fill_then_target = U.pbars(H.at(10, 5), [W.OPT_PRE, *W.OPTION_SCENARIOS["ce_target"][1]])
        U.step_book(book, fill_then_target, H.at(10, 7), series="BANKNIFTY")
        assert U.row_of(db.engine, "O1")["status"] == "OPEN"
        pricer.failing = True
        U.step_book(book, fill_then_target, H.at(10, 9), series="BANKNIFTY")
        row = U.row_of(db.engine, "O1")
        assert (row["status"], row["close_reason"]) == ("CLOSED", "TARGET")
        for column in ("exit_paise", "gross_paise", "costs_paise", "net_paise"):
            assert row[column] is None, f"{column} must stay NULL, never a fake 0"
        assert row["entry_paise"] == 45280 and row["risk_paise"] == 307650
        assert W.pricing_events(db.engine, "O1", leg="exit")

    def test_a_stock_closed_with_no_usable_bar_books_no_money_either(self, db):
        W.unpriced_stock_close(db.engine)
        row = U.row_of(db.engine, "U1")
        assert (row["status"], row["close_reason"]) == ("CLOSED", "SESSION_END")
        for column in ("exit_paise", "gross_paise", "costs_paise", "net_paise"):
            assert row[column] is None, column
        assert row["entry_paise"] == 100030, "the fill is real; only the exit is unknown"

    def test_an_unpriced_close_is_not_a_trade_the_ledger_counts(self, db):
        W.unpriced_stock_close(db.engine)
        totals = W.make_ledger(db.engine).totals()
        assert W.as_dict(totals) == dict.fromkeys(W.LEDGER_FIELDS, 0)


class TestNoFillRowsKeepZeroMoney:
    @pytest.mark.parametrize("name", list(W.NO_FILLS))
    def test_gross_costs_and_net_are_exact_zero_not_null(self, db, name):
        W.NO_FILLS[name](db.engine)
        (row,) = H.trade_rows(db.engine)
        assert row["status"] == "NO_FILL"
        assert (row["gross_paise"], row["costs_paise"], row["net_paise"]) == (0, 0, 0)
        assert row["entry_paise"] is None and row["exit_paise"] is None
        for column in ("gross_paise", "costs_paise", "net_paise"):
            assert W.typeofs(db.engine, column) == ["integer"], column

    @pytest.mark.parametrize("name", list(W.NO_FILLS))
    def test_a_no_fill_is_not_a_trade_for_the_ledger(self, db, name):
        W.NO_FILLS[name](db.engine)
        assert W.make_ledger(db.engine).totals().trades == 0

    def test_a_no_fill_never_asks_the_cost_model_anything(self, db):
        recorder = W.RecordingCosts()
        W.no_fill_gap(db.engine, cost_for=recorder.cost_for)
        assert recorder.legs == [] and H.trade_rows(db.engine)[0]["status"] == "NO_FILL"

    @pytest.mark.parametrize("name", list(W.NO_FILLS))
    def test_a_restart_keeps_the_zeros(self, db, name):
        W.NO_FILLS[name](db.engine)
        before = H.trade_rows(db.engine)
        W.make_book(db.engine)
        assert H.trade_rows(db.engine) == before


def legacy_database(db):
    """Six closed trades, a priced-exit-less close, a NO_FILL, an OPEN trade and a PENDING one,
    with the six closed rows turned back into what US-002 wrote (costs and net NULL)."""
    _, closed = W.close_many(db.engine)
    W.unpriced_stock_close(db.engine, "U1", key="INFY")
    W.no_fill_gap(db.engine, "G1")
    book = W.make_book(db.engine)
    H.open_trade(book, W.stock_alert("OPEN1", key="SBIN"))
    H.open_trade(book, W.stock_alert("PEND1", key="INFY"))
    quiet = U.pbars(H.at(10, 5), [U.PRE_BAR, *W.stock_rows(W.stock_alert("OPEN1"), "quiet")])
    book.step(H.at(10, 8), H.Feed(SBIN=quiet))
    W.null_out_costs(db.engine)
    return closed


def assert_backfilled(db, closed):
    """The legacy CLOSED rows now carry costs and net (so the idempotence and 'left alone'
    checks below can never pass just because nothing was ever done)."""
    for legacy in closed:
        row = U.row_of(db.engine, legacy["alert_id"])
        assert row["costs_paise"] is not None and row["net_paise"] is not None, row["alert_id"]


class TestStartupBackfill:
    def test_a_new_book_computes_costs_and_net_for_every_legacy_closed_row(self, db):
        closed = legacy_database(db)
        assert all(
            r["costs_paise"] is None for r in H.trade_rows(db.engine) if r["status"] == "CLOSED"
        )
        W.make_book(db.engine)
        for legacy in closed:
            row = U.row_of(db.engine, legacy["alert_id"])
            gross, costs, net = W.money_of(row)
            assert row["costs_paise"] == costs, row["alert_id"]
            assert row["net_paise"] == net == row["gross_paise"] - costs, row["alert_id"]
            assert row["gross_paise"] == gross

    def test_nothing_else_about_those_rows_changes(self, db):
        closed = legacy_database(db)
        before = W.rows_without(db.engine, "costs_paise", "net_paise", "updated_at")
        events = H.event_rows(db.engine)
        W.make_book(db.engine)
        assert_backfilled(db, closed)
        assert W.rows_without(db.engine, "costs_paise", "net_paise", "updated_at") == before
        kept = H.event_rows(db.engine)[: len(events)]
        assert kept == events, "no existing event is changed or removed"
        closes = [e for e in H.event_rows(db.engine) if str(H.val(e["kind"])) == "CLOSED"]
        assert len(closes) == 7, (
            "six priced closes and the unpriced one: the backfill closes nothing"
        )

    def test_running_it_twice_changes_nothing_more(self, db):
        closed = legacy_database(db)
        W.make_book(db.engine)
        assert_backfilled(db, closed)
        once = W.rows_without(db.engine, "updated_at")
        events = H.event_rows(db.engine)
        W.make_book(db.engine)
        W.make_book(db.engine)
        assert W.rows_without(db.engine, "updated_at") == once
        assert H.event_rows(db.engine) == events

    def test_it_is_computed_once_a_later_start_with_other_rates_never_recomputes(self, db):
        closed = legacy_database(db)
        W.make_book(db.engine)
        first = {r["alert_id"]: (r["costs_paise"], r["net_paise"]) for r in H.trade_rows(db.engine)}
        W.make_book(db.engine, cost_for=W.loud_cost_for())
        later = {r["alert_id"]: (r["costs_paise"], r["net_paise"]) for r in H.trade_rows(db.engine)}
        assert later == first
        assert first[closed[0]["alert_id"]][0] is not None

    def test_rows_that_must_stay_as_they_are_stay_as_they_are(self, db):
        closed = legacy_database(db)
        W.make_book(db.engine)
        assert_backfilled(db, closed)
        rows = {r["alert_id"]: r for r in H.trade_rows(db.engine)}
        unpriced = rows["U1"]
        assert unpriced["status"] == "CLOSED" and unpriced["exit_paise"] is None
        assert (unpriced["gross_paise"], unpriced["costs_paise"], unpriced["net_paise"]) == (
            None,
            None,
            None,
        ), "an unpriced exit is still unknown: never a fake zero"
        nofill = rows["G1"]
        assert (nofill["gross_paise"], nofill["costs_paise"], nofill["net_paise"]) == (0, 0, 0)
        for alert_id in ("OPEN1", "PEND1"):
            assert rows[alert_id]["costs_paise"] is None and rows[alert_id]["net_paise"] is None

    def test_the_backfill_books_what_the_close_books_for_the_same_trade(self, db, tmp_path):
        """Two databases, the same six trades: one closes natively, the other is backfilled."""
        _, native = W.close_many(db.engine)
        other = H.new_db(tmp_path, "legacy.db")
        try:
            W.close_many(other.engine)
            W.null_out_costs(other.engine)
            W.make_book(other.engine)
            for row in native:
                twin = U.row_of(other.engine, row["alert_id"])
                assert row["costs_paise"] is not None and twin["costs_paise"] is not None
                assert (twin["costs_paise"], twin["net_paise"]) == (
                    row["costs_paise"],
                    row["net_paise"],
                ), row["alert_id"]
        finally:
            other.dispose()

    def test_every_paise_column_is_still_an_sql_integer_afterwards(self, db):
        legacy_database(db)
        W.make_book(db.engine)
        for column in ("costs_paise", "net_paise", "gross_paise"):
            kinds = W.typeofs(db.engine, column)
            assert set(kinds) <= {"integer", "null"}, (column, kinds)
        priced = [r for r in H.trade_rows(db.engine) if r["status"] == "CLOSED" and r["exit_paise"]]
        assert len(priced) == 6 and all(type(r["costs_paise"]) is int for r in priced)

    def test_the_ledger_sees_the_backfilled_trades(self, db):
        closed = legacy_database(db)
        assert W.make_ledger(db.engine).totals().trades == 0, "NULL net: not yet a ledger trade"
        W.make_book(db.engine)
        totals = W.make_ledger(db.engine).totals()
        money = [W.money_of(r) for r in closed]
        assert totals.trades == 6
        assert totals.net_paise == int(sum(Decimal(n) for _, _, n in money))

    def test_three_hundred_legacy_rows_are_all_backfilled_exactly(self, db):
        legacy = [
            W.Booked(f"L{i:03d}", "CLOSED", g, None, None, "live", H.at(10, 7))
            for i, g in enumerate(range(-3000, 3000, 20))
        ]
        W.insert_booked(db.engine, legacy)
        W.make_book(db.engine)
        model = W.model_for("SBIN")
        checked = 0
        for row in H.trade_rows(db.engine):
            try:
                costs = W.expected_costs(
                    model, "STOCK", "long", row["entry_paise"], row["exit_paise"], 1
                )
            except W.Tie:  # a leg total on a half-paisa under two readings: not this test's rule
                continue
            assert (row["costs_paise"], row["net_paise"]) == (costs, row["gross_paise"] - costs)
            checked += 1
        assert checked >= 290
        assert W.make_ledger(db.engine).totals().trades == 300


class TestFromRowNeverTruncatesMoney:
    @pytest.fixture
    def stored(self, db):
        book = W.make_book(db.engine)
        H.open_trade(book, W.stock_alert("A1"))
        money = {
            "entry_paise": 100030,
            "exit_paise": 102000,
            "gross_paise": 1970,
            "costs_paise": 107,
            "net_paise": 1863,
            "risk_paise": 1030,
        }
        return {**H.trade_row(db.engine, "A1"), **money}  # from_row does not judge the status

    @staticmethod
    def from_row(row):
        from onelot.paper.book import PaperTrade

        return PaperTrade.from_row(row)

    @pytest.mark.parametrize("column", W.PAISE_COLUMNS)
    @pytest.mark.parametrize(
        "value",
        [1205.7, -0.5, 1e-9, 0.99999, 100030.5, "12.7", Decimal("12.5"), NAN, INF, True],
        ids=["1205.7", "-0.5", "1e-9", "0.99999", "x.5", "str", "decimal", "nan", "inf", "bool"],
    )
    def test_a_non_integer_amount_is_unknown_never_truncated(self, stored, column, value):
        trade = self.from_row({**stored, column: value})
        assert getattr(trade, column) is None, f"{column}={value!r} must read as unknown (None)"
        for other in W.PAISE_COLUMNS:
            if other != column:
                assert getattr(trade, other) == stored[other], other

    @pytest.mark.parametrize("column", W.PAISE_COLUMNS)
    @pytest.mark.parametrize("value", [0, -7, 100030, 2**62, -(2**62)])
    def test_an_integer_amount_is_kept_exactly(self, stored, column, value):
        trade = self.from_row({**stored, column: value})
        assert getattr(trade, column) == value and type(getattr(trade, column)) is int

    def test_a_null_amount_stays_null(self, stored):
        trade = self.from_row({**stored, "costs_paise": None, "net_paise": None})
        assert trade.costs_paise is None and trade.net_paise is None

    def test_a_book_loading_a_row_with_a_fractional_amount_does_not_truncate_it(self, db):
        book = W.make_book(db.engine)
        H.open_trade(book, W.stock_alert("A1"))
        frame = U.pbars(H.at(10, 5), [U.PRE_BAR, *W.stock_rows(W.stock_alert("A1"), "quiet")])
        U.step_book(book, frame, H.at(10, 8))
        with db.engine.begin() as conn:
            conn.execute(text("UPDATE paper_trades SET entry_paise = 100030.7 WHERE alert_id='A1'"))
        reborn = W.make_book(db.engine)
        (trade,) = reborn.open_trades()
        assert trade.alert_id == "A1" and trade.entry_paise is None


CORRUPTIONS = {
    "unknown_grade": "UPDATE paper_trades SET grade = 'sideways' WHERE alert_id = :a",
    "plan_is_not_json": "UPDATE paper_trades SET plan = '{not json' WHERE alert_id = :a",
    "time_is_garbage": "UPDATE paper_trades SET created_at = 'garbage' WHERE alert_id = :a",
}


def raw_row(engine, alert_id):
    with engine.connect() as conn:
        return tuple(
            conn.execute(
                text("SELECT * FROM paper_trades WHERE alert_id = :a"), {"a": alert_id}
            ).one()
        )


class TestLoadActiveSkipsAndCountsACorruptRow:
    def seed(self, db, names):
        """GOOD plus one waiting trade per corruption, then each corrupted in place."""
        book = W.make_book(db.engine)
        H.open_trade(book, W.stock_alert("GOOD", key="SBIN"))
        for i, name in enumerate(names):
            alert_id = f"BAD{i}"
            H.open_trade(book, W.stock_alert(alert_id, key="RELIANCE"))
            with db.engine.begin() as conn:
                conn.execute(text(CORRUPTIONS[name]), {"a": alert_id})
        return [f"BAD{i}" for i in range(len(names))]

    @pytest.mark.parametrize("name", list(CORRUPTIONS))
    def test_the_book_starts_skips_the_row_and_counts_it(self, db, name):
        (bad,) = self.seed(db, [name])
        book = W.make_book(db.engine)  # must not raise
        assert [t.alert_id for t in book.open_trades()] == ["GOOD"]
        assert W.counted_problems(book) == 1, book.status()
        assert book.status()["opened"] == 0, "loading is not opening"
        assert bad not in {t.alert_id for t in book.open_trades()}

    @pytest.mark.parametrize("name", list(CORRUPTIONS))
    def test_the_row_is_logged_and_left_exactly_as_it_was(self, db, name, caplog):
        (bad,) = self.seed(db, [name])
        before = raw_row(db.engine, bad)
        with caplog.at_level(logging.WARNING, logger="onelot.paper"):
            W.make_book(db.engine)
        assert raw_row(db.engine, bad) == before, "a corrupt row is never repaired or deleted"
        names = (bad, f"trade {before[0]}", f"id={before[0]}", f"id {before[0]}")
        assert any(n in r.getMessage() for r in caplog.records for n in names), (
            "the log names the corrupt row (its alert id, or its row id)"
        )

    def test_every_corrupt_row_is_counted_and_the_good_ones_all_load(self, db):
        self.seed(db, list(CORRUPTIONS) * 2)
        book = W.make_book(db.engine)
        assert [t.alert_id for t in book.open_trades()] == ["GOOD"]
        assert W.counted_problems(book) == 6

    def test_the_count_is_per_book_so_a_second_start_counts_the_same_rows_again(self, db):
        self.seed(db, ["unknown_grade", "plan_is_not_json"])
        assert W.counted_problems(W.make_book(db.engine)) == 2
        assert W.counted_problems(W.make_book(db.engine)) == 2

    def test_a_clean_database_counts_nothing(self, db):
        book = W.make_book(db.engine)
        H.open_trade(book, W.stock_alert("GOOD"))
        assert W.counted_problems(W.make_book(db.engine)) == 0

    def test_the_book_that_started_around_a_corrupt_row_still_works(self, db):
        self.seed(db, ["unknown_grade"])
        book = W.make_book(db.engine)
        H.open_trade(book, W.stock_alert("NEW1", key="INFY"))
        frame = U.pbars(H.at(10, 5), [U.PRE_BAR, *W.stock_rows(W.stock_alert("GOOD"), "target")])
        events = book.step(H.at(10, 9), H.Feed(SBIN=frame))
        assert [str(e.kind) for e in events if str(e.kind) == "CLOSED"] == ["CLOSED"]
        row = U.row_of(db.engine, "GOOD")
        assert row["status"] == "CLOSED" and row["costs_paise"] is not None
        assert {t.alert_id for t in book.open_trades()} == {"NEW1"}
