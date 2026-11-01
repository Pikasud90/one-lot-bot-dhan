"""US-023: behaviours the story is SILENT about, pinned to the Tester's PROPOSED reading.

Each class names the clarity note it belongs to (A-n, see the TEST REPORT). They are kept apart so
the Manager can accept, change or drop a proposal in one place without touching the acceptance
tests of AC1-AC4:

  A-2  a trade whose net is exactly 0 is neither a win nor a loss (as US-030's panel counts it)
  A-3  the ledger window is half-open [since, until) on the trade's exit time
  A-4  a naive `since` / `until` is rejected, never guessed (the package-wide rule)
  A-5  an OPEN stock trade also shows mark = its last close and unrealised = sign x (mark - entry)
  A-11 the startup backfill runs even when paper.enabled is false (it opens nothing)
  A-12 a cost model that fails at a close, or at the startup backfill, never loses the close or
       stops the book from starting: costs and net stay NULL until a start that can compute them
       (a backfill row that fails counts once in hook_errors and books no event: Manager, accepted)
  A-18 the stop leg of an option's RISK is priced on model + basis too (a chain trade's R is
       then comparable with a synthetic one's)

Decided by the Manager on 2026-11-02 and therefore NOT here any more: the ledger refuses an unknown
grade and since > until, [t, t) is zero (test_us023_ac2_ledger.py); the NaN / non-positive VIX
fallback is for the paper pricing path only, the desk's own `_premium_at` is US-025 (the former
A-13 is dropped); a mark is floored at one tick and a failure STREAK raises one event (AC3 file).
A-2, A-3 and A-5 agree with the developers' own tests (tests/paper/test_ledger.py).
"""

from __future__ import annotations

import datetime as dt

import pytest
import us001_helpers as H
import us002_helpers as U
import us023_helpers as W
from sqlalchemy import text

from onelot.clock import IST


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


def at(day: int, hour: int = 10, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(
        W.IST_DAY + dt.timedelta(days=day), dt.time(hour, minute), tzinfo=IST
    )


class TestA2AZeroNetIsNeitherAWinNorALoss:
    def test_a_breakeven_trade_is_counted_as_a_trade_only(self, db):
        W.insert_booked(
            db.engine,
            [
                W.booked("Z1", 100, 100, ts=at(1)),  # net exactly 0
                W.booked("W1", 500, 100, ts=at(1)),
                W.booked("L1", 50, 100, ts=at(1)),
            ],
        )
        totals = W.make_ledger(db.engine).totals()
        assert (totals.trades, totals.wins, totals.losses) == (3, 1, 1)
        assert totals.net_paise == 0 + 400 - 50


class TestA3TheWindowIsHalfOpenOnTheExitTime:
    def test_a_trade_exactly_at_since_is_in_and_exactly_at_until_is_out(self, db):
        W.insert_booked(db.engine, [W.booked("B1", 900, 100, ts=at(2, 10, 0))])
        ledger = W.make_ledger(db.engine)
        assert ledger.totals(since=at(2, 10, 0)).trades == 1, "since is inclusive"
        assert ledger.totals(until=at(2, 10, 0)).trades == 0, "until is exclusive"
        assert ledger.totals(since=at(2, 10, 0), until=at(2, 10, 1)).trades == 1

    def test_the_window_is_judged_on_when_the_trade_closed_not_when_it_was_raised(self, db):
        W.insert_booked(db.engine, [W.booked("X1", 900, 100, ts=at(1))])
        from onelot.store.schema import paper_trades

        with db.engine.begin() as conn:
            conn.execute(
                paper_trades.update().where(paper_trades.c.alert_id == "X1").values(exit_ts=at(2))
            )
        ledger = W.make_ledger(db.engine)
        assert ledger.totals(since=at(2, 0)).trades == 1, "closed on day 2"
        assert ledger.totals(until=at(2, 0)).trades == 0, "raised on day 1, but not closed yet"


class TestA4ANaiveWindowIsRejected:
    @pytest.mark.parametrize("name", ["since", "until"])
    def test_a_naive_datetime_is_a_value_error_never_a_guess(self, db, name):
        ledger = W.make_ledger(db.engine)
        naive = dt.datetime(2026, 10, 29, 0, 0)
        with pytest.raises(ValueError):
            ledger.totals(**{name: naive})


class TestA5AnOpenStockShowsItsMarkToo:
    @pytest.mark.parametrize(
        "direction, entry", [("long", 100030), ("short", 99970)], ids=["long", "short"]
    )
    def test_mark_is_the_last_close_and_unrealised_follows_the_side(self, db, direction, entry):
        book, row, _ = W.run_stock(db.engine, direction=direction, hit="quiet")
        assert row["status"] == "OPEN" and row["entry_paise"] == entry
        trade = W.api_trade(book, "A1")
        assert trade["mark"] == "1000.00", "the close of the last managed bar, unslipped"
        sign = 1 if direction == "long" else -1
        assert trade["unrealised"] == W.rupees(sign * (100000 - entry) * 1)

    def test_a_stock_with_no_bar_managed_yet_has_a_null_mark(self, db):
        book = W.make_book(db.engine)
        H.open_trade(book, W.stock_alert("A1"))
        trade = W.api_trade(book, "A1")
        assert trade["status"] == "PENDING_FILL"
        assert trade.get("mark") is None and trade.get("unrealised") is None


class TestA11TheBackfillRunsEvenWhenPaperIsDisabled:
    def test_a_disabled_book_still_completes_the_costs_of_closed_trades(self, db):
        W.run_stock(db.engine)
        W.null_out_costs(db.engine)
        book = W.make_book(db.engine, enabled=False)
        row = U.row_of(db.engine, "A1")
        assert row["costs_paise"] == 107 and row["net_paise"] == 1863
        assert book.open_trades() == [], "and it opens nothing"


class TestA12ACostModelThatFailsNeverLosesTheClose:
    @staticmethod
    def broken(key):
        raise RuntimeError("the cost table is unreadable")

    def test_the_close_is_booked_without_costs_and_the_trade_is_final(self, db):
        book, row, events = W.run_stock(db.engine, cost_for=self.broken)
        assert (row["status"], row["close_reason"]) == ("CLOSED", "TARGET")
        assert (row["entry_paise"], row["exit_paise"], row["gross_paise"]) == (100030, 102000, 1970)
        assert row["costs_paise"] is None and row["net_paise"] is None, "never a fake 0"
        assert [str(e.kind) for e in events if str(e.kind) == "CLOSED"] == ["CLOSED"]
        assert book.open_trades() == [], "the trade is not left active to close twice"

    def test_a_later_start_with_a_working_cost_table_completes_it(self, db):
        W.run_stock(db.engine, cost_for=self.broken)
        W.make_book(db.engine)
        row = U.row_of(db.engine, "A1")
        assert (row["costs_paise"], row["net_paise"]) == (107, 1863)

    def test_a_cost_model_that_fails_at_startup_does_not_stop_the_book_or_the_other_rows(self, db):
        W.insert_booked(
            db.engine,
            [
                W.Booked("OK1", "CLOSED", 500, None, None, "live", at(1)),
                W.Booked("GONE1", "CLOSED", 500, None, None, "live", at(1)),
                W.Booked("OK2", "CLOSED", -500, None, None, "live", at(1)),
            ],
        )
        with db.engine.begin() as conn:
            conn.execute(
                text("UPDATE paper_trades SET instrument_key='GONE' WHERE alert_id='GONE1'")
            )
        events_before = H.event_rows(db.engine)
        book = W.make_book(db.engine)  # cost_for('GONE') raises KeyError: must not raise here
        assert book.status()["hook_errors"] == 1, "one row failed: counted (Manager: no event)"
        assert H.event_rows(db.engine) == events_before, "and no event is booked for it"
        rows = {r["alert_id"]: r for r in H.trade_rows(db.engine)}
        assert rows["GONE1"]["costs_paise"] is None and rows["GONE1"]["net_paise"] is None
        for alert_id in ("OK1", "OK2"):
            assert rows[alert_id]["costs_paise"] is not None, alert_id
            assert (
                rows[alert_id]["net_paise"]
                == rows[alert_id]["gross_paise"] - rows[alert_id]["costs_paise"]
            )
        assert book.open_trades() == []


class TestA18TheRiskLegIsOnModelPlusBasisToo:
    """risk_paise = (entry fill - the stop exit fill) x qty. With the basis applied to every
    option premium (Manager decision 1) the stop leg carries it as well, so a chain trade's risk,
    and with it R, is what the Lab books on the shifted pricer: comparable with a synthetic one."""

    @pytest.mark.parametrize(
        "name, direction", [("ce_target", "long"), ("pe_target", "short")], ids=["ce", "pe"]
    )
    def test_a_chain_trades_risk_equals_the_labs_on_a_pricer_shifted_by_the_basis(
        self, db, name, direction
    ):
        alert = W.chain_alert(direction=direction)
        _, rows, _, _ = W.OPTION_SCENARIOS[name]
        _, row, events = W.run_option(db.engine, rows, alert=alert)
        close = U.close_of(events)
        lab = W.lab_option_row(
            bt=H.BT,
            spec=W.SPECS["BANKNIFTY_OPT"],
            opt_type="CE" if direction == "long" else "PE",
            underlying_entry=54000.0,
            underlying_exit=close.price / 100,
            entry_ts=H.at(10, 6),
            exit_ts=close.at,
            pricer=W.shifted(H.make_pricer(), 5.25),
            stop=alert["economics"]["underlying_stop"],
        )
        assert row["risk_paise"] == H.to_paise(lab["risk_inr"])
