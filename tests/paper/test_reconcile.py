"""Startup reconciliation details (US-001 AC6) not pinned elsewhere: counters, the row id
as the key, and a payload whose JSON is corrupt."""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import text

from onelot.store.schema import desk_signals, paper_trades

from .conftest import make_alert, table_rows


def store(engine, alert, payload=...):
    with engine.begin() as conn:
        conn.execute(
            desk_signals.insert().values(
                id=alert["id"],
                created_at=dt.datetime.fromisoformat(alert["created_at"]),
                expires_at=dt.datetime.fromisoformat(alert["expires_at"]),
                instrument_key=alert["instrument_key"],
                series_key=alert["series_key"],
                kind=alert["kind"],
                side=alert["side"],
                strategy=alert["strategy"],
                timeframe=alert["timeframe"],
                status=alert["status"],
                payload=alert if payload is ... else payload,
            )
        )


def test_reconciled_trades_count_as_opened_by_this_book(make_book, engine):
    for i in range(3):
        store(engine, make_alert(alert_id=f"r{i}"))
    book = make_book()
    assert book.status()["opened"] == 3
    assert make_book().status()["opened"] == 0  # a second start finds nothing missing


def test_the_desk_signals_row_id_wins_over_a_different_payload_id(make_book, engine, caplog):
    alert = make_alert(alert_id="row1")
    store(engine, alert, payload={**alert, "id": "stale"})
    with caplog.at_level(logging.WARNING):
        make_book()
    assert [r["alert_id"] for r in table_rows(engine, paper_trades)] == ["row1"]
    assert any("row1" in r.getMessage() and "stale" in r.getMessage() for r in caplog.records)
    make_book()  # keyed by the row id, so the next start does not try again
    assert len(table_rows(engine, paper_trades)) == 1


def test_a_payload_with_corrupt_json_is_skipped_alone(make_book, engine, caplog):
    store(engine, make_alert(alert_id="bad1"))
    store(engine, make_alert(alert_id="good1"))
    with engine.begin() as conn:
        conn.execute(text("UPDATE desk_signals SET payload = '{not json' WHERE id = 'bad1'"))
    with caplog.at_level(logging.WARNING):
        book = make_book()
    assert [t.alert_id for t in book.open_trades()] == ["good1"]
    assert any("bad1" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
