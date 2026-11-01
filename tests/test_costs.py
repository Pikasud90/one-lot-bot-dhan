"""Cost model tests.

The reference figures below are computed BY HAND in
docs/05_COST_RECONCILIATION.md. If a statutory rate changes in
config/costs.yaml, these tests SHOULD fail — that is the point. Update the
hand-worked document first, then these numbers.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from onelot.costs import (
    CostModel,
    Quote,
    compute_economics,
    effective_entry,
    effective_exit,
    round_to_tick,
)

QTY = 70  # 2 lots x 35
BUY_PREMIUM = 300.0
SELL_PREMIUM = 400.0

# Hand-computed, docs/05_COST_RECONCILIATION.md
HAND_BUY_TOTAL = 33.059114
HAND_SELL_TOTAL = 63.372152


class TestLegBreakdown:
    def test_buy_leg_matches_hand_computation(self, cost_model: CostModel):
        b = cost_model.leg(BUY_PREMIUM, QTY, "buy")
        assert b.turnover == pytest.approx(21_000.0)
        assert b.brokerage == pytest.approx(20.0)
        assert b.stt == 0.0, "STT is never charged on an option purchase"
        assert b.exchange_txn == pytest.approx(7.3563)
        assert b.sebi_fee == pytest.approx(0.021)
        assert b.ipft == pytest.approx(0.105)
        assert b.stamp_duty == pytest.approx(0.63)
        assert b.gst == pytest.approx(4.946814)
        assert b.total == pytest.approx(HAND_BUY_TOTAL, abs=1e-6)

    def test_sell_leg_matches_hand_computation(self, cost_model: CostModel):
        s = cost_model.leg(SELL_PREMIUM, QTY, "sell")
        assert s.stt == pytest.approx(28.0), "0.10% of 28,000 premium turnover"
        assert s.stamp_duty == 0.0, "stamp duty is buy-side only"
        assert s.total == pytest.approx(HAND_SELL_TOTAL, abs=1e-6)

    def test_gst_excludes_stt_and_stamp_duty(self, cost_model: CostModel, cost_cfg):
        """A classic real-world error: GST is on services, not on taxes."""
        s = cost_model.leg(SELL_PREMIUM, QTY, "sell")
        expected = cost_cfg.taxes.gst_pct * (s.brokerage + s.exchange_txn + s.sebi_fee + s.ipft)
        assert s.gst == pytest.approx(expected)
        assert s.gst < cost_cfg.taxes.gst_pct * (expected / cost_cfg.taxes.gst_pct + s.stt)

    def test_every_v1_omission_is_now_nonzero(self, cost_model: CostModel):
        """v1 omitted GST, stamp duty, SEBI fee and IPFT entirely (defect A1)."""
        b = cost_model.leg(BUY_PREMIUM, QTY, "buy")
        s = cost_model.leg(SELL_PREMIUM, QTY, "sell")
        assert b.gst > 0 and s.gst > 0
        assert b.stamp_duty > 0
        assert b.sebi_fee > 0 and s.sebi_fee > 0
        assert b.ipft > 0 and s.ipft > 0

    def test_v1_undercounts_by_more_than_40_percent(self, cost_model: CostModel):
        v1_model = 40.0 + 0.001 * SELL_PREMIUM * QTY
        actual = cost_model.round_trip_cost(BUY_PREMIUM, SELL_PREMIUM, QTY)
        assert actual > v1_model * 1.4


class TestNetPnl:
    def test_net_pnl_is_gross_minus_costs(self, cost_model: CostModel):
        net = cost_model.net_pnl(BUY_PREMIUM, SELL_PREMIUM, QTY)
        gross = (SELL_PREMIUM - BUY_PREMIUM) * QTY
        assert net == pytest.approx(gross - (HAND_BUY_TOTAL + HAND_SELL_TOTAL), abs=1e-6)
        assert net < gross, "net must always be below gross for a long round trip"

    def test_flat_exit_is_a_loss(self, cost_model: CostModel):
        """Exiting at your entry price loses money. It is not break-even."""
        assert cost_model.net_pnl(BUY_PREMIUM, BUY_PREMIUM, QTY) < 0


class TestBreakeven:
    def test_breakeven_is_above_entry(self, cost_model: CostModel):
        be = cost_model.breakeven_premium(BUY_PREMIUM, QTY)
        assert be > BUY_PREMIUM

    def test_breakeven_is_exact_to_one_tick(self, cost_model: CostModel):
        be = cost_model.breakeven_premium(BUY_PREMIUM, QTY, tick=0.05)
        assert cost_model.net_pnl(BUY_PREMIUM, be, QTY) >= 0
        assert cost_model.net_pnl(BUY_PREMIUM, be - 0.05, QTY) < 0

    def test_breakeven_lands_on_the_tick_grid(self, cost_model: CostModel):
        be = cost_model.breakeven_premium(BUY_PREMIUM, QTY, tick=0.05)
        assert abs((be / 0.05) - round(be / 0.05)) < 1e-6

    @settings(max_examples=40, deadline=None)
    @given(
        premium=st.floats(min_value=5.0, max_value=2000.0, allow_nan=False),
        lots=st.integers(min_value=1, max_value=10),
    )
    def test_breakeven_property_holds_everywhere(self, cost_model: CostModel, premium, lots):
        qty = lots * 35
        be = cost_model.breakeven_premium(premium, qty)
        assert cost_model.net_pnl(premium, be, qty) >= -1e-6
        assert cost_model.net_pnl(premium, be - 0.05, qty) < 0


class TestExerciseStt:
    def test_exercise_stt_is_on_intrinsic_not_premium(self, cost_model: CostModel, cost_cfg):
        got = cost_model.exercise_stt_if_expired_itm(500.0, QTY)
        assert got == pytest.approx(cost_cfg.taxes.stt_exercise_pct * 500.0 * QTY)

    def test_exercise_costs_more_than_selling(self, cost_model: CostModel):
        """Justifies mandatory square-off — modestly, not by an order of magnitude."""
        intrinsic = 500.0
        exercise = cost_model.exercise_stt_if_expired_itm(intrinsic, QTY)
        sell_stt = cost_model.leg(intrinsic, QTY, "sell").stt
        assert exercise > sell_stt
        assert exercise < sell_stt * 2, "should be ~25% more, not orders of magnitude"


class TestEffectivePricing:
    """v1 defect A2: assumed you transact at LTP on both sides."""

    def test_entry_pays_the_ask_plus_slippage(self, cfg):
        q = Quote(bid=299.0, ask=301.0, ltp=300.0)
        e = effective_entry(q, cfg.execution_assumptions, 0.05)
        assert e == pytest.approx(301.10)
        assert e > q.ltp, "you never buy at LTP"
        assert e > q.mid

    def test_exit_receives_less_than_the_level(self, cfg):
        x = effective_exit(300.0, cfg.execution_assumptions, 0.05)
        assert x == pytest.approx(299.90)
        assert x < 300.0

    def test_entry_rounds_up_and_exit_rounds_down(self):
        assert round_to_tick(301.06, 0.05, "up") == pytest.approx(301.10)
        assert round_to_tick(301.09, 0.05, "down") == pytest.approx(301.05)

    def test_zero_bid_or_ask_is_not_a_valid_market(self):
        assert not Quote(bid=0.0, ask=301.0).is_valid
        assert not Quote(bid=299.0, ask=0.0).is_valid
        assert not Quote(bid=302.0, ask=301.0).is_valid, "crossed book"
        assert Quote(bid=299.0, ask=301.0).is_valid

    def test_spread_pct_uses_mid(self):
        q = Quote(bid=98.0, ask=102.0)
        assert q.mid == pytest.approx(100.0)
        assert q.spread == pytest.approx(4.0)
        assert q.spread_pct == pytest.approx(0.04)


class TestEconomics:
    def _econ(self, cost_model, cfg, lots=3, stop=40.0):
        return compute_economics(
            quote=Quote(bid=299.0, ask=301.0, ltp=300.0),
            stop_distance=stop,
            rr_target_gross=cfg.strategy.exits.rr_target_gross,
            lots=lots,
            lot_size=35,
            cost_model=cost_model,
            exec_cfg=cfg.execution_assumptions,
            tick=0.05,
        )

    def test_net_rr_is_below_gross_rr(self, cost_model, cfg):
        e = self._econ(cost_model, cfg)
        assert e.rr_net < cfg.strategy.exits.rr_target_gross, (
            "net R/R must be strictly below the gross target multiple — "
            "this is exactly why v1's rr>=2.5 gate on a 2.5R target was unreachable"
        )

    def test_breakeven_sits_between_entry_and_target(self, cost_model, cfg):
        e = self._econ(cost_model, cfg)
        assert e.entry_eff < e.breakeven < e.target_level

    def test_larger_size_amortises_fixed_brokerage(self, cost_model, cfg):
        """Fixed per-order fees mean R/R improves with size. Sizing must
        account for this rather than dividing by one lot's risk."""
        small = self._econ(cost_model, cfg, lots=1)
        big = self._econ(cost_model, cfg, lots=8)
        assert big.rr_net > small.rr_net

    def test_zero_lots_returns_a_coherent_audit_object(self, cost_model, cfg):
        e = self._econ(cost_model, cfg, lots=0)
        assert e.qty == 0 and e.net_risk == 0.0
        assert e.warnings, "must explain itself for the audit log"

    def test_stop_is_below_entry_and_target_above(self, cost_model, cfg):
        e = self._econ(cost_model, cfg)
        assert e.stop_level < e.entry_eff < e.target_level
        assert e.stop_exit_eff < e.stop_level
        assert e.target_exit_eff < e.target_level

    def test_tiny_target_is_flagged_as_unwinnable(self, cost_model, cfg):
        e = compute_economics(
            quote=Quote(bid=299.0, ask=301.0),
            stop_distance=0.05,
            rr_target_gross=1.0,
            lots=1,
            lot_size=35,
            cost_model=cost_model,
            exec_cfg=cfg.execution_assumptions,
            tick=0.05,
        )
        assert e.net_reward <= 0 or e.warnings

    @settings(max_examples=30, deadline=None)
    @given(lots=st.integers(min_value=1, max_value=10))
    def test_costs_monotonic_in_size(self, cost_model, cfg, lots):
        a = self._econ(cost_model, cfg, lots=lots)
        b = self._econ(cost_model, cfg, lots=lots + 1)
        assert b.net_risk > a.net_risk
        assert math.isfinite(b.rr_net)
