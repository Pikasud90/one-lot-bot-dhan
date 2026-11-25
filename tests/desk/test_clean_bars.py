"""US-097 point 3: the micro-trend rule raises no alert while today's bars are not all one-minute
bars of the live source (a bar from the delayed feed, or a hole of more than five minutes), and
the card says why. With clean bars it raises what it raised before (`test_microtrend.py` runs
the same walk through the same desk and is not changed). Other slots are not affected."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent))

from desk_harness import new_desk, small_cfg  # noqa: E402
from test_microtrend import BARS, DAY, Clock, poll_through, weekdays  # noqa: E402

from onelot.clock import IST  # noqa: E402
from onelot.data.store import save_bars  # noqa: E402
from onelot.desk import clean_bars  # noqa: E402
from onelot.desk.config import DeskInstrumentCfg, StrategySlotCfg, load_desk_config  # noqa: E402
from onelot.desk.tracker import needs_one_minute  # noqa: E402
from onelot.paper.factory import build_paper_book  # noqa: E402
from onelot.store import init_database  # noqa: E402
from onelot.store.schema import paper_trades  # noqa: E402
from onelot.strategies.registry import REGISTRY  # noqa: E402

D = dt.date(2026, 11, 23)


def ts(hhmm: str, day: dt.date = D) -> pd.Timestamp:
    h, m = (int(x) for x in hhmm.split(":"))
    return pd.Timestamp(dt.datetime.combine(day, dt.time(h, m)), tz=IST)


def minute_bars(start: str, end: str, day: dt.date = D, freq: str = "1min") -> pd.DataFrame:
    idx = pd.date_range(ts(start, day), ts(end, day), freq=freq)
    return pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0.0},
                        index=idx)  # fmt: skip


# ── the pure rule ─────────────────────────────────────────────────────────────
def test_clean_one_minute_bars_of_the_live_source_have_no_problem():
    bars = minute_bars("09:15", "12:00")
    assert clean_bars.problem(bars, {}, D, ts("12:00")) is None


def test_a_morning_of_delayed_two_minute_bars_holds_the_rule_and_says_since_when():
    delayed = minute_bars("09:15", "10:55", freq="2min")
    live = minute_bars("10:57", "12:00")
    unclean = clean_bars.note_frame({}, delayed, D, clean=False, width_min=2)
    unclean = clean_bars.note_frame(unclean, live, D, clean=True, width_min=1)
    bars = pd.concat([delayed, live])
    u = clean_bars.problem(bars, unclean, D, ts("12:00"))
    assert u is not None and u.since == ts("10:57")
    assert u.text().startswith("waiting for clean one-minute bars since 10:57")


def test_live_bars_that_replace_the_delayed_ones_make_the_day_clean_again():
    delayed = minute_bars("09:15", "10:55", freq="2min")
    unclean = clean_bars.note_frame({}, delayed, D, clean=False, width_min=2)
    refetched = minute_bars("09:15", "12:00")  # the live source served the whole day
    unclean = clean_bars.note_frame(unclean, refetched, D, clean=True, width_min=1)
    assert unclean == {}
    assert clean_bars.problem(refetched, unclean, D, ts("12:00")) is None


def test_a_delayed_bar_of_an_earlier_day_does_not_count():
    old = minute_bars("09:15", "15:29", day=D - dt.timedelta(days=3), freq="2min")
    unclean = clean_bars.note_frame({}, old, D - dt.timedelta(days=3), False, 2)
    unclean = clean_bars.note_frame(unclean, None, D, True, 1)  # the next day: dropped
    assert unclean == {}
    bars = pd.concat([old, minute_bars("09:15", "11:00")])
    assert clean_bars.problem(bars, unclean, D, ts("11:00")) is None


def test_a_hole_longer_than_five_minutes_holds_and_five_does_not():
    six = pd.concat([minute_bars("09:15", "11:00"), minute_bars("11:07", "12:00")])  # 11:01-11:06
    u = clean_bars.problem(six, {}, D, ts("12:00"))
    assert u is not None and u.since == ts("11:07") and "no bar from 11:01 to 11:07" in u.detail
    five = pd.concat([minute_bars("09:15", "11:00"), minute_bars("11:06", "12:00")])  # 11:01-11:05
    assert clean_bars.problem(five, {}, D, ts("12:00")) is None


def test_a_hole_after_the_signal_does_not_count_and_a_late_first_bar_does():
    bars = pd.concat([minute_bars("09:15", "11:00"), minute_bars("11:30", "12:00")])
    assert clean_bars.problem(bars, {}, D, ts("11:00")) is None  # the signal closed at 11:00
    late = minute_bars("09:40", "11:00")
    u = clean_bars.problem(late, {}, D, ts("11:00"))
    assert u is not None and "no bar from 09:15 to 09:40" in u.detail


def test_only_the_micro_trend_strategy_needs_one_minute_bars():
    from types import SimpleNamespace

    from onelot.strategies.registry import get_strategy

    needing = []
    for name in REGISTRY:
        try:
            strat = get_strategy(name, {"timeframe": "3m" if name == "microtrend" else "15m"})
        except Exception:  # noqa: BLE001 - a strategy that wants other settings is not a ladder
            continue
        if needs_one_minute(SimpleNamespace(strategy=strat)):  # type: ignore[arg-type]
            needing.append(name)
    assert needing == ["microtrend"]


# ── through the desk and the real paper book ──────────────────────────────────────
class MixedSource:
    """The 23 Nov morning: the delayed feed serves until 10:57 (the same minutes, labelled
    Yahoo), then the live source serves from its last bar on, as `DhanBars` does after a renewal.
    `hole` drops a stretch of minutes from what the live source serves."""

    name, delayed, resolution, primary, last_error = "dhan+yahoo", False, "1m", None, None

    def __init__(self, live_from: str | None = "10:57", hole: tuple[str, str] | None = None):
        self.served_by: dict[str, str] = {}
        self.live_from = live_from
        self.hole = hole

    def fetch(self, specs, days, since=None):
        live = BARS[BARS.index.date >= weekdays(DAY, 3)[0]]
        live = live[live.index + pd.Timedelta(minutes=1) <= Clock.now]
        if self.hole is not None:
            a, b = (ts(x, DAY) for x in self.hole)
            live = live[(live.index < a) | (live.index >= b)]
        switch = ts(self.live_from, DAY) if self.live_from else None
        out = {}
        for k in specs:
            if switch is not None and Clock.now < switch:
                self.served_by[k] = "yahoo"
                out[k] = live
            else:
                self.served_by[k] = "dhan"
                start = (since or {}).get(k)
                out[k] = live if start is None else live[live.index >= start - pd.Timedelta("5min")]
        return out


@pytest.fixture
def make_rig(cfg, tmp_path, monkeypatch):
    def build(source):
        db = init_database(f"sqlite:///{tmp_path / 'clean.db'}")
        save_bars(db.engine, "NIFTY", "1m", BARS[BARS.index.date < DAY], "test")
        clock = Clock()
        monkeypatch.setattr("onelot.desk.desk.now_ist", clock)
        book = build_paper_book(cfg, db.engine, clock=clock)
        shipped = load_desk_config().indices[0].strategies[0]
        slot = StrategySlotCfg("microtrend", "3m", dict(shipped.params))
        desk_cfg = small_cfg(indices=(DeskInstrumentCfg("NIFTY", "NIFTY_OPT", "INDEX", (slot,)),),
                             stocks=())  # fmt: skip
        desk, notifier, _ = new_desk(cfg, db, desk_cfg=desk_cfg, paper=book)
        desk.source = source
        desk.fine_minutes = 1
        for t in desk.trackers.values():
            t.fine_minutes = 1
        desk._load_minute_history(dt.datetime.combine(DAY, dt.time(8, 30), tzinfo=IST))
        return desk, notifier, db

    return build


def _trades(db):
    with db.engine.connect() as conn:
        return [dict(r) for r in conn.execute(select(paper_trades)).mappings()]


def test_with_clean_bars_the_rule_alerts_as_before(make_rig):
    desk, notifier, db = make_rig(MixedSource(live_from=None))
    poll_through(desk, "15:30")
    assert [n for n in notifier.sent if "microtrend" in n.title]
    assert _trades(db)
    t = desk.trackers["NIFTY"]
    assert t.unclean_bars == {} and t.clean_note is None


def test_a_morning_on_the_delayed_feed_raises_nothing_and_the_card_says_why(make_rig):
    desk, notifier, db = make_rig(MixedSource(live_from="10:57"))
    poll_through(desk, "15:30")
    assert not [n for n in notifier.sent if "microtrend" in n.title]
    assert _trades(db) == []
    t = desk.trackers["NIFTY"]
    assert t.unclean_bars, "the delayed bars of the morning are remembered"
    # the live source served from 5 minutes before its first poll at 10:57: clean from 10:49
    assert t.clean_note is not None
    assert t.clean_note.startswith("waiting for clean one-minute bars since 10:49: today's bars")


def test_the_card_note_while_the_market_is_open(make_rig):
    desk, notifier, db = make_rig(MixedSource(live_from="10:57"))
    poll_through(desk, "12:30")
    card = next(d for d in desk.snapshot()["indices"] if d["key"] == "NIFTY")
    assert card["health"]["clean_note"].startswith("waiting for clean one-minute bars since 10:49")
    assert "waiting for clean one-minute bars" in (card["trend"]["note"] or "")


def test_a_hole_in_the_live_bars_before_the_signal_raises_nothing(make_rig):
    desk, notifier, db = make_rig(MixedSource(live_from=None, hole=("10:00", "10:20")))
    poll_through(desk, "15:30")
    assert not [n for n in notifier.sent if "microtrend" in n.title]
    note = desk.trackers["NIFTY"].clean_note
    assert note == "waiting for clean one-minute bars since 10:20: no bar from 10:00 to 10:20"


def test_a_failing_check_holds_nothing(make_rig, monkeypatch):
    desk, notifier, db = make_rig(MixedSource(live_from="10:57"))

    def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(clean_bars, "problem", broken)
    poll_through(desk, "15:30")
    assert [n for n in notifier.sent if "microtrend" in n.title]  # as before this story
    assert desk.hook_errors > 0
