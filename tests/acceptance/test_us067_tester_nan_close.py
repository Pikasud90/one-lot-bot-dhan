"""US-067 tester (MINOR-2), passing since the fix: one NaN close in the stored history must not
silence the SMA200 for good: the bar is left out with a note."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_us067_tester_desk import STORE, L, World, at  # noqa: E402


def test_a_nan_close_long_ago_does_not_silence_the_rule(tmp_path):
    st = dict(STORE)
    s = st["SBIN"].copy()
    s.iloc[10, s.columns.get_loc("close")] = np.nan  # 220 sessions before the signal
    st["SBIN"] = s
    w = World(tmp_path, store=st)
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    assert "SBIN" in w.trades() or w.desk.notes.get("SBIN"), w.desk.card("SBIN")["reading"]
