"""PaperBook.step manages OPEN trades to a close (US-002 integration, T3).

The rules live in `onelot.paper.manage` (tested there); these tests pin what the BOOK adds:
the exit price (a stock slipped by slippage_pct against the trader, a target unslipped, an
option sold at `option_exit_paise` on the unslipped underlying level), gross_paise, the row's
exit columns, the state that must survive a restart, data time for a delayed feed, the
strategy-exit callback, and that a step with nothing new writes nothing.

Worked stock (conftest): RELIANCE long, alert entry 1000.00, stop 995.00, target 1010.00,
created 10:05:03; the 10:06 bar opens 1000.00 so the fill is 1000.30 (100030 paise).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import update

from onelot.paper.manage import StrategyExit
from onelot.store.schema import paper_events, paper_trades

from .conftest import THU, ist, make_alert, table_rows

Row = tuple[float, float, float, float]
FILL = (1000.00, 1001.00, 999.00, 1000.50)  # the 10:06 fill bar: touches nothing
QUIET = (1000.00, 1001.00, 999.00, 1000.50)
ENTRY = 100_030


def bars(rows: Sequence[Row], start: dt.datetime | None = None) -> pd.DataFrame:
    start = start or ist(THU, 10, 6)
    idx = pd.date_range(start, periods=len(rows), freq="1min")
    o, h, l, c = (list(col) for col in zip(*rows, strict=True))
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": 1e3}, index=idx)


def feed(**frames: pd.DataFrame | None) -> Callable[[str], pd.DataFrame | None]:
    return lambda key: frames.get(key)


def at(minute: int, hour: int = 10) -> dt.datetime:
    return ist(THU, hour, minute)


def the_row(engine) -> dict[str, Any]:
    (row,) = table_rows(engine, paper_trades)
    return row


def kinds(engine) -> list[str]:
    return [e["kind"] for e in table_rows(engine, paper_events)]


def opened(make_book, alert: Mapping[str, Any] | None = None, **kw: Any):
    book = make_book(**kw)
    book.on_alert(alert or make_alert(), ist(THU, 10, 5, 4))
    return book


class TestTheClose:
    def test_a_stop_closes_at_the_slipped_level_in_the_same_step_as_the_fill(
        self, make_book, engine
    ):
        book = opened(make_book)
        events = book.step(at(8), feed(RELIANCE=bars([FILL, (999.00, 999.50, 994.00, 996.00)])))

        row = the_row(engine)
        # 995.00 x (1 - 0.03 %) = 994.7015 -> a long sells, so the tick floors it: 994.70
        assert (row["status"], row["close_reason"], row["exit_paise"]) == ("CLOSED", "STOP", 99_470)
        assert row["gross_paise"] == 99_470 - ENTRY == -560
        assert row["exit_ts"] == at(7) and row["last_bar_ts"] == at(7)
        # US-023: booked with the close. BUY 1000.30: 42.15 -> 42; SELL 994.70: 63.80 -> 64
        assert (row["costs_paise"], row["net_paise"]) == (106, -560 - 106)
        assert [e.kind for e in events] == ["FILLED", "CLOSED"]
        closed = events[-1]
        assert (closed.ts, closed.detail["price_paise"], closed.detail["exit_paise"]) == (
            at(7),
            99_500,
            99_470,
        )
        assert kinds(engine) == ["OPENED", "FILLED", "CLOSED"]
        assert book.open_trades() == [] and book.status()["closed"] == 1

    def test_a_target_is_a_limit_fill_and_is_not_slipped(self, make_book, engine):
        book = opened(make_book)
        book.step(at(8), feed(RELIANCE=bars([FILL, (1005.00, 1010.05, 1004.00, 1009.00)])))
        row = the_row(engine)
        assert (row["close_reason"], row["exit_paise"], row["gross_paise"]) == (
            "TARGET",
            101_000,
            101_000 - ENTRY,
        )

    def test_a_short_buys_back_up(self, make_book, engine):
        book = opened(make_book, make_alert(direction="short"))  # stop 1005.00, fill 999.70
        book.step(at(8), feed(RELIANCE=bars([FILL, (1002.00, 1005.00, 1001.00, 1003.00)])))
        row = the_row(engine)
        # 1005.00 x 1.0003 = 1005.3015 -> a short buys back, so the tick ceils it: 1005.35
        assert (row["close_reason"], row["exit_paise"]) == ("STOP", 100_535)
        assert row["entry_paise"] == 99_970 and row["gross_paise"] == -(100_535 - 99_970)

    def test_an_option_is_sold_at_the_labs_exit_fill_on_the_unslipped_level(
        self, make_book, engine
    ):
        calls: list[tuple[float, dt.datetime]] = []

        def premium_at(option: Mapping[str, Any], underlying: float, ts: dt.datetime) -> float:
            calls.append((underlying, ts))
            return 312.34 + 0.65 * (underlying - 45210.35)

        alert = make_alert("INDEX")  # BUY_CE: underlying stop 45110.35
        book = opened(make_book, alert, premium_at=premium_at)
        rows = [(45210.35, 45220.00, 45200.00, 45215.00), (45150.00, 45160.00, 45100.00, 45120.00)]
        book.step(at(8), feed(BANKNIFTY=bars(rows)))

        row = the_row(engine)
        assert row["close_reason"] == "STOP"
        assert calls[-1] == (45110.35, at(7))  # the stop level itself, at the closing bar
        fair = 312.34 + 0.65 * (45110.35 - 45210.35)
        from onelot.paper.fill import option_exit_paise

        assert row["exit_paise"] == option_exit_paise(fair, 0.006, 2, 0.05)
        assert row["gross_paise"] == (row["exit_paise"] - row["entry_paise"]) * 30  # long premium

    def test_an_option_exit_that_cannot_be_priced_closes_without_money_and_says_so(
        self, make_book, engine
    ):
        def premium_at(option: Mapping[str, Any], underlying: float, ts: dt.datetime):
            return 312.34 if ts <= at(6) else None  # entry and risk price; the exit does not

        book = opened(make_book, make_alert("INDEX"), premium_at=premium_at)
        rows = [(45210.35, 45220.00, 45200.00, 45215.00), (45150.00, 45160.00, 45100.00, 45120.00)]
        events = book.step(at(8), feed(BANKNIFTY=bars(rows)))

        row = the_row(engine)
        assert (row["status"], row["close_reason"]) == ("CLOSED", "STOP")
        assert row["exit_paise"] is None and row["gross_paise"] is None  # never a silent zero
        unpriced = [e for e in events if e.kind == "PRICING_UNAVAILABLE"]
        assert [e.detail["leg"] for e in unpriced] == ["exit"]


class TestTheStateSurvivesARestart:
    # a 1-minute strategy: every 1-minute close is a strategy-bar close the trail may use
    TRAIL = {"trail_atr_mult": 1.0, "atr": 2.0, "tf_min": 1}

    def test_a_trailed_stop_is_persisted(self, make_book, engine):
        alert = make_alert()
        alert["manage"] |= self.TRAIL
        rows = [FILL, (1000.00, 1006.00, 999.50, 1005.60), (1004.00, 1004.50, 1003.60, 1003.80)]
        first = opened(make_book, alert)
        first.step(at(8), feed(RELIANCE=bars(rows)))  # 10:07 closes +1R (risk 5.30): armed
        assert the_row(engine)["status"] == "OPEN"
        reborn = make_book()
        reborn.step(at(9), feed(RELIANCE=bars(rows)))
        row = the_row(engine)
        # 1005.60 - 2.00 = 1003.60 from 10:08; the 10:08 low touches it
        assert (row["close_reason"], row["exit_ts"]) == ("TRAIL_STOP", at(8))
        assert kinds(engine) == ["OPENED", "FILLED", "STOP_MOVED", "CLOSED"]

    def test_a_pending_strategy_exit_is_persisted(self, make_book, engine):
        rows = [FILL, QUIET, QUIET, QUIET, (1002.00, 1003.00, 1001.00, 1002.50)]  # 10:06 .. 10:10
        signal = StrategyExit("SIGNAL_EXIT", at(10))
        first = opened(make_book)
        forming = at(10) + dt.timedelta(seconds=30)  # the 10:10 exit bar has not closed
        first.step(forming, feed(RELIANCE=bars(rows)), lambda t, n: signal)
        reborn = make_book()
        reborn.step(at(11), feed(RELIANCE=bars(rows)))  # the callback no longer says so
        row = the_row(engine)
        assert (row["close_reason"], row["exit_ts"]) == ("SIGNAL_EXIT", at(10))
        assert row["exit_paise"] == 100_165  # 1002.00 x 0.9997 = 1001.6994 -> 1001.65

    def test_an_open_row_without_a_position_record_is_rebuilt_from_its_fill(
        self, make_book, engine
    ):
        book = opened(make_book)
        book.step(at(7), feed(RELIANCE=bars([FILL])))
        with engine.begin() as conn:  # a row opened before US-002 stored no position
            row = the_row(engine)
            plan = {k: v for k, v in row["plan"].items() if k != "position"}
            conn.execute(update(paper_trades).values(plan=plan))
        reborn = make_book()
        reborn.step(at(8), feed(RELIANCE=bars([FILL, (999.00, 999.50, 994.00, 996.00)])))
        assert (the_row(engine)["close_reason"], the_row(engine)["exit_paise"]) == ("STOP", 99_470)


class TestStepping:
    def test_a_step_with_nothing_new_writes_nothing(self, make_book, engine):
        book = opened(make_book)
        frame = bars([FILL, QUIET])
        book.step(at(8), feed(RELIANCE=frame))
        before = the_row(engine)
        assert book.step(at(8), feed(RELIANCE=frame)) == []
        assert the_row(engine) == before  # updated_at included: no write at all

    def test_a_delayed_trade_is_managed_on_data_time(self, make_book, engine):
        book = opened(make_book, make_alert(source="yahoo"), feed_delay_minutes=15)
        frame = bars([FILL, (999.00, 999.50, 994.00, 996.00)])
        book.step(at(8), feed(RELIANCE=frame))  # fills; data time 09:53 has no closed bar
        assert the_row(engine)["status"] == "OPEN"
        book.step(at(23), feed(RELIANCE=frame))  # data time 10:08: the 10:07 bar has closed
        assert the_row(engine)["close_reason"] == "STOP"

    def test_a_tuple_from_the_desk_is_a_strategy_exit(self, make_book, engine):
        rows = [FILL, QUIET, QUIET, QUIET, (1002.00, 1003.00, 1001.00, 1002.50)]
        book = opened(make_book)
        book.step(at(11), feed(RELIANCE=bars(rows)), lambda t, n: ("OPPOSITE_SIGNAL", at(10)))
        assert the_row(engine)["close_reason"] == "OPPOSITE_SIGNAL"

    @pytest.mark.parametrize(
        "answer",
        [
            "SIGNAL_EXIT",  # not a StrategyExit
            ("SQUARE_OFF", "x"),  # not a reason
            ("SIGNAL_EXIT", dt.datetime(2026, 10, 29, 10, 10)),  # naive
        ],
    )
    def test_a_malformed_answer_is_ignored_and_counted(self, make_book, engine, answer):
        rows = [FILL, (999.00, 999.50, 994.00, 996.00)]
        book = opened(make_book)
        book.step(at(8), feed(RELIANCE=bars(rows)), lambda t, n: answer)
        assert the_row(engine)["close_reason"] == "STOP"  # managed anyway
        assert book.status()["hook_errors"] == 1

    def test_an_answer_ahead_of_the_data_waits_as_pending_and_is_not_fatal(self, make_book, engine):
        """Tester BUG-1: an exit whose bar closes after the trade's data time is remembered
        (it closes only at an open the data reaches), never dropped and never an error."""
        rows = [FILL, QUIET]
        book = opened(make_book)
        book.step(
            at(8), feed(RELIANCE=bars(rows)), lambda t, n: StrategyExit("SIGNAL_EXIT", at(30))
        )
        row = the_row(engine)
        assert row["status"] == "OPEN" and row["last_bar_ts"] == at(7)
        pending = row["plan"]["position"]["pending_exit"]
        assert pending == {"reason": "SIGNAL_EXIT", "bar_close_ts": at(30).isoformat()}
        assert book.status()["hook_errors"] == 0

    def test_a_failing_callback_is_counted_every_time_but_logged_once_per_trade(
        self, make_book, engine, caplog
    ):
        def broken(trade: Any, now: dt.datetime) -> None:
            raise RuntimeError("strategy state unavailable")

        book = opened(make_book)
        frame = bars([FILL, QUIET, QUIET, QUIET])
        with caplog.at_level("DEBUG", logger="onelot.paper"):
            for minute in (8, 9, 10):
                book.step(at(minute), feed(RELIANCE=frame), broken)
        failed = [r for r in caplog.records if "strategy_exit failed" in r.getMessage()]
        assert [r.levelname for r in failed] == ["ERROR", "DEBUG", "DEBUG"]
        assert book.status()["hook_errors"] == 3 and the_row(engine)["status"] == "OPEN"

    def test_remembering_an_exit_before_the_fill_bar_is_managed_keeps_the_rows_cursor(
        self, make_book, engine
    ):
        """NIT: on data time a delayed trade's fill bar is not managed yet; a write that only
        remembers an exit must not reset the row's cursor (the fill bar) to NULL."""
        book = opened(make_book, make_alert(source="yahoo"), feed_delay_minutes=15)
        frame = bars([FILL, QUIET])
        book.step(at(8), feed(RELIANCE=frame))  # fills at 10:06; data time 09:53
        book.step(at(9), feed(RELIANCE=frame), lambda t, n: StrategyExit("SIGNAL_EXIT", at(9)))
        row = the_row(engine)
        assert row["plan"]["position"]["pending_exit"] is not None
        assert row["last_bar_ts"] == at(6)


class TestASeriesThatGoesQuiet:
    def test_an_open_trade_without_bars_still_closes_once_its_session_is_over(
        self, make_book, engine
    ):
        """The desk dropped the series (or the feed died): no bar will come, yet no paper
        trade is carried overnight. The last close it saw is the price (A5)."""
        book = opened(make_book)
        book.step(at(8), feed(RELIANCE=bars([FILL, (1000.00, 1001.00, 999.00, 1000.40)])))
        assert the_row(engine)["status"] == "OPEN"
        assert book.step(ist(THU, 15, 29), feed()) == []  # not over yet: nothing changes
        events = book.step(ist(THU, 15, 30), feed())
        row = the_row(engine)
        assert (row["close_reason"], row["exit_ts"]) == ("SESSION_END", at(8))
        # 1000.40 x 0.9997 = 1000.0999 -> 1000.05 for a seller
        assert row["exit_paise"] == 100_005 and [e.kind for e in events] == ["CLOSED"]

    @pytest.mark.parametrize(
        "spoil",
        [
            lambda f: f.tz_localize(None),  # a naive index: never guessed
            lambda f: f.drop(columns="high"),  # a feed without the full OHLC
        ],
        ids=["naive-index", "no-high-column"],
    )
    def test_an_open_trade_whose_frame_cannot_be_read_still_closes_once_its_session_is_over(
        self, make_book, engine, spoil
    ):
        """A frame the OHLC view rejects is no bars for this step: nothing is managed on it,
        yet the trade is not carried overnight either (AC5)."""
        book = opened(make_book)
        book.step(at(8), feed(RELIANCE=bars([FILL, (1000.00, 1001.00, 999.00, 1000.40)])))
        unreadable = spoil(bars([FILL, QUIET, QUIET]))
        assert book.step(ist(THU, 15, 29), feed(RELIANCE=unreadable)) == []
        events = book.step(ist(THU, 15, 30), feed(RELIANCE=unreadable))
        row = the_row(engine)
        assert (row["close_reason"], row["exit_ts"]) == ("SESSION_END", at(8))
        assert row["exit_paise"] == 100_005 and [e.kind for e in events] == ["CLOSED"]


class TestASessionThatEndsWithoutAUsableBar:
    """Review round 1, MINOR-1: no trade stays OPEN forever. One whose session is over without
    a single usable managed bar closes SESSION_END at the session close with no money (NULL
    exit and gross, never an invented price) and a PRICING_UNAVAILABLE event that says why,
    like an option exit that cannot be priced; the warning is logged once."""

    def test_it_closes_with_no_money_and_says_why(self, make_book, engine, caplog):
        nan = float("nan")
        book = opened(make_book)
        book.step(at(8), feed(RELIANCE=bars([(1000.00, nan, nan, nan), (1000.00, nan, nan, nan)])))
        assert the_row(engine)["status"] == "OPEN"  # filled on the open, nothing managed
        with caplog.at_level("WARNING", logger="onelot.paper"):
            events = book.step(ist(THU, 15, 30), feed())
            assert book.step(ist(THU, 15, 31), feed()) == []
        row = the_row(engine)
        assert (row["status"], row["close_reason"], row["exit_ts"]) == (
            "CLOSED",
            "SESSION_END",
            ist(THU, 15, 30),
        )
        assert row["exit_paise"] is None and row["gross_paise"] is None
        assert [e.kind for e in events] == ["CLOSED", "PRICING_UNAVAILABLE"]
        assert events[1].detail["leg"] == "exit" and "no usable bar" in events[1].detail["error"]
        assert len([r for r in caplog.records if "no usable bar" in r.getMessage()]) == 1


def with_target(alert: dict[str, Any], target: float) -> dict[str, Any]:
    alert["economics"]["underlying_target"] = target
    return alert


class TestTheStockTargetLevel:
    """Review round 1: MINOR-3 snaps a stock target once, against the trader (a long's floors,
    a short's ceils); MAJOR-2 judges the target against the SLIPPED entry."""

    @pytest.mark.parametrize(
        ("direction", "target", "snapped"),
        [("long", 1010.03, 101_000), ("short", 989.97, 99_000)],
    )
    def test_an_off_tick_target_is_snapped_once_in_the_levels(
        self, make_book, direction, target, snapped
    ):
        book = make_book()
        trade = book.on_alert(
            with_target(make_alert(direction=direction), target), ist(THU, 10, 5, 4)
        )
        assert trade is not None and trade.plan["levels"]["underlying_target_paise"] == snapped

    def test_the_target_exit_and_its_trade_through_use_the_snapped_level(self, make_book, engine):
        book = opened(make_book, with_target(make_alert(), 1010.03))  # T = 1010.00
        book.step(at(8), feed(RELIANCE=bars([FILL, (1005.00, 1010.05, 1004.00, 1009.00)])))
        row = the_row(engine)  # 1010.05 trades through 1010.00 (not through 1010.03 + 1 tick)
        assert (row["close_reason"], row["exit_paise"]) == ("TARGET", 101_000)

    @pytest.mark.parametrize(
        ("direction", "target", "fills"),
        [
            ("long", 1000.30, False),  # the fill 1000.30 is AT the target
            ("long", 1000.35, True),  # one tick inside
            ("short", 999.70, False),  # the short's fill 999.70 is AT the target
            ("short", 999.65, True),
        ],
    )
    def test_a_slipped_entry_at_or_beyond_the_target_is_no_fill(
        self, make_book, engine, direction, target, fills
    ):
        book = opened(make_book, with_target(make_alert(direction=direction), target))
        events = book.step(at(7), feed(RELIANCE=bars([FILL])))
        row = the_row(engine)
        if fills:
            assert row["entry_paise"] is not None and "FILLED" in [e.kind for e in events]
            return
        assert (row["status"], row["close_reason"]) == ("NO_FILL", "GAP_THROUGH_TARGET")
        assert row["entry_paise"] is None and row["gross_paise"] == 0
        (event,) = events
        assert (event.kind, event.ts, event.detail["reason"]) == (
            "NO_FILL",
            at(6),
            "gap_through_target",
        )
        assert event.detail["entry_paise"] == event.detail["target_paise"]


class TestNoFillAtOrAfterExitTime:
    """Review round 1, MINOR-2: the Lab never fills at or after exit_time; nor does paper."""

    def test_a_fill_bar_opening_at_exit_time_is_no_fill_session_end(self, make_book, engine):
        book = make_book()
        book.on_alert(make_alert(created=ist(THU, 15, 14, 30)), ist(THU, 15, 14, 31))
        frame = bars([QUIET, QUIET], start=ist(THU, 15, 14))  # 15:14 is before the alert
        events = book.step(ist(THU, 15, 16), feed(RELIANCE=frame))
        row = the_row(engine)
        assert (row["status"], row["close_reason"], row["entry_paise"]) == (
            "NO_FILL",
            "NO_FILL",
            None,
        )
        assert [(e.kind, e.ts, e.detail["reason"]) for e in events] == [
            ("NO_FILL", ist(THU, 15, 15), "session_end")
        ]

    def test_the_bar_before_exit_time_still_fills_and_ends_at_the_next_open(
        self, make_book, engine
    ):
        book = make_book()
        book.on_alert(make_alert(created=ist(THU, 15, 13, 30)), ist(THU, 15, 13, 31))
        frame = bars([QUIET, QUIET, (1000.20, 1001.00, 999.00, 1000.50)], start=ist(THU, 15, 13))
        events = book.step(ist(THU, 15, 16), feed(RELIANCE=frame))
        assert [e.kind for e in events] == ["FILLED", "CLOSED"]
        row = the_row(engine)
        assert (row["entry_ts"], row["close_reason"], row["exit_ts"]) == (
            ist(THU, 15, 14),
            "SESSION_END",
            ist(THU, 15, 15),
        )


class TestTheRulesAreReadOnce:
    """Review round 1 NIT: the exit rules are parsed from the alert once, into the plan, and
    a step never re-parses them (nor re-warns about a value it could not read)."""

    def test_the_plan_carries_the_rules_from_the_open(self, make_book):
        alert = make_alert()
        alert["manage"] |= {"atr": 4.2, "atr_trail": 1.7, "trail_atr_mult": 1.5, "max_bars": 6}
        trade = make_book().on_alert(alert, ist(THU, 10, 5, 4))
        assert trade is not None
        rules = trade.plan["rules"]
        assert (rules["atr"], rules["trail_atr_mult"], rules["max_bars"]) == (1.7, 1.5, 6)
        assert (rules["tf_min"], rules["exit_time"], rules["tick_paise"]) == (5, "15:15", 5)

    def test_an_unreadable_value_is_warned_about_once_not_every_step(
        self, make_book, engine, caplog
    ):
        alert = make_alert()
        alert["manage"]["max_bars"] = "lots"
        with caplog.at_level("WARNING", logger="onelot.paper"):
            book = opened(make_book, alert)
            for minute in (7, 8, 9, 10):
                book.step(at(minute), feed(RELIANCE=bars([FILL, QUIET, QUIET, QUIET])))
        warned = [r for r in caplog.records if "max_bars" in r.getMessage()]
        assert len(warned) == 1 and the_row(engine)["status"] == "OPEN"
