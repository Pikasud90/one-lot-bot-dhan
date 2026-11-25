"""`scripts/merge_option_bars.py`: copies only the blocks the target lacks, never overwrites,
writes nothing on a dry run, and what it copies reads back through the Simulator's store."""

from __future__ import annotations

import datetime as dt
import importlib.util
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

from onelot.clock import IST
from onelot.data.expired_options import FIELDS, Block, OptionBarStore, RollingKey
from onelot.sim.pricing import ReadOnlyOptionStore

ROOT = Path(__file__).resolve().parents[1]


def _load():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location("merge_option_bars_t", ROOT / "scripts" /
                                                  "merge_option_bars.py")  # fmt: skip
    mod = importlib.util.module_from_spec(spec)
    sys.modules["merge_option_bars_t"] = mod
    spec.loader.exec_module(mod)
    return mod


M = _load()
D1, D2 = dt.date(2024, 7, 23), dt.date(2024, 7, 24)
PUT = RollingKey("NIFTY", 1, "PE", 0)
CALL = RollingKey("NIFTY", 1, "CE", 0)


def _block(key: RollingKey, day: dt.date, price: float, n: int = 3) -> Block:
    t0 = int(dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST).timestamp())
    data = {"ts": np.array([t0 + 60 * i for i in range(n)], dtype=np.int64)}
    for f in FIELDS:
        data[f] = np.full(n, 24000.0 if f in ("strike", "spot") else price)
    return Block(key, day, True, data)


def _stores(tmp_path: Path) -> tuple[Path, Path]:
    src, dst = tmp_path / "research.sqlite", tmp_path / "app_copy.db"
    s = OptionBarStore(src)
    for b in (_block(PUT, D1, 100.0), _block(PUT, D2, 110.0), _block(CALL, D1, 999.0),
              _block(PUT, dt.date(2024, 7, 27), 0.0, n=0)):  # fmt: skip
        s.put(b)
    s.close()
    t = OptionBarStore(dst)
    t.put(_block(CALL, D1, 120.0))  # the target's own call: must survive
    t.close()
    return src, dst


def _rows(path: Path) -> list[tuple]:
    db = sqlite3.connect(path)
    try:
        return db.execute(
            "SELECT underlying, opt, day, rows, data FROM blocks ORDER BY 1, 2, 3"
        ).fetchall()
    finally:
        db.close()


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    src, dst = _stores(tmp_path)
    before = _rows(dst)
    lines: list[str] = []
    counts = M.merge(src, dst, dry_run=True, out=lines.append)
    assert _rows(dst) == before
    assert counts == {"blocks": 3, "bars": 6, "days": 3, "held": 1, "differing": 1}
    assert lines[0].startswith("would add 3 blocks (6 bars) on 3 days")


def test_merge_adds_missing_never_overwrites_and_is_idempotent(tmp_path: Path) -> None:
    src, dst = _stores(tmp_path)
    M.merge(src, dst, out=lambda _s: None)
    store = ReadOnlyOptionStore(dst)
    put = store.get(PUT, D2)
    assert put is not None and put.complete and put.rows == 3
    assert float(put.data["close"][0]) == 110.0
    call = store.get(CALL, D1)
    assert call is not None and float(call.data["close"][0]) == 120.0  # kept, not replaced
    empty = store.get(PUT, dt.date(2024, 7, 27))
    assert empty is not None and empty.rows == 0  # an empty day is copied too: never asked again
    store.close()
    again = M.merge(src, dst, out=lambda _s: None)
    assert again["blocks"] == 0 and again["held"] == 4


def test_refuses_the_same_file_and_a_foreign_table(tmp_path: Path) -> None:
    src, _ = _stores(tmp_path)
    with pytest.raises(SystemExit):
        M.merge(src, src)
    other = tmp_path / "other.db"
    db = sqlite3.connect(other)
    db.execute("CREATE TABLE blocks (x INTEGER)")
    db.commit()
    db.close()
    with pytest.raises(SystemExit):
        M.merge(src, other)


def test_source_is_opened_read_only(tmp_path: Path) -> None:
    src, dst = _stores(tmp_path)
    before = _rows(src)
    src.chmod(0o444)
    try:
        M.merge(src, dst, out=lambda _s: None)
    finally:
        src.chmod(0o644)
    assert _rows(src) == before
