"""Entry gates as pure, individually testable functions.

Two design rules, both fixing v1 shortcomings:

1. EVERY gate is evaluated on every bar, even after one has already failed.
   v1 short-circuited, so a rejected bar told you nothing except which gate
   happened to be checked first. Here the full result set is persisted, which
   is what makes the "why did I get no signal today?" histogram possible
   (v1 defect B2 — a system that fires rarely and cannot explain itself is
   indistinguishable from a broken one).

2. Every gate returns a STRUCTURED result — the observed value and a
   human-readable reason — not a bare bool. The reason string ends up on the
   dashboard and in the audit table.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np

from ..analytics.ivp import IVPResult
from ..analytics.regime import IntradayChopResult, Regime, RegimeResult


@dataclass(frozen=True)
class GateResult:
    gate: str
    passed: bool
    value: str = ""
    reason: str = ""
    advisory: bool = False        # advisory gates inform but never block

    def as_dict(self) -> dict:
        return {"gate": self.gate, "passed": self.passed, "value": self.value,
                "reason": self.reason, "advisory": self.advisory}


@dataclass
class GateReport:
    direction: str
    results: list[GateResult] = field(default_factory=list)

    def add(self, r: GateResult) -> GateResult:
        self.results.append(r)
        return r

    @property
    def blocking_failures(self) -> list[GateResult]:
        return [r for r in self.results if not r.passed and not r.advisory]

    @property
    def passed(self) -> bool:
        return not self.blocking_failures

    @property
    def first_block(self) -> str | None:
        f = self.blocking_failures
        return f[0].gate if f else None

    def as_dicts(self) -> list[dict]:
        return [r.as_dict() for r in self.results]

    def summary(self) -> str:
        if self.passed:
            return "all gates passed"
        return "; ".join(f"{r.gate}: {r.reason}" for r in self.blocking_failures)


# ── gate 0: preflight ─────────────────────────────────────────────────────

def gate_health(may_signal: bool, reasons: list[str]) -> GateResult:
    """INVARIANT I4: no signal unless data health is HEALTHY."""
    if may_signal:
        return GateResult("health", True, "HEALTHY")
    return GateResult("health", False, "DEGRADED/DEAD",
                      "; ".join(reasons[:3]) or "data health not HEALTHY")


def gate_session(now: dt.datetime, window, allow_entries: bool = True) -> GateResult:
    """Entry window. v1 never specified one, so it would have signalled into
    the opening auction and into the closing illiquidity."""
    if window is None:
        return GateResult("session", False, now.strftime("%H:%M"), "not a trading day")
    if not window.is_open(now):
        return GateResult("session", False, now.strftime("%H:%M"), "market is closed")
    if not allow_entries:
        return GateResult("session", False, now.strftime("%H:%M"), "entries disabled")
    if now < window.first_entry_at:
        return GateResult("session", False, now.strftime("%H:%M"),
                          f"before first_entry_after "
                          f"({window.first_entry_at.strftime('%H:%M')})")
    if now > window.no_new_entry_after:
        return GateResult("session", False, now.strftime("%H:%M"),
                          f"after no_new_entry_after "
                          f"({window.no_new_entry_after.strftime('%H:%M')}) — "
                          "too late for the trade to work before square-off")
    return GateResult("session", True, now.strftime("%H:%M"))


def gate_no_open_position(open_count: int, max_open: int) -> GateResult:
    if open_count < max_open:
        return GateResult("open_positions", True, f"{open_count}/{max_open}")
    return GateResult("open_positions", False, f"{open_count}/{max_open}",
                      "already at the maximum concurrent positions")


# ── gate 1: regime bias (daily HMM) ───────────────────────────────────────

def gate_regime(regime: RegimeResult, direction: str, enabled: bool = True) -> GateResult:
    if not enabled:
        return GateResult("regime", True, "disabled", "regime gate disabled in config",
                          advisory=True)
    val = f"{regime.regime.value}@{regime.confidence:.2f}"
    if regime.regime.allows(direction):
        return GateResult("regime", True, val, regime.note)
    if regime.regime is Regime.CHOP:
        return GateResult("regime", False, val,
                          "regime is CHOP — buying premium in a range bleeds to theta")
    if regime.regime is Regime.UNKNOWN:
        return GateResult("regime", False, val,
                          f"regime unknown: {regime.note or 'insufficient history'}")
    return GateResult("regime", False, val,
                      f"regime is {regime.regime.value}, which does not permit {direction}")


# ── gate 2: intraday chop ─────────────────────────────────────────────────

def gate_intraday_chop(res: IntradayChopResult) -> GateResult:
    return GateResult("intraday_chop", res.is_trending,
                      f"ADX {res.adx:.1f}", res.reason)


# ── gate 3: trend ─────────────────────────────────────────────────────────

def gate_trend(close: float, ema_value: float, direction: str) -> GateResult:
    if np.isnan(ema_value):
        return GateResult("trend", False, "nan",
                          "EMA undefined — warm-up incomplete")
    val = f"close {close:.1f} vs EMA {ema_value:.1f}"
    ok = close > ema_value if direction == "CE" else close < ema_value
    if ok:
        return GateResult("trend", True, val)
    side = "above" if direction == "CE" else "below"
    return GateResult("trend", False, val, f"{direction} requires close {side} the EMA")


# ── gate 4: momentum ──────────────────────────────────────────────────────

def gate_momentum(rsi_value: float, direction: str,
                  ce_band: tuple[float, float],
                  pe_band: tuple[float, float]) -> GateResult:
    if np.isnan(rsi_value):
        return GateResult("momentum", False, "nan", "RSI undefined — warm-up incomplete")
    lo, hi = ce_band if direction == "CE" else pe_band
    val = f"RSI {rsi_value:.1f}"
    if lo <= rsi_value <= hi:
        return GateResult("momentum", True, val)
    where = "below" if rsi_value < lo else "above"
    return GateResult("momentum", False, val,
                      f"RSI {rsi_value:.1f} is {where} the [{lo:g}, {hi:g}] band")


# ── gate 5: volatility (IV percentile) ────────────────────────────────────

def gate_ivp(ivp: IVPResult, max_percentile: float) -> GateResult:
    ok, why = ivp.gate_passes(max_percentile)
    val = "n/a" if np.isnan(ivp.percentile) else f"IVP {ivp.percentile:.1f}"
    if ok and not ivp.sufficient and ivp.mode == "degraded":
        # Passes, but the human must be told the number is weak (defect B5).
        return GateResult("ivp", True, val, ivp.note)
    return GateResult("ivp", ok, val, why)


# ── gate 7: theta burn ────────────────────────────────────────────────────

def gate_theta(theta_per_hour: float | None, premium: float,
               expected_hold_hours: float, max_burn_pct: float) -> GateResult:
    """v1 specified 'overnight theta < 10% of premium' for what is an intraday
    system — the wrong units entirely (defect B6). Expressed here as burn over
    the EXPECTED HOLD."""
    if theta_per_hour is None or premium <= 0:
        return GateResult("theta", False, "n/a", "theta unavailable")
    burn = abs(theta_per_hour) * expected_hold_hours
    pct = burn / premium
    val = f"{pct:.1%} over {expected_hold_hours:g}h"
    if pct <= max_burn_pct:
        return GateResult("theta", True, val)
    return GateResult("theta", False, val,
                      f"theta would consume {pct:.1%} of premium over the expected "
                      f"hold (limit {max_burn_pct:.0%})")


# ── gate 8: net risk/reward ───────────────────────────────────────────────

def gate_net_rr(rr_net: float, minimum: float, warnings: list[str] | None = None) -> GateResult:
    """THE gate v1 got structurally wrong (defects A1/A2/A6).

    `rr_net` is computed on effective prices (buy the ask, sell the bid) net of
    every statutory charge. v1 compared a gross ratio against the same number
    its target was defined by, which is unreachable once costs exist.
    """
    val = f"{rr_net:.2f}"
    if warnings:
        return GateResult("net_rr", False, val, "; ".join(warnings))
    if rr_net >= minimum:
        return GateResult("net_rr", True, val)
    return GateResult("net_rr", False, val,
                      f"net-of-cost R/R {rr_net:.2f} below the {minimum:g} minimum")


# ── gate 9: tradeable market ──────────────────────────────────────────────

def gate_market(lots: int, reason: str | None) -> GateResult:
    """There is no sizing gate: the rule is one lot, always, whatever it costs.

    All that remains is whether a market exists to buy into. A zero or crossed
    bid/ask is a data problem, not a risk decision.
    """
    if lots >= 1:
        return GateResult("market", True, f"{lots} lot")
    return GateResult("market", False, "no market", reason or "no tradeable market")


# ── gate 10: risk governor ────────────────────────────────────────────────

def gate_risk_governor(allowed: bool, reason: str, state: str) -> GateResult:
    return GateResult("risk_governor", allowed, state,
                      "" if allowed else reason)


def gate_cooldown(last_signal_at: dt.datetime | None, now: dt.datetime,
                  cooldown_minutes: int) -> GateResult:
    if last_signal_at is None:
        return GateResult("cooldown", True, "none")
    elapsed = (now - last_signal_at).total_seconds() / 60.0
    val = f"{elapsed:.1f}m since last"
    if elapsed >= cooldown_minutes:
        return GateResult("cooldown", True, val)
    return GateResult("cooldown", False, val,
                      f"cooldown active — {cooldown_minutes - elapsed:.1f} minutes remain")


def gate_dedup(recent_keys: set[str], candidate_key: str) -> GateResult:
    """Prevents the same strike/side being re-signalled inside the dedup window."""
    if candidate_key not in recent_keys:
        return GateResult("dedup", True, candidate_key)
    return GateResult("dedup", False, candidate_key,
                      "this contract was already signalled inside the dedup window")


def gate_chain_fresh(age_sec: float, max_age_sec: float) -> GateResult:
    """Sizing off a stale chain is sizing off fiction."""
    val = f"{age_sec:.1f}s"
    if age_sec <= max_age_sec:
        return GateResult("chain_fresh", True, val)
    return GateResult("chain_fresh", False, val,
                      f"option chain is {age_sec:.0f}s old (limit {max_age_sec:g}s)")


def gate_expiry_safety(expiry: str, today: dt.date,
                       allow_expiry_day: bool = False) -> GateResult:
    """v1 defect A3: never open a position in a contract expiring today unless
    explicitly enabled — settlement takes your exit price out of your hands."""
    try:
        exp = dt.date.fromisoformat(expiry[:10])
    except (ValueError, TypeError):
        return GateResult("expiry_safety", False, str(expiry), "unparseable expiry date")
    days = (exp - today).days
    val = f"{days}d to expiry"
    if days < 0:
        return GateResult("expiry_safety", False, val, "contract has already expired")
    if days == 0 and not allow_expiry_day:
        return GateResult("expiry_safety", False, val,
                          "expires today — entries disabled on expiry day")
    return GateResult("expiry_safety", True, val)
