"""Alpha engine: gate semantics, audit completeness, and end-to-end signalling."""

from __future__ import annotations

import datetime as dt
import warnings

import numpy as np
import pytest

from onelot.analytics.ivp import IVPercentileEngine, IVPResult
from onelot.analytics.regime import Regime, RegimeResult, compute_for_date
from onelot.broker.mock import MockBroker, SyntheticConfig, SyntheticMarket
from onelot.clock import IST, SessionClock, TradingCalendar
from onelot.costs import CostModel
from onelot.market.candles import CandleSeries
from onelot.market.chain import ChainCache
from onelot.strategy import gates as G
from onelot.strategy.alpha import AlphaEngine, EvaluationContext


def good_ivp(pct=20.0):
    return IVPResult(pct, pct, 0.14, 200, 60, True, "strict", True, "ok")


def bull(conf=0.9):
    return RegimeResult(Regime.BULL, conf, {"BULL": conf}, "hmm", "abc", "2026-08-19")


# ─────────────────────────── gate unit tests ──────────────────────────────

class TestGateSemantics:
    def test_there_is_no_capital_gate(self, cfg):
        """Capital is optional now: with one fixed lot there is nothing to
        size, so an undeclared account size cannot make the engine unsafe.
        A capital gate here would block every signal on the shipped config."""
        assert not hasattr(G, "gate_capital")

    def test_health_gate_carries_the_reason_through(self):
        r = G.gate_health(False, ["WebSocket down", "chain 40s stale"])
        assert not r.passed and "WebSocket down" in r.reason

    def test_session_gate_blocks_before_first_entry(self, cfg):
        cal = TradingCalendar({}, verified=True)
        win = SessionClock(cfg.session, cal).window_for(dt.date(2026, 8, 20))
        early = dt.datetime(2026, 8, 20, 9, 20, tzinfo=IST)
        assert not G.gate_session(early, win).passed

    def test_session_gate_blocks_after_last_entry(self, cfg):
        cal = TradingCalendar({}, verified=True)
        win = SessionClock(cfg.session, cal).window_for(dt.date(2026, 8, 20))
        late = dt.datetime(2026, 8, 20, 15, 0, tzinfo=IST)
        r = G.gate_session(late, win)
        assert not r.passed and "no_new_entry_after" in r.reason

    def test_session_gate_allows_the_middle_of_the_day(self, cfg):
        cal = TradingCalendar({}, verified=True)
        win = SessionClock(cfg.session, cal).window_for(dt.date(2026, 8, 20))
        assert G.gate_session(dt.datetime(2026, 8, 20, 11, 0, tzinfo=IST), win).passed

    def test_regime_gate_is_directional(self):
        assert G.gate_regime(bull(), "CE").passed
        assert not G.gate_regime(bull(), "PE").passed

    def test_chop_regime_blocks_both_sides_with_a_theta_reason(self):
        chop = RegimeResult(Regime.CHOP, 0.8, {}, "hmm")
        for d in ("CE", "PE"):
            r = G.gate_regime(chop, d)
            assert not r.passed and "theta" in r.reason

    def test_trend_gate_is_symmetric(self):
        assert G.gate_trend(54_100, 54_000, "CE").passed
        assert not G.gate_trend(53_900, 54_000, "CE").passed
        assert G.gate_trend(53_900, 54_000, "PE").passed
        assert not G.gate_trend(54_100, 54_000, "PE").passed

    def test_trend_gate_blocks_on_nan_rather_than_guessing(self):
        r = G.gate_trend(54_000, float("nan"), "CE")
        assert not r.passed and "warm-up" in r.reason

    def test_momentum_bands_are_side_specific(self, cfg):
        ce, pe = cfg.strategy.momentum.ce_band, cfg.strategy.momentum.pe_band
        assert G.gate_momentum(60.0, "CE", ce, pe).passed
        assert not G.gate_momentum(60.0, "PE", ce, pe).passed
        assert G.gate_momentum(40.0, "PE", ce, pe).passed
        assert not G.gate_momentum(75.0, "CE", ce, pe).passed

    def test_theta_gate_uses_expected_hold_not_overnight(self):
        """v1 defect B6: overnight theta is meaningless for an intraday system."""
        assert G.gate_theta(-8.0, 300.0, 1.5, 0.10).passed        # 4% burn
        assert not G.gate_theta(-40.0, 300.0, 1.5, 0.10).passed   # 20% burn

    def test_theta_gate_blocks_when_unavailable(self):
        assert not G.gate_theta(None, 300.0, 1.5, 0.10).passed

    def test_net_rr_gate(self):
        assert G.gate_net_rr(2.4, 2.0).passed
        assert not G.gate_net_rr(1.8, 2.0).passed

    def test_net_rr_gate_blocks_on_economics_warnings(self):
        r = G.gate_net_rr(3.0, 2.0, ["target does not clear costs"])
        assert not r.passed and "costs" in r.reason

    def test_market_gate_blocks_only_when_there_is_no_market(self):
        """There is no sizing gate. One lot, always. All that can stop a trade
        here is the absence of a two-sided market."""
        assert G.gate_market(1, None).passed
        r = G.gate_market(0, "no two-sided market (zero or crossed bid/ask)")
        assert not r.passed and "two-sided" in r.reason

    def test_expiry_day_entries_are_blocked_by_default(self):
        today = dt.date(2026, 8, 31)
        r = G.gate_expiry_safety("2026-08-31", today)
        assert not r.passed and "expires today" in r.reason
        assert G.gate_expiry_safety("2026-08-31", today, allow_expiry_day=True).passed

    def test_expired_contract_is_blocked(self):
        assert not G.gate_expiry_safety("2026-08-01", dt.date(2026, 8, 20)).passed

    def test_cooldown_gate(self):
        now = dt.datetime(2026, 8, 20, 11, 0, tzinfo=IST)
        assert G.gate_cooldown(None, now, 15).passed
        assert not G.gate_cooldown(now - dt.timedelta(minutes=5), now, 15).passed
        assert G.gate_cooldown(now - dt.timedelta(minutes=20), now, 15).passed

    def test_dedup_gate(self):
        assert G.gate_dedup(set(), "K1").passed
        assert not G.gate_dedup({"K1"}, "K1").passed

    def test_stale_chain_gate(self):
        assert G.gate_chain_fresh(4.0, 20.0).passed
        assert not G.gate_chain_fresh(45.0, 20.0).passed


class TestGateReport:
    def test_advisory_gates_never_block(self):
        rep = G.GateReport("CE")
        rep.add(G.GateResult("x", False, advisory=True))
        assert rep.passed

    def test_summary_lists_every_blocking_failure(self):
        rep = G.GateReport("CE")
        rep.add(G.GateResult("a", True))
        rep.add(G.GateResult("b", False, reason="nope"))
        rep.add(G.GateResult("c", False, reason="also nope"))
        assert rep.first_block == "b"
        assert "nope" in rep.summary() and "also nope" in rep.summary()


# ─────────────────────────── end to end ───────────────────────────────────

class SessionRunner:
    """Drives the alpha engine across a synthetic session."""

    def __init__(self, cfg, seed=7, sessions=200, drift=0.0, trend=0.006,
                 ivp_mode="strict"):
        self.cfg = cfg
        self.cm = CostModel(__import__("onelot.config", fromlist=["CostConfig"])
                            .CostConfig.load("config/costs.yaml"))
        self.alpha = AlphaEngine(cfg, self.cm)
        self.cal = TradingCalendar({}, verified=True)
        self.sclock = SessionClock(cfg.session, self.cal)
        self.ivpe = IVPercentileEngine(ivp_mode, 252, 60)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.m = SyntheticMarket(SyntheticConfig(
                seed=seed, sessions=sessions, drift_per_session=drift,
                trend_strength=trend))
        daily = self.m.daily_bars("index")
        self.ddates = [b.ts.date() for b in daily]
        self.dcloses = np.array([b.close for b in daily])
        self.model = None

    def run(self, day, **ctx_over):
        b = MockBroker(self.m, now=dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST))
        win = self.sclock.window_for(day)
        idx = CandleSeries("INDEX", 5, win.open_at, has_volume=False)
        for d in [x for x in self.m.session_dates if x < day][-8:]:
            idx.seed_from_history(self.m.bars(d, 5, "index"))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            reg, self.model = compute_for_date(self.ddates, self.dcloses, day,
                                               model=self.model)
        ivh = [self.m.atm_iv(dt.datetime.combine(d, dt.time(12, 0), tzinfo=IST))
               for d in self.m.session_dates if d < day]
        chain = ChainCache(b, "BANKNIFTY", b.expiry_date.isoformat(),
                           forward_price_fn=lambda: self.m.futures_price(b.now()))
        sigs, outcomes = [], []
        idx_c = b.instruments("BANKNIFTY")[0]
        t = win.open_at
        while t <= win.close_at:
            b.set_now(t)
            closed = []
            for q in b.quote([idx_c]).values():
                closed += idx.on_tick(t, q.ltp, None)
            closed += idx.mark_closed_through(t, self.cfg.data.bar_settle_delay_sec)
            if closed:
                chain.poll_once(b.expiry_dt, t)
                a = idx.arrays()
                base = {
                    "now": t, "bar_ts": closed[-1].ts, "session_window": win,
                    "health_may_signal": True, "health_reasons": [],
                    "index_high": a["high"], "index_low": a["low"],
                    "index_close": a["close"],
                    "chain_rows": chain.snapshot.rows if chain.snapshot else [],
                    "chain_age_sec": chain.age_seconds(t), "regime": reg,
                    "ivp": self.ivpe.compute(ivh, chain.atm_iv()),
                    "expiry": b.expiry_date.isoformat(), "lot_size": 35,
                    "risk_allowed": True, "risk_reason": "ok",
                    "risk_state": "ACTIVE", "open_positions": 0,
                    "last_signal_at": sigs[-1].created_at if sigs else None,
                    "recent_signal_keys": {s.contract_key for s in sigs}}
                base.update(ctx_over)
                out = self.alpha.evaluate(EvaluationContext(**base))
                outcomes.append(out)
                if out.signal:
                    sigs.append(out.signal)
            t += dt.timedelta(minutes=1)
        return sigs, outcomes


@pytest.fixture(scope="module")
def runner_bull():
    from onelot.config import AppConfig
    cfg = AppConfig.load()
    cfg = cfg.model_copy(update={
        "account": cfg.account.model_copy(update={"capital_inr": 1_000_000.0})})
    return SessionRunner(cfg)


@pytest.mark.slow
class TestEndToEnd:
    def test_the_system_actually_produces_signals(self, runner_bull):
        """v1 defect B2: filters that jointly reject everything are worse than
        useless, because the failure is silent."""
        total = 0
        for day in runner_bull.m.session_dates[-25:]:
            sigs, _ = runner_bull.run(day)
            total += len(sigs)
        assert total > 0, "the calibrated strategy must fire at least sometimes"

    def test_every_signal_is_fully_specified(self, runner_bull):
        for day in runner_bull.m.session_dates[-25:]:
            sigs, _ = runner_bull.run(day)
            for s in sigs:
                assert s.lots >= 1 and s.qty == s.lots * s.lot_size
                assert s.economics.entry_eff > 0
                assert s.economics.stop_level < s.economics.entry_eff
                assert s.economics.target_level > s.economics.entry_eff
                assert s.economics.entry_eff < s.economics.breakeven
                assert s.economics.rr_net >= runner_bull.cfg.strategy.exits.rr_net_min
                assert 0.60 <= abs(s.delta) <= 0.70
                assert s.expires_at > s.created_at
                assert s.alert_text()
            if sigs:
                return
        pytest.skip("no signals in the sampled window")

    def test_every_signal_is_exactly_one_lot(self, runner_bull):
        """The rule, end to end. There is no budget to exceed — one lot,
        always — so what matters is that nothing ever sizes differently."""
        seen = 0
        for day in runner_bull.m.session_dates[-25:]:
            sigs, _ = runner_bull.run(day)
            for s in sigs:
                assert s.lots == runner_bull.cfg.risk.lots_per_trade == 1
                assert s.qty == s.lot_size
                seen += 1
        if seen == 0:
            pytest.skip("no signals in the sampled window on this tape")

    def test_extreme_volatility_is_filtered_upstream_not_by_a_risk_gate(
            self, runner_bull):
        """Risk on one lot scales with ATR, and the only thing bounding it is
        the intraday chop filter's ATR-percentile band. If a signal fires, its
        ATR must have been inside that band."""
        cfg = runner_bull.cfg
        for day in runner_bull.m.session_dates[-25:]:
            sigs, _ = runner_bull.run(day)
            for s in sigs:
                assert s.chop is not None and s.chop.is_trending
                pct = s.chop.atr_percentile
                if not np.isnan(pct):
                    assert (cfg.strategy.intraday_chop.atr_percentile_min
                            <= pct
                            <= cfg.strategy.intraday_chop.atr_percentile_max)

    def test_put_signals_fire_on_a_bearish_tape(self, rich_cfg):
        """*** v1 defect A8 REGRESSION *** — a put must actually be signallable."""
        r = SessionRunner(rich_cfg, seed=19, sessions=200, drift=-0.005,
                          trend=0.004, ivp_mode="degraded")
        pes = []
        for day in r.m.session_dates[-30:]:
            sigs, _ = r.run(day)
            pes += [s for s in sigs if s.direction == "PE"]
        assert pes, "no PE signal was ever produced — defect A8 has regressed"
        s = pes[0]
        assert s.delta < 0, "put delta must be negative"
        assert not (0.60 <= s.delta <= 0.70), "v1's signed filter would reject this"
        assert 0.60 <= abs(s.delta) <= 0.70


class TestAuditCompleteness:
    """v1 short-circuited on the first failure, so a rejected bar explained nothing."""

    def test_all_gates_are_evaluated_even_after_one_fails(self, runner_bull):
        _, outcomes = runner_bull.run(runner_bull.m.session_dates[-1])
        assert outcomes
        for out in outcomes:
            names = {g["gate"] for g in out.all_gate_dicts()}
            for expected in ("health", "session", "risk_governor",
                             "CE:regime", "CE:trend", "CE:momentum", "CE:ivp",
                             "CE:contract", "PE:regime", "PE:trend"):
                assert expected in names, f"{expected} missing from the audit trail"

    def test_both_directions_are_always_evaluated(self, runner_bull):
        _, outcomes = runner_bull.run(runner_bull.m.session_dates[-1])
        for out in outcomes:
            assert {e.direction for e in out.evaluations} == {"CE", "PE"}

    def test_outcome_summary_is_human_readable(self, runner_bull):
        _, outcomes = runner_bull.run(runner_bull.m.session_dates[-1])
        assert all(isinstance(o.summary(), str) and o.summary() for o in outcomes)


@pytest.mark.slow
class TestBlockingConditions:
    def test_undeclared_capital_does_not_suppress_signals(self, cfg):
        """Regression: an earlier capital gate would have blocked every signal
        once capital became optional."""
        zero = cfg.model_copy(update={
            "account": cfg.account.model_copy(update={"capital_inr": 0.0})})
        r = SessionRunner(zero)
        for day in r.m.session_dates[-3:]:
            _, outcomes = r.run(day)
            assert all(o.blocked_by != "capital" for o in outcomes)

    def test_degraded_health_suppresses_signals(self, runner_bull):
        """INVARIANT I4."""
        for day in runner_bull.m.session_dates[-3:]:
            sigs, outcomes = runner_bull.run(
                day, health_may_signal=False, health_reasons=["WebSocket down"])
            assert not sigs
            assert all(o.blocked_by == "health" for o in outcomes)

    def test_risk_halt_suppresses_signals(self, runner_bull):
        for day in runner_bull.m.session_dates[-3:]:
            sigs, _ = runner_bull.run(day, risk_allowed=False,
                                      risk_reason="daily loss limit", risk_state="HALTED")
            assert not sigs

    def test_open_position_suppresses_new_signals(self, runner_bull):
        for day in runner_bull.m.session_dates[-3:]:
            sigs, _ = runner_bull.run(day, open_positions=1)
            assert not sigs

    def test_stale_chain_suppresses_signals(self, runner_bull):
        for day in runner_bull.m.session_dates[-3:]:
            sigs, _ = runner_bull.run(day, chain_age_sec=120.0)
            assert not sigs

    def test_strict_ivp_with_no_history_suppresses_signals(self, rich_cfg):
        r = SessionRunner(rich_cfg, ivp_mode="strict")
        empty = IVPResult(float("nan"), float("nan"), 0.14, 0, 60, False,
                          "strict", False, "no ATM IV history recorded yet")
        for day in r.m.session_dates[-3:]:
            sigs, _ = r.run(day, ivp=empty)
            assert not sigs
