"""manage_step: one OPEN paper trade managed bar by bar (US-002 AC1-AC4). Pure: no I/O.

Levels in PositionState are integer paise on the MANAGED series (the stock itself, or the
index under an option); bar frames arrive in rupees like the feed. Every price below sits
on the 5-paise tick grid so no assertion depends on a float tie.

Test list (ping-pong; Dev Beta T1 writes AC1/AC2 green and AC3/AC4 red):
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
      bar's open closes before anything inside that bar.
  Contract: naive now / naive index / missing columns / wrong-side stop / bad tick raise;
      an unusable bar is skipped; a UTC-indexed frame behaves like an IST one.
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
)

from .conftest import THU, ist

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
TRAIL = replace(RULES, trail_atr_mult=1.0, trail_activate_r=1.0, atr=2.0)  # ATR in RUPEES
QUIET = (1000.00, 1001.00, 999.00, 1000.50)  # a bar that touches nothing

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
) -> PositionState:
    """A freshly filled position (nothing managed yet); levels in paise."""
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
        opened_at=OPENED,
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
        assert event.detail["bar"] == {
            "open_paise": 99_800,
            "high_paise": 99_900,
            "low_paise": 99_500,
            "close_paise": 99_700,
        }
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

    def test_an_inconsistent_bar_is_skipped(self) -> None:
        # low above the open: a corrupt print, not a stop touch at 990
        rows = [(998.00, 999.00, 999.50, 990.00), QUIET]
        state, _ = step(position(), rows)
        assert state.closed is None

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
