"""Contract selection — v1 defects A8 (put delta sign) and B1 (flat spread cap)."""

from __future__ import annotations

import pytest

from onelot.broker.base import ChainRow
from onelot.strategy.selector import select_contract


def row(strike, opt, delta, bid, ask, oi=500_000, vol=50_000, bq=980, aq=980,
        src="cross_checked", iv=0.14):
    return ChainRow(strike=strike, opt_type=opt, security_id=f"S{strike}{opt}",
                    ltp=(bid + ask) / 2, bid=bid, ask=ask, bid_qty=bq, ask_qty=aq,
                    volume=vol, oi=oi, iv=iv, delta=delta, gamma=0.0001,
                    theta=-8.0, vega=12.0, greek_source=src)


class TestPutDeltaSign:
    """*** v1 defect A8 *** — `0.60 <= delta <= 0.70` matches ZERO puts."""

    def test_a_put_with_negative_delta_is_selected(self, cfg):
        rows = [row(54_500, "PE", -0.65, 300.0, 302.0)]
        r = select_contract(rows, "PE", cfg.strategy.contract)
        assert r.ok, r.reason
        assert r.chosen.delta < 0

    def test_v1s_signed_comparison_would_have_rejected_it(self, cfg):
        d = -0.65
        assert not (0.60 <= d <= 0.70), "the exact expression v1 used"
        assert cfg.strategy.contract.delta_min <= abs(d) <= cfg.strategy.contract.delta_max

    def test_calls_and_puts_both_select_symmetrically(self, cfg):
        rows = [row(53_500, "CE", 0.65, 300.0, 302.0),
                row(54_500, "PE", -0.65, 300.0, 302.0)]
        assert select_contract(rows, "CE", cfg.strategy.contract).ok
        assert select_contract(rows, "PE", cfg.strategy.contract).ok

    def test_out_of_band_delta_is_rejected_both_signs(self, cfg):
        assert not select_contract([row(1, "CE", 0.30, 10.0, 10.5)],
                                   "CE", cfg.strategy.contract).ok
        assert not select_contract([row(1, "PE", -0.95, 10.0, 10.5)],
                                   "PE", cfg.strategy.contract).ok


class TestRelativeSpreadGate:
    """*** v1 defect B1 *** — a flat Rs 1.50 cap rejects every ITM monthly strike."""

    def test_wide_rupee_spread_on_an_expensive_option_is_accepted(self, cfg):
        # Rs 8.25 spread on a Rs 1,047 option = 0.79%, entirely normal.
        r = select_contract([row(52_400, "CE", 0.68, 1043.35, 1051.60)],
                            "CE", cfg.strategy.contract)
        assert r.ok, r.reason
        assert r.quote.spread == pytest.approx(8.25)

    def test_v1s_flat_cap_would_have_rejected_that_contract(self):
        assert not (1051.60 - 1043.35) <= 1.50

    def test_proportionally_wide_spread_is_still_rejected(self, cfg):
        # Rs 30 on a Rs 100 option = 30%: genuinely untradeable.
        assert not select_contract([row(56_000, "CE", 0.65, 85.0, 115.0)],
                                   "CE", cfg.strategy.contract).ok

    def test_absolute_floor_protects_cheap_options(self, cfg):
        """A Rs 0.50 spread on a Rs 12 option is 4% but only 10 ticks."""
        cc = cfg.strategy.contract
        assert cc.max_allowed_spread(12.0) == pytest.approx(cc.spread_abs_floor)
        assert cc.max_allowed_spread(1000.0) == pytest.approx(20.0)


class TestLiquidityAndSafety:
    def test_zero_bid_is_rejected(self, cfg):
        r = select_contract([row(54_000, "CE", 0.65, 0.0, 302.0)], "CE",
                            cfg.strategy.contract)
        assert not r.ok and "two-sided" in r.reason

    def test_low_open_interest_is_rejected(self, cfg):
        r = select_contract([row(54_000, "CE", 0.65, 300.0, 302.0, oi=100)],
                            "CE", cfg.strategy.contract)
        assert not r.ok and "open interest" in r.reason

    def test_thin_depth_is_rejected(self, cfg):
        """A tight spread on one lot is not liquidity."""
        r = select_contract([row(54_000, "CE", 0.65, 300.0, 302.0, bq=35, aq=35)],
                            "CE", cfg.strategy.contract)
        assert not r.ok and "depth" in r.reason

    def test_rejected_greeks_exclude_the_contract(self, cfg):
        """v1 defect A9: a wrong broker greek must not reach the sizer."""
        r = select_contract([row(54_000, "CE", 0.65, 300.0, 302.0, src="rejected")],
                            "CE", cfg.strategy.contract)
        assert not r.ok and "greeks unusable" in r.reason

    def test_missing_delta_excludes_the_contract(self, cfg):
        bad = row(54_000, "CE", 0.65, 300.0, 302.0)
        bad.delta = None
        assert not select_contract([bad], "CE", cfg.strategy.contract).ok


class TestSelectionChoice:
    def test_picks_the_delta_closest_to_the_band_midpoint(self, cfg):
        rows = [row(53_000, "CE", 0.695, 400.0, 402.0),
                row(53_500, "CE", 0.650, 300.0, 302.0),
                row(54_000, "CE", 0.605, 200.0, 202.0)]
        r = select_contract(rows, "CE", cfg.strategy.contract)
        assert r.chosen.strike == 53_500, "0.65 is the middle of 0.60-0.70"

    def test_rejection_summary_explains_an_empty_result(self, cfg):
        rows = [row(56_000, "CE", 0.20, 10.0, 10.5),
                row(57_000, "CE", 0.15, 5.0, 5.5),
                row(52_000, "CE", 0.95, 2000.0, 2002.0)]
        r = select_contract(rows, "CE", cfg.strategy.contract)
        assert not r.ok
        assert r.rejection_summary()["delta outside band"] == 3
        assert r.considered == 3

    def test_empty_chain_is_handled(self, cfg):
        r = select_contract([], "CE", cfg.strategy.contract)
        assert not r.ok and "no CE rows" in r.reason

    def test_real_chain_yields_both_sides(self, cfg, enriched_chain):
        rows = enriched_chain.snapshot.rows
        assert select_contract(rows, "CE", cfg.strategy.contract).ok
        assert select_contract(rows, "PE", cfg.strategy.contract).ok
