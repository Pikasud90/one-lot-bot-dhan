"""Reading the paper book never waits for the database (US-001 review round 1, BUG-2).

`open_trades()` and `status()` are the API's thread-safe copies (US-030 GET /api/paper): they
read memory only, so they must answer while an `on_alert` is stuck on a held SQLite write
lock. Idempotency then rests on the UNIQUE alert_id and the IntegrityError path, not on the
in-process lock.
"""

from __future__ import annotations

import threading
import time

from onelot.store.schema import paper_events, paper_trades

from .conftest import BUSY, THU, ist, make_alert, table_rows, write_lock


def test_status_and_open_trades_answer_while_on_alert_waits_for_the_lock(busy):
    path, book, _ = busy
    book.on_alert(make_alert(alert_id="earlier"), ist(THU, 10, 5, 4))
    outcome: dict[str, object] = {}

    def stuck() -> None:
        try:
            book.on_alert(make_alert(alert_id="late"), ist(THU, 10, 5, 4))
        except Exception as exc:  # the lock outlives the busy timeout: it must fail cleanly
            outcome["error"] = exc

    with write_lock(path):
        writer = threading.Thread(target=stuck)
        writer.start()
        time.sleep(BUSY / 4)  # the writer is inside on_alert, waiting for the lock
        started = time.perf_counter()
        status, listed = book.status(), book.open_trades()
        elapsed = time.perf_counter() - started
        writer.join(timeout=5)

    assert elapsed < BUSY / 2, f"reads waited {elapsed:.3f}s behind a stuck on_alert"
    assert status["opened"] == 1 and [t.alert_id for t in listed] == ["earlier"]
    assert "locked" in str(outcome.get("error", "")).lower()
    assert [t.alert_id for t in book.open_trades()] == ["earlier"]  # nothing phantom


def test_parallel_on_alert_for_one_id_still_books_one_trade(busy):
    """Idempotency no longer rests on the in-process lock: UNIQUE + IntegrityError do it."""
    _, book, eng = busy
    alert = make_alert(alert_id="race")
    barrier = threading.Barrier(6)
    ids: list[int] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            trade = book.on_alert(dict(alert), ist(THU, 10, 5, 4))
            assert trade is not None
            ids.append(trade.id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    # A writer that outlives the 0.2 s busy timeout may fail with "locked"; none may duplicate.
    assert all("locked" in str(e).lower() for e in errors), errors
    assert len(set(ids)) <= 1 and len(table_rows(eng, paper_trades)) == 1
    assert book.status()["opened"] == 1
    assert [e["kind"] for e in table_rows(eng, paper_events)] == ["OPENED"]
