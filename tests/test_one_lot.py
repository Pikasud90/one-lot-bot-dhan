"""The one-lot rule.

Exactly one lot per trade, always, whatever it costs. There is no position
sizing, no affordability gate, and no signal is ever rejected for being
expensive. This file pins that rule and the risk model that replaced sizing:
the STOP bounds a single trade, and absolute rupee limits bound the day and
the cumulative drawdown.
"""

from __future__ import annotations

import pytest

from onelot.config import RiskCfg
from onelot.costs import Quote
from onelot.strategy.sizer import size_position

LOT = 35


def _size(cfg, cost_model, premium=870.0, atr=80.0, delta=0.65, capital=0.0, **risk_over):
    risk = cfg.risk.model_copy(update=risk_over) if risk_over else cfg.risk
    q = Quote(bid=premium - 2, ask=premium + 2, ltp=premium)
    return size_position(
        quote=q,
        stop_distance=1.5 * atr * delta,
        rr_target_gross=2.5,
        lot_size=LOT,
        risk_cfg=risk,
        cost_model=cost_model,
        exec_cfg=cfg.execution_assumptions,
        capital=capital,
    )


class TestAlwaysOneLot:
    def test_shipped_rule_is_one_lot(self, cfg):
        assert cfg.risk.lots_per_trade == 1

    @pytest.mark.parametrize("premium", [60.0, 300.0, 870.0, 1600.0, 3000.0])
    def test_price_never_changes_the_lot_count(self, cfg, cost_model, premium):
        """The whole point: an expensive contract is still exactly one lot."""
        r = _size(cfg, cost_model, premium=premium)
        assert r.is_tradeable and r.lots == 1 and r.qty == LOT

    @pytest.mark.parametrize("atr", [30.0, 80.0, 150.0, 250.0])
    def test_volatility_never_changes_the_lot_count(self, cfg, cost_model, atr):
        r = _size(cfg, cost_model, atr=atr)
        assert r.is_tradeable and r.lots == 1

    @pytest.mark.parametrize("capital", [0.0, 50_000.0, 1_000_000.0, 50_000_000.0])
    def test_capital_never_changes_the_lot_count(self, cfg, cost_model, capital):
        """Capital is informational. It cannot make the system trade bigger."""
        r = _size(cfg, cost_model, capital=capital)
        assert r.is_tradeable and r.lots == 1

    def test_undeclared_capital_still_trades(self, cfg, cost_model):
        """Capital 0 means 'not disclosed', not 'blocked'."""
        assert cfg.account.capital_inr == 0
        assert cfg.blocking_problems() == []
        assert _size(cfg, cost_model, capital=0.0).is_tradeable


class TestNothingIsRejectedOnCost:
    """There is no affordability gate. A trade is never skipped for price."""

    def test_a_very_expensive_contract_is_still_taken(self, cfg, cost_model):
        r = _size(cfg, cost_model, premium=3000.0)
        assert r.is_tradeable
        assert r.economics.premium_outlay > 100_000

    def test_a_very_wide_stop_is_still_taken(self, cfg, cost_model):
        """Extreme volatility is excluded upstream by the chop filter, not here."""
        r = _size(cfg, cost_model, atr=300.0)
        assert r.is_tradeable
        assert r.economics.net_risk > 8_000

    def test_the_only_rejection_left_is_a_missing_market(self, cfg, cost_model):
        q = Quote(bid=0.0, ask=870.0)
        r = size_position(
            quote=q,
            stop_distance=78.0,
            rr_target_gross=2.5,
            lot_size=LOT,
            risk_cfg=cfg.risk,
            cost_model=cost_model,
            exec_cfg=cfg.execution_assumptions,
        )
        assert not r.is_tradeable
        assert "two-sided market" in r.rejected_reason

    def test_a_crossed_book_is_rejected(self, cfg, cost_model):
        q = Quote(bid=880.0, ask=870.0)
        r = size_position(
            quote=q,
            stop_distance=78.0,
            rr_target_gross=2.5,
            lot_size=LOT,
            risk_cfg=cfg.risk,
            cost_model=cost_model,
            exec_cfg=cfg.execution_assumptions,
        )
        assert not r.is_tradeable


class TestRiskIsAnOutput:
    def test_rupee_risk_tracks_the_market_not_the_account(self, cfg, cost_model):
        a = _size(cfg, cost_model, atr=50.0, capital=50_000.0).economics.net_risk
        b = _size(cfg, cost_model, atr=50.0, capital=5_000_000.0).economics.net_risk
        assert a == pytest.approx(b), "capital must not affect a fixed-lot trade"

    def test_risk_scales_with_volatility(self, cfg, cost_model):
        a = _size(cfg, cost_model, atr=50.0).economics.net_risk
        b = _size(cfg, cost_model, atr=100.0).economics.net_risk
        assert b > a * 1.7

    def test_risk_scales_with_delta(self, cfg, cost_model):
        a = _size(cfg, cost_model, delta=0.35).economics.net_risk
        b = _size(cfg, cost_model, delta=0.70).economics.net_risk
        assert b > a * 1.7

    def test_concentration_is_reported_only_when_capital_is_declared(self, cfg, cost_model):
        assert _size(cfg, cost_model, capital=0.0).concentration_pct is None
        pct = _size(cfg, cost_model, capital=50_000.0).concentration_pct
        assert pct is not None and pct > 0.5


class TestAbsoluteRupeeLimits:
    """Limits are rupees, so the engine never needs to know the account size."""

    def test_shipped_limits_are_absolute(self, cfg):
        assert cfg.risk.daily_loss_limit_inr > 0
        assert cfg.risk.max_drawdown_inr > 0
        assert not hasattr(cfg.risk, "risk_pct_per_trade")
        assert not hasattr(cfg.risk, "max_deployment_pct")

    def test_drawdown_below_daily_limit_is_rejected(self):
        with pytest.raises(ValueError, match="max_drawdown_inr"):
            RiskCfg(daily_loss_limit_inr=6000, max_drawdown_inr=3000)

    def test_zero_daily_limit_is_rejected(self):
        with pytest.raises(ValueError, match="daily_loss_limit_inr"):
            RiskCfg(daily_loss_limit_inr=0)

    def test_lots_per_trade_must_be_positive(self):
        with pytest.raises(ValueError, match="lots_per_trade"):
            RiskCfg(lots_per_trade=0)

    def test_shipped_limits_are_a_whole_number_of_typical_losses(self, cfg, cost_model):
        """Sanity: the daily limit should allow at least one normal loss, or
        the system halts before it can ever trade."""
        typical = _size(cfg, cost_model, atr=80.0).economics.net_risk
        assert cfg.risk.daily_loss_limit_inr >= typical, (
            f"daily limit Rs {cfg.risk.daily_loss_limit_inr:,.0f} is below a "
            f"single typical loss of Rs {typical:,.0f}"
        )
        assert cfg.risk.max_drawdown_inr >= typical * 2
