"""US-002 AC3 on `manage_step`: the trailing stop.

Given trail_atr_mult above 0 and a bar CLOSE at or beyond trail_activate_r in profit, the stop
ratchets at bar close from that close and applies from the NEXT bar (never within the same
bar), never loosens, uses the ATR frozen at alert time (None, 0 or NaN disables the trail; no
fallback to a live ATR), and a close by a moved stop is TRAIL_STOP iff the stop ever moved
beyond the initial stop.

The worked numbers (paise): long entry 100030, initial stop 99000, so one R is 1030. With
atr 4.00 (rupees) and a 1.5 multiple the trail distance is exactly 6.00 = 600 paise, and
activation (1.0 R) needs a close of at least 101060.

    bar 1  10:06  (100000, 101200, 100100, 101100)   close = +1.04 R -> new stop 100500
                  its LOW (100100) is below 100500: the new stop must NOT act inside bar 1

Activation is judged on the CLOSE (the Lab judges it on the bar's best price: a deliberate
difference). Trailed stops in these tests sit on the 5-paise tick grid, so every defensible
rounding of the level agrees.
"""

from __future__ import annotations

import datetime as dt
import math

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
BAR1 = (100000, 101200, 100100, 101100)  # closes +1.04 R: the trail arms at 100500
BAR1_SHORT = (100000, 100100, 98800, 98900)  # closes +1.04 R for the short: the trail arms at 99500
START = H.at(10, 6)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


def run_long(rows, rules=None, state=None, now=None):
    return U.run(state or U.mk_state(), U.pbars(START, rows), rules or U.trail_rules(), now)


def run_short(rows, rules=None, now=None):
    return U.run(U.mk_state("short"), U.pbars(START, rows), rules or U.trail_rules(), now)


class TestTheRatchet:
    def test_it_ratchets_at_the_close_from_that_close(self):
        state, events = run_long([BAR1])
        assert U.close_of(events) is None
        assert state.stop_paise == 101100 - 600 == 100500
        assert state.trail_active is True

    def test_and_applies_from_the_next_bar_never_within_the_same_bar(self):
        state, events = run_long([BAR1])
        assert U.close_of(events) is None, (
            "bar 1's low (100100) is below the NEW stop: it must not act"
        )

    def test_the_next_bar_touching_the_new_stop_closes_trail_stop_at_it(self):
        _, events = run_long([BAR1, (101100, 101300, 100500, 101250)])
        close = U.close_of(events)
        assert (close.reason, close.price, close.gap) == ("TRAIL_STOP", 100500, False)
        assert close.at == H.at(10, 7), "stamped with the OPEN of the bar that touched it"

    def test_the_moved_stop_is_booked_at_the_close_of_the_bar_after_which_it_takes_effect(self):
        """Manager decision 4: STOP_MOVED is stamped with the bar CLOSE (10:07 for bar 10:06),
        the moment the new stop takes effect; the CLOSED event of a later touch is stamped with
        its bar's OPEN."""
        _, events = run_long([BAR1])
        moved = [e for e in events if str(H.val(e.kind)) == "STOP_MOVED"]
        assert len(moved) == 1 and moved[0].ts == H.at(10, 7)
        assert U.mentions(moved[0].detail, 100500), moved[0].detail
        _, events = run_long([BAR1, (101100, 101300, 100500, 101250)])
        assert [e.ts for e in events if str(H.val(e.kind)) == "STOP_MOVED"] == [H.at(10, 7)]
        assert U.close_of(events).at == H.at(10, 7)

    def test_a_tighter_stop_on_a_later_bar_is_booked_again_at_that_bars_close(self):
        _, events = run_long([BAR1, (101100, 101300, 100800, 101150)])
        moved = [e for e in events if str(H.val(e.kind)) == "STOP_MOVED"]
        assert [e.ts for e in moved] == [H.at(10, 7), H.at(10, 8)]
        assert U.mentions(moved[1].detail, 100550)

    def test_a_close_that_does_not_move_the_stop_books_no_stop_moved(self):
        _, events = run_long([BAR1, (101100, 101150, 100800, 101065)])
        assert [e.ts for e in events if str(H.val(e.kind)) == "STOP_MOVED"] == [H.at(10, 7)]

    def test_a_next_bar_one_tick_above_the_new_stop_does_not_close(self):
        state, events = run_long([BAR1, (101100, 101300, 100505, 101250)])
        assert U.close_of(events) is None

    def test_a_gap_through_the_trailed_stop_closes_at_the_open_trail_stop(self):
        _, events = run_long([BAR1, (100200, 100300, 100100, 100200)])
        close = U.close_of(events)
        assert (close.reason, close.price, close.gap) == ("TRAIL_STOP", 100200, True)
        assert close.at == H.at(10, 7)

    def test_the_trail_uses_the_close_not_the_high_of_the_bar(self):
        state, _ = run_long([(100000, 101900, 100100, 101100)])
        assert state.stop_paise == 100500  # not 101900 - 600

    def test_the_stop_never_loosens_when_the_close_falls_back_after_arming(self):
        """close 101065 is still >= 1.0 R (101060) but its candidate 100465 is below 100500."""
        state, events = run_long([BAR1, (101100, 101150, 100800, 101065)])
        assert U.close_of(events) is None and state.stop_paise == 100500

    def test_a_lower_close_below_the_activation_never_loosens_it_either(self):
        state, _ = run_long([BAR1, (101100, 101150, 100800, 100900)])
        assert state.stop_paise == 100500 and state.trail_active is True

    def test_a_higher_close_tightens_it(self):
        state, _ = run_long([BAR1, (101100, 101300, 100800, 101150)])
        assert state.stop_paise == 101150 - 600 == 100550

    def test_it_keeps_ratcheting_bar_after_bar(self):
        rows = [BAR1, (101100, 101300, 100800, 101150), (101150, 101400, 100900, 101300)]
        state, _ = run_long(rows)
        assert state.stop_paise == 101300 - 600 == 100700

    def test_the_initial_stop_is_never_replaced_as_the_unit_of_r(self):
        """After the stop trailed to 100500, R is still measured from the INITIAL 99000: a
        close of 101060 (1.0 R) is what arms a second ratchet, not 100500 + 1030."""
        state, _ = run_long([BAR1, (101100, 101200, 100800, 101070)])
        assert state.initial_stop_paise == 99000

    def test_a_short_mirrors_the_long(self):
        state, events = run_short([BAR1_SHORT])
        assert U.close_of(events) is None
        assert state.stop_paise == 98900 + 600 == 99500 and state.trail_active is True
        _, events = run_short([BAR1_SHORT, (98900, 99500, 98800, 99000)])
        close = U.close_of(events)
        assert (close.reason, close.price) == ("TRAIL_STOP", 99500)

    def test_a_short_never_loosens(self):
        state, _ = run_short([BAR1_SHORT, (98900, 99000, 98800, 98935)])
        assert state.stop_paise == 99500  # candidate 98935 + 600 = 99535 is looser


class TestActivation:
    def test_a_close_exactly_one_r_in_profit_arms_the_trail(self):
        state, _ = run_long([(100000, 101100, 100100, 101060)])
        assert state.stop_paise == 101060 - 600 and state.trail_active is True

    def test_a_close_one_tick_short_of_the_activation_does_not_arm_it(self):
        state, events = run_long([(100000, 101100, 100100, 101055)])
        assert state.stop_paise == U.STOP_L and state.trail_active is False

    def test_a_high_far_beyond_one_r_with_a_weak_close_does_not_arm_it(self):
        """The Lab arms on the bar's best price; the paper book on the CLOSE (AC3)."""
        state, _ = run_long([(100000, 102000, 100000, 100500)])
        assert state.stop_paise == U.STOP_L and state.trail_active is False

    def test_the_activation_r_is_taken_from_the_rules(self):
        rules = U.trail_rules(activate=2.0)
        state, _ = run_long([BAR1], rules)  # +1.04 R is not +2.0 R
        assert state.trail_active is False and state.stop_paise == U.STOP_L
        state, _ = run_long([(100000, 101900, 100100, 102090)], rules)  # +2.0 R
        assert state.trail_active is True

    def test_the_multiple_is_taken_from_the_rules(self):
        state, _ = run_long([BAR1], U.trail_rules(mult=2.5))  # 2.5 x 4.00 = 10.00
        assert state.stop_paise == 101100 - 1000

    def test_a_losing_close_never_arms_it(self):
        state, _ = run_long([(100000, 100100, 99100, 99200)])
        assert state.stop_paise == U.STOP_L and state.trail_active is False


class TestWhatDisablesTheTrail:
    @pytest.mark.parametrize("atr", [None, 0.0, math.nan], ids=["none", "zero", "nan"])
    def test_an_unusable_frozen_atr_disables_it_and_the_stop_closes_as_stop(self, atr):
        rows = [BAR1, (101100, 101200, 98900, 99000)]
        state, events = run_long(rows, U.trail_rules(atr=atr))
        close = U.close_of(events)
        assert (close.reason, close.price) == ("STOP", 99000)
        assert state.trail_active is False

    def test_a_zero_multiple_disables_it(self):
        state, _ = run_long([BAR1], U.trail_rules(mult=0.0))
        assert state.stop_paise == U.STOP_L and state.trail_active is False

    def test_there_is_no_fallback_to_an_atr_column_in_the_bars(self):
        bars = U.pbars(START, [BAR1]).assign(atr=4.0, atr_trail=4.0, atr14=4.0)
        state, _ = U.run(U.mk_state(), bars, U.trail_rules(atr=None))
        assert state.stop_paise == U.STOP_L and state.trail_active is False


class TestTrailStopIffTheStopMoved:
    def test_a_stop_that_trailed_closes_trail_stop(self):
        _, events = run_long([BAR1, (101100, 101300, 100500, 101250)])
        assert U.close_of(events).reason == "TRAIL_STOP"

    def test_an_armed_trail_that_never_moved_the_stop_closes_plain_stop(self):
        """atr 50.00 puts the candidate (101100 - 7500) far below the initial stop: the stop
        never moved beyond the initial one, so the close is STOP."""
        rows = [BAR1, (100000, 100100, 98900, 99000)]
        state, events = run_long(rows, U.trail_rules(atr=50.0))
        close = U.close_of(events)
        assert (close.reason, close.price) == ("STOP", 99000)
        assert state.trail_active is False

    def test_a_candidate_exactly_equal_to_the_initial_stop_is_not_beyond_it(self):
        """101100 - 3.0 x 7.00 = 990.00 = the initial stop: it did not move beyond it."""
        rules = U.trail_rules(mult=3.0, atr=7.0)
        state, events = run_long([BAR1, (100000, 100100, 98900, 99000)], rules)
        assert U.close_of(events).reason == "STOP" and state.trail_active is False

    def test_a_short_that_trailed_closes_trail_stop_and_one_that_did_not_closes_stop(self):
        _, events = run_short([BAR1_SHORT, (98900, 99500, 98800, 99000)])
        assert U.close_of(events).reason == "TRAIL_STOP"
        _, events = run_short([(100000, 100100, 99900, 100000), (100000, 101000, 99900, 100500)])
        assert U.close_of(events).reason == "STOP"

    def test_the_gap_through_a_trailed_stop_is_trail_stop_too(self):
        _, events = run_long([BAR1, (100200, 100300, 100100, 100200)])
        assert U.close_of(events).reason == "TRAIL_STOP"


class TestIndexOptionTrailOnTheUnderlying:
    """For an index position every price (entry, stop, bars) is the UNDERLYING, so the trail is
    measured in underlying points; BUY_PE is short and its trailed stop falls."""

    OPT = {"strike": 54000.0, "opt_type": "PE"}

    def test_a_buy_pe_trail_ratchets_down_as_the_underlying_falls(self):
        """entry 54000.00, stop 54150.00 (risk 15000 paise); atr 100.00 x 1.5 = 150.00."""
        state = U.mk_state("short", "INDEX", option=self.OPT)
        bar = (5400000, 5400500, 5384000, 5385000)  # close 53850 = +1.0 R for the short
        rules = U.trail_rules(mult=1.5, activate=1.0, atr=100.0)
        out, events = U.run(state, U.pbars(START, [bar]), rules)
        assert U.close_of(events) is None
        assert out.stop_paise == 5385000 + 15000 == 5400000 and out.trail_active is True


class TestTheStopOnlyEverTightens:
    @PROP
    @given(
        direction=st.sampled_from(["long", "short"]),
        steps=st.lists(st.integers(-40, 40), min_size=2, max_size=14),
        mult=st.sampled_from([1.0, 2.0, 3.0]),
        atr=st.sampled_from([1.0, 2.0, 4.0]),
        activate=st.sampled_from([0.5, 1.0]),
    )
    def test_property_bar_by_bar_the_stop_is_monotone(self, direction, steps, mult, atr, activate):
        """A quiet random walk (no gaps, one-tick wicks) stepped ONE BAR AT A TIME: whatever the
        trail does, a long's stop only rises and a short's only falls, from bar to bar, until
        the trailed stop itself closes the trade."""
        sign = 1 if direction == "long" else -1
        price, rows = 100000, []
        for d in steps:
            o, c = price, price + d * U.TICK
            rows.append((o, max(o, c) + U.TICK, min(o, c) - U.TICK, c))
            price = c
        # the initial stop is 50 rupees away: only a TRAILED stop can close this walk
        state = U.mk_state(
            direction, entry=100000 + sign * 30, stop=100000 - sign * 5000, target=None
        )
        bars = U.pbars(START, rows)
        rules = U.trail_rules(mult=mult, activate=activate, atr=atr)
        previous = state.stop_paise
        for k in range(1, len(rows) + 1):
            now = (START + dt.timedelta(minutes=k)).astimezone(START.tzinfo)
            state, events = U.run(state, bars.iloc[:k], rules, now)
            if U.close_of(events) is not None:
                assert U.close_of(events).reason == "TRAIL_STOP" and state.trail_active
                break
            assert (state.stop_paise - previous) * sign >= 0, "the stop loosened"
            previous = state.stop_paise
        # the flag is the desk's: True exactly when the stop has moved beyond the initial one
        assert state.trail_active == (state.stop_paise != state.initial_stop_paise)
