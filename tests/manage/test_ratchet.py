"""The trade-management rule of 2026-11-23 (`onelot.manage.ratchet`): the owner's numbers,
the direction the levels may move, and the bar convention every caller shares."""

from __future__ import annotations

import random

import pytest

from onelot.manage.ratchet import (
    Plan,
    RatchetParams,
    State,
    advance,
    exit_on_bar,
    plan_from_risk,
    progress,
    start,
    stop_label,
)

# entry 100, first stop 98 (risk 2), target 3 risks away = 106: the planned move is 6
LONG = plan_from_risk(1, 100.0, 2.0, 3.0, cost=0.1)
SHORT = plan_from_risk(-1, 100.0, 2.0, 3.0, cost=0.1)


def test_a_plan_from_risk_has_its_levels_on_the_right_sides():
    assert (LONG.stop, LONG.target, LONG.move, LONG.risk) == (98.0, 106.0, 6.0, 2.0)
    assert (SHORT.stop, SHORT.target) == (102.0, 94.0)
    assert start(LONG) == State(98.0, 106.0, 100.0, 0)


def test_nothing_moves_before_a_quarter_of_the_move():
    s, ev = advance(LONG, start(LONG), high=101.4, low=99.0)
    assert (s.stop, s.target, s.stage, ev) == (98.0, 106.0, 0, ())
    assert s.best == 101.4 and progress(LONG, s.best) == pytest.approx(0.2333, abs=1e-4)


def test_at_a_quarter_the_stop_goes_above_breakeven_with_a_little_profit():
    s, ev = advance(LONG, start(LONG), high=101.5, low=100.2)
    # entry + cost 0.1 + 5% of the move (0.3)
    assert s.stop == pytest.approx(100.4) and s.target == 106.0 and s.stage == 1
    assert ev == ("BREAKEVEN", "STOP_MOVED") and stop_label(s) == "BREAKEVEN_STOP"


def test_at_half_the_stop_locks_35_percent_of_the_move():
    s, ev = advance(LONG, start(LONG), high=103.0, low=100.2)
    assert s.stop == pytest.approx(102.1) and s.target == 106.0 and s.stage == 2
    assert ev == ("BREAKEVEN", "LOCKED", "STOP_MOVED")


def test_the_target_holds_until_80_percent_then_moves_out_and_the_stop_trails():
    s, _ = advance(LONG, start(LONG), high=104.7, low=100.2)  # 78%
    assert s.target == 106.0 and s.stage == 2
    s, ev = advance(LONG, s, high=104.8, low=104.0)  # 80%
    assert s.stage == 3 and s.stop == pytest.approx(100 + 0.6 * 6)
    assert s.target == pytest.approx(109.0) and ev == ("TRAILING", "STOP_MOVED", "TARGET_MOVED")
    s, _ = advance(LONG, s, high=108.0, low=105.0)  # 133%: stop at 113% of the move
    assert s.stop == pytest.approx(100 + (8 / 6 - 0.2) * 6) and s.target == pytest.approx(112.0)


def test_a_fixed_target_stays_where_it_is():
    p = RatchetParams(extend=False)
    s, ev = advance(LONG, start(LONG), high=105.5, low=100.2, p=p)
    assert s.target == 106.0 and "TARGET_MOVED" not in ev and s.stage == 3
    assert exit_on_bar(LONG, s, 105.4, 106.2, 105.0) == ("TARGET", 106.0)


def test_a_short_is_the_mirror_image():
    s, _ = advance(SHORT, start(SHORT), high=99.8, low=98.5)
    assert s.stop == pytest.approx(99.6) and s.best == 98.5 and s.stage == 1
    s, _ = advance(SHORT, s, high=98.0, low=95.2)
    assert (
        s.stage == 3 and s.stop == pytest.approx(100 - 0.6 * 6) and s.target == pytest.approx(91.0)
    )


def test_levels_never_move_against_the_holder():
    rnd = random.Random(7)
    for plan in (LONG, SHORT):
        for extend in (True, False):
            p, s, px = RatchetParams(extend=extend), start(plan), plan.entry
            for _ in range(400):
                px += rnd.uniform(-0.6, 0.65) * plan.side
                hi, lo = px + rnd.uniform(0, 0.4), px - rnd.uniform(0, 0.4)
                n, _ = advance(plan, s, hi, lo, p)
                assert plan.side * (n.stop - s.stop) >= 0
                assert plan.side * (n.target - s.target) >= 0
                assert n.stage >= s.stage
                assert (
                    plan.side * (n.best - n.stop) >= p.min_gap * plan.move - 1e-9
                    or n.stop == s.stop
                )
                s = n


def test_a_large_cost_cannot_put_the_stop_on_top_of_the_price():
    dear = plan_from_risk(1, 100.0, 2.0, 3.0, cost=1.5)  # breakeven would be 101.8
    s, _ = advance(dear, start(dear), high=101.5, low=100.0)
    assert s.stop == pytest.approx(101.5 - 0.1 * 6)  # held 10% of the move below the best price


def test_exits_on_a_bar_stop_first_and_gaps_at_the_open():
    s0 = start(LONG)
    assert exit_on_bar(LONG, s0, 100.0, 100.5, 98.6) is None
    assert exit_on_bar(LONG, s0, 100.0, 100.5, 98.0) == ("STOP", 98.0)
    assert exit_on_bar(LONG, s0, 97.5, 99.0, 97.0) == ("GAP_STOP", 97.5)
    assert exit_on_bar(LONG, s0, 100.0, 106.0, 99.0) == ("TARGET", 106.0)
    assert exit_on_bar(LONG, s0, 106.4, 107.0, 106.0) == ("GAP_TARGET", 106.4)
    assert exit_on_bar(LONG, s0, 100.0, 106.5, 97.9) == ("STOP", 98.0)  # both touched
    locked, _ = advance(LONG, s0, high=103.0, low=100.0)
    assert exit_on_bar(LONG, locked, 102.5, 102.6, 102.0) == ("LOCK_STOP", pytest.approx(102.1))
    assert exit_on_bar(SHORT, start(SHORT), 100.0, 102.0, 99.0) == ("STOP", 102.0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"side": 0, "entry": 100, "stop": 98, "target": 106},
        {"side": 1, "entry": 100, "stop": 101, "target": 106},
        {"side": 1, "entry": 100, "stop": 98, "target": 99},
        {"side": -1, "entry": 100, "stop": 98, "target": 94},
        {"side": 1, "entry": 100, "stop": 98, "target": 106, "cost": -1},
    ],
)
def test_a_plan_that_makes_no_sense_is_refused(kwargs):
    with pytest.raises(ValueError):
        Plan(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [{"be_at": 0.6}, {"lock": 0.6}, {"min_gap": 0.3}, {"extend_by": 0}, {"extend_at": 1.0}],
)
def test_parameters_out_of_order_are_refused(kwargs):
    with pytest.raises(ValueError):
        RatchetParams(**kwargs)
    with pytest.raises(ValueError):
        plan_from_risk(1, 100.0, 0.0, 3.0)
