"""Desk = Lab on index volume (the paper test, 2026-11-08).

An index or spot FX series has no traded volume (`SeriesSpec.has_volume` False). Dhan's
1-minute index bars carry a volume figure from 2025-12-03 and the socket's bars none, and the
Lab zeroes it (`lab/runner.py`, commit 4904e0c), so every volume filter of an index strategy is
neutral there. The desk read Dhan's figure: on the year of index volume the index candidate
(supertrend_vol 30m, `rvol_min` 1.0) gave 87 Lab entries and 81 desk entries, only 38 the
same. The desk's bar cleaner now zeroes it too.
"""

from __future__ import annotations

import datetime as dt
import math

import pandas as pd
import pytest

from onelot.clock import IST
from onelot.data.universe import load_specs
from onelot.desk.data import COLUMNS, DhanBars, _clean
from onelot.strategies.base import Context
from onelot.strategies.registry import get_strategy

DAY = dt.date(2026, 11, 6)  # a past session: every bar is closed


@pytest.fixture(scope="module")
def series():
    specs, _ = load_specs()
    return specs


def frame(n: int = 30, volume: float = 1_000_000.0, start=(10, 0)) -> pd.DataFrame:
    t0 = dt.datetime.combine(DAY, dt.time(*start), tzinfo=IST)
    idx = pd.DatetimeIndex([t0 + dt.timedelta(minutes=i) for i in range(n)])
    px = [100.0 + i * 0.1 for i in range(n)]
    return pd.DataFrame(
        {"open": px, "high": [p + 0.2 for p in px], "low": [p - 0.2 for p in px], "close": px},
        index=idx,
    ).assign(volume=volume)


@pytest.mark.parametrize("key", ["BANKNIFTY", "NIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"])
def test_an_index_bar_reaches_the_strategies_without_volume(series, key):
    assert series[key].has_volume is False  # the universe declares it
    out = _clean(frame(), series[key], 1)
    assert list(out.columns) == COLUMNS and len(out) == 30
    assert (out["volume"] == 0.0).all()


def test_spot_fx_has_no_volume_either(series):
    assert series["USDINR"].has_volume is False
    assert (_clean(frame(), series["USDINR"], 1)["volume"] == 0.0).all()


@pytest.mark.parametrize("key", ["SBIN", "RELIANCE"])
def test_a_stock_keeps_its_volume(series, key):
    out = _clean(frame(volume=1234.0), series[key], 1)
    assert (out["volume"] == 1234.0).all()


def test_the_cleaner_still_drops_what_it_dropped(series):
    df = frame(n=3)
    df.iloc[1, df.columns.get_loc("high")] = 0.0  # a bad print
    late = frame(n=1, start=(15, 45))  # after the close
    out = _clean(pd.concat([df, late]), series["BANKNIFTY"], 1)
    assert len(out) == 2 and (out["volume"] == 0.0).all()


class _Adapter:
    """A Dhan adapter that serves index bars WITH a volume figure, as Dhan does."""

    def __init__(self, df: pd.DataFrame) -> None:
        self.df = df

    def historical_intraday(self, contract, interval, start, end):
        from types import SimpleNamespace

        return [
            SimpleNamespace(ts=ts, open=r.open, high=r.high, low=r.low, close=r.close, volume=5e8)
            for ts, r in self.df.iterrows()
        ]


class _Provider:
    def contract_for(self, spec):
        return spec.key


def test_dhans_index_volume_figure_never_reaches_the_desk(series):
    source = DhanBars(_Adapter(frame()), pause_s=0.0)
    source.provider = _Provider()
    source.can_serve = lambda spec: True
    out = source.fetch({"BANKNIFTY": series["BANKNIFTY"]}, days=1)
    assert len(out["BANKNIFTY"]) == 30 and (out["BANKNIFTY"]["volume"] == 0.0).all()


def test_so_the_index_candidates_volume_filter_is_neutral_as_in_the_lab(series):
    """The same 30-minute bars give the same features to the desk and the Lab: relative volume
    is unknown (NaN), so `rvol_min` never blocks an index entry and the strength is the Lab's."""
    import yaml

    from onelot.settings import PROJECT_ROOT

    cfg = yaml.safe_load((PROJECT_ROOT / "config" / "desk.yaml").read_text(encoding="utf-8"))
    slot = cfg["defaults"]["index_strategies"][0]
    params = {**slot["params"], "timeframe": slot["timeframe"]}
    t0 = dt.datetime.combine(DAY, dt.time(9, 15), tzinfo=IST)
    idx = pd.DatetimeIndex([t0 - dt.timedelta(days=d) for d in range(40, 0, -1)])
    raw = pd.DataFrame(
        {
            "open": [100.0 + i for i in range(40)],
            "high": [101.0 + i for i in range(40)],
            "low": [99.0 + i for i in range(40)],
            "close": [100.5 + i for i in range(40)],
            "volume": [1e6 * (1 + (i % 3)) for i in range(40)],
        },
        index=idx,
    )
    desk = _clean(raw, series["BANKNIFTY"], 1)  # the desk's path
    lab = raw.assign(volume=0.0)  # the Lab's path (runner.py)
    ctx = Context(symbol="BANKNIFTY", daily=None, market=None, extras={})
    fd = get_strategy(slot["name"], params).generate(desk, ctx)
    fl = get_strategy(slot["name"], params).generate(lab, ctx)
    assert fd["rvol"].isna().all() and fl["rvol"].isna().all()
    # the cleaner names the index "ts"; the values are what must agree
    pd.testing.assert_series_equal(fd["long_score"], fl["long_score"], check_names=False)
    pd.testing.assert_series_equal(fd["long_entry"], fl["long_entry"], check_names=False)
    assert not math.isnan(float(fd["long_score"].iloc[-1]))
