"""The paper ledger (US-023 AC2): exact totals of the closed paper trades, in integer paise.

`PaperLedger(engine).totals(grade=, since=, until=)` sums the CLOSED rows of `paper_trades`
whose net is known, in a half-open window [since, until) on the trade's exit time. Read-only:
it never writes and never takes the book's lock, so it reads the database each call (a trade
closed after the ledger was built is counted).

The sums are Python ints, never SQL SUM() and never a float: SQLite's SUM() turns REAL as soon
as one corrupt cell is a REAL, and a float loses whole paise beyond 2**53. Integer addition is
exact and associative, so the totals do not depend on the order trades were booked. A row whose
gross, costs or net is not a whole number of paise (a corrupt write) is UNKNOWN: it is left out
of every total and warned about, never truncated into the sum (as `PaperTrade.from_row` reads it).

A trade with net above zero is a win, below zero a loss, exactly zero neither (it still counts
as a trade). NO_FILL rows (zero money, never filled), open and waiting trades, and closes whose
exit was never priced (net NULL) are not trades the ledger counts.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import get_args

from sqlalchemy import Engine, select, type_coerce
from sqlalchemy.types import NullType

from ..store.schema import paper_trades
from .alert import Grade
from .money import stored_paise

__all__ = ["GRADES", "LedgerTotals", "PaperLedger"]

log = logging.getLogger(__name__)

GRADES: frozenset[str] = frozenset(get_args(Grade))  # the grades the book writes
WARN_CAP = 1000  # corrupt cells a ledger remembers having warned about (LRU)
_MONEY = ("gross_paise", "costs_paise", "net_paise")


@dataclass(frozen=True)
class LedgerTotals:
    """Counts and exact sums of the closed paper trades that have a net, in paise."""

    trades: int
    wins: int
    losses: int
    gross_paise: int
    costs_paise: int
    net_paise: int


class PaperLedger:
    """Totals of the paper book's closed trades, read from `paper_trades`."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._warned: OrderedDict[tuple[object, str], None] = OrderedDict()
        self._warn_lock = threading.Lock()

    def totals(
        self,
        *,
        grade: str | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
    ) -> LedgerTotals:
        """The CLOSED trades with a net, of `grade` (None: every grade), whose exit time is in
        [since, until) (None: unbounded on that side).

        Raises ValueError for a grade the book never writes ("live" and "delayed" only, case
        and all: an unknown grade is a caller bug, not an empty total), a naive `since` or
        `until` (never guessed), and `since` after `until`. [t, t) holds nothing: all zeros.
        """
        _check_grade(grade)
        _check_aware(since, "since")
        _check_aware(until, "until")
        if since is not None and until is not None and since > until:
            raise ValueError(f"since={since.isoformat()} is after until={until.isoformat()}")
        t = paper_trades.c
        where = [t.status == "CLOSED", t.net_paise.is_not(None)]
        if grade is not None:
            where.append(t.grade == grade)
        if since is not None:
            where.append(t.exit_ts >= since)
        if until is not None:
            where.append(t.exit_ts < until)
        # raw cells: a REAL in a BIGINT column must be seen as what it is, not coerced
        cells = [type_coerce(t[name], NullType()).label(name) for name in _MONEY]
        stmt = select(t.id, *cells).where(*where).order_by(t.id)
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).all()
        trades = wins = losses = gross = costs = net = 0
        for trade_id, *raw in rows:
            money = self._money(trade_id, raw)
            if money is None:
                continue
            trade_gross, trade_costs, trade_net = money
            trades += 1
            if trade_net > 0:
                wins += 1
            elif trade_net < 0:
                losses += 1
            gross += trade_gross
            costs += trade_costs
            net += trade_net
        return LedgerTotals(trades, wins, losses, gross, costs, net)

    def _money(self, trade_id: object, raw: list[object]) -> tuple[int, int, int] | None:
        """(gross, costs, net) of one row as integer paise, or None (warned) when any of them
        is not a whole number of paise or is missing beside a known net."""
        money: list[int] = []
        for name, value in zip(_MONEY, raw, strict=True):
            try:
                paise = stored_paise(value)
            except ValueError:
                paise = None
            if paise is None:
                self._warn_once(trade_id, name, value)
                return None
            money.append(paise)
        gross, costs, net = money
        return gross, costs, net

    def _warn_once(self, trade_id: object, column: str, value: object) -> None:
        """WARNING the first time this ledger meets a bad cell, DEBUG after (bounded LRU)."""
        key = (trade_id, column)
        with self._warn_lock:
            first = key not in self._warned
            self._warned[key] = None
            self._warned.move_to_end(key)
            while len(self._warned) > WARN_CAP:
                self._warned.popitem(last=False)
        log.log(
            logging.WARNING if first else logging.DEBUG,
            "paper ledger: trade %s %s=%r is not integer paise; left out of the totals, "
            "never truncated",
            trade_id,
            column,
            value,
        )


def _check_grade(grade: object) -> None:
    if grade is not None and (not isinstance(grade, str) or grade not in GRADES):
        raise ValueError(f"grade={grade!r} is not one the paper book writes {sorted(GRADES)}")


def _check_aware(ts: object, name: str) -> None:
    if ts is None:
        return
    if not isinstance(ts, dt.datetime) or ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{name}={ts!r} must be a timezone-aware datetime")
