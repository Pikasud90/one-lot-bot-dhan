"""The pure management helpers shared by the paper book and (from US-025) the desk.

US-002 AC8 / ADR-001 amendment 1. No I/O, no clock: every input is explicit. Prices may be
rupees (the desk) or integer paise (the paper book); the helpers are unit-agnostic, so the
tick passed to `ratchet_stop` must be in the same unit as the prices.
"""

from __future__ import annotations

import datetime as dt
import math

import pytest

from onelot.clock import IST
from onelot.desk.rules import (
    EXIT_PRECEDENCE,
    bars_held,
    exit_priority,
    r_multiple,
    ratchet_stop,
    session_due,
    sessions_held,
)

THU = dt.date(2026, 10, 29)
FRI = dt.date(2026, 10, 30)
UTC = dt.timezone.utc


def ist(d: dt.date, hh: int, mm: int, ss: int = 0) -> dt.datetime:
    return dt.datetime.combine(d, dt.time(hh, mm, ss), tzinfo=IST)


# ── ratchet_stop ───────────────────────────────────────────────────────────────


class TestRatchetStop:
    def test_long_moves_the_stop_up_to_close_minus_k_atr(self) -> None:
        assert ratchet_stop("long", 99_500, 101_000, 400.0, 1.5) == 100_400

    def test_short_moves_the_stop_down_to_close_plus_k_atr(self) -> None:
        assert ratchet_stop("short", 100_500, 99_000, 400.0, 1.5) == 99_600

    def test_never_loosens_a_long(self) -> None:
        assert ratchet_stop("long", 100_500, 100_600, 400.0, 1.5) == 100_500

    def test_never_loosens_a_short(self) -> None:
        assert ratchet_stop("short", 99_500, 99_400, 400.0, 1.5) == 99_500

    @pytest.mark.parametrize("atr", [None, 0.0, 0, math.nan, -3.0, math.inf])
    def test_an_unusable_atr_disables_the_trail(self, atr: float | None) -> None:
        assert ratchet_stop("long", 99_500, 101_000, atr, 1.5) == 99_500
        assert ratchet_stop("short", 100_500, 99_000, atr, 1.5) == 100_500

    @pytest.mark.parametrize("mult", [0.0, -1.0, math.nan, math.inf])
    def test_an_unusable_multiplier_disables_the_trail(self, mult: float) -> None:
        assert ratchet_stop("long", 99_500, 101_000, 400.0, mult) == 99_500

    def test_decimal_exact_where_floats_would_drift(self) -> None:
        # 1000 - 1.5 x 0.8 = 998.80, on the 0.05 grid. In floats 998.8 / 0.05 is
        # 19975.999..., which floors a whole tick low to 998.75.
        assert ratchet_stop("long", 990.0, 1000.0, 0.8, 1.5, tick=0.05) == 998.8
        assert ratchet_stop("short", 1010.0, 1000.0, 0.8, 1.5, tick=0.05) == 1001.2

    def test_tick_snaps_a_long_stop_down_against_the_trader(self) -> None:
        # candidate 100_400 - 3 = 100_397 -> floor to the 5-paise grid
        assert ratchet_stop("long", 99_500, 101_000, 402.0, 1.5, tick=5) == 100_395

    def test_tick_snaps_a_short_stop_up_against_the_trader(self) -> None:
        # candidate 99_600 + 3 = 99_603 -> ceil to the 5-paise grid
        assert ratchet_stop("short", 100_500, 99_000, 402.0, 1.5, tick=5) == 99_605

    def test_snapping_never_loosens_either(self) -> None:
        # candidate 100_501 snaps to 100_500 == stop: unchanged, not 100_495
        assert ratchet_stop("long", 100_500, 101_101, 400.0, 1.5, tick=5) == 100_500

    def test_rupee_ticks_snap_on_the_decimal_grid(self) -> None:
        assert ratchet_stop("long", 995.0, 1010.07, 4.0, 1.5, tick=0.05) == 1004.05

    def test_a_non_finite_close_is_rejected_not_propagated(self) -> None:
        with pytest.raises(ValueError, match="close"):
            ratchet_stop("long", 99_500, math.nan, 400.0, 1.5)

    def test_an_unknown_direction_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="direction"):
            ratchet_stop("up", 99_500, 101_000, 400.0, 1.5)

    def test_returns_a_float(self) -> None:
        assert isinstance(ratchet_stop("long", 99_500, 101_000, 400.0, 1.5, tick=5), float)


# ── r_multiple ─────────────────────────────────────────────────────────────────


class TestRMultiple:
    def test_long(self) -> None:
        assert r_multiple("long", 100_000, 99_500, 101_000) == 2.0
        assert r_multiple("long", 100_000, 99_500, 99_500) == -1.0

    def test_short(self) -> None:
        assert r_multiple("short", 100_000, 100_500, 99_250) == 1.5
        assert r_multiple("short", 100_000, 100_500, 100_500) == -1.0

    def test_exact_on_paise(self) -> None:
        # 150 / 500 is the same double as the literal 0.3: activation ties compare equal
        assert r_multiple("long", 100_000, 99_500, 100_150) == 0.3

    def test_exact_on_rupees_too(self) -> None:
        # floats: (1000.15 - 1000.0) / 0.5 = 0.29999999999995453, a missed activation tie
        assert r_multiple("long", 1000.0, 999.5, 1000.15) == 0.3

    def test_zero_risk_is_undefined_not_a_huge_number(self) -> None:
        with pytest.raises(ValueError, match="risk"):
            r_multiple("long", 100_000, 100_000, 100_500)

    def test_a_stop_on_the_wrong_side_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="stop"):
            r_multiple("long", 100_000, 100_500, 101_000)
        with pytest.raises(ValueError, match="stop"):
            r_multiple("short", 100_000, 99_500, 99_000)

    def test_non_finite_inputs_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            r_multiple("long", 100_000, 99_500, math.nan)

    def test_an_unknown_direction_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="direction"):
            r_multiple("flat", 100_000, 99_500, 101_000)


# ── bars_held ──────────────────────────────────────────────────────────────────


class TestBarsHeld:
    def test_whole_bars_of_the_timeframe(self) -> None:
        opened = ist(THU, 10, 0)
        assert bars_held(opened, ist(THU, 10, 4, 59), 5) == 0
        assert bars_held(opened, ist(THU, 10, 5), 5) == 1
        assert bars_held(opened, ist(THU, 10, 29), 5) == 5

    def test_never_negative(self) -> None:
        assert bars_held(ist(THU, 10, 0), ist(THU, 9, 30), 5) == 0

    def test_instant_not_wall_clock_face(self) -> None:
        opened = ist(THU, 10, 0)  # 04:30 UTC
        now_utc = dt.datetime.combine(THU, dt.time(4, 45), tzinfo=UTC)
        assert bars_held(opened, now_utc, 5) == 3

    def test_naive_times_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="aware"):
            bars_held(dt.datetime(2026, 10, 29, 10, 0), ist(THU, 10, 5), 5)

    @pytest.mark.parametrize("tf", [0, -5])
    def test_a_non_positive_timeframe_is_rejected(self, tf: int) -> None:
        with pytest.raises(ValueError, match="tf_min"):
            bars_held(ist(THU, 10, 0), ist(THU, 10, 5), tf)


# ── sessions_held ──────────────────────────────────────────────────────────────


class TestSessionsHeld:
    def test_distinct_session_dates(self) -> None:
        assert sessions_held([THU, THU, FRI, THU]) == 2

    def test_none(self) -> None:
        assert sessions_held([]) == 0


# ── session_due ────────────────────────────────────────────────────────────────


class TestSessionDue:
    def test_same_day(self) -> None:
        exit_time = dt.time(15, 15)
        assert not session_due(ist(THU, 15, 14, 59), exit_time, THU)
        assert session_due(ist(THU, 15, 15), exit_time, THU)
        assert session_due(ist(THU, 15, 40), exit_time, THU)

    def test_a_later_day_is_due_even_before_the_exit_time(self) -> None:
        # date-aware, unlike the desk's `now.time() >= exit_time`
        assert session_due(ist(FRI, 9, 20), dt.time(15, 15), THU)

    def test_an_earlier_day_is_not_due(self) -> None:
        assert not session_due(ist(THU, 15, 20), dt.time(15, 15), FRI)

    def test_judged_in_ist_whatever_the_zone_of_now(self) -> None:
        # 09:45 UTC is 15:15 IST
        now_utc = dt.datetime.combine(THU, dt.time(9, 45), tzinfo=UTC)
        assert session_due(now_utc, dt.time(15, 15), THU)
        assert not session_due(now_utc - dt.timedelta(seconds=1), dt.time(15, 15), THU)

    def test_naive_now_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="aware"):
            session_due(dt.datetime(2026, 10, 29, 15, 20), dt.time(15, 15), THU)


# ── exit_priority ──────────────────────────────────────────────────────────────


class TestExitPriority:
    def test_the_documented_order(self) -> None:
        assert EXIT_PRECEDENCE == (
            "STOP",
            "TRAIL_STOP",
            "TARGET",
            "SESSION_END",
            "SIGNAL_EXIT",
            "OPPOSITE_SIGNAL",
            "TIME_STOP",
        )

    @pytest.mark.parametrize(
        ("reasons", "winner"),
        [
            (["TARGET", "STOP"], "STOP"),
            (["TARGET", "TRAIL_STOP"], "TRAIL_STOP"),
            (["SESSION_END", "TARGET"], "TARGET"),
            (["SIGNAL_EXIT", "SESSION_END"], "SESSION_END"),
            (["OPPOSITE_SIGNAL", "SIGNAL_EXIT"], "SIGNAL_EXIT"),
            (["TIME_STOP", "OPPOSITE_SIGNAL"], "OPPOSITE_SIGNAL"),
            (["TIME_STOP"], "TIME_STOP"),
            (["TIME_STOP", "TIME_STOP", "TARGET"], "TARGET"),
        ],
    )
    def test_highest_wins(self, reasons: list[str], winner: str) -> None:
        assert exit_priority(reasons) == winner

    def test_any_iterable(self) -> None:
        assert exit_priority(r for r in ("TIME_STOP", "SESSION_END")) == "SESSION_END"

    def test_nothing_due_is_an_error_not_a_default(self) -> None:
        with pytest.raises(ValueError, match="no exit reason"):
            exit_priority([])

    @pytest.mark.parametrize("bad", ["SQUARE_OFF", "NO_FILL", "stop", "EOD"])
    def test_an_unknown_reason_is_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError, match="unknown exit reason"):
            exit_priority(["STOP", bad])
