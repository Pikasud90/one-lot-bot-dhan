"""The strategy's own trail in manage_step (2026-11-11): the stop of a paper trade follows
the level the strategy's line allows (`TrailLevel`), for a trade whose alert froze
`ManageRules.line_trail`. Pure: no I/O. The worked trade is the one of test_manage.py: long
from 1000.00, stop 995.00, filled on the 10:06 one-minute bar; every price sits on the
5-paise tick grid unless the test is about the snap.

  when     from the first fine bar opening at or after the close of the bar that gave the
           level; a level whose bar closed at or before the fill is stale; one not due yet
           waits and is NOT remembered (the desk reports its latest level at every poll); a
           level from a bar that closes after `now` is look-ahead
  how      only ever tighter; snapped to the tick against the trader; no profit needed first;
           booked once as STOP_MOVED (`source: "strategy_line"`), stamped with that bar's OPEN
  the bar  judged against the moved stop: an open at or through it closes TRAIL_STOP at the
           open (a gap when strictly through), a touch inside closes at the level
  both     with the ATR trail on as well, the tighter stop stands
"""

from __future__ import annotations

import copy
import datetime as dt
from collections.abc import Sequence
from dataclasses import replace

import pytest

from onelot.paper.events import PaperEvent
from onelot.paper.manage import (
    ManageRules,
    PaperClose,
    PositionState,
    TrailLevel,
    manage_step,
)

from .test_manage import OPENED, QUIET, RULES, TRAIL, Row, at, frame, position

LINE = replace(RULES, line_trail=True)  # a 5-minute strategy over one-minute bars
UNDER_WATER = (999.00, 999.50, 998.50, 999.00)  # below the entry, above every level used here


def level(paise: int, minute: int = 10) -> TrailLevel:
    """The line's level on the strategy bar that closed at 10:`minute`."""
    return TrailLevel(paise, at(minute))


def step(
    state: PositionState,
    rows: Sequence[Row],
    trail: TrailLevel | None,
    *,
    rules: ManageRules = LINE,
    now: dt.datetime | None = None,
) -> tuple[PositionState, list[PaperEvent]]:
    """manage_step over one-minute `rows` from the fill bar; `now` defaults to the close of
    the last row."""
    now = now or OPENED + dt.timedelta(minutes=len(rows))
    return manage_step(state, frame(rows), rules, now, None, trail)


def moves(events: Sequence[PaperEvent]) -> list[tuple[dt.datetime, int]]:
    return [(e.ts, e.detail["to_paise"]) for e in events if e.kind == "STOP_MOVED"]


class TestTheStopFollowsTheLine:
    def test_it_stands_at_the_level_from_the_first_bar_after_the_levels_bar_closed(self) -> None:
        rows = [QUIET] * 6  # 10:06 .. 10:11
        state, events = step(position(target=None), rows, level(99_800))
        assert state.closed is None
        assert (state.stop_paise, state.trail_active) == (99_800, True)
        assert state.initial_stop_paise == 99_500  # the risk the trade was taken with stays
        assert [(e.kind, e.ts) for e in events] == [("STOP_MOVED", at(10))]
        assert events[0].detail == {
            "from_paise": 99_500,
            "to_paise": 99_800,
            "initial_stop_paise": 99_500,
            "bar_ts": at(10).isoformat(),
            "source": "strategy_line",
            "line_paise": 99_800,
            "line_bar_close_ts": at(10).isoformat(),
        }
        assert events[0].trade_id == 7

    def test_the_bars_before_that_open_run_on_the_stop_they_had(self) -> None:
        # the 10:09 bar dips to 996.00: under the level to come, above the stop in force
        rows = [QUIET, QUIET, QUIET, (1000.00, 1000.50, 996.00, 1000.00), QUIET]
        state, events = step(position(target=None), rows, level(99_800))
        assert state.closed is None and moves(events) == [(at(10), 99_800)]

    def test_no_profit_is_needed_first(self) -> None:
        """The ATR trail waits for `trail_activate_r` of profit; the line does not: it is the
        strategy's stop, wherever the price is."""
        rows = [UNDER_WATER] * 6
        state, events = step(position(target=None), rows, level(99_700))
        assert (state.stop_paise, moves(events)) == (99_700, [(at(10), 99_700)])
        with_atr_trail_only, none = step(position(target=None), rows, None, rules=TRAIL)
        assert (with_atr_trail_only.stop_paise, none) == (99_500, [])

    def test_it_is_never_loosened(self) -> None:
        state, _ = step(position(target=None), [QUIET] * 6, level(99_800))
        state, events = step(state, [QUIET] * 11, level(99_600, 15))  # the line stepped back
        assert (state.stop_paise, state.trail_active, events) == (99_800, True, [])
        fresh, events = step(position(target=None), [QUIET] * 6, level(99_400))
        assert (fresh.stop_paise, fresh.trail_active, events) == (99_500, False, [])

    def test_a_level_equal_to_the_stop_moves_nothing(self) -> None:
        state, events = step(position(target=None), [QUIET] * 6, level(99_500))
        assert (state.stop_paise, state.trail_active, events) == (99_500, False, [])

    def test_successive_levels_ratchet(self) -> None:
        rows = [QUIET] * 16  # 10:06 .. 10:21
        state, first = step(position(target=None), rows[:6], level(99_700))
        state, second = step(state, rows[:11], level(99_850, 15))
        state, third = step(state, rows, level(99_800, 20))  # back again: not followed
        assert moves(first + second + third) == [(at(10), 99_700), (at(15), 99_850)]
        assert second[0].detail["from_paise"] == 99_700
        assert state.stop_paise == 99_850 and state.closed is None

    def test_the_same_level_at_every_poll_moves_the_stop_once(self) -> None:
        rows = [QUIET] * 10
        state, booked = position(target=None), []
        for minutes in range(2, len(rows) + 1):  # from 10:08 on: a new bar, the same level
            state, events = step(state, rows[:minutes], level(99_800, 8))
            booked += events
        assert moves(booked) == [(at(8), 99_800)]
        at_once, events = step(position(target=None), rows, level(99_800, 8))
        assert at_once == state and events == booked  # bar by bar == all at once

    @pytest.mark.parametrize(
        ("direction", "reported", "stood"),
        [
            ("long", 99_803, 99_800),  # a long's stop is snapped down
            ("long", 99_799, 99_795),
            ("long", 99_800, 99_800),
            ("short", 100_203, 100_205),  # a short's stop is snapped up
            ("short", 100_201, 100_205),
            ("short", 100_200, 100_200),
        ],
    )
    def test_it_is_snapped_to_the_tick_against_the_trader(
        self, direction: str, reported: int, stood: int
    ) -> None:
        state, events = step(position(direction, target=None), [QUIET] * 6, level(reported))
        assert state.stop_paise == stood and stood % 5 == 0
        assert events[0].detail["to_paise"] == stood
        assert events[0].detail["line_paise"] == reported  # what the strategy said, unsnapped

    def test_a_short_mirrors(self) -> None:
        rows = [QUIET] * 4 + [(1000.00, 1002.50, 999.50, 1001.00)]
        state, events = step(position("short", target=None), rows, level(100_200))
        assert moves(events) == [(at(10), 100_200)]
        assert state.closed == PaperClose("TRAIL_STOP", 100_200, at(10), gap=False)
        loose, events = step(position("short", target=None), [QUIET] * 6, level(100_600))
        assert (loose.stop_paise, events) == (100_500, [])  # above the stop: looser, ignored

    def test_the_input_state_is_not_touched(self) -> None:
        state = position(target=None)
        before = copy.deepcopy(state)
        new, _ = step(state, [QUIET] * 6, level(99_800))
        assert state == before and new is not state


class TestWhenALevelCounts:
    def test_a_level_from_a_bar_that_closed_at_or_before_the_fill_is_stale(self) -> None:
        """The entry stop was built from it already (the fill bar opens 10:06)."""
        for closed in (at(5), OPENED):
            state, events = step(position(target=None), [QUIET] * 6, TrailLevel(99_800, closed))
            assert (state.stop_paise, state.trail_active, events) == (99_500, False, [])
        state, events = step(position(target=None), [QUIET] * 6, level(99_800, 7))
        assert moves(events) == [(at(7), 99_800)]  # one minute after the fill: it counts

    def test_a_level_waits_for_the_first_bar_at_or_after_its_bars_close(self) -> None:
        """The feed is behind the clock: the level's bar has closed (10:15) but the bars reach
        10:14 only. Nothing moves, and nothing is remembered either."""
        rows = [QUIET] * 10  # 10:06 .. 10:15
        late = level(99_800, 15)
        state, events = step(position(target=None), rows[:9], late, now=at(15))
        assert (state.stop_paise, state.last_bar_ts, events) == (99_500, at(14), [])
        unreported, events = step(state, rows, None)  # the 10:15 bar, the level not repeated
        assert (unreported.stop_paise, events) == (99_500, [])
        state, events = step(state, rows, late)
        assert moves(events) == [(at(15), 99_800)]

    def test_a_level_from_a_bar_that_has_not_closed_is_look_ahead(self) -> None:
        with pytest.raises(ValueError, match="look-ahead"):
            step(position(target=None), [QUIET] * 6, level(99_800, 20))

    def test_a_trade_whose_alert_froze_no_line_trail_ignores_any_level(self) -> None:
        state, events = step(position(target=None), [QUIET] * 6, level(99_800), rules=RULES)
        assert (state.stop_paise, state.trail_active, events) == (99_500, False, [])

    def test_a_closed_position_is_left_alone(self) -> None:
        closed, _ = step(position(target=None), [(998.00, 999.00, 995.00, 997.00)], None)
        assert closed.closed == PaperClose("STOP", 99_500, at(6), gap=False)
        again, events = step(closed, [QUIET] * 6, level(99_800))
        assert again is closed and events == []


class TestTheBarTheLevelTakesEffectOn:
    def test_a_touch_inside_it_closes_at_the_level(self) -> None:
        rows = [QUIET] * 4 + [(1000.00, 1001.00, 999.40, 1000.00)]  # the 10:10 low: 999.40
        state, events = step(position(target=None), rows, level(99_950))
        assert state.closed == PaperClose("TRAIL_STOP", 99_950, at(10), gap=False)
        assert [(e.kind, e.ts) for e in events] == [("STOP_MOVED", at(10)), ("CLOSED", at(10))]
        closed = events[-1].detail
        assert (closed["reason"], closed["price_paise"]) == ("TRAIL_STOP", 99_950)
        assert (closed["stop_paise"], closed["initial_stop_paise"]) == (99_950, 99_500)

    def test_an_open_exactly_at_it_closes_at_the_open_and_is_no_gap(self) -> None:
        rows = [QUIET] * 4 + [(999.50, 1000.00, 999.00, 999.80)]
        state, _ = step(position(target=None), rows, level(99_950))
        assert state.closed == PaperClose("TRAIL_STOP", 99_950, at(10), gap=False)

    def test_a_line_carried_past_the_price_closes_at_the_open_as_a_gap(self) -> None:
        """As in the Lab: a stop the line has carried beyond the price is a gap at the next
        open. The trade ends at that open, not at the level it never traded at."""
        rows = [QUIET] * 4 + [(1000.00, 1001.00, 999.50, 1000.50)]
        state, events = step(position(target=None), rows, level(100_100))
        assert state.closed == PaperClose("TRAIL_STOP", 100_000, at(10), gap=True)
        assert moves(events) == [(at(10), 100_100)]

    def test_a_stop_that_never_moved_still_closes_stop(self) -> None:
        rows = [QUIET] * 4 + [(998.00, 999.00, 995.00, 997.00)]
        state, events = step(position(target=None), rows, level(99_400))  # looser: ignored
        assert state.closed == PaperClose("STOP", 99_500, at(10), gap=False)
        assert [e.kind for e in events] == ["CLOSED"]

    def test_the_session_end_at_the_same_open_gives_way_to_a_gap_through_the_line(self) -> None:
        """A gap through the stop outranks the session end due at the same open; an open
        above the line is the session end, at that open."""
        rules = replace(LINE, exit_time=dt.time(10, 10))
        rows = [QUIET] * 4 + [(1000.00, 1001.00, 999.50, 1000.50)]
        gapped, _ = step(position(target=None), rows, level(100_100), rules=rules)
        assert gapped.closed == PaperClose("TRAIL_STOP", 100_000, at(10), gap=True)
        ended, events = step(position(target=None), rows, level(99_800), rules=rules)
        assert ended.closed == PaperClose("SESSION_END", 100_000, at(10), gap=False)
        assert moves(events) == [(at(10), 99_800)]  # booked: it was in force at that open


class TestBothTrails:
    BOTH = replace(TRAIL, line_trail=True)  # the ATR trail (1 x 2.00) on a 1-minute strategy
    UP = (1000.00, 1006.50, 999.50, 1006.00)  # closes +1.2R: the ATR trail arms at 1004.00
    HIGH = (1006.00, 1006.50, 1005.50, 1006.00)

    def test_the_tighter_stop_stands(self) -> None:
        rows = [self.UP, self.HIGH, self.HIGH]
        state, events = step(position(), rows[:2], level(100_300, 7), rules=self.BOTH)
        # the ATR trail moved it at the 10:06 close; the line's 1003.00 is looser
        assert moves(events) == [(at(7), 100_400)] and "source" not in events[0].detail
        state, events = step(state, rows, level(100_500, 8), rules=self.BOTH)
        assert moves(events) == [(at(8), 100_500)]
        assert events[0].detail["source"] == "strategy_line"
        assert (state.stop_paise, state.closed) == (100_500, None)

    def test_the_line_first_then_the_atr_trail_from_the_same_bars_close(self) -> None:
        rows = [QUIET, self.UP]  # the line at the 10:07 open; that bar's close arms the trail
        state, events = step(position(), rows, level(99_900, 7), rules=self.BOTH)
        assert moves(events) == [(at(7), 99_900), (at(8), 100_400)]
        assert state.stop_paise == 100_400


class TestTheContract:
    @pytest.mark.parametrize("paise", [99_800.0, 0, -5, True, None, "99800"])
    def test_a_level_is_positive_integer_paise(self, paise: object) -> None:
        with pytest.raises(ValueError, match="positive integer paise"):
            TrailLevel(paise, at(10))  # type: ignore[arg-type]

    def test_a_levels_time_is_timezone_aware(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            TrailLevel(99_800, dt.datetime(2026, 10, 29, 10, 10))

    def test_anything_else_passed_as_a_level_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must be a TrailLevel or None"):
            step(position(target=None), [QUIET] * 6, (998.0, at(10)))  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [1, "yes", None])
    def test_the_rule_is_a_bool(self, value: object) -> None:
        rules = replace(RULES, line_trail=value)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="line_trail"):
            step(position(target=None), [QUIET], None, rules=rules)
