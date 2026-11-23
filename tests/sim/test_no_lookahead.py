"""US-075: a past session computes its signals once on all the bars it loaded. That is only
allowed because they equal the signals computed on the bars cut at the cursor: checked here
at 20 random cursors for `microtrend` (seven timeframes from the one-minute bars) and for
`ema_cross` (one timeframe, with the market index's bars and daily levels)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from onelot.data.universe import load_specs
from onelot.sim.engine import build_signals
from onelot.strategies.registry import get_strategy

from .conftest import MemBars, walk_minutes

COLS = ["long_entry", "short_entry", "long_exit", "short_exit"]
MIN = pd.Timedelta(minutes=1)


@pytest.fixture(scope="module")
def bars() -> MemBars:
    return MemBars(
        {
            "NIFTY": walk_minutes("2025-01-06", 80, 25000.0, 0.0006, seed=11, volume=False),
            "RELIANCE": walk_minutes("2025-01-06", 80, 1300.0, 0.0008, seed=12, volume=True),
        }
    )


@pytest.mark.parametrize(
    "key, tracked, strategy, tf",
    [("NIFTY", "NIFTY_OPT", "microtrend", "3m"), ("RELIANCE", "RELIANCE", "ema_cross", "5m")],
)
def test_signals_on_all_bars_equal_signals_at_the_cursor(bars, key, tracked, strategy, tf):
    series, specs = load_specs()
    spec = specs[tracked]
    params = {"entry_start": "09:30", "entry_end": "14:45", "exit_time": "15:15"}
    minute = bars.frames[key]
    strat = get_strategy(strategy, {**params, "timeframe": tf})
    full, candles = build_signals(strat, spec, key, tf, minute, bars, series[key])
    assert full is not None
    ends = pd.DatetimeIndex(candles.index) + pd.Timedelta(minutes=int(tf[:-1]))
    rng = np.random.default_rng(5)
    # cursors in the last 25 sessions, where every timeframe of the ladder has warmed up
    late = minute.index[minute.index >= minute.index[-1].normalize() - pd.Timedelta(days=35)]
    cursors = sorted(rng.choice(late, size=20, replace=False))
    compared = fired = 0
    for cur in cursors:
        cur = pd.Timestamp(cur)
        cut = minute[minute.index <= cur]
        s2 = get_strategy(strategy, {**params, "timeframe": tf})
        f_cut, _ = build_signals(s2, spec, key, tf, cut, bars, series[key])
        assert f_cut is not None
        closed = full.index[ends <= cur + MIN]
        a = full.loc[closed, COLS].astype(bool)
        b = f_cut.reindex(closed)[COLS].fillna(False).astype(bool)
        pd.testing.assert_frame_equal(a, b, check_names=False)
        compared += len(closed)
    fired = int(full.loc[late[0] :, COLS[:2]].to_numpy().sum())
    assert compared > 0 and fired > 0  # the comparison saw real signals
