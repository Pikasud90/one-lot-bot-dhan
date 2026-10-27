"""Central token-bucket rate limiting.

*** v1 defect D3 ***
v1 rate-limited by putting `sleep(5)` inside the polling thread. That is not
rate limiting, it is one thread being polite. Three independent consumers share
the same broker quota — the 5s chain poller, the 1/sec REST failover, and
historical warm-up fetches — and they collide precisely when the WebSocket
drops, because that is when failover starts hammering while the poller keeps
polling. You get RL001-locked at the exact moment you most need data.

Every broker call in this codebase acquires from a shared limiter. Buckets are
per-endpoint plus a global ceiling, so no combination of callers can exceed the
published limits.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


class TokenBucket:
    """Thread-safe token bucket. `capacity` controls the permitted burst."""

    def __init__(self, rate_per_sec: float, capacity: float | None = None, name: str = ""):
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        self.name = name
        self.rate = rate_per_sec
        self.capacity = capacity if capacity is not None else max(1.0, rate_per_sec)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self._penalty_until = 0.0

    def _refill_locked(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        with self._lock:
            if time.monotonic() < self._penalty_until:
                return False
            self._refill_locked()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def acquire(self, tokens: float = 1.0, timeout: float | None = None) -> bool:
        """Block until tokens are available. Returns False on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                if now >= self._penalty_until:
                    self._refill_locked()
                    if self._tokens >= tokens:
                        self._tokens -= tokens
                        return True
                    wait = max((tokens - self._tokens) / self.rate, 0.001)
                else:
                    wait = self._penalty_until - now
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait = min(wait, remaining)
            time.sleep(min(wait, 0.25))

    def penalise(self, seconds: float) -> None:
        """Freeze this bucket after a broker-side rejection (RL001 / HTTP 429)."""
        with self._lock:
            self._penalty_until = max(self._penalty_until, time.monotonic() + seconds)
            self._tokens = 0.0

    @property
    def available(self) -> float:
        with self._lock:
            self._refill_locked()
            return self._tokens

    @property
    def penalised_for(self) -> float:
        return max(0.0, self._penalty_until - time.monotonic())


@dataclass
class EndpointSpec:
    name: str
    rate_per_sec: float
    capacity: float | None = None


# Published Dhan limits, deliberately configured with headroom rather than at
# the exact ceiling: a clock skew or a retry should not push us over.
DEFAULT_ENDPOINTS: tuple[EndpointSpec, ...] = (
    # Option chain: documented at 1 request per 3 seconds. No burst allowed.
    EndpointSpec("option_chain", rate_per_sec=1.0 / 3.2, capacity=1.0),
    # Market quote REST, used as the WebSocket failover path.
    EndpointSpec("quote", rate_per_sec=1.0, capacity=2.0),
    # Historical / warm-up candles: infrequent, chunky.
    EndpointSpec("historical", rate_per_sec=0.5, capacity=2.0),
    # Instrument master download and expiry list.
    EndpointSpec("reference", rate_per_sec=0.2, capacity=1.0),
    # Free Trading-API reads (profile, funds, holdings, positions, ledger, trades).
    # Dhan allows 20/s for non-trading calls; these are user-initiated and rare.
    EndpointSpec("account", rate_per_sec=2.0, capacity=4.0),
)

GLOBAL_RATE_PER_SEC = 8.0
GLOBAL_CAPACITY = 12.0


@dataclass
class RateLimiterStats:
    acquisitions: int = 0
    timeouts: int = 0
    penalties: int = 0
    total_wait_sec: float = 0.0
    per_endpoint: dict[str, int] = field(default_factory=dict)


class RateLimiter:
    """One limiter per process. Every broker call must pass through it."""

    def __init__(self, endpoints: tuple[EndpointSpec, ...] = DEFAULT_ENDPOINTS,
                 global_rate: float = GLOBAL_RATE_PER_SEC,
                 global_capacity: float = GLOBAL_CAPACITY):
        self._buckets = {
            e.name: TokenBucket(e.rate_per_sec, e.capacity, name=e.name) for e in endpoints
        }
        self._global = TokenBucket(global_rate, global_capacity, name="global")
        self.stats = RateLimiterStats()
        self._lock = threading.Lock()

    def bucket(self, endpoint: str) -> TokenBucket:
        if endpoint not in self._buckets:
            # Unknown endpoints get a conservative default rather than a free pass.
            with self._lock:
                if endpoint not in self._buckets:
                    log.warning("Unregistered endpoint %r — applying conservative 0.5/s", endpoint)
                    self._buckets[endpoint] = TokenBucket(0.5, 1.0, name=endpoint)
        return self._buckets[endpoint]

    def acquire(self, endpoint: str, timeout: float | None = 30.0) -> bool:
        """Acquire from the endpoint bucket AND the global ceiling."""
        start = time.monotonic()
        b = self.bucket(endpoint)
        if not b.acquire(1.0, timeout):
            self.stats.timeouts += 1
            log.warning("Rate limit timeout on %s after %.1fs", endpoint, timeout or 0)
            return False
        remaining = None if timeout is None else max(0.0, timeout - (time.monotonic() - start))
        if not self._global.acquire(1.0, remaining):
            self.stats.timeouts += 1
            return False
        waited = time.monotonic() - start
        self.stats.acquisitions += 1
        self.stats.total_wait_sec += waited
        self.stats.per_endpoint[endpoint] = self.stats.per_endpoint.get(endpoint, 0) + 1
        if waited > 1.0:
            log.debug("Waited %.2fs for %s quota", waited, endpoint)
        return True

    def note_rate_limited(self, endpoint: str, attempt: int = 1,
                          base_backoff: float = 4.0, max_backoff: float = 120.0) -> float:
        """Called when the broker returns RL001 / HTTP 429.

        Jittered exponential backoff: a fixed backoff makes every caller retry
        in lockstep and re-trigger the same limit (the thundering-herd retry
        storm that turns a brief throttle into a sustained lockout).
        """
        delay = min(max_backoff, base_backoff * (2 ** max(0, attempt - 1)))
        delay *= 0.5 + random.random()      # jitter in [0.5x, 1.5x]
        self.bucket(endpoint).penalise(delay)
        self._global.penalise(min(delay / 2.0, 10.0))
        self.stats.penalties += 1
        log.warning("Broker rate-limited %s (attempt %d) — backing off %.1fs",
                    endpoint, attempt, delay)
        return delay

    def snapshot(self) -> dict:
        return {
            "global_available": round(self._global.available, 2),
            "global_penalised_for": round(self._global.penalised_for, 2),
            "endpoints": {
                n: {"available": round(b.available, 2),
                    "penalised_for": round(b.penalised_for, 2),
                    "rate_per_sec": round(b.rate, 4)}
                for n, b in self._buckets.items()
            },
            "stats": {
                "acquisitions": self.stats.acquisitions,
                "timeouts": self.stats.timeouts,
                "penalties": self.stats.penalties,
                "avg_wait_sec": round(
                    self.stats.total_wait_sec / max(1, self.stats.acquisitions), 4),
                "per_endpoint": dict(self.stats.per_endpoint),
            },
        }
