"""US-023 mode B: the story through a real Desk poll (the production caller of `PaperBook.step`).

The rig is US-002's: a warmed Desk over scripted bars with a real PaperBook (real specs and cost
tables, a constant fake premium of 100.00). A polled option or stock trade that closes carries
exact costs and net, which the ledger and the panel agree on; nothing leaks as a hook error on
either side. (The rig polls on the wall clock over yesterday's bars, so every trade it opens is
past its session end and closes at once: an OPEN trade's marks are covered at the book level and
through the Engine's book, not here.)
"""

from __future__ import annotations

import pytest
import us001_helpers as H
import us023_helpers as W
from test_us002_b_desk_end_to_end import Rig, levels, rupees

from onelot.config import AppConfig


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def app_cfg():
    return AppConfig.load()


@pytest.fixture
def desk_db(tmp_path):
    d = H.new_db(tmp_path, "desk.db", make_global=True)
    yield d
    H.dispose_db(d, was_global=True)


@pytest.fixture
def rig(app_cfg, desk_db):
    return Rig(app_cfg, desk_db)


class TestAPolledOptionTradeClosesWithCosts:
    def test_a_polled_option_that_closes_leaves_no_mark_and_books_costs(self, rig):
        alert = rig.alert("BANKNIFTY", "long")
        _, target = levels(alert)
        o = rig.fill_open("BANKNIFTY")
        through = rupees(target) + 0.5
        rig.script("BANKNIFTY", {3: (o, max(o, through), min(o, through), o)})
        rig.poll()
        row = rig.row(alert)
        assert (row["status"], row["close_reason"]) == ("CLOSED", "TARGET")
        model = W.model_for("BANKNIFTY_OPT")
        costs = W.expected_costs(
            model, "INDEX", "long", row["entry_paise"], row["exit_paise"], row["qty"], True
        )
        assert (row["costs_paise"], row["net_paise"]) == (costs, row["gross_paise"] - costs)
        trade = W.api_trade(rig.book, alert["id"])
        assert trade.get("mark") is None and trade["net"] == W.rupees(row["net_paise"])


class TestAPolledStockTradeClosesWithCostsAndNet:
    @pytest.mark.parametrize("direction", ["long", "short"])
    def test_costs_net_ledger_and_panel_agree(self, rig, direction):
        alert = rig.alert("SBIN", direction)
        stop, _ = levels(alert)
        o = rig.fill_open("SBIN")
        sign = 1 if direction == "long" else -1
        breach = rupees(stop) - sign * 0.5
        rig.script("SBIN", {2: (o, max(o, breach) + 0.1, min(o, breach) - 0.1, o)})
        rig.poll()
        row = rig.row(alert)
        assert (row["status"], row["close_reason"]) == ("CLOSED", "STOP")
        costs = W.expected_costs(
            W.model_for("SBIN"), "STOCK", direction, row["entry_paise"], row["exit_paise"], 1, True
        )
        assert (row["costs_paise"], row["net_paise"]) == (costs, row["gross_paise"] - costs)
        assert row["net_paise"] < row["gross_paise"], "a stop is a loss and costs deepen it"
        totals = W.make_ledger(rig.engine).totals()
        assert (totals.trades, totals.losses, totals.net_paise) == (1, 1, row["net_paise"])
        assert W.api_trade(rig.book, alert["id"])["net"] == W.rupees(row["net_paise"])
        assert rig.book.status()["hook_errors"] == 0 and rig.desk.hook_errors == 0
