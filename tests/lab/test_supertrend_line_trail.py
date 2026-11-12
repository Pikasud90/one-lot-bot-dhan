"""`line_trail` of the two plain Supertrend strategies (2026-11-11): the stop follows the
Supertrend line while a trade is open, the rule the owner asked for.

The option is off unless a slot asks for it, so the paper-test slots that have been running
keep their logic until their 30-trade review. Off, the strategy's frame is what it was. On,
the frame gains the level the line allows on the side the line is on, and nothing else
changes: the same entries, the same first stops. The simulator then moves the stop along that
level (tests/lab/test_simulator_line_trail.py pins its rules on hand-made frames).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from tests.lab.conftest import make_intraday

from onelot.lab.simulator import simulate
from onelot.strategies.registry import get_strategy

PLAIN = ("supertrend_vol", "supertrend_adx")
#: (seed, drift, volatility) of forty sessions of five-minute bars
WORLDS = [(4, 0.0003, 0.002), (8, 0.0004, 0.003), (3, 0.0002, 0.002), (11, 0.0, 0.002)]
TRAIL_COLUMNS = ["long_trail", "short_trail"]


def bars_of(world: tuple[int, float, float]) -> pd.DataFrame:
    seed, drift, vol = world
    return make_intraday(days=40, seed=seed, drift=drift, vol=vol)


def run(name: str, world: tuple[int, float, float], **params):
    """(the strategy's frame, its trades) on one world."""
    strategy = get_strategy(name, {"timeframe": "5m", **params})
    frame = strategy.generate(bars_of(world))
    return frame, simulate(frame, strategy.p, "X", name)


@pytest.mark.parametrize("name", PLAIN)
def test_the_option_is_off_unless_asked_for(name):
    strategy = get_strategy(name, {"timeframe": "5m"})

    frame = strategy.generate(bars_of(WORLDS[0]))

    assert strategy.p.line_trail is False
    assert not set(TRAIL_COLUMNS) & set(frame.columns)


@pytest.mark.parametrize("name", PLAIN)
@pytest.mark.parametrize("world", WORLDS)
def test_on_only_the_two_levels_are_added(name, world):
    off, _ = run(name, world)
    on, _ = run(name, world, line_trail=True)

    assert sorted(set(on.columns) - set(off.columns)) == TRAIL_COLUMNS
    for col in off.columns:
        assert on[col].equals(off[col]), col  # the same entries, stops, exits and scores


@pytest.mark.parametrize("name", PLAIN)
@pytest.mark.parametrize("world", WORLDS)
def test_the_level_is_the_stop_at_the_line_on_the_side_the_line_is_on(name, world):
    f, _ = run(name, world, line_trail=True)
    up, down = (f["st_dir"] == 1).to_numpy(), (f["st_dir"] == -1).to_numpy()
    long_level, short_level = f["long_trail"].to_numpy(float), f["short_trail"].to_numpy(float)
    long_stop, short_stop = f["long_stop"].to_numpy(float), f["short_stop"].to_numpy(float)

    assert up.any() and down.any()
    # (the first bars of the history have no ATR yet: no stop there, and so no level)
    assert np.array_equal(long_level[up], long_stop[up], equal_nan=True)
    assert np.array_equal(short_level[down], short_stop[down], equal_nan=True)
    assert np.isnan(long_level[~up]).all(), "no long level while the line is above the price"
    assert np.isnan(short_level[~down]).all()
    # a long's level is under the close of its bar, a short's above it
    close = f["close"].to_numpy(float)
    below, above = up & ~np.isnan(long_level), down & ~np.isnan(short_level)
    assert below.sum() > 100 and above.sum() > 100
    assert (long_level[below] < close[below]).all()
    assert (short_level[above] > close[above]).all()


@pytest.mark.parametrize("name", PLAIN)
def test_the_stop_buffer_moves_the_level_with_the_stop(name):
    near, _ = run(name, WORLDS[0], line_trail=True, stop_buffer_atr=0.0)
    far, _ = run(name, WORLDS[0], line_trail=True, stop_buffer_atr=1.0)
    up = (near["st_dir"] == 1).to_numpy() & near["atr"].notna().to_numpy()

    line = near["st_line"].to_numpy(float)[up]
    assert np.allclose(near["long_trail"].to_numpy(float)[up], line)
    assert np.allclose(far["long_trail"].to_numpy(float)[up], line - far["atr"].to_numpy(float)[up])


@pytest.mark.parametrize("name", PLAIN)
@pytest.mark.parametrize("world", WORLDS)
def test_in_the_simulator_the_first_trade_starts_the_same_and_ends_no_later(name, world):
    _, plain = run(name, world)
    _, trailed = run(name, world, line_trail=True)

    a, b = plain[0], trailed[0]
    assert (a.signal_ts, a.side, a.entry_price, a.stop_initial) == (
        b.signal_ts,
        b.side,
        b.entry_price,
        b.stop_initial,
    )
    assert b.exit_ts <= a.exit_ts, "a stop that only tightens cannot keep a trade open longer"


@pytest.mark.parametrize("name", PLAIN)
@pytest.mark.parametrize("world", WORLDS)
def test_a_trade_the_line_stops_out_ends_beyond_its_first_stop(name, world):
    """The stop only ever moved in the trade's favour: an exit at the trailed stop is on the
    profit side of the stop the trade began with (the fill's slippage aside)."""
    _, trailed = run(name, world, line_trail=True)
    stopped = [t for t in trailed if t.exit_reason == "trail"]

    assert stopped, "this world has trades the line stopped out"
    for t in stopped:
        slack = 0.001 * t.stop_initial
        assert t.side * (t.avg_exit - t.stop_initial) > -slack, t.signal_ts


@pytest.mark.parametrize("name", PLAIN)
def test_the_line_stops_trades_out_that_used_to_wait_for_the_flip(name):
    """Without the trail the stop stays where it began and the trade waits for the close that
    flips the Supertrend; with it the stop is already at the line when the price gets there."""
    reasons = {False: set(), True: set()}
    for world in WORLDS:
        for on in (False, True):
            _, trades = run(name, world, line_trail=on)
            reasons[on] |= {t.exit_reason for t in trades}

    assert "signal" in reasons[False]
    assert "trail" in reasons[True]
