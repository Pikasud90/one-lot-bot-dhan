"""US-023 AC4: `economics._costs_for` parses costs.yaml ONCE per process.

`_costs_for(spec)` is called for every desk alert and every manual close; it re-reads and
re-parses config/costs.yaml (and logs the "UNVERIFIED rates" warning) on every call. The story
asks for a cache and "a test asserting a single load". Two tests assert it:

* in this process, however the table is read (CostConfig.load or a yaml parse that looks like
  costs.yaml): at most ONE parse over 40 calls and two products. At most, not exactly: the cache
  may already be warm from an earlier test in the run;
* in a FRESH interpreter, where nothing is warm: exactly ONE parse in total for 40 calls over two
  products, whether it happens at import or on the first call. Two products still means one parse
  of the file: a cache keyed by product that re-reads the file per product is a miss.

A cache must also keep the products apart: the stock table (STT 0.025 %, brokerage min-of) and
the option table (STT 0.1 %, flat) answer in turn, whatever was asked first.
"""

from __future__ import annotations

import logging

import pytest
import us001_helpers as H
import us023_helpers as W

from onelot.desk import economics


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


STOCK, OPTION = H.SPECS["SBIN"], H.SPECS["BANKNIFTY_OPT"]


def parses(counts: dict[str, int]) -> int:
    """One parse is counted by both hooks; a parse that bypasses CostConfig counts once."""
    return max(counts["load"], counts["yaml"])


class TestCostsYamlIsParsedOncePerProcess:
    def test_forty_calls_over_two_products_parse_the_table_at_most_once_here(self):
        with W.count_cost_table_parses() as counts:
            for i in range(40):
                economics._costs_for(STOCK if i % 2 == 0 else OPTION)
        assert parses(counts) <= 1, f"costs.yaml was parsed {counts} times in 40 calls"

    def test_a_fresh_process_parses_the_table_exactly_once_for_forty_calls(self):
        result = W.cold_process_parses()
        assert parses(result["counts"]) == 1, result["counts"]

    def test_the_second_product_does_not_trigger_a_second_parse(self):
        """The first call may warm the cache; every later call, whichever product, is free."""
        economics._costs_for(STOCK)
        with W.count_cost_table_parses() as counts:
            economics._costs_for(OPTION)
            economics._costs_for(STOCK)
            economics._costs_for(OPTION)
        assert parses(counts) == 0, counts

    def test_the_unverified_rates_warning_is_not_repeated_on_every_call(self, caplog):
        economics._costs_for(STOCK)  # warm
        with caplog.at_level(logging.WARNING, logger="onelot.config"):
            for _ in range(20):
                economics._costs_for(OPTION)
        unverified = [r for r in caplog.records if "UNVERIFIED" in r.getMessage()]
        assert len(unverified) == 0, "a cached table is not re-read, so it cannot re-warn"


class TestTheCachedModelsAreStillRightPerProduct:
    def test_the_stock_and_option_tables_answer_in_turn(self):
        for spec in (STOCK, OPTION, OPTION, STOCK, STOCK, OPTION):
            model = economics._costs_for(spec)
            if spec is STOCK:
                assert model.cfg.taxes.stt_sell_pct == 0.00025
                assert model.cfg.brokerage.mode == "min_of"
            else:
                assert model.cfg.taxes.stt_sell_pct == 0.0010
                assert model.cfg.brokerage.mode == "flat"

    def test_a_cached_model_prices_exactly_like_a_freshly_built_one(self):
        fresh_stock, fresh_option = W.model_for("SBIN"), W.model_for("BANKNIFTY_OPT")
        for _ in range(3):
            assert economics._costs_for(STOCK).round_trip_cost(1000.3, 1020.0, 1) == (
                fresh_stock.round_trip_cost(1000.3, 1020.0, 1)
            )
            assert economics._costs_for(OPTION).round_trip_cost(452.8, 641.0, 30) == (
                fresh_option.round_trip_cost(452.8, 641.0, 30)
            )

    def test_the_cold_process_saw_the_right_table_for_every_call(self):
        seen = W.cold_process_parses()["seen"]
        assert len(seen) == 40
        assert seen[0::2] == [[0.00025, "min_of"]] * 20, "SBIN: equity_intraday"
        assert seen[1::2] == [[0.0010, "flat"]] * 20, "BANKNIFTY_OPT: index_options"

    def test_an_unknown_product_still_falls_back_to_the_default_table_not_an_error(self):
        from onelot.data.universe import InstrumentSpec

        odd = InstrumentSpec("ODD", "odd", "STOCK", "ODD", 1, 0.05, "no_such_product", 0.0)
        model = economics._costs_for(odd)
        assert model.cfg.taxes.stt_sell_pct == 0.0010, "the (most expensive) default table"
