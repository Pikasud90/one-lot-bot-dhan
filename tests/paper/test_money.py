"""Rupees -> integer paise at the boundary (ADR-001 amendment 5)."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from onelot.paper.money import to_decimal, to_paise


@pytest.mark.parametrize(
    ("value", "paise"),
    [
        (1000.3, 100030),
        (1000, 100000),
        (np.float64(45210.35), 4521035),
        (np.int64(7), 700),
        (Decimal("312.34"), 31234),
        (" 0.05 ", 5),
        (0.1 + 0.2, 30),  # float noise does not leak: 0.30000000000000004 -> 30
        (0.005, 0),  # half-even: 0.5 paise -> 0
        (0.015, 2),  # half-even: 1.5 paise -> 2
        (-12.345, -1234),  # half-even on negatives too
    ],
)
def test_to_paise(value, paise):
    got = to_paise(value)
    assert got == paise
    assert type(got) is int


@pytest.mark.parametrize(
    "bad", [None, True, float("nan"), float("inf"), np.float64("nan"), "abc", "NaN", [1]]
)
def test_non_prices_are_rejected(bad):
    with pytest.raises(ValueError):
        to_decimal(bad)


def test_floats_use_their_shortest_repr():
    assert to_decimal(1000.3) == Decimal("1000.3")
