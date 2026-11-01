"""US-023 AC2 (developer tests): `PaperLedger.totals` sums the CLOSED rows that have a net.

    LedgerTotals(trades, wins, losses, gross_paise, costs_paise, net_paise), frozen, plain ints
    totals(*, grade=None, since=None, until=None)   window half-open on exit_ts [since, until)

The Tester's acceptance tests (tests/acceptance/test_us023_ac2_ledger.py) cover the oracle sum,
permutation invariance, sums past 2**53 and the grade partition. These pin the unhappy paths at
the boundary: a row whose money is not a whole number, a grade nobody books, an inverted window.
Rows are inserted straight into paper_trades (the public schema), so any history can be stated.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from typing import Any

import pytest
from sqlalchemy import text

from onelot.store.schema import paper_trades

from .conftest import THU, ist


def insert(
    engine,
    alert_id: str,
    *,
    status: str = "CLOSED",
    gross: int | None = 0,
    costs: int | None = 0,
    net: int | None = 0,
    grade: str = "live",
    exit_ts: dt.datetime | None = None,
) -> None:
    ts = exit_ts or ist(THU, 11, 0)
    closed = status == "CLOSED"
    row: dict[str, Any] = {
        "alert_id": alert_id,
        "status": status,
        "close_reason": "TARGET" if closed else status,
        "grade": grade,
        "data_source": "dhan" if grade == "live" else "yahoo",
        "instrument_key": "RELIANCE",
        "series_key": "RELIANCE",
        "kind": "STOCK",
        "side": "BUY",
        "direction": "long",
        "strategy": "ema_cross",
        "timeframe": "5m",
        "confidence": 62.5,
        "product": "equity_intraday",
        "qty": 1,
        "entry_ts": ts if status in ("OPEN", "CLOSED") else None,
        "fill_bar_ts": ts if status in ("OPEN", "CLOSED") else None,
        "exit_ts": ts if closed else None,
        "last_bar_ts": ts if status in ("OPEN", "CLOSED") else None,
        "resolution_min": 1,
        "entry_paise": 100_030 if status in ("OPEN", "CLOSED") else None,
        "exit_paise": (100_030 + gross) if closed and gross is not None else None,
        "gross_paise": gross,
        "costs_paise": costs,
        "net_paise": net,
        "risk_paise": 530 if status in ("OPEN", "CLOSED") else None,
        "created_at": ist(THU, 10, 5),
        "expires_at": ist(THU, 10, 15),
        "plan": {"alert": {}, "levels": {}},
        "updated_at": ts,
    }
    with engine.begin() as conn:
        conn.execute(paper_trades.insert().values(**row))


def ledger(engine):
    from onelot.paper.ledger import PaperLedger

    return PaperLedger(engine)


def test_totals_counts_only_closed_rows_with_a_net(engine):
    insert(engine, "W", gross=970, costs=107, net=863)
    insert(engine, "L", gross=-560, costs=106, net=-666)
    insert(engine, "Z", gross=100, costs=100, net=0)  # break-even: a trade, neither win nor loss
    insert(engine, "U", gross=None, costs=None, net=None)  # exit never priced: not a trade
    insert(engine, "N", status="NO_FILL", gross=0, costs=0, net=0)  # zero money, not a trade
    insert(engine, "O", status="OPEN", gross=None, costs=None, net=None)
    from onelot.paper.ledger import LedgerTotals

    totals = ledger(engine).totals()
    assert totals == LedgerTotals(
        trades=3, wins=1, losses=1, gross_paise=510, costs_paise=313, net_paise=197
    )
    assert all(type(getattr(totals, f.name)) is int for f in dataclasses.fields(totals))


def test_a_net_that_is_not_a_whole_number_is_left_out_and_warned_never_truncated(engine, caplog):
    """A REAL in a paise column (a corrupt write) is UNKNOWN, as PaperTrade.from_row reads it:
    never truncated into the sum, never silently turning SUM() into a float."""
    insert(engine, "W", gross=970, costs=107, net=863)
    insert(engine, "BAD", gross=970, costs=107, net=863)
    with engine.begin() as conn:
        conn.execute(text("UPDATE paper_trades SET net_paise = 862.5 WHERE alert_id = 'BAD'"))
    with caplog.at_level(logging.WARNING, logger="onelot.paper.ledger"):
        totals = ledger(engine).totals()
    assert (totals.trades, totals.net_paise, totals.gross_paise) == (1, 863, 970)
    assert type(totals.net_paise) is int
    assert any("net_paise" in r.getMessage() for r in caplog.records), caplog.text


@pytest.mark.parametrize("grade", ["Live", "LIVE", "paper", ""])
def test_a_grade_the_book_never_writes_is_refused_not_an_empty_total(engine, grade):
    insert(engine, "W", gross=970, costs=107, net=863)
    with pytest.raises(ValueError, match="grade"):
        ledger(engine).totals(grade=grade)


def test_an_inverted_window_is_refused_but_an_empty_one_is_zero(engine):
    insert(engine, "W", gross=970, costs=107, net=863, exit_ts=ist(THU, 11, 0))
    book = ledger(engine)
    with pytest.raises(ValueError, match="since"):
        book.totals(since=ist(THU, 12, 0), until=ist(THU, 11, 0))
    empty = book.totals(since=ist(THU, 11, 0), until=ist(THU, 11, 0))  # [t, t) holds nothing
    assert (empty.trades, empty.net_paise) == (0, 0)
