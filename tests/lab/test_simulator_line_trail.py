"""The Lab simulator's line trail: the stop follows the strategy's own line.

A strategy may emit `long_trail` / `short_trail` (NaN = no level on this bar). After each bar
closes, an open trade's stop moves to that bar's level when the level is tighter, never back,
and the moved stop is in force from the NEXT bar. No profit is needed first.
"""

import numpy as np
import pandas as pd
import pytest

from onelot.lab.simulator import simulate
from onelot.strategies.base import StrategyParams


def frame(ohlc, start="2026-09-01 09:15", freq="5min"):
    idx = pd.date_range(start, periods=len(ohlc), freq=freq, tz="Asia/Kolkata")
    df = pd.DataFrame(ohlc, columns=["open", "high", "low", "close"], index=idx, dtype=float)
    df["volume"] = 1000.0
    df["atr"] = 1.0
    df["atr_trail"] = 1.0
    df["d_atr"] = np.nan
    for col in ("long_entry", "short_entry", "long_exit", "short_exit"):
        df[col] = False
    df["long_stop"] = np.nan
    df["short_stop"] = np.nan
    df["long_score"] = 70.0
    df["short_score"] = 70.0
    return df


#: no target and no ATR trail: only the stop, the line and the session end can close a trade
P = {
    "stop_atr_min": 0.1,
    "stop_atr_max": 10,
    "min_risk_pct": 0.0,
    "target_r": 0.0,
    "trail_atr_mult": 0.0,
    "cooldown_bars": 0,
    "max_trades_per_day": 5,
    "entry_start": "09:15",
    "entry_end": "15:00",
}

ENTRY_BAR = 3  # the signal is on bar 2, the fill at the open of bar 3 (100.0, no slippage)


def flat(n, px=100.0):
    return [[px, px + 0.1, px - 0.1, px]] * n


def long_frame(bars, trail=None, stop=99.0):
    """Three quiet bars, a long signal on the third, then `bars`; `trail` maps bar -> level."""
    df = frame(flat(3) + bars)
    df.loc[df.index[2], ["long_entry", "long_stop"]] = [True, stop]
    if trail is not None:
        df["long_trail"] = np.nan
        for i, level in trail.items():
            df.loc[df.index[i], "long_trail"] = level
    return df


def short_frame(bars, trail=None, stop=101.0):
    df = frame(flat(3) + bars)
    df.loc[df.index[2], ["short_entry", "short_stop"]] = [True, stop]
    if trail is not None:
        df["short_trail"] = np.nan
        for i, level in trail.items():
            df.loc[df.index[i], "short_trail"] = level
    return df


def run(df, **params):
    return simulate(df, StrategyParams(**{**P, **params}), "X", "s", slippage_pct=0.0)


RISING = [
    [100.0, 100.5, 99.8, 100.4],  # bar 3: the entry bar
    [100.4, 101.5, 100.3, 101.4],  # bar 4
    [101.4, 102.0, 101.0, 101.8],  # bar 5
    [101.8, 101.9, 100.9, 101.0],  # bar 6: falls back
    [101.0, 101.1, 100.9, 101.0],
    [101.0, 101.1, 100.9, 101.0],
]


def test_the_stop_follows_the_line_up_and_the_exit_is_at_the_line():
    df = long_frame(RISING, trail={3: 99.5, 4: 100.2, 5: 101.2})
    trades = run(df)
    assert len(trades) == 1
    t = trades[0]
    assert t.entry_ts == df.index[ENTRY_BAR] and t.entry_price == 100.0
    assert t.stop_initial == 99.0  # the trade's own record keeps the stop it was opened with
    # bar 6 trades down through 101.2, the level bar 5 closed with
    assert t.exit_ts == df.index[6]
    assert t.exit_reason == "trail" and t.legs[-1].price == pytest.approx(101.2)
    assert t.r_multiple == pytest.approx(1.2)


def test_without_the_line_the_same_bars_run_to_the_session_end():
    df = long_frame(RISING)
    trades = run(df)
    assert len(trades) == 1
    assert trades[0].exit_reason == "eod" and trades[0].legs[-1].price == pytest.approx(101.0)


def test_a_lower_line_never_moves_the_stop_back():
    bars = [
        [100.0, 100.5, 99.8, 100.4],
        [100.4, 101.5, 100.3, 101.4],
        [101.4, 101.5, 100.6, 100.8],  # bar 5: the line reads lower here
        [100.8, 100.9, 100.1, 100.3],  # bar 6: touches 100.2, the level of bar 4
        [100.3, 100.4, 100.2, 100.3],
    ]
    df = long_frame(bars, trail={3: 99.5, 4: 100.2, 5: 99.0})
    t = run(df)[0]
    assert t.exit_ts == df.index[6]
    assert t.exit_reason == "trail" and t.legs[-1].price == pytest.approx(100.2)


def test_a_bar_without_a_level_leaves_the_stop_where_it_was():
    bars = [
        [100.0, 100.5, 99.8, 100.4],
        [100.4, 100.6, 100.0, 100.2],  # NaN
        [100.2, 100.3, 99.8, 99.9],  # NaN
        [99.9, 100.0, 99.4, 99.6],  # touches 99.5, the last level given
        [99.6, 99.7, 99.5, 99.6],
    ]
    df = long_frame(bars, trail={3: 99.5})
    t = run(df)[0]
    assert t.exit_ts == df.index[6]
    assert t.exit_reason == "trail" and t.legs[-1].price == pytest.approx(99.5)


def test_all_nan_trail_columns_change_nothing():
    bars = [
        [100.0, 100.4, 99.8, 100.0],
        [100.0, 100.2, 98.9, 99.2],  # the opening stop 99.0 is hit
        [99.2, 99.3, 99.1, 99.2],
        [99.2, 99.3, 99.1, 99.2],
    ]
    plain = long_frame(bars)
    with_columns = plain.copy()
    with_columns["long_trail"] = np.nan
    with_columns["short_trail"] = np.nan

    def summary(trades):
        return [(t.entry_ts, t.exit_ts, t.exit_reason, t.avg_exit, t.stop_initial) for t in trades]

    assert summary(run(with_columns)) == summary(run(plain))
    assert summary(run(plain)) == [(plain.index[3], plain.index[4], "stop", 99.0, 99.0)]


def test_the_short_side_follows_short_trail_down():
    bars = [
        [100.0, 100.2, 99.5, 99.6],  # bar 3: the entry bar
        [99.6, 99.7, 98.9, 99.0],  # bar 4
        [99.0, 99.9, 98.9, 99.5],  # bar 5: trades up through 99.8
        [99.5, 99.6, 99.4, 99.5],
    ]
    df = short_frame(bars, trail={3: 100.6, 4: 99.8})
    t = run(df)[0]
    assert t.side == -1 and t.entry_price == 100.0 and t.stop_initial == 101.0
    assert t.exit_ts == df.index[5]
    assert t.exit_reason == "trail" and t.legs[-1].price == pytest.approx(99.8)
    assert t.r_multiple == pytest.approx(0.2)


def test_a_higher_short_level_never_moves_the_stop_back():
    bars = [
        [100.0, 100.2, 99.5, 99.6],
        [99.6, 99.7, 98.9, 99.0],
        [99.0, 99.3, 98.9, 99.2],  # the line reads higher here
        [99.2, 99.85, 99.1, 99.7],  # touches 99.8, the level of bar 4
        [99.7, 99.8, 99.6, 99.7],
    ]
    df = short_frame(bars, trail={3: 100.6, 4: 99.8, 5: 100.9})
    t = run(df)[0]
    assert t.exit_ts == df.index[6]
    assert t.exit_reason == "trail" and t.legs[-1].price == pytest.approx(99.8)


def test_each_side_reads_only_its_own_column():
    """A short trade ignores `long_trail` (and a long ignores `short_trail`)."""
    bars = [
        [100.0, 100.2, 99.5, 99.6],
        [99.6, 99.7, 98.9, 99.0],
        [99.0, 99.9, 98.9, 99.5],
        [99.5, 99.6, 99.4, 99.5],
    ]
    df = short_frame(bars)
    df["long_trail"] = 99.3  # would have stopped the short, were it read
    t = run(df)[0]
    assert t.exit_reason == "eod"

    up = long_frame(RISING)
    up["short_trail"] = 101.5
    assert run(up)[0].exit_reason == "eod"


def test_the_entry_bars_own_level_applies_from_the_next_bar_with_no_profit():
    bars = [
        [100.0, 100.05, 99.92, 99.95],  # bar 3: the entry bar closes below the fill
        [99.95, 100.0, 99.85, 99.9],  # bar 4: touches 99.9
        [99.9, 100.0, 99.8, 99.9],
    ]
    df = long_frame(bars, trail={3: 99.9})
    t = run(df)[0]
    assert t.mfe == pytest.approx(0.05)  # never 1R in profit: the line needs none
    assert t.exit_ts == df.index[4]
    assert t.exit_reason == "trail" and t.legs[-1].price == pytest.approx(99.9)
    assert t.r_multiple == pytest.approx(-0.1)


def test_a_level_is_not_used_inside_the_bar_that_produced_it():
    """Bar 4 trades below the level it closes with. That level is known only at its close."""
    bars = [
        [100.0, 100.5, 99.8, 100.4],
        [100.4, 100.6, 100.1, 100.5],  # low 100.1 < 100.2, the level this bar ends with
        [100.5, 100.6, 100.15, 100.3],  # the first bar the level is in force
        [100.3, 100.4, 100.2, 100.3],
    ]
    df = long_frame(bars, trail={3: 99.5, 4: 100.2})
    t = run(df)[0]
    assert t.exit_ts == df.index[5]
    assert t.exit_reason == "trail" and t.legs[-1].price == pytest.approx(100.2)


def test_a_line_beyond_the_price_closes_at_the_next_open_not_at_the_line():
    """The line may end a bar on the far side of the price (a slower line that jumped).
    The next bar then opens through the stop: the fill is the open, never the better level."""
    bars = [
        [100.0, 100.5, 99.8, 100.2],  # closes at 100.2, the line reads 100.5
        [100.2, 100.3, 100.0, 100.1],
        [100.1, 100.2, 100.0, 100.1],
    ]
    df = long_frame(bars, trail={3: 100.5})
    t = run(df)[0]
    assert t.exit_ts == df.index[4]
    assert t.exit_reason == "trail" and t.legs[-1].price == pytest.approx(100.2)


def test_with_both_trails_the_tighter_stop_stands():
    bars = [
        [100.0, 100.5, 99.8, 100.4],  # ATR trail: 100.4 - 1 = 99.4; line 99.5 -> 99.5
        [100.4, 101.5, 100.3, 101.4],  # ATR trail: 101.4 - 1 = 100.4; line 100.2 -> 100.4
        [101.4, 101.5, 100.35, 100.6],  # touches 100.4 but not 100.2
        [100.6, 100.7, 100.5, 100.6],
    ]
    both = long_frame(bars, trail={3: 99.5, 4: 100.2})
    t = run(both, trail_atr_mult=1.0, trail_activate_r=0.0)[0]
    assert t.exit_ts == both.index[5]
    assert t.exit_reason == "trail" and t.legs[-1].price == pytest.approx(100.4)

    line_only = run(both)[0]  # the ATR trail off: 100.2 is never touched
    assert line_only.exit_reason == "eod"


def test_a_signal_exit_still_closes_a_line_trailed_trade_at_the_next_open():
    bars = [
        [100.0, 100.5, 99.8, 100.4],
        [100.4, 101.5, 100.3, 101.4],  # the strategy asks to leave at this close
        [101.3, 101.4, 101.2, 101.3],
        [101.3, 101.4, 101.2, 101.3],
    ]
    df = long_frame(bars, trail={3: 99.5, 4: 100.2})
    df.loc[df.index[4], "long_exit"] = True
    t = run(df)[0]
    assert t.exit_ts == df.index[5]
    assert t.exit_reason == "signal" and t.legs[-1].price == pytest.approx(101.3)


def test_the_session_end_closes_a_trade_the_line_never_touched():
    """A 60-minute frame: the 14:15 bar is managed, the 15:15 bar is the session end."""
    hours = [
        [100.0, 100.1, 99.9, 100.0],  # 09:15
        [100.0, 100.1, 99.9, 100.0],  # 10:15
        [100.0, 100.1, 99.9, 100.0],  # 11:15: the signal
        [100.0, 100.8, 99.9, 100.7],  # 12:15: the entry bar
        [100.7, 101.6, 100.6, 101.5],  # 13:15
        [101.5, 102.4, 101.4, 102.3],  # 14:15
        [102.3, 102.5, 102.2, 102.4],  # 15:15: closed at its open
    ]
    df = frame(hours, freq="60min")
    df.loc[df.index[2], ["long_entry", "long_stop"]] = [True, 99.0]
    df["long_trail"] = [np.nan, np.nan, np.nan, 99.6, 100.4, 101.2, 101.9]
    t = run(df)[0]
    assert t.exit_ts == df.index[6]
    assert t.exit_reason == "square_off" and t.legs[-1].price == pytest.approx(102.3)
