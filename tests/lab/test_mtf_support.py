"""The multi-timeframe Supertrend ladder as a strategy reads it (`strategies/mtf_support.py`):
which timeframes a gate asks for, which rungs a feed can give, the columns a strategy gets at
each of its decisions (always from bars that have closed) and the ladder cache."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from tests.lab.conftest import make_intraday

from onelot.clock import IST
from onelot.data.resample import resample_bars
from onelot.indicators import core as ind
from onelot.indicators import mtf
from onelot.strategies import mtf_support as ms
from onelot.strategies.base import Context

TFS = ("1m", "5m", "15m", "30m", "60m", "1d")
DAYS = 6  # 2026-10-01 (Thu), 02, 05, 06, 07, 08


def at(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz=IST)


@pytest.fixture(scope="module")
def minute() -> pd.DataFrame:
    return make_intraday(days=DAYS, seed=7, start="2026-10-01", vol=0.0004, freq_min=1)


@pytest.fixture(scope="module")
def two_minute() -> pd.DataFrame:
    return make_intraday(days=DAYS, seed=7, start="2026-10-01", vol=0.0006, freq_min=2)


@pytest.fixture(scope="module")
def five(minute) -> pd.DataFrame:
    return resample_bars(minute, "5m")


@pytest.fixture(scope="module")
def ladder(minute) -> ms.Ladder:
    return ms.build(minute, "1m")


@pytest.fixture(scope="module")
def decisions(five) -> np.ndarray:
    return ms.decision_ns(five.index, "5m")


def one_bar(tf: str, direction: int) -> mtf.TimeframeTrend:
    """A trend of one closed bar, for a ladder whose directions the test sets by hand."""
    return mtf.TimeframeTrend(
        tf, np.array([0]), np.array([1]), np.array([direction], dtype=np.int8), np.array([100.0])
    )


# ── which timeframes must agree ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("gate", "trend_tf", "expected"),
    [
        ("all", "30m", TFS),
        ("all", "1m", TFS),
        ("slower", "30m", ("30m", "60m", "1d")),
        ("slower", "1m", TFS),
        ("slower", "1d", ("1d",)),
        ("faster", "30m", ("1m", "5m", "15m", "30m")),
        ("faster", "1m", ("1m",)),
        ("daily", "30m", ("30m", "1d")),
        ("daily", "1d", ("1d",)),
        ("trend", "15m", ("15m",)),
        ("slower", "1h", ("60m", "1d")),  # an alias of 60m
    ],
)
def test_a_gate_names_the_timeframes_that_must_agree(gate, trend_tf, expected):
    assert ms.gate_timeframes(gate, trend_tf) == expected


def test_an_unknown_gate_or_timeframe_is_refused():
    with pytest.raises(ValueError, match="gate 'sideways' is not one of all, slower, faster"):
        ms.gate_timeframes("sideways", "30m")
    with pytest.raises(ValueError, match="trend timeframe '2m' is not on the ladder 1m, 5m"):
        ms.gate_timeframes("all", "2m")
    assert ms.GATES == ("all", "slower", "faster", "daily", "trend")


# ── the bars of each rung ─────────────────────────────────────────────────────


def test_one_minute_bars_give_every_rung(minute):
    frames = ms.rung_frames(minute, "1m", None)
    assert tuple(frames) == TFS
    assert frames["1m"] is minute  # the base itself, not a copy
    # 375 minutes a session: 75 five-minute bars, 25, 13 (the last cut short), 7, and the day
    assert [len(frames[tf]) for tf in TFS] == [n * DAYS for n in (375, 75, 25, 13, 7, 1)]
    pd.testing.assert_frame_equal(frames["30m"], resample_bars(minute, "30m"))


def test_the_daily_rung_is_the_daily_series_given(minute):
    daily = resample_bars(minute, "1d").iloc[:3]
    assert ms.rung_frames(minute, "1m", daily)["1d"] is daily
    # no daily series (or an empty one): the days are resampled from the base bars
    for nothing in (None, daily.iloc[:0]):
        assert len(ms.rung_frames(minute, "1m", nothing)["1d"]) == DAYS


def test_a_rung_the_base_cannot_give_exactly_is_unavailable(two_minute, five):
    frames = ms.rung_frames(two_minute, "2m", None)
    assert [tf for tf, bars in frames.items() if bars is None] == ["1m", "5m", "15m"]
    assert (len(frames["30m"]), len(frames["60m"]), len(frames["1d"])) == (78, 42, 6)
    frames = ms.rung_frames(five, "5m", None)
    assert [tf for tf, bars in frames.items() if bars is None] == ["1m"]
    assert frames["5m"] is five
    hourly = resample_bars(five, "60m")
    frames = ms.rung_frames(hourly, "1h", None)  # the base may be named by an alias
    assert [tf for tf, bars in frames.items() if bars is None] == ["1m", "5m", "15m", "30m"]
    assert frames["60m"] is hourly


def test_a_ladder_says_which_rungs_it_does_not_have(minute, two_minute, ladder):
    assert (ladder.base, ladder.period, ladder.mult, ladder.has_volume) == ("1m", 14, 1.7, True)
    assert ladder.unavailable == () and tuple(ladder.trends) == TFS
    delayed = ms.build(two_minute, "2m", period=10, mult=3.0, has_volume=False)
    assert (delayed.base, delayed.period, delayed.mult, delayed.has_volume) == (
        "2m",
        10,
        3.0,
        False,
    )
    assert delayed.unavailable == ("1m", "5m", "15m")
    for tf in delayed.unavailable:  # no bars, so no direction at any time
        assert len(delayed.trends[tf].direction) == 0
        d, line = delayed.trends[tf].at(np.array([at("2026-10-08 15:30").value]))
        assert d.tolist() == [0] and np.isnan(line).all()


# ── reading one rung ──────────────────────────────────────────────────────────


def test_position_is_the_last_bar_closed_at_or_before_each_time(ladder):
    closes = ladder.trends["30m"].close_ns
    times = np.array([closes[0] - 1, closes[0], closes[1] - 1, closes[1], closes[-1] + 10**12])
    pos, known = ladder.position("30m", times)
    assert known.tolist() == [False, True, True, True, True]
    assert pos.tolist() == [0, 0, 0, 1, len(closes) - 1]


def test_runs_number_the_trends_and_count_the_bars_since_each_turn(minute):
    ladder = ms.build(minute, "1m")
    d = np.array([0, 0, 1, 1, -1, -1, -1, 1], dtype=np.int8)
    ladder.trends["30m"] = mtf.TimeframeTrend(
        "30m", np.arange(8), np.arange(8) + 1, d, np.full(8, 100.0)
    )
    seg, since = ladder.runs("30m")
    # the first reading after the warm-up is not a turn: nobody saw the trend before it
    assert seg.tolist() == [0, 0, 0, 0, 1, 1, 1, 2]
    assert since.tolist() == [ms.NO_FLIP] * 4 + [0, 1, 2, 0]
    assert ladder.runs("30m")[0] is seg  # worked out once


def test_an_unavailable_rung_has_no_runs_and_no_inputs(two_minute):
    delayed = ms.build(two_minute, "2m")
    seg, since = delayed.runs("5m")
    assert len(seg) == 0 and len(since) == 0
    inp = delayed.inputs("5m")
    assert (len(inp.atr), len(inp.adx), len(inp.rsi), len(inp.rvol)) == (0, 0, 0, 0)


def test_inputs_are_the_indicators_of_that_timeframes_own_bars(minute, ladder):
    bars = resample_bars(minute, "30m")
    h, l, c, v = bars["high"], bars["low"], bars["close"], bars["volume"]
    inp = ladder.inputs("30m")
    assert ms.FILTER_PERIOD == 14
    np.testing.assert_allclose(inp.atr, ind.atr(h, l, c, 14).to_numpy(float), equal_nan=True)
    np.testing.assert_allclose(inp.adx, ind.adx(h, l, c, 14)["adx"].to_numpy(float), equal_nan=True)
    np.testing.assert_allclose(inp.rsi, ind.rsi(c, 14).to_numpy(float), equal_nan=True)
    # relative volume against the same time of day, falling back to the plain 20-bar figure
    rvol = ind.tod_rvol(v, days=10).fillna(ind.rvol(v, 20)).to_numpy(float)
    np.testing.assert_allclose(inp.rvol, rvol, equal_nan=True)
    assert ladder.inputs("30m") is inp  # worked out once
    days = resample_bars(minute, "1d")
    np.testing.assert_allclose(
        ladder.inputs("1d").rvol, ind.rvol(days["volume"], 20).to_numpy(float), equal_nan=True
    )


def test_without_traded_volume_the_relative_volume_has_no_reading(minute):
    """An index has no volume: the figure is NaN, which a volume filter reads as neutral."""
    declared = ms.build(minute, "1m", has_volume=False).inputs("30m")
    assert np.isnan(declared.rvol).all() and len(declared.rvol) == 13 * DAYS
    assert np.isfinite(declared.atr[-1])  # the price inputs are not affected
    silent = ms.build(minute.assign(volume=0.0), "1m").inputs("30m")  # declared, but all zero
    assert np.isnan(silent.rvol).all()


# ── what a strategy reads at each decision ────────────────────────────────────


def test_a_strategy_decides_at_the_close_of_its_bar(five, decisions):
    assert decisions[0] == at("2026-10-01 09:20").value
    assert decisions[-1] == at("2026-10-08 15:30").value
    assert (np.diff(decisions[:75]) == 5 * 60 * 10**9).all()


def test_ladder_columns_are_the_direction_and_line_of_every_rung(ladder, decisions):
    cols = ms.ladder_columns(ladder, decisions)
    assert list(cols) == [k for tf in TFS for k in (f"mtf_{tf}", f"mtf_line_{tf}")] + [
        "mtf_up",
        "mtf_down",
    ]
    dirs, lines = mtf.ladder_at(ladder.trends, decisions)
    for tf in TFS:
        assert cols[f"mtf_{tf}"].dtype == np.int8
        np.testing.assert_array_equal(cols[f"mtf_{tf}"], dirs[tf])
        np.testing.assert_array_equal(cols[f"mtf_line_{tf}"], lines[tf])
    stack = np.vstack([cols[f"mtf_{tf}"] for tf in TFS])
    np.testing.assert_array_equal(cols["mtf_up"], (stack == 1).sum(axis=0))
    np.testing.assert_array_equal(cols["mtf_down"], (stack == -1).sum(axis=0))
    assert set(np.unique(stack)) == {-1, 0, 1}  # the fast rungs are warm, the slow ones not yet
    assert (cols["mtf_up"] + cols["mtf_down"] <= 6).all()


def test_trend_columns_come_from_the_last_closed_bar_of_the_entry_timeframe(
    minute, five, ladder, decisions
):
    cols = ms.trend_columns(ladder, "30m", decisions)
    assert list(cols) == ["mtf_seg", "mtf_since", "mtf_atr", "mtf_adx", "mtf_rsi", "mtf_rvol"]
    # 09:20 .. 09:40 on the first morning: no 30-minute bar has closed yet
    assert cols["mtf_seg"][:5].tolist() == [-1] * 5
    assert cols["mtf_since"][:5].tolist() == [ms.NO_FLIP] * 5
    for name in ("mtf_atr", "mtf_adx", "mtf_rsi", "mtf_rvol"):
        assert np.isnan(cols[name][:5]).all()
    bars = resample_bars(minute, "30m")
    atr = ind.atr(bars["high"], bars["low"], bars["close"], 14)
    rsi = ind.rsi(bars["close"], 14)
    forming = five.index.get_loc(at("2026-10-06 10:00"))  # decides at 10:05
    closed = five.index.get_loc(at("2026-10-06 10:10"))  # decides at 10:15
    # at 10:05 the 09:45 bar is still forming: the reading is the 09:15 bar's
    assert cols["mtf_atr"][forming] == pytest.approx(atr.loc[at("2026-10-06 09:15")])
    assert cols["mtf_rsi"][forming] == pytest.approx(rsi.loc[at("2026-10-06 09:15")])
    # at 10:15 it has just closed
    assert cols["mtf_atr"][closed] == pytest.approx(atr.loc[at("2026-10-06 09:45")])
    assert cols["mtf_rsi"][closed] == pytest.approx(rsi.loc[at("2026-10-06 09:45")])
    seg, since = ladder.runs("30m")
    pos, known = ladder.position("30m", decisions)
    np.testing.assert_array_equal(cols["mtf_seg"][known], seg[pos[known]])
    np.testing.assert_array_equal(cols["mtf_since"][known], since[pos[known]])
    aliased = ms.trend_columns(ladder, "1h", decisions)  # the timeframe may be named by an alias
    np.testing.assert_array_equal(
        aliased["mtf_atr"], ms.trend_columns(ladder, "60m", decisions)["mtf_atr"]
    )


def test_the_last_bar_of_a_session_is_first_read_the_next_morning(minute, five, ladder, decisions):
    """The 15:15 half-hour bar is cut short by the close; it counts as closed at 15:45, so
    the session's last decision (15:30) still reads the 14:45 bar."""
    cols = ms.trend_columns(ladder, "30m", decisions)
    bars = resample_bars(minute, "30m")
    rsi = ind.rsi(bars["close"], 14)
    last = five.index.get_loc(at("2026-10-06 15:25"))  # decides at 15:30
    assert cols["mtf_rsi"][last] == pytest.approx(rsi.loc[at("2026-10-06 14:45")])
    first = five.index.get_loc(at("2026-10-07 09:15"))  # decides at 09:20 the next morning
    assert cols["mtf_rsi"][first] == pytest.approx(rsi.loc[at("2026-10-06 15:15")])


def test_trend_columns_of_an_unavailable_rung_have_no_reading(two_minute, decisions):
    cols = ms.trend_columns(ms.build(two_minute, "2m"), "15m", decisions)
    assert (cols["mtf_seg"] == -1).all() and (cols["mtf_since"] == ms.NO_FLIP).all()
    for name in ("mtf_atr", "mtf_adx", "mtf_rsi", "mtf_rvol"):
        assert np.isnan(cols[name]).all() and len(cols[name]) == len(decisions)


def test_trail_columns_are_the_line_and_the_atr_of_the_trail_timeframe(
    minute, five, ladder, decisions
):
    cols = ms.trail_columns(ladder, "5m", decisions)
    assert list(cols) == ["mtf_trail_dir", "mtf_trail_line", "mtf_trail_atr"]
    direction, line = ladder.trends["5m"].at(decisions)
    np.testing.assert_array_equal(cols["mtf_trail_dir"], direction)
    np.testing.assert_array_equal(cols["mtf_trail_line"], line)
    assert set(np.unique(direction[100:])) == {-1, 1}  # warm, and it has turned both ways
    # a five-minute strategy reads the five-minute bar that has just closed: its own bar
    st = ind.supertrend(five["high"], five["low"], five["close"], 14, 1.7)
    np.testing.assert_array_equal(direction[100:], st["st_dir"].to_numpy()[100:])
    np.testing.assert_allclose(line[100:], st["st_line"].to_numpy(float)[100:])
    atr5 = ind.atr(five["high"], five["low"], five["close"], 14).to_numpy(float)
    np.testing.assert_allclose(cols["mtf_trail_atr"], atr5, equal_nan=True)
    # a slower trail: the ATR is that timeframe's own, from its last closed bar
    hourly = resample_bars(minute, "60m")
    atr60 = ind.atr(hourly["high"], hourly["low"], hourly["close"], 14)
    slow = ms.trail_columns(ladder, "1h", decisions)
    forming = five.index.get_loc(at("2026-10-06 11:00"))  # decides at 11:05, inside the 10:15 bar
    assert slow["mtf_trail_atr"][forming] == pytest.approx(atr60.loc[at("2026-10-06 09:15")])
    # 42 hourly bars are fewer than the 70 the hourly Supertrend warms up on: no line yet
    assert (slow["mtf_trail_dir"] == 0).all() and np.isnan(slow["mtf_trail_line"]).all()
    empty = ms.trail_columns(ms.build(five, "5m"), "1m", decisions)  # a rung that does not exist
    assert (empty["mtf_trail_dir"] == 0).all() and np.isnan(empty["mtf_trail_atr"]).all()


# ── where the ladder comes from ───────────────────────────────────────────────


def test_without_fine_bars_the_strategys_own_bars_are_the_base(five):
    for ctx in (Context(symbol="X"), Context(extras={"minute_bars": five.iloc[:0]})):
        ladder = ms.ladder_for(five, ctx, "5m", 14, 1.7)
        assert ladder.base == "5m" and ladder.unavailable == ("1m",)
        assert len(ladder.trends["5m"].direction) == len(five)


def test_fine_bars_in_the_context_are_the_base(minute, two_minute, five):
    ladder = ms.ladder_for(five, Context(extras={"minute_bars": minute}), "5m", 14, 1.7)
    assert ladder.base == "1m" and ladder.unavailable == ()
    assert len(ladder.trends["1m"].direction) == len(minute)
    delayed = Context(extras={"minute_bars": two_minute, "minute_resolution": "2m"})
    ladder = ms.ladder_for(five, delayed, "5m", 14, 1.7)
    assert ladder.base == "2m" and ladder.unavailable == ("1m", "5m", "15m")


def test_the_contexts_daily_series_and_volume_flag_are_used(minute, five):
    daily = resample_bars(minute, "1d").iloc[:4]
    ctx = Context(daily=daily, extras={"minute_bars": minute, "has_volume": False})
    ladder = ms.ladder_for(five, ctx, "5m", 14, 1.7)
    assert len(ladder.trends["1d"].direction) == 4 and ladder.has_volume is False
    plain = ms.ladder_for(five, Context(extras={"minute_bars": minute}), "5m", 14, 1.7)
    assert len(plain.trends["1d"].direction) == DAYS and plain.has_volume is True
    empty = Context(daily=daily.iloc[:0], extras={"minute_bars": minute})
    assert len(ms.ladder_for(five, empty, "5m", 14, 1.7).trends["1d"].direction) == DAYS


# ── the cache ─────────────────────────────────────────────────────────────────


def cached(minute, cache, symbol: str = "X") -> Context:
    return Context(symbol=symbol, extras={"minute_bars": minute, "mtf_cache": cache})


def test_the_same_data_and_setting_give_the_same_ladder(minute, five):
    cache = ms.LadderCache()
    ctx = cached(minute, cache)
    first = ms.ladder_for(five, ctx, "5m", 14, 1.7)
    assert ms.ladder_for(five, ctx, "5m", 14, 1.7) is first and len(cache) == 1
    assert ms.ladder_for(five, ctx, "5m", 14, 1.7 + 1e-9) is first  # the same setting
    other = ms.ladder_for(five, ctx, "5m", 10, 3.0)
    assert other is not first and (other.period, other.mult) == (10, 3.0) and len(cache) == 2
    # without a cache every call builds a new one
    bare = Context(symbol="X", extras={"minute_bars": minute})
    assert ms.ladder_for(five, bare, "5m", 14, 1.7) is not ms.ladder_for(five, bare, "5m", 14, 1.7)


def test_the_bars_are_resampled_once_for_every_setting_of_the_same_data(minute, five, monkeypatch):
    calls: list[str] = []
    real = ms.rung_frames

    def counting(base_bars, base, daily, *timeframes):  # US-068: a ladder names its timeframes
        calls.append(base)
        return real(base_bars, base, daily, *timeframes)

    monkeypatch.setattr(ms, "rung_frames", counting)
    cache = ms.LadderCache()
    ctx = cached(minute, cache)
    for period, mult in ((14, 1.7), (10, 3.0), (14, 1.7), (7, 2.0)):
        ms.ladder_for(five, ctx, "5m", period, mult)
    assert calls == ["1m"]
    ms.ladder_for(five, cached(minute.iloc[:-1], cache), "5m", 14, 1.7)  # one bar fewer
    assert calls == ["1m", "1m"]
    changed = minute.copy()
    changed.iloc[-1, changed.columns.get_loc("close")] += 1.0  # the last bar was revised
    ms.ladder_for(five, cached(changed, cache), "5m", 14, 1.7)
    ms.ladder_for(five, cached(changed, cache, symbol="Y"), "5m", 14, 1.7)  # another instrument
    assert calls == ["1m"] * 4
    with_days = Context(
        symbol="Y",
        daily=resample_bars(minute, "1d"),
        extras={"minute_bars": changed, "mtf_cache": cache},
    )
    ms.ladder_for(five, with_days, "5m", 14, 1.7)  # another daily series
    assert calls == ["1m"] * 5


def test_the_least_recently_used_ladder_goes_first(minute, five):
    cache = ms.LadderCache(maxsize=2)
    ctx = cached(minute, cache)
    a = ms.ladder_for(five, ctx, "5m", 14, 1.7)
    b = ms.ladder_for(five, ctx, "5m", 10, 3.0)
    assert ms.ladder_for(five, ctx, "5m", 14, 1.7) is a  # used again: now the most recent
    ms.ladder_for(five, ctx, "5m", 7, 2.0)  # a third setting: the 10/3.0 ladder goes
    assert len(cache) == 2
    assert ms.ladder_for(five, ctx, "5m", 14, 1.7) is a
    assert ms.ladder_for(five, ctx, "5m", 10, 3.0) is not b
    assert ms.LadderCache(maxsize=0).maxsize == 1 and ms.LadderCache().maxsize == 4


def test_a_plain_dict_works_as_a_cache_of_four(minute, five):
    store: dict = {}
    ctx = cached(minute, store)
    first = ms.ladder_for(five, ctx, "5m", 14, 1.7)
    assert ms.ladder_for(five, ctx, "5m", 14, 1.7) is first
    for period in (7, 8, 9, 10):
        ms.ladder_for(five, ctx, "5m", period, 1.7)
    assert len(store) == 4
    assert ms.ladder_for(five, ctx, "5m", 14, 1.7) is not first  # it was the oldest


def test_a_fingerprint_tells_one_data_set_from_another(minute):
    assert ms.fingerprint(None) == (0, 0, 0, 0.0) == ms.fingerprint(minute.iloc[:0])
    fp = ms.fingerprint(minute)
    assert fp == (
        len(minute),
        minute.index[0].value,
        minute.index[-1].value,
        float(minute["close"].iloc[-1]),
    )
    assert ms.fingerprint(minute.iloc[:-1]) != fp and ms.fingerprint(minute.iloc[1:]) != fp
    changed = minute.copy()
    changed.iloc[-1, changed.columns.get_loc("close")] += 0.05
    assert ms.fingerprint(changed) != fp
    assert ms.fingerprint(minute.copy()) == fp


# ── the ladder as the desk shows it ───────────────────────────────────────────


def test_a_snapshot_counts_the_rungs_and_says_when_all_agree(minute):
    ladder = ms.build(minute, "1m")
    now = at("2026-10-08 15:30")

    def read(*directions: int) -> dict:
        ladder.trends = {tf: one_bar(tf, d) for tf, d in zip(TFS, directions, strict=True)}
        return ms.snapshot(ladder, now)

    snap = read(1, 1, 1, 1, 1, 1)
    assert list(snap) == ["period", "mult", "base", "rows", "up", "down", "aligned", "unavailable"]
    assert (snap["period"], snap["mult"], snap["base"], snap["unavailable"]) == (14, 1.7, "1m", [])
    assert [r["timeframe"] for r in snap["rows"]] == list(TFS)
    assert [r["direction"] for r in snap["rows"]] == [1] * 6
    assert (snap["up"], snap["down"], snap["aligned"]) == (6, 0, 1)
    snap = read(-1, -1, -1, -1, -1, -1)
    assert (snap["up"], snap["down"], snap["aligned"]) == (0, 6, -1)
    snap = read(1, 1, 1, 1, 1, -1)  # one against
    assert (snap["up"], snap["down"], snap["aligned"]) == (5, 1, 0)
    snap = read(1, 1, 1, 1, 1, 0)  # one without a reading: that is not agreement
    assert (snap["up"], snap["down"], snap["aligned"]) == (5, 0, 0)
    assert snap["rows"][-1] == {
        "timeframe": "1d",
        "direction": 0,
        "line": None,
        "since": None,
        "since_exact": False,
    }


def test_a_snapshot_of_a_delayed_feed_names_the_missing_rungs(two_minute):
    snap = ms.snapshot(ms.build(two_minute, "2m"), at("2026-10-08 15:30"))
    assert snap["base"] == "2m" and snap["unavailable"] == ["1m", "5m", "15m"]
    assert [r["direction"] for r in snap["rows"][:3]] == [0, 0, 0]
    assert snap["aligned"] == 0  # three rungs can never agree: the "all" gate stays shut
    assert snap["rows"][3]["direction"] in (-1, 1)  # 78 half-hour bars: warm
