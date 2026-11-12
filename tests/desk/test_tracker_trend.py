"""The tracker's side of the multi-timeframe Supertrend (2026-11-11): the trend ladder a card
shows, what a multi-timeframe slot is given to read, and the line a slot's stop follows.

The world is the Lab's: twelve sessions of one-minute bars drifting one way, with a hundred
stored days before them. What the tracker reads is checked against the strategy run the way
a backtest runs it, and against the ladder built directly from the same bars."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging

import numpy as np
import pandas as pd
import pytest
from tests.lab.conftest import make_intraday

from onelot.data.hygiene import consistent_daily
from onelot.data.resample import resample_bars
from onelot.data.universe import load_specs
from onelot.desk import tracker as tracker_module
from onelot.desk.config import DeskInstrumentCfg, StrategySlotCfg, load_desk_config
from onelot.desk.tracker import HISTORY_GAP_DAYS, InstrumentTracker, _history_gap
from onelot.indicators import mtf
from onelot.indicators.core import supertrend
from onelot.strategies import mtf_support as ms
from onelot.strategies.base import Context
from onelot.strategies.registry import get_strategy

IST = "Asia/Kolkata"
FIRST_DAY = "2026-10-01"  # a Thursday; the sessions are 1, 2, 5-9 and 12-16 October
MTF = StrategySlotCfg("supertrend_mtf", "5m", {})  # the strategy's own defaults
LADDER = ["1m", "5m", "15m", "30m", "60m", "1d"]


def at(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz=IST)


#: the minute the rising world's only entry bar (10:35) closes
CUT = at("2026-10-15 10:40")


def stored_days(
    first_price: float, step: float, days: int = 100, before: str = FIRST_DAY
) -> pd.DataFrame:
    """`days` stored daily bars ending the last weekday before `before`, drifting by `step`
    a day into `first_price`: stamped 09:15 IST, the way the store keeps them."""
    rng = np.random.default_rng(1)
    dates = pd.bdate_range(end=pd.Timestamp(before) - pd.Timedelta(days=1), periods=days)
    close = first_price * np.exp(-step * np.arange(days, 0, -1) + rng.normal(0, 0.002, days))
    idx = pd.DatetimeIndex(
        [pd.Timestamp(d.date(), tz=IST) + pd.Timedelta(hours=9, minutes=15) for d in dates],
        name="ts",
    )
    return pd.DataFrame(
        {
            "open": close * 0.999,
            "high": close * 1.004,
            "low": close * 0.996,
            "close": close,
            "volume": 1e6,
        },
        index=idx,
    )


def world(seed: int, drift: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(one-minute bars of twelve sessions, the stored days before them), drifting one way."""
    minute = make_intraday(days=12, seed=seed, drift=drift, vol=0.0003, freq_min=1, start=FIRST_DAY)
    step = 0.002 if drift > 0 else -0.002
    return minute, stored_days(float(minute["open"].iloc[0]), step)


@pytest.fixture(scope="module")
def specs():
    return load_specs()


@pytest.fixture(scope="module")
def rising():
    return world(7, 0.00003)


@pytest.fixture(scope="module")
def falling():
    return world(5, -0.00003)


def build(
    specs,
    slots,
    bars: pd.DataFrame,
    history: pd.DataFrame | None,
    *,
    key: str = "NIFTY",
    tracked: str = "NIFTY_OPT",
    kind: str = "INDEX",
    fine: int = 1,
) -> InstrumentTracker:
    """A tracker as the desk builds one, holding `bars` and the stored days `history`."""
    series, instruments = specs
    cfg = DeskInstrumentCfg(key, tracked, kind, tuple(slots))
    t = InstrumentTracker(cfg, series[key], instruments[tracked], fine)
    t.bars, t.daily_history = bars, history
    return t


def before(bars: pd.DataFrame, moment: pd.Timestamp) -> pd.DataFrame:
    return bars[bars.index < moment]


def run(t: InstrumentTracker, now: pd.Timestamp = CUT):
    """One poll's evaluation (every slot looks again, as on a new bar)."""
    for slot in t.slots:
        slot.last_bar_ts = None
    return t.evaluate(now.to_pydatetime(), None, True)


def rows_by_timeframe(t: InstrumentTracker) -> dict[str, dict]:
    return {r["timeframe"]: r for r in t.trend["rows"]}


def lab_frame(minute: pd.DataFrame, history: pd.DataFrame, symbol: str = "NIFTY") -> pd.DataFrame:
    """The strategy on the WHOLE history, given what the Lab's runner gives it."""
    strategy = get_strategy("supertrend_mtf", {"timeframe": "5m"})
    ctx = Context(
        symbol=symbol,
        daily=consistent_daily(history, minute),
        extras={"minute_bars": minute, "has_volume": symbol != "NIFTY"},
    )
    return strategy.generate(resample_bars(minute, "5m"), ctx)


# ── a multi-timeframe slot reads what a backtest reads ────────────────────────


def test_the_slot_alerts_the_entry_a_backtest_finds(specs, rising):
    minute, history = rising
    frame = lab_frame(minute, history)
    entries = list(frame.index[frame["long_entry"]])
    assert entries == [at("2026-10-15 10:35")] and not frame["short_entry"].any()
    row = frame.loc[entries[0]]

    t = build(specs, [MTF], before(minute, CUT), history)
    ((slot, verdict),) = run(t)
    assert slot is t.slots[0] and slot.error is None
    assert (verdict.direction, verdict.fresh) == ("long", True)
    assert verdict.reason == "entry on the last closed bar"
    assert verdict.bar_ts == entries[0].to_pydatetime()
    assert verdict.close == pytest.approx(float(row["close"]))
    assert verdict.stop == pytest.approx(float(row["long_stop"]), abs=1e-4)
    assert verdict.stop < verdict.close
    assert verdict.reasons[0] == (
        "Supertrend(14, 1.7): 1m up, 5m up, 15m up, 30m up, 60m up, 1d up"
    )


def test_one_minute_earlier_the_bar_has_not_closed_and_nothing_fires(specs, rising):
    minute, history = rising
    early = CUT - pd.Timedelta(minutes=1)
    t = build(specs, [MTF], before(minute, early), history)
    assert run(t, early) == []
    assert t.slots[0].last_verdict.reason == "no setup on the last bar"
    assert t.slots[0].last_bar_ts == at("2026-10-15 10:30").to_pydatetime()


def test_a_falling_world_gives_a_stock_a_short(specs, falling):
    minute, history = falling
    frame = lab_frame(minute, history, "SBIN")
    entries = list(frame.index[frame["short_entry"]])
    assert entries == [at("2026-10-15 11:25")] and not frame["long_entry"].any()

    moment = at("2026-10-15 11:30")
    t = build(
        specs, [MTF], before(minute, moment), history, key="SBIN", tracked="SBIN", kind="STOCK"
    )
    ((slot, verdict),) = run(t, moment)
    assert verdict.direction == "short" and verdict.stop > verdict.close
    assert verdict.stop == pytest.approx(float(frame.loc[entries[0], "short_stop"]), abs=1e-4)
    assert slot.last_trail == {"long": None, "short": verdict.stop}
    assert (t.trend["aligned"], t.trend["down"], t.trend["up"]) == (-1, 6, 0)


def test_the_entry_is_refused_while_entries_are_off_but_the_reading_stands(specs, rising):
    minute, history = rising
    t = build(specs, [MTF], before(minute, CUT), history)
    for slot in t.slots:
        slot.last_bar_ts = None
    assert t.evaluate(CUT.to_pydatetime(), None, False) == []
    assert t.slots[0].last_verdict.direction == "long"  # the card still says what it saw
    assert t.trend["aligned"] == 1


# ── the line the stop follows ─────────────────────────────────────────────────


def test_the_slot_reports_the_line_its_stop_follows(specs, rising):
    minute, history = rising
    row = lab_frame(minute, history).loc[at("2026-10-15 10:35")]
    assert np.isnan(row["short_trail"])  # the line is under the price: no level for a short

    t = build(specs, [MTF], before(minute, CUT), history)
    run(t)
    slot = t.slots[0]
    level = round(float(row["long_trail"]), 4)
    assert slot.line_trail is True
    assert slot.last_trail == {"long": level, "short": None}
    assert slot.trail_level("long") == level and slot.trail_level("short") is None
    # the default trails the line of the trend timeframe
    assert slot.trail_view() == {"timeframe": "30m", "long": level, "short": None}
    assert slot.as_dict()["trail"] == slot.trail_view()


def test_the_line_is_reported_on_bars_without_an_entry_too(specs, rising):
    """An open trade's stop follows the line on every closed bar, not only on entry bars."""
    minute, history = rising
    frame = lab_frame(minute, history)
    later = at("2026-10-15 13:00")
    row = frame.loc[later - pd.Timedelta(minutes=5)]
    assert not row["long_entry"]

    t = build(specs, [MTF], before(minute, later), history)
    assert run(t, later) == []
    assert t.slots[0].last_trail == {"long": round(float(row["long_trail"]), 4), "short": None}


def test_a_plain_supertrend_slot_trails_its_line_only_when_asked(specs, rising):
    minute, history = rising
    seen = before(minute, CUT)
    setting = {"st_period": 14, "st_mult": 2.0}
    off = build(specs, [StrategySlotCfg("supertrend_vol", "30m", setting)], seen, history)
    on = build(
        specs,
        [StrategySlotCfg("supertrend_vol", "30m", {**setting, "line_trail": True})],
        seen,
        history,
    )
    run(off)
    run(on)

    plain = off.slots[0]
    assert plain.line_trail is False and plain.last_trail == {}
    assert plain.trail_view() is None and plain.trail_level("long") is None
    assert plain.as_dict()["trail"] is None

    # the level: the Supertrend line of the last CLOSED 30-minute bar, less the stop buffer
    closed = resample_bars(seen, "30m").iloc[:-1]  # the 10:15 bar is still forming at 10:40
    assert closed.index[-1] == at("2026-10-15 09:45")
    st = supertrend(closed["high"], closed["low"], closed["close"], 14, 2.0)
    assert st["st_dir"].iloc[-1] == 1
    slot = on.slots[0]
    features = slot.strategy.generate(closed, Context(symbol="NIFTY", extras={"has_volume": False}))
    expected = float(st["st_line"].iloc[-1]) - 0.1 * float(features["atr"].iloc[-1])
    assert slot.line_trail is True
    assert slot.last_trail == {"long": round(expected, 4), "short": None}
    # no trail timeframe of its own: the line is the slot's
    assert slot.trail_view() == {"timeframe": "30m", "long": round(expected, 4), "short": None}


def test_the_configured_index_slots_on_a_tracker(specs, rising):
    """The paper test as configured: the older Supertrend slot keeps its ATR trail, the new
    one follows the 60-minute line."""
    minute, history = rising
    series, instruments = specs
    nifty = next(i for i in load_desk_config().indices if i.key == "NIFTY")
    # US-067 D9 paused these slots in the shipped desk.yaml (2026-11-12); their settings are
    # what this test checks, so they are switched back on here.
    nifty = dataclasses.replace(
        nifty, strategies=tuple(dataclasses.replace(s, enabled=True) for s in nifty.strategies)
    )
    t = InstrumentTracker(nifty, series["NIFTY"], instruments[nifty.tracked], 1)
    t.bars, t.daily_history = before(minute, CUT), history
    run(t)
    older, multi = t.slots
    assert (older.cfg.name, older.cfg.timeframe) == ("supertrend_vol", "30m")
    assert older.line_trail is False and older.trail_view() is None
    assert (multi.cfg.name, multi.cfg.timeframe) == ("supertrend_mtf", "5m")
    view = multi.trail_view()
    assert view["timeframe"] == "60m" and view["short"] is None
    hour = mtf.snapshot(mtf.build_ladder(t.bars, consistent_daily(history, t.bars)), CUT)[
        LADDER.index("60m")
    ]
    assert hour["direction"] == 1 and view["long"] < hour["line"]  # 2 ATR beyond the line
    assert all(s.error is None for s in t.slots)


# ── the ladder on the card ────────────────────────────────────────────────────


def test_the_card_shows_the_ladder_at_the_close_of_the_last_fine_bar(specs, rising):
    minute, history = rising
    seen = before(minute, CUT)
    t = build(specs, [MTF], seen, history)
    run(t)
    trend = t.trend
    assert {k: v for k, v in trend.items() if k != "rows"} == {
        "period": 14,
        "mult": 1.7,
        "base": "1m",
        "up": 6,
        "down": 0,
        "aligned": 1,
        "unavailable": [],
        "at": "2026-10-15T10:40:00+05:30",
        "note": None,
    }
    assert [r["timeframe"] for r in trend["rows"]] == LADDER
    # the same reading as the ladder built directly from the bars
    direct = mtf.build_ladder(seen, consistent_daily(history, seen))
    assert trend["rows"] == mtf.snapshot(direct, CUT)
    assert all(
        set(r) == {"timeframe", "direction", "line", "since", "since_exact"} for r in trend["rows"]
    )
    json.dumps(trend, allow_nan=False)  # what the API sends


@pytest.mark.parametrize(
    "moment",
    ["2026-10-12 09:16", "2026-10-14 15:30", "2026-10-15 10:40", "2026-10-16 12:07"],
)
def test_bars_that_come_later_change_nothing_about_an_earlier_reading(specs, rising, moment):
    """No look-ahead: the reading at a minute is the same whether the bars end there or not."""
    minute, history = rising
    cut = at(moment)
    t = build(specs, [MTF], before(minute, cut), history)
    run(t, cut)
    whole = mtf.build_ladder(minute, consistent_daily(history, minute))
    assert t.trend["at"] == cut.isoformat()
    assert t.trend["rows"] == mtf.snapshot(whole, cut)


def test_the_ladder_is_read_at_the_data_time_not_the_wall_clock(specs, rising):
    minute, history = rising
    t = build(specs, [MTF], before(minute, CUT), history)
    run(t, CUT + pd.Timedelta(minutes=47))  # a late poll
    assert t.trend["at"] == "2026-10-15T10:40:00+05:30"


def test_the_card_and_the_slot_share_one_ladder(specs, rising, monkeypatch):
    """The resampling is done once a poll, however many read the ladder."""
    minute, history = rising
    calls = []
    real = ms.rung_frames

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(ms, "rung_frames", counted)
    t = build(specs, [MTF], before(minute, CUT), history)
    run(t)
    assert len(calls) == 1 and len(t._ladders) == 1
    run(t)  # the same bars again: nothing is rebuilt
    assert len(calls) == 1
    t.bars = before(minute, CUT + pd.Timedelta(minutes=1))  # the next minute arrives
    run(t, CUT + pd.Timedelta(minutes=1))
    assert len(calls) == 2
    assert t.trend["at"] == "2026-10-15T10:41:00+05:30"


def test_without_bars_there_is_no_ladder(specs, rising):
    _, history = rising
    t = build(specs, [MTF], pd.DataFrame(), history)
    assert run(t) == []
    assert t.trend == {}
    card = t.as_dict(CUT.to_pydatetime(), "dhan", False)
    assert card["trend"] == {} and card["health"]["state"] == "NO DATA"


def test_the_card_carries_the_ladder_and_each_slots_trail(specs, rising):
    minute, history = rising
    t = build(
        specs,
        [StrategySlotCfg("supertrend_vol", "30m", {}), MTF],
        before(minute, CUT),
        history,
    )
    run(t)
    card = t.as_dict(CUT.to_pydatetime(), "dhan", False)
    assert card["trend"] is t.trend and card["trend"]["aligned"] == 1
    assert [s["trail"] for s in card["strategies"]] == [None, t.slots[1].trail_view()]
    json.dumps(card, allow_nan=False)


# ── why a timeframe has no reading ────────────────────────────────────────────


def test_two_minute_bars_leave_the_faster_timeframes_without_a_reading(specs, rising):
    """The free failsafe feed sends 2-minute bars: 1m, 5m and 15m cannot be built from them,
    so the rule "all six agree" can never pass. The card says so and nothing breaks."""
    minute, history = rising
    two = resample_bars(minute, "2m")
    t = build(specs, [MTF], before(two, CUT), history, fine=2)
    assert run(t) == []
    trend = t.trend
    assert trend["base"] == "2m" and trend["unavailable"] == ["1m", "5m", "15m"]
    assert trend["at"] == "2026-10-15T10:41:00+05:30"  # the 10:39 bar closes at 10:41
    assert trend["note"] == "1m, 5m, 15m cannot be built from 2-minute bars"
    by = rows_by_timeframe(t)
    for tf in ("1m", "5m", "15m"):
        assert (by[tf]["direction"], by[tf]["line"], by[tf]["since"]) == (0, None, None)
    assert [by[tf]["direction"] for tf in ("30m", "60m", "1d")] == [1, 1, 1]
    assert (trend["up"], trend["down"], trend["aligned"]) == (3, 0, 0)
    slot = t.slots[0]
    assert slot.error is None and slot.last_verdict.reason == "no setup on the last bar"


@pytest.mark.parametrize("history", [None, pd.DataFrame()], ids=["none", "empty"])
def test_without_stored_days_the_daily_timeframe_has_no_reading(specs, rising, history):
    """The polled bars are far too few for a daily Supertrend. With "all six agree" the slot
    then stays silent; the line of the trail timeframe is still reported."""
    minute, stored = rising
    t = build(specs, [MTF], before(minute, CUT), history)
    assert t.daily_bars() is None
    assert run(t) == []
    by = rows_by_timeframe(t)
    assert by["1d"]["direction"] == 0 and by["1d"]["line"] is None
    assert [by[tf]["direction"] for tf in LADDER[:-1]] == [1, 1, 1, 1, 1]
    assert (t.trend["up"], t.trend["aligned"]) == (5, 0)
    assert t.trend["note"] == "no stored daily history: the 1d trend has no reading"
    slot = t.slots[0]
    assert slot.last_verdict.reason == "no setup on the last bar"
    with_days = build(specs, [MTF], before(minute, CUT), stored)
    run(with_days)
    assert slot.last_trail == with_days.slots[0].last_trail  # the 30m line needs no days

    # the desk's own word about the store is what the card says
    t.history_note = "the store has no daily history for it: no 1d trend reading (run a backfill)"
    run(t)
    assert t.trend["note"] == t.history_note


def test_a_timeframe_with_too_few_bars_is_warming_up(specs, rising):
    minute, _ = rising
    few = minute[minute.index >= at("2026-10-13")]  # three sessions
    history = stored_days(float(few["open"].iloc[0]), 0.002, before="2026-10-13")
    close = at("2026-10-15 15:30")
    t = build(specs, [MTF], before(few, close), history)
    run(t, close)
    by = rows_by_timeframe(t)
    # 1125 one-minute bars, 225 of 5m, 75 of 15m; 39 of 30m and 21 of 60m are under 70
    assert [by[tf]["direction"] != 0 for tf in LADDER] == [True, True, True, False, False, True]
    assert t.trend["note"] == "30m, 60m warming up (70 bars needed)"
    assert t.trend["aligned"] == 0


def test_stored_days_that_end_well_before_the_live_bars_are_said(specs, rising):
    minute, history = rising
    old = history[history.index < at("2026-09-16")]
    t = build(specs, [MTF], before(minute, CUT), old)
    run(t)
    assert t.trend["note"] == (
        "stored daily history ends 15 Sep 2026, 16 days before the live bars begin: "
        "the 1d trend is built without the sessions between (run a backfill)"
    )
    assert rows_by_timeframe(t)["1d"]["direction"] == 1  # it still reads, with that caveat


def test_too_few_stored_days_leave_the_daily_timeframe_without_a_reading(specs, rising):
    minute, history = rising
    t = build(specs, [MTF], before(minute, CUT), history.tail(30))
    run(t)
    assert rows_by_timeframe(t)["1d"]["direction"] == 0
    assert t.trend["note"] == "too few stored days for the 1d trend (70 needed)"


def test_the_gap_note_itself(rising):
    minute, history = rising
    assert history.index[-1].date() == dt.date(2026, 9, 30)  # the day before the first bar
    assert _history_gap(history, minute) is None
    for empty in (None, pd.DataFrame()):
        assert _history_gap(empty, minute) is None
        assert _history_gap(history, empty) is None
    # only days the live bars cover too: nothing is older, so nothing is missing
    assert _history_gap(resample_bars(minute, "1d"), minute) is None
    # a long weekend with a holiday (five calendar days) is no gap; one day more is
    assert HISTORY_GAP_DAYS == 5
    to_friday = stored_days(1000.0, 0.002, days=5, before="2026-10-10")  # ends Friday 9 October
    assert to_friday.index[-1].date() == dt.date(2026, 10, 9)
    assert _history_gap(to_friday, minute[minute.index >= at("2026-10-14")]) is None  # 5 days
    note = _history_gap(to_friday, minute[minute.index >= at("2026-10-15")])
    assert note is not None and note.startswith("stored daily history ends 09 Oct 2026, 6 days")


# ── the bars and the days the ladder is built from ────────────────────────────


def test_the_ladder_reads_the_bars_without_mock_sessions(specs, rising):
    """A Saturday mock session is in the polled bars and not in the ladder's (the Lab drops
    it when it loads); the cleaned frame is kept until the bars change."""
    minute, history = rising
    seen = before(minute, CUT)
    t = build(specs, [MTF], seen, history)
    assert t.fine_bars() is seen  # nothing to repair: the polled frame itself
    run(t)
    clean_rows = t.trend["rows"]

    friday = seen[seen.index.normalize() == at("2026-10-09")]
    mock = friday.copy()
    mock.index = mock.index + pd.Timedelta(days=1)  # Saturday 10 October, at twice the price
    mock[["open", "high", "low", "close"]] *= 2.0
    t.bars = pd.concat([seen, mock]).sort_index()
    cleaned = t.fine_bars()
    assert len(t.bars) == len(seen) + 375 and len(cleaned) == len(seen)
    pd.testing.assert_frame_equal(cleaned, seen)
    assert t.fine_bars() is cleaned  # kept
    run(t)
    assert t.trend["rows"] == clean_rows
    assert t.trend["aligned"] == 1


def test_the_days_are_the_stored_ones_with_the_live_days_on_top(specs, rising):
    minute, history = rising
    seen = before(minute, CUT)
    t = build(specs, [MTF], seen, history)
    days = t.daily_bars()
    pd.testing.assert_frame_equal(days, consistent_daily(history, seen))
    assert len(days) == 100 + 11  # eleven live sessions up to 15 October, today's included
    assert days.index[-1] == at("2026-10-15 09:15")
    assert t.daily_bars() is days  # kept until the bars or the history change
    t.bars = before(minute, CUT + pd.Timedelta(minutes=1))
    assert t.daily_bars() is not days
    assert float(t.daily_bars()["close"].iloc[-1]) == float(t.bars["close"].iloc[-1])


def test_todays_unfinished_day_is_not_read_as_a_closed_one(specs, rising):
    """All through the 15th the daily rung says what the close of the 14th said: a day counts
    from the following midnight, so it does not move while the session runs."""
    minute, history = rising
    t = build(specs, [MTF], before(minute, CUT), history)
    run(t)
    morning = rows_by_timeframe(t)["1d"]
    closed = consistent_daily(history, before(minute, at("2026-10-15")))  # days up to the 14th
    assert closed.index[-1] == at("2026-10-14 09:15")
    st = supertrend(closed["high"], closed["low"], closed["close"], 14, 1.7)
    assert morning["direction"] == int(st["st_dir"].iloc[-1]) == 1
    assert morning["line"] == pytest.approx(float(st["st_line"].iloc[-1]))

    close = at("2026-10-15 15:30")
    evening = build(specs, [MTF], before(minute, close), history)
    run(evening, close)
    assert rows_by_timeframe(evening)["1d"] == morning

    # and through the 14th it was the 13th's
    day_before = build(specs, [MTF], before(minute, at("2026-10-15")), history)
    run(day_before, at("2026-10-14 15:30"))
    assert day_before.trend["at"] == "2026-10-14T15:30:00+05:30"
    assert rows_by_timeframe(day_before)["1d"]["line"] != morning["line"]


# ── the card's regime ─────────────────────────────────────────────────────────


def test_the_cards_regime_comes_from_the_single_timeframe_slot_in_either_order(specs, rising):
    minute, history = rising
    seen = before(minute, CUT)
    vol = StrategySlotCfg("supertrend_vol", "30m", {})
    first = build(specs, [vol, MTF], seen, history)
    last = build(specs, [MTF, vol], seen, history)
    alone = build(specs, [vol], seen, history)
    for t in (first, last, alone):
        run(t)
    assert first.regime == last.regime == alone.regime
    assert first.regime["adx"] is not None and first.regime["trend"] == "up"

    only = build(specs, [MTF], seen, history)  # no other slot: the multi-timeframe one's bars
    run(only)
    assert only.regime["trend"] == "up" and only.regime["atr"] != first.regime["atr"]


# ── a failure of the reading ──────────────────────────────────────────────────


def test_a_failing_ladder_leaves_the_card_without_one_and_the_slot_running(
    specs, rising, monkeypatch, caplog
):
    minute, history = rising
    t = build(specs, [MTF], before(minute, CUT), history)

    def broken(*args, **kwargs):
        raise RuntimeError("no ladder today")

    with monkeypatch.context() as patched:
        patched.setattr(tracker_module.ms, "snapshot", broken)
        with caplog.at_level(logging.ERROR, logger="onelot.desk.tracker"):
            fired = run(t)
            run(t)
    assert t.trend == {}
    assert [v.direction for _, v in fired] == ["long"]  # the strategy alerted all the same
    said = [r for r in caplog.records if "the trend ladder failed" in r.getMessage()]
    assert len(said) == 1  # once, not every poll

    run(t)  # the reading works again
    assert t.trend["aligned"] == 1 and t.trend["note"] is None


def test_a_failing_slot_does_not_take_the_ladder_or_the_other_slot_with_it(
    specs, rising, monkeypatch
):
    minute, history = rising
    t = build(
        specs,
        [MTF, StrategySlotCfg("supertrend_vol", "30m", {})],
        before(minute, CUT),
        history,
    )

    def broken(df, ctx=None):
        raise ValueError("bad bars")

    monkeypatch.setattr(t.slots[0].strategy, "generate", broken)
    assert run(t) == []
    multi, plain = t.slots
    assert multi.error == "ValueError: bad bars"
    assert plain.error is None and plain.last_verdict.reason == "no setup on the last bar"
    assert t.trend["aligned"] == 1
