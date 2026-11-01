"""US-003 BUG-4: one rule for the confidence number on every surface (desk title, phone)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from onelot.notify.base import shown_confidence


@pytest.mark.parametrize(
    ("score", "shown"),
    [
        (0.0, 0),
        (0.5, 0),
        (44.9, 44),
        (45.0, 45),
        (56.5, 56),
        (64.99, 64),
        (65.0, 65),
        (72.5, 72),
        (100, 100),
        (Decimal("57.5"), 57),
    ],
)
def test_the_shown_number_is_the_floored_score(score, shown):
    assert shown_confidence(score) == shown


@pytest.mark.parametrize(("score", "shown"), [(-3.2, 0), (100.4, 100), (1e300, 100)])
def test_out_of_range_scores_are_clamped_to_0_100(score, shown):
    """Keeps the phone budget (three digits at most) and the label in agreement."""
    assert shown_confidence(score) == shown


@pytest.mark.parametrize(
    "score", [None, float("nan"), float("inf"), float("-inf"), True, False, "72", [72]], ids=repr
)
def test_a_score_that_is_not_a_finite_number_has_no_shown_number(score):
    assert shown_confidence(score) is None
