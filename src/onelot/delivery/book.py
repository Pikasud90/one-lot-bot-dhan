"""The delivery paper book of the pullback rule (US-067 D2 to D6, D12). Never an order.

One `delivery_trades` row per (stock, signal date) carries the entry alert, the paper trade
and the exit alert as states: PENDING_ENTRY -> OPEN -> EXIT_PENDING -> CLOSED, or MISSED.
Every change is one transaction and a compare-and-set on `status`, so a restart, a second
desk or a repeated poll can never book a trade twice. Separate from the intraday paper book
on purpose: nothing here expires after minutes or closes at the session end.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, and_, select, update
from sqlalchemy.exc import IntegrityError

from ..clock import IST
from ..costs import CostModel
from ..paper.ledger import Slot, StrategyTotals
from ..store.schema import METADATA, delivery_evaluations, delivery_trades
from .money import rupees_to_paise, trade_result

PENDING_ENTRY, OPEN, EXIT_PENDING, CLOSED, MISSED = (
    "PENDING_ENTRY",
    "OPEN",
    "EXIT_PENDING",
    "CLOSED",
    "MISSED",
)
ACTIVE = (PENDING_ENTRY, OPEN, EXIT_PENDING)


def _ist(v: dt.datetime | None) -> dt.datetime | None:
    if v is None:
        return None
    if v.tzinfo is None:
        v = v.replace(tzinfo=dt.timezone.utc)
    return v.astimezone(IST)


def _paise(v: object) -> int | None:
    return None if v is None else int(v)  # type: ignore[call-overload]


@dataclass(frozen=True)
class DeliveryTrade:
    id: int
    series_key: str
    instrument_key: str
    strategy: str
    timeframe: str
    signal_date: dt.date
    signal_close: float
    qty: int
    status: str
    note: str | None
    warning: str | None
    created_at: dt.datetime
    entry_told_at: dt.datetime | None
    entry_ts: dt.datetime | None
    entry_session: dt.date | None
    entry_paise: int | None
    entry_late: bool
    exit_signal_date: dt.date | None
    exit_reason: str | None
    sessions_held: int | None
    exit_told_at: dt.datetime | None
    exit_ts: dt.datetime | None
    exit_paise: int | None
    exit_late: bool
    gross_paise: int | None
    charges_paise: int | None
    dp_paise: int | None
    net_paise: int | None
    net_pct: float | None
    plan: dict[str, Any]

    @classmethod
    def from_row(cls, r: Mapping[Any, Any]) -> DeliveryTrade:
        def day(v: object) -> dt.date | None:
            return dt.date.fromisoformat(str(v)) if v else None

        plan = r["plan"]
        return cls(
            id=int(r["id"]),
            series_key=str(r["series_key"]),
            instrument_key=str(r["instrument_key"]),
            strategy=str(r["strategy"]),
            timeframe=str(r["timeframe"]),
            signal_date=dt.date.fromisoformat(str(r["signal_date"])),
            signal_close=float(r["signal_close"]),
            qty=int(r["qty"]),
            status=str(r["status"]),
            note=r["note"],
            warning=r["warning"],
            created_at=_ist(r["created_at"]) or dt.datetime.now(IST),
            entry_told_at=_ist(r["entry_told_at"]),
            entry_ts=_ist(r["entry_ts"]),
            entry_session=day(r["entry_session"]),
            entry_paise=_paise(r["entry_paise"]),
            entry_late=bool(r["entry_late"]),
            exit_signal_date=day(r["exit_signal_date"]),
            exit_reason=r["exit_reason"],
            sessions_held=None if r["sessions_held"] is None else int(r["sessions_held"]),
            exit_told_at=_ist(r["exit_told_at"]),
            exit_ts=_ist(r["exit_ts"]),
            exit_paise=_paise(r["exit_paise"]),
            exit_late=bool(r["exit_late"]),
            gross_paise=_paise(r["gross_paise"]),
            charges_paise=_paise(r["charges_paise"]),
            dp_paise=_paise(r["dp_paise"]),
            net_paise=_paise(r["net_paise"]),
            net_pct=None if r["net_pct"] is None else float(r["net_pct"]),
            plan=dict(plan if isinstance(plan, dict) else json.loads(str(plan))),
        )

    @property
    def entry_price(self) -> float | None:
        return None if self.entry_paise is None else self.entry_paise / 100

    @property
    def exit_price(self) -> float | None:
        return None if self.exit_paise is None else self.exit_paise / 100

    def as_dict(self) -> dict[str, Any]:
        """The trade as the page and the API show it (rupees as numbers, dates ISO)."""

        def iso(v: dt.date | dt.datetime | None) -> str | None:
            return None if v is None else v.isoformat()

        def rupees(p: int | None) -> float | None:
            return None if p is None else p / 100

        return {
            "id": self.id,
            "key": f"{self.series_key}:{self.signal_date.isoformat()}",
            "series_key": self.series_key,
            "instrument_key": self.instrument_key,
            "strategy": self.strategy,
            "timeframe": self.timeframe,
            "signal_date": iso(self.signal_date),
            "signal_close": self.signal_close,
            "qty": self.qty,
            "status": self.status,
            "note": self.note,
            "warning": self.warning,
            "created_at": iso(self.created_at),
            "entry_ts": iso(self.entry_ts),
            "entry_session": iso(self.entry_session),
            "entry": self.entry_price,
            "entry_late": self.entry_late,
            "exit_signal_date": iso(self.exit_signal_date),
            "exit_reason": self.exit_reason,
            "sessions_held": self.sessions_held,
            "exit_ts": iso(self.exit_ts),
            "exit": self.exit_price,
            "exit_late": self.exit_late,
            "gross": rupees(self.gross_paise),
            "charges": rupees(self.charges_paise),
            "dp": rupees(self.dp_paise),
            "net": rupees(self.net_paise),
            "net_pct": None if self.net_pct is None else round(self.net_pct, 4),
            "evidence": self.plan.get("evidence"),
        }


@dataclass(frozen=True)
class NewEntry:
    """An entry signal to record with its evaluation (D2)."""

    instrument_key: str
    signal_close: float
    qty: int
    plan: dict[str, Any]
    status: str = PENDING_ENTRY  # MISSED for a signal of an older session (catch-up)
    note: str | None = None


class DeliveryBook:
    """The persistent state of every delivery trade. Thread safe through the database."""

    def __init__(
        self,
        engine: Engine,
        model: CostModel,
        dp_paise: int,
        strategy: str = "pullback",
        timeframe: str = "1d",
    ) -> None:
        self.engine = engine
        self.model = model
        self.dp_paise = int(dp_paise)
        self.strategy, self.timeframe = strategy, timeframe
        METADATA.create_all(engine, tables=[delivery_trades, delivery_evaluations])

    # ── reads ─────────────────────────────────────────────────────────────
    def _select(self, *where: Any) -> list[DeliveryTrade]:
        stmt = select(delivery_trades).where(*where).order_by(delivery_trades.c.id)
        with self.engine.connect() as conn:
            return [DeliveryTrade.from_row(r) for r in conn.execute(stmt).mappings()]

    def active(self) -> list[DeliveryTrade]:
        return self._select(delivery_trades.c.status.in_(ACTIVE))

    def get(self, trade_id: int) -> DeliveryTrade | None:
        rows = self._select(delivery_trades.c.id == trade_id)
        return rows[0] if rows else None

    def recent(self, since: dt.datetime) -> list[DeliveryTrade]:
        """Every trade created or changed since `since`, newest first."""
        rows = self._select(delivery_trades.c.updated_at >= since)
        return sorted(rows, key=lambda t: (t.signal_date, t.id), reverse=True)

    def untold(self) -> list[tuple[DeliveryTrade, str]]:
        """Alerts written but not yet sent: (trade, "entry" | "exit")."""
        out: list[tuple[DeliveryTrade, str]] = []
        for t in self._select(delivery_trades.c.status.in_(ACTIVE + (CLOSED,))):
            if t.entry_told_at is None:  # also one that filled before it could be told
                out.append((t, "entry"))
            if t.exit_told_at is None and t.exit_signal_date is not None:
                out.append((t, "exit"))
        return out

    def last_evaluated(self, series_key: str) -> dt.date | None:
        stmt = select(delivery_evaluations.c.session_date).where(
            delivery_evaluations.c.series_key == series_key
        )
        with self.engine.connect() as conn:
            days = [str(d) for (d,) in conn.execute(stmt)]
        return dt.date.fromisoformat(max(days)) if days else None

    def evaluation(self, series_key: str, session: dt.date) -> dict[str, Any] | None:
        stmt = select(delivery_evaluations.c.reading).where(
            delivery_evaluations.c.series_key == series_key,
            delivery_evaluations.c.session_date == session.isoformat(),
        )
        with self.engine.connect() as conn:
            row = conn.execute(stmt).first()
        return None if row is None else dict(row[0])

    # ── writes ────────────────────────────────────────────────────────────
    def record_evaluation(
        self,
        series_key: str,
        session: dt.date,
        reading: Mapping[str, Any],
        now: dt.datetime,
        entry: NewEntry | None = None,
    ) -> DeliveryTrade | None:
        """Record that `session` was evaluated, and its entry signal if any, in one transaction.
        A session already evaluated is left alone (returns None): the alert is never doubled."""
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    delivery_evaluations.insert().values(
                        series_key=series_key,
                        session_date=session.isoformat(),
                        reading=dict(reading),
                        evaluated_at=now,
                    )
                )
                if entry is None:
                    return None
                key = conn.execute(
                    delivery_trades.insert().values(
                        series_key=series_key,
                        instrument_key=entry.instrument_key,
                        strategy=self.strategy,
                        timeframe=self.timeframe,
                        signal_date=session.isoformat(),
                        signal_close=float(entry.signal_close),
                        qty=int(entry.qty),
                        status=entry.status,
                        note=entry.note,
                        created_at=now,
                        entry_late=False,
                        exit_late=False,
                        plan=dict(entry.plan),
                        updated_at=now,
                    )
                ).inserted_primary_key
                new_id = int(key[0]) if key is not None else None
        except IntegrityError:
            return None  # evaluated already (another poll or desk): nothing written twice
        return None if new_id is None else self.get(new_id)

    def _cas(self, trade_id: int, expected: Iterable[str], now: dt.datetime, **values: Any) -> bool:
        with self.engine.begin() as conn:
            res = conn.execute(
                update(delivery_trades)
                .where(
                    and_(delivery_trades.c.id == trade_id, delivery_trades.c.status.in_(expected))
                )
                .values(updated_at=now, **values)
            )
        return bool(res.rowcount == 1)

    def mark_told(self, trade_id: int, which: str, now: dt.datetime) -> None:
        col = {"entry": "entry_told_at", "exit": "exit_told_at"}[which]
        with self.engine.begin() as conn:
            conn.execute(
                update(delivery_trades).where(delivery_trades.c.id == trade_id).values({col: now})
            )

    def fill_entry(
        self,
        trade_id: int,
        ts: dt.datetime,
        session: dt.date,
        fill_price: float,
        late: bool,
        now: dt.datetime,
    ) -> bool:
        return self._cas(
            trade_id,
            (PENDING_ENTRY,),
            now,
            status=OPEN,
            entry_ts=ts,
            entry_session=session.isoformat(),
            entry_paise=rupees_to_paise(fill_price),
            entry_late=bool(late),
        )

    def miss_entry(self, trade_id: int, why: str, now: dt.datetime) -> bool:
        return self._cas(trade_id, (PENDING_ENTRY,), now, status=MISSED, note=why)

    def signal_exit(
        self,
        trade_id: int,
        session: dt.date,
        reasons: Iterable[str],
        sessions_held: int,
        now: dt.datetime,
    ) -> bool:
        return self._cas(
            trade_id,
            (OPEN,),
            now,
            status=EXIT_PENDING,
            exit_signal_date=session.isoformat(),
            exit_reason=",".join(reasons),
            sessions_held=int(sessions_held),
        )

    def fill_exit(
        self, trade_id: int, ts: dt.datetime, fill_price: float, late: bool, now: dt.datetime
    ) -> bool:
        t = self.get(trade_id)
        if t is None or t.status != EXIT_PENDING or t.entry_paise is None:
            return False
        exit_paise = rupees_to_paise(fill_price)
        r = trade_result(self.model, t.entry_paise, exit_paise, t.qty, self.dp_paise)
        return self._cas(
            trade_id,
            (EXIT_PENDING,),
            now,
            status=CLOSED,
            exit_ts=ts,
            exit_paise=exit_paise,
            exit_late=bool(late),
            gross_paise=r.gross_paise,
            charges_paise=r.charges_paise,
            dp_paise=r.dp_paise,
            net_paise=r.net_paise,
            net_pct=r.net_pct,
        )

    def warn(self, trade_id: int, warning: str, now: dt.datetime) -> bool:
        """D15: a visible warning on an active trade, written once (the first one stays)."""
        with self.engine.begin() as conn:
            res = conn.execute(
                update(delivery_trades)
                .where(
                    and_(
                        delivery_trades.c.id == trade_id,
                        delivery_trades.c.status.in_(ACTIVE),
                        delivery_trades.c.warning.is_(None),
                    )
                )
                .values(warning=warning, updated_at=now)
            )
        return bool(res.rowcount == 1)

    # ── the scoreboard ────────────────────────────────────────────────────
    def by_slot(self) -> dict[Slot, StrategyTotals]:
        """The Home scoreboard's row of this book: closed trades (net known) and MISSED
        entries as no-fills. No R multiple: a delivery trade has no stop distance in rupees
        at the signal that the scoreboard's R would mean."""
        totals = StrategyTotals()
        for t in self._select(delivery_trades.c.status.in_((CLOSED, MISSED))):
            if t.status == MISSED:
                totals.no_fill += 1
                continue
            if t.net_paise is None or t.gross_paise is None or t.charges_paise is None:
                continue
            totals.trades += 1
            totals.gross_paise += t.gross_paise
            totals.costs_paise += t.charges_paise + (t.dp_paise or 0)
            totals.net_paise += t.net_paise
            if t.net_paise > 0:
                totals.wins += 1
                totals.won_paise += t.net_paise
            elif t.net_paise < 0:  # as the paper ledger counts: a zero is neither
                totals.losses += 1
                totals.lost_paise += t.net_paise
            if t.exit_ts is not None and (totals.last_exit is None or t.exit_ts > totals.last_exit):
                totals.last_exit = t.exit_ts
        slot = Slot(self.strategy, self.timeframe, "STOCK")
        return {slot: totals} if totals.trades or totals.no_fill else {}

    def open_count(self) -> int:
        return len([t for t in self.active() if t.status in (OPEN, EXIT_PENDING)])
