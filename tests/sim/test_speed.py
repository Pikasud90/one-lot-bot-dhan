"""US-075 non-functional: a session on 120 days of one-minute bars is created in under 5 s and a
warm one-minute advance answers in under 50 ms (the micro-trend rule, the heaviest one)."""

from __future__ import annotations

import time

import numpy as np
import pandas as pd

from onelot.clock import IST

from .conftest import MemBars, make_manager, trading_days, walk_minutes


def test_create_and_advance_are_fast(db_engine, tmp_path):
    nifty = walk_minutes("2025-01-06", 110, 25000.0, 0.0006, seed=21, volume=False)
    day = trading_days("2025-01-06", 110)[-15]
    m = make_manager(db_engine, MemBars({"NIFTY": nifty}), tmp_path)
    t0 = time.perf_counter()
    out = m.create(
        {"instrument": "NIFTY", "strategy": "microtrend", "timeframe": "3m", "mode": "past",
         "start": pd.Timestamp(f"{day} 09:15", tz=IST).isoformat()}
    )  # fmt: skip
    created = time.perf_counter() - t0
    s = m.get(out["session"]["id"])
    s.advance(1)
    took = []
    for _ in range(60):
        a = time.perf_counter()
        s.advance(1)
        took.append(time.perf_counter() - a)
    assert created < 5.0, f"creating took {created:.2f} s"
    assert float(np.median(took)) < 0.05, f"an advance took {np.median(took) * 1000:.0f} ms"
