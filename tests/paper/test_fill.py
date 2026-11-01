"""PaperBook.step: the fill rule and NO_FILL (US-001 AC2, AC3; the step half of AC7).

Judged on DATA time (ADR-001 amendment 3). A bar is eligible iff
    open > created_at  AND  open + resolution <= now
    AND open <= expires_at (+ paper.feed_delay_minutes for delayed grade)
    AND the bar is on the same IST session date as created_at.
The fill is the OPEN of the first eligible bar, slipped and snapped to the tick against
the trader (ADR-001 amendment 5). slippage_pct is a PERCENT; option_spread_pct a FRACTION.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

import pandas as pd
import pytest

from onelot.costs import round_to_tick
from onelot.paper.book import PaperEvent
from onelot.paper.money import to_paise
from onelot.store.schema import paper_events, paper_trades

from .conftest import BT, FRI, MON, THU, FakePricer, ist, make_alert, make_bars, table_rows

ZERO_MONEY = {"gross_paise": 0, "costs_paise": 0, "net_paise": 0}


def series(**frames: pd.DataFrame) -> Callable[[str], pd.DataFrame | None]:
    return lambda key: frames.get(key)


def the_row(engine) -> dict:
    rows = table_rows(engine, paper_trades)
    assert len(rows) == 1
    return rows[0]


def event_kinds(engine) -> list[str]:
    return [e["kind"] for e in table_rows(engine, paper_events)]


def sessions(res_min: int, *parts: tuple[dt.datetime, list[float]]) -> pd.DataFrame:
    """Bars over several sessions: each part is (first bar open, opens) at `res_min` spacing.

    Only ever split at a session boundary: a hole INSIDE a session is left to AC9 (mixed
    spacing grades delayed), so these fill tests stay neutral on it. A late first bar is
    modelled by a frame that starts late (a desk restart), as in the acceptance tests.
    """
    return pd.concat([make_bars(start, opens, res_min) for start, opens in parts])


# ── AC2: fill at the open of the first eligible closed bar ──────────────────────


class TestFillRule:
    def test_worked_example_long_fills_at_the_1006_open_plus_percent_slippage(
        self, make_book, engine
    ):
        book = make_book()
        book.on_alert(make_alert("STOCK", "long"), ist(THU, 10, 5, 4))  # created 10:05:03
        bars = make_bars(ist(THU, 10, 5), [1000.00, 1000.00])  # opens 10:05 and 10:06

        events = book.step(ist(THU, 10, 8), series(RELIANCE=bars))

        row = the_row(engine)
        assert row["status"] == "OPEN"
        assert row["entry_paise"] == 100030  # 1000.00 x (1 + 0.03 / 100), already on tick
        assert row["fill_bar_ts"] == ist(THU, 10, 6)
        assert row["entry_ts"] == ist(THU, 10, 6)
        assert len(events) == 1 and isinstance(events[0], PaperEvent)
        assert (events[0].kind, events[0].ts) == ("FILLED", ist(THU, 10, 6))
        assert events[0].detail["bar_open_paise"] == 100000  # the bar price used is booked
        assert event_kinds(engine) == ["OPENED", "FILLED"]
        assert book.status()["filled"] == 1
        (held,) = book.open_trades()
        assert (held.status, held.entry_paise) == ("OPEN", 100030)

    def test_short_fills_at_the_open_less_slippage(self, make_book, engine):
        book = make_book()
        book.on_alert(make_alert("STOCK", "short"), ist(THU, 10, 5, 4))
        book.step(ist(THU, 10, 8), series(RELIANCE=make_bars(ist(THU, 10, 5), [1000.0] * 2)))
        assert the_row(engine)["entry_paise"] == 99970  # 1000.00 x (1 - 0.03 / 100)

    @pytest.mark.parametrize(("direction", "paise"), [("long", 100045), ("short", 99975)])
    def test_fill_snaps_to_the_tick_against_the_trader(self, make_book, engine, direction, paise):
        # 1000.10 x 1.0003 = 1000.40003 -> UP to 1000.45; 1000.10 x 0.9997 = 999.79997 -> DOWN
        book = make_book()
        book.on_alert(make_alert("STOCK", direction), ist(THU, 10, 5, 4))
        book.step(ist(THU, 10, 8), series(RELIANCE=make_bars(ist(THU, 10, 5), [1000.10] * 2)))
        assert the_row(engine)["entry_paise"] == paise

    def test_a_bar_is_used_only_once_it_has_closed(self, make_book, engine):
        book = make_book()
        book.on_alert(make_alert(), ist(THU, 10, 5, 4))
        bars = series(RELIANCE=make_bars(ist(THU, 10, 5), [1000.0, 1000.0]))

        assert book.step(ist(THU, 10, 6, 59), bars) == []  # 10:06 bar still forming
        assert the_row(engine)["status"] == "PENDING_FILL"
        assert the_row(engine)["entry_paise"] is None

        assert [e.kind for e in book.step(ist(THU, 10, 7), bars)] == ["FILLED"]  # boundary
        assert the_row(engine)["fill_bar_ts"] == ist(THU, 10, 6)

    def test_no_look_ahead_the_bar_opening_at_created_at_is_not_eligible(self, make_book, engine):
        book = make_book()
        book.on_alert(make_alert(created=ist(THU, 10, 5)), ist(THU, 10, 5))
        book.step(ist(THU, 10, 8), series(RELIANCE=make_bars(ist(THU, 10, 5), [1001.0, 1000.0])))
        row = the_row(engine)
        assert row["fill_bar_ts"] == ist(THU, 10, 6)
        assert row["entry_paise"] == 100030  # not 100130 from the 10:05 bar

    @pytest.mark.parametrize(
        ("source", "feed_delay", "status"),
        [("yahoo", 15, "OPEN"), ("yahoo", 0, "NO_FILL"), ("dhan", 15, "NO_FILL")],
    )
    def test_delayed_grade_extends_the_fill_window_by_the_feed_delay(
        self, make_book, engine, source, feed_delay, status
    ):
        # created 10:05:03, expires 10:15:03; the (restarted) feed's first bar opens 10:20
        book = make_book(feed_delay_minutes=feed_delay)
        book.on_alert(make_alert(source=source, resolution="2m"), ist(THU, 10, 5, 4))
        bars = make_bars(ist(THU, 10, 20), [1000.0] * 2, res_min=2)

        book.step(ist(THU, 10, 40), series(RELIANCE=bars))

        row = the_row(engine)
        assert row["status"] == status
        if status == "OPEN":  # 10:20 <= 10:15:03 + 15 min: inside the delayed window
            assert (row["fill_bar_ts"], row["entry_paise"]) == (ist(THU, 10, 20), 100030)
        else:
            assert row["entry_paise"] is None

    @pytest.mark.parametrize("direction", ["long", "short"])
    def test_index_option_fill_mirrors_lab_execute_trade(self, make_book, engine, direction):
        # BUY_CE and BUY_PE both BUY the premium: ceil_tick(fair*(1+spread) + ticks*tick)
        pricer = FakePricer(fair=312.34)
        book = make_book(premium_at=pricer)
        alert = make_alert("INDEX", direction)
        book.on_alert(alert, ist(THU, 10, 5, 4))
        bars = make_bars(ist(THU, 10, 5), [45200.0, 45224.65])

        book.step(ist(THU, 10, 8), series(BANKNIFTY=bars))

        oracle = round_to_tick(
            312.34 * (1 + BT.option_spread_pct) + BT.option_slippage_ticks * 0.05, 0.05, "up"
        )
        assert to_paise(oracle) == 31435  # 312.34 x 1.006 + 0.10 = 314.31404 -> 314.35
        row = the_row(engine)
        assert (row["status"], row["entry_paise"], row["qty"]) == ("OPEN", 31435, 30)
        option, underlying, ts = pricer.calls[0]
        assert option == alert["economics"]["option"]  # strike, expiry and IV frozen at the alert
        assert (underlying, ts) == (45224.65, ist(THU, 10, 6))


# ── AC3: NO_FILL, never an invented price ───────────────────────────────────────


class TestNoFill:
    @pytest.mark.parametrize(
        ("kind", "direction", "gap_open", "key"),
        [
            ("STOCK", "long", 994.00, "RELIANCE"),  # stop 995.00
            ("STOCK", "short", 1006.00, "RELIANCE"),  # stop 1005.00
            ("INDEX", "long", 45100.00, "BANKNIFTY"),  # underlying stop 45110.35
        ],
    )
    def test_fill_bar_opening_beyond_the_stop_is_gap_through_stop(
        self, make_book, engine, kind, direction, gap_open, key
    ):
        book = make_book()
        book.on_alert(make_alert(kind, direction), ist(THU, 10, 5, 4))
        bars = make_bars(ist(THU, 10, 5), [gap_open, gap_open])

        events = book.step(ist(THU, 10, 8), series(**{key: bars}))

        row = the_row(engine)
        assert (row["status"], row["close_reason"]) == ("NO_FILL", "GAP_THROUGH_STOP")
        assert row["entry_paise"] is None and row["exit_paise"] is None
        assert {k: row[k] for k in ZERO_MONEY} == ZERO_MONEY
        assert [(e.kind, e.detail["reason"]) for e in events] == [("NO_FILL", "gap_through_stop")]
        assert events[0].detail["bar_open_paise"] == to_paise(gap_open)
        assert event_kinds(engine) == ["OPENED", "NO_FILL"]
        assert book.open_trades() == []
        assert book.status()["no_fill"] == 1 and book.status()["filled"] == 0

    def test_data_past_the_deadline_without_an_eligible_bar_is_no_fill_expired(
        self, make_book, engine
    ):
        book = make_book()
        book.on_alert(make_alert(), ist(THU, 10, 5, 4))  # expires 10:15:03
        bars = make_bars(ist(THU, 10, 16), [1000.0] * 2)  # the feed resumes after the deadline

        events = book.step(ist(THU, 10, 20), series(RELIANCE=bars))

        row = the_row(engine)
        assert (row["status"], row["close_reason"]) == ("NO_FILL", "NO_FILL")
        assert row["entry_paise"] is None and row["exit_paise"] is None
        assert {k: row[k] for k in ZERO_MONEY} == ZERO_MONEY
        assert [(e.kind, e.detail["reason"]) for e in events] == [("NO_FILL", "expired")]
        assert book.status()["no_fill"] == 1 and book.status()["filled"] == 0

    def test_waiting_for_late_data_invents_no_price(self, make_book, engine):
        book = make_book()
        book.on_alert(make_alert(), ist(THU, 10, 5, 4))
        stale = make_bars(ist(THU, 10, 0), [1000.0] * 6)  # last bar opens 10:05

        assert book.step(ist(THU, 10, 30), series(RELIANCE=stale)) == []  # wall time is no fill
        row = the_row(engine)
        assert (row["status"], row["entry_paise"]) == ("PENDING_FILL", None)
        for empty in (None, stale.iloc[0:0]):
            assert book.step(ist(THU, 10, 30), series(RELIANCE=empty)) == []
        assert the_row(engine)["status"] == "PENDING_FILL"

        late = make_bars(ist(THU, 10, 0), [1000.0] * 7)  # the 10:06 bar finally arrives
        book.step(ist(THU, 10, 31), series(RELIANCE=late))
        assert (the_row(engine)["fill_bar_ts"], the_row(engine)["entry_paise"]) == (
            ist(THU, 10, 6),
            100030,
        )

    def test_friday_1529_alert_is_no_fill_never_a_monday_fill(self, make_book, engine):
        # a TTL long enough to reach Monday: only the same-session rule can refuse the fill
        book = make_book()
        alert = make_alert(created=ist(FRI, 15, 29, 10), ttl_min=3 * 24 * 60)
        book.on_alert(alert, ist(FRI, 15, 29, 11))
        bars = sessions(1, (ist(FRI, 15, 25), [1000.0] * 5), (ist(MON, 9, 15), [1000.0] * 6))

        events = book.step(ist(MON, 9, 25), series(RELIANCE=bars))

        row = the_row(engine)
        assert (row["status"], row["close_reason"]) == ("NO_FILL", "NO_FILL")
        assert row["entry_paise"] is None and row["fill_bar_ts"] is None
        assert [(e.kind, e.detail["reason"]) for e in events] == [("NO_FILL", "session_closed")]


# ── AC7 (step half): disabled stops new trades, not the management of open ones ─


def test_a_disabled_book_still_fills_trades_opened_while_enabled(make_book, engine):
    make_book().on_alert(make_alert(), ist(THU, 10, 5, 4))
    disabled = make_book(enabled=False)

    events = disabled.step(
        ist(THU, 10, 8), series(RELIANCE=make_bars(ist(THU, 10, 5), [1000.0] * 2))
    )

    assert [e.kind for e in events] == ["FILLED"]
    assert (the_row(engine)["status"], the_row(engine)["entry_paise"]) == ("OPEN", 100030)
    assert disabled.status()["filled"] == 1
