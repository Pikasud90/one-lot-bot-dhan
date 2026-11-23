"""US-079: `simulate()` manages a strategy with `ratchet: true` by `manage/ratchet.py`.

With no one-minute bars it walks the strategy's own bars; the strategy's target, ATR trail
and line trail are not used; and a strategy without the flag is simulated as before.
(`tests/paper/test_ratchet_lab_parity.py` pins the walk on one-minute bars against the
paper book.)
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from onelot.clock import IST
from onelot.lab.simulator import simulate
from onelot.manage import live as rlive
from onelot.manage import ratchet
from onelot.strategies.base import StrategyParams

DAYS = [dt.date(2026, 10, d) for d in (26, 27, 28, 29)]
SETTINGS = rlive.RatchetSettings()


def bars_15m() -> pd.DataFrame:
    """Four sessions of 15m bars: three wavy ones, then a quiet Thursday morning."""
    frames = []
    for k, day in enumerate(DAYS):
        idx = pd.date_range(dt.datetime.combine(day, dt.time(9, 15), IST), periods=25, freq="15min")
        c = 1000.0 + (8.0 * np.sin(np.arange(25) / 3.0 + k) if k < 3 else np.zeros(25))
        o = np.r_[c[0], c[:-1]]
        frames.append(pd.DataFrame({"open": o, "high": np.maximum(o, c) + 2.0,
                                    "low": np.minimum(o, c) - 2.0, "close": c}, index=idx))  # fmt: skip
    return pd.concat(frames)


def strategy_frame(f: pd.DataFrame, signal: pd.Timestamp) -> pd.DataFrame:
    f = f.copy()
    f["atr"] = f["atr_trail"] = 5.0
    f["long_entry"], f["short_entry"] = f.index == signal, False
    f["long_stop"], f["short_stop"] = f["close"] - 3.0, np.nan
    f["long_score"] = f["short_score"] = np.nan
    f["long_exit"] = f["short_exit"] = False
    f["long_trail"] = f["close"] - 0.5  # a line trail the rule must ignore
    return f


def params(**kw) -> StrategyParams:
    base: dict[str, object] = {
        "timeframe": "15m", "target_r": 1.0, "trail_atr_mult": 0.5, "trail_activate_r": 0.0,
        "stop_atr_min": 0.0, "stop_atr_max": 1000.0, "min_risk_pct": 0.0, "max_risk_pct": 10.0,
        "cooldown_bars": 0, "max_trades_per_day": 99, "exit_time": "15:15",
    }  # fmt: skip
    return StrategyParams(**(base | kw))


def test_without_fine_bars_the_rule_walks_the_strategys_own_bars() -> None:
    f = bars_15m()
    signal = pd.Timestamp(dt.datetime.combine(DAYS[3], dt.time(9, 45), IST))
    fill_i = int(f.index.searchsorted(signal)) + 1
    risk = rlive.first_risk(f, signal + pd.Timedelta(minutes=15), "15m", SETTINGS)
    entry = float(f["open"].iloc[fill_i])
    move = 3 * risk
    path = [(0.0, 0.3, -0.05), (0.25, 0.55, 0.2), (0.5, 0.86, 0.45), (0.7, 0.75, 0.5)]
    for k, (o, h, low) in enumerate(path):
        i = fill_i + k
        f.iloc[i, :4] = [entry + o * move, entry + h * move, entry + low * move, entry + o * move]
    (trade,) = simulate(strategy_frame(f, signal), params(ratchet=True), "X", "t", 0.0)

    plan = ratchet.plan_from_risk(1, entry, risk, 3.0, 0.0)
    state, hit = ratchet.start(plan), None
    for k in range(len(path)):
        o, h, low = (float(f[c].iloc[fill_i + k]) for c in ("open", "high", "low"))
        hit = ratchet.exit_on_bar(plan, state, o, h, low)
        if hit is not None:
            exit_k = k
            break
        state, _ = ratchet.advance(plan, state, h, low)
    assert hit is not None and hit[0] == "TRAIL_STOP"
    assert (trade.stop_initial, trade.target) == (plan.stop, plan.target)
    assert (trade.exit_reason, trade.exit_ts) == ("trail_stop", f.index[fill_i + exit_k])
    assert trade.legs[-1].price == hit[1]


def test_without_the_flag_nothing_changes() -> None:
    f = bars_15m()
    signal = pd.Timestamp(dt.datetime.combine(DAYS[3], dt.time(9, 45), IST))
    (trade,) = simulate(strategy_frame(f, signal), params(), "X", "t", 0.0)
    assert trade.risk_ps == 3.0 and trade.target == trade.entry_price + 3.0
