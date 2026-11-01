"""US-002 AC7: no look-ahead. Given any trade and bars truncated after time t, every event up to
t is unchanged (and altering the bars after t changes nothing up to t either).

Property tests on `manage_step`. The generators work in whole TICKS (5 paise) and build every
bar from integers, so a level can never be tied to a bar edge by float noise: ties are only
ever the EXACT ones the rules define (a low equal to the stop, a high equal to the target).

Three statements are checked for random paths (gaps, wicks, either direction, trail on or off,
with or without a target, a time stop, a strategy exit, a session that runs into 15:15):

  A  same `now`, bars cut after bar m          -> events at or before bar m's open unchanged
  B  `now` cut to the close of bar m as well    -> events at or before bar m's open unchanged
  C  same `now`, every bar after m REPLACED     -> events at or before bar m's open unchanged

and the equivalence the story calls the outage catch-up:

  D  any chunking of `now` over the same feed   -> the same events and the same final state
"""

from __future__ import annotations

import datetime as dt
from typing import Any, NamedTuple

import pandas as pd
import us001_helpers as H
import us002_helpers as U
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

PROP = settings(
    max_examples=120,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=list(HealthCheck),
)
MIN = dt.timedelta(minutes=1)


class Case(NamedTuple):
    direction: str
    start: dt.datetime
    rows: list[tuple[int, int, int, int]]
    state_kw: dict[str, Any]
    rules_kw: dict[str, Any]
    se: tuple[str, int] | None  # (reason, minutes after start at which the strategy bar closed)

    def bars(self, rows=None) -> pd.DataFrame:
        return U.pbars(self.start, self.rows if rows is None else rows)

    def state(self):
        return U.mk_state(self.direction, opened_at=self.start, **self.state_kw)

    def rules(self):
        return U.mk_rules(**self.rules_kw)

    def strategy_exit(self, now: dt.datetime | None = None):
        if self.se is None:
            return None
        ts = self.start + self.se[1] * MIN
        return U.strategy_exit(self.se[0], ts) if now is None or now >= ts else None

    def end(self) -> dt.datetime:
        return self.start + len(self.rows) * MIN


@st.composite
def cases(draw) -> Case:
    direction = draw(st.sampled_from(["long", "short"]))
    sign = 1 if direction == "long" else -1
    late = draw(
        st.sampled_from([False, False, True])
    )  # a session that runs through 15:15, or a quiet morning
    # at most 29 bars: even the late session ends before 15:30, so no run here is a 'the session
    # is over' run (the end-of-session fallback is judged by the deterministic AC5 tests)
    n = draw(st.integers(3, 29))
    start = H.at(15, 0) if late else H.at(10, 6)
    price = draw(st.integers(19_800, 20_200))  # ticks of 5 paise: 990.00 .. 1010.00
    drift = sign * draw(
        st.integers(0, 4)
    )  # a trend in the trade's favour, so the trail gets to work
    rows = []
    for _ in range(n):
        o = price + draw(st.sampled_from([0, 0, 0, 0, 0, 0, -6, 6]))  # now and then a gap
        c = max(o + drift + draw(st.integers(-12, 12)), 50)
        hi = max(o, c) + draw(st.integers(0, 8))
        lo = max(min(o, c) - draw(st.integers(0, 8)), 1)
        rows.append((o * 5, hi * 5, lo * 5, c * 5))
        price = c
    entry = rows[0][0] + sign * 5 * draw(st.integers(0, 3))
    stop = entry - sign * 5 * draw(st.integers(20, 80))
    target = draw(st.none() | st.integers(20, 300).map(lambda t: entry + sign * 5 * t))
    mult = draw(st.sampled_from([0.0, 1.0, 1.0, 2.0]))
    se = draw(
        st.none()
        | st.none()
        | st.tuples(st.sampled_from(["SIGNAL_EXIT", "OPPOSITE_SIGNAL"]), st.integers(min(3, n), n))
    )
    return Case(
        direction,
        start,
        rows,
        {"entry": entry, "stop": stop, "target": target},
        {
            "trail_atr_mult": mult,
            "trail_activate_r": draw(st.sampled_from([0.5, 1.0])),
            "atr": draw(st.sampled_from([None, 0.25, 0.5, 1.0, 1.0])),
            "max_bars": draw(st.sampled_from([0, 0, 0, 0, 8, 14])),
            "tf_min": draw(st.sampled_from([1, 2])),
        },
        se,
    )


def garbled(rows, after: int, factor: float):
    """Replace every bar after index `after` by a wildly different (but valid) bar."""
    out = list(rows[: after + 1])
    for o, h, l, c in rows[after + 1 :]:
        scale = lambda v: max(int(v * factor) // 5 * 5, 5)  # noqa: E731 - stays on the tick grid
        out.append((scale(o), scale(h), scale(l), scale(c)))
    return out


class TestTruncation:
    @PROP
    @given(case=cases(), cut=st.integers(0, 29))
    def test_A_cutting_the_bars_after_t_changes_no_event_up_to_t(self, case, cut):
        m = min(cut, len(case.rows) - 1)
        t = case.start + m * MIN
        now = case.end()
        full = U.run(case.state(), case.bars(), case.rules(), now, case.strategy_exit())[1]
        cut_events = U.run(
            case.state(), case.bars().iloc[: m + 1], case.rules(), now, case.strategy_exit()
        )[1]
        assert U.canon(U.upto(full, t)) == U.canon(U.upto(cut_events, t))

    @PROP
    @given(case=cases(), cut=st.integers(0, 29))
    def test_B_a_run_that_only_knows_the_data_up_to_t_agrees_up_to_t(self, case, cut):
        m = min(cut, len(case.rows) - 1)
        t = case.start + m * MIN
        known_now = t + MIN  # the close of bar m
        full = U.run(case.state(), case.bars(), case.rules(), case.end(), case.strategy_exit())[1]
        early = U.run(
            case.state(),
            case.bars().iloc[: m + 1],
            case.rules(),
            known_now,
            case.strategy_exit(known_now),
        )[1]
        assert U.canon(U.upto(full, t)) == U.canon(U.upto(early, t))

    @PROP
    @given(case=cases(), cut=st.integers(0, 29), factor=st.sampled_from([0.5, 0.9, 1.1, 2.0]))
    def test_C_replacing_every_bar_after_t_changes_no_event_up_to_t(self, case, cut, factor):
        m = min(cut, len(case.rows) - 1)
        t = case.start + m * MIN
        now = case.end()
        full = U.run(case.state(), case.bars(), case.rules(), now, case.strategy_exit())[1]
        other = U.run(
            case.state(),
            case.bars(garbled(case.rows, m, factor)),
            case.rules(),
            now,
            case.strategy_exit(),
        )[1]
        assert U.canon(U.upto(full, t)) == U.canon(U.upto(other, t))


class TestCatchUp:
    @PROP
    @given(case=cases(), sizes=st.lists(st.integers(1, 8), min_size=1, max_size=6))
    def test_D_any_chunking_of_now_gives_the_same_events_and_final_state(self, case, sizes):
        once_state, once = U.run(
            case.state(), case.bars(), case.rules(), case.end(), case.strategy_exit()
        )
        state, events, done = case.state(), [], 0
        for size in (*sizes, len(case.rows)):
            done = min(done + size, len(case.rows))
            now = case.start + done * MIN
            state, new = U.run(state, case.bars(), case.rules(), now, case.strategy_exit(now))
            events += new
            if U.closes_of(new):
                break
        assert U.canon(events) == U.canon(once)
        assert state == once_state

    @PROP
    @given(case=cases())
    def test_E_at_most_one_close_and_it_is_one_of_the_rules_reasons(self, case):
        _, events = U.run(case.state(), case.bars(), case.rules(), case.end(), case.strategy_exit())
        closes = U.closes_of(events)
        assert len(closes) <= 1
        if closes:
            reason = U.close_of(events).reason
            assert reason in {
                "STOP",
                "TRAIL_STOP",
                "TARGET",
                "SIGNAL_EXIT",
                "OPPOSITE_SIGNAL",
                "SESSION_END",
                "TIME_STOP",
            }

    @PROP
    @given(case=cases())
    def test_F_a_close_is_a_price_the_bars_or_the_rules_could_have_given(self, case):
        """Everything but a trailed stop closes at the open or close of a bar, the stop, or the
        target: never an invented number (a trailed stop is a computed level)."""
        bars = case.bars()
        _, events = U.run(case.state(), bars, case.rules(), case.end(), case.strategy_exit())
        close = U.close_of(events)
        if close is None or close.reason == "TRAIL_STOP":
            return
        prices = {int(round(v * 100)) for col in ("open", "close") for v in bars[col]}
        prices.add(case.state_kw["stop"])
        if case.state_kw["target"] is not None:
            prices.add(case.state_kw["target"])
        assert close.price in prices
