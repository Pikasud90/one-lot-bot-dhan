"""Trade lifecycle: idempotent acks, crash recovery, Guardian, journal."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from onelot.clock import IST, SessionClock, TradingCalendar
from onelot.costs import Quote
from onelot.risk.governor import RiskGovernor
from onelot.store import init_database
from onelot.store.repo import Repo
from onelot.trade.guardian import Guardian, GuardianInputs, HoldConfidence, check_entry_slippage
from onelot.trade.journal import TradeJournal
from onelot.trade.state import ExitReason, PositionState, SignalStatus, TradeStateMachine

NOW = dt.datetime(2026, 8, 20, 11, 0, tzinfo=IST)


@pytest.fixture()
def db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'trade.db'}"


@pytest.fixture()
def repo(db_url):
    db = init_database(db_url)
    yield Repo(db)
    db.dispose()


@pytest.fixture()
def sm(repo, rich_cfg, cost_model):
    g = RiskGovernor(rich_cfg.risk, rich_cfg.account.capital_inr, "PAPER", "2026-08-20", repo)
    return TradeStateMachine(repo, cost_model, rich_cfg, g)


def make_signal_row(repo, sid="SIG1", entry=310.0, stop=285.0, target=375.0, created=NOW, ttl=75):
    repo.insert_signal(
        {
            "signal_id": sid,
            "created_at": created,
            "expires_at": created + dt.timedelta(seconds=ttl),
            "direction": "CE",
            "underlying": "BANKNIFTY",
            "expiry": "2026-08-31",
            "strike": 54000.0,
            "opt_type": "CE",
            "security_id": "S1",
            "quote_bid": 308.0,
            "quote_ask": 310.0,
            "quote_ltp": 309.0,
            "entry_eff": entry,
            "stop_level": stop,
            "target_level": target,
            "breakeven": 313.0,
            "lots": 2,
            "qty": 70,
            "lot_size": 35,
            "net_risk": 1800.0,
            "net_reward": 4400.0,
            "rr_net": 2.44,
            "delta": 0.65,
            "iv": 0.14,
            "theta_per_hour": -0.5,
            "status": "PENDING",
        }
    )
    return sid


class TestIdempotentAcknowledgement:
    """*** v1 defect D7 *** — two UIs, two sources of truth, possible double trade."""

    def test_double_click_creates_exactly_one_position(self, sm, repo):
        make_signal_row(repo)
        a = sm.acknowledge("SIG1", "TAKE", "dashboard", now=NOW)
        b = sm.acknowledge("SIG1", "TAKE", "dashboard", now=NOW)
        assert a.accepted and not b.accepted
        assert "duplicate" in b.message
        assert len(sm.open_positions) == 1

    def test_two_clients_racing_produce_one_position(self, sm, repo):
        make_signal_row(repo)
        results = [
            sm.acknowledge("SIG1", "TAKE", src, now=NOW)
            for src in ("dashboard", "telegram", "webhook")
        ]
        assert sum(r.accepted for r in results) == 1
        assert len(sm.open_positions) == 1

    def test_conflicting_actions_cannot_both_apply(self, sm, repo):
        make_signal_row(repo)
        assert sm.acknowledge("SIG1", "TAKE", now=NOW).accepted
        assert not sm.acknowledge("SIG1", "SKIP", now=NOW).accepted
        assert repo.get_signal("SIG1")["status"] == SignalStatus.TAKEN.value

    def test_skip_records_and_opens_nothing(self, sm, repo):
        make_signal_row(repo)
        r = sm.acknowledge("SIG1", "SKIP", now=NOW)
        assert r.accepted and r.status is SignalStatus.SKIPPED
        assert not sm.open_positions

    def test_expired_signal_cannot_be_taken(self, sm, repo):
        make_signal_row(repo)
        late = NOW + dt.timedelta(seconds=120)
        r = sm.acknowledge("SIG1", "TAKE", now=late)
        assert not r.accepted and r.status is SignalStatus.EXPIRED
        assert not sm.open_positions

    def test_unknown_signal_is_rejected(self, sm):
        assert not sm.acknowledge("NOPE", "TAKE", now=NOW).accepted

    def test_risk_halt_refuses_the_ack(self, sm, repo):
        make_signal_row(repo)
        sm.governor.manual_panic_halt()
        r = sm.acknowledge("SIG1", "TAKE", now=NOW)
        assert not r.accepted and "risk governor refused" in r.message
        assert not sm.open_positions


class TestFillPrice:
    def test_actual_fill_is_recorded_not_the_signalled_price(self, sm, repo):
        """You rarely fill where the alert said. The system tracks reality."""
        make_signal_row(repo, entry=310.0, stop=285.0)
        r = sm.acknowledge("SIG1", "TAKE", fill_price=313.5, now=NOW)
        assert r.position.entry_price == pytest.approx(313.5)

    def test_the_stop_travels_with_the_fill_preserving_R(self, sm, repo):
        """A worse fill must not silently widen your risk."""
        make_signal_row(repo, entry=310.0, stop=285.0)
        r = sm.acknowledge("SIG1", "TAKE", fill_price=313.5, now=NOW)
        p = r.position
        assert p.r_per_unit == pytest.approx(25.0)
        assert p.initial_stop == pytest.approx(288.5)
        assert p.entry_price - p.initial_stop == pytest.approx(25.0)

    def test_target_scales_from_the_actual_fill(self, sm, repo, rich_cfg):
        make_signal_row(repo, entry=310.0, stop=285.0)
        p = sm.acknowledge("SIG1", "TAKE", fill_price=313.5, now=NOW).position
        expected = 313.5 + rich_cfg.strategy.exits.rr_target_gross * 25.0
        assert p.target == pytest.approx(expected)

    def test_fill_vs_signal_drift_is_recorded(self, sm, repo):
        make_signal_row(repo, entry=310.0)
        p = sm.acknowledge("SIG1", "TAKE", fill_price=313.5, now=NOW).position
        assert p.payload["fill_vs_signal"] == pytest.approx(3.5)


class TestCrashRecovery:
    """*** v1 defect D5 *** — a restart lost the position entirely."""

    def test_open_position_is_recovered_after_a_restart(self, db_url, rich_cfg, cost_model):
        db = init_database(db_url)
        repo = Repo(db)
        g = RiskGovernor(rich_cfg.risk, 1_000_000.0, "PAPER", "2026-08-20", repo)
        sm = TradeStateMachine(repo, cost_model, rich_cfg, g)
        make_signal_row(repo)
        pos = sm.acknowledge("SIG1", "TAKE", fill_price=312.0, now=NOW).position
        sm.update_trail(pos.position_id, trail_stop=300.0, trail_active=True, high_water=330.0)
        db.dispose()

        db2 = init_database(db_url)  # engine restarts
        repo2 = Repo(db2)
        g2 = RiskGovernor(rich_cfg.risk, 1_000_000.0, "PAPER", "2026-08-20", repo2)
        sm2 = TradeStateMachine(repo2, cost_model, rich_cfg, g2)
        recovered = sm2.recover()

        assert len(recovered) == 1
        r = recovered[0]
        assert r.entry_price == pytest.approx(312.0)
        assert r.trail_stop == pytest.approx(300.0)
        assert r.trail_active is True
        assert r.effective_stop == pytest.approx(300.0)
        assert g2.open_positions == 1, "the governor must know a position is live"
        db2.dispose()

    def test_closed_positions_are_not_resurrected(self, sm, repo):
        make_signal_row(repo)
        p = sm.acknowledge("SIG1", "TAKE", now=NOW).position
        sm.confirm_exit(p.position_id, 340.0, ExitReason.TARGET, NOW)
        assert sm.recover() == []


class TestExitAndJournal:
    def test_exit_writes_a_net_of_cost_journal_entry(self, sm, repo, cost_model):
        make_signal_row(repo)
        p = sm.acknowledge("SIG1", "TAKE", fill_price=310.0, now=NOW).position
        entry = sm.confirm_exit(
            p.position_id, 340.0, ExitReason.TARGET, NOW + dt.timedelta(minutes=25)
        )
        assert entry["gross_pnl"] == pytest.approx(30.0 * 70)
        assert entry["total_cost"] > 0
        assert entry["net_pnl"] == pytest.approx(entry["gross_pnl"] - entry["total_cost"])
        assert entry["net_pnl"] < entry["gross_pnl"]
        assert entry["r_multiple"] == pytest.approx(30.0 / 25.0, abs=1e-3)
        assert entry["hold_minutes"] == pytest.approx(25.0)

    def test_exit_feeds_the_risk_governor(self, sm, repo):
        make_signal_row(repo)
        p = sm.acknowledge("SIG1", "TAKE", fill_price=310.0, now=NOW).position
        sm.confirm_exit(p.position_id, 280.0, ExitReason.HARD_SL, NOW)
        assert sm.governor.realized_pnl < 0
        assert sm.governor.consecutive_losses == 1
        assert sm.governor.open_positions == 0

    def test_position_state_reaches_closed(self, sm, repo):
        make_signal_row(repo)
        p = sm.acknowledge("SIG1", "TAKE", now=NOW).position
        sm.confirm_exit(p.position_id, 340.0, ExitReason.TARGET, NOW)
        assert repo.get_position(p.position_id)["state"] == PositionState.CLOSED.value

    def test_double_exit_is_a_noop(self, sm, repo):
        make_signal_row(repo)
        p = sm.acknowledge("SIG1", "TAKE", now=NOW).position
        assert sm.confirm_exit(p.position_id, 340.0, ExitReason.TARGET, NOW)
        assert sm.confirm_exit(p.position_id, 999.0, ExitReason.TARGET, NOW) is None


# ─────────────────────────── Guardian ─────────────────────────────────────


def bars(n=60, start=54000.0, step=20.0, seed=3):
    rng = np.random.default_rng(seed)
    c = start + np.cumsum(np.full(n, step) + rng.normal(0, 6, n))
    return c + 25, c - 25, c


def guardian_inputs(
    bid,
    ask,
    *,
    now=NOW,
    with_futures=True,
    window=None,
    regime=None,
    degraded=False,
    n=60,
    step=20.0,
):
    h, l_, c = bars(n=n, step=step)
    kw = {
        "now": now,
        "quote": Quote(bid=bid, ask=ask, ltp=(bid + ask) / 2),
        "index_high": h,
        "index_low": l_,
        "index_close": c,
        "session_window": window,
        "regime_direction": regime,
        "data_degraded": degraded,
    }
    if with_futures:
        rng = np.random.default_rng(9)
        kw.update(
            futures_high=h + 40,
            futures_low=l_ + 40,
            futures_close=c + 40,
            futures_volume=rng.uniform(4000, 9000, len(c)),
        )
    return GuardianInputs(**kw)


@pytest.fixture()
def guardian(rich_cfg, cost_model):
    return Guardian(rich_cfg, cost_model)


@pytest.fixture()
def position(sm, repo):
    make_signal_row(repo, entry=310.0, stop=285.0, target=375.0)
    p = sm.acknowledge("SIG1", "TAKE", fill_price=310.0, now=NOW).position
    p.payload["delta"] = 0.65
    return p


class TestGuardianStops:
    def test_stop_is_tested_against_the_bid_not_ltp(self, guardian, position):
        """v1 defect A2: LTP can sit above a bid that has already broken the stop."""
        d = guardian.evaluate(position, guardian_inputs(bid=284.0, ask=292.0))
        assert d.is_exit and d.reason is ExitReason.HARD_SL
        assert d.details["exit_price_basis"] == "bid"

    def test_hold_when_comfortably_above_the_stop(self, guardian, position):
        d = guardian.evaluate(position, guardian_inputs(bid=320.0, ask=322.0))
        assert not d.is_exit

    def test_target_exit_uses_the_bid(self, guardian, position):
        d = guardian.evaluate(position, guardian_inputs(bid=376.0, ask=378.0))
        assert d.is_exit and d.reason is ExitReason.TARGET

    def test_trail_does_not_activate_below_one_R(self, guardian, position):
        d = guardian.evaluate(position, guardian_inputs(bid=320.0, ask=322.0))
        assert not d.trail_active, "310 -> 320 is 0.4R, below the 1R activation"

    def test_trail_activates_at_one_R(self, guardian, position):
        d = guardian.evaluate(position, guardian_inputs(bid=336.0, ask=338.0))
        assert d.trail_active, "310 -> 336 is >1R (R = 25)"

    def test_trail_ratchets_and_never_falls(self, guardian, position):
        """A trailing stop that can move against you is not a stop."""
        d1 = guardian.evaluate(position, guardian_inputs(bid=360.0, ask=362.0))
        assert d1.trail_active and d1.trail_stop is not None
        position.trail_stop, position.trail_active = d1.trail_stop, True
        position.high_water_premium = d1.high_water

        d2 = guardian.evaluate(position, guardian_inputs(bid=340.0, ask=342.0))
        assert d2.trail_stop >= d1.trail_stop, "the trail moved DOWN"

    def test_trail_never_goes_below_the_initial_stop(self, guardian, position):
        d = guardian.evaluate(position, guardian_inputs(bid=336.0, ask=338.0))
        assert d.trail_stop >= position.initial_stop

    def test_breached_trail_exits_as_trail_sl(self, guardian, position):
        position.trail_active = True
        position.trail_stop = 330.0
        d = guardian.evaluate(position, guardian_inputs(bid=329.0, ask=331.0))
        assert d.is_exit and d.reason is ExitReason.TRAIL_SL


class TestGuardianTimeAndSession:
    def test_time_stop_fires_when_nothing_happens(self, guardian, position, rich_cfg):
        """v1 defect A7: a position going sideways is bleeding theta, not flat."""
        later = NOW + dt.timedelta(minutes=rich_cfg.strategy.exits.time_stop_minutes + 1)
        d = guardian.evaluate(position, guardian_inputs(bid=312.0, ask=314.0, now=later))
        assert d.is_exit and d.reason is ExitReason.TIME_STOP

    def test_time_stop_does_not_fire_on_a_winning_trade(self, guardian, position, rich_cfg):
        later = NOW + dt.timedelta(minutes=rich_cfg.strategy.exits.time_stop_minutes + 1)
        d = guardian.evaluate(position, guardian_inputs(bid=345.0, ask=347.0, now=later))
        assert not d.is_exit or d.reason is not ExitReason.TIME_STOP

    def test_mandatory_squareoff_overrides_everything(self, guardian, position, cfg):
        """v1 defect A3: v1 never said 'close the position before expiry'."""
        win = SessionClock(cfg.session, TradingCalendar({}, verified=True)).window_for(
            dt.date(2026, 8, 20)
        )
        at = dt.datetime(2026, 8, 20, 15, 20, tzinfo=IST)
        d = guardian.evaluate(position, guardian_inputs(bid=330.0, ask=332.0, now=at, window=win))
        assert d.is_exit and d.reason is ExitReason.EOD_SQUAREOFF
        assert "SQUARE-OFF" in d.message

    def test_eod_advisory_warns_before_it_forces(self, guardian, position, cfg):
        win = SessionClock(cfg.session, TradingCalendar({}, verified=True)).window_for(
            dt.date(2026, 8, 20)
        )
        at = dt.datetime(2026, 8, 20, 15, 12, tzinfo=IST)
        # Open the position recently: a 4-hour-old position would (correctly)
        # trip the time stop first, which outranks an advisory.
        position.opened_at = dt.datetime(2026, 8, 20, 15, 5, tzinfo=IST)
        d = guardian.evaluate(position, guardian_inputs(bid=330.0, ask=332.0, now=at, window=win))
        assert d.action == "WARN" and not d.is_exit

    def test_real_exits_outrank_the_eod_advisory(self, guardian, position, cfg):
        """Priority order matters: a breached stop must not be downgraded to a
        'plan your exit' warning just because it is late in the session."""
        win = SessionClock(cfg.session, TradingCalendar({}, verified=True)).window_for(
            dt.date(2026, 8, 20)
        )
        at = dt.datetime(2026, 8, 20, 15, 12, tzinfo=IST)
        position.opened_at = dt.datetime(2026, 8, 20, 15, 5, tzinfo=IST)
        d = guardian.evaluate(position, guardian_inputs(bid=280.0, ask=284.0, now=at, window=win))
        assert d.is_exit and d.reason is ExitReason.HARD_SL

    def test_regime_flip_exits(self, guardian, position):
        # NOTE: this test originally passed regime="PE" and went GREEN against
        # broken code, because it repeated the code's own domain confusion --
        # a REGIME label is BULL/BEAR/CHOP, never an option side. The regime
        # that opposes a long CALL is BEAR.
        d = guardian.evaluate(position, guardian_inputs(bid=330.0, ask=332.0, regime="BEAR"))
        assert d.is_exit and d.reason is ExitReason.REGIME_FLIP


class TestGuardianVolumeDependence:
    """*** v1 defect B3 *** — the index has no volume."""

    def test_vwap_is_unavailable_without_futures(self, guardian, position):
        d = guardian.evaluate(position, guardian_inputs(bid=330.0, ask=332.0, with_futures=False))
        assert d.details["vwap"] is None
        assert "futures" in d.details["vwap_note"]

    def test_vwap_is_computed_from_futures(self, guardian, position):
        d = guardian.evaluate(position, guardian_inputs(bid=330.0, ask=332.0))
        assert d.details["vwap"] is not None
        assert "vwap_supports" in d.details

    def test_divergence_is_skipped_without_futures_volume(self, guardian, position):
        d = guardian.evaluate(position, guardian_inputs(bid=330.0, ask=332.0, with_futures=False))
        assert "no futures volume" in d.details["divergence"]
        assert d.reason is not ExitReason.DIVERGENCE

    def test_hold_confidence_is_reported(self, guardian, position):
        d = guardian.evaluate(position, guardian_inputs(bid=330.0, ask=332.0))
        assert d.hold_confidence in (
            HoldConfidence.HIGH,
            HoldConfidence.MEDIUM,
            HoldConfidence.LOW,
            HoldConfidence.UNKNOWN,
        )

    def test_hold_confidence_never_forces_an_exit(self, guardian, position):
        """Advisory means advisory. Only stops close positions."""
        d = guardian.evaluate(position, guardian_inputs(bid=330.0, ask=332.0, step=-20.0))
        if d.hold_confidence == HoldConfidence.LOW:
            assert not d.is_exit

    def test_degraded_data_warns_but_keeps_managing(self, guardian, position):
        d = guardian.evaluate(position, guardian_inputs(bid=330.0, ask=332.0, degraded=True))
        assert d.action == "WARN" and not d.is_exit


class TestEntrySlippage:
    """v1 defect A2: v1 compared LTP with a flat 3% band."""

    def test_small_drift_is_within_tolerance(self):
        c = check_entry_slippage(310.0, Quote(bid=309.0, ask=311.5), 0.03, 1.5)
        assert c.within_tolerance

    def test_large_drift_is_flagged(self):
        c = check_entry_slippage(310.0, Quote(bid=340.0, ask=342.0), 0.03, 1.5)
        assert not c.within_tolerance and "moved" in c.message

    def test_tolerance_widens_with_the_spread(self):
        """A pure 3% band rejects valid fills on wide ITM strikes."""
        tight = check_entry_slippage(310.0, Quote(bid=309.0, ask=311.0), 0.03, 1.5)
        wide = check_entry_slippage(310.0, Quote(bid=290.0, ask=320.0), 0.03, 1.5)
        assert wide.tolerance > tight.tolerance

    def test_it_compares_the_ask_because_you_buy_the_ask(self):
        c = check_entry_slippage(310.0, Quote(bid=300.0, ask=318.0), 0.03, 0.0)
        assert c.live == pytest.approx(318.0)


class TestJournalMetrics:
    def _trade(self, sm, repo, sid, entry, exit_px, reason=ExitReason.TARGET):
        make_signal_row(repo, sid=sid, entry=entry, stop=entry - 25, target=entry + 62)
        p = sm.acknowledge(sid, "TAKE", fill_price=entry, now=NOW).position
        assert p is not None, "acknowledge was refused — check the risk governor"
        out = sm.confirm_exit(p.position_id, exit_px, reason, NOW + dt.timedelta(minutes=20))
        # These cases exercise the JOURNAL, not the governor: reset the
        # per-day counters so the shipped 1-trade-per-day cap does not stop
        # the second trade in a two-trade scenario.
        sm.governor.state = sm.governor.state.__class__.ACTIVE
        sm.governor.consecutive_losses = 0
        sm.governor.trades_taken = 0
        sm.governor.realized_pnl = 0.0
        return out

    def test_metrics_are_net_of_costs(self, sm, repo):
        self._trade(sm, repo, "S1", 310.0, 350.0)
        m = TradeJournal(repo).metrics()
        assert m["trades"] == 1
        assert m["net_pnl"] < m["gross_pnl"]
        assert m["total_costs"] > 0
        assert 0 < m["costs_pct_of_gross"] < 1

    def test_empty_journal_is_handled(self, repo):
        assert TradeJournal(repo).metrics()["trades"] == 0

    def test_exit_reasons_are_attributed(self, sm, repo):
        self._trade(sm, repo, "S1", 310.0, 350.0, ExitReason.TARGET)
        self._trade(sm, repo, "S2", 300.0, 275.0, ExitReason.HARD_SL)
        by = TradeJournal(repo).metrics()["by_exit_reason"]
        assert by["TARGET"]["count"] == 1 and by["HARD_SL"]["count"] == 1
        assert by["TARGET"]["net_pnl"] > 0 > by["HARD_SL"]["net_pnl"]

    def test_csv_export_round_trips(self, sm, repo):
        self._trade(sm, repo, "S1", 310.0, 350.0)
        csv_text = TradeJournal(repo).to_csv()
        assert "net_pnl" in csv_text.splitlines()[0]
        assert len(csv_text.strip().splitlines()) == 2


class TestRegimeFlipTypeSafety:
    """Regression for a bug the replay harness caught that no unit test had.

    The Guardian compared a REGIME label (BULL/BEAR/CHOP) against an OPTION
    SIDE (CE/PE). Those domains never intersect, so the condition was always
    true and EVERY position exited as REGIME_FLIP on its first tick — average
    hold, one minute. It is exactly the class of bug that only an end-to-end
    replay surfaces, which is why the harness exists.
    """

    def test_matching_regime_does_not_exit_a_call(self, guardian, position):
        d = guardian.evaluate(position, guardian_inputs(bid=330.0, ask=332.0, regime="BULL"))
        assert d.reason is not ExitReason.REGIME_FLIP
        assert not d.is_exit

    def test_opposite_regime_exits_a_call(self, guardian, position):
        d = guardian.evaluate(position, guardian_inputs(bid=330.0, ask=332.0, regime="BEAR"))
        assert d.is_exit and d.reason is ExitReason.REGIME_FLIP

    def test_chop_is_not_a_reversal(self, guardian, position):
        """CHOP weakens conviction; it does not mean the trade is wrong."""
        d = guardian.evaluate(position, guardian_inputs(bid=330.0, ask=332.0, regime="CHOP"))
        assert d.reason is not ExitReason.REGIME_FLIP

    def test_unknown_regime_is_not_a_reversal(self, guardian, position):
        d = guardian.evaluate(position, guardian_inputs(bid=330.0, ask=332.0, regime="UNKNOWN"))
        assert d.reason is not ExitReason.REGIME_FLIP

    def test_a_put_is_only_flipped_by_a_bull_regime(self, guardian, position):
        position.direction = "PE"
        assert (
            guardian.evaluate(position, guardian_inputs(bid=330.0, ask=332.0, regime="BEAR")).reason
            is not ExitReason.REGIME_FLIP
        )
        assert (
            guardian.evaluate(position, guardian_inputs(bid=330.0, ask=332.0, regime="BULL")).reason
            is ExitReason.REGIME_FLIP
        )

    def test_no_position_exits_instantly_on_a_matching_regime(self, guardian, position):
        """The headline symptom: a 1-minute average hold."""
        for minutes in (0, 1, 2, 5):
            at = NOW + dt.timedelta(minutes=minutes)
            d = guardian.evaluate(
                position, guardian_inputs(bid=330.0, ask=332.0, now=at, regime="BULL")
            )
            assert not d.is_exit, f"exited after {minutes} min: {d.reason}"
