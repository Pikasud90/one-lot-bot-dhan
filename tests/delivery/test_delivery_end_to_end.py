"""US-067 end to end: twelve simulated sessions through the Desk and the delivery book, with
the desk rebuilt (a restart) while an alert is pending, while a trade is open and while an exit
is pending. D1 to D7, D11, D12, D14, D15 and the session-complete ruling.

Two stocks on stored daily bars (230 rising sessions) and live one-minute bars served by a fake
source cut at the simulated clock:
- SBIN falls 10% on L3 (entry signal), is bought at L4's open, closes at its stop on L6 (exit
  signal) and is sold at L7's open by a desk started at 11:00 that day (late).
- ITC falls 10% on L3, is bought at L4's open, closes above its SMA20 on L5 (exit signal) and
  is sold at L6's open.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "desk"))

from delivery_helpers import at, session_bars, weekdays  # noqa: E402
from desk_harness import FakeNotifier, new_desk, small_cfg  # noqa: E402

from onelot.clock import IST, TradingCalendar  # noqa: E402
from onelot.data.store import save_bars  # noqa: E402
from onelot.delivery.book import CLOSED, EXIT_PENDING, OPEN, PENDING_ENTRY  # noqa: E402
from onelot.desk.config import DeliveryCfg, DeskInstrumentCfg, StrategySlotCfg  # noqa: E402
from onelot.store import init_database  # noqa: E402

EVIDENCE = "Backtest: test line. Not proven: this is the paper test."
PULLBACK = StrategySlotCfg(
    "pullback", "1d", {"unit_inr": 10000, "evidence": EVIDENCE}, True, "delivery"
)
# The live sessions lie in the last few weeks of the real calendar: the tracker keeps only the
# last 70 days of fine bars by the wall clock. The calendar here is weekdays only.
_LIVE_START = dt.date.today() - dt.timedelta(days=30)
STORED = weekdays(_LIVE_START - dt.timedelta(days=340), 400)
STORED = [d for d in STORED if d < _LIVE_START][-230:]
LIVE = weekdays(_LIVE_START, 12)
L = {i + 1: d for i, d in enumerate(LIVE)}  # L[1] .. L[12]

# (open, close) of each live session
SBIN_LIVE = {1: (215.2, 215.5), 2: (215.5, 216.0), 3: (215.0, 194.4), 4: (195.0, 190.0),
             5: (189.0, 185.0), 6: (180.0, 160.0), 7: (158.0, 160.0)}  # fmt: skip
ITC_LIVE = {1: (415.2, 415.5), 2: (415.5, 416.0), 3: (415.0, 374.4), 4: (375.0, 380.0),
            5: (381.0, 430.0), 6: (431.0, 431.0)}  # fmt: skip


def stored_days(base: float) -> pd.DataFrame:
    close = base + 0.5 * np.arange(len(STORED), dtype=float)
    idx = pd.DatetimeIndex([pd.Timestamp(d).tz_localize(IST) for d in STORED])
    return pd.DataFrame(
        {"open": close - 0.2, "high": close + 1, "low": close - 1, "close": close, "volume": 1e6},
        index=idx,
    )


def live_bars(path: dict[int, tuple[float, float]], last: str) -> pd.DataFrame:
    """Each live session's bars; after the path ends the price stays at its last close."""
    flat = path[max(path)][1]
    return pd.concat([session_bars(d, *path.get(i, (flat, flat)), last=last) for i, d in L.items()])


FULL = {
    "SBIN": live_bars(SBIN_LIVE, "15:14"),
    "ITC": live_bars(ITC_LIVE, "15:14"),
    "NIFTY": live_bars({i: (25000.0 + 10 * i, 25005.0 + 10 * i) for i in L}, "15:29"),
}


class Clock:
    now = at(L[1], "08:00")


class Source:
    """Serves each series' one-minute bars that have closed by the simulated clock."""

    name, delayed, resolution, primary, last_error = "fake", False, "1m", None, None

    def __init__(self) -> None:
        self.served_by: dict[str, str] = {}

    def fetch(self, specs, days, since=None):
        out = {}
        for k in specs:
            f = FULL.get(k)
            if f is not None:
                out[k] = f[f.index + pd.Timedelta(minutes=1) <= Clock.now]
        return out


@pytest.fixture
def world(cfg, tmp_path, monkeypatch):
    db = init_database(f"sqlite:///{tmp_path / 'e2e.db'}")
    for key, base in (("SBIN", 100.0), ("ITC", 300.0), ("NIFTY", 20000.0)):
        save_bars(db.engine, key, "1d", stored_days(base), "test")
    monkeypatch.setattr("onelot.desk.desk.now_ist", lambda: Clock.now)
    desk_cfg = small_cfg(
        indices=(DeskInstrumentCfg("NIFTY", "NIFTY_OPT", "INDEX", ()),),
        stocks=(
            DeskInstrumentCfg("SBIN", "SBIN", "STOCK", (PULLBACK,)),
            DeskInstrumentCfg("ITC", "ITC", "STOCK", (PULLBACK,)),
        ),
        delivery=DeliveryCfg(top_up=False),
        primary=("pullback",),
    )
    sent: list = []

    def start(when: dt.datetime):
        Clock.now = when
        desk, notifier, _ = new_desk(
            cfg, db, desk_cfg=desk_cfg, calendar=TradingCalendar({}, verified=True)
        )
        desk.source = Source()
        # the history loads on the poll thread here: in market hours the desk loads it in the
        # background, which a test would race (that path has its own test)
        desk.delivery._background = False
        desk.fine_minutes = 1
        for t in desk.trackers.values():
            t.fine_minutes = 1
        notifier.sent = sent  # every desk's messages in one list
        return desk

    return start, sent


def poll(desk, when: dt.datetime) -> None:
    Clock.now = when
    desk.poll()


def trades(desk) -> dict[str, object]:
    return {t.series_key: t for t in desk.delivery.book._select()}


def test_twelve_sessions_with_three_restarts(world):
    start, sent = world
    desk = start(at(L[1], "08:00"))
    for i in (1, 2):
        poll(desk, at(L[i], "15:45"))
    assert sent == [] and trades(desk) == {}
    card = desk.snapshot()["stocks"][0]["pullback"]
    assert card["evidence"] == EVIDENCE and card["history_to"] == L[2].isoformat()
    assert card["reading"]["above_sma200"] and not card["reading"]["signal"]

    # L3 15:30: the stocks' last bar is 15:14 but the clock is not 10 minutes past the close
    poll(desk, at(L[3], "15:35"))
    assert sent == []
    poll(desk, at(L[3], "15:45"))  # D1: evaluated after the close, once
    poll(desk, at(L[3], "16:30"))
    entries = [n for n in sent if n.title.startswith("BUY")]
    assert len(entries) == 2 and len(sent) == 2
    sbin = trades(desk)["SBIN"]
    assert sbin.status == PENDING_ENTRY and sbin.qty == 51 and sbin.signal_close == 194.4
    assert (
        "BUY 51 shares of SBIN at the next open (last close Rs 194.40)"
        in entries[-1].body + entries[0].body
    )
    assert all(EVIDENCE in n.body for n in sent)  # D14
    # D2: pending until the next session's close, not the intraday alerts' ten minutes
    assert all(n.ttl_seconds > 12 * 3600 for n in entries) and desk.signals == {}
    view = desk.snapshot()["delivery"]
    assert {r["series_key"] for r in view["pending"]} == {"SBIN", "ITC"}

    # restart 1: an alert pending; the desk is running before L4's open
    desk = start(at(L[4], "09:00"))
    poll(desk, at(L[4], "09:00"))
    poll(desk, at(L[4], "09:20"))  # D3: the first bar of L4 fills, on time
    sbin = trades(desk)["SBIN"]
    assert sbin.status == OPEN and not sbin.entry_late
    assert sbin.entry_paise == round(195.0 * 1.0003 * 100)
    assert sbin.entry_ts == at(L[4], "09:15") and sbin.entry_session == L[4]
    assert len(sent) == 2  # nothing told twice
    poll(desk, at(L[4], "15:45"))
    poll(desk, at(L[5], "12:00"))  # D4: carried across the session end and the night
    assert trades(desk)["ITC"].status == OPEN

    # restart 2: two trades open
    desk = start(at(L[5], "12:05"))
    poll(desk, at(L[5], "12:05"))
    row = next(r for r in desk.snapshot()["delivery"]["open"] if r["series_key"] == "SBIN")
    assert row["sessions"] == 2 and row["max_sessions"] == 30  # D7: "2 of 30"
    assert row["stop"] == pytest.approx(round(195.0 * 1.0003 * 0.85, 2))
    assert row["last"] is not None and row["unrealised"] == pytest.approx(
        (row["last"] - row["entry"]) * 51, abs=0.02
    )
    assert row["sma_exit"] is not None
    poll(desk, at(L[5], "15:45"))  # ITC closes above its SMA20: D5 exit alert
    itc = trades(desk)["ITC"]
    assert itc.status == EXIT_PENDING and itc.exit_reason == "SMA20" and itc.sessions_held == 2
    exits = [n for n in sent if n.title.startswith("SELL")]
    assert len(exits) == 1 and "ITC" in exits[0].title and EVIDENCE in exits[0].body

    # restart 3: an exit pending
    desk = start(at(L[6], "08:30"))
    poll(desk, at(L[6], "09:20"))
    itc = trades(desk)["ITC"]
    assert itc.status == CLOSED and not itc.exit_late
    assert itc.exit_paise == round(431.0 * 0.9997 * 100) and itc.dp_paise == 1475
    assert itc.net_paise == itc.gross_paise - itc.charges_paise - 1475
    poll(desk, at(L[6], "15:45"))  # SBIN closes at 160 <= its stop: exit alert
    assert trades(desk)["SBIN"].exit_reason == "STOP"

    # restart 4: the desk misses L7's open and starts at 11:00
    desk = start(at(L[7], "11:00"))
    poll(desk, at(L[7], "11:05"))
    sbin = trades(desk)["SBIN"]
    assert sbin.status == CLOSED and sbin.exit_late and sbin.exit_ts == at(L[7], "11:00")
    for i in range(7, 13):
        poll(desk, at(L[i], "15:45"))
    assert [n.title.split()[0] for n in sent] == ["BUY", "BUY", "SELL", "SELL"]
    assert {t.status for t in trades(desk).values()} == {CLOSED}
    board = {(r["strategy"], r["timeframe"]): r for r in _scoreboard(desk)}
    assert board[("pullback", "1d")]["trades"] == 2


def _scoreboard(desk):
    from onelot.api.server import _paper_scoreboard

    return _paper_scoreboard(desk)


def test_a_first_start_after_the_signal_session_raises_it_and_fills_late(world):
    """D1 catch-up: the desk was off at L3's close and starts during L4: L3 is evaluated at
    start, the alert goes out, and the trade opens late at the first bar that opens after the
    alert (round 2, item 2: never a bar earlier than its alert, so not the 10:30 bar)."""
    start, sent = world
    desk = start(at(L[4], "10:30"))
    poll(desk, at(L[4], "10:31"))
    assert [n.title.split()[0] for n in sent] == ["BUY", "BUY"]
    assert trades(desk)["SBIN"].status == PENDING_ENTRY  # its bar has not closed yet
    poll(desk, at(L[4], "10:32"))
    sbin = trades(desk)["SBIN"]
    assert sbin.signal_date == L[3] and sbin.status == OPEN and sbin.entry_late
    assert sbin.entry_ts == at(L[4], "10:31")


def test_d3_a_desk_down_for_the_whole_next_session_misses_the_entry(world):
    start, sent = world
    desk = start(at(L[3], "15:00"))
    poll(desk, at(L[3], "15:45"))
    assert len(sent) == 2
    desk = start(at(L[5], "09:00"))  # down for all of L4
    poll(desk, at(L[5], "09:30"))
    sbin = trades(desk)["SBIN"]
    assert sbin.status == "MISSED" and sbin.note == "missed: the desk was not running"
    board = {(r["strategy"], r["timeframe"]): r for r in _scoreboard(desk)}
    assert board[("pullback", "1d")]["no_fill"] == 2


def test_a_session_that_stops_at_14_00_is_not_complete_and_is_retried(world, monkeypatch):
    start, sent = world
    cut = FULL["SBIN"]
    short = cut[~((cut.index.date == L[3]) & (cut.index.time > dt.time(14, 0)))]
    monkeypatch.setitem(FULL, "SBIN", short)
    desk = start(at(L[3], "15:00"))
    poll(desk, at(L[3], "15:45"))
    assert [n.body.split()[4] for n in sent] == ["ITC"]  # SBIN: no alert
    assert "incomplete (last bar 14:00)" in desk.delivery.notes["SBIN"]
    assert (
        desk.delivery.book.last_evaluated("SBIN") == L[2]
        or desk.delivery.book.last_evaluated("SBIN") is None
    )
    monkeypatch.setitem(FULL, "SBIN", cut)  # the missing minutes arrive (a refetch)
    desk.trackers["SBIN"].bars = pd.DataFrame()
    poll(desk, at(L[3], "16:00"))
    assert sorted(n.body.split()[4] for n in sent) == ["ITC", "SBIN"]


def test_d15_a_gap_flags_the_open_trade(world, monkeypatch):
    start, sent = world
    bars = FULL["SBIN"].copy()
    day5 = bars.index.date == L[5]
    bars.loc[day5, ["open", "high", "low", "close"]] *= 0.7  # a 30% gap at L5's open
    monkeypatch.setitem(FULL, "SBIN", bars)
    desk = start(at(L[3], "15:00"))
    poll(desk, at(L[3], "15:45"))
    poll(desk, at(L[5], "09:20"))
    assert trades(desk)["SBIN"].warning == (
        f"possible corporate action on {L[5].isoformat()}: check the figures"
    )
    assert trades(desk)["ITC"].warning is None


def test_d8_a_short_history_is_shown_and_raises_nothing(world, cfg, tmp_path, monkeypatch):
    start, sent = world
    desk = start(at(L[3], "15:00"))
    desk.delivery.cfg = dataclasses.replace(desk.delivery.cfg, min_bars=400)
    poll(desk, at(L[3], "15:45"))
    assert sent == []
    assert "221" not in desk.delivery.notes["SBIN"] and "400 needed" in desk.delivery.notes["SBIN"]
    assert desk.snapshot()["stocks"][0]["pullback"]["note"] == desk.delivery.notes["SBIN"]


def test_the_alerts_are_archived(world):
    start, sent = world
    desk = start(at(L[3], "15:00"))
    poll(desk, at(L[3], "15:45"))
    got = desk.signals_view(7)["delivery_alerts"]
    assert {(a["series_key"], a["status"]) for a in got} == {
        ("SBIN", PENDING_ENTRY),
        ("ITC", PENDING_ENTRY),
    }
    assert FakeNotifier  # the harness's recorder is the notifier here
