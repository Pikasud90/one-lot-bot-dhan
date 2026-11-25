"""The Simulator's tables (`sim_sessions`, `sim_trades`, `sim_events`): its only writes.

Nothing here touches `paper_trades`, `paper_events`, `desk_signals` or `desk_positions`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import Engine, delete, desc, func, insert, select, update

from ..clock import IST
from ..store.schema import METADATA, sim_events, sim_sessions, sim_trades

#: the trade columns the contract shows, in its order (SIM-API.md)
TRADE_FIELDS = (
    "id",
    "status",
    "side",
    "action",
    "qty",
    "entry_ts",
    "entry",
    "entry_price",
    "source",
    "stop",
    "target",
    "stop0",
    "target0",
    "stage",
    "best",
    "progress",
    "mark",
    "mark_price",
    "open_pnl",
    "exit_ts",
    "exit",
    "exit_price",
    "exit_reason",
    "gross",
    "charges",
    "net",
    "r_multiple",
)


def _ist(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(IST)


class SimStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        METADATA.create_all(engine, tables=[sim_sessions, sim_trades, sim_events])

    # ── sessions ──────────────────────────────────────────────────────────
    def add_session(self, row: dict[str, Any]) -> None:
        with self.engine.begin() as c:
            c.execute(insert(sim_sessions).values(**row))

    def save_session(self, sid: str, cursor: dt.datetime, status: str, params: dict) -> None:
        with self.engine.begin() as c:
            c.execute(
                update(sim_sessions)
                .where(sim_sessions.c.id == sid)
                .values(cursor_ts=cursor, status=status, params=params)
            )

    def session(self, sid: str) -> dict[str, Any] | None:
        with self.engine.connect() as c:
            row = c.execute(select(sim_sessions).where(sim_sessions.c.id == sid)).mappings().first()
        if row is None:
            return None
        out = dict(row)
        for k in ("created_at", "start_ts", "cursor_ts"):
            out[k] = _ist(out[k])
        return out

    def sessions(self) -> dict[str, dict[str, Any]]:
        """id -> (instrument_key, strategy, timeframe, mode) of every session."""
        q = select(
            sim_sessions.c.id,
            sim_sessions.c.instrument_key,
            sim_sessions.c.strategy,
            sim_sessions.c.timeframe,
            sim_sessions.c.mode,
        )
        with self.engine.connect() as c:
            return {r.id: dict(r._mapping) for r in c.execute(q)}

    # ── trades ────────────────────────────────────────────────────────────
    def add_trade(self, row: dict[str, Any]) -> int:
        with self.engine.begin() as c:
            res = c.execute(insert(sim_trades).values(**row))
            key = res.inserted_primary_key
            assert key is not None
            return int(key[0])

    def save_trade(self, tid: int, fields: dict[str, Any]) -> None:
        with self.engine.begin() as c:
            c.execute(update(sim_trades).where(sim_trades.c.id == tid).values(**fields))

    def drop_trade(self, tid: int) -> None:
        with self.engine.begin() as c:
            c.execute(delete(sim_trades).where(sim_trades.c.id == tid))

    def trades(
        self,
        session_id: str | None = None,
        status: str | None = None,
        limit: int | None = None,
        exclude_status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Trades newest first (by exit, then id)."""
        q = select(sim_trades)
        if session_id is not None:
            q = q.where(sim_trades.c.session_id == session_id)
        if status is not None:
            q = q.where(sim_trades.c.status == status)
        if exclude_status is not None:
            q = q.where(sim_trades.c.status != exclude_status)
        q = q.order_by(desc(sim_trades.c.exit_ts), desc(sim_trades.c.id))
        if limit is not None:
            q = q.limit(limit)
        with self.engine.connect() as c:
            rows = [dict(r) for r in c.execute(q).mappings()]
        for r in rows:
            for k in ("entry_ts", "exit_ts", "updated_at"):
                r[k] = _ist(r.get(k))
        return rows

    # ── events ────────────────────────────────────────────────────────────
    def save_step(
        self,
        session_id: str,
        events: list[dict[str, Any]],
        trade: tuple[int, dict[str, Any]] | None = None,
        session: tuple[dt.datetime, str, dict] | None = None,
    ) -> None:
        """The events of a step, the open trade and the session's cursor in ONE transaction
        (server review F8: a kill between two writes left events with an older cursor)."""
        if not events and trade is None and session is None:
            return
        with self.engine.begin() as c:
            if events:
                c.execute(insert(sim_events), self._event_rows(session_id, events))
            if trade is not None:
                tid, fields = trade
                c.execute(update(sim_trades).where(sim_trades.c.id == tid).values(**fields))
            if session is not None:
                cursor, status, params = session
                c.execute(
                    update(sim_sessions)
                    .where(sim_sessions.c.id == session_id)
                    .values(cursor_ts=cursor, status=status, params=params)
                )

    @staticmethod
    def _event_rows(session_id: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "session_id": session_id,
                "seq": e["seq"],
                "ts": e["_ts"],
                "kind": e["kind"],
                "text": e["text"],
                "data": e.get("data") or {},
            }
            for e in events
        ]

    def add_events(self, session_id: str, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        rows = [
            {
                "session_id": session_id,
                "seq": e["seq"],
                "ts": e["_ts"],
                "kind": e["kind"],
                "text": e["text"],
                "data": e.get("data") or {},
            }
            for e in events
        ]
        with self.engine.begin() as c:
            c.execute(insert(sim_events), rows)

    def events(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """The session's last `limit` events, oldest first, in the contract's shape."""
        q = (
            select(sim_events)
            .where(sim_events.c.session_id == session_id)
            .order_by(desc(sim_events.c.seq))
            .limit(limit)
        )
        with self.engine.connect() as c:
            rows = [dict(r) for r in c.execute(q).mappings()]
        out = []
        for r in reversed(rows):
            ts = _ist(r["ts"])
            out.append(
                {
                    "seq": int(r["seq"]),
                    "ts": ts.isoformat() if ts is not None else None,
                    "kind": r["kind"],
                    "text": r["text"],
                    "data": r.get("data") or {},
                }
            )
        return out

    def last_seq(self, session_id: str) -> int:
        q = select(func.max(sim_events.c.seq)).where(sim_events.c.session_id == session_id)
        with self.engine.connect() as c:
            got = c.execute(q).scalar()
        return int(got or 0)
