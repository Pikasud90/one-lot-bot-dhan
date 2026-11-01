"""US-001 AC1: every alert opens exactly one PENDING_FILL paper trade.

    on_alert(alert, now) -> PaperTrade | None

An alert of ANY status and strategy, stock or index option, becomes exactly one
`paper_trades` row (status PENDING_FILL) recording alert id, instrument, side, strategy,
timeframe, confidence score, data source and grade (live iff `data.source` starts with
"dhan", otherwise delayed). A second call with the same alert id returns the existing trade
and creates no row; mutating the alert dict afterwards changes nothing that is stored.

The book is reached through its public contract only (`onelot.paper.book.PaperBook`,
tables in `onelot.store.schema`). Money columns are integer paise.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt

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


SHAPES = [
    pytest.param({"kind": "STOCK", "direction": "long"}, 1, id="stock-long-BUY"),
    pytest.param({"kind": "STOCK", "direction": "short"}, 1, id="stock-short-SELL"),
    pytest.param({"kind": "INDEX", "direction": "long"}, H.LOT, id="index-long-BUY_CE"),
    pytest.param({"kind": "INDEX", "direction": "short"}, H.LOT, id="index-short-BUY_PE"),
]
STATUSES = ["PENDING", "TAKEN", "SKIPPED", "EXPIRED", "CLOSED"]


class TestOneRowPerAlert:
    @pytest.mark.parametrize("status", STATUSES)
    @pytest.mark.parametrize("shape, qty", SHAPES)
    def test_an_alert_of_any_status_and_shape_opens_one_pending_fill_row(
        self, db, shape, qty, status
    ):
        book = H.make_book(db.engine)
        alert = H.make_alert("A1", status=status, **shape)
        trade = book.on_alert(copy.deepcopy(alert), H.CREATED)
        assert trade is not None
        rows = H.trade_rows(db.engine)
        assert len(rows) == 1
        row = rows[0]
        eco = alert["economics"]
        assert row["alert_id"] == "A1"
        assert H.val(row["status"]) == "PENDING_FILL"
        assert row["close_reason"] is None
        assert row["instrument_key"] == alert["instrument_key"]
        assert row["series_key"] == alert["series_key"]
        assert row["kind"] == shape["kind"]
        assert row["side"] == eco["side"] == alert["side"]
        assert row["direction"] == shape["direction"]
        assert row["strategy"] == "ema_cross" and row["timeframe"] == "10m"
        assert row["confidence"] == pytest.approx(61.5)
        # the cost product: spec.product wins ("index_options" for options, lab naming)
        assert row["product"] == H.SPECS[alert["instrument_key"]].product
        assert row["product"] == (
            "equity_intraday" if shape["kind"] == "STOCK" else "index_options"
        )
        assert row["qty"] == qty
        assert H.val(row["grade"]) == "live"
        assert H.same_instant(row["created_at"], H.CREATED)
        assert H.same_instant(row["expires_at"], H.EXPIRES)
        # nothing has been filled, so nothing has a price yet
        for col in ("entry_ts", "fill_bar_ts", "entry_paise", "exit_paise"):
            assert row[col] is None, col

    @pytest.mark.parametrize("shape, qty", SHAPES)
    def test_the_returned_trade_mirrors_the_stored_row(self, db, shape, qty):
        book = H.make_book(db.engine)
        trade = H.open_trade(book, alert_id="A1", **shape)
        row = H.trade_row(db.engine, "A1")
        assert trade.alert_id == "A1" and trade.id == row["id"]
        assert H.val(trade.status) == "PENDING_FILL" and H.val(trade.grade) == "live"
        assert trade.instrument_key == row["instrument_key"] and trade.qty == qty
        assert trade.side == row["side"] and trade.direction == shape["direction"]
        assert trade.strategy == "ema_cross" and trade.timeframe == "10m"
        assert trade.confidence == pytest.approx(61.5)
        assert trade.entry_paise is None and trade.exit_paise is None
        assert trade.created_at == H.CREATED and trade.expires_at == H.EXPIRES

    def test_the_trade_is_listed_by_open_trades_while_it_waits_for_its_fill(self, db):
        book = H.make_book(db.engine)
        trade = H.open_trade(book, alert_id="A1")
        assert [t.id for t in book.open_trades()] == [trade.id]
        assert H.val(book.open_trades()[0].status) == "PENDING_FILL"

    def test_opening_a_trade_also_records_an_opened_event(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1")
        events = H.event_rows(db.engine, "A1")
        assert [H.val(e["kind"]) for e in events] == ["OPENED"]
        assert events[0]["ts"] is not None

    def test_several_alerts_open_several_independent_trades(self, db):
        book = H.make_book(db.engine)
        for i in range(5):
            H.open_trade(book, alert_id=f"A{i}")
        assert sorted(r["alert_id"] for r in H.trade_rows(db.engine)) == [f"A{i}" for i in range(5)]
        assert len({r["id"] for r in H.trade_rows(db.engine)}) == 5
        assert book.status()["opened"] == 5


class TestIdempotentByAlertId:
    def test_a_second_call_returns_the_existing_trade_and_creates_nothing(self, db):
        book = H.make_book(db.engine)
        first = H.open_trade(book, alert_id="A1")
        second = H.open_trade(book, alert_id="A1")
        assert second is not None and second.id == first.id
        assert len(H.trade_rows(db.engine)) == 1
        assert [H.val(e["kind"]) for e in H.event_rows(db.engine, "A1")] == ["OPENED"]
        assert book.status()["opened"] == 1
        assert [t.id for t in book.open_trades()] == [first.id]

    def test_a_repeat_with_different_content_keeps_the_first_rows_values(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1", strategy="ema_cross", score=61.5)
        before = H.trade_row(db.engine, "A1")
        again = H.make_alert("A1", strategy="supertrend_vol", score=3.0, direction="short")
        H.open_trade(book, again)
        assert H.trade_row(db.engine, "A1") == before

    def test_a_repeat_after_the_fill_does_not_reset_the_filled_trade(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1")
        book.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars()))
        filled = H.trade_row(db.engine, "A1")
        assert H.val(filled["status"]) == "OPEN" and filled["entry_paise"] == 100030
        again = H.open_trade(book, alert_id="A1")
        assert again is not None and again.id == filled["id"]
        assert H.trade_row(db.engine, "A1") == filled
        assert len(H.event_rows(db.engine, "A1")) == 2  # OPENED + FILLED, nothing more

    def test_a_new_alert_for_the_same_instrument_is_a_new_trade(self, db):
        book = H.make_book(db.engine)
        a = H.open_trade(book, alert_id="A1")
        b = H.open_trade(book, alert_id="A2")
        assert a.id != b.id and len(H.trade_rows(db.engine)) == 2


class TestTheBookKeepsItsOwnCopyOfTheAlert:
    def _mutate(self, alert: dict) -> None:
        """What the desk does to its alert dict later (status, outcome) and then some."""
        alert["status"] = "TAKEN"
        alert["outcome"] = {"outcome": "STOP"}
        alert["strategy"] = "mutated"
        alert["instrument_key"] = "RELIANCE"
        alert["confidence"]["score"] = 0.0
        alert["data"]["source"] = "yahoo"
        alert["economics"]["stop_price"] = 1100.0
        alert["economics"]["underlying_stop"] = 1100.0
        alert["economics"]["qty"] = 500
        alert["manage"]["target_r"] = 99.0

    def test_mutating_the_alert_after_on_alert_changes_nothing_stored(self, db):
        book = H.make_book(db.engine)
        alert = H.make_alert("A1")
        book.on_alert(alert, H.CREATED)
        before = H.trade_row(db.engine, "A1")
        events_before = H.event_rows(db.engine, "A1")
        self._mutate(alert)
        assert H.trade_row(db.engine, "A1") == before
        assert H.event_rows(db.engine, "A1") == events_before

    def test_the_book_does_not_keep_a_reference_to_the_callers_dict(self, db):
        """The original stop is 990 and the 10:06 open is 995: the trade fills. If the book had
        kept the caller's dict, the mutated stop (1100) would make 995 a gap through the stop."""
        book = H.make_book(db.engine)
        alert = H.make_alert("A1", entry=1000.0, stop=990.0)
        book.on_alert(alert, H.CREATED)
        self._mutate(alert)
        events = book.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars((995.0, 995.0, 996.0))))
        row = H.trade_row(db.engine, "A1")
        assert [H.val(e.kind) for e in events] == ["FILLED"]
        assert H.val(row["status"]) == "OPEN" and row["strategy"] == "ema_cross"
        assert row["qty"] == 1 and row["instrument_key"] == "SBIN"

    def test_the_returned_trade_is_immutable(self, db):
        book = H.make_book(db.engine)
        trade = H.open_trade(book, alert_id="A1")
        assert dataclasses.is_dataclass(trade)
        with pytest.raises(dataclasses.FrozenInstanceError):
            trade.status = "OPEN"  # type: ignore[misc]
        with pytest.raises(dataclasses.FrozenInstanceError):
            trade.entry_paise = 1  # type: ignore[misc]


class TestGradeComesFromTheDataSource:
    @pytest.mark.parametrize(
        "source, grade",
        [
            ("dhan", "live"),
            ("dhan-ws", "live"),
            ("dhan+yahoo", "live"),  # starts with "dhan": the rule is a prefix test
            ("yahoo", "delayed"),
            ("yahoo-2m", "delayed"),
            ("fake", "delayed"),
            ("mock", "delayed"),
            ("", "delayed"),
            (None, "delayed"),
        ],
    )
    def test_live_iff_the_source_starts_with_dhan(self, db, source, grade):
        book = H.make_book(db.engine)
        trade = H.open_trade(book, alert_id="A1", source=source)
        assert H.val(trade.grade) == grade
        assert H.val(H.trade_row(db.engine, "A1")["grade"]) == grade

    @pytest.mark.parametrize(
        "source, flag, grade",
        [
            ("yahoo", False, "delayed"),  # the desk's own flag is wrong for Yahoo-fallback series
            ("dhan", True, "live"),
            ("dhan-ws", True, "live"),
        ],
    )
    def test_the_grade_follows_the_source_never_the_desk_delayed_flag(
        self, db, source, flag, grade
    ):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1", source=source, delayed_flag=flag)
        assert H.val(H.trade_row(db.engine, "A1")["grade"]) == grade

    @pytest.mark.parametrize("source", ["dhan", "dhan-ws", "yahoo"])
    def test_the_data_source_itself_is_recorded_on_the_row(self, db, source):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1", source=source)
        row = H.trade_row(db.engine, "A1")
        # Manager decision: the column is `data_source`, stored verbatim (hyphens included)
        assert row["data_source"] == source, row


class TestOpenedEventsAndCounters:
    def test_status_counts_an_opened_trade_once(self, db):
        book = H.make_book(db.engine)
        assert book.status() == {
            "opened": 0,
            "filled": 0,
            "no_fill": 0,
            "closed": 0,
            "hook_errors": 0,
        }
        H.open_trade(book, alert_id="A1")
        H.open_trade(book, alert_id="A1")  # a repeat is not a second opening
        status = book.status()
        assert status["opened"] == 1 and status["filled"] == 0 and status["closed"] == 0
        assert all(isinstance(v, int) for v in status.values())

    def test_status_returns_a_copy(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1")
        s = book.status()
        s["opened"] = 999
        s["hook_errors"] = 999
        assert book.status()["opened"] == 1 and book.status()["hook_errors"] == 0

    def test_open_trades_returns_a_copy(self, db):
        book = H.make_book(db.engine)
        trade = H.open_trade(book, alert_id="A1")
        listing = book.open_trades()
        listing.clear()
        assert [t.id for t in book.open_trades()] == [trade.id]


class TestTimestampsAreAwareNotGuessed:
    def test_created_and_expires_are_stored_as_the_same_instants_whatever_the_offset(self, db):
        book = H.make_book(db.engine)
        utc = dt.timezone.utc
        alert = H.make_alert("A1")
        alert["created_at"] = H.CREATED.astimezone(utc).isoformat()
        alert["expires_at"] = H.EXPIRES.astimezone(utc).isoformat()
        book.on_alert(alert, H.CREATED)
        row = H.trade_row(db.engine, "A1")
        assert H.same_instant(row["created_at"], H.CREATED)
        assert H.same_instant(row["expires_at"], H.EXPIRES)

    def test_microseconds_in_the_alert_timestamp_survive(self, db):
        book = H.make_book(db.engine)
        created = H.at(10, 5, 3, 123456)
        H.open_trade(book, H.make_alert("A1", created=created), created)
        assert H.same_instant(H.trade_row(db.engine, "A1")["created_at"], created)

    @pytest.mark.parametrize("field", ["created_at", "expires_at"])
    def test_a_naive_alert_timestamp_is_rejected_and_nothing_is_written(self, db, field):
        book = H.make_book(db.engine)
        alert = H.make_alert("A1")
        alert[field] = alert[field][:19]  # "2026-10-29T10:05:03": no offset, not guessable
        try:
            result = book.on_alert(alert, H.CREATED)
        except (ValueError, TypeError):
            result = None  # rejecting by raising is as good as returning None
        assert result is None
        assert H.count_rows(db.engine, "paper_trades") == 0
        assert H.count_rows(db.engine, "paper_events") == 0
