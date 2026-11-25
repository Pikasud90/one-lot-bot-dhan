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


def verdict(line: str) -> str:
    words = line.split()
    return next(w for w in words if w in ("PASS", "MISS"))


def test_the_report_judges_a_known_fast_and_a_known_slow_case():
    """Server review F1 and F6: the best run for a steady budget, the WORST run for a tail
    budget (the create answer and the ticker), each against its exact budget."""
    sb = load_script()
    got = {
        "found": {
            "options cold": [2000.0, 10.0, 30.0],  # best run 10 ms: PASS
            "candles warm": [250.0, 260.0, 300.0],  # best run 250 ms against 200: MISS
            "create answer": [20.0, 400.0, 30.0],  # one run of 400 ms: MISS
            "ticker worst gap": [100.0, 300.0, 90.0],  # one run of 300 ms: MISS
            "ticker p99 gap": [10.0, 20.0, 30.0],  # worst run 30 ms: PASS
            "days cold": [900.0, 1200.0, 950.0],  # best 900 ms: PASS
        },
        "cpu_cold_ms": [],
        "notes": [],
        "cases": [],
        "starts": {},
    }
    lines, ok = sb.report(got, 3)
    want = {"options cold": "PASS", "candles warm": "MISS", "create answer": "MISS",
            "ticker worst gap": "MISS", "ticker p99 gap": "PASS", "days cold": "PASS"}  # fmt: skip
    for what, v in want.items():
        line = next(x for x in lines if x.strip().startswith(what))
        assert verdict(line) == v, line
    assert not ok
    assert sb.value_of("create answer", [20.0, 400.0, 30.0], 3) == 400.0
    assert sb.value_of("options cold", [2000.0, 10.0, 30.0], 3) == 10.0


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
        assert verdict(line) in ("PASS", "MISS"), line
    assert "HDFCBANK has no stored bars" in text  # a missing instrument is said, not a crash
    assert hashlib.sha1(path.read_bytes()).hexdigest() == before  # never written
    assert not Path(f"{path}-wal").exists() or Path(f"{path}-wal").stat().st_size == 0
    assert list(scratch.iterdir()) == []  # the simulator's own tables are gone
    assert sb.main(["--store", str(path), "--runs", "1", "--scratch", str(scratch)]) in (0, 1)
    assert "options cold" in capsys.readouterr().out
