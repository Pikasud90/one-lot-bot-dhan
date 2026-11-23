"""US-075 AC11: mode live with a fake desk whose tracker gains one-minute bars: `advance`
returns only the bars that have arrived and closed, an alert fires on a closed candle, and
a taken trade fills on the next minute that arrives."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from onelot.clock import IST

from .conftest import MemBars, flat_minutes, make_manager, trading_days

DAYS = trading_days("2025-01-06", 15)
D = DAYS[-1]


def T(hhmm: str, sec: int = 0) -> pd.Timestamp:
    return pd.Timestamp(f"{D} {hhmm}", tz=IST) + pd.Timedelta(seconds=sec)


def test_live_reveals_what_has_arrived_alerts_and_fills(db_engine, tmp_path, force):
    every = flat_minutes("2025-01-06", 15, 25000.0, 10.0)
    today = every[every.index >= T("09:15")]
    history = every[every.index < T("09:15")]
    tracker = SimpleNamespace(
        bars=today[today.index <= T("10:05")],
        daily_history=None,
        series=SimpleNamespace(key="NIFTY", label="NIFTY 50", has_volume=False),
        cfg=SimpleNamespace(kind="INDEX"),
        fine_minutes=1,
    )
    desk = SimpleNamespace(trackers={"NIFTY": tracker})
    now = [T("10:06", 30)]
    force(long=[f"{D} 10:10"])
    m = make_manager(db_engine, MemBars({"NIFTY": history}), tmp_path, desk=desk,
                     clock=lambda: now[0].to_pydatetime())  # fmt: skip
    assert {i["key"]: i["live"] for i in m.options()["instruments"]}["NIFTY"] is True
    out = m.create({"instrument": "NIFTY", "strategy": "ema_cross", "timeframe": "5m",
                    "mode": "live", "start": None})  # fmt: skip
    assert out["session"]["mode"] == "live" and out["session"]["cursor"] == T("10:05").isoformat()
    s = m.get(out["session"]["id"])

    def arrive(upto: str, clock: pd.Timestamp) -> dict:
        tracker.bars = today[today.index <= T(upto)]  # a poll replaces the frame
        now[0] = clock
        return s.advance(1)

    r = arrive("10:08", T("10:08", 10))  # 10:08 is still forming
    got = [pd.Timestamp(x[0], unit="s", tz="UTC").tz_convert(IST) for x in r["minutes"]]
    assert got == [T("10:06"), T("10:07")] and r["events"] == []
    assert s.advance(1)["minutes"] == []  # nothing new has arrived
    r = arrive("10:14", T("10:15", 2))
    assert len(r["minutes"]) == 7 and [e["kind"] for e in r["events"]] == ["ALERT"]
    assert r["events"][0]["ts"] == T("10:15").isoformat()
    st = s.take(r["alert"]["id"])
    assert st["trade"]["status"] == "PENDING_FILL"
    r = arrive("10:15", T("10:15", 40))  # 10:15 has not closed yet: no fill
    assert r["minutes"] == [] and r["trade"]["status"] == "PENDING_FILL"
    r = arrive("10:15", T("10:16", 1))
    assert [e["kind"] for e in r["events"]] == ["FILLED"]
    assert r["trade"]["status"] == "OPEN" and r["trade"]["entry_ts"] == T("10:15").isoformat()
    assert r["trade"]["entry"] == 25000.0 and r["trade"]["source"] == "model"
