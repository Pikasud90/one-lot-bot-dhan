"""US-002 AC6 on `manage_step`: idempotence, outage catch-up, data time, finality.

* the same (now, bars) a second time returns an empty list (and the same state);
* stepping all the bars at once yields the same events as stepping bar by bar, and as stepping
  in any chunks (an outage catch-up is the same as having watched the whole time);
* event timestamps are bar or exit times, never `now`;
* a bar is eligible iff its open plus the resolution is not later than `now`;
* booked events are final and store the bar prices they used even if the feed later revises
  that bar; a bar the position has already seen is never evaluated again.

The scenarios are small and exact (paise, 1-minute bars, fill 10:06, entry 100030, initial
stop 99000, target 102000; the trail, when on, is 1.5 x 4.00 = 600 paise).
"""

from __future__ import annotations

import copy
import datetime as dt

import pandas as pd
import pytest
import us001_helpers as H
import us002_helpers as U

START = H.at(10, 6)
MIN = dt.timedelta(minutes=1)
Q = (100000, 100100, 99900, 100000)

# trail arms on bar 1, tightens twice, then the trailed stop (100700) is touched on 10:10
TRAIL_THEN_STOP = [
    (100000, 101200, 100100, 101100),
    (101100, 101300, 100800, 101150),
    (101150, 101400, 100900, 101300),
    (101300, 101350, 101000, 101050),
    (101000, 101100, 100700, 100800),
    (100800, 100900, 100600, 100700),
]
HIGH_Q = (101000, 101100, 100900, 101000)  # stays above the trailed stop, below +1 R, no target
SIGNAL_SCENARIO = [Q, Q, Q, Q, (100200, 100300, 100100, 100250), (100300, 100400, 100200, 100350)]
TARGET_SCENARIO = [Q, Q, (100500, 102010, 100400, 101800), Q]
QUIET_SCENARIO = U.flat(100000, 8)

SCENARIOS = {
    "trail-then-trailed-stop": (TRAIL_THEN_STOP, U.trail_rules(), None),
    "strategy-exit": (SIGNAL_SCENARIO, U.mk_rules(), ("SIGNAL_EXIT", H.at(10, 10))),
    "opposite-signal-with-trail": (
        TRAIL_THEN_STOP[:4] + [HIGH_Q, HIGH_Q],
        U.trail_rules(),
        ("OPPOSITE_SIGNAL", H.at(10, 11)),
    ),
    "target": (TARGET_SCENARIO, U.mk_rules(), None),
    "time-stop": (
        [(100000 + 10 * i, 100100 + 10 * i, 99900, 100000) for i in range(8)],
        U.mk_rules(max_bars=3, tf_min=1),
        None,
    ),
    "quiet": (QUIET_SCENARIO, U.mk_rules(), None),
}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


def scenario(name):
    rows, rules, se = SCENARIOS[name]
    return U.pbars(START, rows), rules, (U.strategy_exit(*se) if se else None)


def step_by_bar(state, bars, rules, se):
    """What a desk that never missed a poll sees: one step as each bar closes."""
    events = []
    for k in range(1, len(bars) + 1):
        now = START + k * MIN
        known = se if se is not None and now >= se.bar_close_ts else None
        state, new = U.run(state, bars, rules, now, known)
        events += new
        if U.closes_of(new):
            break
    return state, events


class TestIdempotence:
    @pytest.mark.parametrize("name", list(SCENARIOS))
    def test_the_same_now_and_bars_a_second_time_book_nothing_and_change_nothing(self, name):
        bars, rules, se = scenario(name)
        once, first = U.run(U.mk_state(), bars, rules, se=se)
        twice, again = U.run(once, bars, rules, se=se)
        assert again == [] and twice == once

    def test_a_trade_still_open_stepped_again_books_nothing(self):
        bars, rules, se = scenario("quiet")
        once, first = U.run(U.mk_state(), bars, rules)
        twice, again = U.run(once, bars, rules)
        assert first == [] and again == [] and twice == once

    @pytest.mark.parametrize("name", list(SCENARIOS))
    def test_the_same_state_and_bars_always_give_the_same_events(self, name):
        bars, rules, se = scenario(name)
        a = U.run(U.mk_state(), bars, rules, se=se)
        b = U.run(U.mk_state(), bars, rules, se=se)
        assert a[0] == b[0] and U.canon(a[1]) == U.canon(b[1])

    @pytest.mark.parametrize("name", list(SCENARIOS))
    def test_the_input_state_and_frame_are_never_mutated(self, name):
        bars, rules, se = scenario(name)
        state = U.mk_state(option={"strike": 54000.0, "opt_type": "CE"})
        before_state, before_bars = copy.deepcopy(state), bars.copy(deep=True)
        U.run(state, bars, rules, se=se)
        assert state == before_state
        pd.testing.assert_frame_equal(bars, before_bars)

    def test_an_empty_frame_books_nothing(self):
        bars = U.pbars(START, [Q]).iloc[:0]
        state, events = U.run(U.mk_state(), bars, now=H.at(10, 30))
        assert events == [] and state.last_bar_ts is None

    def test_a_clock_before_the_first_bar_closes_books_nothing(self):
        bars = U.pbars(START, [(100000, 100100, 98900, 99500)])
        state, events = U.run(U.mk_state(), bars, now=H.at(10, 5))
        assert events == [] and state.last_bar_ts is None


class TestCatchUpIsTheSameAsWatching:
    @pytest.mark.parametrize("name", list(SCENARIOS))
    def test_stepping_bar_by_bar_equals_stepping_all_at_once(self, name):
        bars, rules, se = scenario(name)
        once_state, once = U.run(U.mk_state(), bars, rules, se=se)
        by_bar_state, by_bar = step_by_bar(U.mk_state(), bars, rules, se)
        assert U.canon(by_bar) == U.canon(once)
        assert by_bar_state == once_state

    @pytest.mark.parametrize("name", list(SCENARIOS))
    @pytest.mark.parametrize("chunks", [(2, 5), (1, 3, 4), (4,), (3, 3, 3)])
    def test_stepping_in_chunks_equals_stepping_all_at_once(self, name, chunks):
        bars, rules, se = scenario(name)
        once_state, once = U.run(U.mk_state(), bars, rules, se=se)
        state, events, upto_bar = U.mk_state(), [], 0
        for size in (*chunks, len(bars)):
            upto_bar = min(upto_bar + size, len(bars))
            now = START + upto_bar * MIN
            known = se if se is not None and now >= se.bar_close_ts else None
            state, new = U.run(state, bars, rules, now, known)
            events += new
            if U.closes_of(new):
                break
        assert U.canon(events) == U.canon(once) and state == once_state

    def test_a_restart_midway_is_the_same_as_never_stopping(self):
        """Persist the state after bar 3, drop everything else, resume with the whole feed."""
        bars, rules, se = scenario("trail-then-trailed-stop")
        once_state, once = U.run(U.mk_state(), bars, rules)
        mid, early = U.run(U.mk_state(), bars, rules, now=START + 3 * MIN)
        resumed = copy.deepcopy(mid)  # what a database round trip would hand back
        final, late = U.run(resumed, bars, rules)
        assert U.canon(early + late) == U.canon(once) and final == once_state

    def test_a_late_start_after_the_whole_session_gives_the_closing_events_not_the_wall_clock(self):
        bars, rules, _ = scenario("trail-then-trailed-stop")
        _, events = U.run(U.mk_state(), bars, rules, now=H.at(16, 45))
        close = U.close_of(events)
        assert (close.reason, close.price) == ("TRAIL_STOP", 100700) and close.at < H.at(10, 12)


class TestEventTimesAreDataTimes:
    def test_no_event_is_stamped_with_now(self):
        """CLOSED carries its bar's OPEN time; STOP_MOVED the bar CLOSE at which the new stop
        takes effect (Manager decision 4); nothing carries the wall time."""
        bars, rules, se = scenario("trail-then-trailed-stop")
        now = H.at(10, 47, 13)  # an arbitrary wall time that is not on any bar boundary
        _, events = U.run(U.mk_state(), bars, rules, now=now)
        assert events, "the scenario must produce events"
        opens = set(bars.index)
        closes = {ts + MIN for ts in bars.index}
        for e in events:
            assert e.ts != now, f"{e.kind} stamped with now"
            kind = str(e.kind)
            if kind == "CLOSED":
                assert e.ts in opens, f"CLOSED must carry its bar's OPEN time, got {e.ts}"
            elif kind == "STOP_MOVED":
                assert e.ts in closes, f"STOP_MOVED must carry a bar CLOSE time, got {e.ts}"
            else:
                assert e.ts in opens | closes, f"{kind} stamped {e.ts}"

    def test_a_strategy_exit_is_stamped_with_the_bar_it_closed_in_not_with_the_signal_or_now(self):
        bars, rules, se = scenario("strategy-exit")
        now = H.at(10, 33, 41)
        close = U.close_of(U.run(U.mk_state(), bars, rules, now=now, se=se)[1])
        assert close.at == H.at(10, 10) != now

    def test_a_session_end_is_stamped_with_the_exit_bar_not_with_the_late_step(self):
        bars = U.pbars(
            H.at(15, 0), [(100000 + 10 * i, 100100 + 10 * i, 99900, 100000) for i in range(21)]
        )
        close = U.close_of(U.run(U.mk_state(opened_at=H.at(15, 0)), bars, now=H.at(18, 3, 9))[1])
        assert close.at == H.at(15, 15)


class TestEligibility:
    def test_a_bar_is_eligible_exactly_when_open_plus_resolution_is_not_later_than_now(self):
        bars = U.pbars(START, [Q, (99800, 99900, 98900, 99000), Q])
        state, events = U.run(U.mk_state(), bars, now=H.at(10, 7, 59))
        assert events == [] and state.last_bar_ts == H.at(10, 6)
        _, events = U.run(state, bars, now=H.at(10, 8, 0))
        assert U.close_of(events).reason == "STOP"

    def test_the_resolution_is_the_spacing_of_the_bars_not_the_strategy_timeframe(self):
        """Two-minute bars: the bar opening 10:08 closes at 10:10. tf_min stays 10 throughout."""
        bars = U.pbars(START, [Q, (99800, 99900, 98900, 99000), Q], step=2)
        state, events = U.run(U.mk_state(), bars, U.mk_rules(tf_min=10), now=H.at(10, 9, 59))
        assert events == [] and state.last_bar_ts == H.at(10, 6)
        _, events = U.run(state, bars, U.mk_rules(tf_min=10), now=H.at(10, 10))
        assert U.close_of(events).reason == "STOP"

    def test_a_ten_minute_timeframe_does_not_make_one_minute_bars_wait_ten_minutes(self):
        bars = U.pbars(START, [(100000, 100100, 98900, 99500), Q])
        _, events = U.run(U.mk_state(), bars, U.mk_rules(tf_min=10), now=H.at(10, 7))
        assert U.close_of(events).reason == "STOP"


class TestBookedEventsAreFinal:
    def test_the_closed_event_stores_the_bar_prices_it_used(self):
        bar = (100050, 100100, 98900, 99500)
        _, events = U.run(U.mk_state(), U.pbars(START, [bar]))
        detail = U.close_of(events).event.detail
        for price in bar:
            assert U.mentions(detail, price), f"the CLOSED detail must store {price}: {detail}"

    def test_a_bar_the_position_has_already_seen_is_never_evaluated_again(self):
        """The feed revises 10:07 AFTER the position walked past it: no retroactive stop."""
        bars = U.pbars(START, [Q, Q])
        state, events = U.run(U.mk_state(), bars, now=H.at(10, 8))
        assert events == [] and state.last_bar_ts == H.at(10, 7)
        revised = U.pbars(START, [Q, (100000, 100100, 98000, 98500), Q])
        state, events = U.run(state, revised, now=H.at(10, 9))
        assert events == [] and state.last_bar_ts == H.at(10, 8)

    def test_a_revised_closing_bar_cannot_change_what_was_booked(self):
        hit = (100000, 100100, 98900, 99500)
        state, first = U.run(U.mk_state(), U.pbars(START, [hit]))
        revised = U.pbars(START, [(100000, 100100, 99900, 100000), Q])
        again, more = U.run(state, revised, now=H.at(10, 9))
        assert more == [] and again == state
        assert U.close_of(first).price == 99000

    def test_a_revision_to_a_bar_not_yet_seen_is_used_as_revised(self):
        """Bar 10:07 had not closed when the first step ran; its final print is what counts."""
        bars = U.pbars(START, [Q, (100000, 100100, 99900, 100000)])
        state, _ = U.run(U.mk_state(), bars, now=H.at(10, 7))
        final = U.pbars(START, [Q, (100000, 100100, 98900, 99500)])
        _, events = U.run(state, final, now=H.at(10, 8))
        assert U.close_of(events).reason == "STOP"
