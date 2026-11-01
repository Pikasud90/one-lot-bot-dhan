"""PaperBook.on_alert / open_trades / status (US-001 AC1, AC7) and the paper schema/config."""

from __future__ import annotations

import copy
import threading

import pytest
from pydantic import ValidationError
from sqlalchemy import BigInteger, inspect, text

from onelot.config import AppConfig, PaperCfg
from onelot.paper.book import PaperAlertError, PaperTrade
from onelot.store.schema import paper_events, paper_trades

from .conftest import THU, ist, make_alert, table_rows

ZERO_STATUS = {"opened": 0, "filled": 0, "no_fill": 0, "closed": 0, "hook_errors": 0}


# ── config + schema ──────────────────────────────────────────────────────────


class TestPaperConfig:
    def test_defaults(self):
        assert PaperCfg() == PaperCfg(enabled=True, feed_delay_minutes=15)
        assert AppConfig().paper == PaperCfg()

    def test_repo_config_yaml_carries_the_paper_key(self, cfg):
        assert cfg.paper == PaperCfg(enabled=True, feed_delay_minutes=15)

    def test_unknown_key_and_negative_delay_are_rejected(self):
        with pytest.raises(ValidationError):
            PaperCfg(enabled=True, feed_delay_mins=15)  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            PaperCfg(feed_delay_minutes=-1)


class TestSchema:
    MONEY = ("entry_paise", "exit_paise", "gross_paise", "costs_paise", "net_paise", "risk_paise")

    def test_create_all_builds_both_tables(self, engine):
        names = set(inspect(engine).get_table_names())
        assert {"paper_trades", "paper_events"} <= names

    def test_money_columns_are_bigint_paise(self):
        for col in self.MONEY:
            assert isinstance(paper_trades.c[col].type, BigInteger), col

    def test_alert_id_is_unique(self, engine):
        uniques = inspect(engine).get_unique_constraints("paper_trades")
        indexes = [i for i in inspect(engine).get_indexes("paper_trades") if i["unique"]]
        cols = [u["column_names"] for u in uniques] + [i["column_names"] for i in indexes]
        assert ["alert_id"] in cols


# ── AC1: every alert opens exactly one PENDING_FILL paper trade ──────────────


class TestOnAlertOpens:
    def test_stock_alert_opens_one_pending_fill_row(self, make_book, engine):
        book = make_book()
        alert = make_alert("STOCK", "long")

        trade = book.on_alert(alert, ist(THU, 10, 5, 4))

        assert isinstance(trade, PaperTrade)
        rows = table_rows(engine, paper_trades)
        assert len(rows) == 1
        row = rows[0]
        assert row["id"] == trade.id
        assert row["alert_id"] == "a1b2c3d4e5f6"
        assert row["status"] == "PENDING_FILL"
        assert row["close_reason"] is None
        assert row["instrument_key"] == "RELIANCE"
        assert row["series_key"] == "RELIANCE"
        assert row["kind"] == "STOCK"
        assert row["side"] == "BUY"
        assert row["direction"] == "long"
        assert row["strategy"] == "ema_cross"
        assert row["timeframe"] == "5m"
        assert row["confidence"] == pytest.approx(62.5)
        assert row["data_source"] == "dhan"
        assert row["grade"] == "live"
        assert row["product"] == "equity_intraday"
        assert row["qty"] == 1
        assert row["resolution_min"] == 1
        for col in TestSchema.MONEY:
            assert row[col] is None, col  # no price is invented at birth
        assert row["entry_ts"] is None and row["fill_bar_ts"] is None

    def test_trade_mirrors_the_row_with_ist_aware_times(self, make_book):
        trade = make_book().on_alert(make_alert(), ist(THU, 10, 5, 4))
        assert trade is not None
        assert trade.status == "PENDING_FILL"
        assert trade.grade == "live"
        assert trade.created_at == ist(THU, 10, 5, 3)
        assert trade.expires_at == ist(THU, 10, 15, 3)
        assert trade.created_at.utcoffset() == ist(THU, 0, 0).utcoffset()
        assert trade.entry_paise is None

    def test_index_option_alert_is_one_lot_of_the_alerted_option(self, make_book):
        trade = make_book().on_alert(make_alert("INDEX", "short"), ist(THU, 10, 5, 4))
        assert trade is not None
        assert (trade.kind, trade.side, trade.direction) == ("INDEX", "BUY_PE", "short")
        assert trade.instrument_key == "BANKNIFTY_OPT"
        assert trade.series_key == "BANKNIFTY"
        assert trade.qty == 30
        assert trade.product == "index_options"
        option = trade.plan["alert"]["economics"]["option"]
        assert (option["strike"], option["opt_type"], option["iv"]) == (45400.0, "PE", 0.1532)

    @pytest.mark.parametrize(
        ("source", "grade"),
        [
            ("dhan", "live"),
            ("dhan-ws", "live"),
            ("DHAN", "live"),
            ("yahoo", "delayed"),
            ("mock", "delayed"),
            ("", "delayed"),
            (None, "delayed"),
        ],
    )
    def test_grade_is_live_iff_data_source_starts_with_dhan(self, make_book, source, grade):
        trade = make_book().on_alert(make_alert(source=source), ist(THU, 10, 5, 4))
        assert trade is not None
        assert trade.grade == grade
        assert trade.data_source == (source or "")

    @pytest.mark.parametrize("status", ["PENDING", "EXPIRED", "TAKEN", "SKIPPED"])
    @pytest.mark.parametrize("risk_allowed", [True, False])
    def test_any_alert_status_and_risk_verdict_opens_a_trade(
        self, make_book, engine, status, risk_allowed
    ):
        alert = make_alert(status=status, risk_allowed=risk_allowed)
        assert make_book().on_alert(alert, ist(THU, 10, 5, 4)) is not None
        assert [r["status"] for r in table_rows(engine, paper_trades)] == ["PENDING_FILL"]

    def test_levels_are_stored_once_as_integer_paise(self, make_book):
        trade = make_book().on_alert(make_alert("INDEX", "long"), ist(THU, 10, 5, 4))
        assert trade is not None
        levels = trade.plan["levels"]
        assert levels == {
            "underlying_entry_paise": 4521035,
            "underlying_stop_paise": 4511035,
            "underlying_target_paise": 4541035,
        }
        assert all(type(v) is int for v in levels.values())

    def test_opened_event_is_written_with_the_trade(self, make_book, engine):
        trade = make_book().on_alert(make_alert(source="yahoo"), ist(THU, 10, 5, 4))
        assert trade is not None
        events = table_rows(engine, paper_events)
        assert len(events) == 1
        ev = events[0]
        assert ev["trade_id"] == trade.id
        assert ev["kind"] == "OPENED"
        assert ev["detail"]["grade"] == "delayed"
        assert ev["detail"]["data_source"] == "yahoo"
        assert ev["detail"]["alert_status"] == "PENDING"

    def test_status_counts_the_open_and_is_a_copy(self, make_book):
        book = make_book()
        assert book.status() == ZERO_STATUS
        book.on_alert(make_alert(), ist(THU, 10, 5, 4))
        st = book.status()
        assert st == ZERO_STATUS | {"opened": 1}
        st["opened"] = 99
        assert book.status()["opened"] == 1

    def test_open_trades_is_a_thread_safe_copy(self, make_book):
        book = make_book()
        trade = book.on_alert(make_alert(), ist(THU, 10, 5, 4))
        got = book.open_trades()
        assert got == [trade]
        got.clear()
        assert book.open_trades() == [trade]


class TestOnAlertIdempotent:
    def test_second_call_returns_the_existing_trade_and_creates_no_row(self, make_book, engine):
        book = make_book()
        first = book.on_alert(make_alert(), ist(THU, 10, 5, 4))
        second = book.on_alert(make_alert(), ist(THU, 10, 5, 9))
        assert second == first
        assert len(table_rows(engine, paper_trades)) == 1
        assert len(table_rows(engine, paper_events)) == 1
        assert book.status()["opened"] == 1

    def test_a_second_book_on_the_same_database_does_not_duplicate(self, make_book, engine):
        first = make_book().on_alert(make_alert(), ist(THU, 10, 5, 4))
        again = make_book().on_alert(make_alert(), ist(THU, 10, 5, 9))
        assert again is not None and first is not None
        assert again.id == first.id
        assert len(table_rows(engine, paper_trades)) == 1

    def test_losing_the_insert_race_returns_the_winner_without_integrity_error(
        self, make_book, engine, monkeypatch
    ):
        loser = make_book()  # built before the row exists, so nothing is cached
        winner = make_book().on_alert(make_alert(), ist(THU, 10, 5, 4))
        real_fetch = loser._fetch
        misses = iter([None])  # the race window: the first lookup misses the winner's row
        monkeypatch.setattr(loser, "_fetch", lambda aid: next(misses, None) or real_fetch(aid))

        got = loser.on_alert(make_alert(), ist(THU, 10, 5, 4))

        assert winner is not None and got is not None
        assert got.id == winner.id
        assert len(table_rows(engine, paper_trades)) == 1
        assert len(table_rows(engine, paper_events)) == 1
        assert loser.status()["opened"] == 0

    def test_mutating_the_alert_afterwards_changes_nothing_stored(self, make_book, engine):
        book = make_book()
        alert = make_alert("INDEX", "long")
        trade = book.on_alert(alert, ist(THU, 10, 5, 4))
        assert trade is not None
        row_before = table_rows(engine, paper_trades)
        plan_before = copy.deepcopy(trade.plan)

        alert["strategy"] = "mutated"
        alert["data"]["source"] = "yahoo"
        alert["economics"]["underlying_stop"] = 1.0
        alert["economics"]["option"]["lot"] = 999
        alert["manage"]["atr"] = None

        assert table_rows(engine, paper_trades) == row_before
        assert trade.plan == plan_before
        assert book.open_trades()[0].plan == plan_before
        trade.plan["alert"]["strategy"] = "caller scribbled on its copy"
        assert book.open_trades()[0].plan == plan_before

    def test_concurrent_calls_with_one_alert_id_yield_one_row(self, make_book, engine):
        books = [make_book(), make_book()]  # same book shared AND a second instance
        barrier = threading.Barrier(8)
        results: list[object] = []
        errors: list[BaseException] = []

        def worker(i: int) -> None:
            try:
                barrier.wait()
                results.append(books[i % 2].on_alert(make_alert(), ist(THU, 10, 5, 4)))
            except BaseException as exc:  # noqa: BLE001 - collected and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == []
        assert len(table_rows(engine, paper_trades)) == 1
        assert len(table_rows(engine, paper_events)) == 1
        assert len({r.id for r in results if isinstance(r, PaperTrade)}) == 1
        assert len(results) == 8


class TestOnAlertRejectsBadInput:
    def test_naive_created_at_is_rejected_and_nothing_is_written(self, make_book, engine):
        alert = make_alert()
        alert["created_at"] = "2026-10-29T10:05:03"
        with pytest.raises(PaperAlertError, match="created_at"):
            make_book().on_alert(alert, ist(THU, 10, 5, 4))
        assert table_rows(engine, paper_trades) == []

    def test_naive_now_is_rejected(self, make_book):
        with pytest.raises(ValueError, match="now"):
            make_book().on_alert(make_alert(), ist(THU, 10, 5, 4).replace(tzinfo=None))

    def test_missing_id_is_rejected(self, make_book, engine):
        alert = make_alert()
        del alert["id"]
        with pytest.raises(PaperAlertError, match="id"):
            make_book().on_alert(alert, ist(THU, 10, 5, 4))
        assert table_rows(engine, paper_trades) == []


# ── AC7: paper.enabled false stops NEW trades only ────────────────────────────


class TestDisabled:
    def test_on_alert_returns_none_and_writes_nothing(self, make_book, engine):
        book = make_book(enabled=False)
        assert book.on_alert(make_alert(), ist(THU, 10, 5, 4)) is None
        assert table_rows(engine, paper_trades) == []
        assert table_rows(engine, paper_events) == []
        assert book.status() == ZERO_STATUS
        assert book.open_trades() == []

    def test_trades_already_open_are_still_held(self, make_book, engine):
        opened = make_book().on_alert(make_alert(), ist(THU, 10, 5, 4))
        disabled = make_book(enabled=False)
        assert disabled.open_trades() == [opened]
        with engine.connect() as conn:
            n = conn.execute(text("select count(*) from paper_trades")).scalar_one()
        assert n == 1
