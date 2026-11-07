"""A long idempotent bar load commits batch by batch (owner feedback OF-3, Dhan 5-year backfill).

One transaction for five years of one-minute bars held SQLite's write lock for the whole load,
so the desk's own writes timed out ("database is locked"). `save_bars` now commits per batch.
"""

from __future__ import annotations

import pandas as pd
from sqlalchemy import event, func, select

from onelot.clock import IST
from onelot.data.schema import bars
from onelot.data.store import save_bars
from onelot.store import init_database


def frame(n: int) -> pd.DataFrame:
    idx = pd.date_range(pd.Timestamp("2026-11-02 09:15", tz=IST), periods=n, freq="1min")
    return pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0.0}, index=idx
    )


def test_bars_commit_per_batch_and_every_row_lands(tmp_path):
    db = init_database(f"sqlite:///{tmp_path / 'b.db'}")
    try:
        commits: list[int] = []
        event.listen(db.engine, "commit", lambda conn: commits.append(1))
        n = 7000  # 11 columns -> batches of 2727 rows -> 3 batches
        assert save_bars(db.engine, "X", "1m", frame(n), "dhan") == n
        assert len(commits) >= 3
        with db.engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(bars)).scalar_one() == n
        # idempotent: the same load again changes nothing
        assert save_bars(db.engine, "X", "1m", frame(n), "dhan") == n
        with db.engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(bars)).scalar_one() == n
    finally:
        db.dispose()
