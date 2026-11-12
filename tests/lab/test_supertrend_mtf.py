"""The multi-timeframe Supertrend strategy (`strategies/supertrend_mtf.py`), the owner's rule
of 2026-11-11: one setting (14, 1.7) on six timeframes, a trade when the chosen ones agree,
and the Supertrend line of one of them as the stop and the trail.

Three made-up markets, all from fixed seeds so every number below is reproducible:

* `up` and `down`: twelve sessions of one-minute bars drifting one way, behind a hundred
  stored days that drift the same way (the "1d" rung needs seventy days to warm up);
* `turn`: thirteen sessions that rise, dip late on the twelfth and recover, so the 30-minute
  trend turns up afresh on the morning of the thirteenth (2026-10-19). What the thirteenth
  session does after the entry is scripted per test.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from tests.lab.conftest import make_intraday

from onelot.clock import IST
from onelot.data.hygiene import consistent_daily
from onelot.data.resample import resample_bars
from onelot.lab.simulator import simulate
from onelot.strategies import mtf_support as ms
from onelot.strategies.base import Context
from onelot.strategies.registry import REGISTRY, get_strategy
from onelot.strategies.supertrend_mtf import STMtfParams, SupertrendMTF

D = 375  # one-minute bars in a session
FIRST_DAY = "2026-10-01"
NO_READING = (
    "No reading means too few bars so far, or a feed that cannot give that timeframe "
    "(a delayed 2-minute feed has no 1m, 5m or 15m bars)"
)
#: the form the desk runs: a fresh 30-minute turn with strong ADX and RSI, the 60-minute line
FRESH_TURN = {
    "allow_short": False,
    "gate": "all",
    "trend_tf": "30m",
    "flip_window": 4,
    "trend_adx_min": 25,
    "trend_rsi_min": 60,
    "entry_end": "13:00",
    "trail_tf": "60m",
    "stop_buffer_atr": 2.0,
    "max_trades_per_day": 1,
}
#: drift per minute from that minute on: up, a dip from 13:35 of the twelfth session, a rally
#: from 15:05 that carries on through the thirteenth
TURN = {0: 0.00003, 11 * D + 260: -0.0001, 11 * D + 350: 0.00012}
LAST = 12 * D  # the first minute of the thirteenth session
SIGNAL_COLUMNS = [
    f"{side}_{what}"
    for side in ("long", "short")
    for what in ("entry", "stop", "trail", "exit", "score")
]


@dataclass(frozen=True)
class World:
    minute: pd.DataFrame
    daily: pd.DataFrame
    five: pd.DataFrame


def stored_daily(first_price: float, step: float, days: int = 100) -> pd.DataFrame:
    """`days` stored daily bars ending the day before the first session, drifting by `step`
    a day towards `first_price`."""
    rng = np.random.default_rng(1)
    dates = pd.bdate_range(end=pd.Timestamp(FIRST_DAY) - pd.Timedelta(days=1), periods=days)
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


def scripted(days: int, schedule: dict[int, float], seed: int = 3) -> pd.DataFrame:
    """One-minute bars; `schedule` maps a minute number (0 = the first bar) to the drift per
    minute from there on."""
    stamps: list[pd.Timestamp] = []
    for day in pd.bdate_range(FIRST_DAY, periods=days):
        start = pd.Timestamp(day.date(), tz=IST) + pd.Timedelta(hours=9, minutes=15)
        stamps.extend(pd.date_range(start, periods=D, freq="1min"))
    idx = pd.DatetimeIndex(stamps, name="ts")
    n = len(idx)
    drift = np.zeros(n)
    for minute, value in sorted(schedule.items()):
        drift[minute:] = value
    rng = np.random.default_rng(seed)
    close = 1000.0 * np.exp(np.cumsum(rng.normal(0, 0.0003, n) + drift))
    open_ = np.r_[1000.0, close[:-1]]
    spread = np.abs(rng.normal(0, 0.0003, n)) * close
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + spread,
            "low": np.minimum(open_, close) - spread,
            "close": close,
            "volume": rng.integers(10_000, 50_000, n).astype(float),
        },
        index=idx,
    )


def world_of(minute: pd.DataFrame, step: float = 0.002) -> World:
    daily = consistent_daily(stored_daily(float(minute["open"].iloc[0]), step), minute)
    return World(minute, daily, resample_bars(minute, "5m"))


def trending(seed: int, drift: float) -> World:
    minute = make_intraday(days=12, seed=seed, drift=drift, vol=0.0003, freq_min=1, start=FIRST_DAY)
    return world_of(minute, 0.002 if drift > 0 else -0.002)


@pytest.fixture(scope="module")
def up() -> World:
    return trending(7, 0.00003)


@pytest.fixture(scope="module")
def down() -> World:
    return trending(5, -0.00003)


@functools.cache
def turn(*after: tuple[int, float]) -> World:
    """The `turn` market; `after` = (minute of the thirteenth session, drift from there on)."""
    return world_of(scripted(13, {**TURN, **{LAST + m: d for m, d in after}}))


def signals(world: World, params: dict | None = None, *, has_volume: bool = True):
    strat = get_strategy("supertrend_mtf", params or {})
    ctx = Context(
        symbol="X",
        daily=world.daily,
        extras={"minute_bars": world.minute, "has_volume": has_volume},
    )
    return strat, strat.generate(world.five, ctx)


def fresh_turn(world: World, **over):
    """The desk's form on an index (no traded volume)."""
    return signals(world, {**FRESH_TURN, **over}, has_volume=False)


def entries(f: pd.DataFrame, side: str = "long") -> list[str]:
    return [ts.strftime("%m-%d %H:%M") for ts in f.index[f[f"{side}_entry"].to_numpy()]]


def bar(f: pd.DataFrame, text: str) -> int:
    return int(f.index.get_loc(pd.Timestamp(text, tz=IST)))


def trades(strat, f: pd.DataFrame):
    return simulate(f, strat.p, "X", "supertrend_mtf", slippage_pct=0.0)


def levels(f: pd.DataFrame, trade, side: str = "long") -> list[float]:
    """The distinct trail levels reported from the signal to the bar before the exit."""
    seen = f.loc[trade.signal_ts : trade.exit_ts, f"{side}_trail"].iloc[:-1].dropna()
    return [round(float(x), 3) for x in pd.unique(seen.round(6))]


# ── the setting ───────────────────────────────────────────────────────────────


def test_the_default_setting_is_the_owners_rule():
    p = STMtfParams()
    assert (p.st_period, p.st_mult) == (14, 1.7)  # the same on every timeframe
    assert (p.timeframe, p.trend_tf, p.gate, p.trail_tf) == ("5m", "30m", "all", "trend")
    assert p.exit_on_flip is True and p.stop_buffer_atr == 2.0
    # the line is the only exit rule: no target, no second trail, no partial
    assert (p.target_r, p.trail_atr_mult, p.partial_r) == (0.0, 0.0, 0.0)
    assert p.market_filter is False and p.regime_filter is False
    assert (p.flip_window, p.trend_adx_min, p.trend_rsi_min, p.trend_rvol_min) == (0, 0, 0, 0)


def test_it_is_the_one_registered_strategy_that_reads_other_timeframes():
    assert REGISTRY["supertrend_mtf"] is SupertrendMTF
    assert [name for name, cls in REGISTRY.items() if cls.multi_timeframe] == ["supertrend_mtf"]


def test_the_trail_timeframe_is_the_entry_timeframe_unless_one_is_named():
    assert get_strategy("supertrend_mtf").trail_timeframe == "30m"
    assert get_strategy("supertrend_mtf", {"trend_tf": "15m"}).trail_timeframe == "15m"
    assert get_strategy("supertrend_mtf", {"trail_tf": "60m"}).trail_timeframe == "60m"


@pytest.mark.parametrize("params", [{"gate": "most"}, {"trend_tf": "45m"}, {"trail_tf": "2h"}])
def test_a_setting_that_is_not_on_the_ladder_is_refused(params):
    with pytest.raises(ValidationError):
        get_strategy("supertrend_mtf", params)


# ── the entry ─────────────────────────────────────────────────────────────────


def test_a_long_is_taken_at_the_first_decision_where_all_six_agree(up):
    _, f = signals(up)
    assert entries(f) == ["10-15 10:35"] and entries(f, "short") == []
    i = bar(f, "2026-10-15 10:35")
    assert (f["mtf_up"].iloc[:i] < 6).all() and f["mtf_up"].iloc[i] == 6
    # the six agree on many later decisions too: one entry per trend of the entry timeframe
    assert int((f["mtf_up"] == 6).sum()) > 20


def test_the_stop_is_the_line_of_the_trail_timeframe_less_the_buffer(up):
    _, f = signals(up)
    row = f.iloc[bar(f, "2026-10-15 10:35")]
    assert row["mtf_trail_line"] == row["mtf_line_30m"] == pytest.approx(1089.7590342491133)
    assert row["mtf_trail_atr"] == pytest.approx(3.201300656654217)
    assert row["long_stop"] == pytest.approx(row["mtf_trail_line"] - 2.0 * row["mtf_trail_atr"])
    assert row["long_stop"] == pytest.approx(1083.3564329358048)
    assert row["long_trail"] == row["long_stop"] < row["close"]
    assert row["long_score"] == 50.0  # no strength of its own: the neutral 50


def test_a_short_is_the_mirror_image(down):
    _, f = signals(down)
    assert entries(f, "short") == ["10-15 11:25"] and entries(f) == []
    row = f.iloc[bar(f, "2026-10-15 11:25")]
    assert row["mtf_down"] == 6
    assert row["short_stop"] == pytest.approx(row["mtf_trail_line"] + 2.0 * row["mtf_trail_atr"])
    assert row["short_stop"] == pytest.approx(929.6581818769547)
    assert row["short_trail"] == row["short_stop"] > row["close"]


def test_one_timeframe_against_the_rest_keeps_the_gate_shut(up, down):
    strat, f = signals(up)
    i = bar(f, "2026-10-15 10:35")
    assert not f["long_entry"].iloc[i - 1]
    assert strat.explain(f, i - 1, "long")["blockers"] == ["Not all up yet: 5m down"]
    strat, f = signals(down)
    j = bar(f, "2026-10-15 11:25")
    assert strat.explain(f, j - 1, "short")["blockers"] == ["Not all down yet: 15m up"]


def test_the_reasons_name_every_timeframe_the_readings_and_the_stop(up):
    strat, f = signals(up)
    why = strat.explain(f, bar(f, "2026-10-15 10:35"), "long")
    assert why["reasons"][:4] == [
        "Supertrend(14, 1.7): 1m up, 5m up, 15m up, 30m up, 60m up, 1d up",
        "All 6 timeframes that must agree point up",
        "30m ADX 72.2, RSI 81.0, relative volume 0.9",
        "Stop and trail: the 30m Supertrend line 1089.76. A 30m close beyond it ends the "
        "trade; the stop stands 2 ATR beyond it at 1083.36",
    ]
    assert why["blockers"] == []
    assert why["implies"] == (
        "The timeframes agree that the micro trend is up. The Supertrend line of the trail "
        "timeframe is the stop: it follows the market, never back, and the trade ends when the "
        "market crosses it."
    )
    assert "micro trend is down" in strat.implies("short")


@pytest.mark.parametrize(
    ("gate", "expected"),
    [
        ("all", ["10-15 10:35"]),  # 1m, 5m, 15m, 30m, 60m, 1d
        ("slower", ["10-15 10:10"]),  # 30m, 60m, 1d: agreed 25 minutes earlier
        ("daily", ["10-08 12:10"]),  # 30m and 1d
        ("trend", ["10-08 12:10"]),  # 30m alone
    ],
)
def test_the_gate_names_the_timeframes_that_must_agree(up, gate, expected):
    _, f = signals(up, {"gate": gate})
    assert entries(f) == expected
    at_entry = f[f["long_entry"]]
    for tf in ms.gate_timeframes(gate, "30m"):
        assert (at_entry[f"mtf_{tf}"] == 1).all(), tf


def test_a_faster_entry_timeframe_gives_one_entry_for_each_of_its_trends(up):
    _, f = signals(up, {"gate": "trend", "trend_tf": "5m"})
    for side, sign in (("long", 1), ("short", -1)):
        taken = f[f[f"{side}_entry"]]
        assert len(taken) > 10
        assert (taken["mtf_5m"] == sign).all()
        assert taken["mtf_seg"].is_unique  # never twice in one trend


def test_one_entry_for_each_trend_or_one_in_every_session_of_it(up):
    _, once = signals(up)
    _, daily = signals(up, {"reenter_daily": True})
    assert entries(once) == ["10-15 10:35"]
    # the trend runs on into the next session: its first decision in the entry window (09:30)
    assert entries(daily) == ["10-15 10:35", "10-16 09:25"]
    assert (
        daily["mtf_seg"].iloc[bar(daily, "2026-10-15 10:35")]
        == daily["mtf_seg"].iloc[bar(daily, "2026-10-16 09:25")]
    )


def test_the_entry_window_is_applied_before_the_first_decision_is_picked(up):
    """The first agreement (10:35, decided at 10:40) is outside a window: the trade is taken
    at the first agreement inside it and is not lost."""
    assert entries(signals(up, {"entry_end": "10:40"})[1]) == ["10-15 10:35"]
    assert entries(signals(up, {"entry_end": "10:30"})[1]) == ["10-16 09:25"]
    _, late = signals(up, {"entry_start": "11:00"})
    assert entries(late) == ["10-15 11:05"]
    i = bar(late, "2026-10-15 11:05")
    # the two decisions before it were inside the window, but the 1m trend was down
    assert late["mtf_up"].iloc[i - 2 : i + 1].tolist() == [5, 5, 6]


def test_a_side_that_is_switched_off_still_shows_its_setup(up, down):
    _, f = signals(down, {"allow_short": False})
    assert entries(f, "short") == [] and int(f["setup_short"].sum()) == 1
    _, f = signals(up, {"allow_long": False})
    assert entries(f) == [] and int(f["setup_long"].sum()) == 1


# ── the filters on the entry timeframe ────────────────────────────────────────


def test_a_weak_adx_holds_the_entry_back(up):
    strat, f = signals(up, {"trend_adx_min": 73})
    i = bar(f, "2026-10-15 10:35")
    assert f["mtf_adx"].iloc[i] == pytest.approx(72.17434576459722)
    assert strat.explain(f, i, "long")["blockers"] == [
        "30m ADX 72.2 does not pass the filter (needs at least 73)"
    ]
    assert entries(f) == ["10-16 09:25"]
    assert f.loc[f["long_entry"], "mtf_adx"].min() >= 73
    assert entries(signals(up, {"trend_adx_min": 72})[1]) == ["10-15 10:35"]


def test_a_long_needs_the_rsi_at_or_above_the_limit(up):
    strat, f = signals(up, {"trend_rsi_min": 82})
    i = bar(f, "2026-10-15 10:35")
    assert strat.explain(f, i, "long")["blockers"] == [
        "30m RSI 81.0 does not pass the filter (needs at least 82)"
    ]
    assert entries(f) == ["10-15 10:40"]
    assert f["mtf_rsi"].iloc[i + 1] == pytest.approx(82.02, abs=0.01)


def test_a_short_needs_the_rsi_at_or_below_a_hundred_less_the_limit(down):
    assert entries(signals(down, {"trend_rsi_min": 61})[1], "short") == [
        "10-15 11:25"
    ]  # 38.6 <= 39
    strat, f = signals(down, {"trend_rsi_min": 62})
    i = bar(f, "2026-10-15 11:25")
    assert strat.explain(f, i, "short")["blockers"] == [
        "30m RSI 38.6 does not pass the filter (needs at most 38)"
    ]
    assert entries(f, "short") == ["10-15 14:40"]
    assert f.loc[f["short_entry"], "mtf_rsi"].max() <= 38


def test_thin_volume_holds_the_entry_back_where_there_is_volume(up):
    strat, f = signals(up, {"trend_rvol_min": 1.0})
    i = bar(f, "2026-10-15 10:35")
    assert strat.explain(f, i, "long")["blockers"] == [
        "30m relative volume 0.9 does not pass the filter (needs at least 1)"
    ]
    assert entries(f) == ["10-15 10:40"]
    assert entries(signals(up, {"trend_rvol_min": 5.0})[1]) == []


def test_an_index_has_no_volume_so_the_volume_filter_is_neutral(up):
    strat, f = signals(up, {"trend_rvol_min": 5.0}, has_volume=False)
    assert f["mtf_rvol"].isna().all()
    assert entries(f) == ["10-15 10:35"]
    why = strat.explain(f, bar(f, "2026-10-15 10:35"), "long")
    assert why["reasons"][2] == "30m ADX 72.2, RSI 81.0" and why["blockers"] == []


# ── the fresh turn (the form the desk runs) ───────────────────────────────────


def test_the_fresh_turn_form_enters_on_the_morning_the_trend_turns():
    strat, f = fresh_turn(turn())
    assert entries(f) == ["10-19 10:10"] and entries(f, "short") == []
    i = bar(f, "2026-10-19 10:10")
    assert f["mtf_since"].iloc[i] == 0
    assert f["mtf_adx"].iloc[i] >= 25 and f["mtf_rsi"].iloc[i] >= 60
    assert strat.explain(f, i, "long")["reasons"][:5] == [
        "Supertrend(14, 1.7): 1m up, 5m up, 15m up, 30m up, 60m up, 1d up",
        "All 6 timeframes that must agree point up",
        "The 30m trend turned up on its last closed bar",
        "30m ADX 46.8, RSI 69.8",
        "Stop and trail: the 60m Supertrend line 1140.53. A 60m close beyond it ends the "
        "trade; the stop stands 2 ATR beyond it at 1130.41",
    ]


def test_a_turn_that_is_no_longer_fresh_is_not_entered():
    strat, f = fresh_turn(turn())
    i = bar(f, "2026-10-19 12:10")
    assert f["mtf_up"].iloc[i] == 6 and f["mtf_since"].iloc[i] == 4
    why = strat.explain(f, i, "long")
    assert why["reasons"][2] == "The 30m trend turned up 4 bars of 30m ago"
    assert why["blockers"] == ["The turn is not within the last 4 bars of 30m"]
    # five minutes earlier the turn was three bars old: still inside the window
    before = strat.explain(f, i - 1, "long")
    assert before["reasons"][2] == "The 30m trend turned up 3 bars of 30m ago"
    assert before["blockers"] == []
    assert strat.explain(f, bar(f, "2026-10-19 10:40"), "long")["reasons"][2] == (
        "The 30m trend turned up 1 bar of 30m ago"
    )


def test_a_trend_that_never_turned_is_taken_only_without_the_turn_window(up):
    """The first trend after the warm-up has no turn on record."""
    assert entries(fresh_turn(up)[1]) == []
    _, f = fresh_turn(turn(), flip_window=0)
    assert entries(f) == ["10-15 10:15", "10-19 10:10"]
    assert f["mtf_since"].iloc[bar(f, "2026-10-15 10:15")] == ms.NO_FLIP


# ── the stop and the trail, through the Lab ───────────────────────────────────


def test_the_trail_level_is_reported_only_while_the_line_is_on_the_trades_side(up):
    _, f = signals(up)
    line, atr = f["mtf_trail_line"], f["mtf_trail_atr"]
    above, below = f["mtf_trail_dir"] == 1, f["mtf_trail_dir"] == -1
    assert above.any() and below.any()
    pd.testing.assert_series_equal(
        f["long_trail"], (line - 2.0 * atr).where(above), check_names=False
    )
    pd.testing.assert_series_equal(
        f["short_trail"], (line + 2.0 * atr).where(below), check_names=False
    )
    assert f["long_stop"].equals(f["long_trail"]) and f["short_stop"].equals(f["short_trail"])
    # the exit flag is the line on the other side
    assert f["long_exit"].equals(below) and f["short_exit"].equals(above)


def test_a_calm_trend_is_held_to_the_session_end_with_the_stop_raised_along_the_way():
    strat, f = fresh_turn(turn())
    (trade,) = trades(strat, f)
    assert trade.side == 1
    assert trade.signal_ts == pd.Timestamp("2026-10-19 10:10", tz=IST)
    assert trade.entry_ts == pd.Timestamp("2026-10-19 10:15", tz=IST)
    assert trade.entry_price == pytest.approx(1152.302657692412)  # the next bar's open
    assert trade.stop_initial == pytest.approx(1130.4120419770875)
    assert trade.target is None
    assert levels(f, trade) == [1130.412, 1136.416, 1143.389, 1150.345, 1156.221, 1163.99]
    assert (trade.exit_reason, trade.exit_ts) == (
        "square_off",
        pd.Timestamp("2026-10-19 15:15", tz=IST),
    )
    assert trade.avg_exit == pytest.approx(1189.9576831624581)
    assert trade.r_multiple == pytest.approx(1.72, abs=0.001)


def test_a_fall_through_the_raised_stop_ends_the_trade_at_the_raised_level():
    strat, f = fresh_turn(turn((180, -0.0004)))  # falls from 12:15
    (trade,) = trades(strat, f)
    raised = levels(f, trade)
    assert raised == [1130.412, 1136.416, 1143.389]
    assert (trade.exit_reason, trade.exit_ts) == ("trail", pd.Timestamp("2026-10-19 13:00", tz=IST))
    assert trade.avg_exit == pytest.approx(1143.3886679711225)
    assert round(trade.avg_exit, 3) == raised[-1] > round(trade.stop_initial, 3)
    assert trade.r_multiple == pytest.approx(-0.407, abs=0.001)  # the first stop was a full -1


def test_a_close_beyond_the_line_ends_the_trade_before_the_stop_is_touched():
    strat, f = fresh_turn(turn((120, -0.0003)))  # falls from 11:15
    (trade,) = trades(strat, f)
    flagged = f.index[f["long_exit"].to_numpy() & (f.index >= trade.entry_ts)]
    # the 60-minute bar that closed at 12:15 closed under its line
    assert flagged[0] == pd.Timestamp("2026-10-19 12:10", tz=IST)
    assert (trade.exit_reason, trade.exit_ts) == (
        "signal",
        pd.Timestamp("2026-10-19 12:15", tz=IST),
    )
    assert trade.avg_exit == pytest.approx(1141.8337979220355)  # the next bar's open
    assert trade.avg_exit > levels(f, trade)[-1] == 1136.416


def test_without_the_exit_on_a_turn_the_stop_alone_ends_the_trade():
    strat, f = fresh_turn(turn((120, -0.0003)), exit_on_flip=False)
    assert not f["long_exit"].any() and not f["short_exit"].any()
    (trade,) = trades(strat, f)
    assert (trade.exit_reason, trade.exit_ts) == ("trail", pd.Timestamp("2026-10-19 12:25", tz=IST))
    assert trade.avg_exit == pytest.approx(1136.4155088875355)


def test_a_buffered_level_may_step_back_but_the_stop_in_force_does_not():
    """The line never steps back in a trend, but the stop stands two ATR under it and the ATR
    grows in a slide: the level REPORTED falls from 1135.68 to 1134.87. The stop in force
    stays at the higher one."""
    strat, f = fresh_turn(turn((105, -0.00015)))  # slides from 11:00
    (trade,) = trades(strat, f)
    assert levels(f, trade) == [1130.412, 1135.683, 1134.865]
    assert (trade.exit_reason, trade.exit_ts) == ("trail", pd.Timestamp("2026-10-19 12:55", tz=IST))
    assert trade.avg_exit == pytest.approx(1135.6826856529217)


def test_a_level_under_the_first_stop_never_loosens_it():
    strat, f = fresh_turn(turn((75, -0.0002)))  # slides from 10:30
    (trade,) = trades(strat, f)
    assert levels(f, trade) == [1130.412, 1129.319]
    assert (trade.exit_reason, trade.exit_ts) == ("stop", pd.Timestamp("2026-10-19 12:05", tz=IST))
    assert trade.avg_exit == pytest.approx(trade.stop_initial) == pytest.approx(1130.4120419770875)
    assert trade.r_multiple == pytest.approx(-1.0)


def test_with_no_buffer_the_line_itself_is_the_stop_and_a_touch_exits(up):
    strat, f = signals(up, {"reenter_daily": True, "stop_buffer_atr": 0.0, "trail_tf": "5m"})
    i = bar(f, "2026-10-15 10:35")
    assert f["long_stop"].iloc[i] == f["mtf_line_5m"].iloc[i] == pytest.approx(1093.199, abs=1e-3)
    assert (
        "Stop and trail: the 5m Supertrend line 1093.20 (a touch exits)"
        in (strat.explain(f, i, "long")["reasons"])
    )
    first, second = trades(strat, f)
    for trade, exit_ts, price in (
        (first, "2026-10-15 13:45", 1102.1362257652709),
        (second, "2026-10-16 11:05", 1115.0459257537052),
    ):
        assert (trade.exit_reason, trade.exit_ts) == ("trail", pd.Timestamp(exit_ts, tz=IST))
        assert trade.avg_exit == pytest.approx(price)
        # the highest level the 5-minute line reached while the trade was open
        seen = f.loc[trade.signal_ts : trade.exit_ts, "long_trail"].iloc[:-1]
        assert trade.avg_exit == pytest.approx(seen.max())
        assert trade.avg_exit > trade.stop_initial


def test_a_slower_trail_timeframe_stands_further_from_the_price(up):
    stops = {}
    for tf in ("5m", "30m", "60m", "1d"):
        strat, f = signals(up, {"trail_tf": tf})
        assert strat.trail_timeframe == tf and entries(f) == ["10-15 10:35"]
        row = f.iloc[bar(f, "2026-10-15 10:35")]
        assert row["mtf_trail_line"] == row[f"mtf_line_{tf}"]
        assert row["long_stop"] == pytest.approx(row["mtf_trail_line"] - 2 * row["mtf_trail_atr"])
        stops[tf] = round(float(row["long_stop"]), 3)
    assert stops == {"5m": 1090.102, "30m": 1083.356, "60m": 1078.747, "1d": 1050.048}


# ── what the data cannot give ─────────────────────────────────────────────────


def test_without_daily_history_the_daily_rung_has_no_reading_and_nothing_is_taken(up):
    strat = get_strategy("supertrend_mtf")
    f = strat.generate(up.five, Context(symbol="X", extras={"minute_bars": up.minute}))
    assert (f["mtf_1d"] == 0).all()  # twelve derived days: seventy are needed
    assert f["mtf_up"].max() == 5
    assert not f["long_entry"].any() and not f["short_entry"].any()
    blockers = strat.explain(f, bar(f, "2026-10-15 10:35"), "long")["blockers"]
    assert blockers == ["Not all up yet: 1d no reading", NO_READING]


def test_without_fine_bars_the_strategys_own_bars_are_all_it_has(up):
    strat = get_strategy("supertrend_mtf")
    f = strat.generate(up.five)
    assert (f["mtf_1m"] == 0).all() and (f["mtf_1d"] == 0).all()
    assert set(f["mtf_5m"].unique()) == {-1, 0, 1}
    assert not f["long_entry"].any() and not f["short_entry"].any()
    assert f["long_stop"].notna().any()  # the columns are there, the gate is shut


def test_a_two_minute_feed_cannot_give_the_fast_timeframes():
    minute = make_intraday(days=12, seed=7, drift=0.00003, vol=0.0004, freq_min=2, start=FIRST_DAY)
    daily = consistent_daily(stored_daily(float(minute["open"].iloc[0]), 0.002), minute)
    half_hours = resample_bars(minute, "30m")
    ctx = Context(
        symbol="X", daily=daily, extras={"minute_bars": minute, "minute_resolution": "2m"}
    )
    strat = get_strategy("supertrend_mtf", {"timeframe": "30m"})
    f = strat.generate(half_hours, ctx)
    for tf in ("1m", "5m", "15m"):
        assert (f[f"mtf_{tf}"] == 0).all(), tf
    assert not f["long_entry"].any() and not f["short_entry"].any()
    i = bar(f, "2026-10-15 09:45")
    assert strat.explain(f, i, "long")["blockers"] == [
        "Not all up yet: 1m no reading, 5m no reading, 15m no reading",
        NO_READING,
    ]
    # the timeframes it can give are enough for a gate that asks only for them
    slower = get_strategy("supertrend_mtf", {"timeframe": "30m", "gate": "slower"})
    g = slower.generate(half_hours, ctx)
    assert entries(g) == ["10-15 09:45"]
    assert slower.explain(g, i, "long")["reasons"][:3] == [
        "Supertrend(14, 1.7): 1m no reading, 5m no reading, 15m no reading, 30m up, 60m up, 1d up",
        "All 3 timeframes that must agree point up",
        "The 30m trend turned up 3 bars of 30m ago",
    ]


# ── no look-ahead ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("day", [9, 11])
@pytest.mark.parametrize("params", [{}, FRESH_TURN], ids=["default", "fresh-turn"])
def test_a_decision_reads_nothing_that_came_after_it(up, day, params):
    """Cut the history at 11:30 of a later session, inside a 30-minute and a 60-minute bar
    and inside the day: every column up to there is what it was with the whole history."""
    cut = D * day + 135
    part = world_of(up.minute.iloc[:cut])
    _, whole = signals(up, params)
    _, short = signals(part, params)
    cols = [c for c in whole.columns if c.startswith("mtf_")] + SIGNAL_COLUMNS
    assert len(short) == cut // 5 and set(cols) <= set(short.columns)
    pd.testing.assert_frame_equal(whole.iloc[: len(short)][cols], short[cols])
