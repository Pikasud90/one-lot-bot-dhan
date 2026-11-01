"""US-002 cycle 2 (review round 1, MAJOR-1): the trail arms and ratchets only at STRATEGY-bar
closes, with the strategy's own trail ATR frozen in the alert.

The Lab trails once per strategy bar (`simulate` runs on strategy bars and ratchets at each
close with ATR(trail_atr_period)); the paper book watches FINE bars, so it must not ratchet at
every fine-bar close. Strategy bars are `tf_min`-wide bins from 09:15 IST on the bar's date,
exactly as `data.resample.resample_bars` builds them. A fine bar closes one when its close
reaches the end of the bin its open falls in:

    1-minute bars, 15-minute strategy   the bars opening 09:29, 09:44, ...      (close ON a boundary)
    2-minute bars (09:15 + 2k), 5-minute  09:19, 09:23, 09:29, 09:33, ...         (the bar that CROSSES it)

The independent oracle used here is `resample_bars` itself: give every fine bar a unique id as
its close, resample, and the ids that come back are exactly the last fine bars of their bins.

Contract under test: `manage.closes_strategy_bar(bar_ts, res, tf_min)`; `manage_step` ratchets only
on such bars, from THAT bar's close, acting from the next bar; the ATR is `ManageRules.atr` (the
book fills it from `manage.atr_trail`, falling back to `manage.atr` only for an alert that has no
such key, and never from anything live).
"""

from __future__ import annotations

import datetime as dt
import math
from fractions import Fraction

import numpy as np
import pandas as pd
import pytest
import us001_helpers as H
import us002_helpers as U
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from onelot.data.resample import resample_bars

PROP = settings(
    max_examples=150,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=list(HealthCheck),
)
MIN = dt.timedelta(minutes=1)
SESSION_OPEN = (9, 15)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


def day_grid(res: int, last=(15, 29)) -> list[dt.datetime]:
    """The fine-bar opens of a session: 09:15 + k x res, up to `last`."""
    out, t = [], H.at(*SESSION_OPEN)
    while (t.hour, t.minute) <= last:
        out.append(t)
        t += dt.timedelta(minutes=res)
    return out


_CACHE: dict[tuple[int, int], frozenset[dt.datetime]] = {}


def last_fine_bars(res: int, tf: int) -> frozenset[dt.datetime]:
    """The fine bars that are the LAST of their strategy bar, from `resample_bars` itself."""
    key = (res, tf)
    if key not in _CACHE:
        stamps = day_grid(res)
        ids = np.arange(len(stamps), dtype=float) + 1.0
        frame = pd.DataFrame(
            {"open": ids, "high": ids, "low": ids, "close": ids, "volume": 1.0},
            index=pd.DatetimeIndex(stamps),
        )
        bins = resample_bars(frame, f"{tf}m")
        _CACHE[key] = frozenset(stamps[int(i) - 1] for i in bins["close"])
    return _CACHE[key]


class TestClosesStrategyBar:
    @pytest.mark.parametrize(
        "res, tf",
        [(1, 1), (1, 2), (1, 3), (1, 5), (1, 10), (1, 15), (1, 30), (2, 2), (2, 4), (2, 5)]
        + [(2, 6), (2, 10), (2, 15), (2, 30)]
        + [
            (1, 4),
            (1, 7),
            (1, 12),
            (1, 45),
            (2, 3),
            (2, 7),
            (2, 9),
            (2, 12),
            (2, 45),
        ],  # odd widths
    )
    def test_it_marks_exactly_the_last_fine_bar_of_every_resampled_bar(self, res, tf):
        """Judged up to 15:15, the strategy's exit_time: nothing is managed after it, and the
        session's last bin can be cut short by the 15:30 close (tf 10: [15:25, 15:35)), a bin
        whose boundary is never reached."""
        manage = U.manage()
        until = dt.time(15, 15)
        expected = {ts for ts in last_fine_bars(res, tf) if ts.time() < until}
        got = {
            ts
            for ts in day_grid(res)
            if ts.time() < until and manage.closes_strategy_bar(ts, dt.timedelta(minutes=res), tf)
        }
        assert got == expected

    @pytest.mark.parametrize(
        "res, tf, opens, closes",
        [
            (1, 15, [(9, 29), (9, 44), (9, 59)], [(9, 15), (9, 28), (9, 30), (9, 43)]),
            (1, 10, [(9, 24), (9, 34)], [(9, 23), (9, 25), (9, 33)]),
            (1, 5, [(9, 19), (9, 24), (15, 29)], [(9, 15), (9, 18), (9, 20)]),
            (2, 5, [(9, 19), (9, 23), (9, 29), (9, 33)], [(9, 15), (9, 17), (9, 21), (9, 25)]),
            (2, 10, [(9, 23), (9, 33)], [(9, 15), (9, 21), (9, 25), (9, 31)]),
        ],
    )
    def test_the_hand_written_boundaries(self, res, tf, opens, closes):
        manage = U.manage()
        step = dt.timedelta(minutes=res)
        for hh, mm in opens:
            assert manage.closes_strategy_bar(H.at(hh, mm), step, tf) is True, (hh, mm)
        for hh, mm in closes:
            assert manage.closes_strategy_bar(H.at(hh, mm), step, tf) is False, (hh, mm)

    def test_the_bins_restart_at_09_15_on_every_date_not_at_midnight_or_a_rolling_origin(self):
        manage = U.manage()
        monday = H.at(9, 29, day=H.MONDAY)
        assert manage.closes_strategy_bar(monday, MIN, 15) is True
        assert manage.closes_strategy_bar(H.at(9, 29, day=H.FRIDAY), MIN, 15) is True

    def test_a_utc_stamp_is_the_same_bar_as_its_ist_stamp(self):
        manage = U.manage()
        ist = H.at(10, 14)
        assert manage.closes_strategy_bar(ist, MIN, 5) is True
        assert manage.closes_strategy_bar(ist.astimezone(dt.timezone.utc), MIN, 5) is True
        assert manage.closes_strategy_bar(H.at(10, 13).astimezone(dt.timezone.utc), MIN, 5) is False


def bars_for(res: int, start: dt.datetime, rows):
    stamps = [start + dt.timedelta(minutes=res * i) for i in range(len(rows))]
    return U.pbars_at(stamps, rows)


class TestTheRatchetWaitsForTheStrategyBarClose:
    """tf 10 over 1-minute bars: strategy bars [10:05, 10:15) and [10:15, 10:25); the fill is
    10:06; entry 100030, stop 99000 (one R = 1030), trail 1.5 x 4.00 = 600 paise."""

    def run(self, rows, *, tf=10, res=1, start=None, **rule_kw):
        start = start or H.at(10, 6)
        state = U.mk_state(opened_at=start)
        rules = U.trail_rules(tf_min=tf, **rule_kw)
        bars = bars_for(res, start, rows)
        return U.run(state, bars, rules, bars.index[-1].to_pydatetime() + dt.timedelta(minutes=res))

    ARM = (101100, 101200, 100600, 101100)  # closes +1.04 R
    QUIET = (100000, 100100, 99900, 100000)

    def test_a_profitable_close_inside_a_strategy_bar_does_not_arm_the_trail(self):
        """10:06 .. 10:13 are inside [10:05, 10:15): five profitable closes, no ratchet."""
        state, events = self.run([self.ARM] * 8)
        assert events == [] and state.stop_paise == U.STOP_L and state.trail_active is False

    def test_the_dip_to_the_would_be_stop_after_such_a_close_is_not_a_stop(self):
        rows = [self.ARM, (101100, 101200, 100400, 101100), self.QUIET]
        _, events = self.run(rows)
        assert U.close_of(events) is None

    def test_the_bar_that_closes_the_strategy_bar_arms_it_from_its_own_close(self):
        """10:14 closes at 10:15, the end of [10:05, 10:15)."""
        rows = [self.QUIET] * 8 + [self.ARM]  # 10:06 .. 10:13 quiet, 10:14 is the boundary bar
        state, events = self.run(rows)
        moved = [e for e in events if str(e.kind) == "STOP_MOVED"]
        assert len(moved) == 1 and moved[0].ts == H.at(10, 15), "stamped with the bar's close"
        assert state.stop_paise == 101100 - 600 == 100500 and state.trail_active is True

    def test_it_acts_from_the_bar_after_the_boundary_bar_never_inside_it(self):
        boundary_dips = (101100, 101200, 100100, 101100)  # its LOW is below the new stop
        state, events = self.run([self.QUIET] * 8 + [boundary_dips])
        assert U.close_of(events) is None, "the stop it arms must not act inside its own bar"
        assert state.stop_paise == 100500
        after = bars_for(
            1, H.at(10, 6), [self.QUIET] * 8 + [boundary_dips, (100800, 100900, 100500, 100800)]
        )
        _, events = U.run(
            U.mk_state(opened_at=H.at(10, 6)), after, U.trail_rules(tf_min=10), U.end_of(after)
        )
        close = U.close_of(events)
        assert (close.reason, close.price, close.at) == ("TRAIL_STOP", 100500, H.at(10, 15))

    def test_the_candidate_is_the_boundary_bars_close_not_the_best_close_since(self):
        rows = [(100000, 101900, 99900, 101800)] + [self.QUIET] * 7 + [self.ARM]
        state, _ = self.run(rows)
        assert state.stop_paise == 101100 - 600, "10:06 closed at 101800 but it is not a boundary"

    def test_a_weak_boundary_close_does_not_arm_even_after_strong_closes_inside(self):
        rows = [self.ARM] * 8 + [(100000, 100100, 99900, 100500)]  # the boundary bar is +0.46 R
        state, events = self.run(rows)
        assert events == [] and state.stop_paise == U.STOP_L

    def test_the_next_strategy_bar_ratchets_again_at_its_own_close(self):
        rows = [self.QUIET] * 8 + [self.ARM] + [(101100, 101200, 100700, 101100)] * 9
        rows += [(101100, 101500, 100700, 101400)]  # 10:24 closes [10:15, 10:25)
        state, events = self.run(rows)
        moved = [e for e in events if str(e.kind) == "STOP_MOVED"]
        assert [e.ts for e in moved] == [H.at(10, 15), H.at(10, 25)]
        assert state.stop_paise == 101400 - 600 == 100800

    def test_a_boundary_bar_missing_from_the_feed_does_not_move_the_trail(self):
        """The strategy bar's last fine bar (10:14) never arrives; 10:13 is not a boundary."""
        rows = [self.QUIET] * 7 + [self.ARM]  # 10:06 .. 10:13 only
        state, events = self.run(rows)
        assert events == [] and state.stop_paise == U.STOP_L

    @pytest.mark.parametrize("tf", [1, 2])
    def test_a_timeframe_of_one_fine_bar_ratchets_at_every_close_as_before(self, tf):
        state, events = self.run([self.ARM], tf=tf)
        if tf == 1:
            assert state.stop_paise == 100500
        else:
            assert state.stop_paise in (U.STOP_L, 100500)

    def test_a_short_mirrors_on_the_same_boundaries(self):
        start = H.at(10, 6)
        quiet = (100000, 100100, 99900, 100000)
        arm = (98900, 99400, 98800, 98900)  # closes +1.04 R for the short (entry 99970)
        bars = bars_for(1, start, [quiet] * 8 + [arm])
        state = U.mk_state("short", opened_at=start)
        out, events = U.run(state, bars, U.trail_rules(tf_min=10), U.end_of(bars))
        assert out.stop_paise == 98900 + 600 == 99500 and out.trail_active is True
        assert [e.ts for e in events if str(e.kind) == "STOP_MOVED"] == [H.at(10, 15)]


class TestAnOptionTrailsOnTheSameCadence:
    def test_a_buy_pe_trail_waits_for_the_strategy_bar_close_too(self):
        """Entry 54000, stop 54150 (risk 15000 paise); atr 100.00 x 1.5; 15m strategy bars from
        09:15 on 1-minute fine bars: the bar opening 10:14 closes [10:00, 10:15)."""
        option = {"strike": 54000.0, "opt_type": "PE"}
        start = H.at(10, 6)
        quiet = (5400000, 5400400, 5399600, 5400000)
        arm = (5398000, 5400000, 5384000, 5385000)  # close 53850 = +1.0 R for the short
        rows = [quiet] * 8 + [arm]  # 10:06 .. 10:13 quiet, 10:14 closes the 10:00 bar
        bars = bars_for(1, start, rows)
        state = U.mk_state("short", "INDEX", opened_at=start, option=option)
        rules = U.trail_rules(mult=1.5, activate=1.0, atr=100.0, tf_min=15)
        out, events = U.run(state, bars, rules, U.end_of(bars))
        assert out.stop_paise == 5385000 + 15000 and out.trail_active is True
        early = bars_for(1, start, [arm] * 8)
        out, events = U.run(state, early, rules, U.end_of(early))
        assert events == [] and out.stop_paise == U.U_STOP_S


def trail_reference(state_kw, rows, start, res, tf, mult, atr, activate):
    """An independent model of the trail alone (no target, no time stop, no session end, no
    strategy exit): returns ('closed', reason, price, bar_open) or ('open', stop, moved_closes)."""
    sign = 1 if state_kw["direction"] == "long" else -1
    entry, initial = state_kw["entry"], state_kw["stop"]
    stop, risk, moved = initial, abs(entry - initial), []
    boundary = last_fine_bars(res, tf)
    distance = Fraction(str(atr)) * 100 * Fraction(str(mult))
    for i, (o, h, l, c) in enumerate(rows):
        ts = start + dt.timedelta(minutes=res * i)
        beyond_open = (o - stop) * sign <= 0
        touched = (l if sign > 0 else h) - stop
        if beyond_open:
            reason = "TRAIL_STOP" if (stop - initial) * sign > 0 else "STOP"
            return ("closed", reason, o, ts)
        if touched * sign <= 0:
            reason = "TRAIL_STOP" if (stop - initial) * sign > 0 else "STOP"
            return ("closed", reason, stop, ts)
        if ts in boundary and Fraction(sign * (c - entry), risk) >= Fraction(str(activate)):
            raw = Fraction(c) - sign * distance
            step = math.floor(raw / 5) * 5 if sign > 0 else math.ceil(raw / 5) * 5
            if (step - stop) * sign > 0:
                stop = step
                moved.append(ts + dt.timedelta(minutes=res))
    return ("open", stop, moved)


@st.composite
def trail_cases(draw):
    res = draw(st.sampled_from([1, 2]))
    tf = draw(st.sampled_from([res, 3, 5, 10, 15]))
    direction = draw(st.sampled_from(["long", "short"]))
    sign = 1 if direction == "long" else -1
    first = draw(st.integers(0, 60))  # the fill bar: 09:15 + first x res
    start = H.at(*SESSION_OPEN) + dt.timedelta(minutes=res * first)
    n = draw(st.integers(4, 45))
    drift = sign * draw(st.integers(0, 4))
    price, rows = 20_000 + draw(st.integers(-100, 100)), []
    for _ in range(n):
        o = price + draw(st.sampled_from([0, 0, 0, 0, 0, 0, -6, 6]))
        c = max(o + drift + draw(st.integers(-10, 10)), 50)
        hi = max(o, c) + draw(st.integers(0, 6))
        lo = max(min(o, c) - draw(st.integers(0, 6)), 1)
        rows.append((o * 5, hi * 5, lo * 5, c * 5))
        price = c
    entry = rows[0][0] + sign * 5 * draw(st.integers(0, 3))
    stop = entry - sign * 5 * draw(st.integers(20, 100))
    return {
        "res": res,
        "tf": tf,
        "direction": direction,
        "start": start,
        "rows": rows,
        "entry": entry,
        "stop": stop,
        "mult": draw(st.sampled_from([1.0, 2.0])),
        "atr": draw(st.sampled_from([0.25, 0.5, 1.0])),
        "activate": draw(st.sampled_from([0.5, 1.0])),
    }


class TestTheTrailAgainstAnIndependentModel:
    @PROP
    @given(trail_cases())
    def test_property_manage_step_trails_exactly_like_the_model(self, case):
        bars = bars_for(case["res"], case["start"], case["rows"])
        state = U.mk_state(
            case["direction"],
            entry=case["entry"],
            stop=case["stop"],
            target=None,
            opened_at=case["start"],
        )
        rules = U.trail_rules(
            mult=case["mult"], activate=case["activate"], atr=case["atr"], tf_min=case["tf"]
        )
        # a session that ends before 15:10 so only the trail and the stop are in play
        end = bars.index[-1].to_pydatetime() + dt.timedelta(minutes=case["res"])
        assert end.time() < dt.time(15, 10)
        out, events = U.run(state, bars, rules, end)
        want = trail_reference(
            case,
            case["rows"],
            case["start"],
            case["res"],
            case["tf"],
            case["mult"],
            case["atr"],
            case["activate"],
        )
        moved = [e.ts for e in events if str(e.kind) == "STOP_MOVED"]
        close = U.close_of(events)
        if want[0] == "closed":
            assert close is not None, want
            assert (close.reason, close.price, close.at) == want[1:]
        else:
            assert close is None, want
            assert out.stop_paise == want[1]
        expected_moves = want[2] if want[0] == "open" else None
        if expected_moves is not None:
            assert moved == expected_moves
        assert out.trail_active == (out.stop_paise != case["stop"])


class TestRememberExitIsPure:
    """`manage.remember_exit(state, reported)`: the state also waiting for a strategy exit, with
    no bar managed (for a caller whose DATA lags the report)."""

    def test_it_returns_a_new_state_waiting_for_the_exit(self):
        manage = U.manage()
        state = U.mk_state()
        waiting = manage.remember_exit(state, U.strategy_exit("SIGNAL_EXIT", H.at(10, 10)))
        assert waiting is not state and state.pending_exit is None
        assert waiting.pending_exit == U.strategy_exit("SIGNAL_EXIT", H.at(10, 10))
        assert waiting.last_bar_ts is None and waiting.stop_paise == state.stop_paise

    def test_none_changes_nothing(self):
        state = U.mk_state()
        assert U.manage().remember_exit(state, None) == state

    def test_the_earliest_wins_and_a_tie_goes_to_the_higher_precedence(self):
        manage = U.manage()
        first = manage.remember_exit(U.mk_state(), U.strategy_exit("OPPOSITE_SIGNAL", H.at(10, 8)))
        later = manage.remember_exit(first, U.strategy_exit("SIGNAL_EXIT", H.at(10, 10)))
        assert later.pending_exit == U.strategy_exit("OPPOSITE_SIGNAL", H.at(10, 8))
        tied = manage.remember_exit(first, U.strategy_exit("SIGNAL_EXIT", H.at(10, 8)))
        assert tied.pending_exit == U.strategy_exit("SIGNAL_EXIT", H.at(10, 8))

    @pytest.mark.parametrize("at", [H.at(10, 5), H.at(10, 6)], ids=["before-fill", "at-fill-open"])
    def test_a_stale_report_is_not_remembered(self, at):
        state = U.mk_state()  # opened 10:06
        assert (
            U.manage().remember_exit(state, U.strategy_exit("SIGNAL_EXIT", at)).pending_exit is None
        )

    def test_something_that_is_not_a_strategy_exit_is_refused(self):
        with pytest.raises(ValueError):
            U.manage().remember_exit(U.mk_state(), ("SIGNAL_EXIT", H.at(10, 10)))

    def test_a_remembered_exit_closes_at_its_open_when_the_bars_come(self):
        manage = U.manage()
        waiting = manage.remember_exit(U.mk_state(), U.strategy_exit("SIGNAL_EXIT", H.at(10, 8)))
        bars = U.pbars(H.at(10, 6), [(100000, 100100, 99900, 100000)] * 4)
        _, events = U.run(waiting, bars)
        close = U.close_of(events)
        assert (close.reason, close.at) == ("SIGNAL_EXIT", H.at(10, 8))

    def test_remembering_after_the_close_changes_nothing(self):
        manage = U.manage()
        bars = U.pbars(H.at(10, 6), [(100000, 100100, 99000, 99500)])
        closed, _ = U.run(U.mk_state(), bars)
        again = manage.remember_exit(closed, U.strategy_exit("SIGNAL_EXIT", H.at(10, 20)))
        assert again.closed == closed.closed
