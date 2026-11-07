"""An index has no traded volume, in every year of a backtest.

Dhan's index history carries no volume until 2025-12-03 and a volume figure after it. The
live desk drops index volume (has_volume=False), so the Lab must as well: otherwise every
volume filter is off in the older years and on in the newest one, and a strategy tuned on
the older years is judged on different rules. Volume filters must also be neutral, never
always-false, when a series has no volume (the convention documented in strategies/base.py).
"""

from __future__ import annotations

import numpy as np
import pytest
from tests.lab.conftest import make_intraday

from onelot.strategies.registry import REGISTRY, get_strategy


def _with_late_volume():
    """Ten sessions of an index: no volume in the first five, a volume figure after."""
    df = make_intraday(days=10, seed=11, vol=0.002, base=50000.0)
    days = sorted(set(df.index.normalize()))
    late = df.index.normalize() >= days[5]
    df.loc[~late, "volume"] = 0.0
    return df


def test_the_lab_drops_index_volume_but_keeps_stock_volume(lab_engine):
    from onelot.data.store import save_bars
    from onelot.lab.runner import DataCache

    df = _with_late_volume()
    save_bars(lab_engine, "BANKNIFTY", "5m", df, "dhan")
    save_bars(lab_engine, "RELIANCE", "5m", df, "dhan")
    cache = DataCache(lab_engine)
    assert not cache.series["BANKNIFTY"].has_volume
    index_bars = cache.bars("BANKNIFTY", "5m")
    stock_bars = cache.bars("RELIANCE", "5m")
    assert len(index_bars) == len(df) and (index_bars["volume"] == 0).all()
    assert (stock_bars["volume"] > 0).sum() == int((df["volume"] > 0).sum())
    assert np.allclose(index_bars["close"].to_numpy(), df["close"].to_numpy())


VOLUME_FIELDS = ("rvol_min", "or_rvol_min")


@pytest.mark.parametrize(
    "name",
    sorted(n for n, s in REGISTRY.items() if set(VOLUME_FIELDS) & set(s.Params.model_fields)),
)
def test_volume_thresholds_are_neutral_without_volume(name):
    """No volume: the strategy still trades, and the strictest volume threshold gives the
    same entries as none at all (before the fix orb, supertrend_adx, vwap_pullback and
    squeeze never traded an index)."""
    df = make_intraday(days=40, seed=4, drift=0.0003, vol=0.002, with_volume=False)
    fields = set(VOLUME_FIELDS) & set(REGISTRY[name].Params.model_fields)
    loose = get_strategy(name, {"timeframe": "5m", **dict.fromkeys(fields, 0.0)})
    strict = get_strategy(name, {"timeframe": "5m", **dict.fromkeys(fields, 5.0)})
    a, b = loose.generate(df), strict.generate(df)
    assert int((b["long_entry"] | b["short_entry"]).sum()) > 0, name
    assert (a["long_entry"] == b["long_entry"]).all(), name
    assert (a["short_entry"] == b["short_entry"]).all(), name


@pytest.mark.parametrize("name", ["orb", "supertrend_adx", "vwap_pullback", "squeeze"])
def test_volume_thresholds_still_filter_when_there_is_volume(name):
    """The fix must not switch the filter off for stocks: a high bar removes entries."""
    df = make_intraday(days=30, seed=8, drift=0.0004, vol=0.003, with_volume=True)
    fields = set(VOLUME_FIELDS) & set(REGISTRY[name].Params.model_fields)
    loose = get_strategy(name, {"timeframe": "5m", **dict.fromkeys(fields, 0.0)})
    strict = get_strategy(name, {"timeframe": "5m", **dict.fromkeys(fields, 50.0)})
    a, b = loose.generate(df), strict.generate(df)
    n_loose = int((a["long_entry"] | a["short_entry"]).sum())
    n_strict = int((b["long_entry"] | b["short_entry"]).sum())
    assert n_strict <= n_loose
    assert n_strict == 0 or n_loose == 0 or n_strict < n_loose


@pytest.mark.parametrize("name", sorted(REGISTRY))
def test_every_entry_on_an_index_has_a_stop(name):
    """An index is always in play from its first session, so no entry may come before a stop
    can be set (the gate caught ORB entering on day one with no ATR yet)."""
    df = make_intraday(days=12, seed=3, drift=0.0002, vol=0.002, with_volume=False)
    f = get_strategy(name, {"timeframe": "5m"}).generate(df)
    le, se = f[f["long_entry"]], f[f["short_entry"]]
    assert (le["long_stop"] < le["close"]).all(), name
    assert (se["short_stop"] > se["close"]).all(), name
