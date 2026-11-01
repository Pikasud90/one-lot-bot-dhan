"""The paper book on a busy SQLite database (US-001 review round 1, BUG-1 and BUG-2).

SQLite has one writer. While another connection holds the write lock every other writer
waits its busy timeout (30 s in production) and then fails with "database is locked".

BUG-1  a step must stop at the FIRST lock error (count it once, log it once without a
       traceback) and leave the rest for the next poll, instead of paying one busy timeout
       per waiting trade while the Desk lock is held.
BUG-2  (test_busy_reads.py) open_trades() / status() never wait for the database.
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine, event

from onelot.store.schema import paper_events

from .conftest import THU, ist, make_alert, make_bars, table_rows, write_lock


def count_writes(eng: Engine, table: str) -> dict[str, int]:
    seen = {"n": 0}

    def spy(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if statement.lstrip().upper().startswith(("UPDATE", "INSERT")) and table in statement:
            seen["n"] += 1

    event.listen(eng, "before_cursor_execute", spy)
    return seen


def bars():
    return lambda key: make_bars(ist(THU, 10, 5), [1000.0] * 3) if key == "RELIANCE" else None


# ── BUG-1: one lock error stops the step ─────────────────────────────────────────


def test_a_locked_database_stops_the_step_at_the_first_trade(busy, caplog):
    path, book, eng = busy
    for i in range(5):
        book.on_alert(make_alert(alert_id=f"w{i}"), ist(THU, 10, 5, 4))
    attempts = count_writes(eng, "paper_trades")

    with write_lock(path), caplog.at_level(logging.ERROR, logger="onelot.paper.book"):
        assert book.step(ist(THU, 10, 8), bars()) == []  # never raises

    assert attempts["n"] == 1, f"{attempts['n']} trades each paid a busy timeout"
    assert book.status()["hook_errors"] == 1 and book.status()["filled"] == 0
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and errors[0].exc_info is None  # one line, no traceback storm
    assert "w0" in errors[0].getMessage() and "locked" in errors[0].getMessage()
    assert {t.status for t in book.open_trades()} == {"PENDING_FILL"}

    events = book.step(ist(THU, 10, 9), bars())  # the lock is gone: all five fill, once
    assert [e.kind for e in events] == ["FILLED"] * 5
    assert len(table_rows(eng, paper_events)) == 5 + 5


def test_any_other_failure_still_skips_only_that_trade(busy, monkeypatch):
    _, book, _ = busy
    for i in range(3):
        book.on_alert(make_alert(alert_id=f"w{i}"), ist(THU, 10, 5, 4))
    real = book._step_waiting

    def flaky(trade, view, now):
        if trade.alert_id == "w0":
            raise RuntimeError("not a lock")
        return real(trade, view, now)

    monkeypatch.setattr(book, "_step_waiting", flaky)
    events = book.step(ist(THU, 10, 8), bars())
    assert [e.kind for e in events] == ["FILLED", "FILLED"]
    assert book.status()["hook_errors"] == 1
