"""No fill at or after the strategy's exit_time (US-002 review round 1, MINOR-2).

The Lab never fills a signal on a bar at or after its square-off minute; the paper fill
rule mirrors it: the first eligible bar opening at or after exit_time on the alert's date
is NO_FILL with reason "session_end", stamped with that bar's open.
"""

from __future__ import annotations

import datetime as dt

import pytest

from onelot.paper.fill import decide_fill, normalise_bars

from .conftest import THU, ist, make_bars


def decide(created: dt.datetime, first_open: dt.datetime, now: dt.datetime, **kw):
    bars = normalise_bars(make_bars(first_open, [1000.0] * 6))
    assert bars is not None
    return decide_fill(
        bars,
        created_at=created,
        deadline=created + dt.timedelta(minutes=10),
        resolution_min=1,
        now=now,
        direction="long",
        stop_paise=99_500,
        **kw,
    )


class TestTheExitTimeBoundsTheFill:
    @pytest.mark.parametrize(
        ("created", "bar_ts"),
        [
            (ist(THU, 15, 14, 30), ist(THU, 15, 15)),  # the first bar opens AT exit_time
            (ist(THU, 15, 16, 30), ist(THU, 15, 17)),  # an alert after exit_time
        ],
    )
    def test_a_first_bar_at_or_after_exit_time_is_no_fill(self, created, bar_ts) -> None:
        decision = decide(created, ist(THU, 15, 14), ist(THU, 15, 21), exit_time=dt.time(15, 15))
        assert (decision.outcome, decision.reason, decision.bar_ts) == (
            "no_fill",
            "session_end",
            bar_ts,
        )

    def test_the_bar_before_exit_time_fills(self) -> None:
        decision = decide(
            ist(THU, 15, 13, 30), ist(THU, 15, 13), ist(THU, 15, 21), exit_time=dt.time(15, 15)
        )
        assert (decision.outcome, decision.bar_ts) == ("fill", ist(THU, 15, 14))

    def test_it_waits_while_the_exit_bar_is_still_forming(self) -> None:
        decision = decide(
            ist(THU, 15, 14, 30), ist(THU, 15, 14), ist(THU, 15, 15, 30), exit_time=dt.time(15, 15)
        )
        assert decision.outcome == "wait"

    def test_without_an_exit_time_the_old_window_applies(self) -> None:
        decision = decide(ist(THU, 15, 14, 30), ist(THU, 15, 14), ist(THU, 15, 21))
        assert (decision.outcome, decision.bar_ts) == ("fill", ist(THU, 15, 15))
