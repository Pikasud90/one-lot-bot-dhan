"""The multi-timeframe Supertrend ladder (`indicators/mtf.py`): what the last CLOSED bar of
each timeframe said at a moment, with no look-ahead."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from tests.lab.conftest import make_intraday

from onelot.data.resample import resample_bars
from onelot.indicators import mtf
from onelot.indicators.core import supertrend

IST = "Asia/Kolkata"
MIN = 60 * 1_000_000_000


def ns(ts: str) -> int:
    """An IST wall-clock time as UTC nanoseconds."""
    return int(mtf.index_ns(pd.DatetimeIndex([pd.Timestamp(ts, tz=IST)]))[0])


def trend(directions, lines=None, start="2026-09-01 09:15", minutes=5) -> mtf.TimeframeTrend:
    """A hand-made trend: one bar per direction given, the line 100, 101, 102..."""
    idx = pd.date_range(start, periods=len(directions), freq=f"{minutes}min", tz=IST)
    tf = f"{minutes}m"
    values = lines if lines is not None else [100.0 + i for i in range(len(directions))]
    return mtf.TimeframeTrend(
        tf,
        mtf.index_ns(idx),
        mtf.nominal_close_ns(idx, tf),
        np.array(directions, dtype=np.int8),
        np.array(values, dtype=float),
    )


# ── when a bar counts as closed ───────────────────────────────────────────────


def test_an_intraday_bar_closes_one_bar_length_after_its_start():
    idx = pd.date_range("2026-09-01 09:15", periods=3, freq="5min", tz=IST)
    assert list(mtf.nominal_close_ns(idx, "5m")) == [
        ns("2026-09-01 09:20"),
        ns("2026-09-01 09:25"),
        ns("2026-09-01 09:30"),
    ]
    hour = pd.DatetimeIndex([pd.Timestamp("2026-09-01 15:15", tz=IST)])
    # the hour bar the session cuts short still closes at its nominal end
    assert list(mtf.nominal_close_ns(hour, "60m")) == [ns("2026-09-01 16:15")]
    assert list(mtf.nominal_close_ns(hour, "1h")) == [ns("2026-09-01 16:15")]


def test_a_daily_bar_closes_at_the_following_midnight():
    days = pd.DatetimeIndex(
        [pd.Timestamp("2026-09-01 09:15", tz=IST), pd.Timestamp("2026-09-04 00:00", tz=IST)]
    )
    assert list(mtf.nominal_close_ns(days, "1d")) == [
        ns("2026-09-02 00:00"),
        ns("2026-09-05 00:00"),
    ]


def test_naive_times_are_read_as_utc():
    naive = pd.DatetimeIndex([pd.Timestamp("2026-09-01 03:45")])
    assert list(mtf.index_ns(naive)) == [ns("2026-09-01 09:15")]


# ── the reading at a moment ───────────────────────────────────────────────────


def test_the_reading_is_the_last_bar_closed_at_or_before_the_time():
    t = trend([1, 1, -1])  # bars 09:15, 09:20, 09:25 -> closed 09:20, 09:25, 09:30
    times = np.array(
        [
            ns("2026-09-01 09:19"),  # nothing closed yet
            ns("2026-09-01 09:20"),  # the first bar, exactly at its close
            ns("2026-09-01 09:24"),  # the second bar is still forming
            ns("2026-09-01 09:25"),
            ns("2026-09-01 09:29"),
            ns("2026-09-01 09:30"),
            ns("2026-09-02 10:00"),  # long after: the last bar still stands
        ]
    )
    d, line = t.at(times)
    assert list(d) == [0, 1, 1, 1, 1, -1, -1]
    assert np.isnan(line[0])
    assert list(line[1:]) == [100.0, 100.0, 101.0, 101.0, 102.0, 102.0]
    assert d.dtype == np.int8


def test_a_bar_that_is_not_warmed_up_has_no_line():
    t = trend([0, 1])
    d, line = t.at(np.array([ns("2026-09-01 09:20"), ns("2026-09-01 09:25")]))
    assert list(d) == [0, 1]
    assert np.isnan(line[0]) and line[1] == 101.0


def test_an_empty_trend_reads_nothing():
    t = mtf.timeframe_trend(pd.DataFrame(), "15m")
    assert t.timeframe == "15m" and len(t.direction) == 0
    d, line = t.at(np.array([ns("2026-09-01 09:20"), ns("2026-09-01 12:00")]))
    assert list(d) == [0, 0] and np.isnan(line).all()
    assert mtf.timeframe_trend(None, "1h").timeframe == "60m"


# ── where the trend began ─────────────────────────────────────────────────────


def test_trend_start_is_the_first_bar_of_the_run():
    t = trend([0, 1, 1, -1, -1, 1])
    starts = t.start_ns
    # the up-run that began on bar 1 follows the warm-up, not a down-run: at least that old
    assert t.trend_start(ns("2026-09-01 09:30")) == (int(starts[1]), False)
    # the down-run of bars 3 and 4 follows an up-run: the turn is known exactly
    assert t.trend_start(ns("2026-09-01 09:40")) == (int(starts[3]), True)
    assert t.trend_start(ns("2026-09-01 09:45")) == (int(starts[5]), True)


def test_trend_start_is_none_without_a_reading():
    t = trend([0, 1])
    assert t.trend_start(ns("2026-09-01 09:19")) is None  # before any bar closed
    assert t.trend_start(ns("2026-09-01 09:20")) is None  # the bar is not warmed up


def test_a_run_from_the_very_first_bar_is_not_exact():
    t = trend([1, 1])
    assert t.trend_start(ns("2026-09-01 09:25")) == (int(t.start_ns[0]), False)


# ── Supertrend per timeframe ──────────────────────────────────────────────────


def test_the_trend_is_the_supertrend_after_the_warm_up():
    bars = make_intraday(days=3, freq_min=5)
    t = mtf.timeframe_trend(bars, "5m")
    st = supertrend(bars["high"], bars["low"], bars["close"], mtf.ST_PERIOD, mtf.ST_MULT)
    warm = mtf.WARMUP_FACTOR * mtf.ST_PERIOD
    assert (mtf.ST_PERIOD, mtf.ST_MULT, warm) == (14, 1.7, 70)
    assert (t.direction[:warm] == 0).all()
    assert (t.direction[warm:] == st["st_dir"].to_numpy()[warm:]).all()
    assert set(np.unique(t.direction[warm:])) <= {-1, 1}
    assert np.allclose(t.line[warm:], st["st_line"].to_numpy()[warm:])
    assert list(t.start_ns) == list(mtf.index_ns(bars.index))
    assert list(t.close_ns) == list(mtf.index_ns(bars.index) + 5 * MIN)


def test_the_warm_up_can_be_set_and_never_exposes_a_missing_line():
    bars = make_intraday(days=1, freq_min=5)
    t = mtf.timeframe_trend(bars, "5m", period=4, mult=2.0, warmup_bars=0)
    assert (t.direction[np.isnan(t.line)] == 0).all()
    assert np.isnan(t.line).any()  # the ATR seed: the first bars have no line at all
    assert (t.direction[~np.isnan(t.line)] != 0).all()
    short = mtf.timeframe_trend(bars.iloc[:10], "5m")  # fewer bars than the warm-up
    assert (short.direction == 0).all()


def test_a_past_reading_does_not_change_when_later_bars_arrive():
    """The answer for a past moment is the same computed then or years later."""
    minute = make_intraday(days=4, freq_min=1, seed=11)
    cut_at = pd.Timestamp("2026-09-03 11:07", tz=IST)  # mid-way through a 15m and a 60m bar
    known = minute[minute.index < cut_at]  # the minutes closed by 11:07
    since = pd.Timestamp("2026-09-02 09:15", tz=IST)
    probe = mtf.index_ns(pd.date_range(since, cut_at, freq="1min"))
    for tf in ("5m", "15m", "60m"):
        full = mtf.timeframe_trend(resample_bars(minute, tf), tf, period=3, warmup_bars=6)
        then = mtf.timeframe_trend(resample_bars(known, tf), tf, period=3, warmup_bars=6)
        d_full, line_full = full.at(probe)
        d_then, line_then = then.at(probe)
        assert (d_full == d_then).all(), tf
        assert np.allclose(line_full, line_then, equal_nan=True), tf
        assert (d_full != 0).any(), tf  # the comparison is not of empty readings


def test_a_forming_bar_says_nothing():
    minute = make_intraday(days=2, freq_min=1, seed=3)
    bars = resample_bars(minute, "15m")
    t = mtf.timeframe_trend(bars, "15m", period=2, warmup_bars=0)
    k = 30  # the 15m bar that starts at bars.index[k]
    forming = int(t.start_ns[k]) + 14 * MIN
    d, line = t.at(np.array([forming, int(t.close_ns[k])]))
    assert d[0] == t.direction[k - 1] and line[0] == t.line[k - 1]
    assert d[1] == t.direction[k] and line[1] == t.line[k]


# ── the ladder ────────────────────────────────────────────────────────────────


def test_the_ladder_has_one_trend_per_timeframe_fastest_first():
    minute = make_intraday(days=3, freq_min=1)
    ladder = mtf.build_ladder(minute, period=3)
    assert tuple(ladder) == ("1m", "5m", "15m", "30m", "60m", "1d") == mtf.LADDER_TIMEFRAMES
    assert len(ladder["1m"].direction) == len(minute)
    assert len(ladder["5m"].direction) == len(resample_bars(minute, "5m"))
    assert len(ladder["60m"].direction) == 3 * 7  # 09:15 ... 15:15 on each of 3 sessions
    assert len(ladder["1d"].direction) == 3  # resampled from the minutes


def test_the_daily_rung_uses_the_daily_bars_given():
    minute = make_intraday(days=3, freq_min=1)
    daily = resample_bars(make_intraday(days=40, freq_min=15, start="2026-07-01"), "1d")
    ladder = mtf.build_ladder(minute, daily, period=3)
    assert len(ladder["1d"].direction) == 40
    assert list(ladder["1d"].start_ns) == list(mtf.index_ns(daily.index))


def test_the_base_resolution_is_used_as_given():
    five = make_intraday(days=3, freq_min=5)
    ladder = mtf.build_ladder(five, timeframes=("5m", "15m", "1h"), period=3, base="5m")
    assert tuple(ladder) == ("5m", "15m", "60m")
    assert len(ladder["5m"].direction) == len(five)


def test_ladder_at_reads_every_timeframe_at_the_same_moments():
    ladder = {"5m": trend([1, 1, -1]), "15m": trend([1, -1], minutes=15)}
    times = np.array([ns("2026-09-01 09:25"), ns("2026-09-01 09:30"), ns("2026-09-01 09:45")])
    dirs, lines = mtf.ladder_at(ladder, times)
    assert list(dirs["5m"]) == [1, -1, -1]
    assert list(dirs["15m"]) == [0, 1, -1]  # the first 15m bar closes at 09:30
    assert np.isnan(lines["15m"][0]) and list(lines["15m"][1:]) == [100.0, 101.0]


def test_alignment_needs_every_listed_timeframe_to_agree():
    dirs = {
        "5m": np.array([1, 1, -1, -1, 0, 1], dtype=np.int8),
        "15m": np.array([1, -1, -1, 1, 1, 0], dtype=np.int8),
        "60m": np.array([1, 1, -1, -1, 1, 1], dtype=np.int8),
    }
    assert list(mtf.alignment(dirs, ["5m", "15m", "60m"])) == [1, 0, -1, 0, 0, 0]
    assert list(mtf.alignment(dirs, ["5m", "1h"])) == [1, 1, -1, -1, 0, 1]  # 1h is 60m
    assert list(mtf.alignment(dirs, ["15m"])) == [1, -1, -1, 1, 1, 0]
    assert mtf.alignment(dirs, ["5m"]).dtype == np.int8
    # the inputs are not changed by the call
    assert list(dirs["5m"]) == [1, 1, -1, -1, 0, 1]


def test_alignment_refuses_an_empty_list():
    with pytest.raises(ValueError, match="at least one timeframe"):
        mtf.alignment({"5m": np.array([1], dtype=np.int8)}, [])


# ── the desk's view ───────────────────────────────────────────────────────────


def test_the_snapshot_has_one_row_per_timeframe_with_ist_times():
    ladder = {"5m": trend([0, 1, 1, -1]), "15m": trend([1, 1], minutes=15)}
    rows = mtf.snapshot(ladder, pd.Timestamp("2026-09-01 09:36", tz=IST))
    assert [r["timeframe"] for r in rows] == ["5m", "15m"]
    assert rows[0] == {
        "timeframe": "5m",
        "direction": -1,
        "line": 103.0,
        "since": "2026-09-01T09:30:00+05:30",
        "since_exact": True,
    }
    # only the first 15m bar is closed at 09:36; its run starts with the bars given
    assert rows[1] == {
        "timeframe": "15m",
        "direction": 1,
        "line": 100.0,
        "since": "2026-09-01T09:15:00+05:30",
        "since_exact": False,
    }


def test_the_snapshot_of_a_timeframe_without_a_reading_is_blank():
    ladder = {"5m": trend([0, 0]), "60m": mtf.timeframe_trend(pd.DataFrame(), "60m")}
    rows = mtf.snapshot(ladder, pd.Timestamp("2026-09-01 09:36", tz=IST))
    for row in rows:
        assert row["direction"] == 0 and row["line"] is None
        assert row["since"] is None and row["since_exact"] is False


def test_the_snapshot_takes_a_utc_or_naive_moment():
    ladder = {"5m": trend([1, 1, -1])}
    ist = mtf.snapshot(ladder, pd.Timestamp("2026-09-01 09:26", tz=IST))
    utc = mtf.snapshot(ladder, pd.Timestamp("2026-09-01 03:56", tz="UTC"))
    naive = mtf.snapshot(ladder, pd.Timestamp("2026-09-01 03:56"))
    assert ist == utc == naive
    assert ist[0]["direction"] == 1 and ist[0]["line"] == 101.0
