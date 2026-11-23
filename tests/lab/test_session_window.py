"""The Lab keeps a strategy's own entry window; the instrument's window is the fallback."""

import numpy as np
import pandas as pd
import pytest
from tests.lab.conftest import make_intraday

from onelot.data.universe import load_specs
from onelot.lab.runner import _session_params
from onelot.strategies.registry import get_strategy

_, INSTRUMENTS = load_specs()


def _spec(key: str):
    return INSTRUMENTS[key]


def _gappy(days: int = 30) -> pd.DataFrame:
    """Bars where every session opens 1.5% above the previous close (a relative gap-up)."""
    df = make_intraday(days=days, seed=3, drift=0.0, vol=0.002)
    day = df.index.normalize()
    k = pd.Series(day).rank(method="dense").to_numpy() - 1
    mult = 1.015**k
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].to_numpy() * mult
    return df


def _entry_times(name: str, params: dict, df: pd.DataFrame) -> list:
    f = get_strategy(name, {**params, "timeframe": "5m", "min_open_move_atr": 0.0}).generate(df)
    hit = f[f["long_entry"] | f["short_entry"]]
    return [(ts + pd.Timedelta(minutes=5)).time() for ts in hit.index]


def test_a_strategy_with_its_own_window_keeps_it():
    sbin = _spec("SBIN")
    gap = _session_params(sbin, {}, "gap_fade")
    assert (gap["entry_start"], gap["entry_end"]) == ("09:15", "09:45")
    mom = _session_params(sbin, {}, "intraday_momentum")
    assert (mom["entry_start"], mom["entry_end"]) == ("14:30", "15:00")


def test_a_strategy_without_a_window_gets_the_instruments():
    for name in ("ema_cross", "microtrend", "orb", "no_such_strategy", None):
        got = _session_params(_spec("SBIN"), {}, name)
        assert (got["entry_start"], got["entry_end"]) == ("09:30", "14:45"), name
    fx = next(k for k, v in INSTRUMENTS.items() if v.session.get("first_entry") == "09:15")
    got = _session_params(_spec(fx), {}, "ema_cross")
    assert got["entry_start"] == "09:15"


def test_the_users_own_setting_still_wins_and_the_start_never_precedes_the_open():
    got = _session_params(_spec("SBIN"), {"entry_start": "10:00", "entry_end": "11:00"}, "gap_fade")
    assert (got["entry_start"], got["entry_end"]) == ("10:00", "11:00")
    spec = _spec("SBIN")
    late = type(spec)(**{**spec.__dict__, "session": {**spec.session, "open": "09:20"}})
    assert _session_params(late, {}, "gap_fade")["entry_start"] == "09:20"


@pytest.mark.parametrize("name", ["gap_fade", "intraday_momentum"])
def test_own_window_strategies_signal_inside_their_window(name):
    df = _gappy() if name == "gap_fade" else make_intraday(days=15, seed=4, vol=0.003)
    params = _session_params(_spec("SBIN"), {}, name)
    times = _entry_times(name, params, df)
    assert len(times) > 0, f"{name} gave no signal inside its own window"
    lo, hi = params["entry_start"], params["entry_end"]
    assert all(lo <= f"{t:%H:%M}" <= hi for t in times)
    if name == "gap_fade":  # the old rule (instrument's window over its own) found nothing
        old = {**params, "entry_start": "09:30", "entry_end": "14:45"}
        assert len(_entry_times(name, old, df)) == 0


def test_a_strategy_without_a_window_trades_as_before():
    df = make_intraday(days=15, seed=4, drift=0.0003, vol=0.002)
    new = _session_params(_spec("SBIN"), {}, "ema_cross")
    old = {"entry_start": "09:30", "entry_end": "14:45", "exit_time": "15:15"}
    assert new == old
    assert np.array_equal(
        get_strategy("ema_cross", {**new, "timeframe": "5m"}).generate(df)["long_entry"],
        get_strategy("ema_cross", {**old, "timeframe": "5m"}).generate(df)["long_entry"],
    )
