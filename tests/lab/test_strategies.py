"""Every registered strategy: valid signal frames, window discipline, no look-ahead."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest
from tests.lab.conftest import make_intraday

from onelot.strategies.base import Context
from onelot.strategies.registry import BENCHMARKS, REGISTRY, get_strategy


@pytest.mark.parametrize("name", list(REGISTRY) + list(BENCHMARKS))
def test_every_strategy_generates_valid_signals(name):
    df = make_intraday(days=12, seed=3, drift=0.0002, vol=0.002)
    market = make_intraday(days=12, seed=9, vol=0.001, base=22000).assign(volume=0.0)
    strat = get_strategy(name, {"timeframe": "5m"})
    f = strat.generate(df, Context(symbol="X", market=market))
    for col in ("long_entry", "short_entry", "long_stop", "short_stop", "long_score", "short_score",
                "long_exit", "short_exit"):
        assert col in f.columns, col
    assert f["long_entry"].dtype == bool and f["short_entry"].dtype == bool
    le = f[f["long_entry"]]
    se = f[f["short_entry"]]
    assert (le["long_stop"] < le["close"]).all()
    assert (se["short_stop"] > se["close"]).all()
    assert f["long_score"].between(0, 100).all() and f["short_score"].between(0, 100).all()
    p = strat.p
    t = (f.index + pd.Timedelta(minutes=5)).time
    start = dt.time(*map(int, p.entry_start.split(":")))
    end = dt.time(*map(int, p.entry_end.split(":")))
    entries = f["long_entry"] | f["short_entry"]
    assert all(start <= x <= end for x in np.array(t)[entries.to_numpy()])


@pytest.mark.parametrize("name", list(REGISTRY))
def test_signals_do_not_depend_on_future_bars(name):
    """Signals on a truncated history must equal those on the full history."""
    df = make_intraday(days=12, seed=5, drift=0.0003, vol=0.002)
    strat = get_strategy(name, {"timeframe": "5m"})
    full = strat.generate(df)
    cut = len(df) - 40
    part = strat.generate(df.iloc[:cut])
    a = full["long_entry"].iloc[:cut].to_numpy() | full["short_entry"].iloc[:cut].to_numpy()
    b = part["long_entry"].to_numpy() | part["short_entry"].to_numpy()
    assert (a == b).all(), f"{name} uses future data"


@pytest.mark.parametrize("name", ["ema_cross", "supertrend_vol", "donchian_breakout", "intraday_momentum"])
def test_volume_free_series_still_signals(name):
    """Indices and FX have no volume: volume filters must be neutral, not always-false."""
    df = make_intraday(days=15, seed=11, drift=0.0004, vol=0.0025, with_volume=False)
    strat = get_strategy(name, {"timeframe": "5m"})
    f = strat.generate(df)
    assert f["rvol"].isna().all()
    assert (f["setup_long"] | f["setup_short"]).sum() > 0, f"{name} produced no setups without volume"


def test_param_space_and_docs():
    for name, cls in REGISTRY.items():
        space = cls.param_space()
        docs = cls.param_docs()
        assert space, name
        for k, (lo, hi, step) in space.items():
            assert lo <= hi and step > 0 and k in docs


def test_ema_cross_fires_once_per_cross():
    df = make_intraday(days=20, seed=21, drift=0.0005, vol=0.002)
    strat = get_strategy("ema_cross", {"timeframe": "5m", "adx_min": 0, "rsi_margin": 0, "confirm_atr": 0})
    f = strat.generate(df)
    above = f["ema_fast"] > f["ema_slow"]
    crosses = int((above & ~above.shift(1, fill_value=False)).sum())
    assert 0 < int(f["setup_long"].sum()) <= crosses


def test_intraday_momentum_decides_only_at_entry_time():
    df = make_intraday(days=15, seed=4, drift=0.0, vol=0.003)
    strat = get_strategy("intraday_momentum", {"timeframe": "5m", "min_open_move_atr": 0.0})
    f = strat.generate(df)
    entries = f[f["long_entry"] | f["short_entry"]]
    assert len(entries) > 0
    assert all((ts + pd.Timedelta(minutes=5)).time() == dt.time(14, 45) for ts in entries.index)
