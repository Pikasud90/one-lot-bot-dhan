"""US-095 point 5 and AC5: the strategy's lines (`plots`) never look ahead.

For every strategy with plots, on fixture bars: a value sent for a candle is the same when the
bars after that candle's close are cut away (truncation at random cursors and inside the opening half hour), every plot has
values, and the overlays an `advance` sends are only of candles that closed in its minutes."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from onelot.data.universe import load_specs
from onelot.lab.runner import _session_params
from onelot.sim.engine import build_signals
from onelot.sim.manager import strategy_kinds, strategy_timeframes
from onelot.sim.plots import PLOTS, plot_specs, plot_values
from onelot.strategies.registry import REGISTRY, get_strategy

from .conftest import MemBars, make_manager, trading_days, walk_minutes

MIN = pd.Timedelta(minutes=1)


@pytest.fixture(scope="module")
def bars() -> MemBars:
    return MemBars(
        {
            "NIFTY": walk_minutes("2025-01-06", 60, 25000.0, 0.0006, seed=41, volume=False),
            "RELIANCE": walk_minutes("2025-01-06", 60, 1300.0, 0.0008, seed=42, volume=True),
        }
    )


def pick(name: str) -> tuple[str, str]:
    return ("NIFTY", "NIFTY_OPT") if "INDEX" in strategy_kinds(name) else ("RELIANCE", "RELIANCE")


def test_every_declared_strategy_exists_and_the_rest_send_none():
    assert set(PLOTS) <= set(REGISTRY)
    for name in REGISTRY:
        specs = plot_specs(name)
        assert [p["id"] for p in specs] == [p.id for p in PLOTS.get(name, ())]
        assert all(p["pane"] == "price" and p["label"] for p in specs)
    assert plot_values("gap_fade", pd.DataFrame({"x": [1.0]}), None, "5m") == {}


@pytest.mark.parametrize("name", sorted(PLOTS))
def test_a_value_is_the_same_when_later_bars_are_cut(bars, name):
    series, specs = load_specs()
    key, tracked = pick(name)
    spec = specs[tracked]
    sp = _session_params(spec, {}, name)
    _, tf = strategy_timeframes(name)
    minute = bars.frames[key]
    strat = get_strategy(name, {**sp, "timeframe": tf})
    full, candles = build_signals(strat, spec, key, tf, minute, bars, series[key])
    assert full is not None
    vals = plot_values(name, full, strat.p, tf)
    assert set(vals) == {p.id for p in PLOTS[name]}
    for pid, v in vals.items():
        assert np.isfinite(v).sum() > 100, f"{name} {pid} has almost no values"
    ends = pd.DatetimeIndex(candles.index) + pd.Timedelta(minutes=int(tf[:-1]))
    rng = np.random.default_rng(9)
    late = minute.index[minute.index >= minute.index[0] + pd.Timedelta(days=30)]
    # random cursors, and cursors inside the first half hour of a day (an opening range
    # is only known once it is over)
    days = sorted({t.date() for t in late})[-2:]
    early = [pd.Timestamp(f"{d} {hm}", tz=minute.index.tz) for d in days
             for hm in ("09:19", "09:24", "09:34", "09:44")]  # fmt: skip
    for cur in sorted([*rng.choice(late, size=6, replace=False), *early]):
        cur = pd.Timestamp(cur)
        cut = minute[minute.index <= cur]
        s2 = get_strategy(name, {**sp, "timeframe": tf})
        f_cut, _ = build_signals(s2, spec, key, tf, cut, bars, series[key])
        got = plot_values(name, f_cut, s2.p, tf)
        closed = np.flatnonzero(ends <= cur + MIN)
        for pid in vals:
            a = vals[pid][closed]
            b = got[pid][: len(closed)]
            assert np.array_equal(np.isnan(a), np.isnan(b)), (name, pid, cur)
            np.testing.assert_allclose(a[~np.isnan(a)], b[~np.isnan(b)], rtol=0, atol=1e-9)


def test_overlays_of_an_advance_are_the_candles_that_closed_in_it(db_engine, tmp_path, bars):
    m = make_manager(db_engine, bars, tmp_path)
    day = trading_days("2025-01-06", 60)[45]
    s = m.get(
        m.create({"instrument": "NIFTY", "strategy": "microtrend", "timeframe": "3m",
                  "mode": "past", "start": f"{day}T10:00:00+05:30"})["session"]["id"]
    )  # fmt: skip
    seen: list[int] = []
    for n in (1, 2, 7, 30, 1, 1, 13):
        before = pd.Timestamp(s.cursor)
        r = s.advance(n)
        after = pd.Timestamp(r["cursor"])
        pts = r["overlays"]["st"]
        for t, _ in pts:
            close = pd.Timestamp(t, unit="s", tz="UTC") + pd.Timedelta(minutes=3)
            assert before + MIN < close <= after + MIN  # closed within these minutes
        seen += [t for t, _ in pts]
    assert seen == sorted(set(seen)) and len(seen) > 10
    read = s.read()["overlays"]["st"]
    assert [t for t, _ in read][-len(seen) :] == seen  # the read holds the same points
