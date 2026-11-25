"""US-105: a paper trade on a contract that was not tradeable is marked and left out.

    void_reason(alert)        the plain reason, or None (one rule, from the alert's own facts)
    paper_void                one mark per trade, written at open and by a pass at start
    ledger / day totals       leave the marked trades out and say how many they left out
    OPEN void trade           managed and closed as usual, never counted

The five recorded contracts are the ones the desk alerted on 23 and 25 Nov 2026.
"""

from __future__ import annotations

import copy
import datetime as dt
from typing import Any

import pytest
from sqlalchemy import delete, select

from onelot.store.schema import paper_events, paper_trades, paper_void
from onelot.void import LEFT_OUT_NOTE, VOID_REASON, void_reason

from .conftest import THU, ist, make_alert, table_rows
from .test_book_manage import at, bars, feed
from .test_book_reads import WED, ZERO_TOTALS, closed_trade, write_closed

# the recorded options, as stored in economics.option
RECORDED: dict[str, dict[str, Any]] = {
    "f6a7b9 NIFTY": {"bid": 4962.0, "ask": 4962.0, "premium": 4962.0, "oi": 0.0, "iv": 0.851},
    "950611 FINNIFTY": {"bid": 5696.6, "ask": 5696.6, "premium": 5696.6, "oi": 0.0, "iv": 0.87},
    "551e7c MIDCPNIFTY": {
        "bid": 1313.45,
        "ask": 1678.5,
        "premium": 1313.45,
        "oi": 0.0,
        "iv": 0.625,
    },
    "BANKNIFTY 65700 PE": {"bid": 100.0, "ask": 101.0, "premium": 100.5, "oi": 1050.0, "iv": 0.2},
    "SENSEX 83400 CE": {"bid": 90.0, "ask": 91.0, "premium": 90.5, "oi": 540.0, "iv": 0.2},
}
VOID_NAMES = ["f6a7b9 NIFTY", "950611 FINNIFTY", "551e7c MIDCPNIFTY"]


def index_alert(
    alert_id: str,
    option: dict[str, Any] | None,
    *,
    synthetic: Any = False,
    created: dt.datetime | None = None,
) -> dict[str, Any]:
    """An index option alert whose economics carry `option` (a recorded live chain quote)."""
    alert = make_alert("INDEX", alert_id=alert_id, created=created or ist(THU, 10, 5, 3))
    alert["economics"]["synthetic"] = synthetic
    base = alert["economics"]["option"]
    alert["economics"]["option"] = base if option is None else {**base, **option}
    return alert


def stock_alert_with_zero_oi() -> dict[str, Any]:
    alert = make_alert("STOCK")
    alert["economics"]["synthetic"] = False
    alert["economics"]["option"] = {"oi": 0.0}
    return alert


class TestVoidReason:
    @pytest.mark.parametrize("name", VOID_NAMES)
    def test_the_three_dead_contracts_are_void(self, name):
        assert void_reason(index_alert("x", RECORDED[name])) == VOID_REASON
        assert VOID_REASON == "contract not tradeable: no open interest when the alert was raised"

    @pytest.mark.parametrize("name", ["BANKNIFTY 65700 PE", "SENSEX 83400 CE"])
    def test_the_two_sound_contracts_are_not(self, name):
        assert void_reason(index_alert("x", RECORDED[name])) is None

    def test_oi_unknown_is_not_void(self):
        alert = index_alert("x", None)
        alert["economics"]["option"].pop("oi", None)
        assert void_reason(alert) is None  # an older alert: unknown, not zero
        alert["economics"]["option"]["oi"] = None
        assert void_reason(alert) is None

    @pytest.mark.parametrize("synthetic", [True, None, "false", 0])
    def test_only_a_live_chain_quote_counts(self, synthetic):
        assert void_reason(index_alert("x", {"oi": 0.0}, synthetic=synthetic)) is None

    def test_a_missing_synthetic_flag_is_a_model_estimate(self):
        alert = index_alert("x", {"oi": 0.0})
        del alert["economics"]["synthetic"]
        assert void_reason(alert) is None

    def test_a_stock_alert_is_never_void(self):
        assert void_reason(stock_alert_with_zero_oi()) is None

    @pytest.mark.parametrize("oi", [True, False, "0", "", [], {}, float("nan"), -0.0001, 1, 0.5])
    def test_only_a_number_equal_to_zero_is_void(self, oi):
        alert = index_alert("x", {"oi": oi})
        assert void_reason(alert) is None

    def test_integer_zero_and_negative_zero_are_void(self):
        assert void_reason(index_alert("x", {"oi": 0})) == VOID_REASON
        assert void_reason(index_alert("x", {"oi": -0.0})) == VOID_REASON

    @pytest.mark.parametrize(
        "alert",
        [
            None,
            "text",
            42,
            [],
            {},
            {"kind": "INDEX"},
            {"kind": "INDEX", "economics": None},
            {"kind": "INDEX", "economics": "x"},
            {"kind": "INDEX", "economics": {"synthetic": False}},
            {"kind": "INDEX", "economics": {"synthetic": False, "option": "x"}},
            {"kind": "INDEX", "economics": {"synthetic": False, "option": {}}},
        ],
    )
    def test_a_malformed_payload_is_never_an_exception(self, alert):
        assert void_reason(alert) is None

    def test_it_does_not_change_the_alert(self):
        alert = index_alert("x", RECORDED[VOID_NAMES[0]])
        before = copy.deepcopy(alert)
        void_reason(alert)
        assert alert == before


def open_index(book, alert_id: str, option: dict[str, Any] | None, at_: dt.datetime | None = None):
    created = at_ or ist(THU, 10, 5, 3)
    return book.on_alert(index_alert(alert_id, option, created=created), created)


def marks(engine) -> list[dict[str, Any]]:
    return sorted(table_rows(engine, paper_void), key=lambda r: r["alert_id"])


class TestMarking:
    def test_a_void_alert_is_marked_when_it_opens(self, make_book, engine):
        book = make_book()
        trade = open_index(book, "v1", {"oi": 0.0})
        open_index(book, "s1", {"oi": 1050.0})
        (mark,) = marks(engine)
        assert mark["trade_id"] == trade.id and mark["alert_id"] == "v1"
        assert mark["reason"] == VOID_REASON
        assert mark["marked_at"].astimezone(dt.timezone.utc) == ist(THU, 10, 5, 3).astimezone(
            dt.timezone.utc
        )

    def test_a_trade_and_its_mark_commit_together(self, make_book, engine):
        book = make_book()
        open_index(book, "v1", {"oi": 0.0})
        assert len(table_rows(engine, paper_trades)) == 1 and len(marks(engine)) == 1

    def test_the_start_pass_marks_exactly_the_three(self, make_book, engine):
        book = make_book()
        for i, name in enumerate(RECORDED):
            open_index(book, f"id{i}", RECORDED[name], ist(THU, 10, 5 + i, 3))
        stock = make_alert("STOCK", alert_id="stock1")
        book.on_alert(stock, ist(THU, 10, 5, 3))
        with engine.begin() as conn:  # a store from before US-105: no marks
            conn.execute(delete(paper_void))
        assert marks(engine) == []
        make_book()  # the next start
        got = marks(engine)
        assert [m["alert_id"] for m in got] == ["id0", "id1", "id2"]
        assert all(m["reason"] == VOID_REASON for m in got)

    def test_a_second_pass_changes_nothing_and_a_mark_survives_a_restart(self, make_book, engine):
        book = make_book(clock=lambda: ist(THU, 11, 0))
        open_index(book, "v1", {"oi": 0.0})
        with engine.begin() as conn:
            conn.execute(delete(paper_void))
        make_book(clock=lambda: ist(THU, 12, 0))
        first = marks(engine)
        assert len(first) == 1
        assert first[0]["marked_at"].astimezone(dt.timezone.utc) == ist(THU, 12, 0).astimezone(
            dt.timezone.utc
        )
        assert make_book(clock=lambda: ist(THU, 13, 0))._mark_void() == 0
        assert marks(engine) == first  # the second pass and a restart changed nothing

    def test_a_disabled_book_still_marks(self, make_book, engine):
        open_index(make_book(), "v1", {"oi": 0.0})
        with engine.begin() as conn:
            conn.execute(delete(paper_void))
        make_book(enabled=False)
        assert len(marks(engine)) == 1

    def test_an_unreadable_plan_is_skipped_never_fatal(self, make_book, engine):
        book = make_book()
        open_index(book, "v1", {"oi": 0.0})
        open_index(book, "v2", {"oi": 0.0}, ist(THU, 10, 6, 3))
        with engine.begin() as conn:
            conn.execute(delete(paper_void))
            conn.exec_driver_sql("UPDATE paper_trades SET plan = 'not json' WHERE alert_id = 'v1'")
        make_book()
        assert [m["alert_id"] for m in marks(engine)] == ["v2"]

    def test_a_mark_is_never_removed_by_the_code(self, make_book, engine):
        book = make_book()
        open_index(book, "v1", {"oi": 0.0})
        book.step(at(8), feed(BANKNIFTY=bars([(45210.35, 45220.0, 45200.0, 45215.0)] * 5)))
        make_book()
        assert len(marks(engine)) == 1

    def test_the_reconcile_path_marks_too(self, make_book, engine):
        from .conftest import store_desk_signal

        alert = index_alert("r1", {"oi": 0.0})
        store_desk_signal(engine, alert)
        make_book()  # reconciles the unbooked signal at start
        assert [m["alert_id"] for m in marks(engine)] == ["r1"]


def close(engine, alert_id: str, net: int, exit_ts: dt.datetime, gross: int | None = None) -> None:
    write_closed(
        engine,
        alert_id,
        exit_ts=exit_ts,
        gross_paise=net + 100 if gross is None else gross,
        net_paise=net,
    )


class TestScoresLeaveThemOut:
    def book_with(self, make_book, engine, with_void: bool):
        """Two sound closed trades; with `with_void`, also two void ones (a win and a loss)."""
        book = make_book()
        for i, net in enumerate((900, -300)):
            open_index(book, f"s{i}", {"oi": 500.0}, ist(THU, 10, 5 + i, 3))
            close(engine, f"s{i}", net, ist(THU, 11, i))
        if with_void:
            for i, net in enumerate((-4000, 250)):
                open_index(book, f"v{i}", {"oi": 0.0}, ist(THU, 10, 20 + i, 3))
                close(engine, f"v{i}", net, ist(THU, 12, i))
        return book

    def test_the_day_totals_equal_a_book_that_never_had_them(self, make_book, engine):
        book = self.book_with(make_book, engine, with_void=True)
        got = book.day_totals(THU)
        assert got["left_out_trades"] == 2 and got["left_out_net_paise"] == -3750
        counted = {k: v for k, v in got.items() if not k.startswith("left_out")}
        with engine.begin() as conn:  # now make it the book that never had them
            conn.execute(delete(paper_void))
            gone = select(paper_trades.c.id).where(paper_trades.c.alert_id.like("v%"))
            conn.execute(paper_events.delete().where(paper_events.c.trade_id.in_(gone)))
            conn.execute(paper_trades.delete().where(paper_trades.c.alert_id.like("v%")))
        assert book.day_totals(THU) == counted
        assert counted["closed"] == 2 and counted["net_paise"] == 600

    def test_with_nothing_void_the_totals_have_no_left_out_key(self, make_book, engine):
        book = self.book_with(make_book, engine, with_void=False)
        assert set(book.day_totals(THU)) == {"closed", "wins", "losses", "gross_paise", "net_paise"}
        assert book.day_totals(WED) == ZERO_TOTALS

    def test_the_view_prints_the_left_out_count_only_when_there_is_one(self, make_book, engine):
        from onelot.paper.view import totals_view

        plain = self.book_with(make_book, engine, with_void=False)
        assert "left_out" not in totals_view(plain.day_totals(THU))

    def test_the_view_of_a_day_with_void_trades(self, make_book, engine):
        from onelot.paper.view import totals_view

        book = self.book_with(make_book, engine, with_void=True)
        view = totals_view(book.day_totals(THU))
        assert view["left_out"] == {
            "trades": 2,
            "net": "-37.50",
            "reason": "contracts that were not tradeable (no open interest)",
        }
        assert view["closed"] == 2 and view["net_rupees"] == "6.00"

    def test_the_ledger_leaves_them_out_and_says_so(self, make_book, engine):
        from onelot.paper.ledger import PaperLedger, Slot

        self.book_with(make_book, engine, with_void=True)
        ledger = PaperLedger(engine)
        totals = ledger.totals()
        assert (totals.trades, totals.wins, totals.losses, totals.net_paise) == (2, 1, 1, 600)
        ((slot, row),) = ledger.by_slot().items()
        assert slot == Slot("ema_cross", "5m", "INDEX")
        assert (row.trades, row.net_paise, row.wins, row.losses) == (2, 600, 1, 1)
        assert (row.left_out_trades, row.left_out_net_paise) == (2, -3750)
        assert ledger.by_strategy()["ema_cross"].left_out_trades == 2
        assert ledger.left_out() == (2, -3750)

    def test_the_ledger_with_nothing_void_is_as_before(self, make_book, engine):
        from onelot.paper.ledger import PaperLedger

        self.book_with(make_book, engine, with_void=False)
        ledger = PaperLedger(engine)
        assert ledger.left_out() == (0, 0)
        (row,) = ledger.by_slot().values()
        assert row.left_out_trades == 0

    def test_the_scoreboard_row_carries_left_out_only_when_there_is_one(self, make_book, engine):
        from onelot.paper.ledger import PaperLedger
        from onelot.paper.view import slot_scoreboard_view

        self.book_with(make_book, engine, with_void=True)
        (row,) = slot_scoreboard_view(PaperLedger(engine).by_slot())
        assert row["left_out"]["trades"] == 2 and row["trades"] == 2 and row["net"] == "6.00"
        with engine.begin() as conn:
            conn.execute(delete(paper_void))
        (plain,) = slot_scoreboard_view(PaperLedger(engine).by_slot())
        assert "left_out" not in plain and plain["trades"] == 4

    def test_a_slot_with_only_void_trades_gets_no_row_but_is_counted(self, make_book, engine):
        from onelot.paper.ledger import PaperLedger

        book = make_book()
        open_index(book, "v0", {"oi": 0.0})
        close(engine, "v0", -500, ist(THU, 11, 0))
        ledger = PaperLedger(engine)
        assert ledger.by_slot() == {} and ledger.totals().trades == 0
        assert ledger.left_out() == (1, -500)

    def test_a_void_no_fill_stays_in_the_no_fill_figure(self, make_book, engine):
        """Review: a void NO_FILL used to leave the "No fill" column silently. It carries no
        money, so no score changes by keeping it: the figure counts every alert that never
        filled, and 04_OPERATIONS says so."""
        from onelot.paper.ledger import PaperLedger

        book = make_book()
        open_index(book, "s0", {"oi": 500.0})
        close(engine, "s0", 100, ist(THU, 11, 0))
        open_index(book, "v0", {"oi": 0.0}, ist(THU, 10, 9, 3))
        with engine.begin() as conn:
            conn.execute(
                paper_trades.update()
                .where(paper_trades.c.alert_id == "v0")
                .values(status="NO_FILL", close_reason="NO_FILL", updated_at=ist(THU, 10, 30))
            )
        (row,) = PaperLedger(engine).by_slot().values()
        assert row.no_fill == 1 and row.trades == 1 and row.net_paise == 100
        assert row.left_out_trades == 0 and PaperLedger(engine).left_out() == (0, 0)

    def test_a_void_close_with_an_unknown_net_is_left_out_by_neither_figure(
        self, make_book, engine
    ):
        """Review: the day's line counted it (net as 0) while the ledger's did not."""
        from onelot.paper.ledger import PaperLedger

        book = make_book()
        open_index(book, "v0", {"oi": 0.0})
        write_closed(engine, "v0", exit_ts=ist(THU, 11, 0), gross_paise=-500, net_paise=None)
        assert "left_out_trades" not in book.day_totals(THU)
        assert PaperLedger(engine).left_out() == (0, 0)

    def test_a_day_made_only_of_void_trades_is_not_the_last_session(self, make_book, engine):
        book = make_book()
        closed_trade(book, engine, "w", ist(WED, 14, 0), gross_paise=5000, net_paise=4900)
        thu = ist(THU, 10, 5, 3)
        open_index(book, "v0", {"oi": 0.0}, thu)
        close(engine, "v0", -500, ist(THU, 11, 0))
        assert book.last_session(ist(dt.date(2026, 10, 30), 9, 0)) == WED

    def test_the_recent_list_still_lists_the_void_trade_with_its_prices_and_a_tag(
        self, make_book, engine
    ):
        book = make_book()
        open_index(book, "v0", {"oi": 0.0})
        open_index(book, "s0", {"oi": 500.0}, ist(THU, 10, 6, 3))
        close(engine, "v0", -500, ist(THU, 11, 0))
        by_id = {t["alert_id"]: t for t in book.recent()}
        assert by_id["v0"]["void_reason"] == VOID_REASON
        assert by_id["v0"]["net"] == "-5.00" and by_id["v0"]["status"] == "CLOSED"
        assert "void_reason" not in by_id["s0"]


class TestAnOpenVoidTradeIsStillManaged:
    def test_it_fills_closes_as_usual_and_is_never_counted(self, make_book, engine):
        book = make_book()
        trade = open_index(book, "v0", {"oi": 0.0})
        assert book.open_trades() and book.open_trades()[0].id == trade.id
        rows = [(45210.35, 45220.00, 45200.00, 45215.00), (45150.00, 45160.00, 45100.00, 45120.00)]
        book.step(at(8), feed(BANKNIFTY=bars(rows)))
        (row,) = table_rows(engine, paper_trades)
        assert row["status"] == "CLOSED" and row["close_reason"] == "STOP"
        assert row["net_paise"] is not None and row["net_paise"] < 0
        assert book.open_trades() == []
        day = book.day_totals(THU)
        assert day["closed"] == 0 and day["left_out_trades"] == 1
        assert day["left_out_net_paise"] == row["net_paise"]
        assert len(marks(engine)) == 1

    def test_the_same_trade_unmarked_closes_to_the_same_money(self, make_book, engine, tmp_path):
        book = make_book()
        open_index(book, "v0", {"oi": 0.0})
        open_index(book, "s0", {"oi": 500.0}, ist(THU, 10, 5, 4))
        rows = [(45210.35, 45220.00, 45200.00, 45215.00), (45150.00, 45160.00, 45100.00, 45120.00)]
        book.step(at(8), feed(BANKNIFTY=bars(rows)))
        got = {r["alert_id"]: r for r in table_rows(engine, paper_trades)}
        for key in ("status", "close_reason", "entry_paise", "exit_paise", "net_paise"):
            assert got["v0"][key] == got["s0"][key]


class TestTheStartPass:
    def test_a_new_unmarked_void_trade_is_marked_beside_one_already_marked(
        self, make_book, engine, caplog
    ):
        """Review: without the guard the batch hits the existing mark, fails whole, and the new
        trade stays unmarked (the error was only logged)."""
        book = make_book()
        open_index(book, "v1", {"oi": 0.0})
        open_index(book, "v2", {"oi": 0.0}, ist(THU, 10, 6, 3))
        with engine.begin() as conn:
            conn.execute(delete(paper_void).where(paper_void.c.alert_id == "v2"))
        with caplog.at_level("WARNING"):
            assert book._mark_void() == 1
        assert [m["alert_id"] for m in marks(engine)] == ["v1", "v2"]
        assert not [r for r in caplog.records if r.levelname == "WARNING"]
        assert book._mark_void() == 0

    def test_a_sound_trade_is_never_marked_and_a_stock_trade_is_never_a_candidate(
        self, make_book, engine
    ):
        book = make_book()
        open_index(book, "s1", {"oi": 500.0})
        book.on_alert(make_alert("STOCK", alert_id="k1"), ist(THU, 10, 5, 3))
        assert book._mark_void() == 0 and marks(engine) == []


class TestTheApiFigures:
    """The functions the routes call, on a real book (no HTTP needed)."""

    def desk(self, book):
        from types import SimpleNamespace

        return SimpleNamespace(paper=book, delivery=None)

    def test_a_void_open_trade_is_not_in_the_open_column(self, make_book, engine):
        from onelot.api.server import _paper_scoreboard

        book = make_book()
        open_index(book, "s0", {"oi": 500.0})
        close(engine, "s0", 100, ist(THU, 11, 0))
        open_index(book, "s1", {"oi": 500.0}, ist(THU, 10, 6, 3))  # open, sound
        open_index(book, "v1", {"oi": 0.0}, ist(THU, 10, 7, 3))  # open, void
        (row,) = _paper_scoreboard(self.desk(make_book()))  # a restart: the book reloads its open
        assert row["open"] == 1 and row["trades"] == 1

    def test_the_paper_payload_carries_left_out_for_today_and_the_last_session(
        self, make_book, engine
    ):
        from onelot.api.server import _paper_payload

        book = make_book()
        open_index(book, "w0", {"oi": 0.0}, ist(WED, 10, 5, 3))
        close(engine, "w0", -700, ist(WED, 11, 0))
        open_index(book, "w1", {"oi": 500.0}, ist(WED, 10, 6, 3))
        close(engine, "w1", 300, ist(WED, 11, 1))
        open_index(book, "v0", {"oi": 0.0})
        close(engine, "v0", -500, ist(THU, 11, 0))
        body = _paper_payload(self.desk(book), ist(THU, 16, 0))
        left = {"trades": 1, "net": "-5.00", "reason": LEFT_OUT_NOTE}
        assert body["totals_today"]["left_out"] == left and body["totals_today"]["closed"] == 0
        assert sorted(t["alert_id"] for t in body["trades"] if "void_reason" in t) == ["v0", "w0"]
        later = _paper_payload(self.desk(book), ist(dt.date(2026, 10, 30), 9, 0))
        assert later["last_session"]["totals"]["closed"] == 1
        assert later["last_session"]["totals"]["left_out"]["net"] == "-7.00"
        assert "left_out" not in later["totals_today"]


def test_paper_void_is_one_row_per_trade(make_book, engine):
    book = make_book()
    trade = open_index(book, "v0", {"oi": 0.0})
    with engine.connect() as conn:
        ids = conn.execute(select(paper_void.c.trade_id)).scalars().all()
    assert ids == [trade.id]
