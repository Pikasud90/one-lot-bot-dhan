"""A failed on_alert is retried at the next step (US-001 review round 1, MINOR-3).

The desk's hook calls on_alert once per alert. A RUNTIME failure (a locked or broken
database) used to lose the alert until the next restart's reconciliation. The book now keeps
a bounded in-memory copy (oldest dropped with a WARNING) and retries it at the start of every
step until it is booked: exactly once, at the alert's own time. An alert the book cannot
record (PaperAlertError) is not retried: a retry cannot fix it.
"""

from __future__ import annotations

import contextlib
import copy
import logging
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, event

from onelot.paper.book import PaperAlertError
from onelot.store.schema import paper_events, paper_trades

from .conftest import THU, ist, make_alert, make_bars, table_rows


@contextlib.contextmanager
def broken_inserts(engine: Engine) -> Iterator[dict[str, int]]:
    """Every INSERT into paper_trades raises at the cursor while the block runs (a failure
    while talking to the database); yields how many were attempted."""
    hits = {"n": 0}

    def boom(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if statement.lstrip().upper().startswith("INSERT") and "paper_trades" in statement:
            hits["n"] += 1
            raise RuntimeError("injected: database unavailable")

    event.listen(engine, "before_cursor_execute", boom)
    try:
        yield hits
    finally:
        event.remove(engine, "before_cursor_execute", boom)


def bars():
    return lambda key: make_bars(ist(THU, 10, 5), [1000.0] * 3) if key == "RELIANCE" else None


def kinds(engine: Engine) -> list[str]:
    return [e["kind"] for e in table_rows(engine, paper_events)]


def test_a_failed_on_alert_is_booked_at_the_next_step_at_the_alerts_own_time(make_book, engine):
    book = make_book()
    alert = make_alert(alert_id="lost1")
    with broken_inserts(engine), pytest.raises(RuntimeError):
        book.on_alert(alert, ist(THU, 10, 5, 4))  # the desk counts this hook error
    alert["status"] = "TAKEN"  # the desk mutates its own dict later: the kept copy is deep
    assert table_rows(engine, paper_trades) == [] and book.open_trades() == []

    events = book.step(ist(THU, 10, 8), bars())

    (row,) = table_rows(engine, paper_trades)
    assert row["alert_id"] == "lost1" and row["created_at"] == ist(THU, 10, 5, 3)
    assert (row["status"], row["entry_paise"]) == ("OPEN", 100030)  # filled in the same step
    assert [e.kind for e in events] == ["FILLED"] and kinds(engine) == ["OPENED", "FILLED"]
    assert table_rows(engine, paper_events)[0]["detail"]["alert_status"] == "PENDING"
    assert book.status()["opened"] == 1
    book.step(ist(THU, 10, 9), bars())
    assert kinds(engine) == ["OPENED", "FILLED"]  # retried once, never twice


def test_a_retry_never_double_books_an_alert_booked_meanwhile(make_book, engine):
    book = make_book()
    alert = make_alert(alert_id="dup1")
    with broken_inserts(engine), pytest.raises(RuntimeError):
        book.on_alert(copy.deepcopy(alert), ist(THU, 10, 5, 4))
    other = make_book()  # another book (a restart) books it first
    other.on_alert(copy.deepcopy(alert), ist(THU, 10, 6))

    book.step(ist(THU, 10, 6, 30), bars())

    assert len(table_rows(engine, paper_trades)) == 1 and kinds(engine) == ["OPENED"]
    assert book.status()["opened"] == 0


def test_an_alert_the_book_cannot_record_is_not_kept_for_retry(make_book, engine):
    book = make_book()
    bad = make_alert(alert_id="bad1")
    del bad["economics"]
    with pytest.raises(PaperAlertError):
        book.on_alert(bad, ist(THU, 10, 5, 4))
    assert book.step(ist(THU, 10, 8), bars()) == []
    assert table_rows(engine, paper_trades) == [] and book.status()["hook_errors"] == 0


def test_a_retry_that_fails_again_is_kept_and_counted(make_book, engine):
    book = make_book()
    with broken_inserts(engine):
        with pytest.raises(RuntimeError):
            book.on_alert(make_alert(alert_id="r1"), ist(THU, 10, 5, 4))
        book.step(ist(THU, 10, 6), bars())  # still broken: kept, counted, nothing escapes
    assert book.status()["hook_errors"] == 1 and table_rows(engine, paper_trades) == []
    book.step(ist(THU, 10, 8), bars())
    assert [r["alert_id"] for r in table_rows(engine, paper_trades)] == ["r1"]


def test_the_retry_set_is_bounded_and_drops_the_oldest_with_a_warning(
    make_book, engine, monkeypatch, caplog
):
    monkeypatch.setattr("onelot.paper.book.RETRY_CAP", 3)
    book = make_book()
    with broken_inserts(engine), caplog.at_level(logging.WARNING, logger="onelot.paper.book"):
        for i in range(5):
            with pytest.raises(RuntimeError):
                book.on_alert(make_alert(alert_id=f"q{i}"), ist(THU, 10, 5, 4))
    dropped = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(dropped) == 2 and "q0" in dropped[0] and "q1" in dropped[1]

    book.step(ist(THU, 10, 5, 30), bars())

    assert sorted(r["alert_id"] for r in table_rows(engine, paper_trades)) == ["q2", "q3", "q4"]


# ── final fix (review round 2 MINOR): retries are quiet and only for database failures ─


def insert_attempts(engine: Engine) -> dict[str, int]:
    seen = {"n": 0}

    def spy(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if statement.lstrip().upper().startswith("INSERT") and "paper_trades" in statement:
            seen["n"] += 1

    event.listen(engine, "before_cursor_execute", spy, insert=True)
    return seen


def test_a_persistent_database_failure_is_retried_quietly(make_book, engine, caplog):
    """The Reviewer's probe: 3 failed alerts, the failure persists for 4 steps. Before: every
    alert every step, 12 tracebacks, hook_errors 12. Now: the retry phase stops at the first
    database failure (it is not alert-specific) and hook_errors counts at most once per step;
    the failed alert goes to the BACK of the queue (review round 3: one bad alert never
    blocks the rest), so each alert logs one traceback the first time, then one-line
    warnings."""
    book = make_book()
    with broken_inserts(engine) as hits:
        for i in range(3):
            with pytest.raises(RuntimeError):
                book.on_alert(make_alert(alert_id=f"p{i}"), ist(THU, 10, 5, 4))
        hits["n"] = 0
        with caplog.at_level(logging.WARNING, logger="onelot.paper.book"):
            for minute in range(6, 10):
                book.step(ist(THU, 10, minute), bars())

    assert hits["n"] == 4  # one per step: the head of the queue, then the phase stops
    assert book.status()["hook_errors"] == 4
    tracebacks = [r for r in caplog.records if r.exc_info is not None]
    assert [r.args[0] for r in tracebacks] == ["p0", "p1", "p2"]  # rotated: once each
    later = [r for r in caplog.records if r.levelno == logging.WARNING and r.exc_info is None]
    assert len(later) == 1 and "p0" in later[0].getMessage()  # p0's second turn

    book.step(ist(THU, 10, 10), bars())  # the database recovered: all three, once each
    rows = table_rows(engine, paper_trades)
    assert sorted(r["alert_id"] for r in rows) == ["p0", "p1", "p2"]
    assert {r["created_at"] for r in rows} == {ist(THU, 10, 5, 3)}  # each at its own time


def test_a_non_database_bug_is_raised_once_and_never_kept(make_book, engine, monkeypatch):
    book = make_book()

    def bug(*_a, **_k):
        raise TypeError("a deterministic bug in the book")

    monkeypatch.setattr("onelot.paper.book.trade_values", bug)
    with pytest.raises(TypeError):
        book.on_alert(make_alert(alert_id="b1"), ist(THU, 10, 5, 4))  # the desk logs it once
    monkeypatch.undo()
    attempts = insert_attempts(engine)

    book.step(ist(THU, 10, 8), bars())

    assert attempts["n"] == 0 and table_rows(engine, paper_trades) == []
    assert book.status()["hook_errors"] == 0


def test_a_non_database_bug_at_retry_drops_the_alert_after_one_traceback(
    make_book, engine, monkeypatch, caplog
):
    book = make_book()
    with broken_inserts(engine), pytest.raises(RuntimeError):
        book.on_alert(make_alert(alert_id="d1"), ist(THU, 10, 5, 4))  # kept: a database failure

    def bug(*_a, **_k):
        raise TypeError("a deterministic bug in the book")

    monkeypatch.setattr("onelot.paper.book.trade_values", bug)
    with caplog.at_level(logging.WARNING, logger="onelot.paper.book"):
        for minute in (6, 7, 8):
            book.step(ist(THU, 10, minute), bars())
    monkeypatch.undo()
    book.step(ist(THU, 10, 9), bars())

    assert [r for r in caplog.records if r.exc_info is not None][0].getMessage().count("d1") == 1
    assert len([r for r in caplog.records if r.exc_info is not None]) == 1
    assert book.status()["hook_errors"] == 1  # the step that found the bug, once
    assert table_rows(engine, paper_trades) == []  # dropped: never retried again


def test_an_os_error_while_booking_is_a_database_failure_and_is_retried(
    make_book, engine, monkeypatch
):
    book = make_book()
    real = book._insert_opened
    calls = {"n": 0}

    def disk_full(values, opened_by):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(28, "No space left on device")
        return real(values, opened_by)

    monkeypatch.setattr(book, "_insert_opened", disk_full)
    with pytest.raises(OSError):
        book.on_alert(make_alert(alert_id="o1"), ist(THU, 10, 5, 4))
    book.step(ist(THU, 10, 8), bars())
    assert [r["alert_id"] for r in table_rows(engine, paper_trades)] == ["o1"]
