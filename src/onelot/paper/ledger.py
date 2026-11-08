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
from collections.abc import Callable
from dataclasses import dataclass
from typing import NamedTuple, TypeVar, get_args

from sqlalchemy import ColumnElement, Engine, func, select, type_coerce
from sqlalchemy.types import NullType

from ..store.schema import paper_trades
from .alert import Grade
from .money import stored_paise

__all__ = ["GRADES", "LedgerTotals", "PaperLedger", "Slot", "StrategyTotals"]

log = logging.getLogger(__name__)

GRADES: frozenset[str] = frozenset(get_args(Grade))  # the grades the book writes
WARN_CAP = 1000  # corrupt cells a ledger remembers having warned about (LRU)
_MONEY = ("gross_paise", "costs_paise", "net_paise")


class Slot(NamedTuple):
    """One strategy as the desk runs it: its name, its bar timeframe and its market
    (INDEX | STOCK). The paper test's candidates (2026-11-08) share their names with older
    desk strategies (supertrend_vol ran on 15-minute bars before), so the Home scoreboard
    counts each slot apart: an earlier setting's trades never count toward a candidate."""

    strategy: str
    timeframe: str
    kind: str


K = TypeVar("K")


def _name_key(strategy: object, timeframe: object, kind: object) -> str:
    return str(strategy or "?")


def _slot_key(strategy: object, timeframe: object, kind: object) -> Slot:
    return Slot(str(strategy or "?"), str(timeframe or "?"), str(kind or "?"))


@dataclass(frozen=True)
class LedgerTotals:
    """Counts and exact sums of the closed paper trades that have a net, in paise."""

    trades: int
    wins: int
    losses: int
    gross_paise: int
    costs_paise: int
    net_paise: int


@dataclass
class StrategyTotals:
    """One strategy's closed paper trades that have a net (owner feedback OF-5): the same
    exact sums as `LedgerTotals`, plus what the Home scoreboard needs. `won_paise` is the sum
    of the winning nets, `lost_paise` of the losing ones (<= 0); `r_sum` / `r_trades` average
    the R multiple (net / risk) over the trades whose risk is a positive whole number of paise.
    """

    trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_paise: int = 0
    costs_paise: int = 0
    net_paise: int = 0
    won_paise: int = 0
    lost_paise: int = 0
    r_sum: float = 0.0  # analytics: float is fine here, the money sums stay int
    r_trades: int = 0
    no_fill: int = 0
    last_exit: dt.datetime | None = None


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

    def by_strategy(
        self,
        *,
        grade: str | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
    ) -> dict[str, StrategyTotals]:
        """`totals()` split by strategy, plus NO_FILL counts, for the Home scoreboard (OF-5).

        Same rules as `totals()`: CLOSED rows with a net, exit time in [since, until), exact
        integer sums, a corrupt money cell warned about and left out. NO_FILL rows (no money)
        are counted apart in `no_fill`, with the same window on the time the book closed them.
        Strategies with no trade in the window are absent: the caller lists the ones it wants.
        """
        return self._grouped(_name_key, grade, since, until)

    def by_slot(
        self,
        *,
        grade: str | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
    ) -> dict[Slot, StrategyTotals]:
        """`by_strategy()` split further by timeframe and market: one entry per `Slot`, for
        the Home scoreboard of the paper test. Same rules, same window."""
        return self._grouped(_slot_key, grade, since, until)

    def _grouped(
        self,
        key: Callable[[object, object, object], K],
        grade: str | None,
        since: dt.datetime | None,
        until: dt.datetime | None,
    ) -> dict[K, StrategyTotals]:
        """The closed and NO_FILL rows in the window, summed per `key(strategy, timeframe,
        kind)`: the body of `by_strategy()` and `by_slot()`."""
        _check_grade(grade)
        _check_aware(since, "since")
        _check_aware(until, "until")
        if since is not None and until is not None and since > until:
            raise ValueError(f"since={since.isoformat()} is after until={until.isoformat()}")
        t = paper_trades.c
        when = func.coalesce(t.exit_ts, t.updated_at)
        where: list[ColumnElement[bool]] = [t.status.in_(("CLOSED", "NO_FILL"))]
        if grade is not None:
            where.append(t.grade == grade)
        if since is not None:
            where.append(when >= since)
        if until is not None:
            where.append(when < until)
        cells = [type_coerce(t[name], NullType()).label(name) for name in _MONEY]
        risk = type_coerce(t.risk_paise, NullType()).label("risk_paise")
        stmt = (
            select(t.id, t.strategy, t.timeframe, t.kind, t.status, t.exit_ts, risk, *cells)
            .where(*where)
            .order_by(t.id)
        )
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).all()
        out: dict[K, StrategyTotals] = {}
        for trade_id, strategy, timeframe, kind, status, exit_ts, raw_risk, *raw in rows:
            row = out.setdefault(key(strategy, timeframe, kind), StrategyTotals())
            if status == "NO_FILL":
                row.no_fill += 1
                continue
            if raw[-1] is None:
                continue  # closed without a priced exit: not a trade the ledger counts
            money = self._money(trade_id, raw)
            if money is None:
                continue
            trade_gross, trade_costs, trade_net = money
            row.trades += 1
            if trade_net > 0:
                row.wins += 1
                row.won_paise += trade_net
            elif trade_net < 0:
                row.losses += 1
                row.lost_paise += trade_net
            row.gross_paise += trade_gross
            row.costs_paise += trade_costs
            row.net_paise += trade_net
            try:
                risk_paise = stored_paise(raw_risk)
            except ValueError:
                risk_paise = None
            if risk_paise is not None and risk_paise > 0:
                row.r_sum += trade_net / risk_paise
                row.r_trades += 1
            ts = _as_aware(exit_ts)
            if ts is not None and (row.last_exit is None or ts > row.last_exit):
                row.last_exit = ts
        return out

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


def _as_aware(ts: object) -> dt.datetime | None:
    """A stored time as an aware datetime (TZDateTime already reads back aware UTC)."""
    if not isinstance(ts, dt.datetime):
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=dt.UTC)


def _check_grade(grade: object) -> None:
    if grade is not None and (not isinstance(grade, str) or grade not in GRADES):
        raise ValueError(f"grade={grade!r} is not one the paper book writes {sorted(GRADES)}")


def _check_aware(ts: object, name: str) -> None:
    if ts is None:
        return
    if not isinstance(ts, dt.datetime) or ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{name}={ts!r} must be a timezone-aware datetime")
