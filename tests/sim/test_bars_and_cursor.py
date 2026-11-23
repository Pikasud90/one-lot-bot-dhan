"""US-075 AC1: on stored one-minute bars, a session shows only candles closed at or before the
cursor, and `advance(n)` reveals exactly the next n one-minute bars (the next session after
15:29)."""

from __future__ import annotations

import pandas as pd
import pytest

from onelot.clock import IST
from onelot.data.store import save_bars
from onelot.sim.bars import StoreBars

from .conftest import make_manager, trading_days, walk_minutes

DAYS = trading_days("2025-01-06", 20)


@pytest.fixture
def stored(db_engine):
    nifty = walk_minutes("2025-01-06", 20, 25000.0, 0.0004, seed=3, volume=False)
    save_bars(db_engine, "NIFTY", "1m", nifty, "test")
    return nifty


def test_candles_end_by_the_cursor_and_advance_reveals_the_next_bars(db_engine, tmp_path, stored):
    m = make_manager(db_engine, StoreBars(db_engine), tmp_path)
    day = DAYS[-3]
    out = m.create(
        {"instrument": "NIFTY", "strategy": "ema_cross", "timeframe": "5m", "mode": "past",
         "start": f"{day}T10:02:00+05:30"}
    )  # fmt: skip
    cursor = pd.Timestamp(out["session"]["cursor"])
    assert cursor == pd.Timestamp(f"{day} 10:01", tz=IST)  # the minute before the start
    candles = out["candles"]
    assert 0 < len(candles) <= 300
    last_start = pd.Timestamp(candles[-1][0], unit="s", tz="UTC")
    assert last_start + pd.Timedelta(minutes=5) <= cursor + pd.Timedelta(minutes=1)
    assert last_start == pd.Timestamp(f"{day} 09:55", tz=IST)  # 10:00 has not closed
    assert [c[0] for c in candles] == sorted(c[0] for c in candles)

    s = m.get(out["session"]["id"])
    r = s.advance(7)
    want = stored.loc[pd.Timestamp(f"{day} 10:02", tz=IST) :].iloc[:7]
    assert [x[0] for x in r["minutes"]] == [int(t.timestamp()) for t in want.index]
    assert [x[4] for x in r["minutes"]] == [round(v, 2) for v in want["close"]]
    assert r["cursor"] == pd.Timestamp(f"{day} 10:08", tz=IST).isoformat()


def test_the_day_ends_at_1529_and_the_next_advance_starts_the_next_session(
    db_engine, tmp_path, stored
):
    m = make_manager(db_engine, StoreBars(db_engine), tmp_path)
    day, nxt = DAYS[-3], DAYS[-2]
    out = m.create(
        {"instrument": "NIFTY", "strategy": "ema_cross", "timeframe": "5m", "mode": "past",
         "start": f"{day}T15:20:00+05:30"}
    )  # fmt: skip
    s = m.get(out["session"]["id"])
    r = s.advance(390)
    assert len(r["minutes"]) == 10 and r["day_end"] is True and r["over"] is False
    assert r["events"][-1]["kind"] == "DAY_END"
    assert s.session_view()["status"] == "DAY_END"
    r = s.advance(1)
    first = pd.Timestamp(r["minutes"][0][0], unit="s", tz="UTC").tz_convert(IST)
    assert first == pd.Timestamp(f"{nxt} 09:15", tz=IST) and r["day_end"] is False
    assert s.session_view()["status"] == "RUNNING"


def test_the_last_stored_minute_ends_the_session(db_engine, tmp_path, stored):
    m = make_manager(db_engine, StoreBars(db_engine), tmp_path)
    out = m.create(
        {"instrument": "NIFTY", "strategy": "ema_cross", "timeframe": "5m", "mode": "past",
         "start": f"{DAYS[-1]}T15:25:00+05:30"}
    )  # fmt: skip
    s = m.get(out["session"]["id"])
    r = s.advance(390)
    assert r["over"] is True and r["events"][-1]["kind"] == "DATA_END"
    assert s.session_view()["status"] == "ENDED"
