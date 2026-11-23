"""US-079: `manage/live.unit_cost`, the rule's costs per unit of the managed price."""

from __future__ import annotations

import logging
import math

import pytest

from onelot.manage.live import DELTA_FLOOR, unit_cost

COSTS, LOT = 78.0, 30  # rupees round trip for one lot of 30: 2.60 a unit of premium


def test_a_stock_is_per_share() -> None:
    assert unit_cost(41.2, 1) == pytest.approx(41.2)


def test_a_call_is_per_unit_of_premium_over_its_delta() -> None:
    assert unit_cost(COSTS, LOT, 0.65, option=True) == pytest.approx(2.6 / 0.65)


def test_a_puts_negative_delta_counts_as_its_size() -> None:
    put = unit_cost(COSTS, LOT, -0.65, option=True)
    assert put == pytest.approx(unit_cost(COSTS, LOT, 0.65, option=True)) and put > 0


@pytest.mark.parametrize("delta", [None, math.nan, math.inf, -math.inf, 0.0])
def test_no_usable_delta_uses_the_floor_and_warns(delta, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="onelot.manage.live"):
        got = unit_cost(COSTS, LOT, delta, option=True)
    assert got == pytest.approx(2.6 / DELTA_FLOOR) and math.isfinite(got)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_a_tiny_delta_is_floored_without_a_warning(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="onelot.manage.live"):
        assert unit_cost(COSTS, LOT, 0.01, option=True) == pytest.approx(2.6 / DELTA_FLOOR)
    assert not caplog.records


@pytest.mark.parametrize("costs", [math.nan, 0.0, -5.0])
def test_no_costs_is_zero(costs: float) -> None:
    assert unit_cost(costs, LOT, 0.65, option=True) == 0.0
