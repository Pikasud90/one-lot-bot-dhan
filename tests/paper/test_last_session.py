"""PaperBook.day_totals / last_session (US-032): the most recent session's results.

    day_totals(day)    the CLOSED trades closed on IST date `day` (today_totals generalised:
                       today_totals(now) == day_totals(now's IST date))
    last_session(now)  the most recent IST date BEFORE now's IST date on which a CLOSED trade
                       closed; only when no CLOSED trade closed before today, the most recent
                       such date with a NO_FILL; else None. A trade closes at its exit_ts, or at
                       updated_at when it has none (a NO_FILL never has one): the same rule as
                       the API's closed_at, so the panel and the server agree on the day.

Review round 1 BLOCKER (the owner's Monday): Sunday's reconcile stamped a Friday NO_FILL with
Sunday's time, so "any close" made Sunday the last session on Monday (0 closes) and hid
Friday's 33 closes again. A no-fill alone never outranks a day with closes.

Why (found at the owner's first launch, a Sunday): the book reconciled 68 Thursday/Friday
alerts and closed 67 of them on their own bar times, so "today" showed none of it. Both
methods read the database only: never the book lock, never the Desk.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Any

import pytest

from onelot.store.schema import paper_trades

from .conftest import FRI, MON, THU, ist, make_alert, make_bars
from .test_book_reads import WED, ZERO_TOTALS, closed_trade, open_alert, write_closed

SUN = dt.date(2026, 11, 1)
UTC = dt.timezone.utc


def no_fill(book, engine, alert_id: str, created: dt.datetime, closed: dt.datetime) -> None:
    """A NO_FILL the way the book writes it: no exit time, updated_at = when it was closed."""
    open_alert(book, alert_id, created)
    with engine.begin() as conn:
        conn.execute(
            paper_trades.update()
            .where(paper_trades.c.alert_id == alert_id)
            .values(
                status="NO_FILL",
                close_reason="NO_FILL",
                gross_paise=0,
                costs_paise=0,
                net_paise=0,
                updated_at=closed,
            )
        )


class TestDayTotals:
    def test_today_totals_is_day_totals_of_todays_ist_date(self, make_book, engine):
        book = make_book()
        closed_trade(book, engine, "w", ist(WED, 14, 0), gross_paise=5000)
        closed_trade(book, engine, "t1", ist(THU, 11, 0), gross_paise=970)
        closed_trade(book, engine, "t2", ist(THU, 12, 0), gross_paise=-1205, net_paise=-1300)

        assert book.day_totals(THU) == book.today_totals(ist(THU, 15, 0))
        assert book.day_totals(THU) == {
            "closed": 2,
            "wins": 1,
            "losses": 1,
            "gross_paise": -235,
            "net_paise": None,  # t1 has no net yet
        }
        assert book.day_totals(WED) == {**ZERO_TOTALS, "closed": 1, "wins": 1, "gross_paise": 5000}

    def test_a_day_without_closes_is_all_zero_with_no_net(self, make_book):
        assert make_book().day_totals(FRI) == ZERO_TOTALS

    def test_no_fill_trades_are_not_counted(self, make_book, engine):
        book = make_book()
        no_fill(book, engine, "n", ist(THU, 10, 0), ist(THU, 10, 20))
        assert book.day_totals(THU) == ZERO_TOTALS

    @pytest.mark.parametrize("bad", [ist(THU, 0, 0), "2026-10-29", None, 20261029])
    def test_only_a_plain_date_is_a_day(self, make_book, bad):
        # A datetime would carry a zone of its own: the caller says which IST date it means.
        with pytest.raises(TypeError, match="day"):
            make_book().day_totals(bad)


class TestLastSession:
    def test_an_empty_book_has_none(self, make_book):
        assert make_book().last_session(ist(FRI, 12, 0)) is None

    def test_open_trades_alone_are_no_session(self, make_book, engine):
        book = make_book()
        open_alert(book, "w", ist(THU, 10, 0))
        with engine.begin() as conn:
            conn.execute(paper_trades.update().values(status="OPEN", entry_paise=100030))
        assert book.last_session(ist(FRI, 12, 0)) is None

    def test_the_most_recent_earlier_day_with_a_close(self, make_book, engine):
        book = make_book()
        closed_trade(book, engine, "w", ist(WED, 14, 0), gross_paise=100)
        closed_trade(book, engine, "t", ist(THU, 14, 0), gross_paise=100)
        assert book.last_session(ist(FRI, 12, 0)) == THU

    def test_today_is_never_the_last_session(self, make_book, engine):
        book = make_book()
        closed_trade(book, engine, "w", ist(WED, 14, 0), gross_paise=100)
        closed_trade(book, engine, "today", ist(FRI, 11, 0), gross_paise=100)
        assert book.last_session(ist(FRI, 12, 0)) == WED

    def test_a_no_fill_alone_makes_a_session(self, make_book, engine):
        book = make_book()
        no_fill(book, engine, "n", ist(WED, 10, 0), ist(WED, 10, 20))
        assert book.last_session(ist(FRI, 12, 0)) == WED

    def test_the_owners_monday(self, make_book, engine):
        """The day after: Sunday's stamped NO_FILL must not make Sunday the last session."""
        book = make_book()
        closed_trade(book, engine, "thu", ist(THU, 14, 0), gross_paise=-500, reason="STOP")
        closed_trade(book, engine, "fri1", ist(FRI, 15, 20), gross_paise=1200, reason="SESSION_END")
        closed_trade(book, engine, "fri2", ist(FRI, 13, 5), gross_paise=-300, reason="TIME_STOP")
        no_fill(book, engine, "nf", ist(FRI, 15, 10), ist(SUN, 9, 1))
        now = ist(MON, 9, 20)

        assert book.today_totals(now)["closed"] == 0
        assert book.last_session(now) == FRI
        assert book.day_totals(FRI) == {
            "closed": 2,
            "wins": 1,
            "losses": 1,
            "gross_paise": 900,
            "net_paise": None,
        }

    def test_a_later_no_fill_only_day_never_outranks_a_day_with_closes(self, make_book, engine):
        book = make_book()
        closed_trade(book, engine, "thu", ist(THU, 14, 0), gross_paise=100)
        no_fill(book, engine, "nf", ist(FRI, 10, 0), ist(FRI, 10, 11))
        assert book.last_session(ist(MON, 9, 20)) == THU

    def test_with_no_close_before_today_the_latest_no_fill_day_is_the_session(
        self, make_book, engine
    ):
        book = make_book()
        no_fill(book, engine, "w", ist(WED, 10, 0), ist(WED, 10, 11))
        no_fill(book, engine, "t", ist(THU, 10, 0), ist(THU, 10, 11))
        closed_trade(book, engine, "today", ist(FRI, 11, 0), gross_paise=100)
        assert book.last_session(ist(FRI, 12, 0)) == THU

    def test_a_close_today_does_not_count_against_an_earlier_no_fill_day(self, make_book, engine):
        book = make_book()
        no_fill(book, engine, "w", ist(WED, 10, 0), ist(WED, 10, 11))
        closed_trade(book, engine, "today", ist(FRI, 11, 0), gross_paise=100)
        assert book.last_session(ist(FRI, 12, 0)) == WED

    def test_the_owners_sunday(self, make_book, engine):
        """Thursday and Friday alerts booked at Sunday's start: the closes carry their own
        bar times, and the one NO_FILL is stamped Sunday (when the book wrote it)."""
        book = make_book()
        closed_trade(book, engine, "thu", ist(THU, 14, 0), gross_paise=-500, reason="STOP")
        closed_trade(book, engine, "fri1", ist(FRI, 15, 20), gross_paise=1200, reason="SESSION_END")
        closed_trade(book, engine, "fri2", ist(FRI, 13, 5), gross_paise=-300, reason="TIME_STOP")
        no_fill(book, engine, "nf", ist(FRI, 15, 10), ist(SUN, 9, 1))
        now = ist(SUN, 9, 5)

        assert book.today_totals(now)["closed"] == 0
        assert book.last_session(now) == FRI
        assert book.day_totals(FRI) == {
            "closed": 2,
            "wins": 1,
            "losses": 1,
            "gross_paise": 900,
            "net_paise": None,
        }

    @pytest.mark.parametrize(
        ("closed_at", "expected"),
        [
            (ist(THU, 23, 59, 59), THU),  # the last second before today: yesterday
            (ist(FRI, 0, 0), None),  # IST midnight today (18:30 UTC on THU): today
            (ist(FRI, 0, 10), None),
        ],
        ids=["last-second-yesterday", "ist-midnight", "just-after-midnight"],
    )
    def test_the_day_is_the_ist_calendar_day(self, make_book, engine, closed_at, expected):
        book = make_book()
        open_alert(book, "t", ist(THU, 10, 5))
        write_closed(engine, "t", exit_ts=closed_at, gross_paise=100)
        assert book.last_session(ist(FRI, 12, 0)) == expected

    def test_a_close_without_an_exit_time_counts_on_its_update_day(self, make_book, engine):
        book = make_book()
        closed_trade(book, engine, "t", None, gross_paise=100, updated_at=ist(WED, 16, 0))
        assert book.last_session(ist(FRI, 12, 0)) == WED

    def test_now_in_another_zone_is_read_on_the_ist_calendar(self, make_book, engine):
        book = make_book()
        closed_trade(book, engine, "thu", ist(THU, 14, 0), gross_paise=100)
        late_thu_utc = dt.datetime(2026, 10, 29, 20, 0, tzinfo=UTC)  # 01:30 IST on FRI

        assert book.last_session(late_thu_utc) == THU
        assert book.last_session(late_thu_utc.replace(hour=6)) is None  # 11:30 IST THU

    def test_a_naive_now_is_rejected(self, make_book):
        with pytest.raises(ValueError, match="timezone-aware"):
            make_book().last_session(dt.datetime(2026, 10, 30, 12, 0))

    def test_a_corrupt_close_time_is_skipped_and_reported_once(self, make_book, engine, caplog):
        book = make_book()
        closed_trade(book, engine, "good", ist(WED, 14, 0), gross_paise=100)
        closed_trade(book, engine, "bad", ist(THU, 14, 0), gross_paise=100)
        with engine.begin() as conn:  # sorts between WED's close and today, but is no time
            conn.exec_driver_sql(
                "UPDATE paper_trades SET exit_ts = '2026-10-29 12:99:99' WHERE alert_id = 'bad'"
            )

        with caplog.at_level(logging.DEBUG, logger="onelot.paper.book"):
            first = book.last_session(ist(FRI, 12, 0))
            second = book.last_session(ist(FRI, 12, 0))

        assert first == second == WED
        warned = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warned) == 1 and "close time" in warned[0].getMessage()

    def test_a_real_no_fill_from_the_book_counts_on_the_day_it_was_written(self, make_book):
        book = make_book()
        book.on_alert(make_alert(alert_id="gap"), ist(THU, 10, 5, 4))  # stop 995.00
        gap = make_bars(ist(THU, 10, 5), [990.0, 990.0])  # opens through the stop
        book.step(ist(THU, 10, 8), lambda key: gap if key == "RELIANCE" else None)
        assert book.last_session(ist(FRI, 9, 0)) == THU

    def test_reads_never_wait_for_the_book_lock(self, make_book, engine):
        book = make_book()
        closed_trade(book, engine, "t", ist(THU, 14, 0), gross_paise=100)
        held, release = threading.Event(), threading.Event()

        def hold() -> None:
            with book._lock:
                held.set()
                release.wait(10)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        assert held.wait(5)
        got: dict[str, Any] = {}

        def read() -> None:
            got["day"] = book.last_session(ist(FRI, 12, 0))
            got["totals"] = book.day_totals(THU)

        reader = threading.Thread(target=read, daemon=True)
        try:
            reader.start()
            reader.join(5)
            assert not reader.is_alive(), "last_session()/day_totals() waited for the book lock"
        finally:
            release.set()
            holder.join(5)
        assert got["day"] == THU and got["totals"]["closed"] == 1
