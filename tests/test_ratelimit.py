"""Rate limiter — v1 defect D3.

v1 'rate limited' with sleep(5) in one thread while two other consumers used
the same broker quota. These tests prove the shared limiter holds under
concurrency, which is the only condition that matters (failover fires exactly
when the poller is already busy)."""

from __future__ import annotations

import threading
import time

import pytest

from onelot.ratelimit import DEFAULT_ENDPOINTS, EndpointSpec, RateLimiter, TokenBucket


class TestTokenBucket:
    def test_burst_up_to_capacity_then_throttles(self):
        b = TokenBucket(rate_per_sec=10.0, capacity=3.0)
        assert all(b.try_acquire() for _ in range(3))
        assert not b.try_acquire(), "capacity exhausted"

    def test_refills_over_time(self):
        b = TokenBucket(rate_per_sec=20.0, capacity=1.0)
        assert b.try_acquire()
        assert not b.try_acquire()
        time.sleep(0.12)
        assert b.try_acquire()

    def test_blocking_acquire_waits(self):
        b = TokenBucket(rate_per_sec=10.0, capacity=1.0)
        b.acquire()
        t0 = time.monotonic()
        assert b.acquire(timeout=2.0)
        assert time.monotonic() - t0 >= 0.05

    def test_timeout_returns_false(self):
        b = TokenBucket(rate_per_sec=0.5, capacity=1.0)
        b.acquire()
        assert not b.acquire(timeout=0.15)

    def test_penalty_freezes_the_bucket(self):
        b = TokenBucket(rate_per_sec=100.0, capacity=10.0)
        b.penalise(0.3)
        assert not b.try_acquire()
        assert b.penalised_for > 0
        time.sleep(0.35)
        assert b.try_acquire()

    def test_rejects_invalid_rate(self):
        with pytest.raises(ValueError):
            TokenBucket(0.0)


class TestOptionChainLimit:
    """Dhan's option chain endpoint permits 1 request per 3 seconds. Breaching
    it triggers RL001, which locks you out at the worst possible moment."""

    def test_configured_below_one_per_three_seconds(self):
        spec = next(e for e in DEFAULT_ENDPOINTS if e.name == "option_chain")
        assert spec.rate_per_sec <= 1 / 3.0, "must have headroom under the 1-per-3s limit"
        assert spec.capacity == 1.0, "no burst allowed on the chain endpoint"

    def test_second_immediate_call_is_refused(self):
        rl = RateLimiter()
        assert rl.acquire("option_chain", timeout=1.0)
        assert not rl.acquire("option_chain", timeout=0.5)

    def test_config_rejects_a_too_fast_poll_interval(self, cfg):
        from onelot.config import DataCfg

        with pytest.raises(ValueError, match="RL001"):
            DataCfg(chain_poll_interval_sec=1.0)


class TestSharedQuota:
    def test_concurrent_callers_cannot_exceed_the_limit(self):
        """THE v1 failure: poller + failover + warm-up all hitting at once."""
        rl = RateLimiter(
            endpoints=(EndpointSpec("quote", rate_per_sec=5.0, capacity=2.0),),
            global_rate=5.0,
            global_capacity=2.0,
        )
        granted, lock = [], threading.Lock()

        def worker():
            for _ in range(6):
                if rl.acquire("quote", timeout=0.05):
                    with lock:
                        granted.append(time.monotonic())

        t0 = time.monotonic()
        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.monotonic() - t0
        # capacity 2 burst + rate 5/s for the elapsed window, plus slack
        assert len(granted) <= 2 + int(5.0 * elapsed) + 2

    def test_global_ceiling_binds_across_endpoints(self):
        rl = RateLimiter(
            endpoints=(EndpointSpec("a", 100.0, 50.0), EndpointSpec("b", 100.0, 50.0)),
            global_rate=1.0,
            global_capacity=2.0,
        )
        got = sum(1 for _ in range(10) if rl.acquire("a" if _ % 2 == 0 else "b", timeout=0.01))
        assert got <= 3, "the global bucket must bind even when endpoints are generous"


class TestBackoff:
    def test_rl001_triggers_jittered_backoff(self):
        rl = RateLimiter()
        d1 = rl.note_rate_limited("option_chain", attempt=1)
        d2 = rl.note_rate_limited("option_chain", attempt=3)
        assert d2 > d1, "backoff must grow with consecutive failures"
        assert rl.stats.penalties == 2

    def test_backoff_is_capped(self):
        rl = RateLimiter()
        assert rl.note_rate_limited("quote", attempt=20, max_backoff=60.0) <= 90.0

    def test_jitter_prevents_lockstep_retries(self):
        """Identical backoffs make every caller retry simultaneously and
        re-trigger the same limit — a self-sustaining lockout."""
        rl = RateLimiter()
        delays = {round(rl.note_rate_limited("historical", attempt=2), 4) for _ in range(12)}
        assert len(delays) > 1

    def test_penalised_endpoint_refuses_immediately(self):
        rl = RateLimiter()
        rl.note_rate_limited("quote", attempt=2)
        assert not rl.acquire("quote", timeout=0.05)


class TestObservability:
    def test_unknown_endpoint_gets_conservative_default_not_a_free_pass(self):
        rl = RateLimiter()
        assert rl.acquire("brand_new_endpoint", timeout=1.0)
        assert not rl.acquire("brand_new_endpoint", timeout=0.1)

    def test_snapshot_reports_everything_the_dashboard_needs(self):
        rl = RateLimiter()
        rl.acquire("quote", timeout=1.0)
        snap = rl.snapshot()
        assert "option_chain" in snap["endpoints"]
        assert snap["stats"]["acquisitions"] >= 1
        assert "avg_wait_sec" in snap["stats"]
