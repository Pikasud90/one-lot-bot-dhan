"""US-023 mode B: the whole story through the Engine's own paper book.

`H.real_engine` is a real `onelot.engine.Engine` (mock broker, never started) over a throw-away
copy of config/, so its book is the one production builds: `Engine._paper_book()` ->
`build_paper_book` with the real instrument specs, the real costs.yaml, the real Black-76
pricer and the real `paper:` / `backtest:` sections. Nothing is mocked but the clock of the bars.

The run: two desk alerts of TODAY (a stock and an index option) sit in `desk_signals` with no
paper row, as after a crash between the desk's insert and the hook. Starting the Engine's book
RECONCILES them; one `step` fills and closes them; then

  * every closed row carries costs and net equal to an oracle that knows only the real cost table
    (BUY/SELL legs, each rounded once half-up) and, for the option, an independent Black-76;
  * `PaperLedger` sees both and sums them exactly;
  * `GET /api/paper`, served by the real app factory over the REAL Engine object, shows the net
    of each trade and today's net total;
  * a second Engine start (a fresh book on the same database) changes nothing, and if the closes
    are put back as US-002 wrote them (costs and net NULL) that start books them again, to the
    same paise, and the panel shows them again.
"""

from __future__ import annotations

import datetime as dt
import warnings
from decimal import Decimal
from types import SimpleNamespace

import pytest
import us001_helpers as H
import us002_helpers as U
import us023_helpers as W

from onelot.clock import now_ist


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def eng(tmp_path):
    with H.real_engine(tmp_path) as engine:
        yield engine


def today():
    return now_ist().date()


def expiry_of(day):
    """An expiry three weeks out, whenever the test runs (the Lab's convention is the date)."""
    return (day + dt.timedelta(days=21)).isoformat()


class Run:
    """The two alerts, their bars, and the closed rows after the Engine's book stepped them."""

    def __init__(self, eng):
        self.eng = eng
        self.day = today()
        self.expiry = expiry_of(self.day)
        created = H.at(10, 5, 3, day=self.day)
        self.stock = W.stock_alert("E1", created=created)
        self.option = U.make_alert(
            "E2", kind="INDEX", direction="long", created=created, expiry=self.expiry
        )
        for alert in (self.stock, self.option):
            H.insert_desk_signal(eng.db.engine, alert)
        self.book = eng._paper_book()  # reconciles both desk signals
        feed = H.Feed(
            SBIN=U.pbars(
                H.at(10, 5, day=self.day), [U.PRE_BAR, *W.stock_rows(self.stock, "target")]
            ),
            BANKNIFTY=U.pbars(
                H.at(10, 5, day=self.day), [W.OPT_PRE, *W.OPTION_SCENARIOS["ce_target"][1]]
            ),
        )
        self.events = self.book.step(H.at(10, 9, day=self.day), feed)

    def row(self, alert_id):
        return U.row_of(self.eng.db.engine, alert_id)

    def expected_option(self):
        """(entry, exit, gross, costs) of the option from the real specs, config and costs and
        an independent Black-76 (the Lab's pricer): the Engine's book must equal it to the paise."""
        cfg = self.eng.cfg
        close = U.close_of([e for e in self.events if e.trade_id == self.row("E2")["id"]])
        rate = cfg.backtest.risk_free_rate

        def fair(underlying, ts):
            return H.lab_fair_premium(
                strike=54000.0,
                opt_type="CE",
                expiry=self.expiry,
                underlying=underlying,
                ts=ts,
                iv=0.16,
                rate=rate,
            )

        spread, ticks = cfg.backtest.option_spread_pct, cfg.backtest.option_slippage_ticks
        entry = W.option_entry_oracle(fair(54000.0, H.at(10, 6, day=self.day)), spread, ticks)
        exit_ = W.option_exit_oracle(fair(close.price / 100, close.at), spread, ticks)
        qty = self.book.specs["BANKNIFTY_OPT"].lot_size
        gross = (exit_ - entry) * qty
        costs = W.expected_costs(W.model_for("BANKNIFTY_OPT"), "INDEX", "long", entry, exit_, qty)
        return entry, exit_, gross, costs


@pytest.fixture
def run(eng):
    return Run(eng)


class TestAReconciledAlertClosesWithCostsAndNet:
    def test_both_desk_signals_were_reconciled_into_trades_and_closed(self, run):
        for alert_id in ("E1", "E2"):
            row = run.row(alert_id)
            assert (row["status"], row["close_reason"]) == ("CLOSED", "TARGET"), alert_id
        opened = [e for e in H.event_rows(run.eng.db.engine) if str(H.val(e["kind"])) == "OPENED"]
        assert [e["detail"]["opened_by"] for e in opened] == ["reconcile", "reconcile"]
        assert run.book.status()["hook_errors"] == 0

    def test_the_stock_books_the_two_legs_exactly(self, run):
        row = run.row("E1")
        assert (row["entry_paise"], row["exit_paise"], row["gross_paise"]) == (100030, 102000, 1970)
        assert (row["costs_paise"], row["net_paise"]) == (107, 1863)
        assert row["product"] == "equity_intraday"

    def test_the_option_equals_the_real_pricer_cost_table_and_config(self, run):
        row = run.row("E2")
        entry, exit_, gross, costs = run.expected_option()
        assert (
            row["product"] == "index_options"
            and row["qty"] == run.book.specs["BANKNIFTY_OPT"].lot_size
        )
        assert (row["entry_paise"], row["exit_paise"]) == (entry, exit_)
        assert row["gross_paise"] == gross
        assert (row["costs_paise"], row["net_paise"]) == (costs, gross - costs)
        for column in W.PAISE_COLUMNS:
            assert type(row[column]) is int, column
            assert W.typeofs(run.eng.db.engine, column) == ["integer", "integer"], column

    def test_the_ledger_sees_both_and_sums_them_exactly(self, run):
        _, _, gross_o, costs_o = run.expected_option()
        totals = W.make_ledger(run.eng.db.engine).totals()
        assert totals.trades == 2
        assert totals.gross_paise == 1970 + gross_o
        assert totals.costs_paise == 107 + costs_o
        assert totals.net_paise == 1863 + gross_o - costs_o
        assert totals.net_paise == totals.gross_paise - totals.costs_paise
        assert totals.wins + totals.losses <= 2
        assert W.make_ledger(run.eng.db.engine).totals(grade="live").trades == 2
        assert W.make_ledger(run.eng.db.engine).totals(grade="delayed").trades == 0


def paper_api_over_the_engine(eng, book):
    """GET /api/paper through the real app factory over the REAL Engine object."""
    from onelot.api.server import create_app

    with warnings.catch_warnings():  # starlette warns that its test client prefers httpx2
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient

    eng.desk = SimpleNamespace(paper=book, hook_errors=0)
    client = TestClient(create_app(eng, "test-token"), raise_server_exceptions=False)
    response = client.get("/api/paper")
    assert response.status_code == 200, response.text
    return response.json()


class TestThePanelShowsTheNet:
    def test_each_trade_and_todays_total_show_net_in_rupees(self, run):
        _, _, gross_o, costs_o = run.expected_option()
        body = paper_api_over_the_engine(run.eng, run.book)
        trades = {t["alert_id"]: t for t in body["trades"]}
        assert trades["E1"]["net"] == "18.63" and trades["E1"]["gross"] == "19.70"
        assert trades["E2"]["net"] == W.rupees(gross_o - costs_o)
        assert trades["E2"]["gross"] == W.rupees(gross_o)
        totals = body["totals_today"]
        net_total = 1863 + gross_o - costs_o
        assert totals["net_rupees"] == W.rupees(net_total)
        assert totals["gross_rupees"] == W.rupees(1970 + gross_o)
        assert totals["closed"] == 2 and totals["wins"] + totals["losses"] <= 2
        assert Decimal(totals["net_rupees"]) == Decimal(net_total) / 100
        assert body["status"]["hook_errors"] == 0 and body["status"]["closed"] == 2

    def test_closed_trades_show_no_mark(self, run):
        body = paper_api_over_the_engine(run.eng, run.book)
        for trade in body["trades"]:
            assert trade.get("mark") is None and trade.get("unrealised") is None


class TestASecondEngineStart:
    def test_a_fresh_book_on_the_same_database_changes_nothing(self, run):
        before = (H.trade_rows(run.eng.db.engine), H.event_rows(run.eng.db.engine))
        second = run.eng._paper_book()
        assert second.open_trades() == [] and second.status()["opened"] == 0
        assert (H.trade_rows(run.eng.db.engine), H.event_rows(run.eng.db.engine)) == before

    def test_closes_written_as_us002_wrote_them_are_booked_again_to_the_same_paise(self, run):
        native = {a: (run.row(a)["costs_paise"], run.row(a)["net_paise"]) for a in ("E1", "E2")}
        assert None not in {v for pair in native.values() for v in pair}
        W.null_out_costs(run.eng.db.engine)
        assert W.make_ledger(run.eng.db.engine).totals().trades == 0, "NULL net: not a ledger trade"
        book = run.eng._paper_book()  # the next Engine start
        assert book.status()["hook_errors"] == 0
        for alert_id, money in native.items():
            assert (run.row(alert_id)["costs_paise"], run.row(alert_id)["net_paise"]) == money
        assert W.make_ledger(run.eng.db.engine).totals().trades == 2
        body = paper_api_over_the_engine(run.eng, book)
        assert body["totals_today"]["net_rupees"] is not None
        assert {t["alert_id"]: t["net"] for t in body["trades"]}["E1"] == "18.63"

    def test_a_disabled_engine_still_completes_the_costs_but_opens_nothing(self, tmp_path):
        with H.real_engine(tmp_path, enabled=False) as eng:
            run_book = eng._paper_book()
            assert run_book.open_trades() == [] and run_book.enabled is False
            # a legacy close written while paper was on, costs NULL: the start completes it
            W.insert_booked(
                eng.db.engine, [W.Booked("L1", "CLOSED", 500, None, None, "live", H.at(10, 7))]
            )
            reborn = eng._paper_book()
            row = H.trade_row(eng.db.engine, "L1")
            assert row["costs_paise"] is not None and row["net_paise"] == 500 - row["costs_paise"]
            assert reborn.open_trades() == [] and len(H.trade_rows(eng.db.engine)) == 1
