"""US-001 mode B: the busy-database path.

SQLite allows one writer. While another connection holds the write lock (a Lab job, a restart
overlapping, a stuck process) every other writer waits for its busy timeout and then fails with
"database is locked". The production engine's busy timeout is 30 s (`Database._make_engine`);
these tests use a SHORT one on the same file (WAL, same pragmas) so the wait can be measured:

* `on_alert` under a held lock waits about one busy timeout, then raises; nothing is written,
  nothing phantom is remembered, the Desk still raises the alert, sends its notification and
  counts the hook error, and the next start reconciles the alert (no trade is lost);
* a lock that clears inside the timeout only delays the call;
* `step` under a held lock never raises, counts the failure, leaves every trade untouched and
  fills them on the next step after the lock clears;
* a step must not spend one full busy timeout PER waiting trade (production: N x 30 s of a
  stalled poll while it holds the Desk lock, which also blocks "Trade taken").
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time

import pytest
import us001_helpers as H

from onelot.config import AppConfig

TIMEOUT = 0.4  # busy timeout of the book's engine in these tests (production: 30 s)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def env(tmp_path):
    """(path, normal database, a book on a SHORT busy-timeout engine over the same file)."""
    db = H.new_db(tmp_path, "busy.db")
    short = H.short_timeout_engine(tmp_path / "busy.db", TIMEOUT)
    try:
        yield tmp_path / "busy.db", db, H.make_book(short), short
    finally:
        short.dispose()
        db.dispose()


@pytest.fixture
def desk_db(tmp_path):
    d = H.new_db(tmp_path, "desk.db", make_global=True)
    yield d
    H.dispose_db(d, was_global=True)


def timed(fn, *args):
    started = time.perf_counter()
    try:
        return fn(*args), None, time.perf_counter() - started
    except Exception as exc:  # the caller inspects it
        return None, exc, time.perf_counter() - started


class TestOnAlertUnderAHeldLock:
    def test_it_raises_after_about_one_busy_timeout_and_leaves_nothing_behind(self, env):
        path, db, book, _ = env
        with H.write_lock(path):
            result, error, elapsed = timed(book.on_alert, H.make_alert("A1"), H.CREATED)
            assert result is None and error is not None
            assert "locked" in str(error).lower(), error
            assert TIMEOUT * 0.5 <= elapsed <= TIMEOUT + 3.0, f"waited {elapsed:.2f}s"
            reads = timed(lambda: (book.open_trades(), book.status()))
            assert reads[2] < 0.25, "reading the book must not need the database"
        assert H.count_rows(db.engine, "paper_trades") == 0
        assert H.count_rows(db.engine, "paper_events") == 0
        assert book.open_trades() == [] and book.status()["opened"] == 0

    def test_the_alert_is_not_poisoned_by_the_failure_and_opens_once_the_lock_clears(self, env):
        path, db, book, _ = env
        with H.write_lock(path):
            timed(book.on_alert, H.make_alert("A1"), H.CREATED)
        trade = book.on_alert(H.make_alert("A1"), H.CREATED)
        assert trade is not None and book.status()["opened"] == 1
        assert [H.val(e["kind"]) for e in H.event_rows(db.engine, "A1")] == ["OPENED"]
        assert len(H.trade_rows(db.engine)) == 1

    def test_a_lock_that_clears_inside_the_timeout_only_delays_the_call(self, tmp_path):
        db = H.new_db(tmp_path, "slow.db")
        patient = H.short_timeout_engine(tmp_path / "slow.db", 3.0)
        try:
            book = H.make_book(patient)
            released = threading.Event()

            def hold():
                with H.write_lock(tmp_path / "slow.db"):
                    released.wait(0.4)

            holder = threading.Thread(target=hold)
            holder.start()
            time.sleep(0.1)  # let the holder take the lock first
            result, error, elapsed = timed(book.on_alert, H.make_alert("A1"), H.CREATED)
            holder.join()
            assert error is None and result is not None, error
            assert 0.2 <= elapsed < 2.5, f"waited {elapsed:.2f}s"
            assert H.count_rows(db.engine, "paper_trades") == 1
        finally:
            patient.dispose()
            db.dispose()


class TestReadingTheBookNeverWaitsForTheDatabase:
    def test_status_and_open_trades_answer_while_a_write_is_stuck_on_the_lock(self, env):
        """`open_trades()` and `status()` are documented as thread-safe copies for the API
        (US-030's GET /api/paper). They must not queue behind an on_alert that is waiting for
        the database: that would hang the endpoint for as long as the lock is held (production:
        up to 30 s), although all it needs is memory."""
        path, db, book, _ = env
        H.open_trade(book, alert_id="EARLIER")
        stuck = threading.Thread(
            target=lambda: timed(book.on_alert, H.make_alert("A1"), H.CREATED), daemon=True
        )
        with H.write_lock(path):
            stuck.start()
            time.sleep(0.1)  # the writer is now inside on_alert, waiting for the lock
            _, error, elapsed = timed(lambda: (book.status(), book.open_trades()))
            stuck.join(timeout=5)
        assert error is None
        assert elapsed < 0.15, (
            f"status()/open_trades() waited {elapsed:.2f}s for an on_alert that was blocked on "
            f"the database (busy timeout {TIMEOUT}s)"
        )


class TestTheDeskSurvivesALockedDatabaseAtTheHook:
    def locking_book(self, app_cfg, short, path):
        base = H.paper_book_class()

        class LockingBook(base):  # type: ignore[valid-type, misc]
            """Another writer takes the lock between the desk's insert and the paper insert."""

            def on_alert(self, alert, now):
                with H.write_lock(path):
                    return super().on_alert(alert, now)

        return H.desk_book(app_cfg, short, cls=LockingBook)

    def test_the_alert_and_its_notification_go_out_the_error_is_counted_and_a_restart_books_it(
        self, tmp_path, desk_db, caplog
    ):
        cfg = AppConfig.load()
        short = H.short_timeout_engine(tmp_path / "desk.db", TIMEOUT)
        try:
            book = self.locking_book(cfg, short, tmp_path / "desk.db")
            d, notifier = H.new_desk(cfg, desk_db, paper=book)
            d.poll()
            now = H.mid_session(d, "SBIN")
            started = time.perf_counter()
            with caplog.at_level(logging.ERROR):
                alert = H.raise_alert(d, "SBIN", now=now)  # must not raise
            elapsed = time.perf_counter() - started
            assert elapsed <= TIMEOUT + 3.0, f"the hook held the alert for {elapsed:.2f}s"
            assert alert["id"] in d.signals and H.count_rows(desk_db.engine, "desk_signals") == 1
            assert notifier.sent[-1].signal_id == alert["id"]
            assert notifier.sent[-1].level.value == "SIGNAL"
            assert d.hook_errors == 1
            assert any("on_alert failed" in r.getMessage() for r in caplog.records)
            assert H.count_rows(desk_db.engine, "paper_trades") == 0
        finally:
            short.dispose()
        restarted = H.desk_book(cfg, desk_db.engine, clock=lambda: now + dt.timedelta(minutes=5))
        row = H.trade_row(desk_db.engine, alert["id"])
        assert H.val(row["status"]) == "PENDING_FILL"
        assert [t.alert_id for t in restarted.open_trades()] == [alert["id"]]
        assert H.same_instant(row["created_at"], now)


class TestStepUnderAHeldLock:
    def test_it_never_raises_counts_the_failure_and_fills_on_the_next_step(self, env, caplog):
        path, db, book, _ = env
        H.open_trade(book, alert_id="A1")
        feed = H.Feed(SBIN=H.standard_bars())
        with H.write_lock(path), caplog.at_level(logging.ERROR):
            events, error, elapsed = timed(book.step, H.at(10, 8), feed)
            assert error is None, f"a locked database escaped step: {error!r}"
            assert events == [] and elapsed <= TIMEOUT + 3.0
        assert book.status()["hook_errors"] == 1 and book.status()["filled"] == 0
        assert any("A1" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)
        row = H.trade_row(db.engine, "A1")
        assert H.val(row["status"]) == "PENDING_FILL" and row["entry_paise"] is None
        assert [H.val(t.status) for t in book.open_trades()] == ["PENDING_FILL"]
        events = book.step(H.at(10, 9), feed)  # the lock is gone: the same trade fills, once
        assert [H.val(e.kind) for e in events] == ["FILLED"]
        assert [H.val(e["kind"]) for e in H.event_rows(db.engine, "A1")] == ["OPENED", "FILLED"]
        assert H.trade_row(db.engine, "A1")["entry_paise"] == 100030

    def test_a_lock_that_clears_mid_step_loses_no_trade(self, env):
        path, db, book, _ = env
        for i in range(4):
            H.open_trade(book, alert_id=f"A{i}")
        feed = H.Feed(SBIN=H.standard_bars())
        with H.write_lock(path):
            book.step(H.at(10, 8), feed)
        assert {H.val(r["status"]) for r in H.trade_rows(db.engine)} == {"PENDING_FILL"}
        book.step(H.at(10, 9), feed)
        assert {H.val(r["status"]) for r in H.trade_rows(db.engine)} == {"OPEN"}
        assert H.count_rows(db.engine, "paper_events") == 4 + 4  # one FILLED each, never two

    def test_one_locked_step_must_not_cost_one_busy_timeout_per_waiting_trade(self, env):
        """Six waiting trades, one held lock. Each failed commit waits a whole busy timeout, so
        the step takes six of them: 0.4 s x 6 here, 30 s x 6 = 3 minutes in production (45 trades:
        22 minutes) with the Desk lock held, so 'Trade taken' cannot even be processed. After a
        lock error the step should give up on the rest and retry on the next poll."""
        path, db, book, _ = env
        for i in range(6):
            H.open_trade(book, alert_id=f"A{i}")
        with H.write_lock(path):
            _, error, elapsed = timed(book.step, H.at(10, 8), H.Feed(SBIN=H.standard_bars()))
        assert error is None
        assert elapsed < 1.5 * TIMEOUT + 1.0, (
            f"step stalled {elapsed:.2f}s on a held lock: about one busy timeout ({TIMEOUT}s) per "
            f"waiting trade; expected to stop after the first lock error"
        )
