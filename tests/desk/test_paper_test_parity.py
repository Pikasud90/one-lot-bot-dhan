"""The paper test's desk side: the Lab's own rules at the alert, the contract it names, and the
real option prices behind its paper book (owner's go-ahead, 2026-11-07).

    lab_entry_risk   the Lab's pending-entry risk rule (widen a tight stop, skip a wide one),
                     checked against `lab.simulator.simulate` itself on constructed frames
    pick_expiry      the monthly (or nearest) contract an index alert names
    tracker          fresh entries go through the risk rule, the strategy's own daily limit and
                     the Lab's cooldown
    desk             `_live_chain`, the contract ids on a chain alert, the slot counters that
                     survive a restart, `real_premium_at`, and the engine's late binding

Everything runs offline: synthetic bars, fake adapters, a recorder for a notifier.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from types import MethodType, SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from desk_harness import (
    FakeNotifier,
    new_desk,
    small_cfg,
    synthetic,
    yesterday_close,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from onelot.broker.base import Bar, ChainRow, ChainSnapshot, Contract
from onelot.clock import IST
from onelot.config import AppConfig
from onelot.data.schema import desk_signals
from onelot.desk.config import DeskInstrumentCfg, StrategySlotCfg, load_desk_config
from onelot.desk.rules import lab_entry_risk, pick_expiry
from onelot.desk.tracker import (
    InstrumentTracker,
    Verdict,
    _cooling_down,
    _strategy_limit,
)
from onelot.lab.simulator import simulate
from onelot.store import init_database
from onelot.strategies.base import StrategyParams

# ══ lab_entry_risk ════════════════════════════════════════════════════════════════════════


def params(**over) -> StrategyParams:
    base = {
        "stop_atr_min": 0.4, "stop_atr_max": 2.5, "min_risk_pct": 0.35, "max_risk_pct": 2.0,
        "stop_datr_min": 0.0,
    }  # fmt: skip
    return StrategyParams(**{**base, **over})


class TestLabEntryRiskWorkedExamples:
    """price 1000, ATR 10: floor = max(0.4 x 10, 0.35 % x 1000) = 4, cap = max(min(25, 20), 4) = 20"""

    def test_a_stop_inside_the_band_is_kept(self):
        assert lab_entry_risk("long", 1000.0, 995.0, 10.0, None, params()) == (995.0, None)

    def test_a_stop_tighter_than_the_floor_is_widened_to_the_floor(self):
        assert lab_entry_risk("long", 1000.0, 998.0, 10.0, None, params()) == (996.0, None)

    def test_a_short_widens_upwards(self):
        assert lab_entry_risk("short", 1000.0, 1002.0, 10.0, None, params()) == (1004.0, None)
        assert lab_entry_risk("short", 1000.0, 1005.0, 10.0, None, params()) == (1005.0, None)

    def test_a_stop_exactly_on_the_floor_and_exactly_on_the_cap_are_both_taken(self):
        assert lab_entry_risk("long", 1000.0, 996.0, 10.0, None, params()) == (996.0, None)
        assert lab_entry_risk("long", 1000.0, 980.0, 10.0, None, params()) == (980.0, None)

    def test_a_stop_wider_than_the_cap_is_skipped_with_the_reason(self):
        stop, why = lab_entry_risk("long", 1000.0, 970.0, 10.0, None, params())
        assert stop is None and why is not None
        assert "30.00" in why and "20.00" in why  # the risk and the cap, to the paisa
        assert "Lab" in why

    def test_the_cap_is_the_smaller_of_the_atr_and_the_percent_cap(self):
        # stop_atr_max x ATR = 25 but 2 % of the price = 20: the percent cap binds
        assert lab_entry_risk("long", 1000.0, 979.0, 10.0, None, params())[0] is None
        # with a 5 % cap the ATR cap (25) binds instead
        wide = params(max_risk_pct=5.0)
        assert lab_entry_risk("long", 1000.0, 976.0, 10.0, None, wide) == (976.0, None)
        assert lab_entry_risk("long", 1000.0, 974.0, 10.0, None, wide)[0] is None

    def test_the_prior_sessions_atr_can_set_the_floor(self):
        p = params(stop_datr_min=0.5)  # 0.5 x 20 = 10 beats 4
        assert lab_entry_risk("long", 1000.0, 995.0, 10.0, 20.0, p) == (990.0, None)

    def test_an_unusable_prior_session_atr_adds_no_floor(self):
        p = params(stop_datr_min=0.5)
        for d_atr in (None, 0.0, -3.0, float("nan"), float("inf")):
            assert lab_entry_risk("long", 1000.0, 995.0, 10.0, d_atr, p) == (995.0, None)

    def test_the_floor_can_exceed_the_cap_when_a_percent_floor_is_on(self):
        # min_risk_pct 3 % -> floor 30 > max_risk_pct 2 % -> 20; the cap lifts to the floor
        p = params(min_risk_pct=3.0)
        assert lab_entry_risk("long", 1000.0, 995.0, 10.0, None, p) == (970.0, None)

    def test_without_a_percent_floor_a_floor_above_the_cap_skips_the_trade(self):
        # floor 0.4 x 100 = 40 > cap min(250, 20) = 20, and min_risk_pct is 0: no lifting
        p = params(min_risk_pct=0.0)
        stop, _ = lab_entry_risk("long", 1000.0, 995.0, 100.0, None, p)
        assert stop is None

    def test_an_atr_of_zero_is_treated_like_the_simulator_treats_it(self):
        """simulate only refuses a NaN ATR: at 0 the 0.35 % floor still widens the stop and
        the cap falls back to that floor (tester observation, fixed for parity)."""
        assert lab_entry_risk("long", 1000.0, 997.0, 0.0, None, params()) == (996.5, None)
        stop, why = lab_entry_risk("long", 1000.0, 995.0, 0.0, None, params())
        assert stop is None and "cap" in str(why)

    @pytest.mark.parametrize("atr", [None, -1.0, float("nan"), float("inf")])
    def test_without_a_usable_atr_there_is_no_trade(self, atr):
        assert lab_entry_risk("long", 1000.0, 995.0, atr, None, params()) == (
            None,
            "no usable ATR or stop",
        )

    @pytest.mark.parametrize(("direction", "stop"), [("long", 1000.0), ("long", 1005.0),
                                                     ("short", 1000.0), ("short", 995.0)])  # fmt: skip
    def test_a_stop_on_the_wrong_side_or_at_the_price_is_no_trade(self, direction, stop):
        assert lab_entry_risk(direction, 1000.0, stop, 10.0, None, params()) == (
            None,
            "no usable ATR or stop",
        )

    @pytest.mark.parametrize("direction", ["up", "buy", "LONG", "", None])
    def test_an_unknown_direction_is_refused(self, direction):
        with pytest.raises(ValueError, match="direction"):
            lab_entry_risk(direction, 1000.0, 995.0, 10.0, None, params())  # type: ignore[arg-type]

    def test_the_stop_it_returns_is_a_float(self):
        stop, _ = lab_entry_risk("long", 1000, 995, 10, None, params())
        assert type(stop) is float


# The same rule, held against the Lab's own simulator ────────────────────────────────────────
def sim_frame(
    price: float, side: int, stop: float, atr: float, d_atr: float | None
) -> pd.DataFrame:
    """Six 5-minute bars: a signal on bar 2, the next bar opening AT the signal close (so the
    simulator's fill is exactly `price` at zero slippage)."""
    idx = pd.date_range("2026-09-01 10:00", periods=6, freq="5min", tz=IST)
    f = pd.DataFrame(
        {
            "open": price, "high": price * 1.0005, "low": price * 0.9995, "close": price,
            "volume": 1000.0, "atr": atr, "atr_trail": atr,
            "d_atr": np.nan if d_atr is None else d_atr,
        },
        index=idx,
    )  # fmt: skip
    for col in ("long_entry", "short_entry", "long_exit", "short_exit"):
        f[col] = False
    f["long_stop"] = f["short_stop"] = np.nan
    f["long_score"] = f["short_score"] = 70.0
    col = "long" if side == 1 else "short"
    f.loc[idx[2], [f"{col}_entry", f"{col}_stop"]] = [True, stop]
    return f


def lab_says(price, side, stop, atr, d_atr, p) -> tuple[float | None, float | None]:
    """(stop the Lab trades with, its risk per share) or (None, None) when it takes no trade."""
    trades = simulate(sim_frame(price, side, stop, atr, d_atr), p, "X", "s", slippage_pct=0.0)
    if not trades:
        return None, None
    assert len(trades) == 1
    return trades[0].stop_initial, trades[0].risk_ps


def desk_says(price, side, stop, atr, d_atr, p) -> tuple[float | None, float | None]:
    direction = "long" if side == 1 else "short"
    got, _why = lab_entry_risk(direction, price, stop, atr, d_atr, p)
    return (None, None) if got is None else (got, (price - got) * side)


class TestLabEntryRiskAgreesWithTheSimulator:
    @pytest.mark.parametrize("side", [1, -1])
    @pytest.mark.parametrize(
        ("dist", "label"),
        [(0.5, "tight"), (4.0, "on the floor"), (7.0, "inside"), (20.0, "on the cap"),
         (20.5, "just over"), (60.0, "far over")],
    )  # fmt: skip
    def test_each_regime_for_longs_and_shorts(self, side, dist, label):
        price, atr = 1000.0, 10.0
        stop = price - side * dist
        p = params()
        lab, mine = (
            lab_says(price, side, stop, atr, None, p),
            desk_says(price, side, stop, atr, None, p),
        )
        assert mine == pytest.approx(lab), label

    def test_with_the_prior_sessions_atr_floor(self):
        p = params(stop_datr_min=0.5)
        for d_atr in (None, 20.0, 50.0):
            lab = lab_says(1000.0, 1, 995.0, 10.0, d_atr, p)
            mine = desk_says(1000.0, 1, 995.0, 10.0, d_atr, p)
            assert mine == pytest.approx(lab), d_atr

    def test_a_missing_atr_is_no_trade_in_both(self):
        p = params()
        assert lab_says(1000.0, 1, 995.0, float("nan"), None, p) == (None, None)
        assert lab_entry_risk("long", 1000.0, 995.0, float("nan"), None, p)[0] is None

    @settings(derandomize=True, max_examples=250, deadline=None)
    @given(
        price=st.sampled_from([50.0, 100.0, 1000.0, 25000.0]),
        side=st.sampled_from([1, -1]),
        dist_pct=st.sampled_from([0.02, 0.05, 0.1, 0.2, 0.35, 0.5, 1.0, 2.0, 3.0, 5.0]),
        atr_pct=st.sampled_from([0.05, 0.1, 0.3, 0.5, 1.0, 2.0]),
        d_atr_pct=st.sampled_from([None, 0.0, 0.5, 1.5, 3.0]),
        stop_atr_min=st.sampled_from([0.0, 0.2, 0.4, 0.8]),
        stop_atr_max=st.sampled_from([1.0, 2.5, 4.0]),
        min_risk_pct=st.sampled_from([0.0, 0.1, 0.35, 1.0]),
        max_risk_pct=st.sampled_from([0.5, 2.0, 5.0]),
        stop_datr_min=st.sampled_from([0.0, 0.1, 0.3]),
    )
    def test_any_setup_is_taken_or_skipped_alike_with_the_same_stop(
        self, price, side, dist_pct, atr_pct, d_atr_pct, stop_atr_min, stop_atr_max,
        min_risk_pct, max_risk_pct, stop_datr_min,
    ):  # fmt: skip
        p = params(
            stop_atr_min=stop_atr_min, stop_atr_max=stop_atr_max, min_risk_pct=min_risk_pct,
            max_risk_pct=max_risk_pct, stop_datr_min=stop_datr_min,
        )  # fmt: skip
        stop = price - side * price * dist_pct / 100
        atr = price * atr_pct / 100
        d_atr = None if d_atr_pct is None else price * d_atr_pct / 100
        lab = lab_says(price, side, stop, atr, d_atr, p)
        mine = desk_says(price, side, stop, atr, d_atr, p)
        assert mine == pytest.approx(lab, rel=1e-12, abs=1e-9)


# ══ pick_expiry ═══════════════════════════════════════════════════════════════════════════

TODAY = dt.date(2026, 11, 8)  # a Sunday: what a weekend restart sees
LISTED = [
    "2026-11-10", "2026-11-17", "2026-11-24",  # November weeklies, the last the monthly
    "2026-12-01", "2026-12-08", "2026-12-29",  # December: weeklies, then the monthly
    "2027-03-30",  # a far quarterly
]  # fmt: skip


class TestPickExpiry:
    def test_monthly_is_the_last_listed_expiry_of_a_month_that_is_far_enough(self):
        assert pick_expiry(LISTED, TODAY, "monthly", 3) == "2026-11-24"

    @pytest.mark.parametrize(
        ("today", "want"),
        [
            (dt.date(2026, 11, 20), "2026-11-24"),
            (dt.date(2026, 11, 21), "2026-11-24"),  # exactly 3 days: allowed
            (dt.date(2026, 11, 22), "2026-12-29"),  # 2 days left: rolled to the next month
            (dt.date(2026, 11, 24), "2026-12-29"),  # the expiry day itself: rolled
            (dt.date(2026, 11, 25), "2026-12-29"),
            (dt.date(2026, 12, 25), "2026-12-29"),
            (dt.date(2026, 12, 26), "2026-12-29"),  # 12-29 is exactly 3 days away: allowed
            (dt.date(2026, 12, 27), "2027-03-30"),  # 2 days: rolled to the quarterly
        ],
    )
    def test_the_roll_window_is_inclusive_of_exactly_roll_days(self, today, want):
        assert pick_expiry(LISTED, today, "monthly", 3) == want

    @pytest.mark.parametrize(("roll", "want"), [(0, "2026-11-24"), (16, "2026-11-24"),
                                                (17, "2026-12-29"), (60, "2027-03-30")])  # fmt: skip
    def test_roll_days_is_a_parameter(self, roll, want):
        assert pick_expiry(LISTED, TODAY, "monthly", roll) == want

    def test_roll_days_zero_takes_the_contract_that_expires_today(self):
        assert pick_expiry(LISTED, dt.date(2026, 11, 24), "monthly", 0) == "2026-11-24"

    def test_nearest_is_the_first_expiry_from_today_on(self):
        assert pick_expiry(LISTED, TODAY, "nearest", 3) == "2026-11-10"
        assert pick_expiry(LISTED, dt.date(2026, 11, 10), "nearest", 3) == "2026-11-10"
        assert pick_expiry(LISTED, dt.date(2026, 11, 11), "nearest", 3) == "2026-11-17"

    def test_nearest_ignores_roll_days(self):
        assert pick_expiry(LISTED, dt.date(2026, 11, 9), "nearest", 30) == "2026-11-10"

    def test_the_monthly_is_what_the_broker_lists_last_in_that_month(self):
        # a list that stops inside December: its last listed expiry stands for the month
        short = ["2026-11-10", "2026-11-24", "2026-12-01", "2026-12-08"]
        assert pick_expiry(short, dt.date(2026, 11, 23), "monthly", 3) == "2026-12-08"

    @pytest.mark.parametrize(
        "junk",
        [["garbage", "", None, "2026-13-45", "26-11-24", "11/24/2026", 7, "next tuesday"]],
    )
    def test_unparseable_entries_are_ignored(self, junk):
        assert pick_expiry([*junk, "2026-11-24"], TODAY, "monthly", 3) == "2026-11-24"
        assert pick_expiry([*junk, "2026-11-24"], TODAY, "nearest", 3) == "2026-11-24"
        assert pick_expiry(junk, TODAY, "monthly", 3) is None

    def test_iso_timestamps_are_read_by_their_date(self):
        listed = ["2026-11-24T00:00:00", "2026-12-29T15:30:00+05:30"]
        assert pick_expiry(listed, TODAY, "monthly", 3) == "2026-11-24"

    def test_order_and_duplicates_do_not_matter(self):
        shuffled = list(reversed(LISTED)) + LISTED[:3]
        assert pick_expiry(shuffled, TODAY, "monthly", 3) == "2026-11-24"
        assert pick_expiry(shuffled, TODAY, "nearest", 3) == "2026-11-10"

    @pytest.mark.parametrize("mode", ["monthly", "nearest"])
    def test_nothing_usable_is_none(self, mode):
        assert pick_expiry([], TODAY, mode, 3) is None
        assert pick_expiry(["2026-01-29"], TODAY, mode, 3) is None  # all in the past
        assert pick_expiry(iter(()), TODAY, mode, 3) is None

    def test_monthly_with_only_a_contract_inside_the_roll_window_is_none(self):
        # 11-10 is 2 days from the 8th: inside the 3-day window, nothing else listed
        assert pick_expiry(["2026-11-10"], TODAY, "monthly", 3) is None
        assert pick_expiry(["2026-11-10"], dt.date(2026, 11, 7), "monthly", 3) == "2026-11-10"
        assert pick_expiry(["2026-11-10"], TODAY, "nearest", 3) == "2026-11-10"

    def test_a_weekly_before_a_monthly_is_never_the_monthly(self):
        # 11-17 is a weekly: the month's last listed expiry is 11-24
        assert pick_expiry(["2026-11-17", "2026-11-24"], dt.date(2026, 11, 14), "monthly", 3) == (
            "2026-11-24"
        )

    @pytest.mark.parametrize(
        ("today", "want"),
        [
            (dt.date(2026, 12, 20), "2026-12-29"),
            (dt.date(2026, 12, 26), "2026-12-29"),
            (dt.date(2026, 12, 27), "2027-01-26"),  # December's monthly is inside the window
            (dt.date(2026, 12, 31), "2027-01-26"),
            (dt.date(2027, 1, 1), "2027-01-26"),
            (dt.date(2027, 1, 24), "2027-02-23"),  # and January's, a month on
        ],
    )
    def test_the_roll_crosses_the_year_end(self, today, want):
        listed = ["2026-12-29", "2027-01-05", "2027-01-26", "2027-02-02", "2027-02-23"]
        assert pick_expiry(listed, today, "monthly", 3) == want

    def test_it_accepts_any_iterable(self):
        assert pick_expiry(tuple(LISTED), TODAY, "monthly", 3) == "2026-11-24"
        assert pick_expiry((e for e in LISTED), TODAY, "monthly", 3) == "2026-11-24"

    def test_it_returns_an_iso_string(self):
        got = pick_expiry(LISTED, TODAY, "monthly", 3)
        assert isinstance(got, str) and dt.date.fromisoformat(got)

    def test_the_lab_picks_the_same_month_when_both_contracts_are_tuesdays(self):
        """Parity with `execution.monthly_expiry`: the last Tuesday of the month that is at
        least `min_days` away. Fed the month's real Tuesdays, `pick_expiry` agrees."""
        from onelot.lab.execution import monthly_expiry

        tuesdays = []
        d = dt.date(2026, 10, 1)
        while d < dt.date(2027, 3, 1):
            if d.weekday() == 1:
                tuesdays.append(d.isoformat())
            d += dt.timedelta(days=1)
        day = dt.date(2026, 10, 1)
        while day < dt.date(2027, 1, 15):
            lab = monthly_expiry(day, 1, 3)
            assert pick_expiry(tuesdays, day, "monthly", 3) == lab.isoformat(), day
            day += dt.timedelta(days=1)


# ══ DeskConfig ════════════════════════════════════════════════════════════════════════════


def write_yaml(tmp_path, text: str):
    p = tmp_path / "desk.yaml"
    p.write_text(text, encoding="utf-8")
    return p


class TestDeskConfigExpiry:
    def test_the_defaults_are_the_labs_contract(self, tmp_path):
        cfg = load_desk_config(tmp_path / "does-not-exist.yaml")
        assert (cfg.option_expiry, cfg.option_roll_days) == ("monthly", 3)
        assert dataclasses.replace(small_cfg()).option_expiry == "monthly"

    def test_both_settings_are_read(self, tmp_path):
        cfg = load_desk_config(
            write_yaml(tmp_path, "option_expiry: nearest\noption_roll_days: 5\n")
        )
        assert (cfg.option_expiry, cfg.option_roll_days) == ("nearest", 5)

    @pytest.mark.parametrize("raw", ["Nearest", "  nearest ", "NEAREST", '"nearest"'])
    def test_the_mode_is_case_and_space_insensitive(self, tmp_path, raw):
        cfg = load_desk_config(write_yaml(tmp_path, f"option_expiry: {raw}\n"))
        assert cfg.option_expiry == "nearest"

    @pytest.mark.parametrize("raw", ["weekly", "next", "monthlyy", "'monthly,nearest'", "both"])
    def test_an_unknown_mode_is_refused_naming_the_choices(self, tmp_path, raw):
        with pytest.raises(ValueError, match="monthly or nearest"):
            load_desk_config(write_yaml(tmp_path, f"option_expiry: {raw}\n"))

    @pytest.mark.parametrize("raw", ["null", "~", "''"])
    def test_an_empty_mode_means_monthly(self, tmp_path, raw):
        assert load_desk_config(write_yaml(tmp_path, f"option_expiry: {raw}\n")).option_expiry == (
            "monthly"
        )

    def test_roll_days_must_be_a_whole_number(self, tmp_path):
        with pytest.raises(ValueError):
            load_desk_config(write_yaml(tmp_path, "option_roll_days: soon\n"))
        assert load_desk_config(write_yaml(tmp_path, "option_roll_days: 0\n")).option_roll_days == 0

    def test_the_shipped_desk_yaml_names_the_labs_contract(self):
        cfg = load_desk_config()
        assert (cfg.option_expiry, cfg.option_roll_days) == ("monthly", 3)


# ══ the tracker ═══════════════════════════════════════════════════════════════════════════


class Scripted:
    """What InstrumentTracker uses of a strategy: `.p`, `.generate(df, ctx)`, `.explain(...)`.
    The last bar always carries one entry on `side`, its stop `stop_dist` from the close."""

    regimes: tuple = ()

    def __init__(self, side="long", stop_dist=5.0, atr=10.0, d_atr=None, **p):
        self.side, self.stop_dist, self.atr, self.d_atr = side, stop_dist, atr, d_atr
        self.p = params(**p)

    def generate(self, df, ctx=None):
        f = df.copy()
        for col in ("long_entry", "short_entry", "long_exit", "short_exit"):
            f[col] = False
        f["long_stop"] = f["short_stop"] = np.nan
        f["long_score"] = f["short_score"] = 70.0
        f["atr"] = f["atr_trail"] = self.atr
        if self.d_atr is not None:
            f["d_atr"] = self.d_atr
        last = f.index[-1]
        sign = 1 if self.side == "long" else -1
        f.loc[last, f"{self.side}_entry"] = True
        if self.stop_dist is not None:
            f.loc[last, f"{self.side}_stop"] = float(f.loc[last, "close"]) - sign * self.stop_dist
        return f

    def explain(self, f, i, side):
        return {"reasons": ["scripted"]}


def make_tracker(strategy: Scripted, days: int = 3) -> InstrumentTracker:
    from onelot.data.universe import load_specs

    series, inst = load_specs()
    cfg = DeskInstrumentCfg("SBIN", "SBIN", "STOCK", (StrategySlotCfg("ema_cross", "2m"),))
    t = InstrumentTracker(cfg, series["SBIN"], inst["SBIN"], 2)
    bars = synthetic(days=days, freq_min=2)
    bars.loc[bars.index[-1], ["open", "high", "low", "close"]] = [1000.0, 1001.0, 999.0, 1000.0]
    t.bars = bars
    t.slots[0].strategy = strategy
    return t


def evaluate(t: InstrumentTracker):
    t.slots[0].last_bar_ts = None
    return t.evaluate(t.bars.index[-1] + pd.Timedelta(minutes=2), None, True)


class TestFreshEntriesGoThroughTheLabsRiskRule:
    def test_a_stop_inside_the_band_is_alerted_as_it_is(self):
        t = make_tracker(Scripted(stop_dist=5.0))
        ((slot, v),) = evaluate(t)
        assert (v.direction, v.stop, v.close, v.fresh) == ("long", 995.0, 1000.0, True)
        assert v.strength == 70.0 and v.reasons == ["scripted"]
        assert slot.last_verdict is v

    def test_a_tight_stop_is_widened_on_the_verdict(self):
        t = make_tracker(Scripted(stop_dist=2.0))
        ((_, v),) = evaluate(t)
        assert v.direction == "long" and v.stop == 996.0  # the floor: 0.4 x ATR 10

    def test_a_short_is_widened_upwards(self):
        t = make_tracker(Scripted(side="short", stop_dist=2.0))
        ((_, v),) = evaluate(t)
        assert v.direction == "short" and v.stop == 1004.0

    def test_a_stop_wider_than_the_cap_is_not_alerted_and_says_why(self):
        t = make_tracker(Scripted(stop_dist=30.0))
        assert evaluate(t) == []
        v = t.slots[0].last_verdict
        assert v.direction == "none" and v.stop is None and v.close == 1000.0
        assert "skipped by the strategy's risk rule" in v.reason
        assert "long setup" in v.reason and "30.00" in v.reason
        assert v.strength == 70.0 and v.reasons == ["scripted"]  # why it fired is still shown

    def test_the_percent_floor_widens_when_the_atr_is_small(self):
        # ATR 1 -> 0.4 ATR = 0.4, but 0.35 % of 1000 = 3.5: the percent floor is what binds
        t = make_tracker(Scripted(stop_dist=1.0, atr=1.0))
        ((_, v),) = evaluate(t)
        assert v.stop == pytest.approx(996.5)

    def test_the_prior_sessions_atr_in_the_features_sets_the_floor(self):
        t = make_tracker(Scripted(stop_dist=5.0, d_atr=20.0, stop_datr_min=0.5))
        ((_, v),) = evaluate(t)
        assert v.stop == pytest.approx(990.0)

    def test_a_setup_without_a_usable_atr_is_skipped_by_the_rule(self):
        t = make_tracker(Scripted(stop_dist=5.0, atr=float("nan")))
        assert evaluate(t) == []
        assert "no usable ATR" in t.slots[0].last_verdict.reason

    def test_a_stop_on_the_wrong_side_is_refused_before_the_risk_rule_runs(self):
        t = make_tracker(Scripted(stop_dist=-5.0))  # a long's stop above the close
        assert evaluate(t) == []
        assert "without a usable stop" in t.slots[0].last_verdict.reason

    def test_a_setup_with_no_stop_at_all_is_refused(self):
        t = make_tracker(Scripted(stop_dist=None))
        assert evaluate(t) == []
        assert "without a usable stop" in t.slots[0].last_verdict.reason


class TestTheStrategysOwnDailyLimit:
    @staticmethod
    def slot_with(cap: int, today: int):
        t = make_tracker(Scripted(max_trades_per_day=cap))
        t.slots[0].signals_today = today
        return t

    def test_below_the_limit_the_entry_fires(self):
        t = self.slot_with(2, 1)
        assert len(evaluate(t)) == 1

    def test_at_the_limit_it_is_held_back_and_says_so(self):
        t = self.slot_with(2, 2)
        assert evaluate(t) == []
        v = t.slots[0].last_verdict
        assert v.direction == "none" and v.stop is None and v.close == 1000.0
        assert "2 trade(s) a day" in v.reason and v.fresh is True

    def test_beyond_the_limit_it_stays_held(self):
        assert evaluate(self.slot_with(1, 5)) == []

    def test_a_limit_of_zero_means_no_limit(self):
        t = self.slot_with(0, 50)
        assert len(evaluate(t)) == 1

    def test_the_limit_is_per_strategy_slot_not_per_instrument(self):
        t = self.slot_with(1, 1)
        other = SimpleNamespace(strategy=SimpleNamespace(p=params(max_trades_per_day=1)),
                                signals_today=0)  # fmt: skip
        v = Verdict("long", "x", None, 995.0, 1000.0, 70.0, [], True)
        assert _strategy_limit(other, v) is None  # type: ignore[arg-type]
        assert _strategy_limit(t.slots[0], v) is not None

    def test_the_limit_counts_alerts_so_a_held_entry_does_not_count_again(self):
        t = self.slot_with(1, 1)
        evaluate(t)
        evaluate(t)
        assert t.slots[0].signals_today == 1  # evaluating never raises the count


class TestTheLabsCooldown:
    @staticmethod
    def bars_after(exit_offset_bars: int, total: int = 20):
        """`total` 2-minute bars of one morning, and the time inside bar `exit_offset_bars`."""
        idx = pd.date_range("2026-09-01 09:15", periods=total, freq="2min", tz=IST)
        bars = pd.DataFrame({"close": 1.0}, index=idx)
        return bars, idx[exit_offset_bars].to_pydatetime() + dt.timedelta(seconds=30)

    @staticmethod
    def slot(cooldown: int, exit_at):
        return SimpleNamespace(
            strategy=SimpleNamespace(p=params(cooldown_bars=cooldown)),
            last_exit_at=exit_at,
            cfg=SimpleNamespace(timeframe="2m"),
        )

    @pytest.mark.parametrize(
        ("exit_bar", "cooling"),
        [(19, True), (14, True), (13, True), (12, False), (5, False)],
    )
    def test_a_signal_must_come_more_than_cooldown_bars_after_the_exit_bar(self, exit_bar, cooling):
        # cooldown 6, signal on the last bar (index 19): the Lab needs 19 - exit_bar > 6, so an
        # exit in bar 13 or later still cools and one in bar 12 or earlier does not
        bars, exit_at = self.bars_after(exit_bar)
        reason = _cooling_down(self.slot(6, exit_at), bars)
        assert (reason is not None) is cooling

    def test_the_message_names_the_bars_and_the_timeframe(self):
        bars, exit_at = self.bars_after(15)
        assert "6 2m bars" in (_cooling_down(self.slot(6, exit_at), bars) or "")

    def test_no_exit_yet_or_no_cooldown_means_no_waiting(self):
        bars, exit_at = self.bars_after(19)
        assert _cooling_down(self.slot(6, None), bars) is None
        assert _cooling_down(self.slot(0, exit_at), bars) is None

    def test_an_exit_before_the_first_bar_in_hand_is_long_over(self):
        bars, _ = self.bars_after(0)
        early = bars.index[0].to_pydatetime() - dt.timedelta(days=3)
        assert _cooling_down(self.slot(6, early), bars) is None

    def test_empty_bars_never_block(self):
        _, exit_at = self.bars_after(3)
        assert (
            _cooling_down(self.slot(6, exit_at), pd.DataFrame(index=pd.DatetimeIndex([]))) is None
        )

    def test_bars_are_counted_across_the_overnight_gap_like_the_backtest(self):
        d1 = pd.date_range("2026-09-01 14:50", periods=5, freq="2min", tz=IST)  # to 14:58
        d2 = pd.date_range("2026-09-02 09:15", periods=12, freq="2min", tz=IST)
        bars = pd.DataFrame({"close": 1.0}, index=d1.append(d2))
        exit_at = d1[-1].to_pydatetime()  # exited in the last bar of the day: index 4
        # 12 bars the next morning: the last is index 16 -> 16 - 4 = 12 > 6: free
        assert _cooling_down(self.slot(6, exit_at), bars) is None
        # three bars into the next day (index 7): 7 - 4 = 3 <= 6: still cooling
        assert _cooling_down(self.slot(6, exit_at), bars.iloc[:8]) is not None

    def test_a_cooling_slot_does_not_alert_and_the_reason_comes_before_the_risk_rule(self):
        t = make_tracker(Scripted(stop_dist=30.0, cooldown_bars=6))  # also too wide to trade
        t.slots[0].last_exit_at = t.bars.index[-3].to_pydatetime()
        assert evaluate(t) == []
        assert "cooling down" in t.slots[0].last_verdict.reason

    def test_after_the_cooldown_the_entry_fires(self):
        t = make_tracker(Scripted(stop_dist=5.0, cooldown_bars=6))
        t.slots[0].last_exit_at = t.bars.index[-9].to_pydatetime()  # 8 bars ago: 8 > 6
        assert len(evaluate(t)) == 1

    def test_an_aware_exit_time_compares_with_aware_bars(self):
        t = make_tracker(Scripted(stop_dist=5.0, cooldown_bars=6))
        t.slots[0].last_exit_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
        assert len(evaluate(t)) == 1


# ══ the desk ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def app_cfg() -> AppConfig:
    return AppConfig.load()


@pytest.fixture
def db(tmp_path):
    from onelot.data.db import get_database, use_database

    d = init_database(f"sqlite:///{tmp_path / 'parity.db'}")
    use_database(d)
    yield d
    d.dispose()
    get_database.__globals__["_db"] = None


NOW = dt.datetime(2026, 11, 8, 10, 30, tzinfo=IST)


class ChainAdapter:
    """expiry_list_raw / option_chain with recorders."""

    def __init__(self, expiries=None, rows=None, fail_chain=False, fail_expiry=False):
        self.expiries = LISTED if expiries is None else expiries
        self.rows = rows or []
        self.fail_chain, self.fail_expiry = fail_chain, fail_expiry
        self.expiry_calls: list[tuple] = []
        self.chain_calls: list[tuple] = []

    def expiry_list_raw(self, security_id, segment="IDX_I"):
        self.expiry_calls.append((security_id, segment))
        if self.fail_expiry:
            raise RuntimeError("DH-904 rate limit")
        return list(self.expiries)

    def option_chain(self, underlying, expiry, security_id=25, segment="IDX_I"):
        self.chain_calls.append((underlying, expiry, security_id, segment))
        if self.fail_chain:
            raise RuntimeError("DH-904 rate limit")
        return ChainSnapshot(NOW, underlying, expiry, 45210.0, list(self.rows))


def chain_rows() -> list[ChainRow]:
    return [
        ChainRow(45000.0, "CE", "55001", ltp=420.0, bid=419.0, ask=421.0, delta=0.64, iv=0.14,
                 oi=1000.0),
        ChainRow(45100.0, "CE", "55002", ltp=345.0, bid=344.0, ask=346.0, delta=0.55, iv=0.14),
        ChainRow(45500.0, "PE", "55101", ltp=380.0, bid=379.0, ask=381.0, delta=-0.66, iv=0.14),
    ]  # fmt: skip


class TestLiveChainNamesTheMonthlyContract:
    @pytest.fixture
    def desk(self, app_cfg, db):
        d, _, _ = new_desk(app_cfg, db)
        return d

    def wire(self, desk, adapter, active=True):
        desk._adapter_getter = lambda: adapter
        desk._data_api_active = lambda: active

    def tracker(self, desk):
        return desk.trackers["BANKNIFTY"]

    def test_the_chain_asked_for_is_the_monthly_not_the_first_listed(self, desk):
        adapter = ChainAdapter(rows=chain_rows())
        self.wire(desk, adapter)
        snap = desk._live_chain(self.tracker(desk), NOW)
        assert snap is not None and snap.expiry == "2026-11-24"
        t = self.tracker(desk)
        assert adapter.chain_calls == [
            ("BANKNIFTY", "2026-11-24", int(t.series.dhan_security_id), t.series.dhan_segment)
        ]
        assert adapter.expiry_calls == [(int(t.series.dhan_security_id), t.series.dhan_segment)]

    def test_inside_the_roll_window_it_is_the_next_months(self, desk):
        adapter = ChainAdapter(rows=chain_rows())
        self.wire(desk, adapter)
        late = dt.datetime(2026, 11, 22, 10, 30, tzinfo=IST)
        desk._live_chain(self.tracker(desk), late)
        assert adapter.chain_calls[0][1] == "2026-12-29"

    def test_the_roll_window_follows_the_desk_setting(self, app_cfg, db):
        cfg = small_cfg(option_roll_days=1)
        d, _, _ = new_desk(app_cfg, db, desk_cfg=cfg)
        adapter = ChainAdapter(rows=chain_rows())
        self.wire(d, adapter)
        d._live_chain(d.trackers["BANKNIFTY"], dt.datetime(2026, 11, 22, 10, 30, tzinfo=IST))
        assert adapter.chain_calls[0][1] == "2026-11-24"  # 2 days away is enough with roll 1

    def test_nearest_mode_asks_for_the_first_expiry(self, app_cfg, db):
        d, _, _ = new_desk(app_cfg, db, desk_cfg=small_cfg(option_expiry="nearest"))
        adapter = ChainAdapter(rows=chain_rows())
        self.wire(d, adapter)
        d._live_chain(d.trackers["BANKNIFTY"], NOW)
        assert adapter.chain_calls[0][1] == "2026-11-10"

    def test_no_contract_far_enough_means_no_chain_and_no_request(self, desk):
        adapter = ChainAdapter(expiries=["2026-11-10"])
        self.wire(desk, adapter)
        assert desk._live_chain(self.tracker(desk), NOW) is None
        assert adapter.chain_calls == []

    def test_an_empty_expiry_list_means_no_chain(self, desk):
        adapter = ChainAdapter(expiries=[])
        self.wire(desk, adapter)
        assert desk._live_chain(self.tracker(desk), NOW) is None
        assert adapter.chain_calls == []

    @pytest.mark.parametrize("which", ["adapter", "plan"])
    def test_without_an_adapter_or_a_data_plan_nothing_is_asked(self, desk, which):
        adapter = ChainAdapter(rows=chain_rows())
        self.wire(desk, None if which == "adapter" else adapter, active=which != "plan")
        assert desk._live_chain(self.tracker(desk), NOW) is None
        assert adapter.expiry_calls == [] and adapter.chain_calls == []

    def test_the_chain_is_cached_for_a_minute(self, desk):
        adapter = ChainAdapter(rows=chain_rows())
        self.wire(desk, adapter)
        first = desk._live_chain(self.tracker(desk), NOW)
        again = desk._live_chain(self.tracker(desk), NOW + dt.timedelta(seconds=59))
        assert again is first and len(adapter.chain_calls) == 1
        desk._live_chain(self.tracker(desk), NOW + dt.timedelta(seconds=61))
        assert len(adapter.chain_calls) == 2

    @pytest.mark.parametrize("fail", ["fail_chain", "fail_expiry"])
    def test_a_broker_error_is_no_chain_and_is_not_retried_for_a_minute(self, desk, fail):
        adapter = ChainAdapter(rows=chain_rows(), **{fail: True})
        self.wire(desk, adapter)
        assert desk._live_chain(self.tracker(desk), NOW) is None
        calls = len(adapter.expiry_calls) + len(adapter.chain_calls)
        assert desk._live_chain(self.tracker(desk), NOW + dt.timedelta(seconds=30)) is None
        assert len(adapter.expiry_calls) + len(adapter.chain_calls) == calls


class TestAChainAlertNamesItsContract:
    @staticmethod
    def verdict(t) -> Verdict:
        close = float(t.bars["close"].iloc[-1]) if not t.bars.empty else 45210.0
        return Verdict("long", "x", NOW, close - 100.0, close, 60.0, [], True)

    def economics(self, app_cfg, db, monkeypatch, cfg, key, chain):
        d, _, _ = new_desk(app_cfg, db, desk_cfg=cfg)
        t = d.trackers[key]
        monkeypatch.setattr(d, "_live_chain", lambda tracker, now: chain)
        return d._economics(t, t.slots[0], self.verdict(t), NOW)

    def test_the_option_carries_its_security_id_segment_and_underlying(
        self, app_cfg, db, monkeypatch
    ):
        chain = ChainSnapshot(NOW, "BANKNIFTY", "2026-11-24", 45210.0, chain_rows())
        eco = self.economics(app_cfg, db, monkeypatch, small_cfg(), "BANKNIFTY", chain)
        assert eco.synthetic is False
        assert eco.option["security_id"] == "55001" and eco.option["strike"] == 45000.0
        assert eco.option["segment"] == "NSE_FNO" and eco.option["underlying"] == "BANKNIFTY"
        assert eco.option["expiry"] == "2026-11-24" and eco.option["opt_type"] == "CE"

    def test_a_bse_index_names_the_bse_derivatives_segment(self, app_cfg, db, monkeypatch):
        cfg = small_cfg(
            indices=(
                DeskInstrumentCfg(
                    "SENSEX", "SENSEX_OPT", "INDEX", (StrategySlotCfg("ema_cross", "10m"),)
                ),
            )
        )
        chain = ChainSnapshot(NOW, "SENSEX", "2026-11-24", 80000.0, chain_rows())
        eco = self.economics(app_cfg, db, monkeypatch, cfg, "SENSEX", chain)
        assert eco.option["segment"] == "BSE_FNO" and eco.option["underlying"] == "SENSEX"

    def test_a_chain_alert_can_be_priced_from_its_own_bars(self, app_cfg, db, monkeypatch):
        """The point of the two keys: `real_premium_at` has everything it needs in the dict."""
        chain = ChainSnapshot(NOW, "BANKNIFTY", "2026-11-24", 45210.0, chain_rows())
        eco = self.economics(app_cfg, db, monkeypatch, small_cfg(), "BANKNIFTY", chain)
        assert all(eco.option.get(k) for k in ("security_id", "segment", "underlying", "opt_type"))

    def test_the_synthetic_fallback_has_no_contract_to_price_from(self, app_cfg, db, monkeypatch):
        d, _, _ = new_desk(app_cfg, db)
        t = d.trackers["BANKNIFTY"]
        monkeypatch.setattr(d, "_live_chain", lambda tracker, now: None)
        eco = d._economics(t, t.slots[0], self.verdict(t), NOW)
        assert eco.synthetic is True
        assert "security_id" not in eco.option and "segment" not in eco.option
        assert d.real_premium_at(eco.option, 45210.0, NOW) is None


# ── a slot's last exit, from the alert's exit ──────────────────────────────────────────────
ALERT = {"series_key": "SBIN", "strategy": "ema_cross", "timeframe": "10m", "id": "abc123abc123"}


def desk_with_two_slots(app_cfg, db):
    d, _, _ = new_desk(app_cfg, db)
    slots = {(s.cfg.name, s.cfg.timeframe): s for s in d.trackers["SBIN"].slots}
    return d, slots


class TestANoteOfASlotsLastExit:
    def test_the_slot_remembers_when_its_trade_closed(self, app_cfg, db):
        d, slots = desk_with_two_slots(app_cfg, db)
        d._note_slot_exit(ALERT, {"at": "2026-11-06T11:15:00+05:30", "status": "CLOSED"})
        assert slots[("ema_cross", "10m")].last_exit_at == dt.datetime(
            2026, 11, 6, 11, 15, tzinfo=IST
        )
        assert slots[("ema_cross", "10m")].last_exit_at.utcoffset() is not None

    def test_only_the_slot_of_that_strategy_and_timeframe_is_touched(self, app_cfg, db):
        d, slots = desk_with_two_slots(app_cfg, db)
        d._note_slot_exit(ALERT, {"at": "2026-11-06T11:15:00+05:30", "status": "CLOSED"})
        assert slots[("supertrend_vol", "6m")].last_exit_at is None
        d._note_slot_exit(
            {**ALERT, "timeframe": "5m"}, {"at": "2026-11-06T12:00:00+05:30", "status": "CLOSED"}
        )
        assert slots[("ema_cross", "10m")].last_exit_at == dt.datetime(
            2026, 11, 6, 11, 15, tzinfo=IST
        )

    def test_a_later_exit_replaces_an_earlier_one_never_the_reverse(self, app_cfg, db):
        d, slots = desk_with_two_slots(app_cfg, db)
        slot = slots[("ema_cross", "10m")]
        d._note_slot_exit(ALERT, {"at": "2026-11-06T11:15:00+05:30", "status": "CLOSED"})
        d._note_slot_exit(ALERT, {"at": "2026-11-06T13:45:00+05:30", "status": "CLOSED"})
        d._note_slot_exit(ALERT, {"at": "2026-11-05T09:30:00+05:30", "status": "CLOSED"})
        assert slot.last_exit_at == dt.datetime(2026, 11, 6, 13, 45, tzinfo=IST)

    def test_an_exit_in_another_time_zone_is_the_same_instant(self, app_cfg, db):
        d, slots = desk_with_two_slots(app_cfg, db)
        d._note_slot_exit(ALERT, {"at": "2026-11-06T05:45:00+00:00", "status": "CLOSED"})
        assert slots[("ema_cross", "10m")].last_exit_at == dt.datetime(
            2026, 11, 6, 11, 15, tzinfo=IST
        )

    @pytest.mark.parametrize(
        ("alert", "info"),
        [
            ({**ALERT, "series_key": "NOPE"}, {"at": "2026-11-06T11:15:00+05:30"}),
            ({**ALERT, "series_key": None}, {"at": "2026-11-06T11:15:00+05:30"}),
            ({"strategy": "ema_cross"}, {"at": "2026-11-06T11:15:00+05:30"}),
            (ALERT, {}),
            (ALERT, {"at": None}),
            (ALERT, {"at": ""}),
        ],
    )
    def test_an_alert_it_cannot_place_or_an_exit_without_a_time_changes_nothing(
        self, app_cfg, db, alert, info
    ):
        d, slots = desk_with_two_slots(app_cfg, db)
        d._note_slot_exit(alert, info)
        assert all(s.last_exit_at is None for s in slots.values())

    def test_a_paper_trade_that_closes_starts_its_slots_cooldown(self, app_cfg, db, monkeypatch):
        """Through the real `_alert_exit`: the book says the trade closed at 11:15."""
        d, slots = desk_with_two_slots(app_cfg, db)
        trade = SimpleNamespace(
            id=7, alert_id=ALERT["id"], status="CLOSED", close_reason="STOP",
            exit_ts=dt.datetime(2026, 11, 6, 5, 45, tzinfo=dt.timezone.utc),
            updated_at=dt.datetime(2026, 11, 6, 5, 46, tzinfo=dt.timezone.utc),
            exit_paise=None, net_paise=None, grade="live",
            plan={"alert": dict(ALERT)},
        )  # fmt: skip
        d.paper = SimpleNamespace(trade=lambda trade_id: trade)  # type: ignore[assignment]
        monkeypatch.setattr(d, "_send_alert_exit", lambda *a, **k: None)
        monkeypatch.setattr(d, "_guarded_write", lambda what, work: None)
        d._alert_exit(7)
        assert slots[("ema_cross", "10m")].last_exit_at == dt.datetime(
            2026, 11, 6, 11, 15, tzinfo=IST
        )

    def test_a_trade_that_never_filled_is_not_an_exit_and_starts_no_cooldown(
        self, app_cfg, db, monkeypatch
    ):
        """The Lab's `last_exit_i` moves only when a position closes: a pending entry skipped
        at the fill bar (a gap through the stop) leaves it where it was, so the next signal is
        not made to wait `cooldown_bars` bars after a trade that never existed."""
        d, slots = desk_with_two_slots(app_cfg, db)
        trade = SimpleNamespace(
            id=8, alert_id=ALERT["id"], status="NO_FILL", close_reason="GAP_THROUGH_STOP",
            exit_ts=None, updated_at=dt.datetime(2026, 11, 6, 5, 50, tzinfo=dt.timezone.utc),
            exit_paise=None, net_paise=None, grade="live", plan={"alert": dict(ALERT)},
        )  # fmt: skip
        d.paper = SimpleNamespace(trade=lambda trade_id: trade)  # type: ignore[assignment]
        monkeypatch.setattr(d, "_send_alert_exit", lambda *a, **k: None)
        monkeypatch.setattr(d, "_guarded_write", lambda what, work: None)
        d._alert_exit(8)
        assert slots[("ema_cross", "10m")].last_exit_at is None


# ── the slot counters across a restart ─────────────────────────────────────────────────────
def store_alert(db, alert_id: str, created: dt.datetime, *, exit_at: str | None = None,
                strategy: str = "ema_cross", timeframe: str = "10m", series: str = "SBIN",
                status: str = "EXPIRED", **payload_over) -> None:  # fmt: skip
    payload = {
        "id": alert_id, "created_at": created.isoformat(), "series_key": series,
        "instrument_key": series, "strategy": strategy, "timeframe": timeframe,
        "expires_at": (created + dt.timedelta(minutes=10)).isoformat(), "status": status,
        **payload_over,
    }  # fmt: skip
    if exit_at:
        payload["exit"] = {"at": exit_at, "reason": "STOP", "status": "CLOSED"}
    with db.engine.begin() as conn:
        conn.execute(
            desk_signals.insert().values(
                id=alert_id, created_at=created, expires_at=created + dt.timedelta(minutes=10),
                instrument_key=series, series_key=series, kind="STOCK", side="BUY",
                strategy=strategy, timeframe=timeframe, bar_ts=created, status=status,
                payload=payload,
            )
        )  # fmt: skip


def today_at(h: int, m: int) -> dt.datetime:
    now = dt.datetime.now(IST)
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def capped_cfg(cap: int = 1):
    slot = StrategySlotCfg("ema_cross", "10m", {"max_trades_per_day": cap, "cooldown_bars": 6})
    return small_cfg(
        stocks=(DeskInstrumentCfg("SBIN", "SBIN", "STOCK", (slot,)),),
    )


def fire_a_fresh_entry(desk):
    """Make SBIN's slot see a fresh long on its last closed bar; returns what fired."""
    t = desk.trackers["SBIN"]
    slot = t.slots[0]
    t.update_bars(synthetic(seed=11, base=950.0))
    real = slot.strategy.generate

    def fire(df, ctx=None):
        f = real(df, ctx)
        f.loc[f.index[-1], "long_entry"] = True
        f.loc[f.index[-1], "long_stop"] = float(f["close"].iloc[-1]) * 0.99
        return f

    slot.strategy.generate = fire
    slot.last_bar_ts = None
    last_day = t.bars.index[-1].normalize()
    t.bars = t.bars[t.bars.index <= last_day + pd.Timedelta(hours=15, minutes=13)]
    end = t.bars.index[-1] + pd.Timedelta(minutes=2)
    return t.evaluate(end, None, True), slot


class TestSlotCountersSurviveARestart:
    @pytest.fixture(autouse=True)
    def _bars_needed(self, monkeypatch):
        monkeypatch.setattr("onelot.desk.tracker.MIN_BARS", 10)

    def test_control_without_an_archived_alert_a_fresh_entry_fires(self, app_cfg, db):
        d, _, _ = new_desk(app_cfg, db, desk_cfg=capped_cfg(1))
        fired, slot = fire_a_fresh_entry(d)
        assert len(fired) == 1 and slot.signals_today == 0

    def test_todays_alert_for_a_slot_capped_at_one_blocks_a_second_entry(self, app_cfg, db):
        store_alert(db, "today0000001", today_at(9, 45))
        d, _, _ = new_desk(app_cfg, db, desk_cfg=capped_cfg(1))
        slot = d.trackers["SBIN"].slots[0]
        assert slot.signals_today == 1
        fired, _ = fire_a_fresh_entry(d)
        assert fired == []
        assert "1 trade(s) a day" in slot.last_verdict.reason

    def test_the_first_poll_does_not_wipe_what_was_restored(self, app_cfg, db):
        store_alert(db, "today0000002", today_at(9, 45))
        d, _, _ = new_desk(app_cfg, db, desk_cfg=capped_cfg(1))
        assert d._counter_day == dt.datetime.now(IST).date().isoformat()
        d._reset_daily_counters(dt.datetime.now(IST))  # what the first poll runs
        assert d.trackers["SBIN"].slots[0].signals_today == 1

    def test_a_day_later_the_counters_do_reset(self, app_cfg, db):
        store_alert(db, "today0000003", today_at(9, 45))
        d, _, _ = new_desk(app_cfg, db, desk_cfg=capped_cfg(1))
        d._reset_daily_counters(dt.datetime.now(IST) + dt.timedelta(days=1))
        assert d.trackers["SBIN"].slots[0].signals_today == 0

    def test_only_todays_alerts_count_not_yesterdays(self, app_cfg, db):
        store_alert(db, "yesterday0001", today_at(9, 45) - dt.timedelta(days=1))
        store_alert(db, "today0000004", today_at(10, 5))
        d, _, _ = new_desk(app_cfg, db, desk_cfg=capped_cfg(5))
        assert d.trackers["SBIN"].slots[0].signals_today == 1

    def test_every_alert_of_the_slot_today_counts_whatever_became_of_it(self, app_cfg, db):
        for i, status in enumerate(["TAKEN", "SKIPPED", "EXPIRED"]):
            store_alert(db, f"today00001{i}", today_at(9, 40 + i * 5), status=status)
        d, _, _ = new_desk(app_cfg, db, desk_cfg=capped_cfg(5))
        assert d.trackers["SBIN"].slots[0].signals_today == 3

    def test_alerts_of_other_slots_and_instruments_count_for_their_own_slot_only(self, app_cfg, db):
        store_alert(db, "other0000001", today_at(9, 45), strategy="supertrend_vol", timeframe="6m")
        store_alert(db, "other0000002", today_at(9, 50), timeframe="5m")  # no such slot
        store_alert(db, "other0000003", today_at(9, 55), series="BANKNIFTY")
        d, _, _ = new_desk(app_cfg, db)
        counts = {
            (t.cfg.key, s.cfg.name, s.cfg.timeframe): s.signals_today
            for t in d.trackers.values()
            for s in t.slots
        }
        assert counts[("SBIN", "supertrend_vol", "6m")] == 1
        assert counts[("SBIN", "ema_cross", "10m")] == 0
        assert counts[("BANKNIFTY", "ema_cross", "10m")] == 1

    def test_an_alert_of_a_series_or_slot_the_desk_no_longer_runs_is_ignored(self, app_cfg, db):
        store_alert(db, "gone00000001", today_at(9, 45), series="HDFCBANK")
        store_alert(db, "gone00000002", today_at(9, 46), strategy="orb")
        d, _, _ = new_desk(app_cfg, db)
        assert sum(s.signals_today for t in d.trackers.values() for s in t.slots) == 0

    @pytest.mark.parametrize("created", [None, "garbage", ""])
    def test_an_alert_without_a_readable_time_neither_counts_nor_breaks_the_start(
        self, app_cfg, db, created
    ):
        store_alert(db, "bad000000001", today_at(9, 45), created_at=created)
        d, _, _ = new_desk(app_cfg, db)
        assert d.trackers["SBIN"].slots[0].signals_today == 0

    def test_the_latest_archived_exit_restarts_the_cooldown(self, app_cfg, db):
        yesterday = today_at(13, 0) - dt.timedelta(days=1)
        store_alert(
            db,
            "exit00000001",
            yesterday - dt.timedelta(hours=3),
            exit_at=(yesterday - dt.timedelta(hours=2)).isoformat(),
        )
        store_alert(
            db,
            "exit00000002",
            yesterday,
            exit_at=(yesterday + dt.timedelta(minutes=45)).isoformat(),
        )
        store_alert(
            db,
            "exit00000003",
            yesterday - dt.timedelta(days=1),
            exit_at=(yesterday - dt.timedelta(days=1)).isoformat(),
        )
        d, _, _ = new_desk(app_cfg, db)
        slot = next(s for s in d.trackers["SBIN"].slots if s.cfg.timeframe == "10m")
        assert slot.last_exit_at == yesterday + dt.timedelta(minutes=45)
        assert slot.signals_today == 0  # none of those alerts is from today

    @staticmethod
    def evaluated_bars():
        """The fine bars `fire_a_fresh_entry` evaluates: the feed cut at 15:13 of its last day."""
        bars = synthetic(seed=11, base=950.0)
        return bars[bars.index <= bars.index[-1].normalize() + pd.Timedelta(hours=15, minutes=13)]

    def test_a_restored_exit_makes_the_first_signal_wait_out_the_cooldown(self, app_cfg, db):
        exit_at = self.evaluated_bars().index[-4].to_pydatetime()  # 6 minutes before the signal
        store_alert(
            db, "exit00000004", exit_at - dt.timedelta(hours=1), exit_at=exit_at.isoformat()
        )
        d, _, _ = new_desk(app_cfg, db, desk_cfg=capped_cfg(5))
        fired, slot = fire_a_fresh_entry(d)
        assert slot.last_exit_at == exit_at
        assert fired == [] and "cooling down" in slot.last_verdict.reason

    def test_an_exit_longer_ago_than_the_cooldown_lets_the_entry_through(self, app_cfg, db):
        # cooldown 6 bars of 10 minutes: an exit 90 minutes before the signal is long over
        exit_at = self.evaluated_bars().index[-46].to_pydatetime()
        store_alert(
            db, "exit00000005", exit_at - dt.timedelta(hours=1), exit_at=exit_at.isoformat()
        )
        d, _, _ = new_desk(app_cfg, db, desk_cfg=capped_cfg(5))
        fired, slot = fire_a_fresh_entry(d)
        assert slot.last_exit_at == exit_at and len(fired) == 1


# ── real_premium_at ────────────────────────────────────────────────────────────────────────
OPTION = {
    "security_id": "55001", "segment": "NSE_FNO", "underlying": "BANKNIFTY", "opt_type": "CE",
    "strike": 45000.0, "expiry": "2026-11-24",
}  # fmt: skip
M0 = dt.datetime(2026, 10, 29, 10, 6, tzinfo=IST)  # a past session: the wall clock is later


SESSION_DAY = M0.date()


def minute(h: int, m: int, day: dt.date = SESSION_DAY) -> pd.Timestamp:
    return pd.Timestamp(dt.datetime(day.year, day.month, day.day, h, m, tzinfo=IST))


def one_minute_frame(
    rows: dict[tuple[int, int], tuple[float, float, float, float]],
) -> pd.DataFrame:
    f = pd.DataFrame(
        list(rows.values()),
        index=pd.DatetimeIndex([minute(*k) for k in rows]),
        columns=["open", "high", "low", "close"],
    )
    f["volume"] = 0.0
    return f


UND_1M = one_minute_frame(
    {
        (10, 5): (45200.0, 45215.0, 45190.0, 45210.0),
        (10, 6): (45210.0, 45220.0, 45150.0, 45160.0),
        (10, 7): (45160.0, 45175.0, 45140.0, 45170.0),
    }
)
OPT_BARS = {  # the call's own minutes: open, high, low, close
    (10, 5): (320.0, 324.0, 318.0, 323.0),
    (10, 6): (323.0, 325.0, 292.0, 296.0),
    (10, 7): (296.0, 301.0, 294.0, 299.0),
}


class ContractBars:
    """`historical_intraday` for the desk's contract frames: scripted per call."""

    def __init__(self, script=None, fail=False):
        self.script = script or (lambda n: OPT_BARS)
        self.fail = fail
        self.calls: list[tuple] = []

    def historical_intraday(self, contract: Contract, interval_min, start, end):
        self.calls.append((contract, interval_min, start, end))
        if self.fail:
            raise RuntimeError("DH-904 rate limit")
        rows = self.script(len(self.calls))
        return [Bar(minute(*k).to_pydatetime(), *v, None) for k, v in rows.items()]


class Clock:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


class TestRealPremiumAt:
    @pytest.fixture
    def rig(self, app_cfg, db, monkeypatch):
        d, _, _ = new_desk(app_cfg, db)
        t = d.trackers["BANKNIFTY"]
        t.bars, t.fine_minutes = UND_1M.copy(), 1
        adapter = ContractBars()
        d._adapter_getter = lambda: adapter
        d._data_api_active = lambda: True
        clock = Clock(dt.datetime(2026, 11, 8, 10, 0, tzinfo=IST))
        monkeypatch.setattr("onelot.desk.desk.now_ist", clock)
        return SimpleNamespace(desk=d, tracker=t, adapter=adapter, clock=clock)

    def test_a_fill_at_a_minutes_open_is_the_contracts_open(self, rig):
        got = rig.desk.real_premium_at(OPTION, 45210.0, M0)
        assert got == 323.0  # the 10:06 open of the contract

    def test_an_exit_level_inside_the_minute_is_interpolated_in_the_contracts_own_bar(self, rig):
        # the index went 45,210 -> 45,150 low in the 10:06 minute; level 45,180 is half way
        got = rig.desk.real_premium_at(OPTION, 45180.0, M0)
        assert got == pytest.approx(323.0 + 0.5 * (292.0 - 323.0))

    def test_a_put_moves_the_other_way(self, rig):
        put = dict(OPTION, opt_type="PE")
        got = rig.desk.real_premium_at(put, 45180.0, M0)
        assert got == pytest.approx(323.0 + 0.5 * (325.0 - 323.0))

    def test_a_mark_at_a_close_is_the_contracts_close_of_the_minute_that_just_ended(self, rig):
        at = dt.datetime(2026, 10, 29, 10, 7, tzinfo=IST)
        # only the 10:06 minute is closed in the index frame
        rig.tracker.bars = UND_1M.iloc[:2].copy()
        assert rig.desk.real_premium_at(OPTION, 45160.0, at) == 296.0

    def test_the_request_names_the_contract_the_minute_bar_interval_and_the_session_start(
        self, rig
    ):
        rig.desk.real_premium_at(OPTION, 45210.0, M0)
        ((contract, interval, start, end),) = rig.adapter.calls
        assert (contract.security_id, contract.exchange_segment) == ("55001", "NSE_FNO")
        assert contract.instrument_type == "OPTIDX" and contract.underlying == "BANKNIFTY"
        assert interval == 1
        assert start == dt.datetime(2026, 10, 29, 9, 0, tzinfo=IST)  # the wanted day's 09:00
        assert end == rig.clock.now

    @pytest.mark.parametrize(
        "broken",
        [
            {"security_id": None}, {"security_id": ""}, {"segment": None}, {"segment": ""},
            {"underlying": None}, {"underlying": ""}, {"opt_type": None}, {"opt_type": "XX"},
            {"opt_type": "CALL"}, {"opt_type": "ce"},
        ],
    )  # fmt: skip
    def test_an_option_that_cannot_be_looked_up_is_none_and_asks_nothing(self, rig, broken):
        assert rig.desk.real_premium_at({**OPTION, **broken}, 45210.0, M0) is None
        assert rig.adapter.calls == []

    def test_a_missing_key_is_none_not_an_error(self, rig):
        for k in ("security_id", "segment", "underlying", "opt_type"):
            partial = {a: b for a, b in OPTION.items() if a != k}
            assert rig.desk.real_premium_at(partial, 45210.0, M0) is None
        assert rig.desk.real_premium_at({}, 45210.0, M0) is None
        assert rig.adapter.calls == []

    def test_an_underlying_the_desk_does_not_track_is_none(self, rig):
        assert rig.desk.real_premium_at({**OPTION, "underlying": "MIDCPNIFTY"}, 45210.0, M0) is None
        assert rig.adapter.calls == []

    def test_a_feed_that_is_not_one_minute_bars_is_none(self, rig):
        """Yahoo's 2-minute bars cannot be matched to a contract's 1-minute bars."""
        rig.tracker.fine_minutes = 2
        assert rig.desk.real_premium_at(OPTION, 45210.0, M0) is None
        assert rig.adapter.calls == []

    def test_a_tracker_without_bars_is_none(self, rig):
        rig.tracker.bars = rig.tracker.bars.iloc[:0]
        assert rig.desk.real_premium_at(OPTION, 45210.0, M0) is None
        assert rig.adapter.calls == []

    def test_without_a_data_plan_it_is_none_and_asks_nothing(self, rig):
        rig.desk._data_api_active = lambda: False
        assert rig.desk.real_premium_at(OPTION, 45210.0, M0) is None
        assert rig.adapter.calls == []

    def test_without_an_adapter_it_is_none(self, rig):
        rig.desk._adapter_getter = lambda: None
        assert rig.desk.real_premium_at(OPTION, 45210.0, M0) is None

    def test_a_contract_with_no_bars_is_none(self, rig):
        rig.adapter.script = lambda n: {}
        assert rig.desk.real_premium_at(OPTION, 45210.0, M0) is None

    def test_a_level_the_index_never_reached_in_that_minute_is_none(self, rig):
        assert rig.desk.real_premium_at(OPTION, 46000.0, M0) is None

    def test_a_minute_the_contract_did_not_trade_is_none_never_invented(self, rig):
        rig.adapter.script = lambda n: {k: v for k, v in OPT_BARS.items() if k != (10, 6)}
        mid = M0 + dt.timedelta(seconds=30)  # inside the minute the contract did not trade
        assert rig.desk.real_premium_at(OPTION, 45180.0, mid) is None

    def test_a_broker_error_is_none_so_the_model_prices_it(self, rig):
        rig.adapter.fail = True
        assert rig.desk.real_premium_at(OPTION, 45210.0, M0) is None

    def test_the_contracts_bars_are_not_read_again_within_five_seconds(self, rig):
        rig.adapter.script = lambda n: {k: v for k, v in OPT_BARS.items() if k != (10, 7)}
        rig.desk.real_premium_at(OPTION, 45210.0, M0)
        rig.clock.now += dt.timedelta(seconds=4)
        rig.desk.real_premium_at(OPTION, 45210.0, dt.datetime(2026, 10, 29, 10, 7, tzinfo=IST))
        assert len(rig.adapter.calls) == 1

    def test_they_are_read_again_after_five_seconds_when_the_minute_is_not_in_them(self, rig):
        rig.adapter.script = lambda n: {k: v for k, v in OPT_BARS.items() if k != (10, 7)}
        rig.desk.real_premium_at(OPTION, 45210.0, M0)
        rig.clock.now += dt.timedelta(seconds=6)
        rig.desk.real_premium_at(OPTION, 45170.0, dt.datetime(2026, 10, 29, 10, 7, tzinfo=IST))
        assert len(rig.adapter.calls) == 2

    def test_a_frame_that_already_holds_the_wanted_minute_is_never_read_again(self, rig):
        rig.desk.real_premium_at(OPTION, 45210.0, M0)
        rig.clock.now += dt.timedelta(minutes=30)
        assert rig.desk.real_premium_at(OPTION, 45210.0, M0) == 323.0
        assert len(rig.adapter.calls) == 1

    def test_each_contract_has_its_own_cache(self, rig):
        rig.desk.real_premium_at(OPTION, 45210.0, M0)
        rig.desk.real_premium_at({**OPTION, "security_id": "55002"}, 45210.0, M0)
        assert [c[0].security_id for c in rig.adapter.calls] == ["55001", "55002"]

    def test_a_failed_read_is_not_repeated_for_five_seconds_but_is_after(self, rig):
        rig.adapter.fail = True
        rig.desk.real_premium_at(OPTION, 45210.0, M0)
        rig.clock.now += dt.timedelta(seconds=4)
        rig.desk.real_premium_at(OPTION, 45210.0, M0)
        assert len(rig.adapter.calls) == 1
        rig.clock.now += dt.timedelta(seconds=2)
        rig.adapter.fail = False
        assert rig.desk.real_premium_at(OPTION, 45210.0, M0) == 323.0
        assert len(rig.adapter.calls) == 2

    def test_the_cache_is_bounded(self, rig):
        for i in range(80):
            rig.desk.real_premium_at({**OPTION, "security_id": str(60000 + i)}, 45210.0, M0)
        assert len(rig.desk._contract_cache) <= 65

    def test_a_bar_still_forming_when_it_was_read_is_not_the_minutes_final_bar(self, rig):
        """Dhan's intraday answer includes the minute in progress (the desk's own index feed
        drops it for that reason: `closed bars only`). A frame read at 10:06:05 holds a 10:06
        candle with a few seconds of range; the exit for that minute is priced a minute later,
        and must come from the finished candle (low 292), not the first seconds of it (low 322)."""
        early = {
            (10, 5): OPT_BARS[(10, 5)],
            (10, 6): (323.0, 323.5, 322.0, 322.5),  # the first 5 seconds of 10:06
        }
        rig.adapter.script = lambda n: early if n == 1 else OPT_BARS
        # 10:06:05: the poll marks the open trade at the close of 10:05; 10:06 has not closed
        rig.tracker.bars = UND_1M.iloc[:1].copy()
        rig.clock.now = dt.datetime(2026, 10, 29, 10, 6, 5, tzinfo=IST)
        rig.desk.real_premium_at(OPTION, 45210.0, dt.datetime(2026, 10, 29, 10, 6, tzinfo=IST))
        # 10:07:05: 10:06 has closed; the stop was hit inside it and is priced now
        rig.tracker.bars = UND_1M.iloc[:2].copy()
        rig.clock.now = dt.datetime(2026, 10, 29, 10, 7, 5, tzinfo=IST)
        got = rig.desk.real_premium_at(OPTION, 45180.0, M0)  # the stop level inside minute 10:06
        assert got == pytest.approx(323.0 + 0.5 * (292.0 - 323.0))


# ── the whole chain: an alert, its paper trade, the contract's own bars ────────────────────
SESSION = dt.date(2026, 10, 29)  # long past: the wall clock is later, so bars are all "closed"


def ist_at(h: int, m: int, s: int = 0) -> dt.datetime:
    return dt.datetime(SESSION.year, SESSION.month, SESSION.day, h, m, s, tzinfo=IST)


GLUE_UND = one_minute_frame(
    {
        (10, 5): (45200.0, 45215.0, 45190.0, 45210.0),
        (10, 6): (45210.0, 45220.0, 45150.0, 45160.0),
        (10, 7): (45160.0, 45175.0, 45140.0, 45170.0),
        (10, 8): (45170.0, 45172.0, 45090.0, 45095.0),  # through the 45,110 stop
    }
)
GLUE_OPT = {  # the 55001 call's own minutes, around the chain's 421 at the alert
    (10, 5): (418.0, 424.0, 416.0, 421.0),
    (10, 6): (421.0, 423.0, 392.0, 396.0),
    (10, 7): (396.0, 401.0, 394.0, 399.0),
    (10, 8): (399.0, 400.0, 370.0, 372.0),
}


class TestPaperBookOnTheDesksRealPremiums:
    """The glue nobody else tests: economics -> alert -> book plan -> `Desk.real_premium_at`
    -> the contract's bars, through the real factory-built PaperBook."""

    @pytest.fixture
    def rig(self, app_cfg, db, monkeypatch):
        from onelot.paper.factory import build_paper_book

        holder: dict = {}
        clock = Clock(ist_at(10, 8, 5))
        monkeypatch.setattr("onelot.desk.desk.now_ist", clock)
        book = build_paper_book(
            app_cfg, db.engine, clock=clock,
            real_premium_at=lambda option, u, ts: holder["desk"].real_premium_at(option, u, ts),
        )  # fmt: skip
        d, notifier, _ = new_desk(app_cfg, db, desk_cfg=small_cfg(stocks=()), paper=book)
        holder["desk"] = d
        d.fine_minutes = 1
        d.source.served_by["BANKNIFTY"] = "dhan"  # a live feed, so the trade is live grade
        t = d.trackers["BANKNIFTY"]
        t.fine_minutes = 1
        t.bars = GLUE_UND.iloc[:3].copy()  # what had closed by 10:08
        adapter = ContractBars(script=lambda n: GLUE_OPT if n > 1 else {
            k: v for k, v in GLUE_OPT.items() if k != (10, 8)
        })  # fmt: skip
        d._adapter_getter = lambda: adapter
        d._data_api_active = lambda: True
        chain = ChainSnapshot(ist_at(10, 5), "BANKNIFTY", "2026-11-24", 45210.0, chain_rows())
        monkeypatch.setattr(d, "_live_chain", lambda tracker, now: chain)
        return SimpleNamespace(desk=d, tracker=t, book=book, adapter=adapter, clock=clock,
                               engine=db.engine, cfg=app_cfg)  # fmt: skip

    @staticmethod
    def alert(rig):
        t, d = rig.tracker, rig.desk
        v = Verdict("long", "x", ist_at(10, 5), 45110.0, 45210.0, 60.0, ["test"], True)
        d._raise_signal(t, t.slots[0], v, ist_at(10, 5, 3))
        (sig,) = d.signals.values()
        return sig

    @staticmethod
    def step(rig, now):
        rig.clock.now = now
        rig.book.step(now, lambda key: rig.tracker.bars if key == "BANKNIFTY" else None)

    @staticmethod
    def events(rig, kind):
        from onelot.store.schema import paper_events

        with rig.engine.connect() as conn:
            rows = conn.execute(paper_events.select()).mappings().all()
        return [dict(r) for r in rows if r["kind"] == kind]

    @staticmethod
    def trade(rig):
        from onelot.store.schema import paper_trades

        with rig.engine.connect() as conn:
            (row,) = conn.execute(paper_trades.select()).mappings().all()
        return dict(row)

    def fills(self, rig):
        spread, ticks = rig.cfg.backtest.option_spread_pct, rig.cfg.backtest.option_slippage_ticks
        return spread, ticks

    def test_the_alert_carries_everything_the_contract_lookup_needs(self, rig):
        sig = self.alert(rig)
        option = sig["economics"]["option"]
        assert option["security_id"] == "55001" and option["segment"] == "NSE_FNO"
        assert option["underlying"] == "BANKNIFTY" and option["opt_type"] == "CE"
        assert self.trade(rig)["plan"]["alert"]["economics"]["option"]["segment"] == "NSE_FNO"

    def test_the_trade_fills_at_the_contracts_open_of_the_fill_minute(self, rig):
        self.alert(rig)
        self.step(rig, ist_at(10, 8, 5))
        spread, ticks = self.fills(rig)
        (filled,) = self.events(rig, "FILLED")
        assert filled["detail"]["premium_source"] == "real"
        assert filled["detail"]["fair_premium_paise"] == 42100  # the 10:06 open of the contract
        assert self.trade(rig)["entry_paise"] == ask_paise_for("421.00", spread, ticks)

    def test_the_open_trade_is_marked_at_the_contracts_close_of_its_last_minute(self, rig):
        self.alert(rig)
        self.step(rig, ist_at(10, 8, 5))
        assert self.trade(rig)["plan"]["position"]["mark_paise"] == 39900  # the 10:07 close

    def test_a_stop_is_sold_at_the_price_interpolated_in_the_contracts_own_minute(self, rig):
        self.alert(rig)
        self.step(rig, ist_at(10, 8, 5))
        rig.tracker.bars = GLUE_UND.copy()  # 10:08 has closed
        self.step(rig, ist_at(10, 9, 15))
        row = self.trade(rig)
        assert (row["status"], row["close_reason"]) == ("CLOSED", "STOP")
        # the index fell 60 of the 80 points of the 10:08 minute (45,170 -> 45,110 of -> 45,090):
        # the call went 3/4 of the way from its open 399 to its low 370
        spread, ticks = self.fills(rig)
        assert row["exit_paise"] == bid_paise_for(
            f"{399.0 + 0.75 * (370.0 - 399.0):.2f}", spread, ticks
        )
        (closed,) = self.events(rig, "CLOSED")
        assert closed["detail"]["exit_premium_source"] == "real"

    def test_with_no_data_plan_the_same_trade_is_priced_by_the_model(self, rig):
        rig.desk._data_api_active = lambda: False
        self.alert(rig)
        self.step(rig, ist_at(10, 8, 5))
        (filled,) = self.events(rig, "FILLED")
        assert filled["detail"]["premium_source"] == "model" and rig.adapter.calls == []

    def test_a_yahoo_feed_of_two_minute_bars_is_priced_by_the_model(self, rig):
        rig.tracker.fine_minutes = 2
        self.alert(rig)
        self.step(rig, ist_at(10, 8, 5))
        (filled,) = self.events(rig, "FILLED")
        assert filled["detail"]["premium_source"] == "model"


def ask_paise_for(premium: str, spread: float, ticks: int) -> int:
    from decimal import ROUND_CEILING, Decimal

    raw = Decimal(premium) * (1 + Decimal(str(spread))) + ticks * Decimal("0.05")
    return int((raw / Decimal("0.05")).to_integral_value(rounding=ROUND_CEILING)) * 5


def bid_paise_for(premium: str, spread: float, ticks: int) -> int:
    from decimal import ROUND_FLOOR, Decimal

    raw = Decimal(premium) * (1 - Decimal(str(spread))) - ticks * Decimal("0.05")
    return max(int((raw / Decimal("0.05")).to_integral_value(rounding=ROUND_FLOOR)) * 5, 5)


# ── the engine hands the book a late-bound real-premium source ────────────────────────────
class TestEngineBindsTheRealPremiumSourceLate:
    def test_before_the_desk_exists_it_answers_none(self):
        from onelot.engine import Engine

        fake = SimpleNamespace(desk=None)
        bound = MethodType(Engine._real_premium, fake)
        assert bound(dict(OPTION), 45210.0, M0) is None

    def test_once_the_desk_exists_the_same_callable_asks_it(self):
        from onelot.engine import Engine

        asked = []
        desk = SimpleNamespace(
            real_premium_at=lambda option, underlying, ts: (
                asked.append((option, underlying, ts)) or 123.45
            )
        )
        fake = SimpleNamespace(desk=None)
        bound = MethodType(Engine._real_premium, fake)
        assert bound(dict(OPTION), 45210.0, M0) is None
        fake.desk = desk  # the desk is built after the book
        assert bound(dict(OPTION), 45210.0, M0) == 123.45
        assert asked == [(OPTION, 45210.0, M0)]

    def test_the_engine_gives_its_own_bound_method_to_the_book_factory(self, monkeypatch):
        from onelot.engine import Engine

        seen = {}

        def fake_factory(cfg, engine, clock=None, real_premium_at=None):
            seen.update(cfg=cfg, engine=engine, real=real_premium_at)
            return "BOOK"

        monkeypatch.setattr("onelot.paper.factory.build_paper_book", fake_factory)
        me = SimpleNamespace(cfg="CFG", db=SimpleNamespace(engine="ENGINE"), desk=None,
                             status=SimpleNamespace(warnings=[]))  # fmt: skip
        me._real_premium = MethodType(Engine._real_premium, me)
        assert Engine._paper_book(me) == "BOOK"  # type: ignore[arg-type]
        assert (seen["cfg"], seen["engine"]) == ("CFG", "ENGINE")
        assert seen["real"] is me._real_premium

    def test_a_book_that_cannot_be_built_is_a_warning_not_a_crash(self, monkeypatch, caplog):
        from onelot.engine import Engine

        def boom(*a, **k):
            raise RuntimeError("costs.yaml unreadable")

        monkeypatch.setattr("onelot.paper.factory.build_paper_book", boom)
        me = SimpleNamespace(cfg=None, db=SimpleNamespace(engine=None), desk=None,
                             status=SimpleNamespace(warnings=[]))  # fmt: skip
        me._real_premium = lambda *a: None
        with caplog.at_level(logging.ERROR):
            assert Engine._paper_book(me) is None  # type: ignore[arg-type]
        assert any("Paper book failed" in w for w in me.status.warnings)


class TestFactoryPassesTheSourceThrough:
    def test_the_book_it_builds_asks_the_source_first(self, app_cfg, db):
        from onelot.paper.factory import build_paper_book

        def source(option, underlying, ts):
            return 1.0

        book = build_paper_book(app_cfg, db.engine, real_premium_at=source)
        assert book.real_premium_at is source

    def test_without_one_the_book_runs_on_the_model_alone(self, app_cfg, db):
        from onelot.paper.factory import build_paper_book

        assert build_paper_book(app_cfg, db.engine).real_premium_at is None


class TestTheDeskTellsTheRecorderOnEveryAlert:
    def test_an_alert_is_handed_to_the_fill_cost_recorder(self, app_cfg, db):
        d, _, _ = new_desk(app_cfg, db)
        queued = []
        d.fillcost.on_alert = queued.append  # type: ignore[method-assign]
        t = d.trackers["SBIN"]
        close = float(t.bars["close"].iloc[-1]) if not t.bars.empty else 950.0
        v = Verdict(
            "long", "x", yesterday_close().to_pydatetime(), close * 0.99, close, 60.0, [], True
        )
        d._raise_signal(t, t.slots[0], v, dt.datetime.now(IST))
        assert len(queued) == 1 and queued[0]["id"] in d.signals

    def test_a_recorder_that_fails_never_stops_the_alert(self, app_cfg, db, caplog):
        d, notifier, _ = new_desk(app_cfg, db)

        def boom(sig):
            raise RuntimeError("measurement exploded")

        d.fillcost.on_alert = boom  # type: ignore[method-assign]
        t = d.trackers["SBIN"]
        v = Verdict("long", "x", yesterday_close().to_pydatetime(), 940.0, 950.0, 60.0, [], True)
        with caplog.at_level(logging.ERROR):
            d._raise_signal(t, t.slots[0], v, dt.datetime.now(IST))
        assert len(d.signals) == 1 and len(notifier.sent) == 1
        assert any("fill costs" in r.message for r in caplog.records)


__all__ = ["FakeNotifier"]  # re-exported helper: keeps the harness import honest
