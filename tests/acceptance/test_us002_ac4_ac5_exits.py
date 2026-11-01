"""US-002 AC4 and AC5 on `manage_step`: strategy exits, the session end, the time stop, and
the precedence when several are due on one bar.

AC4  The injected strategy_exit reports an exit or an opposite signal seen on the CLOSED
     strategy bar B (`StrategyExit(reason, bar_close_ts)`): the trade closes at the first
     fine-bar OPEN at or after B's close with reason SIGNAL_EXIT or OPPOSITE_SIGNAL.
     When several reasons are due on one bar the precedence is
         STOP > TARGET > SESSION_END > SIGNAL_EXIT > OPPOSITE_SIGNAL > TIME_STOP.
AC5  Every paper trade closes SESSION_END (exit_time, default 15:15 IST) at the first bar OPEN
     at or after it, or at the CLOSE of the last session bar when no bar exists after it, and
     this fires even when step first runs on a LATER day (date-aware, unlike the desk); no
     trade is carried overnight until US-027. An intraday max_bars time stop closes TIME_STOP.

Worked frame (paise, 1-minute bars, the fill bar opens 10:06, the exit bar of the strategy
bar that closes 10:10 is the bar OPENING 10:10):

    10:06 .. 10:09   Q = (100000, 100100, 99900, 100000)       quiet
    10:10            (100200, 100300, 100100, 100250)          the exit bar: open 100200
    10:11            (100300, 100400, 100200, 100350)

Manager decision 1 (2026-11-01), CHRONOLOGICAL bar judgement: a strategy exit, session end or
time stop that is due at a bar's OPEN closes at that open, before anything inside the bar (this
is what `lab/simulator.py` does at its lines 215-217: no intra-bar look-ahead). The precedence
above only decides between reasons due at the SAME MOMENT: a gap through the stop (or through
the target) at the open beats an exit due at that open; a stop merely TOUCHED later inside the
bar does not. Class `TestAnExitDueAtTheOpenClosesBeforeAnythingInsideTheBar` pins that.
"""

from __future__ import annotations

import datetime as dt

import pytest
import us001_helpers as H
import us002_helpers as U

Q = (100000, 100100, 99900, 100000)
EXIT_BAR = (100200, 100300, 100100, 100250)
NEXT_BAR = (100300, 100400, 100200, 100350)
START = H.at(10, 6)
MIN = dt.timedelta(minutes=1)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


def worked(extra=()):
    """10:06 .. 10:09 quiet, the exit bar at 10:10, one more bar, then `extra` rows."""
    return U.pbars(START, [Q, Q, Q, Q, EXIT_BAR, NEXT_BAR, *extra])


def exit_at(reason="SIGNAL_EXIT", ts=None):
    return U.strategy_exit(reason, ts or H.at(10, 10))


def late_state(**kw):
    """A position opened at 15:00, so the session end is 15 minutes away."""
    return U.mk_state(opened_at=H.at(15, 0), **kw)


def drift_rows(n):
    """n quiet bars whose opens climb 10 paise a bar (so every bar's open is distinguishable)."""
    return [(100000 + 10 * i, 100100 + 10 * i, 99900, 100000 + 10 * i) for i in range(n)]


def late_rows(n=21):
    """15:00 .. 15:20 (n bars): distinct opens 100000, 100010, ...; never near a level."""
    return [(100000 + 10 * i, 100010 + 10 * i, 99990 + 10 * i, 100005 + 10 * i) for i in range(n)]


class TestStrategyExit:
    @pytest.mark.parametrize("reason", ["SIGNAL_EXIT", "OPPOSITE_SIGNAL"])
    @pytest.mark.parametrize("direction", ["long", "short"])
    def test_it_closes_at_the_first_fine_open_after_the_signal_bar_closed(self, reason, direction):
        _, events = U.run(U.mk_state(direction), worked(), se=exit_at(reason))
        close = U.close_of(events)
        assert (close.reason, close.price) == (reason, 100200), "the OPEN of the bar at 10:10"
        assert close.at == H.at(10, 10)

    def test_it_is_priced_at_the_open_never_at_the_close_or_an_extreme(self):
        _, events = U.run(U.mk_state(), worked(), se=exit_at())
        assert U.close_of(events).price not in (100250, 100300, 100100)

    def test_a_signal_bar_closing_mid_minute_exits_at_the_next_open(self):
        """B closes 10:10:30: the first bar OPEN at or after that is the one opening 10:11."""
        _, events = U.run(U.mk_state(), worked(), se=exit_at(ts=H.at(10, 10, 30)))
        close = U.close_of(events)
        assert (close.price, close.at) == (100300, H.at(10, 11))

    def test_the_exit_bar_must_have_closed_before_the_trade_is_closed(self):
        bars = worked()
        state, events = U.run(U.mk_state(), bars, now=H.at(10, 10, 30), se=exit_at())
        assert events == [] and state.last_bar_ts == H.at(10, 9)
        _, events = U.run(state, bars, now=H.at(10, 11), se=exit_at())
        assert (U.close_of(events).reason, U.close_of(events).price) == ("SIGNAL_EXIT", 100200)

    def test_an_exit_bar_that_the_feed_does_not_have_yet_waits_for_it(self):
        bars = worked()
        early = bars.iloc[:4]  # 10:06 .. 10:09 only
        state, events = U.run(U.mk_state(), early, now=H.at(10, 10), se=exit_at())
        assert events == []
        _, events = U.run(state, bars, now=H.at(10, 12), se=exit_at())
        assert U.close_of(events).price == 100200 and U.close_of(events).at == H.at(10, 10)

    def test_nothing_closes_without_a_strategy_exit(self):
        state, events = U.run(U.mk_state(), worked())
        assert events == [] and state.last_bar_ts == H.at(10, 11)

    def test_the_exit_is_only_ever_applied_once(self):
        state, events = U.run(U.mk_state(), worked(), se=exit_at())
        assert len(U.closes_of(events)) == 1
        again, more = U.run(state, worked(), se=exit_at())
        assert more == [] and again == state

    def test_a_long_and_a_short_close_on_the_same_open_for_the_same_signal(self):
        a = U.close_of(U.run(U.mk_state(), worked(), se=exit_at())[1])
        b = U.close_of(U.run(U.mk_state("short"), worked(), se=exit_at())[1])
        assert (a.price, a.at) == (b.price, b.at)


class TestPrecedenceAtTheOpenAmongStopTargetAndTheExit:
    """What both readings of the story agree on: a GAP at the open beats the strategy exit."""

    def test_a_gap_through_the_stop_at_the_due_open_closes_stop_at_the_open(self):
        bars = U.pbars(START, [Q, Q, Q, Q, (98500, 98700, 98400, 98600)])
        close = U.close_of(U.run(U.mk_state(), bars, se=exit_at())[1])
        assert (close.reason, close.price, close.gap) == ("STOP", 98500, True)

    def test_a_gap_through_the_target_at_the_due_open_closes_target_at_the_target(self):
        bars = U.pbars(START, [Q, Q, Q, Q, (102500, 102600, 102400, 102550)])
        close = U.close_of(U.run(U.mk_state(), bars, se=exit_at())[1])
        assert (close.reason, close.price) == ("TARGET", 102000)

    def test_an_opposite_signal_gives_way_to_a_gap_stop_too(self):
        bars = U.pbars(START, [Q, Q, Q, Q, (98500, 98700, 98400, 98600)])
        close = U.close_of(U.run(U.mk_state(), bars, se=exit_at("OPPOSITE_SIGNAL"))[1])
        assert close.reason == "STOP"

    def test_a_stop_that_closed_the_trade_earlier_is_what_closed_it(self):
        bars = U.pbars(START, [Q, (99800, 99900, 98900, 99000), Q, Q, EXIT_BAR])
        close = U.close_of(U.run(U.mk_state(), bars, se=exit_at())[1])
        assert close.reason == "STOP" and close.at < H.at(10, 10)


class TestAnExitDueAtTheOpenClosesBeforeAnythingInsideTheBar:
    """Manager decision 1: the exit is due at this bar's OPEN, so it closes at that open price,
    whatever the bar's range does afterwards (a later touch of the stop, a trade-through of the
    target). No look-ahead into the bar. The gap cases (the open is already at or through a
    level) are the 'same moment' cases above and below."""

    @pytest.mark.parametrize("reason", ["SIGNAL_EXIT", "OPPOSITE_SIGNAL"])
    def test_a_stop_touched_later_inside_the_exit_bar_is_not_a_stop(self, reason):
        bars = U.pbars(START, [Q, Q, Q, Q, (100200, 100300, 98900, 99500)])
        close = U.close_of(U.run(U.mk_state(), bars, se=exit_at(reason))[1])
        assert (close.reason, close.price, close.gap) == (reason, 100200, False)
        assert close.at == H.at(10, 10)

    @pytest.mark.parametrize("reason", ["SIGNAL_EXIT", "OPPOSITE_SIGNAL"])
    def test_a_target_traded_through_later_inside_the_exit_bar_is_not_a_target(self, reason):
        bars = U.pbars(START, [Q, Q, Q, Q, (100200, 102010, 100100, 101000)])
        close = U.close_of(U.run(U.mk_state(), bars, se=exit_at(reason))[1])
        assert (close.reason, close.price) == (reason, 100200)

    def test_the_same_holds_for_a_short(self):
        bars = U.pbars(START, [Q, Q, Q, Q, (100200, 101100, 97900, 100000)])
        close = U.close_of(U.run(U.mk_state("short"), bars, se=exit_at())[1])
        assert (close.reason, close.price) == ("SIGNAL_EXIT", 100200)

    def test_a_stop_touched_in_the_bar_BEFORE_the_exit_bar_is_still_a_stop(self):
        bars = U.pbars(START, [Q, Q, Q, (99800, 99900, 98900, 99000), EXIT_BAR])
        close = U.close_of(U.run(U.mk_state(), bars, se=exit_at())[1])
        assert (close.reason, close.price) == ("STOP", 99000) and close.at == H.at(10, 9)

    def test_the_session_end_open_beats_a_stop_or_target_touched_later_inside_that_bar(self):
        rows = late_rows()
        rows[15] = (100150, 100160, 98900, 99500)  # the 15:15 bar dips through the stop later
        stopped = U.close_of(U.run(late_state(), U.pbars(H.at(15, 0), rows))[1])
        assert (stopped.reason, stopped.price) == ("SESSION_END", 100150)
        rows[15] = (100150, 102010, 100100, 101000)  # or trades through the target later
        targeted = U.close_of(U.run(late_state(), U.pbars(H.at(15, 0), rows))[1])
        assert (targeted.reason, targeted.price) == ("SESSION_END", 100150)

    def test_the_time_stop_open_beats_a_stop_touched_later_inside_that_bar(self):
        rows = [Q, Q, Q, (100030, 100100, 98900, 99500), Q]  # 10:09 is the time-stop bar
        close = U.close_of(
            U.run(U.mk_state(), U.pbars(START, rows), U.mk_rules(max_bars=3, tf_min=1))[1]
        )
        assert (close.reason, close.price, close.at) == ("TIME_STOP", 100030, H.at(10, 9))

    def test_a_gap_through_the_target_at_the_open_still_beats_the_exit_due_at_that_open(self):
        bars = U.pbars(START, [Q, Q, Q, Q, (102500, 102600, 100100, 102550)])
        close = U.close_of(U.run(U.mk_state(), bars, se=exit_at())[1])
        assert (close.reason, close.price) == ("TARGET", 102000)

    def test_an_open_exactly_at_the_stop_is_the_stop_not_the_exit_due_at_that_open(self):
        bars = U.pbars(START, [Q, Q, Q, Q, (99000, 99100, 98900, 99050)])
        close = U.close_of(U.run(U.mk_state(), bars, se=exit_at())[1])
        assert (close.reason, close.price, close.gap) == ("STOP", 99000, False)

    def test_an_open_exactly_at_the_target_is_not_through_it_so_the_exit_wins(self):
        """The target is due at the open only when the open is through it by a whole tick."""
        bars = U.pbars(START, [Q, Q, Q, Q, (102000, 102100, 101900, 102000)])
        close = U.close_of(U.run(U.mk_state(), bars, se=exit_at())[1])
        assert (close.reason, close.price) == ("SIGNAL_EXIT", 102000)


class TestSessionEnd:
    def test_it_closes_at_the_open_of_the_first_bar_at_or_after_exit_time(self):
        bars = U.pbars(H.at(15, 0), late_rows())
        _, events = U.run(late_state(), bars)
        close = U.close_of(events)
        assert close.reason == "SESSION_END" and close.price == 100150, "the 15:15 open"
        assert close.at == H.at(15, 15)

    def test_with_no_bar_at_exactly_exit_time_the_next_bar_open_is_used(self):
        stamps = [H.at(15, 0) + MIN * i for i in range(15)] + [H.at(15, 16), H.at(15, 17)]
        rows = late_rows(17)
        close = U.close_of(U.run(late_state(), U.pbars_at(stamps, rows), now=H.at(15, 18))[1])
        assert close.reason == "SESSION_END" and close.price == rows[15][0]
        assert close.at == H.at(15, 16)

    def test_a_custom_exit_time_is_honoured(self):
        bars = U.pbars(H.at(14, 30), late_rows(30))
        state = U.mk_state(opened_at=H.at(14, 30))
        close = U.close_of(U.run(state, bars, U.mk_rules(exit_time=dt.time(14, 45)))[1])
        assert close.reason == "SESSION_END" and close.at == H.at(14, 45)

    def test_nothing_closes_before_exit_time(self):
        bars = U.pbars(H.at(15, 0), late_rows(14))  # 15:00 .. 15:13
        state, events = U.run(late_state(), bars, now=H.at(15, 14))
        assert events == [] and state.last_bar_ts == H.at(15, 13)

    def test_a_bar_that_has_not_closed_cannot_be_the_exit_bar(self):
        bars = U.pbars(H.at(15, 0), late_rows())
        state, events = U.run(late_state(), bars, now=H.at(15, 15, 30))
        assert events == [] and state.last_bar_ts == H.at(15, 14)
        _, events = U.run(state, bars, now=H.at(15, 16))
        assert U.close_of(events).price == 100150

    def test_a_frozen_exit_time_ignores_the_wall_clock_of_the_caller(self):
        """`now` is only the data horizon: a step run at 15:40 still closes at the 15:15 open."""
        bars = U.pbars(H.at(15, 0), late_rows())
        close = U.close_of(U.run(late_state(), bars, now=H.at(15, 40))[1])
        assert (close.price, close.at) == (100150, H.at(15, 15))

    def test_a_utc_indexed_frame_closes_at_the_same_ist_exit_time(self):
        bars = U.pbars(H.at(15, 0), late_rows())
        close = U.close_of(U.run(late_state(), U.to_utc(bars))[1])
        assert (close.price, close.at) == (100150, H.at(15, 15))

    def test_a_gap_through_the_stop_at_the_session_end_open_is_a_stop_at_the_open(self):
        """Same moment: the open is already through the stop, and STOP outranks SESSION_END."""
        rows = late_rows()
        rows[15] = (98500, 98700, 98400, 98600)
        close = U.close_of(U.run(late_state(), U.pbars(H.at(15, 0), rows))[1])
        assert (close.reason, close.price, close.gap) == ("STOP", 98500, True)

    def test_a_gap_through_the_target_at_the_session_end_open_is_a_target_at_the_target(self):
        rows = late_rows()
        rows[15] = (102500, 102600, 102400, 102550)
        close = U.close_of(U.run(late_state(), U.pbars(H.at(15, 0), rows))[1])
        assert (close.reason, close.price) == ("TARGET", 102000)


class TestSessionEndWhenNoBarExistsAfterExitTime:
    def short_session(self):
        rows = late_rows(10)  # 15:00 .. 15:09
        rows[-1] = (100090, 100100, 100080, 100123)  # the last close, distinct from every open
        return U.pbars(H.at(15, 0), rows)

    @pytest.mark.parametrize(
        "now", [H.at(15, 31), H.at(9, 0, day=H.FRIDAY)], ids=["after-close", "next-morning"]
    )
    def test_it_closes_at_the_close_of_the_last_session_bar(self, now):
        _, events = U.run(late_state(), self.short_session(), now=now)
        close = U.close_of(events)
        assert (close.reason, close.price) == ("SESSION_END", 100123)
        assert close.at == H.at(15, 10), (
            "stamped at the CLOSE of the last bar (open 15:09 + 1 minute)"
        )

    def test_a_feed_that_stops_at_noon_is_closed_at_noons_last_close_the_next_morning(self):
        rows = U.flat(100000, 114)  # 10:06 .. 11:59
        rows[-1] = (100000, 100100, 99900, 100120)
        bars = U.pbars(START, rows)
        close = U.close_of(U.run(U.mk_state(), bars, now=H.at(10, 0, day=H.FRIDAY))[1])
        assert (close.reason, close.price) == ("SESSION_END", 100120)

    def test_the_next_sessions_open_is_never_the_exit_price(self):
        """No bar of the session exists at or after 15:15; the next day's 09:15 bar is NOT it."""
        stamps = [H.at(14, 58) + MIN * i for i in range(12)]  # 14:58 .. 15:09
        stamps += [H.at(9, 15, day=H.FRIDAY) + MIN * i for i in range(5)]
        rows = U.flat(100000, 11) + [(100000, 100100, 99900, 100111)]
        rows += [(90000, 90100, 89900, 90000)] * 5
        state = U.mk_state(opened_at=H.at(14, 58))
        close = U.close_of(U.run(state, U.pbars_at(stamps, rows), now=H.at(9, 30, day=H.FRIDAY))[1])
        assert (close.reason, close.price) == ("SESSION_END", 100111)


class TestNoOvernightCarry:
    def frame_over_two_days(self):
        stamps = [H.at(14, 58) + MIN * i for i in range(32)]  # 14:58 .. 15:29
        stamps += [H.at(9, 15, day=H.FRIDAY) + MIN * i for i in range(10)]
        rows = [
            (100000 + 10 * i, 100010 + 10 * i, 99990 + 10 * i, 100005 + 10 * i) for i in range(32)
        ]
        rows += [(90000, 90100, 89900, 90000)] * 10  # next morning: gaps far through the stop
        return U.pbars_at(stamps, rows), rows

    def test_a_first_step_on_a_later_day_still_closes_at_the_sessions_exit_open(self):
        bars, rows = self.frame_over_two_days()
        state = U.mk_state(opened_at=H.at(14, 58))
        _, events = U.run(state, bars, now=H.at(9, 30, day=H.FRIDAY))
        close = U.close_of(events)
        assert close.reason == "SESSION_END" and close.at == H.at(15, 15)
        assert close.price == rows[17][0], "the 15:15 open of the OPENING day, not Friday's gap"

    def test_the_gap_open_of_the_next_morning_is_never_used_for_a_stop(self):
        bars, _ = self.frame_over_two_days()
        _, events = U.run(U.mk_state(opened_at=H.at(14, 58)), bars, now=H.at(9, 30, day=H.FRIDAY))
        assert U.close_of(events).reason == "SESSION_END"

    def test_stepping_day_by_day_gives_the_same_close_as_one_late_step(self):
        bars, _ = self.frame_over_two_days()
        state = U.mk_state(opened_at=H.at(14, 58))
        once = U.close_of(U.run(state, bars, now=H.at(9, 30, day=H.FRIDAY))[1])
        s1, e1 = U.run(state, bars, now=H.at(15, 40))
        late = U.close_of(e1)
        assert late is not None
        assert (late.reason, late.price, late.at) == (once.reason, once.price, once.at)
        assert s1.last_bar_ts is not None and s1.last_bar_ts <= H.at(15, 29)

    def test_a_trade_is_never_carried_whatever_the_alert_horizon(self):
        """ManageRules has no horizon: swing and intraday alike close SESSION_END (US-027 later)."""
        bars, _ = self.frame_over_two_days()
        for max_bars in (0, 50):
            rules = U.mk_rules(max_bars=max_bars, tf_min=1)
            state = U.mk_state(opened_at=H.at(14, 58))
            _, events = U.run(state, bars, rules, now=H.at(9, 30, day=H.FRIDAY))
            assert U.close_of(events).at.date() == H.DAY


class TestTimeStop:
    def test_it_closes_at_the_open_of_the_bar_after_the_last_allowed_bar(self):
        """tf_min 1 and max_bars 3: bars 10:06, 10:07, 10:08 are held; the exit is the 10:09 open."""
        rows = drift_rows(8)
        _, events = U.run(U.mk_state(), U.pbars(START, rows), U.mk_rules(max_bars=3, tf_min=1))
        close = U.close_of(events)
        assert (close.reason, close.price, close.at) == ("TIME_STOP", 100030, H.at(10, 9))

    def test_it_is_not_due_while_the_last_allowed_bar_is_still_forming(self):
        rows = drift_rows(8)
        bars = U.pbars(START, rows)
        rules = U.mk_rules(max_bars=3, tf_min=1)
        state, events = U.run(U.mk_state(), bars, rules, now=H.at(10, 8, 59))
        assert events == [] and state.last_bar_ts == H.at(10, 7)
        state, events = U.run(state, bars.iloc[:3], rules, now=H.at(10, 9))
        assert events == [], "bar 3 closed, but the exit bar (10:09) is not in the feed yet"
        _, events = U.run(state, bars, rules, now=H.at(10, 10))
        assert U.close_of(events).at == H.at(10, 9)

    def test_the_count_is_in_strategy_bars_not_fine_bars(self):
        """tf_min 5 and max_bars 2: ten minutes from the 10:06 fill, so the 10:16 open exits."""
        rows = drift_rows(15)
        rules = U.mk_rules(max_bars=2, tf_min=5)
        close = U.close_of(U.run(U.mk_state(), U.pbars(START, rows), rules)[1])
        assert (close.reason, close.at, close.price) == ("TIME_STOP", H.at(10, 16), 100100)

    def test_no_time_stop_when_max_bars_is_zero(self):
        rows = U.flat(100000, 120)
        state, events = U.run(U.mk_state(), U.pbars(START, rows), U.mk_rules(max_bars=0, tf_min=1))
        assert events == [] and state.last_bar_ts == START + MIN * 119

    def test_the_stop_still_comes_first(self):
        rows = [Q, (99800, 99900, 98900, 99000), Q, Q, Q]
        close = U.close_of(
            U.run(U.mk_state(), U.pbars(START, rows), U.mk_rules(max_bars=3, tf_min=1))[1]
        )
        assert close.reason == "STOP"


class TestPrecedenceOfTheOtherReasons:
    """Every pair of reasons due at the SAME bar open: STOP > TARGET > SESSION_END >
    SIGNAL_EXIT > OPPOSITE_SIGNAL > TIME_STOP (the gap cases are above; these are the rest)."""

    def due_together(self, *, se_reason=None, max_bars=0, session=False):
        """A frame whose bar at 10:10 (or 15:15) has EVERY requested reason due at its open."""
        if session:
            bars = U.pbars(H.at(15, 0), late_rows())
            state, due_ts = late_state(), H.at(15, 15)
        else:
            bars = U.pbars(START, [Q, Q, Q, Q, EXIT_BAR, NEXT_BAR])
            state, due_ts = U.mk_state(), H.at(10, 10)
        se = U.strategy_exit(se_reason, due_ts) if se_reason else None
        rules = U.mk_rules(max_bars=max_bars, tf_min=1)
        return U.close_of(U.run(state, bars, rules, se=se)[1]), due_ts

    @pytest.mark.parametrize("se_reason", ["SIGNAL_EXIT", "OPPOSITE_SIGNAL"])
    def test_session_end_beats_a_strategy_exit(self, se_reason):
        close, due = self.due_together(se_reason=se_reason, session=True)
        assert close.reason == "SESSION_END" and close.at == due

    def test_session_end_beats_the_time_stop(self):
        close, due = self.due_together(max_bars=15, session=True)  # 15 bars: 15:00 .. 15:14
        assert close.reason == "SESSION_END" and close.at == due

    def test_session_end_beats_a_strategy_exit_and_the_time_stop_together(self):
        close, _ = self.due_together(se_reason="SIGNAL_EXIT", max_bars=15, session=True)
        assert close.reason == "SESSION_END"

    def test_a_signal_exit_beats_the_time_stop(self):
        close, due = self.due_together(
            se_reason="SIGNAL_EXIT", max_bars=4
        )  # 4 bars: 10:06 .. 10:09
        assert close.reason == "SIGNAL_EXIT" and close.at == due

    def test_an_opposite_signal_beats_the_time_stop(self):
        close, due = self.due_together(se_reason="OPPOSITE_SIGNAL", max_bars=4)
        assert close.reason == "OPPOSITE_SIGNAL" and close.at == due

    def test_the_time_stop_alone_is_what_it_says_on_that_bar(self):
        close, due = self.due_together(max_bars=4)
        assert close.reason == "TIME_STOP" and close.at == due


class TestStateCounters:
    def test_last_bar_ts_is_the_open_of_the_last_bar_managed(self):
        state, _ = U.run(U.mk_state(), U.pbars(START, U.flat(100000, 6)))
        assert state.last_bar_ts == H.at(10, 11)

    def test_bars_held_counts_strategy_bars_since_the_fill_and_sessions_start_at_one(self):
        """Six minutes on a 5-minute timeframe is one whole strategy bar; one session."""
        state, _ = U.run(U.mk_state(), U.pbars(START, U.flat(100000, 6)), U.mk_rules(tf_min=5))
        assert state.bars_held == 1 and state.sessions_held == 1
