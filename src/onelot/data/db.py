"""Engine access for the data corpus and the Lab.

One SQLite file (WAL) holds everything: the live engine's tables and the
Lab's bars/runs. The live engine owns a `Database` instance; the Lab and the
CLI reach the same file through `get_engine()`.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import Engine, Table, select
from sqlalchemy.dialects import sqlite

from ..store.db import Database, init_database

_db: Database | None = None
_lock = threading.Lock()


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def get_database(url: str | None = None) -> Database:
    """Process-wide database handle (created on first use)."""
    global _db
    with _lock:
        if _db is None or (url and _db.url != url):
            if url is None:
                from ..settings import current_settings

                url = current_settings().db_url_resolved
            _db = init_database(url)
        return _db


def get_engine(url: str | None = None) -> Engine:
    return get_database(url).engine


def init_db(engine: Engine | None = None) -> Engine:
    return engine or get_engine()


def use_database(db: Database) -> None:
    """Point the Lab at an existing engine-owned database (the live process)."""
    global _db
    with _lock:
        _db = db


def upsert(
    engine: Engine,
    table: Table,
    rows: Sequence[dict[str, Any]],
    keys: Iterable[str],
    update_cols: Iterable[str] | None = None,
    chunk: int = 5000,
    per_chunk: bool = False,
) -> int:
    """INSERT ... ON CONFLICT (keys) DO UPDATE, batched. SQLite dialect.

    One transaction for every row by default. `per_chunk=True` commits each batch on its
    own, so a long idempotent load (five years of one-minute bars) never holds SQLite's
    write lock for long: the desk and the Lab write between batches."""
    if not rows:
        return 0
    keys = list(keys)
    cols = list(rows[0].keys())
    update_cols = [c for c in (update_cols or cols) if c not in keys]
    # SQLite caps bound parameters per statement (32766).
    chunk = max(1, min(chunk, 30000 // max(1, len(cols))))

    def statement(part: Sequence[dict[str, Any]]):
        stmt = sqlite.insert(table).values(list(part))
        if update_cols:
            return stmt.on_conflict_do_update(
                index_elements=keys, set_={c: getattr(stmt.excluded, c) for c in update_cols}
            )
        return stmt.on_conflict_do_nothing(index_elements=keys)

    parts = [rows[i : i + chunk] for i in range(0, len(rows), chunk)]
    if per_chunk:
        for part in parts:
            with engine.begin() as conn:
                conn.execute(statement(part))
        return len(rows)
    with engine.begin() as conn:
        for part in parts:
            conn.execute(statement(part))
    return len(rows)


def set_setting(engine: Engine, key: str, value: Any) -> None:
    from ..store.schema import app_settings

    upsert(engine, app_settings, [{"key": key, "value": value, "updated_at": utcnow()}], ["key"])


def get_setting(engine: Engine, key: str, default: Any = None) -> Any:
    from ..store.schema import app_settings

    with engine.connect() as conn:
        row = conn.execute(select(app_settings).where(app_settings.c.key == key)).mappings().first()
    return row["value"] if row else default


def log_event(
    engine: Engine, kind: str, message: str, level: str = "INFO", payload: Any = None
) -> None:
    from ..store.schema import engine_events

    try:
        with engine.begin() as conn:
            conn.execute(
                engine_events.insert().values(
                    ts=utcnow(), level=level, kind=kind, message=message, payload=payload
                )
            )
    except Exception:  # noqa: BLE001 - never let bookkeeping break a job
        pass
