"""US-023 AC1: a closed trade books per-leg costs with the REAL sides, net, risk and R.

    gross_paise = sign x (exit - entry) x qty
    costs_paise = leg(entry, qty, entry side) + leg(exit, qty, exit side), each leg
                  round_half_up(CostModel.leg(price_rupees, qty, side).total x 100), rounded ONCE
    net_paise   = gross - costs;  risk_paise excludes costs;  R = net / risk;  win iff net > 0
    sides       = long BUY then SELL; stock SHORT SELL then BUY (STT on the ENTRY leg);
                  index option BUY premium then SELL premium (long premium whatever the direction)
    a long's total is within 1 paisa of `CostModel.round_trip_cost`

The book is driven through its public contract (`on_alert`, `step`) and read back from the
`paper_trades` row. Today the close leaves costs_paise and net_paise NULL, so every test here
fails on that until US-023 books them.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest
import us001_helpers as H
import us002_helpers as U
import us023_helpers as W


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def db(tmp_path):
    d = H.new_db(tmp_path)
    yield d
    d.dispose()


def assert_costed(row):
    """The close booked costs and net (today they stay NULL until US-023)."""
    assert row["status"] == "CLOSED", row
    assert row["costs_paise"] is not None, "costs_paise must be booked with the close"
    assert row["net_paise"] is not None, "net_paise must be booked with the close"


def assert_books_the_money(row, model, kind, direction, qty):
    """The whole money story of one CLOSED, priced row against the oracle."""
    assert row["status"] == "CLOSED" and row["exit_paise"] is not None, row
    entry, exit_ = row["entry_paise"], row["exit_paise"]
    assert row["gross_paise"] == W.expected_gross(kind, direction, entry, exit_, qty)
    costs = W.expected_costs(model, kind, direction, entry, exit_, qty)
    assert row["costs_paise"] == costs, "costs are the two legs, each rounded once half-up"
    assert row["net_paise"] == row["gross_paise"] - costs, "net = gross - costs, exactly"
    for column in ("costs_paise", "net_paise"):
        assert type(row[column]) is int, f"{column} must be a plain integer of paise"


class TestTheWorkedExamples:
    """SBIN, one share: BUY at 1000.30 (the slipped fill), then SELL. Golden values, derived by
    hand from config/costs.yaml in pure Decimal (see `pure_decimal_leg`), not from CostModel."""

    def test_a_long_stock_that_hits_its_target_books_42_plus_65_paise_of_costs(self, db):
        _, row, _ = W.run_stock(db.engine, direction="long", hit="target")
        assert (row["entry_paise"], row["exit_paise"], row["gross_paise"]) == (100030, 102000, 1970)
        # BUY 1000.30: 42.153 paise -> 42; SELL 1020.00: 65.423 paise -> 65. Rounding the SUM
        # (107.576) would book 108: each leg is rounded once, on its own.
        assert row["costs_paise"] == 107
        assert row["net_paise"] == 1863
        assert W.pure_decimal_costs("equity_intraday", "STOCK", "long", 100030, 102000, 1) == 107

    def test_a_long_stock_that_hits_its_stop_books_42_plus_63_paise_of_costs(self, db):
        _, row, _ = W.run_stock(db.engine, direction="long", hit="stop")
        assert (row["entry_paise"], row["exit_paise"], row["gross_paise"]) == (100030, 98970, -1060)
        # SELL 989.70: 63.48 paise -> 63. Rounding the SUM (105.63) would book 106.
        assert row["costs_paise"] == 105
        assert row["net_paise"] == -1165
        assert W.pure_decimal_costs("equity_intraday", "STOCK", "long", 100030, 98970, 1) == 105

    def test_the_costs_of_the_worked_examples_equal_the_contract_oracle(self, db):
        _, row, _ = W.run_stock(db.engine, hit="target")
        assert_books_the_money(row, W.model_for("SBIN"), "STOCK", "long", 1)


class TestRealSides:
    """A short SELLS first: STT is on the ENTRY leg and stamp duty on the exit (buy-back) leg."""

    @pytest.mark.parametrize("hit", ["target", "stop"])
    def test_a_short_stock_pays_stt_on_its_entry_leg(self, db, hit):
        _, row, _ = W.run_stock(db.engine, direction="short", key="MRF", entry=100000.0, hit=hit)
        assert_costed(row)
        model = W.model_for("MRF")
        entry, exit_ = row["entry_paise"], row["exit_paise"]
        real = W.expected_costs(model, "STOCK", "short", entry, exit_, 1)
        assumed_long = W.wrong_sides_costs(model, "short", entry, exit_, 1)
        assert real != assumed_long, "the scenario must tell the two apart"
        assert row["costs_paise"] == real, "a short is SELL then BUY, never BUY then SELL"
        assert row["gross_paise"] == -(exit_ - entry)
        assert row["net_paise"] == row["gross_paise"] - real

    @pytest.mark.parametrize("direction", ["long", "short"])
    @pytest.mark.parametrize("hit", ["target", "stop"])
    def test_with_a_loud_tax_table_a_wrong_side_would_cost_whole_rupees(self, db, direction, hit):
        """STT 1 % on the sell leg and stamp 0.5 % on the buy leg: the booked costs must equal the
        real-sides oracle and be thousands of paise away from the other assignment."""
        loud = W.loud_cost_for()
        _, row, _ = W.run_stock(
            db.engine, direction=direction, key="MRF", entry=100000.0, hit=hit, cost_for=loud
        )
        model = loud("MRF")
        entry, exit_ = row["entry_paise"], row["exit_paise"]
        real = W.expected_costs(model, "STOCK", direction, entry, exit_, 1)
        other = W.wrong_sides_costs(model, direction, entry, exit_, 1)
        if direction == "short":
            assert abs(real - other) > 100, "the loud table must make the sides rupees apart"
        assert row["costs_paise"] == real
        assert row["net_paise"] == row["gross_paise"] - real

    def test_the_short_target_golden_values(self, db):
        """Short MRF target at 98000: gross +1,970.00 rupees; costs 82.54 with STT on the entry."""
        _, row, _ = W.run_stock(db.engine, direction="short", key="MRF", entry=100000.0)
        assert (row["entry_paise"], row["exit_paise"], row["gross_paise"]) == (
            9997000,
            9800000,
            197000,
        )
        assert row["costs_paise"] == 8254  # the buy-then-sell assumption would book 8211
        assert row["net_paise"] == 197000 - 8254

    def test_the_short_golden_values_agree_with_the_pure_decimal_derivation(self, db):
        _, row, _ = W.run_stock(db.engine, direction="short", key="MRF", entry=100000.0)
        derived = W.pure_decimal_costs(
            "equity_intraday", "STOCK", "short", row["entry_paise"], row["exit_paise"], 1
        )
        assert row["costs_paise"] == derived == 8254


class TestIndexOptionsAreLongPremium:
    """BUY premium then SELL premium on the index_options product, qty = one lot, whichever
    way the alert's underlying direction points (a BUY_PE is long premium too)."""

    @pytest.mark.parametrize("name", list(W.OPTION_SCENARIOS))
    def test_costs_are_a_buy_leg_then_a_sell_leg_of_one_lot(self, db, name):
        direction, rows, reason, _ = W.OPTION_SCENARIOS[name]
        _, row, _ = W.run_option(db.engine, rows, direction=direction)
        assert row["close_reason"] == reason and row["qty"] == H.LOT
        assert row["product"] == "index_options"
        assert_books_the_money(row, W.model_for("BANKNIFTY_OPT"), "INDEX", direction, H.LOT)

    def test_a_nifty_lot_of_65_is_costed_at_65(self, db):
        alert = U.make_alert("N1", kind="INDEX", direction="long", instrument_key="NIFTY_OPT")
        _, rows, _, _ = W.OPTION_SCENARIOS["ce_target"]
        _, row, _ = W.run_option(db.engine, rows, alert=alert)
        assert row["qty"] == H.NIFTY_LOT == 65
        assert_books_the_money(row, W.model_for("NIFTY_OPT"), "INDEX", "long", H.NIFTY_LOT)

    def test_the_option_golden_values(self, db):
        """BUY CE 452.80 x 30 then SELL 641.00 x 30 (the Lab's exit fill for the 54300 target)."""
        _, rows, _, _ = W.OPTION_SCENARIOS["ce_target"]
        _, row, _ = W.run_option(db.engine, rows)
        assert (row["entry_paise"], row["exit_paise"]) == (45280, 64100)
        assert row["gross_paise"] == (64100 - 45280) * 30 == 564600
        derived = W.pure_decimal_costs("index_options", "INDEX", "long", 45280, 64100, 30)
        assert row["costs_paise"] == derived == 8063
        assert row["net_paise"] == 564600 - 8063

    def test_a_buy_pe_is_costed_buy_then_sell_even_though_the_underlying_is_sold(self, db):
        _, rows, _, _ = W.OPTION_SCENARIOS["pe_target"]
        _, row, _ = W.run_option(db.engine, rows, direction="short")
        assert row["side"] == "BUY_PE" and row["direction"] == "short"
        assert_costed(row)
        model = W.model_for("BANKNIFTY_OPT")
        entry, exit_ = row["entry_paise"], row["exit_paise"]
        buy_then_sell = W.expected_costs(model, "INDEX", "short", entry, exit_, 30)
        sell_then_buy = W.expected_costs(model, "STOCK", "short", entry, exit_, 30)
        assert buy_then_sell != sell_then_buy, "the scenario must tell the two apart"
        assert row["costs_paise"] == buy_then_sell, "an option is never SELL then BUY"
        assert row["gross_paise"] == (exit_ - entry) * 30


class TestNetWinsAndR:
    @pytest.mark.parametrize(
        "direction, target",
        [("long", 1000.90), ("short", 999.10)],
        ids=["long", "short"],
    )
    def test_a_trade_that_wins_on_gross_but_not_after_costs_is_a_loss(self, db, direction, target):
        _, row, _ = W.run_stock(
            db.engine, direction=direction, hit="target", alert_kw={"target": target}
        )
        assert row["close_reason"] == "TARGET"
        assert_costed(row)
        assert row["gross_paise"] == 60 > 0, "60 paise of gross profit ..."
        assert row["costs_paise"] > 60, "... against more than that in charges"
        assert row["net_paise"] == 60 - row["costs_paise"] < 0, "so net is below zero: a LOSS"

    def test_risk_excludes_costs_and_survives_the_close_unchanged(self, db):
        book = W.make_book(db.engine)
        alert = W.stock_alert("A1")
        H.open_trade(book, alert)
        frame = U.pbars(H.at(10, 5), [U.PRE_BAR, *W.stock_rows(alert, "target")])
        U.step_book(book, frame, H.at(10, 7))  # only the fill bar has closed
        before = U.row_of(db.engine, "A1")
        assert before["status"] == "OPEN" and before["risk_paise"] == 1030
        U.step_book(book, frame, H.at(10, 9))  # the target bar has closed
        row = U.row_of(db.engine, "A1")
        assert_costed(row)
        assert row["risk_paise"] == 1030 == abs(row["entry_paise"] - 99000) * row["qty"]
        assert row["costs_paise"] > 0, "costs exist, yet risk_paise did not move"

    def test_r_is_net_over_risk_not_gross_over_risk(self, db):
        _, row, _ = W.run_stock(db.engine, hit="target")
        assert_costed(row)
        r_net = Fraction(row["net_paise"], row["risk_paise"])
        r_gross = Fraction(row["gross_paise"], row["risk_paise"])
        assert r_net == Fraction(1863, 1030) and r_net < r_gross == Fraction(1970, 1030)
        assert float(r_net) == pytest.approx(1.8087, abs=1e-4)

    def test_an_option_loss_has_negative_net_and_r_below_minus_one(self, db):
        """Stopped out: the premium falls to the stop fill and the costs make the loss deeper
        than the risk (which excludes them): R is below -1."""
        _, rows, _, _ = W.OPTION_SCENARIOS["ce_stop"]
        _, row, _ = W.run_option(db.engine, rows)
        assert_costed(row)
        assert row["gross_paise"] == -row["risk_paise"], "the stop fill is the risk leg"
        assert row["net_paise"] < row["gross_paise"] < 0
        assert Fraction(row["net_paise"], row["risk_paise"]) < -1


class TestPerLegRoundingAndTheCallContract:
    """With a stand-in cost model whose totals are exact binary fractions, the rounding rule and
    the arguments of every `leg` call are pinned exactly."""

    @pytest.mark.parametrize(
        "buy, sell, expected",
        [
            (0.125, 0.625, 76),  # 12.5 -> 13, 62.5 -> 63. half-even or truncation: 74; sum: 75
            (0.125, 0.125, 26),  # 12.5 -> 13 twice. half-even: 24; rounding the sum (25): 25
            (1.125, 2.625, 376),  # 112.5 -> 113, 262.5 -> 263. half-even: 374; sum: 375
        ],
    )
    @pytest.mark.parametrize("direction", ["long", "short"])
    def test_each_leg_is_rounded_once_half_up_never_the_sum_never_half_even(
        self, db, direction, buy, sell, expected
    ):
        recorder = W.RecordingCosts({"buy": buy, "sell": sell})
        _, row, _ = W.run_stock(db.engine, direction=direction, cost_for=recorder.cost_for)
        assert row["status"] == "CLOSED"
        assert row["costs_paise"] == expected
        assert row["net_paise"] == row["gross_paise"] - expected

    @pytest.mark.parametrize(
        "direction, first, second", [("long", "buy", "sell"), ("short", "sell", "buy")]
    )
    def test_a_stock_is_costed_leg_by_leg_with_its_prices_in_rupees_and_its_sides(
        self, db, direction, first, second
    ):
        recorder = W.RecordingCosts()
        _, row, _ = W.run_stock(db.engine, direction=direction, cost_for=recorder.cost_for)
        entry, exit_ = row["entry_paise"] / 100, row["exit_paise"] / 100
        assert sorted(recorder.legs) == sorted([(entry, 1, first), (exit_, 1, second)])
        assert set(recorder.asked) == {"SBIN"}, "cost_for(instrument_key)"
        assert all(isinstance(price, float) for price, _, _ in recorder.legs)

    @pytest.mark.parametrize("direction", ["long", "short"])
    def test_an_option_is_costed_buy_then_sell_on_its_premiums_for_one_lot(self, db, direction):
        recorder = W.RecordingCosts()
        _, rows, _, _ = W.OPTION_SCENARIOS["ce_target" if direction == "long" else "pe_target"]
        _, row, _ = W.run_option(db.engine, rows, direction=direction, cost_for=recorder.cost_for)
        entry, exit_ = row["entry_paise"] / 100, row["exit_paise"] / 100
        assert sorted(recorder.legs) == sorted([(entry, 30, "buy"), (exit_, 30, "sell")])
        assert set(recorder.asked) == {"BANKNIFTY_OPT"}
        assert row["costs_paise"] == 76

    def test_costs_are_booked_once_at_the_close_and_a_later_step_does_not_recost(self, db):
        recorder = W.RecordingCosts()
        book, row, _ = W.run_stock(db.engine, cost_for=recorder.cost_for)
        assert len(recorder.legs) == 2
        alert = W.stock_alert("A1")
        frame = U.pbars(H.at(10, 5), [U.PRE_BAR, *W.stock_rows(alert, "target")])
        assert U.step_book(book, frame, H.at(10, 30)) == []
        assert len(recorder.legs) == 2, "the closed trade is final: no second costing"
        assert U.row_of(db.engine, "A1")["costs_paise"] == row["costs_paise"] == 76


class TestPerTradeTotalIsWithinOnePaisaOfRoundTripCostForLongs:
    @pytest.mark.parametrize("key, entry", [("SBIN", 1000.0), ("MRF", 100000.0), ("INFY", 1650.35)])
    @pytest.mark.parametrize("hit", ["target", "stop"])
    def test_a_long_stock(self, db, key, entry, hit):
        _, row, _ = W.run_stock(db.engine, direction="long", key=key, entry=entry, hit=hit)
        assert_costed(row)
        exact = W.round_trip_gap(W.model_for(key), row["entry_paise"], row["exit_paise"], 1)
        assert abs(Decimal(row["costs_paise"]) - exact) <= 1, (row["costs_paise"], exact)

    @pytest.mark.parametrize("name", ["ce_target", "ce_stop", "ce_gap_stop"])
    def test_a_long_option(self, db, name):
        direction, rows, _, _ = W.OPTION_SCENARIOS[name]
        _, row, _ = W.run_option(db.engine, rows, direction=direction)
        assert_costed(row)
        model = W.model_for("BANKNIFTY_OPT")
        exact = W.round_trip_gap(model, row["entry_paise"], row["exit_paise"], H.LOT)
        assert abs(Decimal(row["costs_paise"]) - exact) <= 1, (row["costs_paise"], exact)


class TestNothingIsBookedHalfWay:
    """A CLOSED row with a priced exit always carries costs and net: they are written with the
    close, in the same transaction, never in a later step."""

    @pytest.mark.parametrize("hit", ["target", "stop"])
    def test_the_events_and_the_row_agree_on_one_close(self, db, hit):
        _, row, events = W.run_stock(db.engine, hit=hit)
        closes = [e for e in events if str(e.kind) == "CLOSED"]
        assert len(closes) == 1
        stored = [e for e in H.event_rows(db.engine, "A1") if str(H.val(e["kind"])) == "CLOSED"]
        assert len(stored) == 1
        assert row["costs_paise"] is not None and row["net_paise"] is not None
        assert row["net_paise"] == row["gross_paise"] - row["costs_paise"]

    def test_a_trade_that_is_still_open_has_no_costs_yet(self, db):
        _, row, _ = W.run_stock(db.engine, hit="quiet")
        assert row["status"] == "OPEN"
        assert (row["exit_paise"], row["gross_paise"], row["costs_paise"], row["net_paise"]) == (
            None,
            None,
            None,
            None,
        )
