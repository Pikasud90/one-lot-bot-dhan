"""The strategy's own trail in the paper book (2026-11-11): an OPEN paper trade whose alert
froze `manage.line_trail` follows the level the desk reports for its strategy's line.

The rules of the move itself live in `onelot.paper.manage` (test_manage_line_trail.py);
these tests pin what the BOOK and `paper.position` add: the rule read from the alert and
kept in the plan, the desk's plain `(level in rupees, bar close)` answer turned into paise,
who is asked and when, data time for a delayed feed, the moved stop surviving a restart,
and a callback that fails or answers nonsense never stopping the management.

Worked stock (conftest): RELIANCE long, alert entry 1000.00, stop 995.00, target 1010.00;
the 10:06 bar opens 1000.00 so the fill is 1000.30 (100030 paise).
"""

from __future__ import annotations

import datetime as dt
import inspect
import math
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import numpy as np
import pytest

from onelot.paper.fill import option_exit_paise
from onelot.paper.manage import TrailLevel
from onelot.paper.position import (
    rules_from_alert,
    rules_from_json,
    rules_to_json,
    trail_level_of,
)

from .conftest import THU, ist, make_alert
from .test_book_manage import ENTRY, FILL, QUIET, at, bars, feed, kinds, opened, the_row

DIP = (999.00, 999.50, 997.50, 998.00)  # trades down through 998.00, above the 995.00 stop
TO_THE_STOP = (999.00, 999.50, 994.00, 996.00)  # touches the alert's own stop
ROWS = [FILL, QUIET, QUIET, QUIET, QUIET, DIP]  # 10:06 .. 10:11


def line_alert(*args: Any, **kw: Any) -> dict[str, Any]:
    """A desk alert of a strategy whose line is the stop and the trail."""
    alert = make_alert(*args, **kw)
    alert["manage"]["line_trail"] = True
    return alert


def line_at(rupees: float, minute: int = 10):
    """A desk callback: the line allows `rupees` on the strategy bar that closed 10:`minute`."""
    return lambda trade, now: (rupees, at(minute))


def stop_of(engine) -> int:
    return the_row(engine)["plan"]["position"]["stop_paise"]


def rules(manage: dict[str, Any]):
    return rules_from_alert({"manage": manage}, tick_paise=5, resolution=1, timeframe="5m")


class TestTheRuleIsFrozenAtTheAlert:
    def test_it_is_on_only_when_the_alert_says_exactly_true(self) -> None:
        assert rules({"line_trail": True}).line_trail is True
        for other in (False, None, 1, "true", "yes", [True]):
            assert rules({"line_trail": other}).line_trail is False, other
        assert rules({}).line_trail is False  # every alert of a strategy without a line

    def test_the_plan_carries_it_only_when_it_is_on(self, make_book) -> None:
        with_line = make_book().on_alert(line_alert(), ist(THU, 10, 5, 4))
        assert with_line is not None and with_line.plan["rules"]["line_trail"] is True
        plain = make_book().on_alert(make_alert(alert_id="ffff00001111"), ist(THU, 10, 5, 4))
        assert plain is not None and "line_trail" not in plain.plan["rules"]

    @pytest.mark.parametrize("on", [True, False])
    def test_the_stored_rules_read_back_as_they_were(self, on: bool) -> None:
        frozen = rules({"atr": 4.2, "trail_atr_mult": 1.5, "max_bars": 6, "line_trail": on})
        stored = rules_to_json(frozen)
        assert ("line_trail" in stored) is on
        assert rules_from_json(stored, resolution=1) == frozen

    def test_a_plan_stored_before_the_rule_existed_has_it_off(self) -> None:
        stored = rules_to_json(rules({"atr": 4.2}))
        assert rules_from_json(stored, resolution=1).line_trail is False
        assert rules_from_json(stored | {"line_trail": "yes"}, resolution=1).line_trail is False


class TestTheDesksAnswer:
    def test_none_and_a_trail_level_pass_through(self) -> None:
        level = TrailLevel(99_800, at(10))
        assert trail_level_of(None) is None
        assert trail_level_of(level) is level

    @pytest.mark.parametrize(
        ("rupees", "paise"),
        [(998.0, 99_800), (998, 99_800), (np.float64(1003.45), 100_345), (45190.05, 4_519_005)],
    )
    def test_the_desks_tuple_is_rupees_and_becomes_paise_once(self, rupees, paise: int) -> None:
        assert trail_level_of((rupees, at(10))) == TrailLevel(paise, at(10))

    @pytest.mark.parametrize(
        "answer",
        [
            "998.00",  # not a tuple
            998.0,
            [998.0, at(10)],  # a list is not the desk's tuple
            (998.0,),
            (998.0, at(10), "long"),
            (math.nan, at(10)),
            (math.inf, at(10)),
            (None, at(10)),
            (0.0, at(10)),  # not a price
            (-998.0, at(10)),
            (998.0, dt.datetime(2026, 10, 29, 10, 10)),  # a naive time
            (998.0, "2026-10-29T10:10:00+05:30"),
        ],
    )
    def test_anything_else_is_refused(self, answer: object) -> None:
        with pytest.raises(ValueError):
            trail_level_of(answer)


class TestTheBookFollowsTheLine:
    def test_the_stop_moves_to_the_level_and_the_trade_ends_on_it(self, make_book, engine):
        book = opened(make_book, line_alert())
        book.follow_trail(line_at(998.00))
        events = book.step(at(12), feed(RELIANCE=bars(ROWS)))

        assert [e.kind for e in events] == ["FILLED", "STOP_MOVED", "CLOSED"]
        moved = events[1]
        assert moved.ts == at(10)  # the first one-minute open after the level's bar closed
        assert (moved.detail["from_paise"], moved.detail["to_paise"]) == (99_500, 99_800)
        assert moved.detail["source"] == "strategy_line"
        row = the_row(engine)
        # 998.00 x (1 - 0.03 %) = 997.7006 -> a long sells, so the tick floors it: 997.70
        assert (row["status"], row["close_reason"], row["exit_paise"]) == (
            "CLOSED",
            "TRAIL_STOP",
            99_770,
        )
        assert (row["exit_ts"], row["gross_paise"]) == (at(11), 99_770 - ENTRY)
        assert row["plan"]["position"]["trail_active"] is True and stop_of(engine) == 99_800
        assert kinds(engine) == ["OPENED", "FILLED", "STOP_MOVED", "CLOSED"]

    def test_a_trade_whose_alert_froze_no_line_trail_is_never_asked_about(self, make_book, engine):
        asked: list[str] = []

        def level(trade: Any, now: dt.datetime) -> tuple[float, dt.datetime]:
            asked.append(trade.alert_id)
            return 998.00, at(10)

        book = opened(make_book)  # an alert of a strategy without a line
        book.follow_trail(level)
        book.step(at(12), feed(RELIANCE=bars(ROWS)))
        assert asked == []
        assert the_row(engine)["status"] == "OPEN" and stop_of(engine) == 99_500

    def test_the_callback_is_asked_about_the_open_trade_at_every_step(self, make_book, engine):
        asked: list[tuple[str, str, dt.datetime]] = []

        def level(trade: Any, now: dt.datetime) -> None:
            asked.append((trade.alert_id, trade.status, now))
            return None  # the line has no level on this bar

        book = opened(make_book, line_alert())
        book.follow_trail(level)
        frame = bars(ROWS[:4])
        assert book.step(ist(THU, 10, 5, 30), feed(RELIANCE=frame)) == []  # still waiting
        book.step(at(8), feed(RELIANCE=frame))
        book.step(at(9), feed(RELIANCE=frame))
        assert asked == [("a1b2c3d4e5f6", "OPEN", at(8)), ("a1b2c3d4e5f6", "OPEN", at(9))]
        assert stop_of(engine) == 99_500 and book.status()["hook_errors"] == 0

    def test_a_trail_level_object_is_taken_as_it_is(self, make_book, engine):
        book = opened(make_book, line_alert())
        book.follow_trail(lambda trade, now: TrailLevel(99_803, at(10)))
        book.step(at(11), feed(RELIANCE=bars(ROWS[:5])))
        assert stop_of(engine) == 99_800  # snapped to the 5-paise tick, against the trader

    def test_a_short_follows_its_line_down(self, make_book, engine):
        book = opened(make_book, line_alert(direction="short"))  # stop 1005.00, fill 999.70
        book.follow_trail(line_at(1002.00))
        rows = [FILL, QUIET, QUIET, QUIET, QUIET, (1001.00, 1002.50, 1000.50, 1002.00)]
        book.step(at(12), feed(RELIANCE=bars(rows)))
        row = the_row(engine)
        # 1002.00 x 1.0003 = 1002.3006 -> a short buys back, so the tick ceils it: 1002.35
        assert (row["close_reason"], row["exit_paise"]) == ("TRAIL_STOP", 100_235)

    def test_an_option_trade_follows_the_line_on_its_underlying(self, make_book, engine):
        calls: list[tuple[float, dt.datetime]] = []

        def premium_at(option: Mapping[str, Any], underlying: float, ts: dt.datetime) -> float:
            calls.append((underlying, ts))
            return 312.34 + 0.65 * (underlying - 45210.35)

        book = opened(make_book, line_alert("INDEX"), premium_at=premium_at)  # stop 45110.35
        book.follow_trail(lambda trade, now: (45190.00, at(10)))
        calm = (45210.35, 45220.00, 45200.00, 45215.00)
        rows = [calm, calm, calm, calm, (45215.00, 45220.00, 45170.00, 45180.00)]
        book.step(at(11), feed(BANKNIFTY=bars(rows)))

        row = the_row(engine)
        assert (row["close_reason"], row["exit_ts"]) == ("TRAIL_STOP", at(10))
        assert calls[-1] == (45190.00, at(10))  # the option is priced at the line's level
        fair = 312.34 + 0.65 * (45190.00 - 45210.35)
        assert row["exit_paise"] == option_exit_paise(fair, 0.006, 2, 0.05)
        assert row["gross_paise"] == (row["exit_paise"] - row["entry_paise"]) * 30

    def test_a_moved_stop_survives_a_restart(self, make_book, engine):
        first = opened(make_book, line_alert())
        first.follow_trail(line_at(998.00))
        first.step(at(11), feed(RELIANCE=bars(ROWS[:5])))
        assert the_row(engine)["status"] == "OPEN" and stop_of(engine) == 99_800
        reborn = make_book()  # no desk has registered its callback yet
        reborn.step(at(12), feed(RELIANCE=bars(ROWS)))
        row = the_row(engine)
        assert (row["close_reason"], row["exit_ts"], row["exit_paise"]) == (
            "TRAIL_STOP",
            at(11),
            99_770,
        )

    def test_a_level_at_every_poll_is_booked_once(self, make_book, engine):
        book = opened(make_book, line_alert())
        book.follow_trail(line_at(998.00, 8))
        frame = bars([FILL] + [QUIET] * 6)
        for minute in range(8, 14):
            book.step(at(minute), feed(RELIANCE=frame))
        assert kinds(engine) == ["OPENED", "FILLED", "STOP_MOVED"]
        before = the_row(engine)
        assert book.step(at(13), feed(RELIANCE=frame)) == []
        assert the_row(engine) == before  # nothing new: no write at all


class TestWhoIsAskedForTheLevel:
    def test_a_callback_registered_later_takes_the_earlier_ones_place(self, make_book, engine):
        book = opened(make_book, line_alert())
        book.follow_trail(line_at(998.00))
        book.follow_trail(line_at(997.00))
        book.step(at(11), feed(RELIANCE=bars(ROWS[:5])))
        assert stop_of(engine) == 99_700

    def test_the_step_keeps_its_contract_signature(self, make_book):
        """The level comes through `follow_trail`; `step` is the US-001 public contract."""
        book = opened(make_book, line_alert())
        assert list(inspect.signature(book.step).parameters) == ["now", "bars", "strategy_exit"]

    def test_following_can_be_switched_off(self, make_book, engine):
        book = opened(make_book, line_alert())
        book.follow_trail(line_at(998.00))
        book.follow_trail(None)
        book.step(at(12), feed(RELIANCE=bars(ROWS)))
        assert the_row(engine)["status"] == "OPEN" and stop_of(engine) == 99_500

    def test_without_a_callback_the_trade_runs_on_the_stop_it_has(self, make_book, engine):
        book = opened(make_book, line_alert())
        book.step(at(8), feed(RELIANCE=bars([FILL, TO_THE_STOP])))
        assert the_row(engine)["close_reason"] == "STOP"


class TestDataTime:
    def test_a_level_ahead_of_the_data_is_not_due_and_not_an_error(self, make_book, engine):
        """The desk's clock is ahead of a delayed trade's data time: the level waits. It is
        not remembered either: the desk reports its latest level again at every poll."""
        book = opened(make_book, line_alert(source="yahoo"), feed_delay_minutes=15)
        book.follow_trail(line_at(998.00))
        frame = bars(ROWS[:5])
        book.step(at(8), feed(RELIANCE=frame))  # fills; data time 09:53
        book.step(at(12), feed(RELIANCE=frame))  # data time 09:57: the 10:10 close is ahead
        assert the_row(engine)["status"] == "OPEN" and stop_of(engine) == 99_500
        assert book.status()["hook_errors"] == 0
        book.step(at(26), feed(RELIANCE=frame))  # data time 10:11: the 10:10 bar has closed
        assert stop_of(engine) == 99_800 and kinds(engine)[-1] == "STOP_MOVED"

    def test_a_live_trade_never_sees_a_level_from_a_bar_that_has_not_closed(
        self, make_book, engine
    ):
        book = opened(make_book, line_alert())
        book.follow_trail(line_at(998.00, 30))  # a bar that closes 10:30
        book.step(at(12), feed(RELIANCE=bars(ROWS)))
        assert the_row(engine)["status"] == "OPEN" and stop_of(engine) == 99_500
        assert book.status()["hook_errors"] == 0


class TestACallbackThatFails:
    @pytest.mark.parametrize(
        "answer",
        [
            "998.00",  # not a level
            (998.0,),
            (math.nan, at(10)),
            (-998.0, at(10)),
            (998.0, dt.datetime(2026, 10, 29, 10, 10)),  # naive
        ],
    )
    def test_a_malformed_answer_is_ignored_and_counted(self, make_book, engine, answer):
        book = opened(make_book, line_alert())
        book.follow_trail(lambda trade, now: answer)
        book.step(at(8), feed(RELIANCE=bars([FILL, TO_THE_STOP])))
        assert the_row(engine)["close_reason"] == "STOP"  # managed anyway, on its own stop
        assert book.status()["hook_errors"] == 1

    def test_it_is_counted_every_time_but_logged_once_per_trade(self, make_book, engine, caplog):
        def broken(trade: Any, now: dt.datetime) -> None:
            raise RuntimeError("the tracker is being rebuilt")

        book = opened(make_book, line_alert())
        book.follow_trail(broken)
        frame = bars([FILL, QUIET, QUIET, QUIET])
        with caplog.at_level("DEBUG", logger="onelot.paper"):
            for minute in (8, 9, 10):
                book.step(at(minute), feed(RELIANCE=frame))
        failed = [r for r in caplog.records if "trail_level failed" in r.getMessage()]
        assert [r.levelname for r in failed] == ["ERROR", "DEBUG", "DEBUG"]
        assert book.status()["hook_errors"] == 3 and the_row(engine)["status"] == "OPEN"

    def test_a_failing_trail_does_not_cost_the_strategy_exit(self, make_book, engine):
        def broken(trade: Any, now: dt.datetime) -> None:
            raise RuntimeError("no level")

        rows = [FILL, QUIET, QUIET, QUIET, (1002.00, 1003.00, 1001.00, 1002.50)]
        book = opened(make_book, line_alert())
        book.follow_trail(broken)
        book.step(at(11), feed(RELIANCE=bars(rows)), lambda t, n: ("SIGNAL_EXIT", at(10)))
        assert the_row(engine)["close_reason"] == "SIGNAL_EXIT"


def test_the_helper_frames_are_the_ones_described() -> None:
    """Guards the worked example: every bar before the dip stays above 998.00."""
    assert all(low > 998.00 for _, _, low, _ in ROWS[:5]) and DIP[2] < 998.00 < DIP[0]
    assert replace(TrailLevel(99_800, at(10)), level_paise=99_805).level_paise == 99_805
