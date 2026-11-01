"""Market layer: closed-bar discipline, warm-up, health model, chain validation."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from onelot.broker.base import Bar
from onelot.broker.mock import Faults, MockBroker, SyntheticConfig, SyntheticMarket
from onelot.clock import IST
from onelot.market.candles import CandleSeries
from onelot.market.chain import ChainCache
from onelot.market.health import HealthMonitor, HealthState

OPEN = dt.datetime(2026, 8, 20, 9, 15, tzinfo=IST)


def at(h, m, s=0):
    return dt.datetime(2026, 8, 20, h, m, s, tzinfo=IST)


class TestClosedBarDiscipline:
    """*** INVARIANT I2 / v1 defect C4 *** — repainting."""

    def test_forming_bar_is_not_exposed_as_closed(self):
        s = CandleSeries("IDX", 5, OPEN, has_volume=False)
        s.on_tick(at(9, 16), 54000.0)
        s.on_tick(at(9, 18), 54050.0)
        assert s.count == 0, "an in-progress bar must never count as closed"
        assert s.forming_bar is not None

    def test_bar_closes_only_after_its_boundary(self):
        s = CandleSeries("IDX", 5, OPEN, has_volume=False)
        s.on_tick(at(9, 16), 54000.0)
        assert s.mark_closed_through(at(9, 19, 59), 2.0) == []
        assert s.mark_closed_through(at(9, 20, 1), 2.0) == [], "settle delay not elapsed"
        assert len(s.mark_closed_through(at(9, 20, 3), 2.0)) == 1
        assert s.count == 1

    def test_closed_bar_values_are_frozen(self):
        s = CandleSeries("IDX", 5, OPEN, has_volume=False)
        for t, p in [(at(9, 16), 54000.0), (at(9, 17), 54120.0), (at(9, 19), 53980.0)]:
            s.on_tick(t, p)
        s.on_tick(at(9, 21), 55000.0)  # next bar opens, prior one closes
        b = s.latest_closed()
        assert (b.open, b.high, b.low, b.close) == (54000.0, 54120.0, 53980.0, 53980.0)
        s.on_tick(at(9, 23), 99999.0)  # cannot retroactively alter it
        assert s.latest_closed().high == 54120.0

    def test_bars_align_to_the_session_open(self):
        s = CandleSeries("IDX", 5, OPEN, has_volume=False)
        s.on_tick(at(9, 37), 54000.0)
        assert s.forming_bar.ts == at(9, 35), "must align to 09:15, not to the hour"

    def test_quiet_interval_still_closes_the_bar(self):
        """A period with no trades must not stall the strategy."""
        s = CandleSeries("IDX", 5, OPEN, has_volume=False)
        s.on_tick(at(9, 16), 54000.0)
        assert len(s.mark_closed_through(at(9, 30), 2.0)) == 1


class TestVolumeSemantics:
    def test_index_series_never_accumulates_volume(self):
        s = CandleSeries("IDX", 5, OPEN, has_volume=False)
        s.on_tick(at(9, 16), 54000.0, cumulative_volume=999_999.0)
        s.on_tick(at(9, 21), 54100.0, cumulative_volume=1_500_000.0)
        assert s.latest_closed().volume is None, (
            "storing volume on an index is what let v1 compute VWAP on 0/0"
        )

    def test_futures_series_accumulates_deltas_from_cumulative(self):
        s = CandleSeries("FUT", 5, OPEN, has_volume=True)
        s.on_tick(at(9, 16), 54000.0, cumulative_volume=1000.0)
        s.on_tick(at(9, 17), 54010.0, cumulative_volume=1600.0)
        s.on_tick(at(9, 18), 54020.0, cumulative_volume=2100.0)
        s.on_tick(at(9, 21), 54030.0, cumulative_volume=2500.0)
        assert s.latest_closed().volume == pytest.approx(1100.0)

    def test_volume_counter_reset_is_not_treated_as_negative(self):
        s = CandleSeries("FUT", 5, OPEN, has_volume=True)
        s.on_tick(at(9, 16), 54000.0, cumulative_volume=5000.0)
        s.on_tick(at(9, 17), 54010.0, cumulative_volume=10.0)  # feed reset
        s.on_tick(at(9, 21), 54020.0, cumulative_volume=60.0)
        assert s.latest_closed().volume >= 0


class TestWarmup:
    """v1 defect B4: indicators are NaN for the first ~100 minutes."""

    def test_seeding_makes_indicators_available_immediately(self):
        from onelot.analytics.indicators import adx, ema, rsi

        s = CandleSeries("IDX", 5, OPEN, has_volume=False)
        assert not s.warmup_status(40)[0]

        m = SyntheticMarket(SyntheticConfig(seed=3, sessions=10))
        seed_bars: list[Bar] = []
        for d in m.session_dates[:-1]:
            seed_bars.extend(m.bars(d, 5, "index"))
        assert s.seed_from_history(seed_bars) > 100
        ok, note = s.warmup_status(40)
        assert ok, note

        a = s.arrays()
        assert not np.isnan(ema(a["close"], 21)[-1])
        assert not np.isnan(rsi(a["close"], 14)[-1])
        assert not np.isnan(adx(a["high"], a["low"], a["close"], 14)[0][-1])

    def test_warmup_status_explains_itself(self):
        s = CandleSeries("IDX", 5, OPEN, has_volume=False)
        ok, note = s.warmup_status(40)
        assert not ok and "0/40" in note and "blocked" in note

    def test_seeding_is_idempotent(self):
        m = SyntheticMarket(SyntheticConfig(seed=3, sessions=4))
        bars = m.bars(m.session_dates[0], 5, "index")
        s = CandleSeries("IDX", 5, OPEN, has_volume=False)
        assert s.seed_from_history(bars) == len(bars)
        assert s.seed_from_history(bars) == 0
        assert s.count == len(bars)


class TestHealthModel:
    """v1 defect D4: 3s of tick silence is NOT a dead socket."""

    def _healthy(self):
        h = HealthMonitor(socket_dead_after_sec=12.0, quote_stale_after_sec=20.0)
        now = at(11, 0)
        h.market_open = True
        h.warmup_complete = True
        h.ivp_usable = True
        h.note_socket(True, now)
        h.note_chain(now)
        h.note_quote("IDX", now)
        h.note_quote("FUT", now)
        return h, now

    def test_nominal_is_healthy_and_may_signal(self):
        h, now = self._healthy()
        r = h.evaluate(now, ["IDX", "FUT"])
        assert r.state is HealthState.HEALTHY and r.may_signal

    def test_ten_seconds_of_tick_silence_does_not_trigger_failover(self):
        """THE v1 bug: quiet strikes routinely go 10s+ between trades."""
        h, now = self._healthy()
        assert not h.should_failover_to_rest(now + dt.timedelta(seconds=10))

    def test_dead_socket_triggers_failover(self):
        h, now = self._healthy()
        h.note_socket(False, now)
        assert not h.should_failover_to_rest(now + dt.timedelta(seconds=5))
        assert h.should_failover_to_rest(now + dt.timedelta(seconds=13))

    def test_warmup_incomplete_blocks_signals(self):
        h, now = self._healthy()
        h.warmup_complete = False
        h.warmup_note = "warm-up incomplete: 3/40 closed bars"
        r = h.evaluate(now, ["IDX", "FUT"])
        assert not r.may_signal and r.state is HealthState.DEGRADED

    def test_insufficient_ivp_history_blocks_signals(self):
        h, now = self._healthy()
        h.ivp_usable = False
        h.ivp_note = "only 3/60 sessions of IV history"
        assert not h.evaluate(now, ["IDX", "FUT"]).may_signal

    def test_stale_chain_blocks_signals(self):
        h, now = self._healthy()
        later = now + dt.timedelta(seconds=40)
        h.note_quote("IDX", later)
        h.note_quote("FUT", later)
        r = h.evaluate(later, ["IDX", "FUT"])
        assert not r.may_signal
        assert any("stale" in x for x in r.reasons)

    def test_degraded_still_manages_open_positions(self):
        """Losing the feed must never mean abandoning a live trade."""
        h, now = self._healthy()
        h.warmup_complete = False
        r = h.evaluate(now, ["IDX", "FUT"])
        assert r.state is HealthState.DEGRADED
        assert not r.may_signal
        assert r.state.may_manage_positions

    def test_expired_token_is_dead(self):
        h, now = self._healthy()
        h.token_expires_at = now - dt.timedelta(minutes=1)
        r = h.evaluate(now, ["IDX", "FUT"])
        assert r.state is HealthState.DEAD
        assert any("EXPIRED" in x for x in r.reasons)

    def test_token_expiring_soon_warns_before_it_breaks(self):
        h, now = self._healthy()
        h.token_expires_at = now + dt.timedelta(hours=1)
        r = h.evaluate(now, ["IDX", "FUT"])
        assert r.state is HealthState.DEGRADED
        assert r.details["token_hours_remaining"] == pytest.approx(1.0, abs=0.05)

    def test_repeated_api_errors_are_dead(self):
        h, now = self._healthy()
        for _ in range(5):
            h.note_api_error("boom")
        assert h.evaluate(now, ["IDX", "FUT"]).state is HealthState.DEAD


class TestChainValidation:
    """v1 defect A9: broker greeks were trusted blindly."""

    def _cache(self, broker):
        return ChainCache(
            broker,
            "BANKNIFTY",
            broker.expiry_date.isoformat(),
            greek_tolerance=0.08,
            forward_price_fn=lambda: broker.market.futures_price(broker.now()),
        )

    def test_agreeing_greeks_are_cross_checked(self):
        b = MockBroker(now=dt.datetime(2026, 8, 20, 11, 0, tzinfo=IST))
        c = self._cache(b)
        assert c.poll_once(b.expiry_dt, b.now())
        srcs = {r.greek_source for r in c.snapshot.rows}
        assert "cross_checked" in srcs

    def test_missing_greeks_are_computed_locally(self):
        b = MockBroker(
            now=dt.datetime(2026, 8, 20, 11, 0, tzinfo=IST), faults=Faults(omit_greeks=True)
        )
        c = self._cache(b)
        c.poll_once(b.expiry_dt, b.now())
        assert c.stats.greeks_repaired > 0
        assert all(r.delta is not None for r in c.usable_rows())

    def test_corrupt_broker_greeks_are_rejected_not_used(self):
        b = MockBroker(
            now=dt.datetime(2026, 8, 20, 11, 0, tzinfo=IST), faults=Faults(corrupt_delta=0.30)
        )
        c = self._cache(b)
        c.poll_once(b.expiry_dt, b.now())
        assert c.stats.greeks_rejected > 0
        assert all(r.greek_source != "rejected" for r in c.usable_rows())

    def test_zero_bid_rows_are_excluded_from_sizing(self):
        b = MockBroker(now=dt.datetime(2026, 8, 20, 11, 0, tzinfo=IST))
        chain = b.option_chain("BANKNIFTY", b.expiry_date.isoformat())
        bad = chain.rows[0].strike
        b.faults = Faults(zero_bid_strikes={bad})
        c = self._cache(b)
        c.poll_once(b.expiry_dt, b.now())
        assert all(r.strike != bad or r.opt_type != chain.rows[0].opt_type for r in c.usable_rows())

    def test_put_delta_sign_is_normalised_negative(self):
        b = MockBroker(now=dt.datetime(2026, 8, 20, 11, 0, tzinfo=IST))
        c = self._cache(b)
        c.poll_once(b.expiry_dt, b.now())
        pes = [r for r in c.usable_rows("PE") if abs(r.delta) > 0.05]
        assert pes and all(r.delta < 0 for r in pes)

    def test_stale_chain_is_reported_stale(self):
        b = MockBroker(
            now=dt.datetime(2026, 8, 20, 11, 0, tzinfo=IST), faults=Faults(chain_staleness_sec=45.0)
        )
        c = self._cache(b)
        c.poll_once(b.expiry_dt, b.now())
        assert not c.is_fresh(b.now())
        assert c.age_seconds(b.now()) >= 45

    def test_rate_limit_is_survived_not_crashed(self):
        b = MockBroker(
            now=dt.datetime(2026, 8, 20, 11, 0, tzinfo=IST), faults=Faults(rate_limit_every_n=2)
        )
        c = self._cache(b)
        results = [c.poll_once(b.expiry_dt, b.now()) for _ in range(4)]
        assert any(results) and not all(results)
        assert c.stats.rate_limited > 0


class TestBarClosePathsBothFire:
    """Regression: bars close by TWO paths and a consumer must handle both.

    The engine originally listened only to the clock-driven path. Because
    on_tick() rolls a bar the moment a tick lands in the next interval, the
    tick-driven path always wins in a live feed — so the clock path found
    nothing to close and the engine evaluated ZERO bars while reporting
    perfect health.
    """

    def test_tick_driven_close_returns_the_bar(self):
        s = CandleSeries("IDX", 5, OPEN, has_volume=False)
        s.on_tick(at(9, 16), 54000.0)
        closed = s.on_tick(at(9, 21), 54100.0)  # crosses into the next bar
        assert len(closed) == 1 and closed[0].ts == at(9, 15)

    def test_clock_driven_close_finds_nothing_after_a_tick_close(self):
        """Proves the two paths are mutually exclusive for a given bar."""
        s = CandleSeries("IDX", 5, OPEN, has_volume=False)
        s.on_tick(at(9, 16), 54000.0)
        assert len(s.on_tick(at(9, 21), 54100.0)) == 1
        assert s.mark_closed_through(at(9, 21, 5), 2.0) == [], (
            "the 09:15 bar was already closed by the tick path"
        )

    def test_feed_callback_fires_for_tick_driven_closes(self):
        """MarketFeed must surface tick-driven closes, or they vanish."""
        from onelot.broker.base import Contract, QuoteSnapshot
        from onelot.market.feed import INDEX_KEY, FeedContracts, MarketFeed
        from onelot.market.health import HealthMonitor
        from onelot.ratelimit import RateLimiter

        seen = []
        idx = Contract(
            security_id="I1",
            exchange_segment="IDX_I",
            symbol="X",
            underlying="X",
            instrument_type="INDEX",
        )
        feed = MarketFeed(
            MockBroker(),
            FeedContracts(index=idx),
            RateLimiter(),
            HealthMonitor(),
            OPEN,
            on_bar_closed=lambda k, b: seen.append((k, b)),
        )
        feed._on_tick(QuoteSnapshot("I1", at(9, 16), ltp=54000.0, source="ws"))
        feed._on_tick(QuoteSnapshot("I1", at(9, 21), ltp=54100.0, source="ws"))
        assert [k for k, _ in seen] == [INDEX_KEY]

    def test_quiet_interval_still_closes_via_the_clock_path(self):
        """Both paths must work: no trades at all must not stall the strategy."""
        s = CandleSeries("IDX", 5, OPEN, has_volume=False)
        s.on_tick(at(9, 16), 54000.0)
        assert len(s.mark_closed_through(at(9, 30), 2.0)) == 1
