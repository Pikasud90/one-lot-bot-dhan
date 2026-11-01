"""US-001 AC6 and AC7: restart safety, atomic commits, and the paper.enabled switch.

AC6  After a restart non-closed paper trades (PENDING_FILL and OPEN) reload and keep being
     managed with no duplicates; every fill or close row commits together with its
     paper_events row in ONE transaction (an injected failure leaves no half-state); alerts
     present in desk_signals without a paper row are reconciled at startup.

AC7  With paper.enabled false, on_alert returns None and writes nothing, while step still
     manages trades that are already open.

Restart = a new PaperBook on the same database; reconciliation is pinned to construction
(`PaperBook(...)` is what a restarted engine does before the first poll).
"""

from __future__ import annotations

import copy
import datetime as dt

import pytest
import us001_helpers as H

from onelot.config import AppConfig


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


def gap_bars():
    return H.minute_bars(H.at(10, 5), [1000.0, 985.0, 986.0])


def three_trades(engine):
    """A: filled (OPEN).  B: waiting (RELIANCE has no bars yet).  C: NO_FILL (gapped through)."""
    book = H.make_book(engine)
    H.open_trade(book, alert_id="A", instrument_key="SBIN")
    H.open_trade(book, alert_id="B", instrument_key="RELIANCE")
    H.open_trade(book, alert_id="C", instrument_key="HDFCBANK", stop=990.0)
    book.step(
        H.at(10, 8),
        H.Feed(SBIN=H.standard_bars(), RELIANCE=None, HDFCBANK=gap_bars()),
    )
    statuses = {r["alert_id"]: H.val(r["status"]) for r in H.trade_rows(engine)}
    assert statuses == {"A": "OPEN", "B": "PENDING_FILL", "C": "NO_FILL"}, "harness"
    return book


class TestRestart:
    def test_non_closed_trades_reload_and_finished_ones_do_not(self, db):
        three_trades(db.engine)
        restarted = H.make_book(db.engine)
        listed = {t.alert_id: H.val(t.status) for t in restarted.open_trades()}
        assert listed == {"A": "OPEN", "B": "PENDING_FILL"}

    def test_a_reloaded_open_trade_keeps_every_stored_field(self, db):
        three_trades(db.engine)
        before = H.trade_row(db.engine, "A")
        (a,) = [t for t in H.make_book(db.engine).open_trades() if t.alert_id == "A"]
        assert a.entry_paise == before["entry_paise"] == 100030
        assert a.qty == before["qty"] and a.id == before["id"]
        assert a.fill_bar_ts == before["fill_bar_ts"] and a.created_at == before["created_at"]
        assert H.val(a.grade) == "live"

    def test_a_reloaded_waiting_trade_keeps_being_managed_and_fills_exactly_once(self, db):
        three_trades(db.engine)
        restarted = H.make_book(db.engine)
        feed = H.Feed(SBIN=H.standard_bars(), RELIANCE=H.standard_bars(), HDFCBANK=gap_bars())
        events = restarted.step(H.at(10, 9), feed)
        assert [(H.val(e.kind), e.trade_id) for e in events] == [
            ("FILLED", H.trade_row(db.engine, "B")["id"])
        ]
        row_b = H.trade_row(db.engine, "B")
        assert H.val(row_b["status"]) == "OPEN" and row_b["entry_paise"] == 100030
        # exactly one fill event per trade: nothing was booked twice
        for alert_id, kinds in {
            "A": ["OPENED", "FILLED"],
            "B": ["OPENED", "FILLED"],
            "C": ["OPENED", "NO_FILL"],
        }.items():
            assert [H.val(e["kind"]) for e in H.event_rows(db.engine, alert_id)] == kinds
        assert restarted.step(H.at(10, 9), feed) == []

    def test_a_restart_creates_no_duplicates_and_on_alert_still_dedups(self, db):
        three_trades(db.engine)
        ids = {r["alert_id"]: r["id"] for r in H.trade_rows(db.engine)}
        for _ in range(3):  # three restarts in a row
            book = H.make_book(db.engine)
            for alert_id in ids:
                again = H.open_trade(book, alert_id=alert_id)
                assert again is not None and again.id == ids[alert_id]
        assert {r["alert_id"]: r["id"] for r in H.trade_rows(db.engine)} == ids
        assert len(H.trade_rows(db.engine)) == 3
        assert H.count_rows(db.engine, "paper_events") == 3 + 2  # 3 OPENED, 1 FILLED, 1 NO_FILL

    def test_a_trade_waiting_across_a_restart_is_still_judged_on_data_time(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="W1")
        stale = H.minute_bars(H.at(10, 0), [1000.0] * 6)
        assert book.step(H.at(10, 12), H.Feed(SBIN=stale)) == []
        restarted = H.make_book(db.engine)  # the wall clock has moved on; the data has not
        assert restarted.step(H.at(12, 0), H.Feed(SBIN=stale)) == []
        assert H.val(H.trade_row(db.engine, "W1")["status"]) == "PENDING_FILL"


class TestStartupReconciliation:
    def test_alerts_in_desk_signals_without_a_paper_row_get_one_at_startup(self, db):
        alerts = [
            H.make_alert("R1"),
            H.make_alert("R2", kind="INDEX"),
            H.make_alert("R3", direction="short", status="SKIPPED"),
            H.make_alert("R4", status="EXPIRED", risk_allowed=False),
        ]
        for a in alerts:
            H.insert_desk_signal(db.engine, a)
        book = H.make_book(db.engine, clock=lambda: H.at(10, 30))
        rows = {r["alert_id"]: r for r in H.trade_rows(db.engine)}
        assert set(rows) == {"R1", "R2", "R3", "R4"}
        for r in rows.values():
            assert H.val(r["status"]) == "PENDING_FILL"
            assert H.same_instant(r["created_at"], H.CREATED)  # the alert's time, not startup's
            assert [H.val(e["kind"]) for e in H.event_rows(db.engine, r["alert_id"])] == ["OPENED"]
        assert rows["R2"]["qty"] == H.LOT and rows["R1"]["qty"] == 1
        assert {t.alert_id for t in book.open_trades()} == {"R1", "R2", "R3", "R4"}

    def test_a_reconciled_trade_fills_exactly_like_one_opened_on_time(self, db):
        """The hook never ran (crash between the desk_signals insert and on_alert); at startup,
        20 minutes later, the trade must still fill at the first bar after the ALERT's time."""
        H.insert_desk_signal(db.engine, H.make_alert("R1"))
        book = H.make_book(db.engine, clock=lambda: H.at(10, 25))
        frame = H.minute_bars(H.at(10, 0), [990.0] * 6 + [1000.0] + [1010.0] * 20)
        events = book.step(H.at(10, 27), H.Feed(SBIN=frame))
        row = H.trade_row(db.engine, "R1")
        assert [H.val(e.kind) for e in events] == ["FILLED"]
        assert H.same_instant(row["fill_bar_ts"], H.at(10, 6)) and row["entry_paise"] == 100030

    def test_reconciliation_adds_only_the_missing_rows(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="R1")
        book.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars()))
        before = H.trade_row(db.engine, "R1")
        H.insert_desk_signal(db.engine, H.make_alert("R1"))  # already has a paper row
        H.insert_desk_signal(db.engine, H.make_alert("R2", instrument_key="RELIANCE"))
        for _ in range(2):  # and a second restart changes nothing more
            H.make_book(db.engine)
            assert sorted(r["alert_id"] for r in H.trade_rows(db.engine)) == ["R1", "R2"]
            assert H.trade_row(db.engine, "R1") == before
        assert [H.val(e["kind"]) for e in H.event_rows(db.engine, "R2")] == ["OPENED"]

    def test_a_late_on_alert_for_a_reconciled_alert_returns_the_existing_trade(self, db):
        alert = H.make_alert("R1")
        H.insert_desk_signal(db.engine, alert)
        book = H.make_book(db.engine)
        existing = H.trade_row(db.engine, "R1")
        again = H.open_trade(book, alert)
        assert again is not None and again.id == existing["id"]
        assert len(H.trade_rows(db.engine)) == 1

    def test_an_alert_the_desk_stored_before_any_book_existed_is_picked_up(self, desk_db):
        """End to end with the real desk: the alert is raised with no paper book attached (the
        state a crash between the insert and the hook leaves behind), then the book starts."""
        cfg = AppConfig.load()
        d, _ = H.new_desk(cfg, desk_db)
        d.poll()
        alert = H.raise_alert(d, "SBIN", now=H.mid_session(d, "SBIN"))
        assert H.count_rows(desk_db.engine, "desk_signals") == 1
        started = dt.datetime.fromisoformat(alert["created_at"]) + dt.timedelta(minutes=5)
        book = H.desk_book(cfg, desk_db.engine, clock=lambda: started)
        row = H.trade_row(desk_db.engine, alert["id"])
        assert H.val(row["status"]) == "PENDING_FILL" and row["instrument_key"] == "SBIN"
        assert [t.alert_id for t in book.open_trades()] == [alert["id"]]


class TestAtomicCommits:
    @pytest.mark.parametrize("failing_table", ["paper_events", "paper_trades"])
    def test_a_failure_while_filling_leaves_neither_half(self, db, failing_table):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1")
        before = H.snapshot_tables(db.engine)
        with H.failing_sql(db.engine, failing_table) as injected:
            try:
                book.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars()))
            except Exception:  # the book may re-raise or swallow; the data must not care
                pass
        assert injected["hits"] >= 1, "the injected failure never fired"
        assert H.snapshot_tables(db.engine) == before, "a fill committed half of its pair"
        (pending,) = book.open_trades()
        assert H.val(pending.status) == "PENDING_FILL" and pending.entry_paise is None
        assert book.status()["filled"] == 0

    @pytest.mark.parametrize("failing_table", ["paper_events", "paper_trades"])
    def test_the_fill_goes_through_when_the_failure_clears(self, db, failing_table):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1")
        with H.failing_sql(db.engine, failing_table):
            try:
                book.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars()))
            except Exception:
                pass
        events = book.step(H.at(10, 9), H.Feed(SBIN=H.standard_bars()))
        row = H.trade_row(db.engine, "A1")
        assert [H.val(e.kind) for e in events] == ["FILLED"]
        assert H.val(row["status"]) == "OPEN" and row["entry_paise"] == 100030
        assert [H.val(e["kind"]) for e in H.event_rows(db.engine, "A1")] == ["OPENED", "FILLED"]

    @pytest.mark.parametrize("failing_table", ["paper_events", "paper_trades"])
    def test_a_failure_while_closing_a_no_fill_leaves_neither_half(self, db, failing_table):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1", stop=990.0)
        before = H.snapshot_tables(db.engine)
        with H.failing_sql(db.engine, failing_table) as injected:
            try:
                book.step(H.at(10, 8), H.Feed(SBIN=gap_bars()))
            except Exception:
                pass
        assert injected["hits"] >= 1
        assert H.snapshot_tables(db.engine) == before
        assert H.val(H.trade_row(db.engine, "A1")["status"]) == "PENDING_FILL"
        events = book.step(H.at(10, 9), H.Feed(SBIN=gap_bars()))
        assert [H.val(e.kind) for e in events] == ["NO_FILL"]
        assert H.val(H.trade_row(db.engine, "A1")["close_reason"]) == "GAP_THROUGH_STOP"

    @pytest.mark.parametrize("failing_table", ["paper_events", "paper_trades"])
    def test_a_failure_while_opening_leaves_no_trade_without_its_opened_event(
        self, db, failing_table
    ):
        book = H.make_book(db.engine)
        with H.failing_sql(db.engine, failing_table) as injected:
            try:
                book.on_alert(H.make_alert("A1"), H.CREATED)
            except Exception:
                pass
        assert injected["hits"] >= 1
        assert H.count_rows(db.engine, "paper_trades") == 0
        assert H.count_rows(db.engine, "paper_events") == 0
        assert book.open_trades() == []
        retry = H.open_trade(book, alert_id="A1")  # the alert is not poisoned by the failure
        assert retry is not None
        assert [H.val(e["kind"]) for e in H.event_rows(db.engine, "A1")] == ["OPENED"]
        assert len(H.trade_rows(db.engine)) == 1

    def test_a_failed_fill_is_picked_up_again_after_a_restart(self, db):
        book = H.make_book(db.engine)
        H.open_trade(book, alert_id="A1")
        with H.failing_sql(db.engine, "paper_events"):
            try:
                book.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars()))
            except Exception:
                pass
        restarted = H.make_book(db.engine)
        assert [H.val(t.status) for t in restarted.open_trades()] == ["PENDING_FILL"]
        events = restarted.step(H.at(10, 9), H.Feed(SBIN=H.standard_bars()))
        assert [H.val(e.kind) for e in events] == ["FILLED"]
        assert len(H.event_rows(db.engine, "A1")) == 2

    def test_the_trade_is_committed_before_on_alert_returns(self, db, tmp_path):
        """A second connection pool on the same file (the dashboard, a restart) sees the row as
        soon as the call is over."""
        book = H.make_book(db.engine)
        trade = H.open_trade(book, alert_id="A1")
        other = H.new_db(tmp_path)
        try:
            assert [r["id"] for r in H.trade_rows(other.engine)] == [trade.id]
        finally:
            other.dispose()


class TestPaperDisabled:
    def test_on_alert_returns_none_and_writes_nothing(self, db):
        book = H.make_book(db.engine, enabled=False)
        assert book.on_alert(copy.deepcopy(H.make_alert("A1")), H.CREATED) is None
        assert H.count_rows(db.engine, "paper_trades") == 0
        assert H.count_rows(db.engine, "paper_events") == 0
        assert book.open_trades() == []
        assert book.status()["opened"] == 0

    def test_a_disabled_book_still_manages_the_trades_already_open(self, db):
        enabled = H.make_book(db.engine)
        H.open_trade(enabled, alert_id="A", instrument_key="SBIN")
        H.open_trade(enabled, alert_id="B", instrument_key="RELIANCE")
        enabled.step(H.at(10, 8), H.Feed(SBIN=H.standard_bars(), RELIANCE=None))
        assert H.val(H.trade_row(db.engine, "A")["status"]) == "OPEN"

        disabled = H.make_book(db.engine, enabled=False)  # the owner flipped paper.enabled off
        assert {t.alert_id for t in disabled.open_trades()} == {"A", "B"}
        assert H.open_trade(disabled, alert_id="C") is None  # no NEW trades ...
        assert H.count_rows(db.engine, "paper_trades") == 2
        events = disabled.step(  # ... but the open ones are still managed
            H.at(10, 9),
            H.Feed(SBIN=H.standard_bars(), RELIANCE=H.standard_bars((1998.0, 2000.0, 2001.0))),
        )
        assert [H.val(e.kind) for e in events] == ["FILLED"]
        assert H.val(H.trade_row(db.engine, "B")["status"]) == "OPEN"

    def test_a_disabled_book_closes_a_waiting_trade_no_fill_when_it_cannot_fill(self, db):
        enabled = H.make_book(db.engine)
        H.open_trade(enabled, alert_id="A", stop=990.0)
        disabled = H.make_book(db.engine, enabled=False)
        events = disabled.step(H.at(10, 8), H.Feed(SBIN=gap_bars()))
        assert [H.val(e.kind) for e in events] == ["NO_FILL"]

    def test_a_disabled_book_does_not_reconcile_desk_signals(self, db):
        H.insert_desk_signal(db.engine, H.make_alert("R1"))
        book = H.make_book(db.engine, enabled=False)
        assert H.count_rows(db.engine, "paper_trades") == 0
        assert book.open_trades() == []
