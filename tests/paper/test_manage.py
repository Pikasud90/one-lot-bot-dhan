"""manage_step: one OPEN paper trade managed bar by bar (US-002 AC1-AC6). Pure: no I/O.

Levels in PositionState are integer paise on the MANAGED series (the stock itself, or the
index under an option); bar frames arrive in rupees like the feed. Every price below sits
on the 5-paise tick grid so no assertion depends on a float tie.

Test list (ping-pong; Beta T1: AC1/AC2 green, AC3/AC4 red; Alpha T2: AC3/AC4 green, AC5/AC6
red here and the AC7 properties in test_manage_properties.py):
  AC1 stop: touch counts; one tick short does not; gap closes at the open; an open exactly
      at S is not a gap; STOP beats TARGET; shorts mirror; BUY_PE = short on the
      underlying; from the fill bar inclusive; earlier bars ignored; forming bar ignored;
      one CLOSED event only; idempotent; input untouched; the event stores its bar prices.
  AC2 target: reaching T is not enough; T + 1 tick fills at exactly T; a gap beyond T
      still fills at T (gap=True); shorts mirror; no target, no TARGET.
  AC3 trail: ratchets at the close, applies from the NEXT bar; activation on the close not
      the high; never loosens; ATR None/0/NaN disables (no frame ATR fallback); TRAIL_STOP
      iff the stop moved; shorts mirror; tick snap against the trader; gap through a
      trailed stop; STOP_MOVED event at the bar close.
  AC4 strategy exit: closes at the first fine open at/after B's close; pending across steps;
      a stale B (closed at or before the fill) is ignored; B after `now` is look-ahead;
      at-open precedence STOP/TARGET > SIGNAL_EXIT > OPPOSITE_SIGNAL; an exit due at a
      bar's open closes before anything inside that bar; a late report closes at the next
      bar not yet managed; a report with no new bar is remembered.
  AC5 session end: the first open at/after exit_time (no bar exactly at it: the next one);
      that open beats a touch inside the bar, a gap through the stop beats it, it beats a
      strategy exit at the same open; custom exit_time; date-aware on a first step the next
      day. Fallback (no bar at/after exit_time): once the session is over (15:30 IST on the
      opening date, or a bar of a later date) close at the last managed bar's CLOSE, stamped
      at that close time (decision A5), also when that bar was managed by an earlier step,
      with the close the position saw; a later session's bar is never managed.
      Time stop: the open once max_bars strategy bars have passed; that open beats a touch
      inside the bar; a strategy exit at the same open outranks it; max_bars 0 is off;
      a negative max_bars is rejected.
  AC6 bar by bar == all at once (trail moves + session end, and the fallback close); the
      same (now, bars) again books nothing; event times are bar times, never now.
  Contract: naive now / naive index / missing columns / wrong-side stop / bad tick raise;
      an unusable bar (NaN, high below low) is skipped; an open outside high/low is judged
      at the open, a close outside it is not a touch; a UTC-indexed frame behaves like an
      IST one; the latest revision of a duplicated bar wins.
"""

from __future__ import annotations

import copy
import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import replace

import pandas as pd
import pytest

from onelot.paper.book import PaperEvent
from onelot.paper.manage import (
    ManageRules,
    PaperClose,
    PositionState,
    StrategyExit,
    manage_step,
    remember_exit,
)

from .conftest import FRI, THU, ist

TICK = 5
OPENED = ist(THU, 10, 6)  # the fill bar's open
RULES = ManageRules(
    target_r=2.0,
    trail_atr_mult=0.0,
    trail_activate_r=1.0,
    atr=None,
    max_bars=0,
    tf_min=5,
    exit_time=dt.time(15, 15),
    tick_paise=TICK,
)
# ATR in RUPEES. A 1-minute strategy over 1-minute bars: every fine bar closes a strategy bar,
# so the trail may move at each close (review round 1, MAJOR-1: only strategy-bar closes).
TRAIL = replace(RULES, trail_atr_mult=1.0, trail_activate_r=1.0, atr=2.0, tf_min=1)
QUIET = (1000.00, 1001.00, 999.00, 1000.50)  # a bar that touches nothing
BAR_KEYS = ("bar_open_paise", "bar_high_paise", "bar_low_paise", "bar_close_paise")  # A1

Row = tuple[float, float, float, float]


def frame(rows: Sequence[Row], start: dt.datetime = OPENED, res_min: int = 1) -> pd.DataFrame:
    """OHLC rows in RUPEES at `res_min` spacing from `start` (bar OPEN times)."""
    idx = pd.date_range(start, periods=len(rows), freq=f"{res_min}min")
    o, h, l, c = (list(col) for col in zip(*rows, strict=True))
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": 1e3}, index=idx)


def position(
    direction: str = "long",
    *,
    kind: str = "STOCK",
    entry: int = 100_000,
    stop: int | None = None,
    target: int | None = -1,
    option: dict | None = None,
    opened: dt.datetime = OPENED,
) -> PositionState:
    """A freshly filled position (nothing managed yet); levels in paise; `opened` is the
    fill bar's open."""
    sign = 1 if direction == "long" else -1
    stop = entry - sign * 500 if stop is None else stop
    target = entry + sign * 1000 if target == -1 else target
    return PositionState(
        direction=direction,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        qty=1 if kind == "STOCK" else 30,
        entry_paise=entry,
        initial_stop_paise=stop,
        stop_paise=stop,
        target_paise=target,
        trail_active=False,
        opened_at=opened,
        last_bar_ts=None,
        bars_held=0,
        sessions_held=0,
        option=option,
        trade_id=7,
    )


def step(
    state: PositionState,
    rows: Sequence[Row],
    *,
    now: dt.datetime | None = None,
    rules: ManageRules = RULES,
    strategy_exit: StrategyExit | None = None,
    start: dt.datetime = OPENED,
) -> tuple[PositionState, list[PaperEvent]]:
    """manage_step over `rows`; by default `now` is the close of the last row."""
    now = now or start + dt.timedelta(minutes=len(rows))
    return manage_step(state, frame(rows, start), rules, now, strategy_exit)


def at(minute: int) -> dt.datetime:
    return ist(THU, 10, minute)


# ── AC1: the stop ──────────────────────────────────────────────────────────────


class TestAC1Stop:
    def test_a_touch_of_the_stop_closes_at_the_stop(self) -> None:
        state, events = step(position(), [(998.00, 999.00, 995.00, 997.00)])
        assert state.closed == PaperClose("STOP", 99_500, at(6), gap=False)
        assert [e.kind for e in events] == ["CLOSED"]

    def test_one_tick_short_of_the_stop_does_not_close(self) -> None:
        state, events = step(position(), [(998.00, 999.00, 995.05, 997.00)])
        assert state.closed is None and events == []
        assert state.last_bar_ts == at(6)

    def test_a_gap_through_the_stop_closes_at_the_open(self) -> None:
        state, _ = step(position(), [QUIET, (994.00, 996.00, 993.00, 995.00)])
        assert state.closed == PaperClose("STOP", 99_400, at(7), gap=True)

    def test_an_open_exactly_at_the_stop_is_a_touch_not_a_gap(self) -> None:
        # Same rule as the fill (Manager decision 4): a gap is STRICTLY beyond the stop.
        state, _ = step(position(), [QUIET, (995.00, 996.00, 994.00, 995.50)])
        assert state.closed == PaperClose("STOP", 99_500, at(7), gap=False)

    def test_stop_beats_target_on_the_same_bar(self) -> None:
        state, events = step(position(), [(1000.00, 1010.05, 995.00, 1000.00)])
        assert state.closed == PaperClose("STOP", 99_500, at(6), gap=False)
        assert len(events) == 1

    def test_a_short_stops_on_a_touch_from_below(self) -> None:
        state, _ = step(position("short"), [(1002.00, 1005.00, 1001.00, 1003.00)])
        assert state.closed == PaperClose("STOP", 100_500, at(6), gap=False)

    def test_a_short_one_tick_short_of_the_stop_does_not_close(self) -> None:
        state, _ = step(position("short"), [(1002.00, 1004.95, 1001.00, 1003.00)])
        assert state.closed is None

    def test_a_short_gapping_up_through_the_stop_closes_at_the_open(self) -> None:
        state, _ = step(position("short"), [QUIET, (1006.00, 1007.00, 1005.50, 1006.50)])
        assert state.closed == PaperClose("STOP", 100_600, at(7), gap=True)

    def test_a_short_stop_beats_its_target_on_the_same_bar(self) -> None:
        state, _ = step(position("short"), [(1000.00, 1005.00, 989.95, 1000.00)])
        assert state.closed == PaperClose("STOP", 100_500, at(6), gap=False)

    def test_buy_pe_is_short_the_underlying_with_the_stop_above(self) -> None:
        option = {"strike": 45400.0, "opt_type": "PE", "expiry": "2026-11-24", "iv": 0.15}
        pe = position(
            "short", kind="INDEX", entry=4_521_035, stop=4_531_035, target=4_501_035, option=option
        )
        rows = [(45215.00, 45310.35, 45200.00, 45250.00)]
        state, _ = step(pe, rows)
        assert state.closed == PaperClose("STOP", 4_531_035, at(6), gap=False)
        assert state.option == option  # carried for the book's premium repricing

    def test_buy_pe_target_is_below_on_the_underlying(self) -> None:
        pe = position("short", kind="INDEX", entry=4_521_035, stop=4_531_035, target=4_501_035)
        state, _ = step(pe, [(45150.00, 45160.00, 45010.30, 45020.00)])
        assert state.closed == PaperClose("TARGET", 4_501_035, at(6), gap=False)

    def test_the_fill_bar_itself_is_managed(self) -> None:
        state, _ = step(position(), [(1000.00, 1000.50, 994.50, 995.50)])
        assert state.closed is not None and state.closed.at == OPENED

    def test_bars_before_the_fill_are_ignored(self) -> None:
        early = (996.00, 997.00, 990.00, 991.00)  # 10:05, before the fill: below the stop
        state, events = step(position(), [early, QUIET], start=at(5))
        assert state.closed is None and events == []
        assert state.last_bar_ts == at(6)

    def test_a_forming_bar_is_not_used(self) -> None:
        rows = [(998.00, 999.00, 995.00, 997.00)]
        state, events = step(position(), rows, now=at(6) + dt.timedelta(seconds=59))
        assert (state.closed, events, state.last_bar_ts) == (None, [], None)
        state, _ = step(state, rows, now=at(7))
        assert state.closed == PaperClose("STOP", 99_500, at(6), gap=False)

    def test_only_the_first_close_is_booked(self) -> None:
        hit = (998.00, 999.00, 994.00, 995.00)
        state, events = step(position(), [hit, hit, hit])
        assert [e.kind for e in events] == ["CLOSED"]
        assert state.closed is not None and state.closed.at == at(6)
        assert state.last_bar_ts == at(6)

    def test_stepping_again_books_nothing(self) -> None:
        rows = [QUIET, QUIET, (998.00, 999.00, 995.00, 997.00)]
        once, first = step(position(), rows)
        twice, again = step(once, rows)
        assert len(first) == 1 and again == []
        assert twice == once

    def test_an_open_position_stepped_again_books_nothing(self) -> None:
        once, _ = step(position(), [QUIET, QUIET])
        twice, again = step(once, [QUIET, QUIET])
        assert again == [] and twice == once

    def test_the_input_state_is_not_mutated(self) -> None:
        state = position(option={"strike": 45000.0})
        before = copy.deepcopy(state)
        step(state, [QUIET, (998.00, 999.00, 995.00, 997.00)])
        assert state == before

    def test_the_closed_event_is_final_and_stores_its_bar(self) -> None:
        _, (event,) = step(position(), [(998.00, 999.00, 995.00, 997.00)])
        assert (event.trade_id, event.ts, event.kind) == (7, at(6), "CLOSED")
        assert event.detail["reason"] == "STOP"
        assert event.detail["price_paise"] == 99_500
        assert event.detail["gap"] is False
        assert event.detail["bar_ts"] == at(6).isoformat()
        assert [event.detail[k] for k in BAR_KEYS] == [99_800, 99_900, 99_500, 99_700]
        assert event.detail["stop_paise"] == 99_500


# ── AC2: the target ────────────────────────────────────────────────────────────


class TestAC2Target:
    def test_reaching_the_target_exactly_does_not_fill(self) -> None:
        state, events = step(position(), [(1005.00, 1010.00, 1004.00, 1009.00)])
        assert state.closed is None and events == []

    def test_trading_through_by_one_tick_fills_at_exactly_the_target(self) -> None:
        state, _ = step(position(), [(1005.00, 1010.05, 1004.00, 1009.00)])
        assert state.closed == PaperClose("TARGET", 101_000, at(6), gap=False)

    def test_a_gap_up_beyond_the_target_still_fills_at_the_target(self) -> None:
        state, _ = step(position(), [QUIET, (1012.00, 1013.00, 1011.00, 1012.50)])
        assert state.closed == PaperClose("TARGET", 101_000, at(7), gap=True)

    def test_an_open_one_tick_through_the_target_is_a_gap(self) -> None:
        state, _ = step(position(), [QUIET, (1010.05, 1010.05, 1009.00, 1009.50)])
        assert state.closed == PaperClose("TARGET", 101_000, at(7), gap=True)

    def test_an_open_at_the_target_that_never_trades_through_does_not_fill(self) -> None:
        state, _ = step(position(), [QUIET, (1010.00, 1010.00, 1008.00, 1009.00)])
        assert state.closed is None

    def test_a_short_target_fills_one_tick_through_at_exactly_the_target(self) -> None:
        state, _ = step(position("short"), [(995.00, 996.00, 989.95, 991.00)])
        assert state.closed == PaperClose("TARGET", 99_000, at(6), gap=False)

    def test_a_short_reaching_its_target_exactly_does_not_fill(self) -> None:
        state, _ = step(position("short"), [(995.00, 996.00, 990.00, 991.00)])
        assert state.closed is None

    def test_a_short_gapping_down_beyond_the_target_fills_at_the_target(self) -> None:
        state, _ = step(position("short"), [QUIET, (988.00, 989.00, 987.00, 988.50)])
        assert state.closed == PaperClose("TARGET", 99_000, at(7), gap=True)

    def test_no_target_means_no_target_close(self) -> None:
        state, _ = step(position(target=None), [(1005.00, 1100.00, 1004.00, 1090.00)])
        assert state.closed is None


# ── AC3: the trailing stop ─────────────────────────────────────────────────────


class TestAC3Trail:
    def test_the_trail_ratchets_at_the_close_and_applies_from_the_next_bar(self) -> None:
        # close 1005.00 is +1R (risk 5.00): stop -> 1005.00 - 1 x 2.00 = 1003.00. This bar's
        # own low 999.50 is below 1003.00, yet the new stop must not act within the bar.
        bar1 = (1000.00, 1005.50, 999.50, 1005.00)
        state, events = step(position(target=None), [bar1], rules=TRAIL)
        assert state.closed is None
        assert (state.stop_paise, state.trail_active) == (100_300, True)
        state, _ = step(state, [bar1, (1004.00, 1004.50, 1003.00, 1003.50)], rules=TRAIL)
        assert state.closed == PaperClose("TRAIL_STOP", 100_300, at(7), gap=False)

    def test_a_moved_stop_is_booked_at_the_bar_close(self) -> None:
        _, events = step(position(target=None), [(1000.00, 1005.50, 999.50, 1005.00)], rules=TRAIL)
        assert [(e.kind, e.ts) for e in events] == [("STOP_MOVED", at(7))]
        assert (events[0].detail["from_paise"], events[0].detail["to_paise"]) == (99_500, 100_300)

    def test_activation_is_judged_on_the_close_not_the_high(self) -> None:
        # high +2R, close 1004.95 is 0.99R: no trail (a deliberate difference from the Lab)
        state, events = step(
            position(target=None), [(1000.00, 1010.00, 999.50, 1004.95)], rules=TRAIL
        )
        assert (state.stop_paise, state.trail_active, events) == (99_500, False, [])

    def test_the_trail_never_loosens(self) -> None:
        rows = [
            (1000.00, 1005.50, 999.50, 1005.00),  # -> 1003.00
            (1005.00, 1006.50, 1004.50, 1006.00),  # -> 1004.00
            (1006.00, 1006.50, 1005.00, 1005.50),  # +1.1R, candidate 1003.50: kept 1004.00
        ]
        state, _ = step(position(target=None), rows, rules=TRAIL)
        assert state.closed is None and state.stop_paise == 100_400

    @pytest.mark.parametrize("atr", [None, 0.0, math.nan])
    def test_an_unusable_frozen_atr_disables_the_trail(self, atr: float | None) -> None:
        rules = replace(TRAIL, atr=atr)
        rows = [(1000.00, 1010.00, 999.50, 1009.00), (1000.00, 1001.00, 995.00, 996.00)]
        state, _ = step(position(target=None), rows, rules=rules)
        assert state.closed == PaperClose("STOP", 99_500, at(7), gap=False)
        assert state.trail_active is False

    def test_no_fallback_to_an_atr_column_in_the_bars(self) -> None:
        bars = frame([(1000.00, 1010.00, 999.50, 1009.00)]).assign(atr=2.0, atr_trail=2.0)
        state, _ = manage_step(position(target=None), bars, replace(TRAIL, atr=None), at(7))
        assert (state.stop_paise, state.trail_active) == (99_500, False)

    def test_an_activated_trail_that_never_moves_the_stop_still_closes_stop(self) -> None:
        # ATR 5.00: candidate 1005.00 - 5.00 = 1000.00 > 995.00 would move; ATR 10.00 gives
        # exactly the initial 995.00, so the stop never moved: STOP, not TRAIL_STOP.
        rules = replace(TRAIL, atr=10.0)
        rows = [(1000.00, 1005.50, 999.50, 1005.00), (1000.00, 1001.00, 995.00, 996.00)]
        state, events = step(position(target=None), rows, rules=rules)
        assert state.closed == PaperClose("STOP", 99_500, at(7), gap=False)
        assert [e.kind for e in events] == ["CLOSED"]

    def test_a_short_trail_mirrors(self) -> None:
        rows = [(1000.00, 1000.50, 994.50, 995.00), (996.00, 997.00, 995.50, 996.50)]
        state, _ = step(position("short", target=None), rows, rules=TRAIL)
        assert state.closed == PaperClose("TRAIL_STOP", 99_700, at(7), gap=False)

    def test_a_trailed_stop_snaps_to_the_tick_against_the_trader(self) -> None:
        # 1005.00 - 2.03 = 1002.97 -> a long's stop floors to 1002.95
        rules = replace(TRAIL, atr=2.03)
        state, _ = step(position(target=None), [(1000.00, 1005.50, 999.50, 1005.00)], rules=rules)
        assert state.stop_paise == 100_295

    def test_a_gap_through_a_trailed_stop_closes_at_the_open(self) -> None:
        rows = [(1000.00, 1005.50, 999.50, 1005.00), (1002.00, 1002.50, 1001.00, 1002.00)]
        state, _ = step(position(target=None), rows, rules=TRAIL)
        assert state.closed == PaperClose("TRAIL_STOP", 100_200, at(7), gap=True)

    def test_the_stop_moved_event_stores_the_bar_whose_close_moved_it(self) -> None:
        bar1 = (1000.00, 1005.50, 999.50, 1005.00)
        _, (moved,) = step(position(target=None), [bar1], rules=TRAIL)
        assert (moved.trade_id, moved.detail["bar_ts"]) == (7, at(6).isoformat())
        assert [moved.detail[k] for k in BAR_KEYS] == [100_000, 100_550, 99_950, 100_500]

    def test_a_nan_activation_level_disables_the_trail(self) -> None:
        # `r < nan` is False: without the guard a NaN would arm the trail on every bar
        rules = replace(TRAIL, trail_activate_r=math.nan)
        state, events = step(
            position(target=None), [(1000.00, 1001.00, 999.50, 1000.00)], rules=rules
        )
        assert (state.stop_paise, state.trail_active, events) == (99_500, False, [])

    def test_a_zero_risk_position_never_arms_the_trail(self) -> None:
        flat_risk = position(stop=100_000, target=None)  # entry == stop: there is no R
        state, events = step(flat_risk, [(1001.00, 1005.50, 1000.50, 1005.00)], rules=TRAIL)
        assert (state.stop_paise, state.trail_active, events) == (100_000, False, [])

    # Review round 1, MAJOR-1: the trail is the STRATEGY's, so it arms and ratchets only at
    # strategy-bar closes. Strategy bars are tf_min wide from 09:15 IST, like resample_bars.

    def test_on_a_slower_strategy_it_moves_only_at_the_strategy_bar_close(self) -> None:
        rules = replace(TRAIL, tf_min=5)  # strategy bars [10:05, 10:10), [10:10, 10:15)
        rows = [
            (1000.00, 1005.50, 999.50, 1005.00),  # 10:06: +1.0 R, but mid strategy bar
            (1005.00, 1007.00, 1004.50, 1006.50),  # 10:07
            (1006.50, 1008.00, 1006.00, 1007.50),  # 10:08
            (1007.50, 1008.00, 1006.50, 1007.00),  # 10:09 closes it: 1007.00 - 2.00
        ]
        state, events = step(position(target=None), rows, rules=rules)
        assert [(e.kind, e.ts, e.detail["to_paise"]) for e in events] == [
            ("STOP_MOVED", at(10), 100_500)
        ]
        assert (state.stop_paise, state.trail_active) == (100_500, True)

    def test_a_fine_close_mid_strategy_bar_moves_nothing_a_later_dip_can_touch(self) -> None:
        rules = replace(TRAIL, tf_min=5)
        rows = [(1000.00, 1005.50, 999.50, 1005.00), (1005.00, 1005.50, 1003.00, 1004.00)]
        state, events = step(position(target=None), rows, rules=rules)
        assert (state.closed, events, state.stop_paise) == (None, [], 99_500)

    def test_coarse_fine_bars_close_a_strategy_bar_when_they_cross_its_end(self) -> None:
        # resample_bars puts the 2-minute bars of 10:05, 10:07 and 10:09 into the 10:05
        # strategy bar; the 10:09 bar closes at 10:11, past its end, so it is the last one.
        rows = [
            (1000.00, 1005.50, 999.50, 1005.00),
            (1005.00, 1006.00, 1004.50, 1005.50),
            (1005.50, 1006.50, 1005.00, 1006.00),
        ]
        bars = frame(rows, at(5), res_min=2)
        rules = replace(TRAIL, tf_min=5)
        state, events = manage_step(position(target=None, opened=at(5)), bars, rules, at(11))
        assert [(e.kind, e.ts, e.detail["to_paise"]) for e in events] == [
            ("STOP_MOVED", at(11), 100_400)
        ]


# ── AC4: strategy exits ────────────────────────────────────────────────────────


def exit_at(minute: int, reason: str = "SIGNAL_EXIT") -> StrategyExit:
    return StrategyExit(reason, at(minute))  # type: ignore[arg-type]


FOUR_QUIET = [QUIET] * 4  # 10:06 .. 10:09


class TestAC4StrategyExit:
    def test_a_signal_exit_closes_at_the_first_fine_open_after_the_signal_bar(self) -> None:
        rows = [*FOUR_QUIET, (1002.00, 1003.00, 1001.00, 1002.50), QUIET]
        state, events = step(position(), rows, strategy_exit=exit_at(10))
        assert state.closed == PaperClose("SIGNAL_EXIT", 100_200, at(10), gap=False)
        assert [e.kind for e in events] == ["CLOSED"]

    def test_an_opposite_signal_closes_the_same_way(self) -> None:
        rows = [*FOUR_QUIET, (1002.00, 1003.00, 1001.00, 1002.50)]
        state, _ = step(position(), rows, strategy_exit=exit_at(10, "OPPOSITE_SIGNAL"))
        assert state.closed == PaperClose("OPPOSITE_SIGNAL", 100_200, at(10), gap=False)

    def test_the_exit_waits_for_the_next_closed_fine_bar_across_steps(self) -> None:
        rows = [*FOUR_QUIET, (1002.00, 1003.00, 1001.00, 1002.50)]
        forming = at(10) + dt.timedelta(seconds=30)
        state, events = step(position(), rows, now=forming, strategy_exit=exit_at(10))
        assert (state.closed, events) == (None, [])
        state, _ = step(state, rows, now=at(11))  # the callback no longer says so
        assert state.closed == PaperClose("SIGNAL_EXIT", 100_200, at(10), gap=False)

    @pytest.mark.parametrize("minute", [5, 6])
    def test_a_signal_bar_that_closed_before_the_fill_is_ignored(self, minute: int) -> None:
        state, events = step(position(), [QUIET] * 5, strategy_exit=exit_at(minute))
        assert (state.closed, events) == (None, [])

    def test_a_signal_bar_closing_after_now_is_look_ahead(self) -> None:
        with pytest.raises(ValueError, match="bar_close_ts"):
            step(position(), [QUIET] * 5, now=at(11), strategy_exit=exit_at(15))

    def test_a_gap_through_the_stop_at_the_due_open_closes_stop(self) -> None:
        rows = [*FOUR_QUIET, (994.00, 995.00, 993.00, 994.50)]
        state, _ = step(position(), rows, strategy_exit=exit_at(10))
        assert state.closed == PaperClose("STOP", 99_400, at(10), gap=True)

    def test_a_gap_through_the_target_at_the_due_open_closes_target(self) -> None:
        rows = [*FOUR_QUIET, (1011.00, 1012.00, 1010.50, 1011.50)]
        state, _ = step(position(), rows, strategy_exit=exit_at(10))
        assert state.closed == PaperClose("TARGET", 101_000, at(10), gap=True)

    @pytest.mark.parametrize(
        "bar",
        [
            (1000.00, 1001.00, 994.00, 996.00),  # the stop is touched later in the bar
            (1000.00, 1012.00, 999.00, 1011.00),  # the target trades through later
        ],
    )
    def test_an_exit_due_at_the_open_closes_before_anything_inside_the_bar(self, bar: Row) -> None:
        state, _ = step(position(), [*FOUR_QUIET, bar], strategy_exit=exit_at(10))
        assert state.closed == PaperClose("SIGNAL_EXIT", 100_000, at(10), gap=False)

    def test_signal_exit_beats_opposite_signal_due_on_the_same_open(self) -> None:
        rows = [*FOUR_QUIET, (1002.00, 1003.00, 1001.00, 1002.50)]
        forming = at(10) + dt.timedelta(seconds=30)
        state, _ = step(position(), rows, now=forming, strategy_exit=exit_at(10, "OPPOSITE_SIGNAL"))
        state, _ = step(state, rows, now=at(11), strategy_exit=exit_at(10))
        assert state.closed == PaperClose("SIGNAL_EXIT", 100_200, at(10), gap=False)

    def test_an_earlier_pending_exit_is_not_displaced_by_a_later_one(self) -> None:
        rows = [*FOUR_QUIET, (1002.00, 1003.00, 1001.00, 1002.50), QUIET]
        forming = at(8) + dt.timedelta(seconds=30)
        state, _ = step(
            position(), rows[:3], now=forming, strategy_exit=exit_at(8, "OPPOSITE_SIGNAL")
        )
        state, _ = step(state, rows, strategy_exit=exit_at(10))
        assert state.closed == PaperClose("OPPOSITE_SIGNAL", 100_000, at(8), gap=False)

    def test_an_exit_reported_when_no_new_bar_has_closed_is_remembered(self) -> None:
        rows = [*FOUR_QUIET, (1002.00, 1003.00, 1001.00, 1002.50)]
        state, _ = step(position(), rows, now=at(10))  # 10:06 .. 10:09 managed
        half = at(10) + dt.timedelta(seconds=30)
        state, events = step(state, rows, now=half, strategy_exit=exit_at(10))
        assert (events, state.pending_exit, state.last_bar_ts) == ([], exit_at(10), at(9))
        state, _ = step(state, rows, now=at(11))
        assert state.closed == PaperClose("SIGNAL_EXIT", 100_200, at(10), gap=False)
        assert state.pending_exit is None

    def test_an_exit_reported_after_its_bar_was_managed_closes_at_the_next_open(self) -> None:
        # Booked events are final: the open at B's close is past, so the first bar not yet
        # managed is the first open the paper trade can still act on.
        rows = [*FOUR_QUIET, QUIET, QUIET, (1001.00, 1002.00, 1000.00, 1001.50)]  # .. 10:12
        state, _ = step(position(), rows, now=at(12))  # 10:06 .. 10:11 managed
        state, _ = step(state, rows, now=at(13), strategy_exit=exit_at(10))
        assert state.closed == PaperClose("SIGNAL_EXIT", 100_100, at(12), gap=False)

    # Tester BUG-1 (review round 1): a report AHEAD of the caller's data time (a delayed feed)
    # is remembered with remember_exit, never dropped, and closes only at an open the data
    # has reached.

    def test_an_exit_ahead_of_the_data_is_remembered_and_closes_once_the_data_gets_there(
        self,
    ) -> None:
        rows = [*FOUR_QUIET, (1002.00, 1003.00, 1001.00, 1002.50), QUIET]  # .. 10:11
        waiting = remember_exit(position(), exit_at(10))  # the data has only reached 10:05
        assert waiting.pending_exit == exit_at(10)
        state, events = step(waiting, rows, now=at(10))  # data 10:10: the exit bar not closed
        assert (state.closed, events, state.pending_exit) == (None, [], exit_at(10))
        state, _ = step(state, rows, now=at(11))
        assert state.closed == PaperClose("SIGNAL_EXIT", 100_200, at(10), gap=False)

    def test_remembering_keeps_the_earliest_exit_and_ignores_a_stale_one(self) -> None:
        early = remember_exit(position(), exit_at(10, "OPPOSITE_SIGNAL"))
        assert remember_exit(early, exit_at(12)).pending_exit == exit_at(10, "OPPOSITE_SIGNAL")
        assert remember_exit(position(), exit_at(6)).pending_exit is None  # at the fill: stale

    def test_remembering_rejects_anything_but_a_strategy_exit(self) -> None:
        with pytest.raises(ValueError, match="StrategyExit"):
            remember_exit(position(), ("SIGNAL_EXIT", at(10)))  # type: ignore[arg-type]


# ── AC5: the session end and the time stop (written red for Dev Beta, T3) ──────


def when(hh: int, mm: int, ss: int = 0, day: dt.date = THU) -> dt.datetime:
    return ist(day, hh, mm, ss)


def frame_at(stamps: Sequence[dt.datetime], rows: Sequence[Row]) -> pd.DataFrame:
    """OHLC rows in RUPEES on explicit bar-open stamps (holes, two sessions)."""
    o, h, l, c = (list(col) for col in zip(*rows, strict=True))
    return pd.DataFrame(
        {"open": o, "high": h, "low": l, "close": c, "volume": 1e3},
        index=pd.DatetimeIndex(list(stamps)),
    )


LATE = when(15, 10)  # a fill five minutes before the 15:15 exit time
EXIT_BAR = (1000.50, 1001.00, 999.50, 1000.75)  # the exit bar: open 100_050
LAST_BAR = (1000.00, 1001.00, 999.00, 1000.35)  # a session's last bar: close 100_035
NEXT_DAY = (900.00, 901.00, 899.00, 900.50)  # the next morning, far through the stop


class TestAC5SessionEnd:
    def test_it_closes_at_the_first_open_at_or_after_exit_time(self) -> None:
        rows = [QUIET] * 5 + [EXIT_BAR, QUIET]  # 15:10 .. 15:16
        state, events = step(position(opened=LATE), rows, start=LATE)
        assert state.closed == PaperClose("SESSION_END", 100_050, when(15, 15), gap=False)
        assert [(e.kind, e.ts) for e in events] == [("CLOSED", when(15, 15))]

    def test_with_no_bar_at_exit_time_the_next_open_is_used(self) -> None:
        stamps = [when(15, 10 + i) for i in range(5)] + [when(15, 17)]
        bars = frame_at(stamps, [QUIET] * 5 + [EXIT_BAR])
        state, _ = manage_step(position(opened=LATE), bars, RULES, when(15, 18))
        assert state.closed == PaperClose("SESSION_END", 100_050, when(15, 17), gap=False)

    def test_its_open_beats_a_stop_touched_later_inside_that_bar(self) -> None:
        rows = [QUIET] * 5 + [(1000.50, 1001.00, 994.00, 996.00)]
        state, _ = step(position(opened=LATE), rows, start=LATE)
        assert state.closed == PaperClose("SESSION_END", 100_050, when(15, 15), gap=False)

    def test_a_gap_through_the_stop_at_that_open_is_a_stop(self) -> None:
        rows = [QUIET] * 5 + [(994.00, 995.00, 993.00, 994.50)]
        state, _ = step(position(opened=LATE), rows, start=LATE)
        assert state.closed == PaperClose("STOP", 99_400, when(15, 15), gap=True)

    def test_it_outranks_a_strategy_exit_due_at_the_same_open(self) -> None:
        signal = StrategyExit("SIGNAL_EXIT", when(15, 15))
        rows = [QUIET] * 5 + [EXIT_BAR]
        state, _ = step(position(opened=LATE), rows, start=LATE, strategy_exit=signal)
        assert state.closed == PaperClose("SESSION_END", 100_050, when(15, 15), gap=False)

    def test_a_custom_exit_time_is_honoured(self) -> None:
        rules = replace(RULES, exit_time=dt.time(15, 12))
        rows = [QUIET, QUIET, EXIT_BAR, QUIET]  # 15:10 .. 15:13
        state, _ = step(position(opened=LATE), rows, start=LATE, rules=rules)
        assert state.closed == PaperClose("SESSION_END", 100_050, when(15, 12), gap=False)

    def test_a_first_step_on_the_next_day_closes_at_the_opening_days_exit_open(self) -> None:
        stamps = [when(15, 10 + i) for i in range(7)] + [when(9, 15 + i, day=FRI) for i in range(3)]
        rows = [QUIET] * 5 + [EXIT_BAR, QUIET] + [NEXT_DAY] * 3
        bars = frame_at(stamps, rows)
        state, events = manage_step(position(opened=LATE), bars, RULES, when(9, 30, day=FRI))
        assert state.closed == PaperClose("SESSION_END", 100_050, when(15, 15), gap=False)
        assert [e.kind for e in events] == ["CLOSED"]


class TestAC5SessionEndFallback:
    """No bar at or after exit_time: once the session is over (15:30 IST on the opening date,
    as `fill.SESSION_CLOSE_IST`, or a bar of a later date exists) the trade closes at the
    CLOSE of the last bar it managed, stamped at that bar's close time (decision A5)."""

    START = when(15, 5)
    ROWS = [QUIET] * 4 + [LAST_BAR]  # 15:05 .. 15:09

    def opened(self) -> PositionState:
        return position(opened=self.START)

    def bars(self, rows: Sequence[Row] | None = None) -> pd.DataFrame:
        return frame(self.ROWS if rows is None else rows, self.START)

    def test_once_the_session_is_over_it_closes_at_the_last_bars_close(self) -> None:
        state, (event,) = manage_step(self.opened(), self.bars(), RULES, when(15, 30))
        assert state.closed == PaperClose("SESSION_END", 100_035, when(15, 10), gap=False)
        assert (event.kind, event.ts) == ("CLOSED", when(15, 10))
        assert (event.detail["reason"], event.detail["price_paise"]) == ("SESSION_END", 100_035)

    def test_not_before_the_session_is_over(self) -> None:
        # a quiet minute or a late feed: 15:15 .. 15:29 may still arrive
        state, events = manage_step(self.opened(), self.bars(), RULES, when(15, 29, 59))
        assert (state.closed, events, state.last_bar_ts) == (None, [], when(15, 9))

    def test_the_last_bar_may_have_been_managed_by_an_earlier_step(self) -> None:
        state, _ = manage_step(self.opened(), self.bars(), RULES, when(15, 10))
        assert (state.closed, state.last_bar_ts) == (None, when(15, 9))
        state, (event,) = manage_step(state, self.bars(), RULES, when(15, 31))
        assert state.closed == PaperClose("SESSION_END", 100_035, when(15, 10), gap=False)
        assert event.ts == when(15, 10)

    def test_it_uses_the_close_the_position_saw_not_a_later_revision(self) -> None:
        # AC6: a bar the position has already seen is never evaluated again
        state, _ = manage_step(self.opened(), self.bars(), RULES, when(15, 10))
        revised = self.bars([QUIET] * 4 + [(1000.00, 1001.00, 999.00, 1000.95)])
        state, _ = manage_step(state, revised, RULES, when(15, 31))
        assert state.closed is not None and state.closed.price_paise == 100_035

    # Review round 1, MINOR-1: a position whose session is over without one usable bar gets a
    # final state, SESSION_END with NO price (none is invented), at the session's close.

    def test_one_that_never_saw_a_usable_bar_closes_without_a_price(self) -> None:
        nan = float("nan")
        bars = self.bars([(nan, nan, nan, nan)] * 5)
        state, (event,) = manage_step(self.opened(), bars, RULES, when(15, 31))
        assert state.closed == PaperClose("SESSION_END", None, when(15, 30), gap=False)
        assert (event.kind, event.ts) == ("CLOSED", when(15, 30))
        assert (event.detail["price_paise"], event.detail["fallback"]) == (None, "no_usable_bar")
        assert state.last_bar_ts == when(15, 9)  # the cursor still moved past the garbage

    def test_one_that_never_saw_any_bar_closes_without_a_price_too(self) -> None:
        state, (event,) = manage_step(self.opened(), self.bars().iloc[:0], RULES, when(15, 30))
        assert state.closed == PaperClose("SESSION_END", None, when(15, 30), gap=False)
        assert (state.last_bar_ts, event.ts) == (None, when(15, 30))

    def test_not_before_its_session_is_over(self) -> None:
        state, events = manage_step(self.opened(), self.bars().iloc[:0], RULES, when(15, 29))
        assert (state.closed, events) == (None, [])

    def test_the_last_close_seen_is_remembered_with_its_close_time(self) -> None:
        state, _ = manage_step(self.opened(), self.bars(), RULES, when(15, 10))
        assert (state.last_close_paise, state.last_close_at) == (100_035, when(15, 10))

    def test_a_later_days_bar_ends_the_session_and_is_never_managed(self) -> None:
        stamps = [when(15, 5 + i) for i in range(5)] + [when(9, 15 + i, day=FRI) for i in range(3)]
        bars = frame_at(stamps, [*self.ROWS] + [NEXT_DAY] * 3)
        state, events = manage_step(self.opened(), bars, RULES, when(9, 20, day=FRI))
        assert state.closed == PaperClose("SESSION_END", 100_035, when(15, 10), gap=False)
        assert [e.kind for e in events] == ["CLOSED"]
        assert (state.last_bar_ts, state.sessions_held) == (when(15, 9), 1)


TIME_RULES = replace(RULES, max_bars=1, tf_min=5)  # one 5-minute bar from the 10:06 fill
DUE_BAR = (1000.20, 1001.00, 999.00, 1000.50)  # 10:11, when one strategy bar has passed


class TestAC5TimeStop:
    def test_it_closes_at_the_open_once_max_bars_strategy_bars_have_passed(self) -> None:
        state, events = step(position(), [QUIET] * 5 + [DUE_BAR, QUIET], rules=TIME_RULES)
        assert state.closed == PaperClose("TIME_STOP", 100_020, at(11), gap=False)
        assert [(e.kind, e.ts) for e in events] == [("CLOSED", at(11))]

    def test_not_before_the_last_allowed_bar_has_passed(self) -> None:
        state, events = step(position(), [QUIET] * 5, rules=TIME_RULES)  # 10:06 .. 10:10
        assert (state.closed, events) == (None, [])

    def test_its_open_beats_a_stop_touched_later_inside_that_bar(self) -> None:
        rows = [QUIET] * 5 + [(1000.20, 1001.00, 994.00, 996.00)]
        state, _ = step(position(), rows, rules=TIME_RULES)
        assert state.closed == PaperClose("TIME_STOP", 100_020, at(11), gap=False)

    def test_a_strategy_exit_due_at_the_same_open_outranks_it(self) -> None:
        rows = [QUIET] * 5 + [DUE_BAR]
        state, _ = step(position(), rows, rules=TIME_RULES, strategy_exit=exit_at(11))
        assert state.closed == PaperClose("SIGNAL_EXIT", 100_020, at(11), gap=False)

    def test_max_bars_zero_means_no_time_stop(self) -> None:
        state, _ = step(position(), [QUIET] * 30, rules=replace(RULES, tf_min=1))
        assert state.closed is None

    def test_a_negative_max_bars_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_bars"):
            manage_step(position(), frame([QUIET]), replace(RULES, max_bars=-1), at(7))

    def test_an_exit_time_that_is_not_a_time_is_rejected(self) -> None:
        rules = replace(RULES, exit_time="15:15")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="exit_time"):
            manage_step(position(), frame([QUIET]), rules, at(7))


# ── AC6: stepping is idempotent and catches up (written red for Dev Beta, T3) ──

TRAIL_THEN_SESSION_END = [  # 15:05 .. 15:16; risk 5.00, trail 1 x 2.00
    (1000.00, 1003.00, 999.50, 1002.00),  # +0.4 R
    (1002.00, 1005.50, 1001.50, 1005.00),  # +1.0 R: the stop -> 1003.00 from 15:07
    (1005.00, 1007.00, 1004.00, 1006.50),  # -> 1004.50 from 15:08
    (1006.50, 1008.00, 1006.00, 1007.50),  # -> 1005.50 from 15:09
    *[(1007.50, 1008.00, 1006.50, 1007.50)] * 6,  # 15:09 .. 15:14: no further move
    (1007.00, 1008.00, 1006.00, 1007.50),  # 15:15: SESSION_END at the open, 100_700
    (1007.50, 1008.00, 1006.50, 1007.50),
]


class TestAC6SteppingIsIdempotentAndCatchesUp:
    START = when(15, 5)

    def once(self, now: dt.datetime | None = None) -> tuple[PositionState, list[PaperEvent]]:
        bars = frame(TRAIL_THEN_SESSION_END, self.START)
        return manage_step(position(opened=self.START), bars, TRAIL, now or when(15, 17))

    def test_bar_by_bar_equals_all_at_once(self) -> None:
        once_state, once = self.once()
        bars = frame(TRAIL_THEN_SESSION_END, self.START)
        state, events = position(opened=self.START), []
        for k in range(1, len(TRAIL_THEN_SESSION_END) + 1):
            state, new = manage_step(state, bars, TRAIL, self.START + dt.timedelta(minutes=k))
            events += new
        assert [(e.kind, e.ts) for e in once] == [
            ("STOP_MOVED", when(15, 7)),
            ("STOP_MOVED", when(15, 8)),
            ("STOP_MOVED", when(15, 9)),
            ("CLOSED", when(15, 15)),
        ]
        assert events == once and state == once_state

    def test_the_same_now_and_bars_again_book_nothing(self) -> None:
        state, first = self.once()
        bars = frame(TRAIL_THEN_SESSION_END, self.START)
        again, more = manage_step(state, bars, TRAIL, when(15, 17))
        assert first[-1].kind == "CLOSED" and more == [] and again == state

    def test_event_times_are_bar_times_never_now(self) -> None:
        now = when(17, 3, 9)
        _, events = self.once(now)
        edges = {self.START + dt.timedelta(minutes=i) for i in range(13)}  # opens and closes
        assert events and events[-1].kind == "CLOSED"
        assert all(e.ts in edges and e.ts != now for e in events)

    def test_the_fallback_close_is_the_same_bar_by_bar_and_all_at_once(self) -> None:
        start = when(15, 5)
        stamps = [when(15, 5 + i) for i in range(5)] + [when(9, 15 + i, day=FRI) for i in range(3)]
        bars = frame_at(stamps, [QUIET] * 4 + [LAST_BAR] + [NEXT_DAY] * 3)
        once_state, once = manage_step(position(opened=start), bars, RULES, when(9, 20, day=FRI))
        nows = [when(15, 6 + i) for i in range(26)] + [when(9, 16 + i, day=FRI) for i in range(5)]
        state, events = position(opened=start), []
        for now in nows:  # 15:06 .. 15:31, then the next morning
            state, new = manage_step(state, bars, RULES, now)
            events += new
        assert [(e.kind, e.ts, e.detail["reason"]) for e in once] == [
            ("CLOSED", when(15, 10), "SESSION_END")
        ]
        assert events == once and state == once_state


# ── the contract ───────────────────────────────────────────────────────────────


class TestContract:
    def test_a_naive_now_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="aware"):
            manage_step(position(), frame([QUIET]), RULES, dt.datetime(2026, 10, 29, 10, 7))

    def test_a_naive_bar_index_is_rejected(self) -> None:
        bars = frame([QUIET]).tz_localize(None)
        with pytest.raises(ValueError, match="tz-aware"):
            manage_step(position(), bars, RULES, at(7))

    @pytest.mark.parametrize("column", ["open", "high", "low", "close"])
    def test_a_missing_price_column_is_rejected(self, column: str) -> None:
        with pytest.raises(ValueError, match=column):
            manage_step(position(), frame([QUIET]).drop(columns=column), RULES, at(7))

    def test_a_stop_on_the_wrong_side_of_the_entry_is_rejected(self) -> None:
        # a BUY_PE built as long would put the stop above the entry: a sign error upstream
        bad = position("long", stop=100_500)
        with pytest.raises(ValueError, match="stop"):
            manage_step(bad, frame([QUIET]), RULES, at(7))

    def test_a_looser_stop_than_the_initial_is_rejected(self) -> None:
        bad = replace(position(), stop_paise=99_000)
        with pytest.raises(ValueError, match="stop"):
            manage_step(bad, frame([QUIET]), RULES, at(7))

    def test_float_paise_are_rejected(self) -> None:
        bad = replace(position(), stop_paise=99_500.0)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="paise"):
            manage_step(bad, frame([QUIET]), RULES, at(7))

    @pytest.mark.parametrize("tick", [0, -5])
    def test_a_non_positive_tick_is_rejected(self, tick: int) -> None:
        with pytest.raises(ValueError, match="tick_paise"):
            manage_step(position(), frame([QUIET]), replace(RULES, tick_paise=tick), at(7))

    def test_an_unknown_strategy_exit_reason_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="reason"):
            StrategyExit("SQUARE_OFF", at(10))  # type: ignore[arg-type]

    def test_a_naive_strategy_exit_time_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="aware"):
            StrategyExit("SIGNAL_EXIT", dt.datetime(2026, 10, 29, 10, 10))

    def test_an_unusable_bar_is_skipped_not_guessed(self) -> None:
        rows = [(998.00, 999.00, math.nan, 997.00), QUIET]
        state, events = step(position(), rows)
        assert (state.closed, events, state.last_bar_ts) == (None, [], at(7))

    def test_an_open_below_the_low_still_gaps_the_stop(self) -> None:
        # Some feeds print the auction open apart from the continuous range: the bar is kept
        # and its open is judged on its own, then the event stores the feed's own prints.
        rows = [QUIET, (994.00, 997.00, 996.00, 996.50)]
        state, (event,) = step(position(), rows)
        assert state.closed == PaperClose("STOP", 99_400, at(7), gap=True)
        assert event.detail["bar_low_paise"] == 99_600

    def test_a_close_outside_the_high_low_is_not_a_touch(self) -> None:
        # Touches come from high/low (and the open); the close only feeds the trail.
        state, events = step(position(), [(998.00, 999.00, 997.00, 990.00)])
        assert (state.closed, events) == (None, [])

    def test_an_open_above_the_high_can_trade_a_long_through_its_target(self) -> None:
        rows = [QUIET, (1010.05, 1009.00, 1005.00, 1006.00)]
        state, _ = step(position(), rows)
        assert state.closed == PaperClose("TARGET", 101_000, at(7), gap=True)

    def test_a_bar_with_its_high_below_its_low_is_skipped(self) -> None:
        rows = [(998.00, 990.00, 999.00, 997.00), QUIET]
        state, events = step(position(), rows)
        assert (state.closed, events, state.last_bar_ts) == (None, [], at(7))

    def test_a_utc_indexed_frame_gives_the_same_answer(self) -> None:
        rows = [QUIET, (998.00, 999.00, 995.00, 997.00)]
        bars = frame(rows).tz_convert("UTC")
        state, _ = manage_step(position(), bars, RULES, at(8))
        assert state.closed == PaperClose("STOP", 99_500, at(7), gap=False)

    def test_a_duplicated_bar_time_uses_the_latest_revision(self) -> None:
        first = frame([QUIET])
        revised = frame([(998.00, 999.00, 995.00, 997.00)])
        state, _ = manage_step(position(), pd.concat([first, revised]), RULES, at(7))
        assert state.closed == PaperClose("STOP", 99_500, at(6), gap=False)

    def test_last_bar_and_holding_counters_advance(self) -> None:
        state, _ = step(position(), [QUIET] * 6)  # 10:06 .. 10:11, closed by 10:12
        assert state.last_bar_ts == at(11)
        assert state.bars_held == 1  # six minutes on a 5-minute timeframe
        assert state.sessions_held == 1
