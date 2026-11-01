"""US-023 mode B: the startup backfill under concurrency, at scale and against hostile rows.

The backfill (T3) computes costs for every CLOSED row a close left without them, then writes
them in ONE transaction of guarded UPDATEs (costs and net still NULL, entry/exit/gross unchanged).
What that has to survive:

  * two PaperBooks starting on one database at the same instant: every row ends with the oracle's
    costs and net exactly once, nobody raises, nobody counts a problem; with two DIFFERENT cost
    tables racing, every row is one table's answer, never costs from one and net from the other;
  * a live book closing a trade while another book starts and backfills;
  * 3,000 legacy rows: all costed exactly, in a sane time, and the next start is cheap and writes
    nothing; with 1 % of the rows corrupt the rest are still costed and the corrupt ones are counted;
  * the rows that must NOT be costed (money that is not whole paise, a gross that is not
    sign x (exit - entry) x qty, an unknown kind or direction, qty below one, a cost model that
    fails): left exactly as they are, counted once each in hook_errors, no event, and retried by
    the next start once repaired;
  * a locked database: the book still starts, nothing is written, one problem is counted, and the
    next start completes the work.
"""

from __future__ import annotations

import threading
import time

import pytest
import us001_helpers as H
import us002_helpers as U
import us023_helpers as W
from sqlalchemy import text


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


def legacy(n: int, start: int = -3000, step: int = 20, prefix: str = "L") -> list[W.Booked]:
    """`n` CLOSED stock longs as US-002 wrote them: a gross, costs and net NULL."""
    return [
        W.Booked(f"{prefix}{i:05d}", "CLOSED", start + step * i, None, None, "live", H.at(10, 7))
        for i in range(n)
    ]


def assert_oracle_costs(engine, model=None, *, expect: int | None = None) -> int:
    """Every CLOSED row carries exactly the oracle's costs and net; returns how many rows."""
    model = model or W.model_for("SBIN")
    rows = [r for r in H.trade_rows(engine) if r["status"] == "CLOSED"]
    for row in rows:
        costs = W.expected_costs(
            model, "STOCK", "long", row["entry_paise"], row["exit_paise"], 1, tie_ok=True
        )
        assert (row["costs_paise"], row["net_paise"]) == (costs, row["gross_paise"] - costs), row[
            "alert_id"
        ]
    if expect is not None:
        assert len(rows) == expect
    return len(rows)


def start_together(engine, *cost_fors):
    """Build one PaperBook per cost_for on `engine` at the same instant, each in its own thread.
    Returns (books, errors)."""
    barrier = threading.Barrier(len(cost_fors))
    books: list = [None] * len(cost_fors)
    errors: list[BaseException] = []

    def go(i, cost_for):
        barrier.wait()
        try:
            books[i] = W.make_book(engine, cost_for=cost_for)
        except BaseException as exc:  # noqa: BLE001 - reported by the test
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(i, c)) for i, c in enumerate(cost_fors)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not any(t.is_alive() for t in threads), "a book start hung"
    return books, errors


class TestTwoBooksStartingTogether:
    @pytest.mark.parametrize("round_", range(6))
    def test_every_row_is_costed_exactly_once_and_nobody_counts_a_problem(self, tmp_path, round_):
        d = H.new_db(tmp_path, f"two{round_}.db")
        try:
            W.insert_booked(d.engine, legacy(300))
            events = H.event_rows(d.engine)
            books, errors = start_together(d.engine, None, None)
            assert errors == []
            assert assert_oracle_costs(d.engine, expect=300) == 300
            assert [b.status()["hook_errors"] for b in books] == [0, 0]
            assert H.event_rows(d.engine) == events, "a backfill books no event"
            once = H.trade_rows(d.engine)
            W.make_book(d.engine)  # and a third start changes nothing at all
            assert H.trade_rows(d.engine) == once
        finally:
            d.dispose()

    @pytest.mark.parametrize("round_", range(6))
    def test_two_different_cost_tables_never_leave_a_row_half_one_and_half_the_other(
        self, tmp_path, round_
    ):
        d = H.new_db(tmp_path, f"mix{round_}.db")
        try:
            W.insert_booked(d.engine, legacy(300))
            books, errors = start_together(d.engine, None, W.loud_cost_for())
            assert errors == []
            real, loud = W.model_for("SBIN"), W.loud_cost_for()("SBIN")
            rows = H.trade_rows(d.engine)
            assert len(rows) == 300
            for row in rows:
                answers = {
                    W.expected_costs(
                        m, "STOCK", "long", row["entry_paise"], row["exit_paise"], 1, True
                    )
                    for m in (real, loud)
                }
                assert row["costs_paise"] in answers, row["alert_id"]
                assert row["net_paise"] == row["gross_paise"] - row["costs_paise"], (
                    "costs from one book, net from the other: " + row["alert_id"]
                )
            assert [b.status()["hook_errors"] for b in books] == [0, 0]
        finally:
            d.dispose()


class TestABackfillNextToALiveBook:
    def test_a_trade_closing_while_another_book_backfills_stays_consistent(self, db):
        book = W.make_book(db.engine)
        alert = W.stock_alert("LIVE1")
        H.open_trade(book, alert)
        frame = U.pbars(H.at(10, 5), [U.PRE_BAR, *W.stock_rows(alert, "target")])
        U.step_book(book, frame, H.at(10, 7))  # filled, OPEN
        assert U.row_of(db.engine, "LIVE1")["status"] == "OPEN"
        W.insert_booked(db.engine, legacy(300))
        starter: list = []
        thread = threading.Thread(target=lambda: starter.append(W.make_book(db.engine)))
        thread.start()
        U.step_book(book, frame, H.at(10, 9))  # the live book closes its trade meanwhile
        thread.join(timeout=120)
        assert not thread.is_alive() and len(starter) == 1
        live = U.row_of(db.engine, "LIVE1")
        assert (live["status"], live["costs_paise"], live["net_paise"]) == ("CLOSED", 107, 1863)
        assert assert_oracle_costs(db.engine, expect=301) == 301
        assert book.status()["hook_errors"] == 0 and starter[0].status()["hook_errors"] == 0


class TestAtScale:
    def test_three_thousand_legacy_rows_are_all_costed_exactly_in_a_sane_time(self, db):
        W.insert_booked(db.engine, legacy(3000, start=-30_000, step=20))
        started = time.perf_counter()
        book = W.make_book(db.engine)
        elapsed = time.perf_counter() - started
        assert assert_oracle_costs(db.engine, expect=3000) == 3000
        assert book.status()["hook_errors"] == 0
        assert elapsed < 30, f"backfilling 3,000 rows took {elapsed:.1f}s"
        totals = W.make_ledger(db.engine).totals()
        assert totals.trades == 3000 and totals.net_paise == totals.gross_paise - totals.costs_paise

    def test_the_next_start_over_3000_costed_rows_is_cheap_and_writes_nothing(self, db):
        W.insert_booked(db.engine, legacy(3000, start=-30_000, step=20))
        W.make_book(db.engine)
        before = H.trade_rows(db.engine)
        started = time.perf_counter()
        W.make_book(db.engine)
        elapsed = time.perf_counter() - started
        assert H.trade_rows(db.engine) == before, "not even updated_at moves"
        assert elapsed < 15, f"a start with nothing to backfill took {elapsed:.1f}s"

    def test_one_percent_corrupt_rows_are_counted_and_the_rest_costed(self, db):
        W.insert_booked(db.engine, legacy(3000, start=-30_000, step=20))
        bad = [f"L{i:05d}" for i in range(0, 3000, 100)]  # 30 rows
        with db.engine.begin() as conn:
            for alert_id in bad:
                conn.execute(
                    text("UPDATE paper_trades SET gross_paise = gross_paise + 1 WHERE alert_id=:a"),
                    {"a": alert_id},
                )
        book = W.make_book(db.engine)
        assert W.counted_problems(book) == 30
        rows = H.trade_rows(db.engine)
        assert sum(r["costs_paise"] is None for r in rows) == 30
        good = [r for r in rows if r["alert_id"] not in bad]
        assert all(r["costs_paise"] is not None and r["net_paise"] is not None for r in good)
        assert len(good) == 2970


def interfering(engine, sql: str):
    """A `cost_for` that, on its first call (the backfill has already read its candidates and is
    computing), runs `sql` from 'another book': the race the guarded UPDATE exists for."""
    base = H.cost_for_specs(W.SPECS)
    ran: list[bool] = []

    def cost_for(instrument_key):
        if not ran:
            ran.append(True)
            with engine.begin() as conn:
                conn.execute(text(sql))
        return base(instrument_key)

    return cost_for


class TestARowChangedUnderTheBackfill:
    def test_a_value_another_book_wrote_meanwhile_is_never_overwritten(self, db):
        W.insert_booked(db.engine, legacy(20))
        other = "UPDATE paper_trades SET costs_paise = 777, net_paise = gross_paise - 777"
        book = W.make_book(db.engine, cost_for=interfering(db.engine, other))
        rows = H.trade_rows(db.engine)
        assert len(rows) == 20
        assert all(
            (r["costs_paise"], r["net_paise"]) == (777, r["gross_paise"] - 777) for r in rows
        )
        assert book.status()["hook_errors"] == 0, "costed elsewhere is not a problem"

    def test_a_row_whose_prices_changed_meanwhile_is_not_costed_from_the_stale_ones(self, db):
        W.insert_booked(db.engine, legacy(20))
        revised = (
            "UPDATE paper_trades SET exit_paise = exit_paise + 100, gross_paise = gross_paise + 100"
        )
        W.make_book(db.engine, cost_for=interfering(db.engine, revised))
        rows = H.trade_rows(db.engine)
        assert all(r["costs_paise"] is None and r["net_paise"] is None for r in rows), (
            "costs computed on the old exit must not be written against the new one"
        )
        W.make_book(db.engine)  # the next start sees the revised, consistent rows
        assert assert_oracle_costs(db.engine, expect=20) == 20


def corrupt(engine, sql: str, alert_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(text(sql), {"a": alert_id})


HOSTILE = {
    "real_money": "UPDATE paper_trades SET entry_paise = 10000000.5 WHERE alert_id = :a",
    "inconsistent_gross": "UPDATE paper_trades SET gross_paise = gross_paise + 1 WHERE alert_id = :a",
    "unknown_kind": "UPDATE paper_trades SET kind = 'WEIRD' WHERE alert_id = :a",
    "unknown_direction": "UPDATE paper_trades SET direction = 'sideways' WHERE alert_id = :a",
    "qty_below_one": "UPDATE paper_trades SET qty = 0, gross_paise = 0 WHERE alert_id = :a",
    "cost_model_fails": "UPDATE paper_trades SET instrument_key = 'GONE' WHERE alert_id = :a",
}


class TestTheRowsThatMustNotBeCosted:
    def seed(self, db, names):
        W.insert_booked(db.engine, legacy(4, start=500, step=100, prefix="G"))  # four good rows
        bad = []
        for name in names:
            alert_id = f"BAD_{name}"
            W.insert_booked(
                db.engine, [W.Booked(alert_id, "CLOSED", 700, None, None, "live", H.at(10, 7))]
            )
            corrupt(db.engine, HOSTILE[name], alert_id)
            bad.append(alert_id)
        return bad

    def raw(self, engine, alert_id):
        with engine.connect() as conn:
            return tuple(
                conn.execute(
                    text("SELECT * FROM paper_trades WHERE alert_id=:a"), {"a": alert_id}
                ).one()
            )

    @pytest.mark.parametrize("name", list(HOSTILE))
    def test_one_hostile_row_is_left_alone_counted_once_and_does_not_hurt_the_others(
        self, db, name
    ):
        (bad,) = self.seed(db, [name])
        before = self.raw(db.engine, bad)
        events = H.event_rows(db.engine)
        book = W.make_book(db.engine)  # must start
        assert self.raw(db.engine, bad) == before, "never repaired, truncated or guessed"
        assert W.counted_problems(book) == 1, book.status()
        assert H.event_rows(db.engine) == events, "no event for a skipped backfill row"
        good = [r for r in H.trade_rows(db.engine) if r["alert_id"].startswith("G")]
        assert len(good) == 4 and all(r["net_paise"] is not None for r in good)
        for row in good:
            costs = W.expected_costs(
                W.model_for("SBIN"),
                "STOCK",
                "long",
                row["entry_paise"],
                row["exit_paise"],
                1,
                True,
            )
            assert (row["costs_paise"], row["net_paise"]) == (costs, row["gross_paise"] - costs)

    def test_all_the_hostile_kinds_together_each_count_once(self, db):
        bad = self.seed(db, list(HOSTILE))
        book = W.make_book(db.engine)
        assert W.counted_problems(book) == len(bad) == 6
        rows = {r["alert_id"]: r for r in H.trade_rows(db.engine)}
        assert all(rows[a]["costs_paise"] is None and rows[a]["net_paise"] is None for a in bad)
        assert book.open_trades() == []

    def test_the_count_is_per_book_and_a_repaired_row_is_costed_by_the_next_start(self, db):
        (bad,) = self.seed(db, ["inconsistent_gross"])
        assert W.counted_problems(W.make_book(db.engine)) == 1
        assert W.counted_problems(W.make_book(db.engine)) == 1, "retried, counted again"
        corrupt(db.engine, "UPDATE paper_trades SET gross_paise = 700 WHERE alert_id = :a", bad)
        book = W.make_book(db.engine)
        assert W.counted_problems(book) == 0
        row = H.trade_row(db.engine, bad)
        assert row["costs_paise"] is not None and row["net_paise"] == 700 - row["costs_paise"]

    def test_a_row_with_no_gross_is_an_unpriced_close_not_a_candidate_and_not_a_problem(self, db):
        W.insert_booked(
            db.engine, [W.Booked("U1", "CLOSED", None, None, None, "live", H.at(10, 7))]
        )
        book = W.make_book(db.engine)
        row = H.trade_row(db.engine, "U1")
        assert (row["gross_paise"], row["costs_paise"], row["net_paise"]) == (None, None, None)
        assert W.counted_problems(book) == 0, "unknown by design (the exit was never priced)"


class TestALockedDatabase:
    def test_the_book_starts_writes_nothing_counts_one_problem_and_the_next_start_completes(
        self, db
    ):
        W.insert_booked(db.engine, legacy(50))
        path = db.engine.url.database
        impatient = H.short_timeout_engine(path, 0.3)
        try:
            with H.write_lock(path):
                book = W.make_book(impatient)  # its backfill cannot get the write lock
                assert W.counted_problems(book) == 1, book.status()
                assert book.open_trades() == []
            rows = H.trade_rows(db.engine)
            assert all(r["costs_paise"] is None and r["net_paise"] is None for r in rows)
        finally:
            impatient.dispose()
        recovered = W.make_book(db.engine)
        assert W.counted_problems(recovered) == 0
        assert assert_oracle_costs(db.engine, expect=50) == 50
