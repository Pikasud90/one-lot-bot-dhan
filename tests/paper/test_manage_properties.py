"""manage_step properties (US-002 AC5-AC7): no look-ahead, catch-up equals watching, and no
trade outlives its session. Pure, offline and derandomized.

Prices are drawn as whole TICKS (5 paise) and reach the frame as `paise / 100`, the
correctly rounded float, so a level is only ever tied to a bar edge EXACTLY (a low equal to
the stop), never by float noise. Paths may run through 15:15 and into the next morning, with
or without a trail, a target, a time stop and a strategy exit.

  truncation   bars cut after bar m, same `now`              -> events up to bar m unchanged
  no hindsight bars AND clock cut at bar m's close            -> events up to bar m unchanged
  catch-up     any chunking of `now` over the same feed       -> same events, same final state
  one close    at most one CLOSED, last, and events in time order
  no carry     stepped the next morning, every trade is closed on its opening date (AC5)
"""

from __future__ import annotations

import datetime as dt
from typing import NamedTuple

import pandas as pd
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from onelot.clock import IST
from onelot.paper.events import PaperEvent
from onelot.paper.manage import ManageRules, PositionState, StrategyExit, manage_step

from .conftest import FRI, THU, ist

TICK = 5
MIN = dt.timedelta(minutes=1)
SESSION_CLOSE = dt.datetime.combine(THU, dt.time(15, 30), tzinfo=IST)
NEXT_MORNING = ist(FRI, 9, 20)  # after every generated bar has closed
PROP = settings(
    max_examples=60,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=list(HealthCheck),
)

Paise = tuple[int, int, int, int]


class Scenario(NamedTuple):
    state: PositionState
    rules: ManageRules
    stamps: list[dt.datetime]
    rows: list[Paise]
    exit: StrategyExit | None

    def bars(self, count: int | None = None) -> pd.DataFrame:
        k = len(self.rows) if count is None else count
        rupees = [[p / 100 for p in row] for row in self.rows[:k]]
        index = pd.DatetimeIndex(self.stamps[:k])
        return pd.DataFrame(rupees, columns=["open", "high", "low", "close"], index=index)

    @property
    def end(self) -> dt.datetime:
        """The close of the last bar: every bar is eligible."""
        return self.stamps[-1] + MIN

    def exit_known_at(self, now: dt.datetime) -> StrategyExit | None:
        """The callback's answer at `now`: a signal bar is reported once it has closed."""
        return self.exit if self.exit is not None and self.exit.bar_close_ts <= now else None

    def run(
        self, count: int | None = None, now: dt.datetime | None = None
    ) -> tuple[PositionState, list[PaperEvent]]:
        now = now or self.end
        return manage_step(self.state, self.bars(count), self.rules, now, self.exit_known_at(now))


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    direction = draw(st.sampled_from(["long", "short"]))
    sign = 1 if direction == "long" else -1
    start = ist(THU, *draw(st.sampled_from([(10, 6), (14, 58), (15, 6)])))
    room = (SESSION_CLOSE - start) // MIN  # the session's last bar opens at 15:29
    n_today = draw(st.integers(2, min(room, 35)))
    n_next = draw(st.integers(0, 3))  # bars of the next morning (0: the feed ends today)
    stamps = [start + i * MIN for i in range(n_today)]
    stamps += [ist(FRI, 9, 15) + i * MIN for i in range(n_next)]

    price, rows = draw(st.integers(19_600, 20_400)), []  # in ticks: 980.00 .. 1020.00
    for _ in stamps:
        o = price + draw(st.sampled_from([0, 0, 0, 0, 0, -8, 8]))  # now and then a gap
        c = max(o + sign * draw(st.integers(0, 2)) + draw(st.integers(-8, 8)), 50)
        hi = max(o, c) + draw(st.integers(0, 5))
        lo = max(min(o, c) - draw(st.integers(0, 5)), 1)
        rows.append((o * TICK, hi * TICK, lo * TICK, c * TICK))
        price = c

    entry = rows[0][0] + sign * TICK * draw(st.integers(0, 2))
    stop = entry - sign * TICK * draw(st.integers(10, 60))
    target = draw(st.none() | st.integers(10, 150).map(lambda k: entry + sign * TICK * k))
    rules = ManageRules(
        target_r=2.0,
        trail_atr_mult=draw(st.sampled_from([0.0, 1.0, 2.0])),
        trail_activate_r=draw(st.sampled_from([0.5, 1.0])),
        atr=draw(st.sampled_from([None, 0.25, 0.5, 1.0])),
        max_bars=draw(st.sampled_from([0, 0, 4, 10])),
        tf_min=draw(st.sampled_from([1, 2, 5])),
        exit_time=dt.time(15, 15),
        tick_paise=TICK,
    )
    signal = None
    if draw(st.booleans()):
        reason = draw(st.sampled_from(["SIGNAL_EXIT", "OPPOSITE_SIGNAL"]))
        signal = StrategyExit(reason, start + draw(st.integers(1, n_today)) * MIN)  # type: ignore[arg-type]
    state = PositionState(
        direction=direction,  # type: ignore[arg-type]
        kind="STOCK",
        qty=1,
        entry_paise=entry,
        initial_stop_paise=stop,
        stop_paise=stop,
        target_paise=target,
        trail_active=False,
        opened_at=start,
        last_bar_ts=None,
        bars_held=0,
        sessions_held=0,
        option=None,
        trade_id=1,
    )
    return Scenario(state, rules, stamps, rows, signal)


def upto(events: list[PaperEvent], t: dt.datetime) -> list[PaperEvent]:
    return [e for e in events if e.ts <= t]


class TestNoLookAhead:
    @PROP
    @given(sc=scenarios(), cut=st.integers(0, 40))
    def test_truncating_the_bars_after_t_changes_no_event_up_to_t(
        self, sc: Scenario, cut: int
    ) -> None:
        m = min(cut, len(sc.rows) - 1)
        t = sc.stamps[m]
        _, full = sc.run()
        _, cut_events = sc.run(count=m + 1)
        assert upto(cut_events, t) == upto(full, t)

    @PROP
    @given(sc=scenarios(), cut=st.integers(0, 40))
    def test_a_step_that_only_knows_up_to_t_agrees_up_to_t(self, sc: Scenario, cut: int) -> None:
        m = min(cut, len(sc.rows) - 1)
        t = sc.stamps[m]
        _, full = sc.run()
        _, early = sc.run(count=m + 1, now=t + MIN)
        assert upto(early, t) == upto(full, t)


class TestCatchUp:
    @PROP
    @given(sc=scenarios(), sizes=st.lists(st.integers(1, 9), min_size=1, max_size=8))
    def test_any_chunking_of_now_gives_the_same_events_and_state(
        self, sc: Scenario, sizes: list[int]
    ) -> None:
        once_state, once = sc.run(now=NEXT_MORNING)
        nows = [s + MIN for s in sc.stamps] + [SESSION_CLOSE, NEXT_MORNING]
        nows = sorted(set(nows))
        bars = sc.bars()
        state, events, k = sc.state, [], 0
        for size in (*sizes, len(nows)):
            k = min(k + size, len(nows))
            now = nows[k - 1]
            state, new = manage_step(state, bars, sc.rules, now, sc.exit_known_at(now))
            events += new
        assert events == once
        assert state == once_state


class TestOneCloseInTimeOrder:
    @PROP
    @given(sc=scenarios())
    def test_at_most_one_close_it_comes_last_and_events_are_in_time_order(
        self, sc: Scenario
    ) -> None:
        state, events = sc.run(now=NEXT_MORNING)
        kinds = [e.kind for e in events]
        assert kinds.count("CLOSED") == (0 if state.closed is None else 1)
        if state.closed is not None:
            assert kinds[-1] == "CLOSED" and events[-1].ts == state.closed.at
        assert [e.ts for e in events] == sorted(e.ts for e in events)


class TestNoOvernightCarry:
    @PROP
    @given(sc=scenarios())
    def test_stepped_the_next_morning_every_trade_closed_on_its_opening_date(
        self, sc: Scenario
    ) -> None:
        state, events = sc.run(now=NEXT_MORNING)
        assert state.closed is not None, "no paper trade is carried overnight (AC5)"
        assert state.closed.at.astimezone(IST).date() == THU
        assert state.closed.at <= SESSION_CLOSE
        assert all(e.ts.astimezone(IST).date() == THU for e in events)
