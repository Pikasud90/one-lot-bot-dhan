"""A database that fails as a whole, not one trade (US-001 review round 3, carried into US-002).

1. A retried alert whose booking hits a database failure that is NOT a lock goes to the back
   of the retry queue, so one bad alert can never block the alerts behind it (a lock keeps
   stop-and-keep-order: nothing else could be written either).
2. A step stops at the first non-lock DBAPIError (a read-only database, a disk I/O error) as
   it does for a lock: the failure is not the trade's, so every other trade would pay it too.
   Counted once and logged once; the next poll retries. A data error tied to one row (an
   IntegrityError) still skips only that trade.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, event
from sqlalchemy.exc import IntegrityError, OperationalError

from onelot.store.schema import paper_trades

from .conftest import THU, ist, make_alert, make_bars, table_rows


def bars():
    return lambda key: make_bars(ist(THU, 10, 5), [1000.0] * 3) if key == "RELIANCE" else None


@contextlib.contextmanager
def failing_insert_for(engine: Engine, alert_id: str) -> Iterator[None]:
    """Every INSERT of `alert_id` into paper_trades fails like a read-only database."""

    def boom(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if "INSERT INTO paper_trades" in statement and alert_id in str(parameters):
            raise sqlite3.OperationalError("attempt to write a readonly database")

    event.listen(engine, "before_cursor_execute", boom)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", boom)


class TestTheRetryQueue:
    def test_a_non_lock_failure_moves_the_alert_to_the_back_of_the_queue(self, make_book, engine):
        book = make_book()
        with failing_insert_for(engine, "bad1"):
            with pytest.raises(OperationalError):
                book.on_alert(make_alert(alert_id="bad1"), ist(THU, 10, 5, 4))
            with failing_insert_for(engine, "good1"), pytest.raises(OperationalError):
                book.on_alert(make_alert(alert_id="good1"), ist(THU, 10, 5, 4))
            book.step(ist(THU, 10, 8), bars())  # bad1 fails again: to the back
            book.step(ist(THU, 10, 9), bars())  # good1 is tried first now
        booked = {row["alert_id"] for row in table_rows(engine, paper_trades)}
        assert booked == {"good1"}


class TestTheStepStopsOnADatabaseFailure:
    @pytest.mark.parametrize(
        "error",
        [
            OperationalError(
                "UPDATE", {}, sqlite3.OperationalError("attempt to write a readonly database")
            ),
            OperationalError("UPDATE", {}, sqlite3.OperationalError("disk I/O error")),
        ],
        ids=["readonly", "disk-io"],
    )
    def test_the_first_non_lock_database_error_stops_the_step(
        self, make_book, monkeypatch, caplog, error
    ):
        book = make_book()
        for i in range(3):
            book.on_alert(make_alert(alert_id=f"w{i}"), ist(THU, 10, 5, 4))
        tried: list[str] = []

        def failing(trade, view, now):  # noqa: ANN001
            tried.append(trade.alert_id)
            raise error

        monkeypatch.setattr(book, "_step_waiting", failing)
        with caplog.at_level(logging.ERROR, logger="onelot.paper.book"):
            assert book.step(ist(THU, 10, 8), bars()) == []  # never raises
        assert tried == ["w0"], "every other trade would pay the same failure"
        assert book.status()["hook_errors"] == 1
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1 and errors[0].exc_info is None

    def test_an_integrity_error_still_skips_only_that_trade(self, make_book, monkeypatch):
        book = make_book()
        for i in range(3):
            book.on_alert(make_alert(alert_id=f"w{i}"), ist(THU, 10, 5, 4))
        real = book._step_waiting

        def flaky(trade, view, now):  # noqa: ANN001
            if trade.alert_id == "w0":
                raise IntegrityError("UPDATE", {}, sqlite3.IntegrityError("CHECK failed"))
            return real(trade, view, now)

        monkeypatch.setattr(book, "_step_waiting", flaky)
        events = book.step(ist(THU, 10, 8), bars())
        assert [e.kind for e in events] == ["FILLED", "FILLED"]
        assert book.status()["hook_errors"] == 1
