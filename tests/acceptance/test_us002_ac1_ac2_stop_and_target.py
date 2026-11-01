"""US-002 AC1 and AC2 on `manage_step`: the stop and the target, bar by bar.

AC1  A long stops when a bar LOW is at or below the stop S (a touch counts, like the desk) and
     closes STOP at S, or at the bar OPEN when the bar opens at or below S (a gap). STOP beats
     TARGET on the same bar; shorts mirror. For an index option alert (BUY_PE is direction
     "short" on the underlying, stop above) the stop and target live on the UNDERLYING.
AC2  A long target T fills only if a bar HIGH is at or above T + one tick (it traded through),
     at exactly T, even on a gap-up where T lies outside the bar range; a bar that only reaches
     T exactly does not fill. Shorts mirror.

The worked scenario (paise): long entry 100030, stop S = 99000, target T = 102000, tick 5; the
short twin has entry 99970, stop 101000, target 98000. Bars are 1 minute, the fill bar opens
10:06, and `now` is the close of the last bar unless a test says otherwise.

Manager decisions of 2026-11-01 pinned here: a CLOSED event is stamped with its bar's OPEN time
(the Lab convention), and an open EXACTLY at the stop closes STOP at S with gap False (a gap is
strictly beyond the stop). The `gap` flag of a gap-up target fill is not asserted.
"""

from __future__ import annotations

import pandas as pd
import pytest
import us001_helpers as H
import us002_helpers as U

Q = (100000, 100100, 99900, 100000)  # a bar that touches nothing in the worked scenario


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


def at_minute(m: int):
    return H.at(10, m)


class TestLongStop:
    def test_a_touch_of_the_stop_closes_stop_at_the_stop(self):
        state, events = U.run(U.mk_state(), U.pbars(H.at(10, 6), [(100000, 100100, 99000, 99500)]))
        close = U.close_of(events)
        assert (close.reason, close.price) == ("STOP", 99000)
        assert close.at == H.at(10, 6), "stamped with the bar's OPEN time"

    def test_one_tick_above_the_stop_is_not_a_touch(self):
        bars = U.pbars(H.at(10, 6), [(100000, 100100, 99005, 99500)])
        state, events = U.run(U.mk_state(), bars)
        assert U.close_of(events) is None
        assert state.stop_paise == U.STOP_L and state.last_bar_ts == H.at(10, 6)

    def test_a_trade_through_closes_at_the_stop_not_at_the_low(self):
        bars = U.pbars(H.at(10, 6), [(99800, 99900, 98000, 98500)])
        close = U.close_of(U.run(U.mk_state(), bars)[1])
        assert (close.reason, close.price, close.gap) == ("STOP", 99000, False)

    def test_a_gap_below_the_stop_closes_at_the_open_flagged_gap(self):
        bars = U.pbars(H.at(10, 6), [Q, (98500, 98700, 98000, 98600)])
        close = U.close_of(U.run(U.mk_state(), bars)[1])
        assert (close.reason, close.price, close.gap) == ("STOP", 98500, True)
        assert close.at == H.at(10, 7)  # the exit happened at that bar's open

    def test_an_open_exactly_at_the_stop_closes_stop_at_S_and_is_not_a_gap(self):
        bars = U.pbars(H.at(10, 6), [Q, (99000, 99100, 98900, 99050)])
        close = U.close_of(U.run(U.mk_state(), bars)[1])
        assert (close.reason, close.price, close.gap) == ("STOP", 99000, False)
        assert close.at == H.at(10, 7)

    def test_stop_beats_target_when_one_bar_touches_both(self):
        bars = U.pbars(H.at(10, 6), [(100000, 102100, 98900, 100000)])
        close = U.close_of(U.run(U.mk_state(), bars)[1])
        assert (close.reason, close.price) == ("STOP", 99000)

    def test_a_gap_below_the_stop_beats_a_high_that_would_have_been_a_target(self):
        bars = U.pbars(H.at(10, 6), [(98500, 102100, 98000, 100000)])
        close = U.close_of(U.run(U.mk_state(), bars)[1])
        assert (close.reason, close.price, close.gap) == ("STOP", 98500, True)

    def test_the_fill_bar_itself_is_managed(self):
        bars = U.pbars(H.at(10, 6), [(100000, 100100, 98900, 99500)])
        assert U.close_of(U.run(U.mk_state(), bars)[1]).reason == "STOP"

    def test_a_bar_before_the_fill_is_never_used(self):
        pre = (99000, 99000, 50000, 99000)  # 10:05, below the stop, before the fill
        bars = U.pbars(H.at(10, 5), [pre, Q])
        state, events = U.run(U.mk_state(), bars)
        assert U.close_of(events) is None and state.last_bar_ts == H.at(10, 6)

    def test_a_bar_that_has_not_closed_is_not_used(self):
        bars = U.pbars(H.at(10, 6), [(100000, 100100, 98900, 99500)])
        state, events = U.run(U.mk_state(), bars, now=H.at(10, 6, 59))
        assert events == [] and state.last_bar_ts is None
        _, events = U.run(state, bars, now=H.at(10, 7))  # open + resolution <= now: eligible
        assert U.close_of(events).reason == "STOP"

    def test_only_the_first_close_is_booked_however_many_bars_would_stop(self):
        hit = (99800, 99900, 98900, 99000)
        _, events = U.run(U.mk_state(), U.pbars(H.at(10, 6), [hit, hit, hit]))
        assert len(U.closes_of(events)) == 1
        assert U.close_of(events).at < H.at(10, 8)

    def test_the_stop_levels_of_a_quiet_trade_do_not_move_with_the_trail_off(self):
        state, _ = U.run(U.mk_state(), U.pbars(H.at(10, 6), U.flat(100000, 5)))
        assert state.stop_paise == state.initial_stop_paise == U.STOP_L


class TestShortStop:
    @pytest.fixture
    def short(self):
        return U.mk_state("short")

    def test_a_touch_from_below_closes_stop_at_the_stop(self, short):
        bars = U.pbars(H.at(10, 6), [(100000, 101000, 99900, 100500)])
        close = U.close_of(U.run(short, bars)[1])
        assert (close.reason, close.price) == ("STOP", 101000)

    def test_one_tick_short_of_the_stop_is_not_a_touch(self, short):
        bars = U.pbars(H.at(10, 6), [(100000, 100995, 99900, 100500)])
        assert U.close_of(U.run(short, bars)[1]) is None

    def test_a_gap_above_the_stop_closes_at_the_open_flagged_gap(self, short):
        bars = U.pbars(H.at(10, 6), [Q, (101500, 101700, 101400, 101600)])
        close = U.close_of(U.run(short, bars)[1])
        assert (close.reason, close.price, close.gap) == ("STOP", 101500, True)

    def test_an_open_exactly_at_the_stop_closes_stop_at_S_and_is_not_a_gap(self, short):
        bars = U.pbars(H.at(10, 6), [Q, (101000, 101100, 100900, 101050)])
        close = U.close_of(U.run(short, bars)[1])
        assert (close.reason, close.price, close.gap) == ("STOP", 101000, False)

    def test_stop_beats_target_when_one_bar_touches_both(self, short):
        bars = U.pbars(H.at(10, 6), [(100000, 101100, 97900, 100000)])
        close = U.close_of(U.run(short, bars)[1])
        assert (close.reason, close.price) == ("STOP", 101000)

    def test_a_bar_before_the_fill_is_never_used(self, short):
        pre = (101000, 150000, 101000, 101000)
        state, events = U.run(short, U.pbars(H.at(10, 5), [pre, Q]))
        assert U.close_of(events) is None and state.last_bar_ts == H.at(10, 6)


class TestLongTarget:
    def test_trading_through_by_one_tick_fills_at_exactly_the_target(self):
        bars = U.pbars(H.at(10, 6), [(100500, 102005, 100400, 101800)])
        close = U.close_of(U.run(U.mk_state(), bars)[1])
        assert (close.reason, close.price, close.gap) == ("TARGET", 102000, False)

    def test_an_intrabar_target_is_stamped_with_the_bars_open_time(self):
        bars = U.pbars(H.at(10, 6), [Q, (100500, 102005, 100400, 101800)])
        assert U.close_of(U.run(U.mk_state(), bars)[1]).at == H.at(10, 7)

    @pytest.mark.parametrize("high", [102000, 102004], ids=["exactly-T", "T-plus-4-paise"])
    def test_a_high_short_of_T_plus_one_tick_does_not_fill(self, high):
        bars = U.pbars(H.at(10, 6), [(100500, high, 100400, 101800)])
        state, events = U.run(U.mk_state(), bars)
        assert U.close_of(events) is None and state.last_bar_ts == H.at(10, 6)

    @pytest.mark.parametrize("high", [102005, 102010, 150000])
    def test_any_high_at_or_above_T_plus_one_tick_fills_at_T_never_higher(self, high):
        bars = U.pbars(H.at(10, 6), [(100500, high, 100400, 101800)])
        close = U.close_of(U.run(U.mk_state(), bars)[1])
        assert (close.reason, close.price) == ("TARGET", 102000)

    def test_a_gap_up_beyond_the_target_still_fills_at_exactly_the_target(self):
        """T = 1020.00 lies OUTSIDE this bar (1029.00 .. 1031.00): the story still fills at T."""
        bars = U.pbars(H.at(10, 6), [Q, (103000, 103100, 102900, 103050)])
        close = U.close_of(U.run(U.mk_state(), bars)[1])
        assert (close.reason, close.price) == ("TARGET", 102000)
        assert close.at == H.at(10, 7)

    def test_reaching_T_exactly_then_trading_through_on_the_next_bar_fills_on_the_next(self):
        bars = U.pbars(
            H.at(10, 6), [(100500, 102000, 100400, 101800), (101800, 102010, 101700, 101900)]
        )
        close = U.close_of(U.run(U.mk_state(), bars)[1])
        assert (close.reason, close.price) == ("TARGET", 102000)
        assert close.at == H.at(10, 7)

    def test_the_stop_is_judged_first_on_every_bar_a_target_never_hides_a_stop(self):
        bars = U.pbars(
            H.at(10, 6), [(100500, 102000, 100400, 101800), (101800, 101900, 98900, 99500)]
        )
        close = U.close_of(U.run(U.mk_state(), bars)[1])
        assert (close.reason, close.price) == ("STOP", 99000)

    def test_no_target_means_no_target_close_however_far_it_runs(self):
        bars = U.pbars(H.at(10, 6), [(100500, 900000, 100400, 800000)])
        state, events = U.run(U.mk_state(target=None), bars, U.mk_rules(target_r=2.0))
        assert U.close_of(events) is None

    def test_the_target_is_the_states_level_target_r_never_re_derives_it(self):
        """target_r (2.0) would put a target at 100030 + 2 x 1030 = 102090: the state's own
        target_paise (101000 here) is the only level that closes the trade."""
        bars = U.pbars(H.at(10, 6), [(100500, 101010, 100400, 100900)])
        close = U.close_of(U.run(U.mk_state(target=101000), bars, U.mk_rules(target_r=2.0))[1])
        assert (close.reason, close.price) == ("TARGET", 101000)


class TestShortTarget:
    @pytest.fixture
    def short(self):
        return U.mk_state("short")

    def test_trading_through_by_one_tick_fills_at_exactly_the_target(self, short):
        bars = U.pbars(H.at(10, 6), [(99500, 99600, 97995, 98200)])
        close = U.close_of(U.run(short, bars)[1])
        assert (close.reason, close.price) == ("TARGET", 98000)

    @pytest.mark.parametrize("low", [98000, 97996], ids=["exactly-T", "T-minus-4-paise"])
    def test_a_low_short_of_T_minus_one_tick_does_not_fill(self, short, low):
        bars = U.pbars(H.at(10, 6), [(99500, 99600, low, 98200)])
        assert U.close_of(U.run(short, bars)[1]) is None

    def test_a_gap_down_beyond_the_target_still_fills_at_exactly_the_target(self, short):
        bars = U.pbars(H.at(10, 6), [Q, (97000, 97100, 96900, 97050)])
        close = U.close_of(U.run(short, bars)[1])
        assert (close.reason, close.price) == ("TARGET", 98000) and close.at == H.at(10, 7)

    def test_no_target_means_no_target_close(self):
        short = U.mk_state("short", target=None)
        assert (
            U.close_of(U.run(short, U.pbars(H.at(10, 6), [(99500, 99600, 1000, 1000)]))[1]) is None
        )


class TestIndexOptionManagedOnTheUnderlying:
    """For an index option the stop and the target are levels of the UNDERLYING (the book
    prices the option from the close level); BUY_PE is direction short with the stop ABOVE."""

    def state(self, direction):
        return U.mk_state(direction, "INDEX", option={"strike": 54000.0, "opt_type": "CE"})

    def test_a_buy_ce_stops_when_the_underlying_falls_to_the_stop(self):
        bars = U.pbars(H.at(10, 6), [(5400000, 5401000, 5385000, 5392000)])
        close = U.close_of(U.run(self.state("long"), bars)[1])
        assert (close.reason, close.price) == ("STOP", U.U_STOP_L)

    def test_a_buy_ce_hits_its_target_above_the_underlying(self):
        bars = U.pbars(H.at(10, 6), [(5400000, 5430005, 5399000, 5420000)])
        close = U.close_of(U.run(self.state("long"), bars)[1])
        assert (close.reason, close.price) == ("TARGET", U.U_TARGET_L)

    def test_a_buy_pe_is_short_the_underlying_so_a_rise_stops_it(self):
        bars = U.pbars(H.at(10, 6), [(5400000, 5415000, 5399000, 5410000)])
        close = U.close_of(U.run(self.state("short"), bars)[1])
        assert (close.reason, close.price) == ("STOP", U.U_STOP_S)

    def test_a_buy_pe_wins_when_the_underlying_FALLS_through_its_target(self):
        bars = U.pbars(H.at(10, 6), [(5400000, 5400500, 5369995, 5372000)])
        close = U.close_of(U.run(self.state("short"), bars)[1])
        assert (close.reason, close.price) == ("TARGET", U.U_TARGET_S)

    def test_a_falling_underlying_never_stops_a_buy_pe(self):
        bars = U.pbars(H.at(10, 6), [(5400000, 5400500, 5300000, 5310000)])
        state, events = U.run(self.state("short"), bars)
        assert [e for e in U.closes_of(events) if U.close_of([e]).reason == "STOP"] == []

    def test_a_buy_pe_gap_up_through_the_stop_closes_at_the_open(self):
        bars = U.pbars(
            H.at(10, 6),
            [(5400000, 5400500, 5399500, 5400000), (5420000, 5421000, 5419000, 5420500)],
        )
        close = U.close_of(U.run(self.state("short"), bars)[1])
        assert (close.reason, close.price, close.gap) == ("STOP", 5420000, True)

    def test_the_option_description_is_carried_through_untouched(self):
        state, _ = U.run(
            self.state("long"), U.pbars(H.at(10, 6), [(5400000, 5401000, 5399000, 5400500)])
        )
        assert state.option == {"strike": 54000.0, "opt_type": "CE"}


class TestTheBarsAreReadOnlyAndTimezoneBlind:
    def test_a_utc_indexed_frame_gives_the_same_events_as_an_ist_one(self):
        ist = U.pbars(H.at(10, 6), [Q, (99800, 99900, 98900, 99000)])
        a = U.run(U.mk_state(), ist)
        b = U.run(U.mk_state(), U.to_utc(ist))
        assert U.canon(a[1]) == U.canon(b[1]) and a[0] == b[0]

    def test_the_callers_frame_is_never_changed(self):
        bars = U.pbars(H.at(10, 6), [Q, (99800, 99900, 98900, 99000)])
        before = bars.copy(deep=True)
        U.run(U.mk_state(), bars)
        pd.testing.assert_frame_equal(bars, before)

    def test_unsorted_rows_give_the_same_answer_as_sorted_rows(self):
        bars = U.pbars(H.at(10, 6), [Q, Q, (99800, 99900, 98900, 99000)])
        shuffled = bars.iloc[[2, 0, 1]]
        a, b = U.run(U.mk_state(), bars), U.run(U.mk_state(), shuffled)
        assert U.canon(a[1]) == U.canon(b[1])

    def test_a_row_of_nan_is_skipped_not_guessed_and_never_wedges_the_trade(self):
        """Either defensible policy: refuse the frame with a ValueError, or skip the unusable
        bar; never a close at a price that did not trade, never an exception of another type."""
        nan = float("nan")
        bars = U.pbars(H.at(10, 6), [Q, Q, (99800, 99900, 98900, 99000)])
        bars.iloc[1] = [nan, nan, nan, nan, 0.0]
        try:
            state, events = U.run(U.mk_state(), bars)
        except ValueError:
            return
        close = U.close_of(events)
        assert close is not None and (close.reason, close.price) == ("STOP", 99000)
        assert U.close_of(events).price > 0
