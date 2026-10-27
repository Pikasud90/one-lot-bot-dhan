"""Database engine and initialisation.

SQLite only, deliberately. It ships with Python, installs nothing on either
macOS or Windows, and comfortably handles one user recording one instrument.
An earlier version carried an optional PostgreSQL/TimescaleDB path; it was a
documented-but-untested code branch serving a scale this system will never
reach, so it is gone.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.engine import Connection

from .schema import METADATA

log = logging.getLogger(__name__)


class Database:
    """Thin wrapper over a SQLAlchemy Engine with the SQLite pragmas that
    matter for a process writing ticks while another reads the dashboard."""

    def __init__(self, url: str, echo: bool = False):
        self.url = url
        self.is_sqlite = url.startswith("sqlite")
        self._lock = threading.Lock()
        self.engine: Engine = self._make_engine(url, echo)

    def _make_engine(self, url: str, echo: bool) -> Engine:
        if self.is_sqlite:
            # The project path may contain spaces; SQLAlchemy handles that
            # provided we do not pre-quote it. check_same_thread=False is
            # required because the feed, the alpha loop and the API all read.
            path = url.replace("sqlite:///", "")
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            eng = create_engine(
                url, echo=echo, future=True,
                connect_args={"check_same_thread": False, "timeout": 30.0},
            )

            @event.listens_for(eng, "connect")
            def _pragmas(dbapi_conn, _rec):  # pragma: no cover - driver callback
                cur = dbapi_conn.cursor()
                # WAL lets the dashboard read while the recorder writes.
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()

            return eng
        # Non-sqlite URLs still work through SQLAlchemy if someone insists,
        # but nothing here is tested against them.
        return create_engine(url, echo=echo, future=True, pool_pre_ping=True)

    # ── lifecycle ─────────────────────────────────────────────────────────
    def create_all(self) -> None:
        METADATA.create_all(self.engine)
        log.info("Database ready: %s", self._safe_url())

    def _safe_url(self) -> str:
        """Never log credentials embedded in a Postgres URL."""
        if "@" in self.url and "://" in self.url:
            scheme, rest = self.url.split("://", 1)
            return f"{scheme}://***@{rest.split('@', 1)[1]}"
        return self.url

    def dispose(self) -> None:
        self.engine.dispose()

    # ── helpers ───────────────────────────────────────────────────────────
    def connect(self) -> Connection:
        return self.engine.connect()

    def begin(self):
        return self.engine.begin()

    def execute(self, stmt, params: Any = None):
        with self.engine.begin() as conn:
            return conn.execute(stmt, params or {})

    def fetch_all(self, stmt, params: Any = None) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(stmt, params or {}).mappings().all()
        return [dict(r) for r in rows]

    def fetch_one(self, stmt, params: Any = None) -> dict | None:
        rows = self.fetch_all(stmt, params)
        return rows[0] if rows else None

    def table_names(self) -> list[str]:
        return sorted(inspect(self.engine).get_table_names())

    def upsert(self, table, values: dict, pk_cols: list[str]) -> None:
        """Cross-backend upsert.

        SQLAlchemy's dialect-specific `on_conflict_do_update` differs between
        SQLite and Postgres, so we use the portable
        try-update-then-insert-under-a-lock pattern. Volumes here are tiny
        (a handful of writes per minute), so the simplicity is worth more
        than the micro-optimisation.
        """
        from sqlalchemy import and_, select, update
        from sqlalchemy import insert as sa_insert

        cond = and_(*[table.c[c] == values[c] for c in pk_cols])
        with self._lock, self.engine.begin() as conn:
            existing = conn.execute(select(table).where(cond)).first()
            if existing:
                upd = {k: v for k, v in values.items() if k not in pk_cols}
                if upd:
                    conn.execute(update(table).where(cond).values(**upd))
            else:
                conn.execute(sa_insert(table).values(**values))

    def purge_older_than(self, table, ts_col: str, days: int) -> int:
        """Retention — v1 defect E4: unbounded tick logs fill the disk."""
        from sqlalchemy import delete

        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        with self.engine.begin() as conn:
            res = conn.execute(delete(table).where(table.c[ts_col] < cutoff))
        return res.rowcount or 0


def init_database(url: str, echo: bool = False) -> Database:
    db = Database(url, echo=echo)
    db.create_all()
    return db
