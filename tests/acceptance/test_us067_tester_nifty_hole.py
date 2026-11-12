"""US-067 tester (MAJOR-1), passing since the fix: an open trade still exits (its 15% stop) when
NIFTY has no bar on an earlier session: exits read only the stock's own bars."""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_us067_tester_desk import FULL, L, TradingCalendar, World, at  # noqa: E402


def test_an_open_trade_still_exits_when_nifty_has_a_hole_in_its_history(tmp_path):
    """R3 does not read NIFTY. A NIFTY bar missing on one earlier session must at most stop NEW
    entries (R1b: no NIFTY bar that day, no signal); D8 says no alert for a hole in the history,
    which could be read as exits too, so this one is an observation, not a hard failure."""
    cal = TradingCalendar({dt.date(2026, 1, 10): "x"}, verified=True)
    nf = FULL["NIFTY"]
    nf = nf[nf.index.date != L[5]]  # L5 absent from NIFTY (live), present for the stocks
    full = dict(FULL, NIFTY=nf)
    w = World(tmp_path, full, cal=cal)
    w.start(at(L[3], "15:00"))
    w.poll(at(L[3], "15:45"))
    w.start(at(L[4], "09:00"))
    w.poll(at(L[4], "09:20"))
    w.poll(at(L[4], "15:45"))
    w.poll(at(L[5], "15:45"))
    w.poll(at(L[6], "15:45"))
    assert "SBIN" in w.trades(), w.desk.notes
    assert w.trades()["SBIN"].exit_reason == "STOP", (w.trades()["SBIN"].status, w.desk.notes)
