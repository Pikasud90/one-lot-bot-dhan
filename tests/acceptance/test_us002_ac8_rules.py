"""US-002 AC8 (the shared rules): `onelot/desk/rules.py` holds six PURE helpers that the paper
book uses now and the desk adopts later (US-025, behind the characterisation tests).

    ratchet_stop(direction, stop, last_close, atr, mult) -> float
        the trailing stop: close -/+ mult x atr for a long/short, applied only if it TIGHTENS the
        stop; an atr that is None, 0 or NaN (and a mult of 0) disables it: the stop is returned
    r_multiple(direction, entry, initial_stop, price) -> float
        direction-signed move from entry in units of the initial risk |entry - initial_stop|
    bars_held(opened_at, now, tf_min) -> int
        whole strategy bars elapsed: floor(whole minutes since opened_at / tf_min), never below 0
    sessions_held(session_dates) -> int
        the number of DISTINCT session dates
    session_due(now, exit_time, session_date) -> bool
        True from exit_time on session_date and at any time on a LATER date (the desk's own
        check, `now.time() >= exit_time`, is not date-aware); exit_time is an IST wall time
    exit_priority(reasons) -> str
        the reason that wins: STOP > TARGET > SESSION_END > SIGNAL_EXIT > OPPOSITE_SIGNAL > TIME_STOP

The numbers in the examples are the desk's own arithmetic (`Desk._manage_positions`).
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import itertools
import math
from pathlib import Path

import pytest
import us001_helpers as H
import us002_helpers as U
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

PROP = settings(
    max_examples=60,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=list(HealthCheck),
)
NAN = float("nan")
ORDER = ["STOP", "TARGET", "SESSION_END", "SIGNAL_EXIT", "OPPOSITE_SIGNAL", "TIME_STOP"]
RULES_PY = Path(U.H.ROOT) / "src" / "onelot" / "desk" / "rules.py"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


class TestTheModuleIsTheContract:
    @pytest.mark.parametrize(
        "name, params",
        [
            ("ratchet_stop", ["direction", "stop", "last_close", "atr", "mult"]),
            ("r_multiple", ["direction", "entry", "initial_stop", "price"]),
            ("bars_held", ["opened_at", "now", "tf_min"]),
            ("sessions_held", ["session_dates"]),
            ("session_due", ["now", "exit_time", "session_date"]),
            ("exit_priority", ["reasons"]),
        ],
    )
    def test_each_helper_exists_with_the_contract_parameters(self, name, params):
        """The contract parameters come first, in order; an extra one is allowed only as an
        optional keyword-only refinement (ratchet_stop(..., tick=None))."""
        fn = getattr(U.rules_mod(), name)
        sig = list(inspect.signature(fn).parameters.values())
        assert [p.name for p in sig[: len(params)]] == params
        assert all(p.kind is not p.KEYWORD_ONLY for p in sig[: len(params)])
        for extra in sig[len(params) :]:
            assert extra.kind is extra.KEYWORD_ONLY and extra.default is not extra.empty

    def test_the_module_is_pure_it_imports_no_io_and_never_reads_the_clock(self):
        tree = ast.parse(RULES_PY.read_text(encoding="utf-8"))
        banned = {"socket", "sqlite3", "requests", "httpx", "urllib", "sqlalchemy", "subprocess"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                roots = {(node.module or "").split(".")[0]}
            else:
                continue
            assert not roots & banned, f"rules.py must be pure, it imports {roots & banned}"
        called = {
            n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
        }
        assert not called & {"now", "utcnow", "today", "now_ist", "time", "open", "print"}, called


class TestRatchetStop:
    def test_a_long_trail_is_the_close_minus_mult_times_atr(self):
        assert U.rules_mod().ratchet_stop("long", 990.0, 1020.0, 4.0, 1.5) == 1014.0

    def test_a_short_trail_is_the_close_plus_mult_times_atr(self):
        assert U.rules_mod().ratchet_stop("short", 1010.0, 980.0, 4.0, 1.5) == 986.0

    @pytest.mark.parametrize(
        "direction, stop, close",
        [("long", 1014.0, 1010.0), ("short", 986.0, 990.0)],
        ids=["long", "short"],
    )
    def test_the_stop_never_loosens(self, direction, stop, close):
        assert U.rules_mod().ratchet_stop(direction, stop, close, 4.0, 1.5) == stop

    @pytest.mark.parametrize("direction, stop", [("long", 1014.0), ("short", 986.0)])
    def test_a_candidate_equal_to_the_stop_leaves_it_alone(self, direction, stop):
        close = stop + 6.0 if direction == "long" else stop - 6.0
        assert U.rules_mod().ratchet_stop(direction, stop, close, 4.0, 1.5) == stop

    @pytest.mark.parametrize("atr", [None, 0.0, NAN], ids=["none", "zero", "nan"])
    @pytest.mark.parametrize(
        "direction, stop, close", [("long", 990.0, 1100.0), ("short", 1010.0, 900.0)]
    )
    def test_a_missing_zero_or_nan_atr_disables_the_trail(self, atr, direction, stop, close):
        assert U.rules_mod().ratchet_stop(direction, stop, close, atr, 1.5) == stop

    @pytest.mark.parametrize("mult", [0.0, -1.0])
    def test_a_non_positive_multiple_disables_the_trail(self, mult):
        assert U.rules_mod().ratchet_stop("long", 990.0, 1100.0, 4.0, mult) == 990.0

    @pytest.mark.parametrize("direction", ["long", "short"])
    def test_a_nan_close_never_poisons_the_stop(self, direction):
        """Either defensible answer: refuse the bad price, or leave the stop where it was."""
        try:
            out = U.rules_mod().ratchet_stop(direction, 990.0, NAN, 4.0, 1.5)
        except ValueError:
            return
        assert out == 990.0 and math.isfinite(out)

    @PROP
    @given(
        direction=st.sampled_from(["long", "short"]),
        stop=st.integers(100, 100_000),
        close=st.integers(100, 100_000),
        atr=st.sampled_from([None, 0.0, 0.5, 1.0, 4.0, 25.0]),
        mult=st.sampled_from([0.0, 0.5, 1.0, 1.5, 3.0]),
    )
    def test_property_it_only_ever_tightens_and_is_idempotent(
        self, direction, stop, close, atr, mult
    ):
        rm = U.rules_mod()
        once = rm.ratchet_stop(direction, float(stop), float(close), atr, mult)
        if direction == "long":
            assert once >= stop
        else:
            assert once <= stop
        assert rm.ratchet_stop(direction, once, float(close), atr, mult) == once


class TestRMultiple:
    @pytest.mark.parametrize(
        "direction, entry, stop, price, expected",
        [
            ("long", 1000.0, 990.0, 1020.0, 2.0),
            ("long", 1000.0, 990.0, 990.0, -1.0),
            ("long", 1000.0, 990.0, 1000.0, 0.0),
            ("long", 1000.0, 990.0, 985.0, -1.5),
            ("short", 1000.0, 1010.0, 980.0, 2.0),
            ("short", 1000.0, 1010.0, 1010.0, -1.0),
            ("short", 1000.0, 1010.0, 1000.0, 0.0),
            ("short", 1000.0, 1010.0, 1015.0, -1.5),
        ],
    )
    def test_signed_move_in_units_of_the_initial_risk(
        self, direction, entry, stop, price, expected
    ):
        assert U.rules_mod().r_multiple(direction, entry, stop, price) == pytest.approx(expected)

    def test_the_risk_is_the_initial_stop_not_a_trailed_one(self):
        """The third argument is the INITIAL stop: the unit of R never moves with the trail."""
        assert U.rules_mod().r_multiple("long", 1000.0, 990.0, 1010.0) == pytest.approx(1.0)

    def test_a_zero_risk_never_divides_by_zero(self):
        """Either defensible answer: a clear ValueError, or a finite float (never ZeroDivision)."""
        try:
            out = U.rules_mod().r_multiple("long", 1000.0, 1000.0, 1001.0)
        except ValueError:
            return
        assert isinstance(out, float) and math.isfinite(out)

    @PROP
    @given(
        entry=st.integers(2_000, 90_000),
        risk=st.integers(1, 1_000),
        k=st.integers(-8, 8),
        direction=st.sampled_from(["long", "short"]),
    )
    def test_property_a_move_of_k_risks_is_k_r_and_shorts_mirror_longs(
        self, entry, risk, k, direction
    ):
        rm = U.rules_mod()
        sign = 1 if direction == "long" else -1
        stop = entry - sign * risk
        price = entry + sign * k * risk
        assert rm.r_multiple(direction, float(entry), float(stop), float(price)) == pytest.approx(k)


class TestBarsHeld:
    T0 = H.at(10, 0)

    @pytest.mark.parametrize(
        "elapsed_s, tf, expected",
        [
            (0, 10, 0),
            (599, 10, 0),  # 9 whole minutes
            (600, 10, 1),
            (1799, 10, 2),  # 29 whole minutes: the third bar is not done
            (1800, 10, 3),
            (7200, 5, 24),
            (59, 1, 0),
            (60, 1, 1),
        ],
    )
    def test_whole_strategy_bars_since_the_open(self, elapsed_s, tf, expected):
        now = self.T0 + dt.timedelta(seconds=elapsed_s)
        assert U.rules_mod().bars_held(self.T0, now, tf) == expected

    def test_a_clock_before_the_open_holds_zero_bars(self):
        assert U.rules_mod().bars_held(self.T0, self.T0 - dt.timedelta(minutes=30), 10) == 0

    def test_a_zero_timeframe_does_not_divide_by_zero(self):
        """The desk treats 0 as 1 (`max(1, tf_min)`); a clear ValueError is just as defensible."""
        try:
            out = U.rules_mod().bars_held(self.T0, self.T0 + dt.timedelta(minutes=7), 0)
        except ValueError:
            return
        assert out == 7

    def test_it_returns_an_int(self):
        assert isinstance(
            U.rules_mod().bars_held(self.T0, self.T0 + dt.timedelta(hours=1), 10), int
        )

    def test_the_result_is_the_same_whatever_the_timezone_of_the_instants(self):
        utc = dt.timezone.utc
        a = U.rules_mod().bars_held(self.T0, self.T0 + dt.timedelta(minutes=45), 10)
        b = U.rules_mod().bars_held(
            self.T0.astimezone(utc), (self.T0 + dt.timedelta(minutes=45)).astimezone(utc), 10
        )
        assert a == b == 4


class TestSessionsHeld:
    D = dt.date(2026, 10, 26)

    def test_it_counts_distinct_dates(self):
        days = [self.D + dt.timedelta(days=i) for i in range(4)]
        assert U.rules_mod().sessions_held(days) == 4

    def test_repeated_dates_count_once(self):
        days = [self.D, self.D, self.D + dt.timedelta(days=1), self.D]
        assert U.rules_mod().sessions_held(days) == 2

    def test_nothing_is_zero_sessions(self):
        assert U.rules_mod().sessions_held([]) == 0

    def test_a_generator_is_accepted(self):
        assert U.rules_mod().sessions_held(self.D + dt.timedelta(days=i) for i in range(3)) == 3


class TestSessionDue:
    D = H.DAY  # Thursday 2026-10-29
    T = dt.time(15, 15)

    def due(self, now):
        return U.rules_mod().session_due(now, self.T, self.D)

    @pytest.mark.parametrize(
        "hh, mm, ss, expected",
        [
            (9, 15, 0, False),
            (15, 14, 59, False),
            (15, 15, 0, True),  # the boundary itself is due
            (15, 15, 1, True),
            (15, 29, 0, True),
            (23, 59, 59, True),
        ],
    )
    def test_on_the_session_day_it_is_due_from_the_exit_time(self, hh, mm, ss, expected):
        assert self.due(H.at(hh, mm, ss)) is expected

    @pytest.mark.parametrize("hh, mm", [(0, 0), (9, 15), (10, 0), (15, 14)])
    def test_on_a_later_day_it_is_due_at_any_time_of_day(self, hh, mm):
        """The desk's `now.time() >= exit_time` says False here: that is the bug AC5 closes."""
        assert self.due(H.at(hh, mm, day=H.FRIDAY)) is True
        assert self.due(H.at(hh, mm, day=H.MONDAY)) is True

    def test_on_an_earlier_day_it_is_not_due(self):
        assert self.due(H.at(15, 30, day=self.D - dt.timedelta(days=1))) is False

    def test_a_custom_exit_time_is_honoured(self):
        rm = U.rules_mod()
        assert rm.session_due(H.at(14, 45), dt.time(14, 45), self.D) is True
        assert rm.session_due(H.at(14, 44, 59), dt.time(14, 45), self.D) is False

    def test_a_non_ist_clock_is_converted_to_ist_first(self):
        """15:30 IST is 10:00 UTC the same day; 09:44 UTC is 15:14 IST."""
        utc = dt.timezone.utc
        assert self.due(dt.datetime(2026, 10, 29, 10, 0, tzinfo=utc)) is True
        assert self.due(dt.datetime(2026, 10, 29, 9, 44, tzinfo=utc)) is False
        assert self.due(dt.datetime(2026, 10, 29, 9, 45, tzinfo=utc)) is True

    def test_the_result_is_a_bool(self):
        assert isinstance(self.due(H.at(16, 0)), bool)


class TestExitPriority:
    @pytest.mark.parametrize("hi, lo", list(itertools.combinations(ORDER, 2)))
    def test_every_pair_is_ordered_as_the_story_says(self, hi, lo):
        rm = U.rules_mod()
        assert rm.exit_priority([hi, lo]) == hi
        assert rm.exit_priority([lo, hi]) == hi

    @PROP
    @given(
        subset=st.sets(st.sampled_from(ORDER), min_size=1),
        seed=st.randoms(use_true_random=False),
    )
    def test_property_the_winner_is_the_highest_ranked_in_any_order(self, subset, seed):
        items = list(subset) + list(subset)[:1]  # a repeated reason changes nothing
        seed.shuffle(items)
        assert U.rules_mod().exit_priority(items) == min(subset, key=ORDER.index)

    def test_a_single_reason_wins_alone(self):
        assert U.rules_mod().exit_priority(["TIME_STOP"]) == "TIME_STOP"

    def test_any_iterable_is_accepted(self):
        assert U.rules_mod().exit_priority(r for r in ("TIME_STOP", "STOP")) == "STOP"
        assert U.rules_mod().exit_priority(("OPPOSITE_SIGNAL", "SIGNAL_EXIT")) == "SIGNAL_EXIT"

    def test_nothing_due_is_an_error_not_a_made_up_reason(self):
        with pytest.raises(ValueError):
            U.rules_mod().exit_priority([])

    def test_an_unknown_reason_is_an_error_not_silently_ranked(self):
        with pytest.raises((ValueError, KeyError)):
            U.rules_mod().exit_priority(["STOP", "SQUARE_OFF"])
