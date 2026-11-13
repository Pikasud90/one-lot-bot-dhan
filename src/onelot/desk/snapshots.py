"""US-071: the market snapshot. Every tracked price at one moment, saved, to compare by hand.

Read-only on the desk: the rows come from `Desk.snapshot()`; nothing here touches an order.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

from sqlalchemy import desc, insert, select

from ..store.schema import market_snapshots

UTC = dt.timezone.utc


class NoPrices(Exception):
    """The desk holds no price at all yet (still booting): nothing is saved."""


def _num(v: Any) -> float | None:
    """A finite float, else None (a missing price is null, never NaN)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def build_rows(desk_snapshot: dict) -> list[dict]:
    """One row per instrument, the desk's order: the indices, then the stocks."""
    rows = []
    for d in [*(desk_snapshot.get("indices") or []), *(desk_snapshot.get("stocks") or [])]:
        last, health = d.get("last") or {}, d.get("health") or {}
        rows.append(
            {
                "key": d.get("key"),
                "label": d.get("label"),
                "kind": d.get("kind"),
                "price": _num(last.get("price")),
                "prev_close": _num(last.get("prev_close")),
                "change_pct": _num(last.get("change_pct")),
                "bar_ts": last.get("bar_ts"),
                "source": health.get("source"),
                "delayed": health.get("delayed"),
            }
        )
    return rows


def _view(row: dict, rows: list[dict], older: list[dict] | None) -> dict:
    """A stored snapshot with `change_since_prev_pct` against the snapshot taken before it."""
    before = {r.get("key"): r.get("price") for r in (older or [])}
    out = []
    for r in rows:
        p, q = _num(r.get("price")), _num(before.get(r.get("key")))
        chg = (p / q - 1) * 100 if older is not None and p is not None and q else None
        out.append({**r, "change_since_prev_pct": chg})
    taken = row["taken_at"].astimezone(UTC)
    return {
        "id": row["id"],
        "taken_at": taken.isoformat(),
        "count": sum(r["price"] is not None for r in rows),
        "rows": out,
    }


def list_snapshots(db: Any, limit: int = 30) -> list[dict]:
    """The newest `limit` snapshots, newest first."""
    stored = db.fetch_all(
        select(market_snapshots)
        .order_by(desc(market_snapshots.c.taken_at), desc(market_snapshots.c.id))
        .limit(limit + 1)
    )
    return [
        _view(s, s["rows"], stored[i + 1]["rows"] if i + 1 < len(stored) else None)
        for i, s in enumerate(stored[:limit])
    ]


def take_snapshot(db: Any, desk_snapshot: dict, now: dt.datetime | None = None) -> dict:
    """Save the desk's prices now and return the snapshot. Raises NoPrices when it holds none."""
    rows = build_rows(desk_snapshot)
    if not any(r["price"] is not None for r in rows):
        raise NoPrices("The desk has no prices yet (it is still starting). Try again in a minute.")
    with db.begin() as conn:
        conn.execute(
            insert(market_snapshots).values(taken_at=now or dt.datetime.now(UTC), rows=rows)
        )
    return list_snapshots(db, 1)[0]
