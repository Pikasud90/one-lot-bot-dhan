"""US-095 point 2: a strategy's shorter history gives the SAME signals as 120 days.

For every strategy in `engine.WARMUP_DAYS`, on fixture bars (an index or a stock with the
market index), the entry and exit signals of every candle from the session's start on are
equal when the strategy's candles begin `WARMUP_DAYS` days before the window's block instead
of `HISTORY_DAYS`; on the strategy's default timeframe the comparison must see signals (a
strategy that fires too seldom on the fixture keeps 120 days). A strategy NOT in the table
reads 120 days.
"""

from __future__ import annotations

import pandas as pd
import pytest

from onelot.clock import IST
from onelot.data.universe import load_specs
from onelot.lab.runner import _session_params
from onelot.sim import engine as E
from onelot.sim.manager import strategy_kinds, strategy_timeframes
from onelot.strategies.registry import REGISTRY, get_strategy

from .conftest import MemBars, trading_days, walk_minutes

COLS = ["long_entry", "short_entry", "long_exit", "short_exit"]
DAYS = 200


@pytest.fixture(scope="module")
def bars() -> MemBars:
    return MemBars(
        {
            "NIFTY": walk_minutes("2024-01-01", DAYS, 22000.0, 0.0006, seed=7, volume=False),
            "RELIANCE": walk_minutes("2024-01-01", DAYS, 1300.0, 0.0008, seed=17, volume=True),
        }
    )


def test_every_named_strategy_exists_and_the_rest_read_120_days():
    assert set(E.WARMUP_DAYS) <= set(REGISTRY)
    for name in REGISTRY:
        w = E.warmup_days(name)
        assert w == E.WARMUP_DAYS.get(name, E.HISTORY_DAYS) and 0 < w <= E.HISTORY_DAYS


@pytest.mark.parametrize("name", sorted(E.WARMUP_DAYS))
def test_the_shorter_history_gives_the_same_signals(bars, name):
    series, specs = load_specs()
    key, tracked = ("NIFTY", "NIFTY_OPT") if "INDEX" in strategy_kinds(name) else (
        "RELIANCE", "RELIANCE")  # fmt: skip
    spec = specs[tracked]
    sp = _session_params(spec, {}, name)
    tfs, default = strategy_timeframes(name)
    days = trading_days("2024-01-01", DAYS)
    start = pd.Timestamp(f"{days[140]} 09:15", tz=IST)
    stop = pd.Timestamp(f"{days[199]} 15:30", tz=IST)
    w = E.window_at(name, int(start.value))
    long_lo = w.lo_strategy - pd.Timedelta(days=E.HISTORY_DAYS - E.warmup_days(name))
    assert w.lo is not None and w.lo <= long_lo  # the minutes reach back 120 days
    m = bars.frames[key]
    for tf in sorted({default, "60m"} & set(tfs) | {default}):

        def signals(lo: pd.Timestamp, tf: str = tf) -> pd.DataFrame:
            cut = m[(m.index >= lo) & (m.index < stop)]
            strat = get_strategy(name, {**sp, "timeframe": tf})
            f, _ = E.build_signals(strat, spec, key, tf, cut, bars, series[key])
            assert f is not None
            return f.loc[f.index >= start, COLS].astype(bool)

        short, full = signals(w.lo_strategy), signals(long_lo)
        pd.testing.assert_frame_equal(short, full, check_names=False)
        if tf == default:
            fired = int(full[COLS[:2]].to_numpy().sum())
            assert fired >= 5, f"{name} {tf}: only {fired} signals, too few to prove it"
