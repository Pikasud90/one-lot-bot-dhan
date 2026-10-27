"""Risk governor — the six kill switches v1 had none of (defect A5)."""

from __future__ import annotations

import datetime as dt

import pytest

from onelot.clock import now_ist
from onelot.risk.governor import RiskGovernor, RiskState
from onelot.store import init_database
from onelot.store.repo import Repo


@pytest.fixture()
def repo(tmp_path):
    db = init_database(f"sqlite:///{tmp_path / 'risk.db'}")
    yield Repo(db)
    db.dispose()


CAPITAL = 1_000_000.0


def gov(cfg, repo=None, capital=CAPITAL, **risk_over):
    risk = cfg.risk.model_copy(update=risk_over) if risk_over else cfg.risk
    return RiskGovernor(risk, capital, "PAPER", "2026-08-20", repo)


def daily_limit(cfg, capital=CAPITAL):
    return cfg.risk.daily_loss_limit_inr


def dd_limit(cfg, capital=CAPITAL):
    return cfg.risk.max_drawdown_inr


class TestSwitches:
    def test_fresh_governor_permits_trading(self, cfg):
        ok, why = gov(cfg).may_open()
        assert ok, why

    def test_daily_loss_limit_halts(self, cfg):
        g = gov(cfg)
        g.record_close(-(daily_limit(cfg) + 1_000.0))
        ok, why = g.may_open()
        assert not ok and "daily loss limit" in why
        assert g.state is RiskState.HALTED

    def test_daily_trade_cap(self, cfg):
        g = gov(cfg, daily_loss_limit_inr=500_000.0, max_drawdown_inr=900_000.0)
        for _ in range(cfg.risk.max_trades_per_day):
            g.record_open()
            g.record_close(500.0)
        ok, why = g.may_open()
        assert not ok and "trade cap" in why

    def test_consecutive_losses_halt(self, cfg):
        g = gov(cfg)
        for _ in range(cfg.risk.max_consecutive_losses):
            g.record_close(-1_000.0)
        ok, why = g.may_open()
        assert not ok and "consecutive losses" in why

    def test_a_win_resets_the_consecutive_loss_counter(self, cfg):
        g = gov(cfg)
        g.record_close(-1_000.0)
        assert g.consecutive_losses == 1
        g.record_close(2_000.0)
        assert g.consecutive_losses == 0
        assert g.may_open()[0]

    def test_max_open_positions(self, cfg):
        """The shipped config allows 1 trade/day, which would bind first, so
        raise the trade cap to isolate the concurrency switch."""
        g = gov(cfg, max_trades_per_day=5, daily_loss_limit_inr=500_000.0,
                max_drawdown_inr=900_000.0)
        g.record_open()
        ok, why = g.may_open()
        assert not ok and "max concurrent positions" in why

    def test_undeclared_capital_does_not_block(self, cfg):
        """Capital is informational now — limits are absolute rupees."""
        assert gov(cfg, capital=0.0).may_open()[0]

    def test_manual_panic_halt(self, cfg):
        g = gov(cfg)
        g.manual_panic_halt()
        ok, why = g.may_open()
        assert not ok and "MANUAL HALT" in why


class TestHaltStickiness:
    """A kill switch you can clear by restarting is not a kill switch."""

    def test_halt_survives_a_restart(self, cfg, repo):
        g1 = gov(cfg, repo)
        g1.record_close(-(daily_limit(cfg) + 1_000.0))
        assert g1.state is RiskState.HALTED

        g2 = gov(cfg, repo)                # simulate the engine restarting
        assert g2.state is RiskState.HALTED
        assert not g2.may_open()[0]
        assert "daily loss" in (g2.halt_reason or "")

    def test_manual_halt_survives_a_restart(self, cfg, repo):
        gov(cfg, repo).manual_panic_halt()
        g2 = gov(cfg, repo)
        assert g2.manual_halt and g2.state is RiskState.HALTED

    def test_only_an_explicit_reset_clears_a_halt(self, cfg, repo):
        g = gov(cfg, repo)
        g.manual_panic_halt()
        g.reset_halt()
        assert g.state is RiskState.ACTIVE
        assert gov(cfg, repo).state is RiskState.ACTIVE

    def test_realized_pnl_and_counts_persist(self, cfg, repo):
        g = gov(cfg, repo)
        g.record_open()
        g.record_close(-4_000.0)
        g2 = gov(cfg, repo)
        assert g2.realized_pnl == pytest.approx(-4_000.0)
        assert g2.trades_taken == 1
        assert g2.consecutive_losses == 1


class TestSnapshot:
    def test_snapshot_exposes_everything_the_dashboard_needs(self, cfg):
        g = gov(cfg)
        g.record_open()
        s = g.snapshot().as_dict()
        assert s["state"] == "ACTIVE"
        assert s["open_positions"] == 1
        assert s["limits"]["daily_loss_limit"] == pytest.approx(daily_limit(cfg))
        assert s["limits"]["max_trades_per_day"] == cfg.risk.max_trades_per_day

    def test_halt_records_a_timestamp(self, cfg):
        g = gov(cfg)
        g.halt("because", now=now_ist())
        assert isinstance(g.halted_at, dt.datetime)


class TestCumulativeDrawdown:
    """A seventh kill switch, added because the replay harness exposed the gap:
    the daily limit alone does not bound total loss. Thirty consecutive days at
    -3% is -60% of capital without ever breaching a single daily limit."""

    def _lose(self, g, repo, amount, n, start_day=1):
        """Record `n` losing trades of `amount` on separate days."""
        from onelot.clock import now_ist
        for i in range(n):
            repo.insert_journal({
                "trade_id": f"T{start_day + i}", "position_id": f"P{start_day + i}",
                "trade_date": f"2026-08-{start_day + i:02d}",
                "closed_at": now_ist() + dt.timedelta(days=i),
                "direction": "CE", "strike": 54000.0, "opt_type": "CE",
                "lots": 1, "qty": 35, "entry_price": 300.0, "exit_price": 280.0,
                "gross_pnl": -amount, "total_cost": 0.0, "net_pnl": -amount,
                "r_multiple": -1.0, "exit_reason": "HARD_SL", "hold_minutes": 20.0,
                "mode": "PAPER",
            })

    def test_drawdown_limit_halts_across_sessions(self, cfg, repo):
        """The exact scenario the daily limit cannot see."""
        each = (dd_limit(cfg) / 5.0) * 1.2            # 5 days, 20% past the limit
        self._lose(None, repo, each, 5)
        g = gov(cfg, repo)
        assert g.drawdown == pytest.approx(each * 5)
        assert g.drawdown > dd_limit(cfg)
        ok, why = g.may_open()
        assert not ok and "CUMULATIVE DRAWDOWN" in why

    def test_within_the_limit_still_trades(self, cfg, repo):
        self._lose(None, repo, (dd_limit(cfg) / 5.0) * 0.5, 5)   # half the limit
        g = gov(cfg, repo)
        assert g.may_open()[0]

    def test_drawdown_measures_from_the_peak_not_from_zero(self, cfg, repo):
        """Profit first, then a drawdown from that higher peak. Uses absolute
        figures deliberately: the point is the arithmetic of peak-to-trough."""
        from onelot.clock import now_ist
        peak = dd_limit(cfg) * 5          # a big win first
        dip = dd_limit(cfg) * 0.5          # then a dip well inside the limit
        repo.insert_journal({
            "trade_id": "W1", "position_id": "PW1", "trade_date": "2026-08-01",
            "closed_at": now_ist(), "direction": "CE", "strike": 1.0,
            "opt_type": "CE", "lots": 1, "qty": 35, "entry_price": 1.0,
            "exit_price": 2.0, "gross_pnl": peak, "total_cost": 0.0,
            "net_pnl": peak, "r_multiple": 2.0, "exit_reason": "TARGET",
            "hold_minutes": 20.0, "mode": "PAPER"})
        self._lose(None, repo, dip / 3.0, 3, start_day=2)
        g = gov(cfg, repo)
        assert g.peak_equity == pytest.approx(peak)
        assert g.cumulative_pnl == pytest.approx(peak - dip)
        assert g.drawdown == pytest.approx(dip)
        assert g.may_open()[0], "still net positive and inside the drawdown limit"

    def test_live_close_updates_the_drawdown(self, cfg, repo):
        g = gov(cfg, repo)
        step = dd_limit(cfg) * 0.4
        g.record_close(-step)
        assert g.drawdown == pytest.approx(step)
        g.record_close(-step)
        assert g.drawdown == pytest.approx(step * 2), "drawdown must accumulate"
        g.record_close(-step)
        assert g.drawdown > dd_limit(cfg)
        assert not g.may_open()[0]

    def test_reset_rebases_so_the_next_loss_does_not_instantly_retrip(self, cfg, repo):
        """A reset that kept the old peak would be useless: the very next
        rupee of loss would breach the same limit again."""
        g = gov(cfg, repo)
        g.record_close(-(dd_limit(cfg) + 500.0))
        assert not g.may_open()[0]
        g.reset_halt()
        assert g.drawdown == pytest.approx(0.0)
        g.record_close(-100.0)
        assert g.drawdown == pytest.approx(100.0), (
            "drawdown after a reset is measured from the NEW peak")

    def test_drawdown_halt_survives_a_restart(self, cfg, repo):
        self._lose(None, repo, (dd_limit(cfg) / 5.0) * 1.2, 5)
        gov(cfg, repo).may_open()          # trips the halt and persists it
        assert not gov(cfg, repo).may_open()[0]

    def test_config_rejects_a_drawdown_limit_below_the_daily_limit(self):
        from onelot.config import RiskCfg
        with pytest.raises(ValueError, match="max_drawdown_pct"):
            RiskCfg(daily_loss_limit_pct=0.03, max_drawdown_pct=0.01)

    def test_limits_are_reported_to_the_dashboard(self, cfg):
        limits = gov(cfg).snapshot().as_dict()["limits"]
        assert limits["max_drawdown_limit"] == pytest.approx(dd_limit(cfg))
        assert limits["lots_per_trade"] == cfg.risk.lots_per_trade


class TestStandingHaltsOutliveTheSession:
    """Regression: halt state was keyed by TRADE DATE, so a new day loaded no
    row, started ACTIVE, and the cumulative drawdown halt silently cleared
    itself overnight. A cumulative limit that resets daily is not a cumulative
    limit. Only the DAILY loss limit may reset with the date."""

    def _gov_on(self, cfg, repo, day):
        return RiskGovernor(cfg.risk, CAPITAL, "PAPER", day, repo)

    def test_drawdown_halt_survives_into_the_next_session(self, cfg, repo):
        g1 = self._gov_on(cfg, repo, "2026-08-20")
        g1.record_close(-(dd_limit(cfg) + 500.0))
        assert g1.state is RiskState.HALTED

        g2 = self._gov_on(cfg, repo, "2026-08-21")      # a NEW trading day
        assert g2.state is RiskState.HALTED, "the halt cleared overnight"
        assert not g2.may_open()[0]
        assert "DRAWDOWN" in (g2.halt_reason or "").upper()

    def test_manual_halt_survives_into_the_next_session(self, cfg, repo):
        self._gov_on(cfg, repo, "2026-08-20").manual_panic_halt()
        g2 = self._gov_on(cfg, repo, "2026-08-21")
        assert g2.state is RiskState.HALTED and g2.manual_halt

    def test_consecutive_loss_halt_survives_into_the_next_session(self, cfg, repo):
        g1 = self._gov_on(cfg, repo, "2026-08-20")
        for _ in range(cfg.risk.max_consecutive_losses):
            g1.record_close(-100.0)
        assert g1.state is RiskState.HALTED
        assert self._gov_on(cfg, repo, "2026-08-21").state is RiskState.HALTED

    def test_the_daily_limit_DOES_reset_with_the_date(self, cfg, repo):
        """The one halt that should not be standing."""
        g1 = self._gov_on(cfg, repo, "2026-08-20")
        # A loss big enough for the daily limit but inside the drawdown limit.
        g1.record_close(-(cfg.risk.daily_loss_limit_inr + 100.0))
        assert g1.state is RiskState.HALTED
        assert not g1._standing_halt, "a daily halt must not be standing"

        g2 = self._gov_on(cfg, repo, "2026-08-21")
        assert g2.state is RiskState.ACTIVE, "the new day should start fresh"
        assert g2.may_open()[0]

    def test_a_standing_halt_still_binds_even_on_a_fresh_day(self, cfg, repo):
        """Daily reset must not resurrect trading through a standing halt."""
        g1 = self._gov_on(cfg, repo, "2026-08-20")
        g1.manual_panic_halt()
        g2 = self._gov_on(cfg, repo, "2026-08-25")
        assert not g2.may_open()[0]

    def test_operator_reset_clears_it_for_good(self, cfg, repo):
        g1 = self._gov_on(cfg, repo, "2026-08-20")
        g1.record_close(-(dd_limit(cfg) + 500.0))
        g1.reset_halt()
        assert g1.state is RiskState.ACTIVE
        g2 = self._gov_on(cfg, repo, "2026-08-21")
        assert g2.state is RiskState.ACTIVE and g2.may_open()[0]

    def test_reset_rebases_the_drawdown_peak(self, cfg, repo):
        """Otherwise the very next loss re-trips the same halt immediately.

        Note the DAILY limit is a separate matter: after a large loss today
        you stay halted for today even once the standing halt is cleared, and
        that is correct — the day's loss is a fact, not an opinion.
        """
        g = self._gov_on(cfg, repo, "2026-08-20")
        g.record_close(-(dd_limit(cfg) + 500.0))
        g.reset_halt()
        assert g.drawdown == pytest.approx(0.0)

        # Tomorrow, with the day's realised loss reset, trading resumes.
        g2 = self._gov_on(cfg, repo, "2026-08-21")
        assert g2.state is RiskState.ACTIVE and g2.may_open()[0]
