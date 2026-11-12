"""US-067 D16 with the real thing (tester): a real `PaperBook` (the factory's) holds an open
intraday paper trade when its slot is paused by a reload; the desk keeps polling, and the book
still manages it and closes it at the session end, as before. No paused slot raises an alert."""

from __future__ import annotations

import dataclasses
import datetime as dt
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "desk"))

from desk_harness import new_desk, small_cfg  # noqa: E402

from onelot.clock import IST  # noqa: E402
from onelot.desk.config import DeskInstrumentCfg, StrategySlotCfg  # noqa: E402
from onelot.paper.factory import build_paper_book  # noqa: E402
from onelot.store import init_database  # noqa: E402
from onelot.store.schema import paper_trades  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "paper_conftest", HERE.parent / "paper" / "conftest.py"
)
assert _spec and _spec.loader
_paper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_paper)

# a recent weekday: the tracker keeps only the last 70 days of fine bars by the wall clock
DAY = dt.date.today() - dt.timedelta(days=7)
while DAY.weekday() >= 5:
    DAY -= dt.timedelta(days=1)


def at(hhmm: str) -> dt.datetime:
    h, m = map(int, hhmm.split(":"))
    return dt.datetime.combine(DAY, dt.time(h, m), tzinfo=IST)


IDX = pd.date_range(at("09:15"), at("15:29"), freq="1min")
_wave = 1000.0 + 0.5 * np.sin(np.arange(len(IDX)) / 15.0)  # never near the stop or the target
BARS = pd.DataFrame(
    {"open": _wave, "high": _wave + 0.2, "low": _wave - 0.2, "close": _wave, "volume": 1e4},
    index=IDX,
)


class Clock:
    now = at("10:00")

    def __call__(self) -> dt.datetime:
        return Clock.now


class Source:
    name, delayed, resolution, primary, last_error = "dhan", False, "1m", None, None

    def __init__(self) -> None:
        self.served_by: dict[str, str] = {}

    def fetch(self, specs, days, since=None):
        return {k: BARS[BARS.index + pd.Timedelta(minutes=1) <= Clock.now] for k in specs}


def config(enabled: bool):
    slot = StrategySlotCfg("ema_cross", "5m", {}, enabled)
    return small_cfg(
        indices=(), stocks=(DeskInstrumentCfg("RELIANCE", "RELIANCE", "STOCK", (slot,)),)
    )


@pytest.fixture
def db(tmp_path):
    return init_database(f"sqlite:///{tmp_path / 'd16.db'}")


def test_a_paused_slots_open_paper_trade_is_managed_and_closed_as_before(cfg, db, monkeypatch):
    clock = Clock()
    monkeypatch.setattr("onelot.desk.desk.now_ist", clock)
    book = build_paper_book(cfg, db.engine, clock=clock)
    desk, notifier, _ = new_desk(cfg, db, desk_cfg=config(True), paper=book)
    desk.source = Source()
    desk.fine_minutes = 1
    for t in desk.trackers.values():
        t.fine_minutes = 1
    alert = _paper.make_alert("STOCK", created=at("10:05"))
    book.on_alert(alert, at("10:05"))
    Clock.now = at("10:10")
    desk.poll()
    (trade,) = book.open_trades()
    assert trade.status == "OPEN"

    # the owner pauses the slot and reloads the desk while the trade is open
    monkeypatch.setattr("onelot.desk.desk.load_desk_config", lambda: config(False))
    desk.reload()
    for t in desk.trackers.values():
        t.fine_minutes = 1
    assert desk.trackers["RELIANCE"].slots[0].cfg.enabled is False
    for hhmm in ("11:00", "13:00", "15:20", "15:40"):
        Clock.now = at(hhmm)
        desk.poll()
    with db.engine.connect() as conn:
        (row,) = conn.execute(select(paper_trades)).mappings().all()
    assert row["status"] == "CLOSED" and row["net_paise"] is not None
    assert book.open_trades() == []
    assert desk.signals == {} and dataclasses.is_dataclass(desk.cfg)
