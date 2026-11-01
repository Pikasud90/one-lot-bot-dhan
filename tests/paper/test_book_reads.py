"""PaperBook.recent / today_totals (US-030): the read side behind the desk's Paper panel.

    recent(limit=200)  -> trade dicts, newest first (created_at, then id). Money is a rupee
                          string from integer paise (format_rupees: Decimal, exact); times
                          are ISO-8601 IST; anything not known yet is None (JSON null).
    today_totals(now)  -> the CLOSED trades closed on now's IST date: closed, wins, losses,
                          gross_paise, net_paise (None while any of them has no net yet:
                          costs are US-023). A win or loss is judged on net when it exists,
                          else on gross; a zero is neither.

Both read the database only: never the book lock (the API must not queue behind a step),
never the Desk. US-002 (closing) is built in parallel, so CLOSED rows are written here the
way it writes them: status, close_reason, exit_ts, exit/gross (and later costs/net) paise.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Any

import pytest
from sqlalchemy import Engine

from onelot.clock import IST
from onelot.store.schema import paper_trades

from .conftest import FRI, THU, ist, make_alert, make_bars

WED = dt.date(2026, 10, 28)  # the session before THU
UTC = dt.timezone.utc

TRADE_KEYS = {
    "alert_id",
    "instrument",
    "side",
    "direction",
    "kind",
    "strategy",
    "timeframe",
    "grade",
    "status",
    "close_reason",
    "qty",
    "entry_ts",
    "entry",
    "exit",
    "gross",
    "net",
    "created_at",
    "closed_at",
}
# No net is ever invented: an empty day has a zero gross but no net (contract: null until US-023).
ZERO_TOTALS = {"closed": 0, "wins": 0, "losses": 0, "gross_paise": 0, "net_paise": None}


def open_alert(book, alert_id: str, created: dt.datetime, **kw: Any) -> None:
    """Book one alert (a PENDING_FILL trade) created at `created`."""
    book.on_alert(make_alert(alert_id=alert_id, created=created, **kw), created)


def write_closed(
    engine: Engine,
    alert_id: str,
    *,
    exit_ts: dt.datetime | None,
    gross_paise: int | None,
    net_paise: int | None = None,
    exit_paise: int | None = 101000,
    reason: str = "TARGET",
    updated_at: dt.datetime | None = None,
) -> None:
    """Close a trade the way US-002 writes it (costs/net stay NULL until US-023)."""
    values: dict[str, Any] = {
        "status": "CLOSED",
        "close_reason": reason,
        "exit_ts": exit_ts,
        "exit_paise": exit_paise,
        "gross_paise": gross_paise,
        "net_paise": net_paise,
        "costs_paise": None
        if net_paise is None or gross_paise is None
        else gross_paise - net_paise,
        "updated_at": updated_at or exit_ts or ist(THU, 15, 30),
    }
    with engine.begin() as conn:
        conn.execute(
            paper_trades.update().where(paper_trades.c.alert_id == alert_id).values(**values)
        )


def closed_trade(book, engine, alert_id: str, exit_ts: dt.datetime | None, **kw: Any) -> None:
    """One trade raised 10:05 on the exit's session (THU when no exit), then closed."""
    day = exit_ts.astimezone(IST).date() if exit_ts else THU
    open_alert(book, alert_id, ist(day, 10, 5, 3))
    write_closed(engine, alert_id, exit_ts=exit_ts, **kw)


# ── recent(): what the panel lists ───────────────────────────────────────────


class TestRecentShape:
    def test_an_empty_book_lists_nothing(self, make_book):
        assert make_book().recent() == []

    def test_a_waiting_stock_trade_has_no_prices_yet(self, make_book):
        book = make_book()
        book.on_alert(make_alert("STOCK", "long"), ist(THU, 10, 5, 4))

        (view,) = book.recent()

        assert set(view) == TRADE_KEYS
        assert view == {
            "alert_id": "a1b2c3d4e5f6",
            "instrument": "RELIANCE",
            "side": "BUY",
            "direction": "long",
            "kind": "STOCK",
            "strategy": "ema_cross",
            "timeframe": "5m",
            "grade": "live",
            "status": "PENDING_FILL",
            "close_reason": None,
            "qty": 1,
            "entry_ts": None,
            "entry": None,
            "exit": None,
            "gross": None,
            "net": None,
            "created_at": "2026-10-29T10:05:03+05:30",
            "closed_at": None,
        }

    def test_a_filled_trade_shows_its_entry_as_a_rupee_string(self, make_book):
        book = make_book()
        book.on_alert(make_alert("STOCK", "long"), ist(THU, 10, 5, 4))
        bars = make_bars(ist(THU, 10, 5), [1000.00, 1000.00])
        book.step(ist(THU, 10, 8), lambda key: bars if key == "RELIANCE" else None)

        (view,) = book.recent()

        assert view["status"] == "OPEN"
        assert view["entry"] == "1000.30"  # 100030 paise, never the float 1000.3
        assert view["entry_ts"] == "2026-10-29T10:06:00+05:30"
        assert (view["exit"], view["gross"], view["net"], view["closed_at"]) == (None,) * 4

    @pytest.mark.parametrize(
        ("direction", "instrument", "side"),
        [
            ("long", "BANKNIFTY 45000 CE 2026-11-24", "BUY_CE"),
            ("short", "BANKNIFTY 45400 PE 2026-11-24", "BUY_PE"),
        ],
    )
    def test_an_index_trade_names_the_alerted_option(self, make_book, direction, instrument, side):
        book = make_book()
        book.on_alert(make_alert("INDEX", direction), ist(THU, 10, 5, 4))

        (view,) = book.recent()

        assert (view["instrument"], view["side"], view["qty"]) == (instrument, side, 30)
        assert (view["kind"], view["direction"]) == ("INDEX", direction)

    def test_a_delayed_alert_is_graded_delayed(self, make_book):
        book = make_book()
        book.on_alert(make_alert(source="yahoo", resolution="2m"), ist(THU, 10, 5, 4))
        assert book.recent()[0]["grade"] == "delayed"

    def test_a_closed_trade_shows_exit_and_gross_with_a_null_net(self, make_book, engine):
        book = make_book()
        closed_trade(book, engine, "c1", ist(THU, 11, 30), gross_paise=970)

        (view,) = book.recent()

        assert view["status"] == "CLOSED" and view["close_reason"] == "TARGET"
        assert (view["exit"], view["gross"], view["net"]) == ("1010.00", "9.70", None)
        assert view["closed_at"] == "2026-10-29T11:30:00+05:30"

    def test_a_closed_trade_with_costs_shows_a_negative_net(self, make_book, engine):
        book = make_book()
        closed_trade(book, engine, "c1", ist(THU, 11, 30), gross_paise=970, net_paise=-1205)
        view = book.recent()[0]
        assert (view["gross"], view["net"]) == ("9.70", "-12.05")

    def test_a_closed_trade_without_an_exit_time_is_closed_at_its_update(self, make_book, engine):
        book = make_book()
        closed_trade(book, engine, "c1", None, gross_paise=970, updated_at=ist(THU, 12, 1))
        assert book.recent()[0]["closed_at"] == "2026-10-29T12:01:00+05:30"

    def test_a_no_fill_is_closed_at_the_step_that_closed_it_with_zero_money(self, make_book):
        book = make_book()
        book.on_alert(make_alert("STOCK", "long"), ist(THU, 10, 5, 4))  # stop 995.00
        gap = make_bars(ist(THU, 10, 5), [990.0, 990.0])  # 10:06 opens through the stop
        book.step(ist(THU, 10, 8), lambda key: gap if key == "RELIANCE" else None)

        (view,) = book.recent()

        assert (view["status"], view["close_reason"]) == ("NO_FILL", "GAP_THROUGH_STOP")
        assert (view["entry"], view["exit"], view["gross"], view["net"]) == (
            None,
            None,
            "0.00",
            "0.00",
        )
        assert view["closed_at"] == "2026-10-29T10:08:00+05:30"


class TestRecentOrderAndLimit:
    def test_newest_first_by_created_at(self, make_book):
        book = make_book()
        for alert_id, minute in (("t1", 1), ("t3", 3), ("t2", 2)):
            open_alert(book, alert_id, ist(THU, 10, minute))

        assert [v["alert_id"] for v in book.recent()] == ["t3", "t2", "t1"]
        assert [v["alert_id"] for v in book.recent(2)] == ["t3", "t2"]

    def test_a_tie_on_created_at_goes_to_the_later_booking(self, make_book):
        book = make_book()
        for alert_id in ("first", "second"):
            open_alert(book, alert_id, ist(THU, 10, 5))
        assert [v["alert_id"] for v in book.recent()] == ["second", "first"]

    def test_the_default_limit_is_200(self, make_book):
        book = make_book()
        start = ist(THU, 9, 15)
        for i in range(203):
            open_alert(book, f"n{i:03d}", start + dt.timedelta(seconds=i))

        views = book.recent()

        assert len(views) == 200
        assert (views[0]["alert_id"], views[-1]["alert_id"]) == ("n202", "n003")

    @pytest.mark.parametrize("bad", [0, -1, True, 2.0, "5", None])
    def test_limit_must_be_a_positive_int(self, make_book, bad):
        with pytest.raises(ValueError, match="limit"):
            make_book().recent(bad)


class TestRecentKeepsManagedTrades:
    """Manager decision 2026-11-01T21:20: an old open trade never drops off the panel. The
    limit is filled with the managed trades (PENDING_FILL, OPEN) first, then the newest final
    ones; the list is still newest first and never longer than `limit`."""

    @pytest.mark.parametrize("status", ["PENDING_FILL", "OPEN"])
    def test_an_old_managed_trade_outlives_newer_final_ones(self, make_book, engine, status):
        book = make_book()
        open_alert(book, "old", ist(THU, 9, 20))
        with engine.begin() as conn:
            conn.execute(
                paper_trades.update().where(paper_trades.c.alert_id == "old").values(status=status)
            )
        for alert_id in ("f1", "f2", "f3"):  # raised 10:05:03, closed 11:00: all newer
            closed_trade(book, engine, alert_id, ist(THU, 11, 0), gross_paise=100)

        assert [v["alert_id"] for v in book.recent(2)] == ["f3", "old"]

    def test_more_managed_trades_than_the_limit_lists_the_newest_managed(self, make_book, engine):
        book = make_book()
        for alert_id, minute in (("w1", 20), ("w2", 21), ("w3", 22)):
            open_alert(book, alert_id, ist(THU, 9, minute))
        closed_trade(book, engine, "final", ist(THU, 11, 0), gross_paise=100)  # the newest

        assert [v["alert_id"] for v in book.recent(2)] == ["w3", "w2"]


class TestRecentRobustness:
    def test_an_unreadable_row_is_skipped_and_logged_never_fatal(self, make_book, engine, caplog):
        book = make_book()
        for alert_id, minute in (("good1", 1), ("bad", 2), ("good2", 3)):
            open_alert(book, alert_id, ist(THU, 10, minute))
        with engine.begin() as conn:
            conn.execute(
                paper_trades.update().where(paper_trades.c.alert_id == "bad").values(status="BOGUS")
            )

        with caplog.at_level(logging.WARNING, logger="onelot.paper.book"):
            views = book.recent()

        assert [v["alert_id"] for v in views] == ["good2", "good1"]
        assert "BOGUS" in caplog.text

    def test_a_corrupt_plan_hides_only_that_row(self, make_book, engine, caplog):
        book = make_book()
        open_alert(book, "good", ist(THU, 10, 1))
        open_alert(book, "bad", ist(THU, 10, 2))
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE paper_trades SET plan = '{not json' WHERE alert_id = 'bad'"
            )

        with caplog.at_level(logging.WARNING, logger="onelot.paper.book"):
            views = book.recent()

        assert [v["alert_id"] for v in views] == ["good"]
        assert "not shown" in caplog.text

    @pytest.mark.parametrize("column", ["created_at", "entry_ts", "updated_at"])
    def test_a_corrupt_time_hides_only_that_row(self, make_book, engine, caplog, column):
        # Decoded per row: the driver's own datetime parsing must not fail the whole fetch.
        book = make_book()
        open_alert(book, "good", ist(THU, 10, 1))
        open_alert(book, "bad", ist(THU, 10, 2))
        with engine.begin() as conn:
            conn.exec_driver_sql(
                f"UPDATE paper_trades SET {column} = 'garbage' WHERE alert_id = 'bad'"
            )

        with caplog.at_level(logging.WARNING, logger="onelot.paper.book"):
            views = book.recent()

        assert [v["alert_id"] for v in views] == ["good"]
        assert "not shown" in caplog.text

    @pytest.mark.parametrize("value", ["12.5", "'abc'"])
    def test_money_that_is_not_integer_paise_hides_the_row_never_truncated(
        self, make_book, engine, caplog, value
    ):
        book = make_book()
        open_alert(book, "good", ist(THU, 10, 1))
        open_alert(book, "bad", ist(THU, 10, 2))
        with engine.begin() as conn:
            conn.exec_driver_sql(
                f"UPDATE paper_trades SET entry_paise = {value} WHERE alert_id = 'bad'"
            )

        with caplog.at_level(logging.WARNING, logger="onelot.paper.book"):
            views = book.recent()

        assert [v["alert_id"] for v in views] == ["good"]  # never "0.12" from int(12.5)
        assert "entry_paise" in caplog.text

    def test_an_index_plan_without_an_option_falls_back_to_the_label(self, make_book, engine):
        book = make_book()
        book.on_alert(make_alert("INDEX", "long"), ist(THU, 10, 5, 4))
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE paper_trades SET plan = json_set(plan, '$.alert.economics.option', "
                "json('{}'))"
            )
        assert book.recent()[0]["instrument"] == "BANKNIFTY"

    def test_a_disabled_book_still_reads_its_trades(self, make_book):
        open_alert(make_book(), "t1", ist(THU, 10, 1))
        assert [v["alert_id"] for v in make_book(enabled=False).recent()] == ["t1"]

    def test_reads_never_wait_for_the_book_lock(self, make_book, engine):
        book = make_book()
        closed_trade(book, engine, "c1", ist(THU, 11, 30), gross_paise=970)
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
            got["recent"] = book.recent()
            got["totals"] = book.today_totals(ist(THU, 15, 0))

        reader = threading.Thread(target=read, daemon=True)
        try:
            reader.start()
            reader.join(5)
            assert not reader.is_alive(), "recent()/today_totals() waited for the book lock"
        finally:
            release.set()
            holder.join(5)
        assert got["recent"][0]["alert_id"] == "c1" and got["totals"]["closed"] == 1


# ── today_totals(now): the panel's day line ─────────────────────────────────


class TestTodayTotals:
    NOW = ist(THU, 15, 0)

    def test_no_closed_trades_is_zero_with_no_net(self, make_book):
        assert make_book().today_totals(self.NOW) == ZERO_TOTALS

    def test_wins_and_losses_go_by_gross_while_no_net_exists(self, make_book, engine):
        book = make_book()
        closed_trade(book, engine, "w", ist(THU, 11, 0), gross_paise=970)
        closed_trade(book, engine, "l", ist(THU, 12, 0), gross_paise=-1205)

        assert book.today_totals(self.NOW) == {
            "closed": 2,
            "wins": 1,
            "losses": 1,
            "gross_paise": -235,
            "net_paise": None,  # costs are US-023: no net is ever invented
        }

    def test_net_decides_the_outcome_once_costs_exist(self, make_book, engine):
        book = make_book()
        closed_trade(book, engine, "a", ist(THU, 11, 0), gross_paise=970, net_paise=-30)
        closed_trade(book, engine, "b", ist(THU, 12, 0), gross_paise=2000, net_paise=1500)

        assert book.today_totals(self.NOW) == {
            "closed": 2,
            "wins": 1,
            "losses": 1,  # gross +9.70 but net -0.30 after costs: a loss
            "gross_paise": 2970,
            "net_paise": 1470,
        }

    def test_one_trade_without_a_net_makes_the_day_net_unknown(self, make_book, engine):
        book = make_book()
        closed_trade(book, engine, "a", ist(THU, 11, 0), gross_paise=970, net_paise=870)
        closed_trade(book, engine, "b", ist(THU, 12, 0), gross_paise=-500)

        totals = book.today_totals(self.NOW)

        assert totals["net_paise"] is None and totals["gross_paise"] == 470
        assert (totals["wins"], totals["losses"]) == (1, 1)

    def test_a_scratch_is_neither_a_win_nor_a_loss(self, make_book, engine):
        book = make_book()
        closed_trade(book, engine, "s", ist(THU, 11, 0), gross_paise=0)
        assert book.today_totals(self.NOW) == {**ZERO_TOTALS, "closed": 1}

    def test_a_closed_trade_without_gross_makes_the_day_gross_unknown(self, make_book, engine):
        book = make_book()
        closed_trade(book, engine, "a", ist(THU, 11, 0), gross_paise=970)
        closed_trade(book, engine, "b", ist(THU, 12, 0), gross_paise=None, exit_paise=None)

        totals = book.today_totals(self.NOW)

        assert (totals["closed"], totals["gross_paise"], totals["net_paise"]) == (2, None, None)
        assert (totals["wins"], totals["losses"]) == (1, 0)  # b has no outcome to judge

    def test_only_trades_closed_today_count(self, make_book, engine):
        book = make_book()
        closed_trade(book, engine, "yesterday", ist(WED, 14, 0), gross_paise=5000)
        closed_trade(book, engine, "today", ist(THU, 11, 0), gross_paise=970)
        open_alert(book, "waiting", ist(THU, 10, 30))  # PENDING_FILL: not closed
        book.on_alert(make_alert(alert_id="gap"), ist(THU, 10, 5, 4))
        gap = make_bars(ist(THU, 10, 5), [990.0, 990.0])
        book.step(ist(THU, 10, 8), lambda key: gap if key == "RELIANCE" else None)  # NO_FILL

        totals = book.today_totals(self.NOW)

        assert (totals["closed"], totals["gross_paise"]) == (1, 970)

    @pytest.mark.parametrize(
        ("exit_ts", "counted"),
        [
            (ist(THU, 0, 0), True),  # IST midnight = 18:30 UTC on WED
            (ist(THU, 23, 59, 59), True),
            (ist(FRI, 0, 0), False),
            (ist(WED, 23, 59, 59), False),  # 18:29:59 UTC on WED, same UTC date as above
        ],
        ids=["ist-midnight", "last-second", "next-midnight", "previous-day"],
    )
    def test_the_day_is_the_ist_calendar_day(self, make_book, engine, exit_ts, counted):
        book = make_book()
        open_alert(book, "t", ist(WED, 10, 5))
        write_closed(engine, "t", exit_ts=exit_ts, gross_paise=970)
        assert book.today_totals(self.NOW)["closed"] == int(counted)

    def test_now_in_another_zone_is_read_on_the_ist_calendar(self, make_book, engine):
        book = make_book()
        closed_trade(book, engine, "thu", ist(THU, 11, 0), gross_paise=970)
        late_thu_utc = dt.datetime(2026, 10, 29, 20, 0, tzinfo=UTC)  # 01:30 IST on FRI

        assert book.today_totals(late_thu_utc)["closed"] == 0
        assert book.today_totals(late_thu_utc.replace(hour=12))["closed"] == 1

    def test_a_closed_trade_without_an_exit_time_counts_on_its_update_day(self, make_book, engine):
        book = make_book()
        closed_trade(book, engine, "t", None, gross_paise=970, updated_at=ist(THU, 12, 0))
        assert book.today_totals(self.NOW)["closed"] == 1
        assert book.today_totals(ist(FRI, 12, 0))["closed"] == 0

    def test_a_naive_now_is_rejected(self, make_book):
        with pytest.raises(ValueError, match="timezone-aware"):
            make_book().today_totals(dt.datetime(2026, 10, 29, 15, 0))

    @pytest.mark.parametrize("value", ["12.5", "'abc'"])
    def test_an_amount_that_is_not_integer_paise_makes_that_sum_unknown(
        self, make_book, engine, caplog, value
    ):
        # Unknown, like a missing amount: never truncated, never a crash of the API.
        book = make_book()
        closed_trade(book, engine, "good", ist(THU, 11, 0), gross_paise=970)
        closed_trade(book, engine, "bad", ist(THU, 12, 0), gross_paise=100)
        with engine.begin() as conn:
            conn.exec_driver_sql(
                f"UPDATE paper_trades SET gross_paise = {value} WHERE alert_id = 'bad'"
            )

        with caplog.at_level(logging.WARNING, logger="onelot.paper.book"):
            totals = book.today_totals(self.NOW)

        assert totals == {
            "closed": 2,
            "wins": 1,
            "losses": 0,
            "gross_paise": None,
            "net_paise": None,
        }
        assert "gross_paise" in caplog.text


# ── a corrupt row is reported once, not on every poll (US-030 review round 1, MINOR) ──


def warnings_about(caplog, trade_id: int) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "onelot.paper.book"
        and r.levelno == logging.WARNING
        and f"trade {trade_id} " in r.getMessage()
    ]


def debugs_about(caplog, trade_id: int) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == "onelot.paper.book"
        and r.levelno == logging.DEBUG
        and f"trade {trade_id} " in r.getMessage()
    ]


def corrupt(engine: Engine, alert_id: str, assignment: str) -> int:
    """Write a corrupt cell straight into the table; return that trade's id."""
    with engine.begin() as conn:
        conn.exec_driver_sql(f"UPDATE paper_trades SET {assignment} WHERE alert_id = '{alert_id}'")
        row = conn.exec_driver_sql(
            f"SELECT id FROM paper_trades WHERE alert_id = '{alert_id}'"
        ).one()
    return int(row[0])


class TestCorruptRowsWarnOnce:
    """The panel polls every few seconds (about 720 reads an hour): a corrupt row is a
    WARNING once per (trade id, reason) for each reader, then DEBUG. The remembered set is
    bounded, and keeping it never takes the book lock."""

    def test_recent_warns_once_then_logs_at_debug(self, make_book, engine, caplog):
        book = make_book()
        open_alert(book, "good", ist(THU, 10, 1))
        open_alert(book, "bad", ist(THU, 10, 2))
        bad = corrupt(engine, "bad", "status = 'BOGUS'")

        with caplog.at_level(logging.DEBUG, logger="onelot.paper.book"):
            first, second = book.recent(), book.recent()

        assert [v["alert_id"] for v in first] == [v["alert_id"] for v in second] == ["good"]
        assert len(warnings_about(caplog, bad)) == 1
        assert "BOGUS" in warnings_about(caplog, bad)[0].getMessage()
        assert len(debugs_about(caplog, bad)) == 1

    def test_today_totals_warns_once_then_logs_at_debug(self, make_book, engine, caplog):
        book = make_book()
        closed_trade(book, engine, "bad", ist(THU, 12, 0), gross_paise=100)
        bad = corrupt(engine, "bad", "gross_paise = 'abc'")

        with caplog.at_level(logging.DEBUG, logger="onelot.paper.book"):
            first = book.today_totals(TestTodayTotals.NOW)
            second = book.today_totals(TestTodayTotals.NOW)

        assert first == second and first["gross_paise"] is None
        assert len(warnings_about(caplog, bad)) == 1
        assert "gross_paise" in warnings_about(caplog, bad)[0].getMessage()
        assert len(debugs_about(caplog, bad)) == 1

    def test_a_new_reason_for_the_same_trade_warns_again(self, make_book, engine, caplog):
        book = make_book()
        open_alert(book, "bad", ist(THU, 10, 2))
        bad = corrupt(engine, "bad", "status = 'BOGUS'")
        with caplog.at_level(logging.DEBUG, logger="onelot.paper.book"):
            book.recent()
            corrupt(engine, "bad", "status = 'OPEN', entry_paise = 12.5")
            book.recent()
            book.recent()

        messages = [r.getMessage() for r in warnings_about(caplog, bad)]
        assert len(messages) == 2
        assert "BOGUS" in messages[0] and "entry_paise" in messages[1]

    def test_each_book_reports_for_itself(self, make_book, engine, caplog):
        book = make_book()
        open_alert(book, "bad", ist(THU, 10, 2))
        bad = corrupt(engine, "bad", "status = 'BOGUS'")
        with caplog.at_level(logging.WARNING, logger="onelot.paper.book"):
            book.recent()
            make_book().recent()  # a restart: the new book has not said it yet
        assert len(warnings_about(caplog, bad)) == 2

    def test_the_remembered_reasons_are_bounded(self, make_book, engine, caplog, monkeypatch):
        import onelot.paper.book as book_module

        monkeypatch.setattr(book_module, "READ_WARN_CAP", 2)
        book = make_book()
        ids = []
        for alert_id, minute in (("b1", 1), ("b2", 2), ("b3", 3)):
            open_alert(book, alert_id, ist(THU, 10, minute))
            ids.append(corrupt(engine, alert_id, "status = 'BOGUS'"))

        with caplog.at_level(logging.WARNING, logger="onelot.paper.book"):
            book.recent()
            book.recent()

        assert len(book._read_warned) == 2  # never more than the cap
        # Three reasons through a set of two: each read evicts the oldest, so a forgotten
        # one is said again. Bounded memory beats a perfect dedupe; nothing is ever lost.
        assert all(warnings_about(caplog, i) for i in ids)

    def test_reporting_a_corrupt_row_never_waits_for_the_book_lock(self, make_book, engine):
        book = make_book()
        closed_trade(book, engine, "bad", ist(THU, 11, 30), gross_paise=970)
        corrupt(engine, "bad", "gross_paise = 'abc'")
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
            got["recent"] = book.recent()
            got["totals"] = book.today_totals(TestTodayTotals.NOW)

        reader = threading.Thread(target=read, daemon=True)
        try:
            reader.start()
            reader.join(5)
            assert not reader.is_alive(), "reporting a corrupt row waited for the book lock"
        finally:
            release.set()
            holder.join(5)
        assert got["recent"] == [] and got["totals"]["gross_paise"] is None
