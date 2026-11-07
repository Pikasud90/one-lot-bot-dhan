"""Real option premiums for Lab trades (`onelot.lab.real_options`).

Two layers. The pure helpers (`raw_level`, `days_from_ratio`, `implied_days`, `atm_rel`,
`interpolate`, `premium_in_minute`) are pinned with hand-worked numbers and a few properties.
`RealPricer` is then run end to end on a tiny synthetic store, a ten-minute BANKNIFTY morning
built so every number can be worked by hand:

    entry   the option's one-minute OPEN at the entry minute (or the first trade within
            `max_wait_min`); exits at an open reason at the open, `eod` at the close of the
            strategy bar's last minute, level exits at the first touching minute, interpolated;
    rolling Dhan's strike is relative to each MINUTE's at-the-money strike, so one contract
            sits in a different `rel` series as the index moves;
    friction the model's half-spread and ticks, and the cost table, on those real premiums.

The expected fills are worked in exact rational arithmetic here, never by calling the code
under test. No network: the store is a SQLite file in tmp_path and the source a fake.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from fractions import Fraction
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from onelot.analytics.blackscholes import price as b76
from onelot.clock import IST
from onelot.config import BacktestCfg
from onelot.data.expired_options import (
    FIELDS,
    Block,
    OptionBarStore,
    RollingKey,
    Underlying,
    sanitize,
)
from onelot.lab.real_options import (
    OPEN_REASONS,
    NeedBlock,
    RealPricer,
    RealTrade,
    atm_rel,
    days_from_ratio,
    finite,
    implied_days,
    interpolate,
    premium_in_minute,
    raw_level,
)
from onelot.lab.simulator import Leg, SimTrade, simulate
from onelot.strategies.base import StrategyParams

# ══ raw_level ═════════════════════════════════════════════════════════════════════════════


class TestRawLevel:
    @pytest.mark.parametrize("reason", ["target", "partial"])
    @pytest.mark.parametrize("side", [1, -1])
    def test_targets_and_partials_are_the_level_itself(self, reason, side):
        assert raw_level(25012.35, reason, side, 0.03) == 25012.35

    @pytest.mark.parametrize("reason", ["stop", "trail", "breakeven", "signal", "time", "eod"])
    def test_everything_else_has_the_underlying_slippage_taken_back_out(self, reason):
        # a long exits 0.03 % below its level: 100 -> 99.97, and back
        assert raw_level(99.97, reason, 1, 0.03) == pytest.approx(100.0, rel=1e-12)
        # a short exits 0.03 % above: 100 -> 100.03, and back
        assert raw_level(100.03, reason, -1, 0.03) == pytest.approx(100.0, rel=1e-12)

    def test_the_formula_is_price_over_one_minus_side_times_slippage(self):
        assert raw_level(43936.815, "stop", 1, 0.03) == pytest.approx(43936.815 / 0.9997)
        assert raw_level(43963.185, "stop", -1, 0.03) == pytest.approx(43963.185 / 1.0003)

    @pytest.mark.parametrize("reason", ["stop", "target", "eod"])
    def test_zero_slippage_changes_nothing(self, reason):
        assert raw_level(123.45, reason, 1, 0.0) == 123.45 == raw_level(123.45, reason, -1, 0.0)

    @settings(derandomize=True, max_examples=200, deadline=None)
    @given(
        level=st.floats(1.0, 1e6),
        slip=st.floats(0.0, 2.0),
        side=st.sampled_from([1, -1]),
        reason=st.sampled_from(["stop", "trail", "breakeven", "signal", "time", "eod"]),
    )
    def test_it_inverts_the_simulators_exit_fill(self, level, slip, side, reason):
        # simulate() fills an exit at level x (1 - slip x side); raw_level must undo exactly that
        sim_price = level * (1 - side * slip / 100.0)
        assert raw_level(sim_price, reason, side, slip) == pytest.approx(level, rel=1e-9)

    def test_it_undoes_what_the_real_simulator_does(self):
        """End to end: a long's stop exit from `simulate` maps back to the stop level."""
        idx = pd.date_range("2026-09-01 09:15", periods=8, freq="5min", tz=IST)
        rows = [[100, 100.1, 99.9, 100]] * 3 + [
            [100, 100.4, 99.8, 100.2],
            [100.2, 100.3, 98.0, 98.5],
        ]
        rows += [[98.5, 98.6, 98.4, 98.5]] * 3
        f = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx, dtype=float)
        f["volume"], f["atr"], f["atr_trail"], f["d_atr"] = 1000.0, 1.0, 1.0, np.nan
        for col in ("long_entry", "short_entry", "long_exit", "short_exit"):
            f[col] = False
        f["long_stop"] = f["short_stop"] = np.nan
        f["long_score"] = f["short_score"] = 70.0
        f.loc[idx[2], ["long_entry", "long_stop"]] = [True, 99.0]
        (trade,) = simulate(f, StrategyParams(**P_SIM), "X", "s", slippage_pct=0.05)
        leg = trade.legs[-1]
        assert leg.reason == "stop" and leg.price == pytest.approx(99.0 * (1 - 0.0005))
        assert raw_level(leg.price, leg.reason, trade.side, 0.05) == pytest.approx(99.0)


P_SIM = {
    "stop_atr_min": 0.1, "stop_atr_max": 10, "min_risk_pct": 0.0, "target_r": 5.0,
    "trail_atr_mult": 0.0, "cooldown_bars": 0, "max_trades_per_day": 5,
    "entry_start": "09:15", "entry_end": "15:00",
}  # fmt: skip


# ══ days_from_ratio ═══════════════════════════════════════════════════════════════════════


class TestDaysFromRatio:
    @pytest.mark.parametrize(
        ("ratio", "gap", "days"),
        [(0.5, 30, 10.0), (0.25, 30, 2.0), (0.5, 28, 28 / 3), (0.6, 30, 0.36 * 30 / 0.64)],
    )
    def test_worked_values(self, ratio, gap, days):
        assert days_from_ratio(ratio, gap) == pytest.approx(days, rel=1e-12)

    def test_the_default_gap_is_thirty_days(self):
        assert days_from_ratio(0.5) == days_from_ratio(0.5, 30) == pytest.approx(10.0)

    @pytest.mark.parametrize("ratio", [0.0, -0.1, -5.0, 1.0, 1.5, float("nan"), float("inf")])
    def test_a_ratio_that_cannot_come_from_a_near_over_a_next_contract_is_none(self, ratio):
        assert days_from_ratio(ratio) is None

    @settings(derandomize=True, max_examples=200, deadline=None)
    @given(near=st.floats(0.01, 400.0), gap=st.floats(20.0, 40.0))
    def test_it_inverts_square_root_of_time_value(self, near, gap):
        """Time value ~ sqrt(T): near = T, next = T + gap  ->  R = sqrt(T / (T + gap))."""
        ratio = math.sqrt(near / (near + gap))
        assert days_from_ratio(ratio, gap) == pytest.approx(near, rel=1e-6)

    @settings(derandomize=True, max_examples=100, deadline=None)
    @given(a=st.floats(0.01, 0.98), b=st.floats(0.01, 0.98))
    def test_more_near_time_value_means_more_days(self, a, b):
        lo, hi = sorted((a, b))
        d_lo, d_hi = days_from_ratio(lo), days_from_ratio(hi)
        assert d_lo is not None and d_hi is not None and d_lo <= d_hi


# ══ implied_days ══════════════════════════════════════════════════════════════════════════


class TestImpliedDays:
    @pytest.mark.parametrize("opt", ["CE", "PE"])
    @pytest.mark.parametrize("days", [0.5, 2.0, 10.0, 30.0, 90.0])
    @pytest.mark.parametrize("sigmas", [-1.0, 0.0, 1.0])
    def test_it_recovers_the_time_that_priced_the_bar(self, opt, days, sigmas):
        """Strikes within a standard deviation of the money, where the premium carries time
        value (a deep in-the-money premium is intrinsic: it cannot say how long is left)."""
        spot, iv = 44000.0, 14.0
        strike = round(spot * math.exp(sigmas * iv / 100 * math.sqrt(days / 365.0)), 2)
        premium = b76(spot, strike, days / 365.0, iv / 100.0, 0.065, opt)
        got = implied_days(premium, spot, strike, iv, opt)
        assert got == pytest.approx(days, rel=1e-3, abs=1e-3)

    def test_the_rate_is_a_parameter(self):
        premium = b76(44000.0, 44000.0, 20 / 365.0, 0.15, 0.0, "CE")
        assert implied_days(premium, 44000.0, 44000.0, 15.0, "CE", r=0.0) == pytest.approx(
            20.0, rel=1e-3
        )
        assert implied_days(premium, 44000.0, 44000.0, 15.0, "CE", r=0.10) != pytest.approx(
            20.0, rel=1e-3
        )

    @pytest.mark.parametrize(
        ("premium", "spot", "strike", "iv"),
        [
            (0.0, 44000.0, 44000.0, 14.0),
            (-5.0, 44000.0, 44000.0, 14.0),
            (300.0, 0.0, 44000.0, 14.0),
            (300.0, 44000.0, 0.0, 14.0),
            (300.0, 44000.0, 44000.0, 0.0),
            (300.0, 44000.0, 44000.0, -3.0),
            (300.0, 44000.0, 44000.0, None),
        ],
    )
    def test_a_bar_that_cannot_say_gives_none(self, premium, spot, strike, iv):
        assert implied_days(premium, spot, strike, iv, "CE") is None

    def test_no_time_value_left_is_zero_days(self):
        assert implied_days(1000.0, 45000.0, 44000.0, 14.0, "CE") == 0.0  # at intrinsic
        assert implied_days(900.0, 45000.0, 44000.0, 14.0, "CE") == 0.0  # under intrinsic
        assert implied_days(1000.0, 43000.0, 44000.0, 14.0, "PE") == 0.0

    def test_a_premium_no_time_up_to_120_days_can_explain_is_none(self):
        beyond = b76(44000.0, 44000.0, 120 / 365.0, 0.14, 0.065, "CE") * 1.05
        assert implied_days(beyond, 44000.0, 44000.0, 14.0, "CE") is None


# ══ atm_rel ═══════════════════════════════════════════════════════════════════════════════


class TestAtmRel:
    @pytest.mark.parametrize(
        ("strike", "spot", "step", "rel"),
        [
            (44000.0, 44005.0, 100.0, 0),
            (44300.0, 44123.0, 100.0, 2),
            (43900.0, 44123.0, 100.0, -2),
            (44100.0, 44051.0, 100.0, 0),  # nearest strike to 44,051 is 44,100
            (44000.0, 44049.0, 100.0, 0),
            (25100.0, 25012.0, 50.0, 2),
            (24850.0, 25012.0, 50.0, -3),
            (25000.0, 25012.0, 25.0, 0),
            (25025.0, 25012.0, 25.0, 1),
        ],
    )
    def test_how_many_steps_a_strike_is_from_the_one_nearest_the_spot(
        self, strike, spot, step, rel
    ):
        assert atm_rel(strike, spot, step) == rel

    def test_it_returns_a_plain_int(self):
        assert type(atm_rel(44300.0, 44123.0, 100.0)) is int

    @settings(derandomize=True, max_examples=200, deadline=None)
    @given(
        spot=st.floats(5000.0, 90000.0),
        step=st.sampled_from([25.0, 50.0, 100.0]),
        k=st.integers(-10, 10),
    )
    def test_a_strike_k_steps_above_the_atm_is_rel_k(self, spot, step, k):
        atm = round(spot / step) * step
        assert atm_rel(atm + k * step, spot, step) == k


# ══ interpolate ═══════════════════════════════════════════════════════════════════════════

UND_BAR = (100.0, 110.0, 90.0, 105.0)  # open, high, low, close of the underlying minute
OPT_BAR = (50.0, 60.0, 40.0, 55.0)  # the call's (or put's) own minute


class TestInterpolate:
    @pytest.mark.parametrize("opt", ["CE", "PE"])
    def test_at_the_underlyings_open_the_option_is_at_its_open(self, opt):
        assert interpolate(OPT_BAR, UND_BAR, 100.0, opt) == 50.0

    def test_a_call_at_the_underlyings_high_is_at_its_high_and_at_the_low_its_low(self):
        assert interpolate(OPT_BAR, UND_BAR, 110.0, "CE") == 60.0
        assert interpolate(OPT_BAR, UND_BAR, 90.0, "CE") == 40.0

    def test_a_put_moves_against_the_underlying(self):
        assert interpolate(OPT_BAR, UND_BAR, 110.0, "PE") == 40.0  # underlying up: put at its low
        assert interpolate(OPT_BAR, UND_BAR, 90.0, "PE") == 60.0  # underlying down: put at high

    @pytest.mark.parametrize(
        ("level", "opt", "price"),
        [
            (105.0, "CE", 55.0),  # half way up: half way to the call's high
            (95.0, "CE", 45.0),  # half way down: half way to the call's low
            (102.0, "CE", 52.0),  # 20 % of the way up
            (93.0, "CE", 50.0 + 0.7 * (40.0 - 50.0)),  # 70 % of the way down
            (105.0, "PE", 45.0),
            (95.0, "PE", 55.0),
        ],
    )
    def test_it_is_proportional_inside_the_minute(self, level, opt, price):
        assert interpolate(OPT_BAR, UND_BAR, level, opt) == pytest.approx(price)

    @pytest.mark.parametrize("opt", ["CE", "PE"])
    def test_a_level_beyond_the_underlyings_range_stops_at_the_options_extreme(self, opt):
        beyond_up = interpolate(OPT_BAR, UND_BAR, 150.0, opt)
        beyond_down = interpolate(OPT_BAR, UND_BAR, 50.0, opt)
        assert beyond_up == (60.0 if opt == "CE" else 40.0)
        assert beyond_down == (40.0 if opt == "CE" else 60.0)

    @pytest.mark.parametrize("level", [99.0, 100.0, 101.0, 80.0, 120.0])
    def test_a_flat_underlying_minute_gives_the_options_open(self, level):
        flat = (100.0, 100.0, 100.0, 100.0)
        assert interpolate(OPT_BAR, flat, level, "CE") == 50.0

    def test_an_underlying_that_only_went_one_way_still_prices_the_other_side_at_the_open(self):
        rising = (100.0, 110.0, 100.0, 110.0)  # never below its open
        assert interpolate(OPT_BAR, rising, 95.0, "CE") == 50.0  # span 0 below: the open

    def test_the_answer_never_leaves_the_options_own_range(self):
        broken_open = (130.0, 125.0, 120.0, 122.0)  # an open above its own high
        assert interpolate(broken_open, UND_BAR, 100.0, "CE") == 125.0
        broken_low = (10.0, 60.0, 40.0, 55.0)  # an open below its own low
        assert interpolate(broken_low, UND_BAR, 100.0, "CE") == 40.0

    @settings(derandomize=True, max_examples=300, deadline=None)
    @given(
        o=st.floats(5, 500),
        up=st.floats(0, 50),
        down=st.floats(0, 50),
        c_frac=st.floats(0, 1),
        u_o=st.floats(1000, 90000),
        u_up=st.floats(0, 300),
        u_down=st.floats(0, 300),
        level_frac=st.floats(-0.2, 1.2),
        opt=st.sampled_from(["CE", "PE"]),
    )
    def test_always_inside_the_options_range_and_monotone_in_the_level(
        self, o, up, down, c_frac, u_o, u_up, u_down, level_frac, opt
    ):
        high, low = o + up, max(o - down, 0.01)
        close = low + c_frac * (high - low)
        opt_bar = (o, high, low, close)
        und_bar = (u_o, u_o + u_up, u_o - u_down, u_o)
        lo_lvl, hi_lvl = u_o - u_down, u_o + u_up
        level = lo_lvl + level_frac * (hi_lvl - lo_lvl)
        px = interpolate(opt_bar, und_bar, level, opt)
        assert low - 1e-9 <= px <= high + 1e-9
        assert interpolate(opt_bar, und_bar, u_o, opt) == pytest.approx(o)
        # a call never gets cheaper as the underlying level rises, a put never dearer
        step = (hi_lvl - lo_lvl) / 4 or 1.0
        a = interpolate(opt_bar, und_bar, level, opt)
        b = interpolate(opt_bar, und_bar, level + step, opt)
        assert (b >= a - 1e-9) if opt == "CE" else (b <= a + 1e-9)


# ══ premium_in_minute ═════════════════════════════════════════════════════════════════════

DAY = dt.date(2025, 3, 4)  # a Tuesday, long past: every block is complete whatever "today" is


def T(hhmm: str, sec: int = 0, day: dt.date = DAY) -> pd.Timestamp:
    h, m = (int(x) for x in hhmm.split(":"))
    return pd.Timestamp(dt.datetime(day.year, day.month, day.day, h, m, sec, tzinfo=IST))


def EP(hhmm: str, day: dt.date = DAY) -> int:  # noqa: N802 - the epoch second of a minute
    return int(T(hhmm, day=day).timestamp())


def frame(rows: dict[str, tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        list(rows.values()),
        index=pd.DatetimeIndex([T(k) for k in rows], name="ts"),
        columns=["open", "high", "low", "close"],
    )


MIN_UND = frame({"10:06": (100.0, 110.0, 90.0, 105.0), "10:07": (105.0, 112.0, 103.0, 108.0)})
MIN_OPT = frame({"10:06": (50.0, 60.0, 40.0, 55.0), "10:07": (55.0, 63.0, 52.0, 58.0)})


class TestPremiumInMinute:
    def test_a_fill_at_a_minutes_open_is_the_options_open(self):
        assert premium_in_minute(MIN_OPT, MIN_UND, 100.0, T("10:06"), "CE") == 50.0
        assert premium_in_minute(MIN_OPT, MIN_UND, 105.0, T("10:07"), "CE") == 55.0

    def test_a_mark_at_a_close_is_the_options_close_of_the_minute_that_just_ended(self):
        # 10:07:00 is the close time of the 10:06 minute: the level is its close, 105
        only_prior = MIN_UND.iloc[:1], MIN_OPT.iloc[:1]
        assert premium_in_minute(only_prior[1], only_prior[0], 105.0, T("10:07"), "CE") == 55.0

    def test_a_close_inside_the_minute_that_starts_at_ts_is_also_exact(self):
        assert premium_in_minute(MIN_OPT, MIN_UND, 108.0, T("10:07", 30), "CE") == 58.0

    def test_mid_minute_inside_the_range_is_interpolated(self):
        # 10:06:30, the underlying at 95 = half way down: half way to the call's low 40
        assert premium_in_minute(MIN_OPT, MIN_UND, 95.0, T("10:06", 30), "CE") == pytest.approx(
            45.0
        )
        assert premium_in_minute(MIN_OPT, MIN_UND, 95.0, T("10:06", 30), "PE") == pytest.approx(
            55.0
        )

    def test_on_a_minute_boundary_the_minute_starting_there_is_tried_before_the_one_ending(self):
        # 10:07:00, level 110: inside 10:07's range (103-112), not the exact open or close of
        # either; the minute starting at ts decides, by interpolation inside ITS bars
        got = premium_in_minute(MIN_OPT, MIN_UND, 110.0, T("10:07"), "CE")
        assert got == pytest.approx(
            interpolate((55, 63, 52, 58), (105, 112, 103, 108), 110.0, "CE")
        )

    def test_when_the_starting_minute_does_not_reach_the_level_the_ending_minute_is_used(self):
        # 10:07:00, level 92: below 10:07's low (103), inside 10:06's range (90-110)
        got = premium_in_minute(MIN_OPT, MIN_UND, 92.0, T("10:07"), "CE")
        assert got == pytest.approx(interpolate((50, 60, 40, 55), (100, 110, 90, 105), 92.0, "CE"))

    @pytest.mark.parametrize("level", [89.0, 113.0, 0.0, 1e9])
    def test_a_level_the_underlying_never_reached_is_none(self, level):
        assert premium_in_minute(MIN_OPT, MIN_UND, level, T("10:07"), "CE") is None

    def test_a_minute_without_an_option_bar_is_none_never_invented(self):
        opt_gap = MIN_OPT.iloc[1:]  # no 10:06 option bar
        assert premium_in_minute(opt_gap, MIN_UND, 100.0, T("10:06", 30), "CE") is None

    def test_a_minute_without_an_underlying_bar_is_none(self):
        und_gap = MIN_UND.iloc[1:]
        assert premium_in_minute(MIN_OPT, und_gap, 100.0, T("10:06", 30), "CE") is None

    @pytest.mark.parametrize("level", [float("nan"), float("inf"), float("-inf")])
    def test_a_level_that_is_not_a_number_is_none_never_a_price(self, level):
        assert premium_in_minute(MIN_OPT, MIN_UND, level, T("10:06"), "CE") is None
        assert premium_in_minute(MIN_OPT, MIN_UND, level, T("10:06", 30), "PE") is None

    def test_nan_in_a_bar_never_leaks_into_a_price(self):
        broken = MIN_UND.copy()
        broken.loc[T("10:06"), ["high", "low"]] = float("nan")
        got = premium_in_minute(MIN_OPT, broken, 95.0, T("10:06", 30), "CE")
        assert got is None

    def test_empty_frames_give_none(self):
        empty = MIN_OPT.iloc[:0]
        assert premium_in_minute(empty, MIN_UND, 100.0, T("10:06"), "CE") is None
        assert premium_in_minute(MIN_OPT, empty, 100.0, T("10:06"), "CE") is None

    def test_a_level_within_the_tolerance_of_the_open_counts_as_the_open(self):
        assert premium_in_minute(MIN_OPT, MIN_UND, 100.005, T("10:06"), "CE") == 50.0
        assert premium_in_minute(MIN_OPT, MIN_UND, 99.995, T("10:06"), "CE") == 50.0
        beyond = premium_in_minute(MIN_OPT, MIN_UND, 100.007, T("10:06"), "CE")
        assert beyond is not None and beyond != 50.0 and beyond == pytest.approx(50.0, abs=0.01)

    @pytest.mark.parametrize(
        "ts",
        [
            pd.Timestamp("2025-03-04 10:06:30"),  # naive: read as IST
            dt.datetime(2025, 3, 4, 10, 6, 30),
            pd.Timestamp("2025-03-04 04:36:30", tz="UTC"),  # the same instant in UTC
            dt.datetime(2025, 3, 4, 4, 36, 30, tzinfo=dt.timezone.utc),
        ],
        ids=["naive-ts", "naive-datetime", "utc-ts", "utc-datetime"],
    )
    def test_any_flavour_of_timestamp_means_the_same_instant(self, ts):
        assert premium_in_minute(MIN_OPT, MIN_UND, 95.0, ts, "CE") == pytest.approx(45.0)

    @pytest.mark.parametrize("unit", ["s", "ms", "us", "ns"])
    def test_frames_in_any_datetime_resolution_are_matched(self, unit):
        und, opt = MIN_UND.copy(), MIN_OPT.copy()
        und.index, opt.index = und.index.as_unit(unit), opt.index.as_unit(unit)
        assert premium_in_minute(opt, und, 100.0, T("10:06"), "CE") == 50.0


# ══ finite ════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, True), (0.0, True), (-3.5, True), (float("nan"), False), (float("inf"), False),
     (float("-inf"), False), (None, False), ("1.0", False), ([1.0], False)],
)  # fmt: skip
def test_finite_accepts_only_real_finite_numbers(value, expected):
    assert finite(value) is expected


# ══ the pricer on a synthetic morning ═════════════════════════════════════════════════════

LOT, TICK = 30, 0.05
# real_ret_pct is worked from the net already rounded to the paisa, so it can sit a little away
# from a return worked from the unrounded net: 1e-4 for a premium of hundreds, 0.1 % of itself for
# one of a few paise (a notional of Rs 6). Immaterial, but not equal.
RET_TOL = 1.5e-4
RET_REL = 2e-3
UND_ROWS = {  # BANKNIFTY one-minute bars (open, high, low, close)
    "10:00": (44000, 44010, 43990, 44005),
    "10:01": (44005, 44020, 44000, 44015),
    "10:02": (44015, 44030, 44010, 44025),
    "10:03": (44025, 44040, 44020, 44030),
    "10:04": (44030, 44035, 44010, 44020),
    "10:05": (44020, 44025, 43990, 43995),
    "10:06": (43995, 44000, 43940, 43945),
    "10:07": (43945, 43960, 43930, 43950),
    "10:08": (43950, 43965, 43940, 43955),
    "10:09": (43955, 43970, 43945, 43960),
}
CLOSE = {k: float(v[3]) for k, v in UND_ROWS.items()}
# The at-the-money strike of each minute (the strike nearest its spot), as Dhan stamps it:
# 44,000 all morning except 10:06, when the index dips under 43,950 and 43,900 is the nearest.
ATM_OF = {k: float(round(c / 100) * 100) for k, c in CLOSE.items()}
assert ATM_OF["10:06"] == 43900.0 and {v for k, v in ATM_OF.items() if k != "10:06"} == {44000.0}

# Near-month ATM call (rel 0): open, high, low, close, strike. 10:00-10:03 carry exactly 300
# of time value over intrinsic (the days-left estimate reads these); 10:06 is the 43,900 call.
NEAR_ATM = {
    "10:00": (305.0, 308.0, 303.0, 305.0, 44000.0),
    "10:01": (305.0, 318.0, 304.0, 315.0, 44000.0),
    "10:02": (315.0, 328.0, 314.0, 325.0, 44000.0),
    "10:03": (325.0, 333.0, 324.0, 330.0, 44000.0),
    "10:04": (330.0, 332.0, 318.0, 320.0, 44000.0),
    "10:05": (320.0, 321.0, 293.0, 295.0, 44000.0),
    "10:06": (187.5, 188.5, 151.5, 155.0, 43900.0),
    "10:07": (300.0, 303.0, 298.0, 301.0, 44000.0),
    "10:08": (301.0, 304.0, 299.0, 302.0, 44000.0),
    "10:09": (302.0, 305.0, 300.0, 303.0, 44000.0),
}
# Near-month call struck 43,900 while ATM is 44,000 (rel -1): the contract the trades hold.
# 10:06 holds a different strike (43,800: ATM was 43,900 then), a trap for a time-only lookup.
NEAR_M1 = {
    "10:00": (180.0, 183.5, 178.5, 182.0, 43900.0),
    "10:01": (182.0, 189.5, 181.5, 188.5, 43900.0),
    "10:02": (188.5, 195.0, 188.0, 194.5, 43900.0),
    "10:03": (194.5, 197.5, 194.0, 196.5, 43900.0),
    "10:04": (196.5, 197.5, 190.0, 192.0, 43900.0),
    "10:05": (192.0, 193.0, 186.0, 187.5, 43900.0),
    "10:06": (260.0, 262.0, 255.0, 258.0, 43800.0),
    "10:07": (155.0, 158.0, 153.0, 156.0, 43900.0),
    "10:08": (156.0, 159.0, 155.0, 158.0, 43900.0),
    "10:09": (158.0, 160.0, 156.0, 157.0, 43900.0),
}
# The next month's contract (code 2), same strike 43,900, rel -1, a different price level.
NEXT_M1 = {
    "10:00": (210.0, 213.0, 208.0, 212.0, 43900.0),
    "10:05": (222.0, 224.0, 219.0, 220.0, 43900.0),
}
UNDERLYING = Underlying("BANKNIFTY", "25", "NSE_FNO", 100.0)
BT = BacktestCfg()  # half-spread 0.6 % of the premium, 2 ticks, roll inside 3 days


def key(code: int, opt: str, rel: int) -> RollingKey:
    return RollingKey("BANKNIFTY", code, opt, rel)


def make_block(
    k: RollingKey, rows: dict[str, tuple], day: dt.date = DAY, shift_s: int = 0
) -> Block:
    """A stored day block from {minute: (open, high, low, close, strike)}; spot = the index close.
    `shift_s` moves every stamp (the spot is still that of the minute the row is named for)."""
    arr: dict[str, np.ndarray] = {
        "ts": np.array([EP(m, day) + shift_s for m in rows], dtype="int64")
    }
    vals = list(rows.values())
    for j, name in enumerate(["open", "high", "low", "close", "strike"]):
        arr[name] = np.array([v[j] for v in vals], dtype=float)
    arr["spot"] = np.array([CLOSE[m] for m in rows], dtype=float)
    arr["iv"] = np.full(len(rows), 12.0)
    arr["volume"] = np.full(len(rows), 1000.0)
    arr["oi"] = np.full(len(rows), 5000.0)
    return Block(k, day, True, sanitize(arr, day, day))


def next_atm(ratio: float) -> dict[str, tuple]:
    """The next month's ATM call over 10:00-10:03 such that near/next time value = `ratio`."""
    out = {}
    for m in ("10:00", "10:01", "10:02", "10:03"):
        intrinsic = max(CLOSE[m] - ATM_OF[m], 0.0)
        tv_near = NEAR_ATM[m][3] - intrinsic
        close = tv_near / ratio + intrinsic
        out[m] = (close, close + 3, close - 3, close, ATM_OF[m])
    return out


def fill_store(store: OptionBarStore, ratio: float = 0.5, *, with_code2: bool = False) -> None:
    store.put(make_block(key(1, "CE", 0), NEAR_ATM))
    store.put(make_block(key(2, "CE", 0), next_atm(ratio)))
    store.put(make_block(key(1, "CE", -1), NEAR_M1))
    store.put(make_block(key(1, "CE", -2), {}))  # the neighbour a lookup may try
    if with_code2:
        store.put(make_block(key(2, "CE", -1), NEXT_M1))
        store.put(make_block(key(2, "CE", -2), {}))


class FlatCosts:
    """A cost table of 0.05 % of turnover per leg; remembers every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, int, str]] = []

    def leg(self, premium: float, qty: int, side: str):
        self.calls.append((premium, qty, side))
        return SimpleNamespace(total=premium * qty * 0.0005)


@pytest.fixture
def store(tmp_path):
    s = OptionBarStore(tmp_path / "real.db")
    yield s
    s.close()


@pytest.fixture
def und_1m() -> pd.DataFrame:
    return frame({k: tuple(map(float, v)) for k, v in UND_ROWS.items()})


def make_pricer(store, und_1m, tf=5, costs=None, **kw) -> RealPricer:
    spec = SimpleNamespace(lot_size=LOT, tick_size=TICK)
    return RealPricer(UNDERLYING, spec, BT, costs or FlatCosts(), store, und_1m, tf, **kw)


def sim_trade(legs, side=1, entry="10:00", entry_price=44000.0) -> SimTrade:
    return SimTrade(
        "BANKNIFTY", "supertrend_vol", side, T("09:55"), T(entry), entry_price,
        entry_price - 100.0 * side, 100.0, entry_price + 200.0 * side, 70.0, 80.0, legs=list(legs),
    )  # fmt: skip


def model_row(strike: float = 43900.0) -> dict:
    return {"detail": {"strike": strike}, "ret_pct": 1.5, "net_pnl": 400.0}


def leg(at: str, price: float, reason: str, frac: float = 1.0) -> Leg:
    return Leg(T(at), price, frac, reason)


# ── exact oracles (rational arithmetic) ───────────────────────────────────────────────────
def fr(x) -> Fraction:
    return Fraction(str(x)) if not isinstance(x, Fraction) else x


def ceil_tick(x: Fraction) -> Fraction:
    t = fr(TICK)
    return Fraction(math.ceil(x / t)) * t


def floor_tick(x: Fraction) -> Fraction:
    t = fr(TICK)
    return Fraction(math.floor(x / t)) * t


def buy_fill(premium) -> Fraction:
    """ceil_tick(p x (1 + 0.6 %) + 2 ticks)."""
    return ceil_tick(fr(premium) * fr("1.006") + 2 * fr(TICK))


def sell_fill(premium) -> Fraction:
    """floor_tick(p x (1 - 0.6 %) - 2 ticks), never under one tick."""
    return max(floor_tick(fr(premium) * fr("0.994") - 2 * fr(TICK)), fr(TICK))


def flat_cost(premium: Fraction, qty: int) -> Fraction:
    return premium * qty * fr("0.0005")


def expected_book(entry_px, exits: list[tuple[Fraction, float]]) -> dict:
    """gross / net / return of buying at `entry_px` and selling the (price, fraction) legs."""
    e = buy_fill(entry_px)
    gross, cost = Fraction(0), flat_cost(e, LOT)
    for px, frac in exits:
        fill = sell_fill(px)
        q = LOT * fr(frac)
        gross += (fill - e) * q
        cost += flat_cost(fill, int(round(q))) * (q / int(round(q)))
    net = gross - cost
    return {
        "gross": round(float(gross), 2),
        "net": round(float(net), 2),
        "ret": round(float(net / (e * LOT) * 100), 4),
    }


class TestEntryAndOpenExits:
    def test_a_signal_exit_is_the_option_open_of_the_strategy_bars_first_minute(
        self, store, und_1m
    ):
        fill_store(store)
        costs = FlatCosts()
        rt = make_pricer(store, und_1m, costs=costs).price(
            sim_trade([leg("10:05", 44000.0, "signal")]), model_row()
        )
        assert rt.unpriced is None and rt.code == 1 and rt.days_to_expiry == pytest.approx(10.0)
        assert (rt.real_entry, rt.real_exit) == (180.0, 192.0)  # opens at 10:00 and 10:05
        assert rt.entry_wait_min == 0 and rt.exit_wait_min == 0
        assert rt.rels == [-1, -1]  # the 43,900 call sat in the ATM-1 series both times
        want = expected_book(180.0, [(fr(192.0), 1.0)])
        assert (rt.real_gross, rt.real_net) == (want["gross"], want["net"])
        assert rt.real_ret_pct == pytest.approx(want["ret"], abs=RET_TOL, rel=RET_REL)
        assert (want["gross"], want["net"]) == (285.0, 279.42)  # 190.70 sold over 181.20 bought
        assert costs.calls == [(181.2, 30, "buy"), (190.7, 30, "sell")]

    @pytest.mark.parametrize("reason", sorted(OPEN_REASONS))
    def test_every_open_reason_is_priced_at_the_open_not_at_a_level(self, store, und_1m, reason):
        fill_store(store)
        # the price on the leg is irrelevant to an open exit: it must not be searched for
        rt = make_pricer(store, und_1m).price(
            sim_trade([leg("10:05", 12345.6, reason)]), model_row()
        )
        assert rt.unpriced is None and rt.real_exit == 192.0

    def test_the_exit_reason_the_simulator_gives_at_exit_time_is_an_open_reason(self):
        """The session-end exit's reason string comes from `simulate` itself (never typed here)."""
        idx = pd.date_range("2026-09-01 14:15", periods=14, freq="5min", tz=IST)
        f = pd.DataFrame(
            [[100.0, 100.1, 99.9, 100.0]] * 14, columns=["open", "high", "low", "close"], index=idx
        )
        f["volume"], f["atr"], f["atr_trail"], f["d_atr"] = 1000.0, 1.0, 1.0, np.nan
        for col in ("long_entry", "short_entry", "long_exit", "short_exit"):
            f[col] = False
        f["long_stop"] = f["short_stop"] = np.nan
        f["long_score"] = f["short_score"] = 70.0
        i = list(idx.strftime("%H:%M")).index("15:05")
        f.loc[idx[i], ["long_entry", "long_stop"]] = [True, 99.0]
        p = StrategyParams(**{**P_SIM, "entry_end": "15:10"})
        (trade,) = simulate(f, p, "X", "s", slippage_pct=0.0)
        assert trade.exit_ts.strftime("%H:%M") == "15:15"
        assert trade.exit_reason in OPEN_REASONS

    def test_a_stop_in_the_same_strategy_bar_is_priced_in_the_minute_that_touched_it(
        self, store, und_1m
    ):
        fill_store(store)
        rt = make_pricer(store, und_1m).price(
            sim_trade([leg("10:05", 43950.0, "stop")]), model_row()
        )
        # the 10:05 minute only fell to 43,990; 10:06 reached 43,940. There the index went from
        # its open 43,995 to 43,950: 45 of the 55 points down to its low, so the 43,900 call
        # (now the ATM call, rel 0) went 45/55 of the way from its open 187.5 to its low 151.5
        px = Fraction(1875, 10) + Fraction(45, 55) * (Fraction(1515, 10) - Fraction(1875, 10))
        assert rt.rels == [-1, 0]  # the exit minute used the ATM series, the entry the ATM-1
        assert rt.real_exit == round(float(px), 2) == 158.05
        want = expected_book(180.0, [(px, 1.0)])
        assert (rt.real_gross, rt.real_net) == (want["gross"], want["net"])

    def test_a_gap_exit_fills_at_the_open(self, store, und_1m):
        """A stop the index gapped through is booked at the bar's open: the open of that minute."""
        fill_store(store)
        gap_price = (
            44020.0  # the 10:05 bar opened at 44,020 (the simulator fills a gap at the open)
        )
        rt = make_pricer(store, und_1m).price(
            sim_trade([leg("10:05", gap_price, "stop")]), model_row()
        )
        assert rt.real_exit == 192.0  # the 10:05 open, not an interpolation

    def test_eod_is_the_close_of_the_strategy_bars_last_minute(self, store, und_1m):
        fill_store(store)
        rt = make_pricer(store, und_1m).price(
            sim_trade([leg("10:05", 43960.0, "eod")]), model_row()
        )
        assert rt.real_exit == 157.0  # the 10:09 option close (10:05 + 5 minutes - 1)
        assert rt.exit_wait_min == 0

    def test_eod_of_a_one_minute_strategy_is_that_minutes_close(self, store, und_1m):
        fill_store(store)
        rt = make_pricer(store, und_1m, tf=1).price(
            sim_trade([leg("10:07", 43950.0, "eod")]), model_row()
        )
        assert rt.real_exit == 156.0  # the 10:07 close

    def test_a_target_is_the_level_itself_a_stop_has_the_slippage_taken_out_first(
        self, store, und_1m
    ):
        """The simulator fills a stop at level x (1 - 0.03 %) but a target at the level. Taking
        the slippage out of the stop recovers 43,950 (the 10:06 touch, 158.05); a target at
        43,936.8 is NOT adjusted and is touched later (10:07, 153.91)."""
        fill_store(store)
        slipped = 43950.0 * (1 - 0.0003)
        as_stop = make_pricer(store, und_1m, slip_pct=0.03).price(
            sim_trade([leg("10:05", slipped, "stop")]), model_row()
        )
        as_target = make_pricer(store, und_1m, slip_pct=0.03).price(
            sim_trade([leg("10:05", slipped, "target")]), model_row()
        )
        assert as_stop.real_exit == 158.05 and as_target.real_exit == 153.91
        no_slip = make_pricer(store, und_1m, slip_pct=0.0).price(
            sim_trade([leg("10:05", slipped, "stop")]), model_row()
        )
        assert no_slip.real_exit == as_target.real_exit  # without slip_pct nothing is undone

    @pytest.mark.parametrize("reason", ["stop", "trail", "breakeven"])
    def test_every_stop_like_reason_is_a_level_exit(self, store, und_1m, reason):
        fill_store(store)
        rt = make_pricer(store, und_1m).price(
            sim_trade([leg("10:05", 43950.0, reason)]), model_row()
        )
        assert rt.real_exit == 158.05

    def test_a_short_underlying_buys_a_put_and_a_stop_above_is_interpolated_downwards(
        self, store, und_1m
    ):
        """side -1: BUY PE. The short's stop sits ABOVE: 44,022 is reached in the 10:05 minute
        (open 44,020, high 44,025): 2 of the 5 points up, so the put goes 40 % of the way from
        its open to its low. The 0.03 % slippage a short pays (stop x 1.0003) comes out first."""
        fill_store(store)
        pe_entry = {"10:00": (150.0, 152.0, 148.0, 151.0, 44100.0)}
        pe_exit = {"10:05": (130.0, 131.0, 126.0, 128.0, 44100.0)}
        store.put(make_block(key(1, "PE", 1), {**pe_entry, **pe_exit}))
        store.put(make_block(key(1, "PE", 0), {}))
        store.put(make_block(key(1, "PE", 2), {}))
        stop_fill = 44022.0 * (1 + 0.0003)
        rt = make_pricer(store, und_1m, slip_pct=0.03).price(
            sim_trade([leg("10:05", stop_fill, "stop")], side=-1), model_row(44100.0)
        )
        assert rt.opt == "PE" and rt.side == -1 and rt.unpriced is None
        assert rt.real_entry == 150.0
        assert rt.real_exit == round(130.0 + 0.4 * (126.0 - 130.0), 2) == 128.4
        assert rt.rels == [1, 1]

    def test_two_legs_are_booked_with_their_fractions(self, store, und_1m):
        fill_store(store)
        legs = [leg("10:05", 0.0, "signal", 0.5), leg("10:07", 0.0, "signal", 0.5)]
        rt = make_pricer(store, und_1m, tf=1).price(sim_trade(legs), model_row())
        assert rt.real_exit == round((192.0 + 155.0) / 2, 2) == 173.5
        want = expected_book(180.0, [(fr(192.0), 0.5), (fr(155.0), 0.5)])
        assert (rt.real_gross, rt.real_net) == (want["gross"], want["net"])
        assert rt.real_ret_pct == pytest.approx(want["ret"], abs=RET_TOL, rel=RET_REL)

    def test_the_cost_table_is_asked_for_each_leg_on_its_real_side_and_size(self, store, und_1m):
        fill_store(store)
        costs = FlatCosts()
        legs = [leg("10:05", 0.0, "signal", 0.5), leg("10:07", 0.0, "signal", 0.5)]
        make_pricer(store, und_1m, tf=1, costs=costs).price(sim_trade(legs), model_row())
        sides = [(qty, side) for _p, qty, side in costs.calls]
        assert sides == [(30, "buy"), (15, "sell"), (15, "sell")]


class TestWaitingForATrade:
    def test_no_trade_in_the_entry_minute_waits_for_the_next_trade_and_uses_its_open(
        self, store, und_1m
    ):
        rows = {m: v for m, v in NEAR_M1.items() if m != "10:00"}  # nothing traded at 10:00
        fill_store(store)
        store.put(make_block(key(1, "CE", -1), rows))
        rt = make_pricer(store, und_1m).price(sim_trade([leg("10:05", 0.0, "signal")]), model_row())
        assert rt.unpriced is None and rt.entry_wait_min == 1 and rt.real_entry == 182.0

    @pytest.mark.parametrize(("first_trade", "wait"), [("10:02", 2), ("10:03", 3)])
    def test_the_wait_is_up_to_three_minutes_by_default(self, store, und_1m, first_trade, wait):
        fill_store(store)
        rows = {m: v for m, v in NEAR_M1.items() if m >= first_trade}
        store.put(make_block(key(1, "CE", -1), rows))
        rt = make_pricer(store, und_1m).price(sim_trade([leg("10:05", 0.0, "signal")]), model_row())
        assert rt.unpriced is None and rt.entry_wait_min == wait
        assert rt.real_entry == NEAR_M1[first_trade][0]

    def test_a_fourth_minute_without_a_trade_leaves_it_unpriced_with_the_reason(
        self, store, und_1m
    ):
        fill_store(store)
        store.put(make_block(key(1, "CE", -1), {m: v for m, v in NEAR_M1.items() if m >= "10:04"}))
        rt = make_pricer(store, und_1m).price(sim_trade([leg("10:05", 0.0, "signal")]), model_row())
        assert rt.unpriced == "no CE 43900 trade within 3 min of entry"
        assert rt.real_entry is None and rt.real_net is None and rt.real_ret_pct is None
        assert rt.real_gross is None and rt.real_exit is None

    def test_max_wait_is_a_parameter(self, store, und_1m):
        fill_store(store)
        store.put(make_block(key(1, "CE", -1), {m: v for m, v in NEAR_M1.items() if m >= "10:04"}))
        rt = make_pricer(store, und_1m, max_wait_min=5).price(
            sim_trade([leg("10:05", 0.0, "signal")]), model_row()
        )
        assert rt.unpriced is None and rt.entry_wait_min == 4 and rt.real_entry == 196.5
        none = make_pricer(store, und_1m, max_wait_min=0).price(
            sim_trade([leg("10:05", 0.0, "signal")]), model_row()
        )
        assert none.unpriced == "no CE 43900 trade within 0 min of entry"

    def test_an_exit_minute_without_a_trade_waits_and_uses_the_next_trades_open(
        self, store, und_1m
    ):
        fill_store(store)
        rows = {m: v for m, v in NEAR_M1.items() if m != "10:05"}
        store.put(make_block(key(1, "CE", -1), rows))
        rt = make_pricer(store, und_1m).price(sim_trade([leg("10:05", 0.0, "signal")]), model_row())
        # 10:05 has no bar in rel -1. At 10:06 the ATM moved to 43,900, so the contract traded
        # in the ATM series (open 187.5): the lookup follows each minute's own ATM (BUG-1), and
        # the first trade after the exit minute is that one, not 10:07's in the old series.
        assert rt.unpriced is None and rt.exit_wait_min == 1 and rt.real_exit == 187.5
        assert rt.rels == [-1, 0]

    def test_an_exit_with_no_trade_in_reach_is_unpriced_and_the_reason_names_the_exit(
        self, store, und_1m
    ):
        fill_store(store)
        rows = {m: v for m, v in NEAR_M1.items() if m < "10:08"}  # nothing at 10:08 or 10:09
        store.put(make_block(key(1, "CE", -1), rows))
        rt = make_pricer(store, und_1m, max_wait_min=1).price(
            sim_trade([leg("10:08", 0.0, "signal")]), model_row()
        )
        assert rt.unpriced == "no CE 43900 trade within 1 min of the signal exit at 10:08"
        assert rt.real_entry is None and rt.real_net is None  # a half-priced trade is no trade
        assert rt.rels == [-1]

    def test_a_neighbouring_series_that_traded_the_strike_in_reach_prices_the_exit(
        self, store, und_1m
    ):
        """No bar for 43,900 in the ATM-1 series from 10:05, but at 10:06 the ATM moved to
        43,900 and the contract trades in the ATM series: one minute's wait finds it there."""
        fill_store(store)
        rows = {m: v for m, v in NEAR_M1.items() if m < "10:05"}
        store.put(make_block(key(1, "CE", -1), rows))
        rt = make_pricer(store, und_1m, max_wait_min=1).price(
            sim_trade([leg("10:05", 0.0, "signal")]), model_row()
        )
        assert rt.unpriced is None and rt.exit_wait_min == 1 and rt.real_exit == 187.5

    def test_a_contract_that_moved_to_another_series_is_found_there_not_in_its_old_one(
        self, store, und_1m
    ):
        """At 10:06 the rel -1 series holds strike 43,800 at 258 (the ATM moved to 43,900): pricing
        the 43,900 call from it would be a 100-point error. It sits in the ATM series now."""
        fill_store(store)
        rt = make_pricer(store, und_1m, tf=1).price(
            sim_trade([leg("10:06", 0.0, "signal")]), model_row()
        )
        assert rt.real_exit == 187.5  # the 43,900 call in the ATM series (rel 0) at 10:06
        assert rt.rels == [-1, 0]


class TestAtmRolling:
    def test_the_contract_is_looked_up_in_the_series_the_minutes_spot_implies(self, store, und_1m):
        fill_store(store)
        # at 10:00 the index closes 44,005 (ATM 44,000): 43,900 is rel -1; at 10:06 (43,945) the
        # ATM is 43,900 itself: rel 0. Same contract, two series.
        rt = make_pricer(store, und_1m, tf=1).price(
            sim_trade([leg("10:06", 0.0, "signal")]), model_row()
        )
        assert rt.rels == [-1, 0] and rt.real_entry == 180.0 and rt.real_exit == 187.5

    def test_the_neighbouring_series_is_tried_when_dhans_atm_rounds_the_other_way(self, store):
        """At exactly 44,050 the nearest strike is a tie: Python rounds it to 44,000 (rel -1 for
        43,900), Dhan may take 44,100 (rel -2). The pricer tries the neighbours."""
        tie = {k: tuple(map(float, v)) for k, v in UND_ROWS.items()}
        tie["10:00"] = (44000.0, 44060.0, 43990.0, 44050.0)
        und = frame(tie)
        fill_store(store)
        wrong_atm = {"10:00": (170.0, 171.0, 169.0, 170.0, 44000.0)}  # Dhan's rel -1 = 44,000
        right_atm = {"10:00": (225.0, 226.0, 224.0, 225.0, 43900.0)}  # Dhan's rel -2 = 43,900
        store.put(make_block(key(1, "CE", -1), {**NEAR_M1, **wrong_atm}))
        store.put(make_block(key(1, "CE", -2), right_atm))
        rt = make_pricer(store, und).price(sim_trade([leg("10:05", 0.0, "signal")]), model_row())
        assert rt.unpriced is None
        # the exact-minute bar of the neighbouring series wins over a LATER bar of the first one
        assert (rt.entry_wait_min, rt.rels[0], rt.real_entry) == (0, -2, 225.0)

    def test_the_series_is_followed_as_the_atm_moves_during_a_wait(self, store):
        """No trade at 10:00 or 10:01. By 10:02 the index has run 150 points and the at-the-money
        strike has moved two steps, so the 43,900 call now sits in the ATM-3 series. A wait that
        keeps looking where the ENTRY minute pointed (ATM-1, and its neighbours ATM-2 and ATM)
        never sees it, and a fast market (exactly when the strike moves) loses the trade."""
        moving = {k: tuple(map(float, v)) for k, v in UND_ROWS.items()}
        moving["10:01"] = (44005.0, 44095.0, 44000.0, 44090.0)  # ATM 44,100
        moving["10:02"] = (44090.0, 44165.0, 44085.0, 44160.0)  # ATM 44,200
        und = frame(moving)
        fill_store(store)
        store.put(make_block(key(1, "CE", -1), {}))
        store.put(make_block(key(1, "CE", -2), {}))
        store.put(make_block(key(1, "CE", -3), {"10:02": (205.0, 207.0, 203.0, 206.0, 43900.0)}))
        rt = make_pricer(store, und).price(sim_trade([leg("10:05", 0.0, "signal")]), model_row())
        assert rt.unpriced is None
        assert (rt.entry_wait_min, rt.rels[0], rt.real_entry) == (2, -3, 205.0)

    def test_a_strike_beyond_ten_steps_from_the_money_is_unpriced_never_guessed(
        self, store, und_1m
    ):
        fill_store(store)  # no block exists beyond ATM+-10: none may even be asked for
        rt = make_pricer(store, und_1m).price(
            sim_trade([leg("10:05", 0.0, "signal")]), model_row(45500.0)
        )
        assert rt.unpriced is not None and rt.real_entry is None and rt.real_net is None


class TestWhichContract:
    def test_ample_time_value_on_the_near_contract_keeps_it(self, store, und_1m):
        fill_store(store, ratio=0.5)
        rt = make_pricer(store, und_1m).price(sim_trade([leg("10:05", 0, "signal")]), model_row())
        assert rt.code == 1 and rt.days_to_expiry == pytest.approx(10.0)
        assert rt.real_entry == 180.0

    def test_a_near_contract_two_days_from_expiry_rolls_to_the_next_month(self, store, und_1m):
        fill_store(store, ratio=0.25, with_code2=True)  # R = 0.25  ->  2.0 days left
        rt = make_pricer(store, und_1m).price(sim_trade([leg("10:05", 0, "signal")]), model_row())
        assert rt.days_to_expiry == pytest.approx(2.0)
        assert rt.code == 2  # the model's roll: never the contract in its last 3 days
        assert (rt.real_entry, rt.real_exit) == (210.0, 222.0)  # the NEXT month's bars
        assert rt.unpriced is None

    def test_the_exits_stay_on_the_contract_the_entry_chose(self, store, und_1m):
        fill_store(store, ratio=0.25, with_code2=True)
        rt = make_pricer(store, und_1m).price(sim_trade([leg("10:05", 0, "signal")]), model_row())
        assert rt.code == 2 and rt.real_exit == 222.0 != 192.0

    def test_no_time_value_at_all_is_zero_days_and_rolls(self, store, und_1m):
        fill_store(store, with_code2=True)
        # near close = intrinsic exactly: the contract is worth nothing but its intrinsic
        dead = {}
        for m in ("10:00", "10:01", "10:02", "10:03"):
            intrinsic = max(CLOSE[m] - ATM_OF[m], 0.0)
            dead[m] = (intrinsic + 0.05, intrinsic + 2, max(intrinsic, 0.05), intrinsic, ATM_OF[m])
        near = {**NEAR_ATM, **dead}
        store.put(make_block(key(1, "CE", 0), near))
        rt = make_pricer(store, und_1m).price(sim_trade([leg("10:05", 0, "signal")]), model_row())
        assert rt.days_to_expiry == 0.0 and rt.code == 2

    def test_an_estimate_the_data_cannot_make_leaves_the_near_contract(self, store, und_1m):
        fill_store(store)
        store.put(make_block(key(2, "CE", 0), {}))  # the next month never traded that morning
        rt = make_pricer(store, und_1m).price(sim_trade([leg("10:05", 0, "signal")]), model_row())
        assert rt.days_to_expiry is None and rt.code == 1 and rt.unpriced is None

    def test_a_next_contract_with_less_time_value_than_the_near_one_is_no_estimate(
        self, store, und_1m
    ):
        fill_store(store, ratio=1.25)  # nonsense: near dearer than next
        rt = make_pricer(store, und_1m).price(sim_trade([leg("10:05", 0, "signal")]), model_row())
        assert rt.days_to_expiry is None and rt.code == 1

    @pytest.mark.parametrize(
        ("shift_s", "found"),
        [(1500, True), (-1500, True), (1800, True), (1860, False), (2400, False), (-2400, False)],
    )
    def test_only_minutes_within_half_an_hour_of_the_entry_count(
        self, store, und_1m, shift_s, found
    ):
        """An entry at 10:00 reads the near and next ATM calls from 09:30 to 10:30 inclusive."""
        fill_store(store)
        near = {m: NEAR_ATM[m] for m in ("10:00", "10:01", "10:02", "10:03")}
        store.put(make_block(key(1, "CE", 0), near, shift_s=shift_s))
        store.put(make_block(key(2, "CE", 0), next_atm(0.5), shift_s=shift_s))
        rt = make_pricer(store, und_1m).price(sim_trade([leg("10:05", 0, "signal")]), model_row())
        assert (rt.days_to_expiry is not None) is found
        assert rt.unpriced is None

    def test_a_next_month_strike_that_differs_from_the_near_months_is_not_compared(
        self, store, und_1m
    ):
        fill_store(store)
        skewed = {m: (*v[:4], v[4] + 100.0) for m, v in next_atm(0.25).items()}
        store.put(make_block(key(2, "CE", 0), skewed))
        rt = make_pricer(store, und_1m).price(sim_trade([leg("10:05", 0, "signal")]), model_row())
        assert rt.days_to_expiry is None

    def test_the_estimate_is_the_median_so_one_bad_minute_does_not_move_it(self, store, und_1m):
        fill_store(store)
        rows = next_atm(0.5)
        m, v = "10:01", rows["10:01"]
        rows[m] = (v[0] * 50, v[1] * 50, v[2] * 50, v[3] * 50, v[4])  # one absurd next price
        store.put(make_block(key(2, "CE", 0), rows))
        rt = make_pricer(store, und_1m).price(sim_trade([leg("10:05", 0, "signal")]), model_row())
        assert rt.days_to_expiry == pytest.approx(10.0)

    def test_a_trade_the_same_half_hour_reuses_the_estimate(self, store, und_1m):
        fill_store(store)
        p = make_pricer(store, und_1m)
        first = p.price(sim_trade([leg("10:05", 0, "signal")]), model_row())
        second = p.price(sim_trade([leg("10:05", 0, "signal")], entry="10:01"), model_row())
        assert first.days_to_expiry == second.days_to_expiry == pytest.approx(10.0)


class TestFetchingBlocks:
    def test_without_a_source_a_missing_block_raises_and_names_it(self, store, und_1m):
        p = make_pricer(store, und_1m)
        with pytest.raises(NeedBlock) as ei:
            p.price(sim_trade([leg("10:05", 0, "signal")]), model_row())
        assert ei.value.key == key(1, "CE", 0) and ei.value.day == DAY
        assert str(DAY) in str(ei.value)

    def test_the_block_it_names_is_the_next_one_it_would_need(self, store, und_1m):
        p = make_pricer(store, und_1m)
        seen = []
        for _ in range(8):
            try:
                p.price(sim_trade([leg("10:05", 0, "signal")]), model_row())
                break
            except NeedBlock as need:
                seen.append(need.key)
                store.put(make_block(need.key, {}))  # the caller fetches it (here: nothing)
        assert seen[:2] == [key(1, "CE", 0), key(2, "CE", 0)]
        assert len(seen) == len(set(seen))  # each missing block is asked for once

    def test_with_a_source_the_blocks_are_fetched_stored_and_counted(self, store, und_1m):
        src = FakeSource()
        p = make_pricer(store, und_1m, source=src)
        rt = p.price(sim_trade([leg("10:05", 0, "signal")]), model_row())
        assert rt.unpriced is None and rt.real_entry == 180.0 and rt.real_exit == 192.0
        assert p.requests == 3 and len(src.calls) == 3
        assert {(c[2], c[3], c[4]) for c in src.calls} == {
            (1, "ATM", "CALL"), (2, "ATM", "CALL"), (1, "ATM-1", "CALL"),
        }  # fmt: skip
        assert all(c[:2] == ("25", "NSE_FNO") and c[5] == DAY for c in src.calls)
        assert store.has(key(1, "CE", -1), DAY)

    def test_a_second_trade_the_same_day_asks_for_nothing_more(self, store, und_1m):
        src = FakeSource()
        p = make_pricer(store, und_1m, source=src)
        p.price(sim_trade([leg("10:05", 0, "signal")]), model_row())
        asked = p.requests
        p.price(sim_trade([leg("10:05", 0, "signal")], entry="10:01"), model_row())
        assert p.requests == asked

    def test_a_source_that_fails_raises_it_is_never_priced_from_nothing(self, store, und_1m):
        class Down(FakeSource):
            def rolling_option(self, *a, **k):
                raise RuntimeError("DH-904 rate limit")

        p = make_pricer(store, und_1m, source=Down())
        with pytest.raises(RuntimeError, match="DH-904"):
            p.price(sim_trade([leg("10:05", 0, "signal")]), model_row())


# Dhan-format answers for FakeSource, from the same tables the store tests use ──────────────
SERIES = {  # (expiry code, strike argument, CALL|PUT) -> {minute: (o, h, l, c, strike)}
    (1, "ATM", "CALL"): NEAR_ATM,
    (2, "ATM", "CALL"): next_atm(0.5),
    (1, "ATM-1", "CALL"): NEAR_M1,
}


class FakeSource:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def rolling_option(
        self, underlying_security_id, fno_segment, expiry_code, strike, option_type, start, end,
        expiry_flag="MONTH", interval_min=1,
    ) -> dict:  # fmt: skip
        self.calls.append(
            (underlying_security_id, fno_segment, expiry_code, strike, option_type, start, end)
        )
        rows = SERIES.get((expiry_code, strike, option_type))
        if rows is None or start != DAY:
            return {}
        cols = {n: [] for n in FIELDS}
        stamps = []
        for m, (o, h, lo, c, k) in rows.items():
            stamps.append(EP(m))
            for n, v in zip(
                ("open", "high", "low", "close", "strike"), (o, h, lo, c, k), strict=True
            ):
                cols[n].append(v)
            cols["spot"].append(CLOSE[m])
            cols["iv"].append(12.0)
            cols["volume"].append(1000.0)
            cols["oi"].append(5000.0)
        return {"timestamp": stamps, **cols}


class TestFrictionMatchesTheModel:
    """`_book` claims to be `execution.execute_trade`'s friction on real premiums. Held to it:
    the same raw premiums through both must give the same rupees to the paisa."""

    @staticmethod
    def model_row_for(entry_px: float, legs: list[Leg], premiums: list[float]) -> dict:
        from onelot.data.universe import InstrumentSpec
        from onelot.lab.execution import execute_trade

        spec = InstrumentSpec(
            "BANKNIFTY_OPT", "BANKNIFTY options", "OPTIONS", "BANKNIFTY", LOT, TICK,
            "index_options", 100.0,
        )  # fmt: skip
        table = {(T("10:00"), 44000.0): entry_px}
        for lg, px in zip(legs, premiums, strict=True):
            table[(pd.Timestamp(lg.ts), float(lg.price))] = px

        class Scripted:
            def iv_for(self, ts):
                return 0.15

            def premium(self, spot, k, ts, expiry, opt):
                return table.get((pd.Timestamp(ts), float(spot)), 1.0)

            def delta(self, spot, k, ts, expiry, opt):
                return 0.65

        trade = sim_trade(legs)
        return execute_trade(trade, spec, FlatCosts(), BT, 0.03, Scripted())  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("entry", "exits"),
        [
            (180.0, [(192.0, 1.0)]),
            (180.0, [(155.0, 1.0)]),
            (180.0, [(192.0, 0.5), (155.0, 0.5)]),
            (180.0, [(192.0, 0.3), (155.0, 0.7)]),
            (181.2, [(0.05, 1.0)]),  # a worthless exit floors at one tick
            (0.05, [(0.05, 1.0)]),  # a worthless entry
            (312.34, [(318.77, 0.4), (301.55, 0.6)]),
            (999.95, [(1500.0, 1.0)]),
        ],
    )
    def test_the_same_raw_premiums_give_the_same_rupees_as_execute_trade(
        self, tmp_path, und_1m, entry, exits
    ):
        store = OptionBarStore(tmp_path / "fx.db")
        try:
            minutes = ["10:05", "10:07"][: len(exits)]
            rows = {"10:00": (entry, entry + 1, max(entry - 1, 0.05), entry, 43900.0)}
            for m, (px, _f) in zip(minutes, exits, strict=True):
                rows[m] = (px, px + 1, max(px - 1, 0.05), px, 43900.0)
            fill_store(store)
            store.put(make_block(key(1, "CE", -1), rows))
            legs = [
                leg(m, 0.0 + i, "signal", f)
                for i, (m, (_, f)) in enumerate(zip(minutes, exits, strict=True))
            ]
            rt = make_pricer(store, und_1m, tf=1).price(sim_trade(legs), model_row())
            assert rt.unpriced is None
            want = self.model_row_for(entry, legs, [px for px, _ in exits])
            assert rt.real_gross == want["gross_pnl"]
            assert rt.real_net == want["net_pnl"]
            assert rt.real_ret_pct == pytest.approx(want["ret_pct"], abs=RET_TOL, rel=RET_REL)
        finally:
            store.close()


class TestNoLookAhead:
    """What the pricer says about a trade must not change when data AFTER the trade changes."""

    def price(self, tmp_path, name, mutate=None):
        store = OptionBarStore(tmp_path / f"{name}.db")
        und = frame({k: tuple(map(float, v)) for k, v in UND_ROWS.items()})
        fill_store(store)
        if mutate is not None:
            und = mutate(store, und)
        try:
            return make_pricer(store, und).price(
                sim_trade([leg("10:05", 0.0, "signal")]), model_row()
            )
        finally:
            store.close()

    def test_bars_after_the_exit_never_matter(self, tmp_path):
        base = self.price(tmp_path, "a")

        def wreck(store, und):
            later = dict.fromkeys(
                ("10:07", "10:08", "10:09"), (999.0, 1000.0, 998.0, 999.0, 43900.0)
            )
            rows = {m: v for m, v in NEAR_M1.items() if m <= "10:06"} | later
            store.put(make_block(key(1, "CE", -1), rows))
            store.put(
                make_block(key(1, "CE", 0), {m: v for m, v in NEAR_ATM.items() if m <= "10:06"})
            )
            return und[und.index <= T("10:06")]  # and the underlying stops at 10:06

        truncated = self.price(tmp_path, "b", wreck)
        for field in ("real_entry", "real_exit", "real_gross", "real_net", "real_ret_pct", "code"):
            assert getattr(truncated, field) == getattr(base, field), field

    def test_bars_after_the_entry_wait_do_not_move_the_entry(self, tmp_path):
        base = self.price(tmp_path, "c")

        def alter(store, und):
            rows = dict(NEAR_M1)
            rows["10:01"] = (500.0, 501.0, 499.0, 500.0, 43900.0)
            rows["10:02"] = (600.0, 601.0, 599.0, 600.0, 43900.0)
            store.put(make_block(key(1, "CE", -1), rows))
            return und

        assert self.price(tmp_path, "d", alter).real_entry == base.real_entry == 180.0


class TestSerialisation:
    def test_a_priced_trade_serialises_to_plain_json(self, store, und_1m):
        fill_store(store)
        rt = make_pricer(store, und_1m).price(
            sim_trade([leg("10:05", 43950.0, "stop")]), model_row()
        )
        d = json.loads(json.dumps(rt.as_dict()))
        assert d["entry_ts"] == "2025-03-04T10:00:00+05:30"
        assert d["unpriced"] is None and d["real_net"] == rt.real_net and d["rels"] == [-1, 0]
        assert d["model_ret_pct"] == 1.5 and d["model_net"] == 400.0

    def test_an_unpriced_trade_serialises_with_its_reason_and_empty_numbers(self, store, und_1m):
        fill_store(store)
        store.put(make_block(key(1, "CE", -1), {}))
        rt = make_pricer(store, und_1m).price(sim_trade([leg("10:05", 0.0, "signal")]), model_row())
        d = json.loads(json.dumps(rt.as_dict()))
        assert d["unpriced"].startswith("no CE 43900 trade")
        assert d["real_entry"] is None and d["real_net"] is None

    def test_the_model_numbers_ride_along_untouched(self, store, und_1m):
        fill_store(store)
        row = {"detail": {"strike": 43900.0}, "ret_pct": -2.25, "net_pnl": -812.5}
        rt = make_pricer(store, und_1m).price(sim_trade([leg("10:05", 0, "signal")]), row)
        assert (rt.model_ret_pct, rt.model_net) == (-2.25, -812.5)
        missing = make_pricer(store, und_1m).price(
            sim_trade([leg("10:05", 0, "signal")]), {"detail": {"strike": 43900.0}}
        )
        assert (missing.model_ret_pct, missing.model_net) == (0.0, 0.0)

    def test_the_trade_is_described_by_its_own_side_strike_and_type(self, store, und_1m):
        fill_store(store)
        rt = make_pricer(store, und_1m).price(sim_trade([leg("10:05", 0, "signal")]), model_row())
        assert isinstance(rt, RealTrade)
        assert (rt.side, rt.strike, rt.opt) == (1, 43900.0, "CE")
        assert rt.entry_ts == T("10:00")


class TestIndexResolution:
    @pytest.mark.parametrize("unit", ["s", "ms", "us"])
    def test_a_one_minute_frame_in_a_coarser_datetime_resolution_is_priced_the_same(
        self, store, und_1m, unit
    ):
        """pandas 2 keeps whatever resolution an index was built in; the paper book reads its
        indexes through `as_unit('ns')` for that reason. The pricer must not silently mistake
        seconds or microseconds for nanoseconds (every trade would read 'unpriced')."""
        fill_store(store)
        coarse = und_1m.copy()
        coarse.index = coarse.index.as_unit(unit)
        rt = make_pricer(store, coarse).price(
            sim_trade([leg("10:05", 44000.0, "signal")]), model_row()
        )
        assert rt.unpriced is None, rt.unpriced
        assert (rt.real_entry, rt.real_exit) == (180.0, 192.0)
