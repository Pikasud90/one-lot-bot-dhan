"""US-002 cycle 2, through the book: the review-round-1 decisions and the two Tester fixes.

* MAJOR-1  the book trails at STRATEGY-bar closes (tf_min 5 and 10 over 1- and 2-minute bars)
           with the ATR in `manage.atr_trail`, frozen in the alert; `manage.atr` only for an alert
           that has no such key; None, 0 and NaN disable the trail (never another ATR);
* MAJOR-2  a stock whose SLIPPED entry is at or beyond the target is NO_FILL
           GAP_THROUGH_TARGET, so a TARGET close can never book a loss (property);
* MINOR-1  a trade that never saw a usable bar closes SESSION_END at the session close with NULL
           money, a CLOSED event (fallback 'no_usable_bar') and a PRICING_UNAVAILABLE event;
* MINOR-2  a fill bar at or after exit_time is NO_FILL 'session_end';
* MINOR-3  stock targets are snapped ONCE, against the trader (long floors, short ceils);
           an option's underlying target is not;
* NIT      the rules are frozen into `plan["rules"]` when the trade opens; a failing strategy
           callback logs its traceback once per trade and is counted every time;
* BUG-1    an exit reported ahead of data time is remembered, across restarts (live and delayed);
* BUG-2    an OPEN trade's update is guarded by its cursor: two books never duplicate an event.

Worked stock: long SBIN 1000.00, stop 990.00, target 1020.00; fill open 1000.00 -> entry 100030,
one R = 1030 paise, a 1.5 x 4.00 trail = 600 paise.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import math
from collections import Counter

import pytest
import us001_helpers as H
import us002_helpers as U
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from test_us002_ac7_truncation import cases
from test_us002_b_properties import alert_for, callback, frame_of, row_keys
from test_us002_c_strategy_bar_trail import day_grid, last_fine_bars

MIN = dt.timedelta(minutes=1)
Q = (100000, 100100, 99900, 100000)
PROP = settings(
    max_examples=80,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=list(HealthCheck),
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


def step(book, frame, now, callback=None):
    return book.step(now, H.Feed(SBIN=frame), callback)


def opened(db, alert=None, **book_kw):
    book = H.make_book(db.engine, **book_kw)
    H.open_trade(book, alert or U.make_alert("A1"))
    return book


def read_plan(engine, alert_id="A1"):
    with engine.connect() as conn:
        raw = conn.execute(
            text("SELECT plan FROM paper_trades WHERE alert_id = :a"), {"a": alert_id}
        ).scalar_one()
    return json.loads(raw) if isinstance(raw, str) else raw


def write_plan(engine, plan, alert_id="A1"):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE paper_trades SET plan = :p WHERE alert_id = :a"),
            {"p": json.dumps(plan), "a": alert_id},
        )


# ── MAJOR-1: the cadence through the book ─────────────────────────────────────────────
def session_rows(res, overrides, price=100000):
    """The whole day on the 09:15 + k x res grid to 15:28, quiet except `overrides` {ts: row}."""
    quiet = (price, price + 100, price - 100, price)
    stamps = day_grid(res, last=(15, 28))
    return U.pbars_at(stamps, [overrides.get(t, quiet) for t in stamps])


class TestTheBookTrailsOnStrategyBars:
    @pytest.mark.parametrize("res", [1, 2])
    @pytest.mark.parametrize("tf", [5, 10])
    def test_only_the_strategy_bar_close_arms_it_and_the_next_bar_stops_it(self, db, res, tf):
        """X and Y (two non-boundary bars) close in profit and Y dips to the would-be trailed
        stop: the old per-fine-bar trail stops there. B1 closes the strategy bar and arms the
        trail from its own close; B2 dips to it: TRAIL_STOP at the stop, stamped with B2's open."""
        manage = {"trail_atr_mult": 1.5, "atr": 4.0, "tf_min": tf}
        alert = U.make_alert("A1", resolution=f"{res}m", manage=manage)
        grid = day_grid(res, last=(15, 28))
        boundary = last_fine_bars(res, tf)
        fill = next(i for i, t in enumerate(grid) if t > H.CREATED)
        kb = next(
            i
            for i in range(fill + 3, len(grid))
            if grid[i] in boundary and grid[i - 1] not in boundary and grid[i - 2] not in boundary
        )
        x, y, b1, b2 = grid[kb - 2], grid[kb - 1], grid[kb], grid[kb + 1]
        frame = session_rows(
            res,
            {
                x: (100000, 101200, 100100, 101100),
                y: (101100, 101200, 100400, 101100),
                b1: (101100, 101200, 100600, 101100),
                b2: (101100, 101200, 100500, 100800),
            },
        )
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        events = step(book, frame, U.end_of(frame))
        row = U.row_of(db.engine)
        assert row["close_reason"] == "TRAIL_STOP", (res, tf, row["close_reason"])
        assert H.same_instant(row["exit_ts"], b2), "not at the early dip (bar Y)"
        assert row["exit_paise"] == U.expected_stock_exit(100500, "long", "TRAIL_STOP")
        moved = [e for e in events if str(e.kind) == "STOP_MOVED"]
        assert [e.ts for e in moved] == [b1 + dt.timedelta(minutes=res)]
        assert book.status()["hook_errors"] == 0

    @pytest.mark.parametrize("res, tf", [(1, 5), (1, 10), (2, 5), (2, 10)])
    def test_without_a_strategy_bar_close_the_profit_never_arms_the_trail(self, db, res, tf):
        """Profitable closes on every non-boundary bar and a dip to the would-be stop; the
        boundary bars close weakly: the trade is never trailed, the dip is no stop."""
        boundary = last_fine_bars(res, tf)
        grid = day_grid(res, last=(15, 28))
        fill = next(i for i, t in enumerate(grid) if t > H.CREATED)
        window = grid[fill + 1 : fill + 25]
        rows = {t: (101100, 101200, 100400, 101100) for t in window if t not in boundary}
        rows |= {t: (100000, 100100, 99900, 100000) for t in window if t in boundary}
        frame = session_rows(res, rows)
        book = opened(
            db,
            U.make_alert(
                "A1", resolution=f"{res}m", manage={"trail_atr_mult": 1.5, "atr": 4.0, "tf_min": tf}
            ),
        )
        step(book, frame, H.at(10, 40))
        assert U.row_of(db.engine)["status"] == "OPEN"
        assert book.status()["hook_errors"] == 0


# ── MAJOR-1: atr_trail versus atr ─────────────────────────────────────────────────────
BAR1 = (100000, 101200, 100100, 101100)  # closes +1.04 R


class TestTheTrailAtr:
    def run(self, db, manage, bar2_low, alert_id="A1"):
        alert = U.make_alert(alert_id, manage={"trail_atr_mult": 1.5, "tf_min": 1, **manage})
        book = opened(db, alert)
        frame = U.story([BAR1, (101100, 101200, bar2_low, 101000)])
        step(book, frame, U.end_of(frame))
        return U.row_of(db.engine, alert_id), book

    def test_the_alerts_atr_trail_is_what_the_trail_uses_not_atr(self, db):
        row, _ = self.run(db, {"atr": 4.0, "atr_trail": 2.0}, bar2_low=100800)
        assert row["close_reason"] == "TRAIL_STOP"  # 101100 - 1.5 x 2.00 = 100800
        assert row["exit_paise"] == U.expected_stock_exit(100800, "long", "TRAIL_STOP")

    def test_a_bar_that_stops_the_atr_trail_but_would_not_have_stopped_the_atr_it_replaces(
        self, db
    ):
        row, _ = self.run(db, {"atr": 4.0, "atr_trail": 2.0}, bar2_low=100700)
        assert row["close_reason"] == "TRAIL_STOP"  # atr alone would trail to 100500: no touch
        assert row["exit_paise"] == U.expected_stock_exit(100800, "long", "TRAIL_STOP")

    def test_an_alert_without_the_key_falls_back_to_atr(self, db):
        row, _ = self.run(db, {"atr": 4.0}, bar2_low=100500)
        assert row["close_reason"] == "TRAIL_STOP"  # 101100 - 1.5 x 4.00 = 100500
        assert row["exit_paise"] == U.expected_stock_exit(100500, "long", "TRAIL_STOP")

    def test_with_the_key_present_a_bigger_trail_atr_trails_wider(self, db):
        row, _ = self.run(db, {"atr": 2.0, "atr_trail": 4.0}, bar2_low=100600)
        assert row["status"] == "OPEN", "the stop is 100500: a 100600 low does not touch it"
        row, _ = self.run(db, {"atr": 2.0, "atr_trail": 4.0}, bar2_low=100500, alert_id="W2")
        assert row["close_reason"] == "TRAIL_STOP"

    @pytest.mark.parametrize("bad", [None, 0.0, float("nan")], ids=["none", "zero", "nan"])
    def test_an_unusable_atr_trail_disables_the_trail_even_when_atr_is_usable(self, db, bad):
        """The key is the strategy's own trail ATR: never another ATR to stand in for it."""
        row, book = self.run(db, {"atr": 4.0, "atr_trail": bad}, bar2_low=98900)
        assert row["close_reason"] == "STOP" and row["exit_paise"] == 98970
        assert book.status()["hook_errors"] == 0

    def test_a_usable_atr_trail_trails_even_when_atr_is_missing(self, db):
        row, _ = self.run(db, {"atr": None, "atr_trail": 2.0}, bar2_low=100800)
        assert row["close_reason"] == "TRAIL_STOP"

    def test_the_desk_freezes_atr_trail_in_every_alert_it_raises(self, tmp_path):
        from onelot.config import AppConfig

        d = H.new_db(tmp_path, "desk.db", make_global=True)
        try:
            desk, _ = H.new_desk(AppConfig.load(), d)
            desk.poll()
            alert = H.raise_alert(desk, "SBIN", now=H.mid_session(desk, "SBIN"))
            trail = alert["manage"].get("atr_trail")
            assert isinstance(trail, float) and math.isfinite(trail) and trail > 0
            assert desk.trackers["SBIN"].slots[0].last_features["atr_trail"] == trail
        finally:
            H.dispose_db(d, was_global=True)

    def test_the_frozen_atr_does_not_follow_a_later_change_to_the_alerts_dict(self, db):
        """The rules were read when the trade opened: editing the stored alert BEFORE the arming
        bar (or the desk's evolving ATR) changes nothing."""
        alert = U.make_alert(
            "A1", manage={"trail_atr_mult": 1.5, "atr": 4.0, "atr_trail": 2.0, "tf_min": 1}
        )
        book = opened(db, alert)
        step(book, U.story([Q]), H.at(10, 7))
        plan = read_plan(db.engine)
        plan["alert"]["manage"]["atr_trail"] = 20.0
        plan["alert"]["manage"]["trail_atr_mult"] = 0.0
        write_plan(db.engine, plan)
        reborn = H.make_book(db.engine)
        frame = U.story([Q, BAR1, (101100, 101200, 100800, 101000)])
        step(reborn, frame, U.end_of(frame))
        row = U.row_of(db.engine)
        assert row["close_reason"] == "TRAIL_STOP"
        assert row["exit_paise"] == U.expected_stock_exit(100800, "long", "TRAIL_STOP")


# ── MINOR-3 and MAJOR-2: the target ───────────────────────────────────────────────────
class TestStockTargetsAreSnappedAgainstTheTrader:
    @pytest.mark.parametrize(
        "direction, target, snapped",
        [
            ("long", 1003.22, 100320),
            ("long", 1003.20, 100320),
            ("long", 1003.24, 100320),
            ("long", 1003.26, 100325),
            ("short", 973.57, 97360),
            ("short", 973.60, 97360),
            ("short", 973.51, 97355),
            ("short", 973.56, 97360),
        ],
    )
    def test_the_plans_target_level_is_on_the_tick_floored_for_a_long_ceiled_for_a_short(
        self, db, direction, target, snapped
    ):
        alert = U.make_alert("A1", direction=direction, target=target)
        opened(db, alert)
        levels = read_plan(db.engine)["levels"]
        assert (
            levels["underlying_target_paise"]
            == snapped
            == U.snap_target(H.to_paise(target), direction)
        )

    @PROP
    @given(paise=st.integers(30_000, 300_000), direction=st.sampled_from(["long", "short"]))
    def test_property_the_snapped_target_is_on_the_grid_and_never_better_for_the_trader(
        self, paise, direction
    ):
        target = paise / 100
        alert = U.make_alert("P1", direction=direction, entry=1000.0, target=target)
        with H.temp_book() as (book, engine):
            H.open_trade(book, alert)
            level = read_plan(engine, "P1")["levels"]["underlying_target_paise"]
        assert level % 5 == 0 and abs(level - paise) < 5
        assert level <= paise if direction == "long" else level >= paise

    def test_a_long_target_fills_only_when_traded_through_the_snapped_level_by_a_tick(self, db):
        """target 1003.22 snaps to 1003.20: a high of 1003.22 is not through it by a tick
        (1003.25 is), and the exit is exactly the snapped level, unslipped."""
        book = opened(db, U.make_alert("A1", target=1003.22))
        step(book, U.story([Q, (100000, 100322, 99950, 100100)]), H.at(10, 8))
        assert U.row_of(db.engine)["status"] == "OPEN"
        through = (100000, 100325, 99950, 100100)  # a NEW bar: a seen bar is never re-judged
        step(book, U.story([Q, (100000, 100322, 99950, 100100), through]), H.at(10, 9))
        row = U.row_of(db.engine)
        assert row["close_reason"] == "TARGET" and row["exit_paise"] == 100320
        assert row["gross_paise"] == 100320 - 100030 == 290

    def test_a_short_target_fills_only_when_traded_through_the_snapped_level_by_a_tick(self, db):
        """target 973.57 snaps UP to 973.60: a low of 973.57 is not through it by a tick (973.55
        is); the exit is exactly 973.60."""
        book = opened(db, U.make_alert("A1", direction="short", target=973.57))
        step(book, U.story([Q, (100000, 100050, 97357, 99900)]), H.at(10, 8))
        assert U.row_of(db.engine)["status"] == "OPEN"
        through = (100000, 100050, 97355, 99900)  # a NEW bar: a seen bar is never re-judged
        step(book, U.story([Q, (100000, 100050, 97357, 99900), through]), H.at(10, 9))
        row = U.row_of(db.engine)
        assert row["close_reason"] == "TARGET" and row["exit_paise"] == 97360
        assert row["gross_paise"] == -(97360 - 99970) == 2610

    def test_an_options_underlying_target_is_not_snapped_it_is_not_a_traded_price(self, db):
        pre = (5399000, 5399500, 5398500, 5399000)
        fill = (5400000, 5401000, 5399000, 5400500)
        pricer = H.make_pricer()
        book = H.make_book(db.engine, premium_at=pricer)
        H.open_trade(book, U.make_alert("O1", kind="INDEX", target=54300.03))
        frame = U.pbars(H.at(10, 5), [pre, fill, (5400500, 5430008, 5400000, 5430000)])
        book.step(U.end_of(frame), H.Feed(BANKNIFTY=frame))
        assert read_plan(db.engine, "O1")["levels"]["underlying_target_paise"] == 5430003
        row = U.row_of(db.engine, "O1")
        assert row["close_reason"] == "TARGET"
        assert pricer.calls[-1][1] == 54300.03, "priced at the alert's own level"


class TestASlippedEntryNeverSitsPastTheTarget:
    @PROP
    @given(
        ticks=st.integers(-30, 30),
        direction=st.sampled_from(["long", "short"]),
        off_tick=st.sampled_from([0, 0, 1, 3]),
    )
    def test_property_filled_means_the_entry_is_short_of_the_target_and_a_target_close_wins(
        self, ticks, direction, off_tick
    ):
        sign = 1 if direction == "long" else -1
        raw_target = 100000 + sign * 150 + sign * off_tick  # paise: off the grid when off_tick
        target = U.snap_target(raw_target, direction)
        open_p = target + ticks * 5 * (1 if sign > 0 else -1) + 0
        entry = H.expected_stock_entry(open_p / 100, direction)
        stop = open_p - sign * 5000
        alert = U.make_alert(
            "P1",
            direction=direction,
            entry=open_p / 100,
            stop=stop / 100,
            target=raw_target / 100,
        )
        through = target + sign * 5
        rows = [
            (open_p, open_p + 5, open_p - 5, open_p),
            (open_p, max(open_p, through) + 5, min(open_p, through) - 5, open_p),
        ]
        frame = U.story(rows)
        with H.temp_book() as (book, engine):
            H.open_trade(book, alert)
            events = step(book, frame, U.end_of(frame))
            row = U.row_of(engine, "P1")
            assert book.status()["hook_errors"] == 0
        no_fill = (open_p - target) * sign >= 0 or (entry - target) * sign >= 0
        if no_fill:
            assert row["status"] == "NO_FILL" and row["entry_paise"] is None, (
                open_p,
                entry,
                target,
            )
            assert row["close_reason"] == "GAP_THROUGH_TARGET"
            assert "gap_through_target" in H.blob(events[-1].detail)
            return
        assert (entry - target) * sign < 0 and row["entry_paise"] == entry
        if row["close_reason"] == "TARGET":
            assert row["exit_paise"] == target
            assert row["gross_paise"] > 0, "a TARGET close never books a loss"


# ── MINOR-2: session_end NO_FILL ──────────────────────────────────────────────────────
class TestAFillBarAtOrAfterExitTime:
    def test_a_stock_alert_whose_first_bar_opens_at_exit_time_is_no_fill_session_end(self, db):
        alert = U.make_alert("A1", created=H.at(15, 14, 3))
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        frame = U.pbars(H.at(15, 13), [Q] * 8)
        events = step(book, frame, H.at(15, 30))
        row = U.row_of(db.engine)
        assert (row["status"], row["close_reason"]) == ("NO_FILL", "NO_FILL")
        assert row["entry_paise"] is None and row["exit_paise"] is None
        assert row["costs_paise"] == 0 == row["net_paise"]
        assert [str(e.kind) for e in events] == ["NO_FILL"]
        assert events[0].detail["reason"] == "session_end"
        assert H.same_instant(events[0].ts, H.at(15, 15)), "stamped with the fill bar's open"
        assert book.status()["no_fill"] == 1 and book.status()["filled"] == 0
        assert book.open_trades() == [] and book.status()["hook_errors"] == 0

    def test_it_is_final_and_never_managed_later(self, db):
        alert = U.make_alert("A1", created=H.at(15, 14, 3))
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        frame = U.pbars(H.at(15, 13), [Q] * 8)
        step(book, frame, H.at(15, 30))
        before = (H.trade_rows(db.engine), H.event_rows(db.engine))
        assert step(book, frame, H.at(15, 45)) == []
        assert (H.trade_rows(db.engine), H.event_rows(db.engine)) == before

    def test_the_exit_time_of_the_alert_is_the_one_in_force_at_the_open(self, db):
        """Rules are frozen when the trade opens: moving exit_time in the stored alert later
        cannot turn a NO_FILL into a fill (the book decides from plan['rules'])."""
        alert = U.make_alert("A1", created=H.at(15, 14, 3), manage={"exit_time": "15:15"})
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        plan = read_plan(db.engine)
        plan["alert"]["manage"]["exit_time"] = "15:30"
        write_plan(db.engine, plan)
        step(book, U.pbars(H.at(15, 13), [Q] * 8), H.at(15, 30))
        assert U.row_of(db.engine)["status"] == "NO_FILL"

    def test_a_delayed_trade_is_judged_on_data_time_like_every_other_fill(self, db):
        """Yahoo grade, 2-minute bars on the 09:15 grid, created 15:14:03: the first bar after is
        15:15 (at exit_time): NO_FILL session_end, once the DATA has reached it."""
        alert = U.make_alert("A1", created=H.at(15, 14, 3), source="yahoo", resolution="2m")
        book = H.make_book(db.engine)
        H.open_trade(book, alert)
        frame = session_rows(2, {})
        step(book, frame, H.at(15, 16))  # the 15:15 bar has not closed at wall 15:16
        assert U.row_of(db.engine)["status"] == "PENDING_FILL"
        events = step(book, frame, H.at(15, 40))
        assert U.row_of(db.engine)["status"] == "NO_FILL"
        assert events[0].detail["reason"] == "session_end"

    def test_through_the_desk_the_same_alert_is_no_fill_session_end(self, tmp_path):
        from onelot.config import AppConfig

        cfg = AppConfig.load()
        d = H.new_db(tmp_path, "desk.db", make_global=True)
        try:
            book = H.desk_book(cfg, d.engine)
            desk, _ = H.new_desk(cfg, d, paper=book)
            desk.poll()
            index = desk.trackers["SBIN"].bars.index
            when = [t for t in index if t.hour == 15 and t.minute == 13][-1] + dt.timedelta(
                seconds=63
            )
            alert = H.raise_alert(desk, "SBIN", now=when.to_pydatetime())
            desk.poll()
            (row,) = [r for r in H.trade_rows(d.engine) if r["alert_id"] == alert["id"]]
            assert H.val(row["status"]) == "NO_FILL" and row["entry_paise"] is None
            assert desk.snapshot()["paper"]["no_fill"] >= 1
        finally:
            H.dispose_db(d, was_global=True)


# ── MINOR-1: no usable bar at the session's end ───────────────────────────────────────
def unusable_frame(rows=7):
    """Every bar after the alert has a valid OPEN (so the trade fills) but a NaN low: the trade
    can be filled and never managed."""
    frame = U.story([Q] * rows)
    frame.iloc[1:, frame.columns.get_loc("low")] = float("nan")
    return frame


class TestNoUsableBarWhenTheSessionEnds:
    def test_the_trade_closes_session_end_at_the_close_with_no_money_and_two_events(self, db):
        book = opened(db)
        events = step(book, unusable_frame(), H.at(15, 40))
        row = U.row_of(db.engine)
        assert (row["status"], row["close_reason"]) == ("CLOSED", "SESSION_END")
        assert row["entry_paise"] == 100030, "it did fill"
        for column in ("exit_paise", "gross_paise", "costs_paise", "net_paise"):
            assert row[column] is None, column
        assert H.same_instant(row["exit_ts"], H.at(15, 30))
        closed = U.close_of(events)
        assert closed.event.detail["fallback"] == "no_usable_bar" and closed.at == H.at(15, 30)
        unpriced = [e for e in events if str(e.kind) == "PRICING_UNAVAILABLE"]
        assert len(unpriced) == 1 and unpriced[0].detail["leg"] == "exit"
        stored = {str(H.val(e["kind"])) for e in H.event_rows(db.engine, "A1")}
        assert {"FILLED", "CLOSED", "PRICING_UNAVAILABLE"} <= stored
        assert book.status()["closed"] == 1 and book.status()["hook_errors"] == 0
        assert book.open_trades() == []

    def test_before_the_session_ends_it_stays_open_and_nothing_is_invented(self, db):
        book = opened(db)
        step(book, unusable_frame(), H.at(15, 29))
        row = U.row_of(db.engine)
        assert row["status"] == "OPEN" and row["exit_paise"] is None and row["close_reason"] is None

    def test_a_series_that_gives_no_bars_at_all_still_ends_the_trade_at_the_close(self, db):
        book = opened(db)
        step(book, unusable_frame(), H.at(10, 30))
        assert U.row_of(db.engine)["status"] == "OPEN"
        events = book.step(H.at(15, 40), H.Feed(SBIN=None))
        row = U.row_of(db.engine)
        assert (row["status"], row["close_reason"]) == ("CLOSED", "SESSION_END")
        assert (
            row["exit_paise"] is None
            and U.close_of(events).event.detail["fallback"] == "no_usable_bar"
        )

    def test_it_is_final(self, db):
        book = opened(db)
        step(book, unusable_frame(), H.at(15, 40))
        before = (H.trade_rows(db.engine), H.event_rows(db.engine))
        assert step(book, unusable_frame(), H.at(16, 0)) == []
        assert (H.trade_rows(db.engine), H.event_rows(db.engine)) == before

    def test_an_option_trade_ends_the_same_way(self, db):
        pre = (5399000, 5399500, 5398500, 5399000)
        fill = (5400000, 5401000, 5399000, 5400500)
        book = H.make_book(db.engine)
        H.open_trade(book, U.make_alert("O1", kind="INDEX"))
        frame = U.pbars(H.at(10, 5), [pre, fill, fill, fill])
        frame.iloc[1:, frame.columns.get_loc("low")] = float("nan")
        events = book.step(H.at(15, 40), H.Feed(BANKNIFTY=frame))
        row = U.row_of(db.engine, "O1")
        assert (row["status"], row["close_reason"]) == ("CLOSED", "SESSION_END")
        assert row["entry_paise"] is not None and row["exit_paise"] is None
        assert row["gross_paise"] is None and row["costs_paise"] is None
        assert "PRICING_UNAVAILABLE" in U.kinds(events)

    def test_a_trade_that_did_manage_a_bar_still_closes_at_that_bars_close_with_money(self, db):
        """The ordinary fallback is unchanged: the last close seen is the price."""
        book = opened(db)
        frame = U.story([(100000, 100100, 99900, 100123)])
        step(book, frame, H.at(15, 40))
        row = U.row_of(db.engine)
        assert row["close_reason"] == "SESSION_END"
        assert row["exit_paise"] == U.expected_stock_exit(100123, "long", "SESSION_END")


# ── BUG-1: remembered exits across restarts ───────────────────────────────────────────
EXIT_ROWS = [Q, Q, Q, Q, (100200, 100300, 100100, 100250), (100300, 100400, 100200, 100350)]


def reporting(reason="SIGNAL_EXIT", at=None):
    when = at or H.at(10, 10)
    return lambda trade, now: U.strategy_exit(reason, when)


def silent(trade, now):
    return None


class TestRememberedExits:
    @pytest.mark.parametrize("source", ["dhan", "yahoo"])
    def test_an_exit_ahead_of_data_time_survives_a_restart_and_a_silent_callback(self, db, source):
        frame = U.story(EXIT_ROWS)
        first = opened(db, U.make_alert("A1", source=source))
        step(first, frame, H.at(10, 8) if source == "dhan" else H.at(10, 20), reporting())
        assert U.row_of(db.engine)["status"] == "OPEN"
        reborn = H.make_book(db.engine)
        step(reborn, frame, H.at(10, 12) if source == "dhan" else H.at(10, 40), silent)
        row = U.row_of(db.engine)
        assert row["close_reason"] == "SIGNAL_EXIT" and H.same_instant(row["exit_ts"], H.at(10, 10))
        assert row["exit_paise"] == 100165 and reborn.status()["hook_errors"] == 0

    def test_a_remembered_exit_never_closes_before_the_data_has_reached_its_bar(self, db):
        frame = U.story(EXIT_ROWS)
        book = opened(db, U.make_alert("A1", source="yahoo"))
        step(book, frame, H.at(10, 20), reporting())  # data time 10:05
        for wall in (H.at(10, 21), H.at(10, 24), H.at(10, 25, 30)):  # data 10:06 .. 10:10:30
            step(book, frame, wall, silent)
            assert U.row_of(db.engine)["status"] == "OPEN", wall
        step(book, frame, H.at(10, 27), silent)  # data 10:12: the exit bar 10:10 has closed
        assert U.row_of(db.engine)["close_reason"] == "SIGNAL_EXIT"

    def test_a_stale_report_ahead_of_data_time_is_not_remembered(self, db):
        frame = U.story(EXIT_ROWS)
        book = opened(db, U.make_alert("A1", source="yahoo"))
        step(book, frame, H.at(10, 20), reporting("SIGNAL_EXIT", H.at(10, 5)))
        step(book, frame, H.at(10, 45), silent)
        assert U.row_of(db.engine)["status"] == "OPEN"

    def test_the_earliest_remembered_exit_wins_across_a_delayed_and_a_later_report(self, db):
        frame = U.story(EXIT_ROWS)
        book = opened(db, U.make_alert("A1", source="yahoo"))
        step(book, frame, H.at(10, 20), reporting("OPPOSITE_SIGNAL", H.at(10, 8)))
        step(book, frame, H.at(10, 21), reporting("SIGNAL_EXIT", H.at(10, 10)))
        step(book, frame, H.at(10, 45), silent)
        row = U.row_of(db.engine)
        assert row["close_reason"] == "OPPOSITE_SIGNAL" and H.same_instant(
            row["exit_ts"], H.at(10, 8)
        )

    def test_a_remembered_exit_gives_way_to_a_stop_on_an_earlier_bar(self, db):
        rows = [Q, Q, (99800, 99900, 98900, 99000), Q, (100200, 100300, 100100, 100250)]
        frame = U.story(rows)
        book = opened(db, U.make_alert("A1", source="yahoo"))
        step(book, frame, H.at(10, 20), reporting())
        step(book, frame, H.at(10, 45), silent)
        row = U.row_of(db.engine)
        assert row["close_reason"] == "STOP" and H.same_instant(row["exit_ts"], H.at(10, 8))

    def test_a_failing_callback_is_logged_once_per_trade_and_counted_every_time(self, db, caplog):
        caplog.set_level(logging.DEBUG, logger="onelot.paper.book")
        book = opened(db)
        frame = U.story([Q] * 5)

        def boom(trade, now):
            raise RuntimeError("strategy exploded")

        for minute in (7, 8, 9):
            step(book, frame, H.at(10, minute), boom)
        tracebacks = [
            r
            for r in caplog.records
            if r.name == "onelot.paper.book" and r.levelno >= logging.ERROR and r.exc_info
        ]
        assert len(tracebacks) == 1, "the traceback is logged once for the trade"
        assert book.status()["hook_errors"] == 3, "and every failure is counted"
        assert U.row_of(db.engine)["status"] == "OPEN"


# ── rules frozen at the open ──────────────────────────────────────────────────────────
class TestTheRulesAreFrozenAtTheOpen:
    def test_a_stored_alert_edited_after_the_open_does_not_change_what_the_book_does(self, db):
        alert = U.make_alert("A1", manage={"max_bars": 3, "tf_min": 1, "exit_time": "15:15"})
        book = opened(db, alert)
        step(book, U.story([Q, Q]), H.at(10, 8))
        plan = read_plan(db.engine)
        plan["alert"]["manage"]["max_bars"] = 0  # a later edit: the time stop would vanish
        write_plan(db.engine, plan)
        reborn = H.make_book(db.engine)
        step(reborn, U.story([Q] * 8), H.at(10, 20))
        row = U.row_of(db.engine)
        assert row["close_reason"] == "TIME_STOP" and H.same_instant(row["exit_ts"], H.at(10, 9))

    def test_an_open_row_written_before_the_rules_were_frozen_is_still_managed(self, db):
        alert = U.make_alert("A1", manage={"trail_atr_mult": 1.5, "atr": 4.0, "tf_min": 1})
        book = opened(db, alert)
        step(book, U.story([Q]), H.at(10, 7))
        plan = read_plan(db.engine)
        plan.pop("rules")
        write_plan(db.engine, plan)
        reborn = H.make_book(db.engine)
        frame = U.story([Q, BAR1, (101100, 101200, 100500, 101000)])
        step(reborn, frame, U.end_of(frame))
        row = U.row_of(db.engine)
        assert row["close_reason"] == "TRAIL_STOP"
        assert "rules" in read_plan(db.engine), "stored back once read from the alert"
        assert reborn.status()["hook_errors"] == 0


# ── BUG-2: the cursor guard ───────────────────────────────────────────────────────────
class TestTwoBooksNeverDuplicateAnEvent:
    ROWS = [BAR1, (101100, 101300, 100800, 101150), (101150, 101400, 100900, 101300)]

    def trail_alert(self):
        return U.make_alert("A1", manage={"trail_atr_mult": 1.5, "atr": 4.0, "tf_min": 1})

    def test_a_stale_books_step_books_nothing_and_adopts_the_row(self, db):
        frame = U.story(self.ROWS)
        first = H.make_book(db.engine)
        H.open_trade(first, self.trail_alert())
        first.step(H.at(10, 7), H.Feed(SBIN=frame))
        stale = H.make_book(db.engine)
        first.step(U.end_of(frame), H.Feed(SBIN=frame))
        assert stale.step(U.end_of(frame), H.Feed(SBIN=frame)) == []
        (mine,) = stale.open_trades()
        assert mine.last_bar_ts == U.row_of(db.engine)["last_bar_ts"], (
            "the stale copy was refreshed"
        )

    def test_the_adopted_book_carries_on_from_the_database_state(self, db):
        full = U.story(
            [*self.ROWS, (101300, 101350, 101000, 101050), (101000, 101100, 100400, 100800)]
        )
        early = U.story(self.ROWS[:1])
        first = H.make_book(db.engine)
        H.open_trade(first, self.trail_alert())
        first.step(H.at(10, 7), H.Feed(SBIN=early))
        stale = H.make_book(db.engine)
        first.step(H.at(10, 9), H.Feed(SBIN=full))
        stale.step(H.at(10, 9), H.Feed(SBIN=full))  # rejected, adopted
        stale.step(U.end_of(full), H.Feed(SBIN=full))  # now carries on and closes it
        kinds = Counter(str(H.val(e["kind"])) for e in H.event_rows(db.engine, "A1"))
        assert kinds["CLOSED"] == 1 and kinds["STOP_MOVED"] == 3
        assert U.row_of(db.engine)["close_reason"] == "TRAIL_STOP"

    @PROP
    @given(case=cases(), sizes=st.lists(st.integers(1, 7), min_size=1, max_size=6))
    def test_property_two_books_alternating_book_exactly_what_one_book_books(self, case, sizes):
        alert = alert_for(case)
        frame = frame_of(case)
        n = len(case.rows)
        with H.temp_book() as (single, single_engine):
            H.open_trade(single, copy.deepcopy(alert))
            single.step(U.end_of(frame), H.Feed(SBIN=frame), callback(case))
            want_events = [
                (str(H.val(e["kind"])), e["ts"]) for e in H.event_rows(single_engine, "A1")
            ]
            want_row = row_keys(single_engine)
        with H.temp_book() as (a, engine):
            H.open_trade(a, copy.deepcopy(alert))
            b = H.make_book(engine)
            books, done = [a, b], 0
            for turn, size in enumerate((*sizes, n)):
                done = min(done + size, n)
                now = case.start + done * MIN
                books[turn % 2].step(now, H.Feed(SBIN=frame), callback(case))
            for turn in range(3):  # a stale book adopts instead of managing: a few more polls
                books[turn % 2].step(U.end_of(frame), H.Feed(SBIN=frame), callback(case))
            got_events = [(str(H.val(e["kind"])), e["ts"]) for e in H.event_rows(engine, "A1")]
            assert got_events == want_events
            assert row_keys(engine) == want_row
            assert sum(bk.status()["hook_errors"] for bk in books) == 0
