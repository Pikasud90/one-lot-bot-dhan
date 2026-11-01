"""Three-state data health model.

*** v1 defect D4 ***
v1 raised an exception when `now - last_tick > 3 seconds` and failed over to
REST. Market data ticks arrive ON TRADES: a quiet strike routinely goes 10+
seconds without one, especially outside the first and last hour. v1 would have
flapped into failover constantly, burning the very quota it needed, and it
conflated two genuinely different questions:

    "Is the socket alive?"    -> connection state + protocol ping/pong (fast)
    "Is this data fresh?"     -> per-instrument age (slower, instrument-specific)

A dead socket is a failover trigger. Silence is not.

*** INVARIANT I4 ***
No signal is ever emitted unless the state is HEALTHY. Degradation blocks new
risk while still managing existing positions — the opposite would be
catastrophic: losing the feed must never mean abandoning an open trade.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger(__name__)


class HealthState(str, Enum):
    HEALTHY = "HEALTHY"  # everything nominal — signals permitted
    DEGRADED = "DEGRADED"  # usable for managing positions, NOT for new ones
    DEAD = "DEAD"  # no usable data at all

    @property
    def may_signal(self) -> bool:
        return self is HealthState.HEALTHY

    @property
    def may_manage_positions(self) -> bool:
        return self is not HealthState.DEAD


@dataclass
class HealthReport:
    state: HealthState
    reasons: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    @property
    def may_signal(self) -> bool:
        return self.state.may_signal

    def as_dict(self) -> dict:
        return {
            "state": self.state.value,
            "reasons": list(self.reasons),
            "may_signal": self.may_signal,
            "may_manage_positions": self.state.may_manage_positions,
            "details": dict(self.details),
        }


class HealthMonitor:
    """Aggregates every condition that should stop the engine taking new risk."""

    def __init__(
        self,
        socket_dead_after_sec: float = 12.0,
        quote_stale_after_sec: float = 20.0,
        chain_stale_after_sec: float = 15.0,
        token_warn_hours: float = 4.0,
    ):
        self.socket_dead_after_sec = socket_dead_after_sec
        self.quote_stale_after_sec = quote_stale_after_sec
        self.chain_stale_after_sec = chain_stale_after_sec
        self.token_warn_hours = token_warn_hours

        self.socket_connected = False
        self.using_rest_failover = False
        self.last_socket_event: dt.datetime | None = None
        self.last_quote_at: dict[str, dt.datetime] = {}
        self.last_chain_at: dt.datetime | None = None
        self.token_expires_at: dt.datetime | None = None
        self.warmup_complete = False
        self.warmup_note = "not started"
        self.ivp_usable = True
        self.ivp_note = ""
        self.market_open = False
        self.consecutive_api_errors = 0
        self.last_error: str | None = None
        self._previous: HealthState | None = None

    # ── inputs ────────────────────────────────────────────────────────────
    def note_socket(self, connected: bool, now: dt.datetime) -> None:
        if connected != self.socket_connected:
            log.info("Market feed socket %s", "connected" if connected else "disconnected")
        self.socket_connected = connected
        self.last_socket_event = now

    def note_quote(self, security_id: str, now: dt.datetime) -> None:
        self.last_quote_at[str(security_id)] = now

    def note_chain(self, now: dt.datetime) -> None:
        self.last_chain_at = now

    def note_api_error(self, message: str) -> None:
        self.consecutive_api_errors += 1
        self.last_error = message[:300]

    def note_api_ok(self) -> None:
        self.consecutive_api_errors = 0
        self.last_error = None

    # ── evaluation ────────────────────────────────────────────────────────
    def evaluate(self, now: dt.datetime, critical_ids: list[str] | None = None) -> HealthReport:
        reasons: list[str] = []
        details: dict = {}
        dead = False

        if not self.market_open:
            reasons.append("market is closed")

        # Socket liveness — the ONLY thing that should trigger failover.
        if not self.socket_connected:
            if self.using_rest_failover:
                reasons.append("WebSocket down — running on REST failover")
            else:
                reasons.append("WebSocket not connected")
        details["socket_connected"] = self.socket_connected
        details["rest_failover"] = self.using_rest_failover

        # Data freshness — a SEPARATE, slower signal. Silence is not death.
        stale: list[str] = []
        for sid in critical_ids or list(self.last_quote_at):
            last = self.last_quote_at.get(str(sid))
            if last is None:
                stale.append(f"{sid}:never")
            else:
                age = (now - last).total_seconds()
                if age > self.quote_stale_after_sec:
                    stale.append(f"{sid}:{age:.0f}s")
        if stale:
            reasons.append(f"stale quotes ({', '.join(stale[:4])})")
        details["stale_quotes"] = stale

        if self.market_open and critical_ids and len(stale) >= len(critical_ids):
            dead = True  # after the bell silence is normal: DEGRADED ("market is closed"), not DEAD
            reasons.append("no fresh data for any critical instrument")

        # Option chain freshness — sizing off a stale chain is sizing off fiction.
        if self.last_chain_at is None:
            reasons.append("no option chain received yet")
        else:
            age = (now - self.last_chain_at).total_seconds()
            details["chain_age_sec"] = round(age, 1)
            if age > self.chain_stale_after_sec:
                reasons.append(f"option chain is {age:.0f}s stale")
            if self.market_open and age > self.chain_stale_after_sec * 6:
                dead = True

        # Warm-up (v1 defect B4)
        if not self.warmup_complete:
            reasons.append(self.warmup_note)
        details["warmup_complete"] = self.warmup_complete

        # IV percentile sufficiency (v1 defect B5)
        if not self.ivp_usable:
            reasons.append(self.ivp_note or "IV percentile history insufficient")
        details["ivp_usable"] = self.ivp_usable

        # Token expiry (v1 defect E3)
        if self.token_expires_at is not None:
            hrs = (self.token_expires_at - now).total_seconds() / 3600.0
            details["token_hours_remaining"] = round(hrs, 2)
            if hrs <= 0:
                dead = True
                reasons.append("access token EXPIRED")
            elif hrs < self.token_warn_hours:
                reasons.append(f"access token expires in {hrs:.1f}h")

        if self.consecutive_api_errors >= 5:
            dead = True
            reasons.append(
                f"{self.consecutive_api_errors} consecutive API errors: {self.last_error}"
            )
        details["consecutive_api_errors"] = self.consecutive_api_errors

        if dead:
            state = HealthState.DEAD
        elif reasons:
            state = HealthState.DEGRADED
        else:
            state = HealthState.HEALTHY

        if state is not self._previous:
            log.warning(
                "Health %s -> %s: %s",
                self._previous.value if self._previous else "INIT",
                state.value,
                "; ".join(reasons) or "nominal",
            )
            self._previous = state

        return HealthReport(state=state, reasons=reasons, details=details)

    def should_failover_to_rest(self, now: dt.datetime) -> bool:
        """Failover on CONNECTION death, never on tick silence (defect D4)."""
        if self.socket_connected:
            return False
        if self.last_socket_event is None:
            return True
        return (now - self.last_socket_event).total_seconds() >= self.socket_dead_after_sec
