"""US-095 AC7: `scripts/sim_budget.py` runs on a small fixture store, opens it read-only,
leaves nothing behind and reports every budget of the second contract with PASS or MISS."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

from onelot.store import init_database

from .conftest import store_frames, walk_minutes

ROOT = Path(__file__).resolve().parents[2]


def load_script():
    spec = importlib.util.spec_from_file_location("sim_budget", ROOT / "scripts" / "sim_budget.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_budget_script_on_a_fixture_store(tmp_path, capsys):
    path = tmp_path / "store.db"
    db = init_database(f"sqlite:///{path}")
    store_frames(
        db.engine,
        {
            "NIFTY": walk_minutes("2025-01-06", 40, 25000.0, 0.0006, seed=1, volume=False),
            "RELIANCE": walk_minutes("2025-01-06", 40, 1300.0, 0.0008, seed=2, volume=True),
        },
    )
    db.dispose()
    for extra in ("-wal", "-shm"):
        Path(f"{path}{extra}").unlink(missing_ok=True)
    before = hashlib.sha1(path.read_bytes()).hexdigest()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    sb = load_script()
    got = sb.measure(
        str(path),
        runs=1,
        cases=(
            ("NIFTY", "microtrend", "3m"),
            ("RELIANCE", "vwap_pullback", "5m"),
            ("HDFCBANK", "squeeze", "15m"),
        ),  # fmt: skip
        scratch=str(scratch),
        advances=20,
    )
    lines, _ = sb.report(got, 1)
    text = "\n".join(lines)
    for what in sb.BUDGETS:
        line = next(x for x in lines if x.strip().startswith(what))
        assert line.rstrip().split("   ")[2].strip() in ("PASS", "MISS") or (
            "PASS" in line or "MISS" in line
        ), line
    assert "HDFCBANK has no stored bars" in text  # a missing instrument is said, not a crash
    assert hashlib.sha1(path.read_bytes()).hexdigest() == before  # never written
    assert not Path(f"{path}-wal").exists() or Path(f"{path}-wal").stat().st_size == 0
    assert list(scratch.iterdir()) == []  # the simulator's own tables are gone
    assert sb.main(["--store", str(path), "--runs", "1", "--scratch", str(scratch)]) in (0, 1)
    assert "options cold" in capsys.readouterr().out
