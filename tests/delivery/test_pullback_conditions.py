"""US-101: the condition labels and the placebo shuffle of `scripts/pullback_conditions.py` on
hand-made evenings. No research file is read: these functions are pure."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "pullback_conditions", ROOT / "scripts" / "pullback_conditions.py"
)
pc = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(pc)  # type: ignore[union-attr]


def label(cond, xs):
    edges, names = pc.CONDITIONS[cond]
    return list(pc.bin_label(xs, edges, names))


def test_breadth_labels_at_their_edges():
    got = label("BREADTH", [1, 2, 3, 9, 10, 19, 20, 57])
    assert got == ["1 to 2", "1 to 2", "3 to 9", "3 to 9", "10 to 19", "10 to 19",
                   "20 or more", "20 or more"]  # fmt: skip


def test_depth_and_vix_labels_at_their_edges_and_missing_values_left_out():
    assert label("DEPTH", [0.0, 0.99, 1.0, 1.99, 2.0, 3.99, 4.0, 12.0, np.nan]) == [
        "under 1%", "under 1%", "1 to 2%", "1 to 2%", "2 to 4%", "2 to 4%", "4% or more",
        "4% or more", None,
    ]  # fmt: skip
    assert label("VIX", [0.5, 0.9, 1.099, 1.1, 1.3, 2.0, np.nan]) == [
        "under 0.9", "0.9 to 1.1", "0.9 to 1.1", "1.1 to 1.3", "1.3 or more", "1.3 or more",
        None,
    ]  # fmt: skip


def test_breadth_counts_the_signals_of_the_same_evening():
    d = pd.Series(pd.to_datetime(["2020-01-01", "2020-01-01", "2020-01-02", "2020-01-01"]))
    assert list(pc.breadth(d)) == [3, 3, 1, 3]


def test_depth_and_vix_use_only_closes_up_to_the_evening():
    idx = pd.bdate_range("2021-01-01", periods=80)
    c = pd.Series(np.r_[np.linspace(100, 120, 40), np.linspace(118, 90, 40)], index=idx)
    full_d, full_v = pc.depth_pct(c), pc.vix_ratio(c)
    assert full_d.iloc[:19].isna().all() and full_v.iloc[:59].isna().all()
    for k in (25, 45, 70, 79):
        cut = c.iloc[: k + 1]
        assert np.isclose(pc.depth_pct(cut).iloc[-1], full_d.iloc[k])
        if k >= 59:
            assert np.isclose(pc.vix_ratio(cut).iloc[-1], full_v.iloc[k])
    # NIFTY at 90 after a high of about 118.6 within the last 20 closes
    assert np.isclose(full_d.iloc[-1], (1 - 90 / c.iloc[-20:].max()) * 100)


def test_placebo_shuffles_evening_labels_only_inside_each_calendar_year():
    dates = pd.to_datetime(["2019-03-01", "2019-06-01", "2019-09-01", "2019-12-01",
                            "2020-02-01", "2020-05-01", "2020-08-01"])  # fmt: skip
    labs = np.array(["a", "a", "b", None, "c", "c", "d"], dtype=object)
    seen = set()
    for seed in range(30):
        sh = pc.shuffle_within_year(dates, labs, np.random.default_rng(seed))
        assert sorted(sh[:3]) == ["a", "a", "b"]  # 2019 keeps its own labels
        assert sh[3] is None  # an evening without a value stays out
        assert sorted(sh[4:]) == ["c", "c", "d"]  # nothing crosses into 2020
        seen.add(tuple(sh))
    assert len(seen) > 1  # it does shuffle
    again = pc.shuffle_within_year(dates, labs, np.random.default_rng(7))
    assert list(again) == list(pc.shuffle_within_year(dates, labs, np.random.default_rng(7)))
    assert list(labs) == ["a", "a", "b", None, "c", "c", "d"]  # the input is not changed


def test_an_evening_keeps_all_its_trades_under_the_placebo():
    ev_dates = pd.to_datetime(["2018-01-02", "2018-05-02", "2018-09-03"])
    ev_lab = np.array(["x", "y", "z"], dtype=object)
    trade_ev = np.array([0, 0, 0, 1, 2, 2])  # three trades on the first evening
    sh = pc.shuffle_within_year(ev_dates, ev_lab, np.random.default_rng(3))
    per_trade = sh[trade_ev]
    assert len(set(per_trade[:3])) == 1 and len(set(per_trade[4:])) == 1


def stats(tune, choose, test, span_t=0.0):
    keys = ("n", "days", "mean", "t")
    return {"TUNE": dict(zip(keys, tune, strict=True)),
            "CHOOSE": dict(zip(keys, choose, strict=True)),
            "TEST": dict(zip(keys, test, strict=True)),
            "ALL": {"n": 0, "days": 0, "mean": 0.0, "t": span_t}}  # fmt: skip


def test_the_selection_steps_and_avoid():
    s = pc.select(stats((100, 40, 0.6, 3.0), (25, 20, 0.2, 1.2), (21, 10, 0.1, 0.5)))
    assert s["passed"] and s["step"] == "PASSED"
    s = pc.select(stats((100, 40, 0.4, 3.0), (25, 20, 0.2, 1.2), (21, 10, 0.1, 0.5)))
    assert not s["discovered"] and s["step"] == "REJECTED"  # +0.40 is under +0.50
    s = pc.select(stats((59, 40, 0.6, 3.0), (25, 20, 0.2, 1.2), (21, 10, 0.1, 0.5)))
    assert s["step"] == "not examined"
    s = pc.select(stats((100, 40, -0.6, -2.6), (25, 20, -0.2, -1.0), (5, 3, -0.1, -0.2)))
    assert s["avoid"]
    s = pc.select(stats((100, 40, -0.6, -2.6), (25, 20, -0.2, -1.0), (5, 3, 0.1, 0.2)))
    assert not s["avoid"]  # TEST positive
    s = pc.select(stats((100, 40, 0.6, 2.6), (19, 10, 0.3, 1.5), (30, 10, 0.2, 1.0), 2.1))
    assert s["step"] == "DISCOVERED only" and s["watch_route"]  # one threshold missed
