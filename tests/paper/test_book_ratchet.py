"""US-079: the paper book manages a trade whose alert froze the stop, target and trail rule
(`manage.ratchet`) by `manage/ratchet.py`, bar by bar, through the REAL `PaperBook`.

Worked option (conftest's INDEX alert, BUY_CE on BANKNIFTY): the alert froze a first risk
of 100.00 index points, target 3 risks, costs 2.00 per index point. The 10:06 bar opens
45210.00, so the plan is anchored there: first stop 45110.00, first target 45510.00, a
planned move of 300.00.

    10:06  45210 45230 45200   nothing yet
    10:07  45220 45290 45215   best 26.7%: the stop goes above breakeven (2 + 15 = 45227)
    10:08  45280 45365 45275   best 51.7%: 35% locked (45315)
    10:09  45360 45460 45355   best 83.3%: trailing (45400); the target moves out to 45660
    10:10  45450 45470 45430   the trail follows the best price (45410)
    10:11  45440 45445 45405   touches 45410: TRAIL_STOP at 45410
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

import pytest

from onelot.manage import ratchet
from onelot.paper.money import to_paise
from onelot.paper.position import rules_from_alert, rules_from_json, rules_to_json
from onelot.store.schema import paper_events

from .conftest import THU, ist, make_alert, table_rows
from .test_book_manage import at, bars, feed, opened, the_row

Row = tuple[float, float, float, float]
RISK, TARGET_R, COST = 100.0, 3.0, 2.0
WALK: list[Row] = [
    (45210.0, 45230.0, 45200.0, 45220.0),
    (45220.0, 45290.0, 45215.0, 45280.0),
    (45280.0, 45365.0, 45275.0, 45360.0),
    (45360.0, 45460.0, 45355.0, 45450.0),
    (45450.0, 45470.0, 45430.0, 45440.0),
    (45440.0, 45445.0, 45405.0, 45410.0),
]
QUIET: Row = (45215.0, 45225.0, 45205.0, 45220.0)


def ratchet_alert(direction: str = "long", **kw: Any) -> dict[str, Any]:
    """The conftest INDEX alert with the rule frozen in `manage`, as Desk._raise_signal
    writes it for a slot with `ratchet: true`."""
    alert = make_alert("INDEX", direction, **kw)
    alert["manage"] |= {
        "target_r": TARGET_R,
        "trail_atr_mult": 0.0,
        "ratchet": {"stop_atr": 1.5, "atr_tf": "60m", "target_r": TARGET_R, "extend": True,
                    "risk": RISK, "cost": COST},
    }  # fmt: skip
    return alert


def replay(rows: Sequence[Row], side: int = 1, start: dt.datetime | None = None):
    """A direct loop over `ratchet.py`, independent of the book: (moves, exit). Moves are
    (when, kind, level); the exit is (when, reason, price) or None."""
    start = start or at(6)
    plan = ratchet.plan_from_risk(side, rows[0][0], RISK, TARGET_R, COST)
    state = ratchet.start(plan)
    moves: list[tuple[dt.datetime, str, float]] = []
    for k, (o, h, low, _c) in enumerate(rows):
        when = start + dt.timedelta(minutes=k)
        hit = ratchet.exit_on_bar(plan, state, o, h, low)
        if hit is not None:
            return moves, (when, hit[0], hit[1])
        state, changes = ratchet.advance(plan, state, h, low)
        if "STOP_MOVED" in changes:
            moves.append((when + dt.timedelta(minutes=1), "STOP_MOVED", state.stop))
        if "TARGET_MOVED" in changes:
            moves.append((when + dt.timedelta(minutes=1), "TARGET_MOVED", state.target))
    return moves, None


def book_moves(events) -> list[tuple[dt.datetime, str, float]]:
    level = {"STOP_MOVED": "stop", "TARGET_MOVED": "target"}
    return [(e.ts, e.kind, e.detail[level[e.kind]]) for e in events if e.kind in level]


def closed_of(events):
    (closed,) = [e for e in events if e.kind == "CLOSED"]
    return closed


class TestTheRuleIsFrozenAtTheAlert:
    def test_the_rules_carry_it_only_when_the_alert_does(self) -> None:
        on = rules_from_alert(ratchet_alert(), tick_paise=5, resolution=1, timeframe="3m")
        off = rules_from_alert(make_alert("INDEX"), tick_paise=5, resolution=1, timeframe="3m")
        assert on.ratchet == ratchet.RatchetParams(extend=True) and off.ratchet is None
        assert rules_from_json(rules_to_json(on), resolution=1) == on
        assert "ratchet" not in rules_to_json(off)  # a plan without it reads as before

    def test_the_fill_anchors_the_plan(self, make_book, engine) -> None:
        book = opened(make_book, ratchet_alert())
        book.step(at(7), feed(BANKNIFTY=bars(WALK[:1])))
        position = the_row(engine)["plan"]["position"]
        assert position["ratchet"]["plan"] == {
            "side": 1, "entry": 45210.0, "stop": 45110.0, "target": 45510.0, "cost": COST,
        }  # fmt: skip
        assert (position["initial_stop_paise"], position["target_paise"]) == (4_511_000, 4_551_000)

    def test_an_alert_without_it_keeps_the_alerts_levels(self, make_book, engine) -> None:
        book = opened(make_book, make_alert("INDEX"))
        book.step(at(7), feed(BANKNIFTY=bars(WALK[:1])))
        position = the_row(engine)["plan"]["position"]
        assert "ratchet" not in position
        assert position["initial_stop_paise"] == to_paise(45210.35 - 100.0)


class TestPaperManagementEqualsTheRule:
    def test_a_day_through_every_stage(self, make_book, engine) -> None:
        book = opened(make_book, ratchet_alert())
        events = book.step(at(12), feed(BANKNIFTY=bars(WALK)))

        moves, exit_ = replay(WALK)
        assert exit_ is not None
        assert book_moves(events) == moves
        stages = [e.detail["stage"] for e in events if e.kind == "STOP_MOVED"]
        assert stages == ["BREAKEVEN", "LOCKED", "TRAILING", "TRAILING"]
        closed = closed_of(events)
        assert (closed.ts, closed.detail["reason"], closed.detail["price_paise"]) == (
            exit_[0],
            exit_[1],
            to_paise(exit_[2]),
        )
        row = the_row(engine)
        assert (row["status"], row["close_reason"]) == ("CLOSED", "TRAIL_STOP")
        assert closed.detail["target_moved"] is True and closed.detail["stage"] == "TRAILING"
        assert [e["kind"] for e in table_rows(engine, paper_events)].count("TARGET_MOVED") == 1

    @pytest.mark.parametrize(
        ("rows", "reason"),
        [
            # 26.7% then back through the breakeven stop
            ([WALK[0], WALK[1], (45250.0, 45260.0, 45220.0, 45230.0)], "BREAKEVEN_STOP"),
            # 51.7% then back through the 35% lock
            ([*WALK[:3], (45330.0, 45335.0, 45300.0, 45310.0)], "LOCK_STOP"),
            # straight down through the first stop
            ([WALK[0], (45200.0, 45205.0, 45100.0, 45120.0)], "STOP"),
        ],
    )
    def test_each_stop_has_its_stages_name(self, make_book, engine, rows, reason) -> None:
        book = opened(make_book, ratchet_alert())
        events = book.step(at(12), feed(BANKNIFTY=bars(rows)))
        _, exit_ = replay(rows)
        assert exit_ is not None and exit_[1] == reason
        closed = closed_of(events)
        assert (closed.detail["reason"], closed.detail["price_paise"]) == (
            reason,
            to_paise(exit_[2]),
        )
        assert the_row(engine)["close_reason"] == reason

    def test_a_bar_touching_the_stop_and_the_target_is_a_stop(self, make_book, engine) -> None:
        rows = [WALK[0], (45220.0, 45520.0, 45100.0, 45300.0)]
        book = opened(make_book, ratchet_alert())
        events = book.step(at(12), feed(BANKNIFTY=bars(rows)))
        closed = closed_of(events)
        assert (closed.detail["reason"], closed.detail["price_paise"]) == ("STOP", 4_511_000)

    def test_a_gap_fills_at_the_open(self, make_book, engine) -> None:
        rows = [*WALK[:3], (45290.0, 45295.0, 45280.0, 45285.0)]  # opens below the 45315 lock
        book = opened(make_book, ratchet_alert())
        events = book.step(at(12), feed(BANKNIFTY=bars(rows)))
        _, exit_ = replay(rows)
        assert exit_ is not None and exit_[1] == "GAP_LOCK_STOP"
        closed = closed_of(events)
        assert (closed.detail["reason"], closed.detail["gap"]) == ("LOCK_STOP", True)
        assert closed.detail["price_paise"] == to_paise(45290.0)

    def test_an_untouched_target_is_a_target(self, make_book, engine) -> None:
        rows = [WALK[0], (45300.0, 45515.0, 45295.0, 45500.0)]  # 101.7% in one bar
        book = opened(make_book, ratchet_alert())
        events = book.step(at(12), feed(BANKNIFTY=bars(rows)))
        closed = closed_of(events)
        assert (closed.detail["reason"], closed.detail["price_paise"]) == ("TARGET", 4_551_000)
        assert closed.detail["target_moved"] is False

    def test_a_short_mirrors_it(self, make_book, engine) -> None:
        mirror = [(2 * 45210.0 - o, 2 * 45210.0 - low, 2 * 45210.0 - h, 2 * 45210.0 - c)
                  for o, h, low, c in WALK]  # fmt: skip
        book = opened(make_book, ratchet_alert("short"))
        events = book.step(at(12), feed(BANKNIFTY=bars(mirror)))
        moves, exit_ = replay(mirror, side=-1)
        assert exit_ is not None and book_moves(events) == moves
        assert closed_of(events).detail["reason"] == exit_[1] == "TRAIL_STOP"


class TestPollsAndRestarts:
    def test_bars_arriving_together_equal_bars_one_by_one(self, make_book, engine) -> None:
        book = opened(make_book, ratchet_alert())
        one_by_one = []
        for k in range(len(WALK)):
            one_by_one += book.step(at(7 + k), feed(BANKNIFTY=bars(WALK[: k + 1])))
        moves, exit_ = replay(WALK)
        assert book_moves(one_by_one) == moves  # and the one-step run equals the same replay
        assert exit_ is not None and closed_of(one_by_one).detail["reason"] == exit_[1]

    def test_a_new_book_continues_from_the_stored_state(self, make_book, engine) -> None:
        first = opened(make_book, ratchet_alert())
        early = first.step(at(9), feed(BANKNIFTY=bars(WALK[:3])))
        stored = the_row(engine)["plan"]["position"]["ratchet"]["state"]
        assert (stored["stage"], stored["best"], stored["stop"]) == (2, 45365.0, 45315.0)

        later = make_book().step(at(12), feed(BANKNIFTY=bars(WALK)))  # a restart
        moves, exit_ = replay(WALK)
        assert book_moves([*early, *later]) == moves
        assert exit_ is not None
        assert closed_of(later).detail["price_paise"] == to_paise(exit_[2])
        assert the_row(engine)["close_reason"] == "TRAIL_STOP"


class TestTheOtherExitsAreUnchanged:
    @pytest.mark.parametrize("with_rule", [True, False])
    def test_the_strategy_exit_closes_at_the_next_open(self, make_book, engine, with_rule):
        alert = ratchet_alert() if with_rule else make_alert("INDEX")
        book = opened(make_book, alert)
        rows = [QUIET] * 6
        events = book.step(
            at(12), feed(BANKNIFTY=bars(rows)), lambda trade, now: ("SIGNAL_EXIT", at(9))
        )
        closed = closed_of(events)
        assert (closed.ts, closed.detail["reason"], closed.detail["price_paise"]) == (
            at(9),
            "SIGNAL_EXIT",
            to_paise(QUIET[0]),
        )

    @pytest.mark.parametrize("with_rule", [True, False])
    def test_the_session_end_closes_at_the_exit_time(self, make_book, engine, with_rule):
        created = ist(THU, 15, 9, 3)
        alert = (ratchet_alert if with_rule else lambda **k: make_alert("INDEX", **k))(
            created=created
        )
        book = make_book(clock=lambda: ist(THU, 15, 20))
        book.on_alert(alert, ist(THU, 15, 9, 4))
        rows = [QUIET] * 8
        events = book.step(ist(THU, 15, 20), feed(BANKNIFTY=bars(rows, start=ist(THU, 15, 10))))
        closed = closed_of(events)
        assert (closed.ts, closed.detail["reason"]) == (ist(THU, 15, 15), "SESSION_END")
        assert closed.detail["price_paise"] == to_paise(QUIET[0])
