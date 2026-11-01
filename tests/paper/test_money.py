"""Rupees -> integer paise at the boundary (ADR-001 amendment 5)."""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pytest

from onelot.paper.money import format_rupees, to_decimal, to_paise


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


# ── integer paise -> a rupee string for display (US-030) ─────────────────────


@pytest.mark.parametrize(
    ("paise", "text"),
    [
        (100030, "1000.30"),
        (-1205, "-12.05"),
        (0, "0.00"),
        (5, "0.05"),
        (-5, "-0.05"),  # a sign below one rupee is kept, never "0.05"
        (100, "1.00"),
        (np.int64(31234), "312.34"),
        (9_223_372_036_854_775_807, "92233720368547758.07"),  # BIGINT max: exact
        (10**20, "1000000000000000000.00"),  # never scientific notation
        (-(10**30) - 1, "-10000000000000000000000000000.01"),  # beyond Decimal's 28 digits
    ],
)
def test_format_rupees(paise, text):
    got = format_rupees(paise)
    assert got == text
    assert type(got) is str


def test_format_rupees_keeps_none_as_none():
    assert format_rupees(None) is None


@pytest.mark.parametrize(
    "bad", [True, np.bool_(True), 1.5, 100.0, "100", Decimal("1.5"), float("nan")]
)
def test_format_rupees_rejects_anything_but_integer_paise(bad):
    with pytest.raises(TypeError):
        format_rupees(bad)


@pytest.mark.parametrize("rupees", ["0.01", "1000.30", "-12.05", "45210.35", "-0.99"])
def test_format_rupees_round_trips_to_paise(rupees):
    assert format_rupees(to_paise(rupees)) == rupees
