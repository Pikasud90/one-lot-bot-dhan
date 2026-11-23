"""US-079 AC11: `scripts/ratchet_desk_replay.py` runs on a small fixture store: the micro-trend
slot's signals on stored one-minute bars, each one's paper trade through the real PaperBook,
and every exit compared with a direct loop over `manage/ratchet.py`. The store is opened
read-only (its bytes are unchanged afterwards)."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from onelot.clock import IST
from onelot.data.store import save_bars
from onelot.store import init_database

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ratchet_desk_replay.py"


def fixture_store(path: Path, sessions: int = 45) -> None:
    """`sessions` weekdays of one-minute NIFTY bars, a trending random walk."""
    rng = np.random.default_rng(7)
    days, d = [], dt.date(2026, 10, 30)
    while len(days) < sessions:
        if d.weekday() < 5:
            days.append(d)
        d -= dt.timedelta(days=1)
    frames, price = [], 24000.0
    for k, day in enumerate(sorted(days)):
        idx = pd.date_range(dt.datetime.combine(day, dt.time(9, 15), IST), periods=375,
                            freq="1min")  # fmt: skip
        drift = 0.0004 * np.sin(k / 3.0)
        c = price * np.exp(np.cumsum(rng.normal(drift, 0.0007, 375)))
        o = np.r_[price, c[:-1]]
        wick = np.abs(rng.normal(0, 0.0004, 375)) * c
        frames.append(pd.DataFrame({"open": o, "high": np.maximum(o, c) + wick,
                                    "low": np.minimum(o, c) - wick, "close": c,
                                    "volume": 0.0}, index=idx))  # fmt: skip
        price = float(c[-1])
    db = init_database(f"sqlite:///{path}")
    save_bars(db.engine, "NIFTY", "1m", pd.concat(frames), "fixture")
    db.dispose()


def test_the_replay_runs_and_every_exit_matches(tmp_path, capsys, monkeypatch) -> None:
    store = tmp_path / "store.db"
    fixture_store(store)
    before = hashlib.sha256(store.read_bytes()).hexdigest()
    spec = importlib.util.spec_from_file_location("ratchet_desk_replay", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "ratchet_desk_replay", module)  # its dataclass needs it
    spec.loader.exec_module(module)

    code = module.main(["--store", str(store), "--sessions", "8", "--instrument", "nifty"])

    out = capsys.readouterr().out
    assert code == 0, out
    assert "RESULT: PASS" in out
    line = next(x for x in out.splitlines() if x.startswith("NIFTY:"))
    assert int(line.split(", ")[1].split()[0]) >= 1, out  # at least one trade compared
    assert hashlib.sha256(store.read_bytes()).hexdigest() == before
