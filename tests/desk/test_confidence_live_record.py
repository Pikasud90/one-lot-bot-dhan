"""C1: the live-record part of the confidence score is neutral at 50 for an even record."""

from __future__ import annotations

import pytest

from onelot.desk.audit import confidence


def live_part(hit_rate: float, avg_r: float, n: int = 20) -> float:
    live = {"n": n, "hit_rate": hit_rate, "avg_r": avg_r}
    return confidence(50, None, live, None)["parts"]["live_record"]


def test_half_winners_and_zero_r_is_neutral():
    assert live_part(50, 0.0) == 50.0


def test_a_losing_record_reads_below_neutral():
    assert live_part(38, -0.10) == 37.8


def test_a_winning_record_reads_above_neutral():
    assert live_part(70, 0.30) == 77.0


def test_rises_with_hit_rate_and_with_average_r():
    rates = [0, 10, 25, 38, 50, 62, 75, 90, 100]
    rs = [-0.5, -0.2, -0.1, 0.0, 0.1, 0.3, 0.8]
    for r in rs:
        vals = [live_part(h, r) for h in rates]
        assert vals == sorted(vals) and vals[0] < vals[-1]
    for h in rates:
        vals = [live_part(h, r) for r in rs]
        assert vals == sorted(vals) and vals[0] < vals[-1]


def test_stays_within_zero_and_hundred_at_the_corners():
    assert live_part(0, -2) == 0.0
    assert live_part(100, 2) == 100.0
    assert 0.0 <= live_part(100, -2) <= 100.0
    assert 0.0 <= live_part(0, 2) <= 100.0


def test_fewer_than_five_alerts_is_neutral_with_the_old_note():
    parts = confidence(50, None, {"n": 4, "hit_rate": 100, "avg_r": 2}, None)["parts"]
    assert parts["live_record"] == 50.0
    assert parts["live_note"] == "fewer than 5 archived alerts for this strategy"


@pytest.mark.parametrize("n", [5, 6])
def test_five_alerts_is_enough(n):
    assert live_part(70, 0.30, n=n) == 77.0
