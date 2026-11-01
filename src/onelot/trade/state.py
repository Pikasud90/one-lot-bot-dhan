"""Persisted trade state machine with crash recovery.

*** v1 defect D5 ***
v1 persisted nothing. A power cut at 11:00 with a live position meant the
Guardian vanished, the trailing stop vanished, and the engine restarted
believing it was flat — while real money sat exposed in the market.

Here every transition is written to the database BEFORE it is acted on
(INVARIANT I5), and `recover()` rebuilds open positions at boot.

*** v1 defect D7 ***
The dashboard and Telegram were two sources of truth for the single most
safety-critical action in the system. `acknowledge()` is idempotent and
atomic at the database level, so a double-click, a retried request, or two
clients acking at once cannot produce two positions.

    IDLE ─► SIGNAL_PENDING ─┬─► TAKEN ─► POSITION_OPEN ─► EXIT_PENDING ─► CLOSED
                            ├─► SKIPPED
                            └─► EXPIRED (TTL)
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum

from ..clock import now_ist

log = logging.getLogger(__name__)


class SignalStatus(str, Enum):
    PENDING = "PENDING"
    TAKEN = "TAKEN"
    SKIPPED = "SKIPPED"
    EXPIRED = "EXPIRED"


class PositionState(str, Enum):
    POSITION_OPEN = "POSITION_OPEN"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"


class ExitReason(str, Enum):
    """A closed set. Every exit is attributed, so the journal can tell you
    which rule actually makes or loses money."""

    HARD_SL = "HARD_SL"
    TRAIL_SL = "TRAIL_SL"
    TARGET = "TARGET"
    TIME_STOP = "TIME_STOP"
    DIVERGENCE = "DIVERGENCE"
    REGIME_FLIP = "REGIME_FLIP"
    EOD_SQUAREOFF = "EOD_SQUAREOFF"
    MANUAL = "MANUAL"


@dataclass
class Position:
    position_id: str
    signal_id: str | None
    state: PositionState
    opened_at: dt.datetime
    direction: str
    underlying: str
    expiry: str
    strike: float
    opt_type: str
    security_id: str | None
    lots: int
    qty: int
    lot_size: int
    entry_price: float
    initial_stop: float
    target: float
    r_per_unit: float
    trail_stop: float | None = None
    trail_active: bool = False
    high_water_premium: float | None = None
    exit_price: float | None = None
    exit_reason: ExitReason | None = None
    closed_at: dt.datetime | None = None
    degraded_data: bool = False
    mode: str = "PAPER"
    payload: dict = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.state in (PositionState.POSITION_OPEN, PositionState.EXIT_PENDING)

    @property
    def effective_stop(self) -> float:
        """The stop actually in force: the trail if it has ratcheted above the
        initial stop, otherwise the initial stop. Never lower than initial."""
        if self.trail_active and self.trail_stop is not None:
            return max(self.trail_stop, self.initial_stop)
        return self.initial_stop

    def unrealised_r(self, premium: float) -> float:
        if self.r_per_unit <= 0:
            return 0.0
        return (premium - self.entry_price) / self.r_per_unit

    def hold_minutes(self, now: dt.datetime) -> float:
        return (now - self.opened_at).total_seconds() / 60.0

    def as_dict(self) -> dict:
        return {
            "position_id": self.position_id,
            "signal_id": self.signal_id,
            "state": self.state.value,
            "opened_at": self.opened_at.isoformat(),
            "direction": self.direction,
            "underlying": self.underlying,
            "expiry": self.expiry,
            "strike": self.strike,
            "opt_type": self.opt_type,
            "security_id": self.security_id,
            "lots": self.lots,
            "qty": self.qty,
            "lot_size": self.lot_size,
            "entry_price": self.entry_price,
            "initial_stop": self.initial_stop,
            "target": self.target,
            "r_per_unit": round(self.r_per_unit, 4),
            "trail_stop": self.trail_stop,
            "trail_active": self.trail_active,
            "effective_stop": self.effective_stop,
            "high_water_premium": self.high_water_premium,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason.value if self.exit_reason else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "degraded_data": self.degraded_data,
            "mode": self.mode,
        }

    @classmethod
    def from_row(cls, row: dict) -> Position:
        from ..clock import to_ist

        return cls(
            position_id=row["position_id"],
            signal_id=row.get("signal_id"),
            state=PositionState(row["state"]),
            opened_at=to_ist(row["opened_at"]),
            direction=row["direction"],
            underlying=row.get("underlying") or "",
            expiry=row.get("expiry") or "",
            strike=float(row.get("strike") or 0.0),
            opt_type=row.get("opt_type") or "",
            security_id=row.get("security_id"),
            lots=int(row.get("lots") or 0),
            qty=int(row.get("qty") or 0),
            lot_size=int(row.get("lot_size") or 0),
            entry_price=float(row["entry_price"]),
            initial_stop=float(row["initial_stop"]),
            target=float(row.get("target") or 0.0),
            r_per_unit=float(row.get("r_per_unit") or 0.0),
            trail_stop=row.get("trail_stop"),
            trail_active=bool(row.get("trail_active")),
            high_water_premium=row.get("high_water_premium"),
            exit_price=row.get("exit_price"),
            exit_reason=ExitReason(row["exit_reason"]) if row.get("exit_reason") else None,
            closed_at=to_ist(row["closed_at"]) if row.get("closed_at") else None,
            degraded_data=bool(row.get("degraded_data")),
            mode=row.get("mode") or "PAPER",
            payload=row.get("payload") or {},
        )


@dataclass
class AckResult:
    accepted: bool
    status: SignalStatus | None
    position: Position | None
    message: str

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "status": self.status.value if self.status else None,
            "position": self.position.as_dict() if self.position else None,
            "message": self.message,
        }


class TradeStateMachine:
    def __init__(self, repo, cost_model, cfg, governor, journal=None):
        self.repo = repo
        self.cost_model = cost_model
        self.cfg = cfg
        self.governor = governor
        self.journal = journal
        self.open_positions: dict[str, Position] = {}

    # ── recovery (v1 defect D5) ───────────────────────────────────────────
    def recover(self) -> list[Position]:
        """Rebuild live state at boot. Called before anything else runs."""
        rows = self.repo.open_positions(mode=self.cfg.mode)
        recovered = [Position.from_row(r) for r in rows]
        self.open_positions = {p.position_id: p for p in recovered}
        self.governor.sync_open_positions(len(recovered))
        if recovered:
            log.warning(
                "RECOVERED %d open position(s) from the database. The Guardian "
                "is resuming; verify against your broker terminal before acting.",
                len(recovered),
            )
            for p in recovered:
                log.warning(
                    "  %s %s %g %s x%d @ %.2f, stop %.2f (trail %s)",
                    p.position_id,
                    p.direction,
                    p.strike,
                    p.opt_type,
                    p.lots,
                    p.entry_price,
                    p.effective_stop,
                    f"{p.trail_stop:.2f}" if p.trail_stop else "inactive",
                )
        return recovered

    # ── signals ───────────────────────────────────────────────────────────
    def register_signal(self, signal) -> None:
        e = signal.economics
        self.repo.insert_signal(
            {
                "signal_id": signal.signal_id,
                "eval_id": None,
                "created_at": signal.created_at,
                "expires_at": signal.expires_at,
                "direction": signal.direction,
                "underlying": signal.underlying,
                "expiry": signal.expiry,
                "strike": signal.strike,
                "opt_type": signal.opt_type,
                "security_id": signal.security_id,
                "quote_bid": signal.quote.bid,
                "quote_ask": signal.quote.ask,
                "quote_ltp": signal.quote.ltp,
                "entry_eff": e.entry_eff,
                "stop_level": e.stop_level,
                "target_level": e.target_level,
                "breakeven": e.breakeven,
                "lots": signal.lots,
                "qty": signal.qty,
                "lot_size": signal.lot_size,
                "net_risk": e.net_risk,
                "net_reward": e.net_reward,
                "rr_net": e.rr_net,
                "delta": signal.delta,
                "iv": signal.iv,
                "theta_per_hour": signal.theta_per_hour,
                "status": SignalStatus.PENDING.value,
                "payload": signal.as_dict(),
            }
        )

    def expire_stale(self, now: dt.datetime | None = None) -> int:
        return self.repo.expire_stale_signals(now or now_ist())

    # ── acknowledgement (idempotent — v1 defect D7) ───────────────────────
    def acknowledge(
        self,
        signal_id: str,
        action: str,
        source: str = "dashboard",
        fill_price: float | None = None,
        now: dt.datetime | None = None,
    ) -> AckResult:
        """Record the human's decision. Safe to call twice; the second is a no-op.

        `fill_price` is the price the human ACTUALLY got. It is deliberately
        separate from the signalled price: you rarely fill exactly where the
        alert said, and the Guardian must manage reality, not the engine's
        opinion of reality.
        """
        now = now or now_ist()
        row = self.repo.get_signal(signal_id)
        if row is None:
            return AckResult(False, None, None, f"unknown signal {signal_id}")

        current = SignalStatus(row["status"])
        if current is not SignalStatus.PENDING:
            return AckResult(
                False,
                current,
                self.open_positions.get(str(row.get("signal_id"))),
                f"signal already resolved as {current.value} — ignoring duplicate ack",
            )

        if now >= row["expires_at"].astimezone(now.tzinfo):
            self.repo.resolve_signal(signal_id, SignalStatus.EXPIRED.value, "ttl", now)
            return AckResult(
                False, SignalStatus.EXPIRED, None, "signal TTL expired before acknowledgement"
            )

        if action.upper() in ("SKIP", "SKIPPED"):
            ok = self.repo.resolve_signal(signal_id, SignalStatus.SKIPPED.value, source, now)
            return AckResult(
                ok, SignalStatus.SKIPPED, None, "skipped" if ok else "already resolved"
            )

        if action.upper() not in ("TAKE", "TAKEN"):
            return AckResult(False, current, None, f"unknown action {action!r}")

        allowed, why = self.governor.may_open(now)
        if not allowed:
            self.repo.resolve_signal(
                signal_id, SignalStatus.SKIPPED.value, f"{source}:risk_block", now
            )
            return AckResult(False, SignalStatus.SKIPPED, None, f"risk governor refused: {why}")

        # ATOMIC: only the first ack flips PENDING -> TAKEN.
        if not self.repo.resolve_signal(signal_id, SignalStatus.TAKEN.value, source, now):
            return AckResult(False, None, None, "another client acknowledged this signal first")

        pos = self._open_position(row, fill_price, now)
        return AckResult(
            True,
            SignalStatus.TAKEN,
            pos,
            f"position {pos.position_id} opened at {pos.entry_price:.2f}",
        )

    def _open_position(self, sig_row: dict, fill_price: float | None, now: dt.datetime) -> Position:
        entry = float(fill_price if fill_price is not None else sig_row["entry_eff"])
        signalled_stop = float(sig_row["stop_level"])
        signalled_entry = float(sig_row["entry_eff"])
        r_per_unit = max(signalled_entry - signalled_stop, self.cfg.instrument.tick_size)

        # The stop travels WITH the actual fill, so risk stays the intended R
        # rather than whatever the fill difference happened to make it.
        initial_stop = round(entry - r_per_unit, 2)
        target = round(entry + self.cfg.strategy.exits.rr_target_gross * r_per_unit, 2)

        pos = Position(
            position_id=uuid.uuid4().hex[:16],
            signal_id=sig_row["signal_id"],
            state=PositionState.POSITION_OPEN,
            opened_at=now,
            direction=sig_row["direction"],
            underlying=sig_row["underlying"],
            expiry=sig_row["expiry"],
            strike=float(sig_row["strike"]),
            opt_type=sig_row["opt_type"],
            security_id=sig_row.get("security_id"),
            lots=int(sig_row["lots"]),
            qty=int(sig_row["qty"]),
            lot_size=int(sig_row["lot_size"]),
            entry_price=entry,
            initial_stop=initial_stop,
            target=target,
            r_per_unit=r_per_unit,
            high_water_premium=entry,
            mode=self.cfg.mode,
            payload={
                "signalled_entry": signalled_entry,
                "fill_vs_signal": round(entry - signalled_entry, 2),
            },
        )
        self.repo.insert_position(
            {
                "position_id": pos.position_id,
                "signal_id": pos.signal_id,
                "state": pos.state.value,
                "opened_at": pos.opened_at,
                "direction": pos.direction,
                "underlying": pos.underlying,
                "expiry": pos.expiry,
                "strike": pos.strike,
                "opt_type": pos.opt_type,
                "security_id": pos.security_id,
                "lots": pos.lots,
                "qty": pos.qty,
                "lot_size": pos.lot_size,
                "entry_price": pos.entry_price,
                "initial_stop": pos.initial_stop,
                "target": pos.target,
                "trail_stop": None,
                "trail_active": False,
                "high_water_premium": pos.high_water_premium,
                "r_per_unit": pos.r_per_unit,
                "mode": pos.mode,
                "payload": pos.payload,
            }
        )
        self.open_positions[pos.position_id] = pos
        self.governor.record_open(now)
        log.info(
            "Position OPEN %s: %s %g %s x%d @ %.2f (stop %.2f, target %.2f)",
            pos.position_id,
            pos.direction,
            pos.strike,
            pos.opt_type,
            pos.lots,
            pos.entry_price,
            pos.initial_stop,
            pos.target,
        )
        return pos

    # ── position updates ──────────────────────────────────────────────────
    def update_trail(
        self, position_id: str, trail_stop: float, trail_active: bool, high_water: float
    ) -> None:
        pos = self.open_positions.get(position_id)
        if pos is None:
            return
        pos.trail_stop = trail_stop
        pos.trail_active = trail_active
        pos.high_water_premium = high_water
        self.repo.update_position(
            position_id,
            trail_stop=trail_stop,
            trail_active=trail_active,
            high_water_premium=high_water,
        )

    def request_exit(self, position_id: str, reason: ExitReason) -> Position | None:
        pos = self.open_positions.get(position_id)
        if pos is None or pos.state is not PositionState.POSITION_OPEN:
            return None
        pos.state = PositionState.EXIT_PENDING
        pos.exit_reason = reason
        self.repo.update_position(position_id, state=pos.state.value, exit_reason=reason.value)
        log.warning("EXIT SIGNALLED %s: %s", position_id, reason.value)
        return pos

    def confirm_exit(
        self,
        position_id: str,
        exit_price: float,
        reason: ExitReason | None = None,
        now: dt.datetime | None = None,
    ) -> dict | None:
        """Close the position with the price the human ACTUALLY got, write the
        net-P&L journal entry, and feed the result to the risk governor."""
        now = now or now_ist()
        pos = self.open_positions.get(position_id)
        if pos is None:
            row = self.repo.get_position(position_id)
            if row is None:
                return None
            pos = Position.from_row(row)
        if pos.state is PositionState.CLOSED:
            return None

        pos.exit_price = float(exit_price)
        pos.exit_reason = reason or pos.exit_reason or ExitReason.MANUAL
        pos.state = PositionState.CLOSED
        pos.closed_at = now

        self.repo.update_position(
            position_id,
            state=pos.state.value,
            exit_price=pos.exit_price,
            exit_reason=pos.exit_reason.value,
            closed_at=now,
        )
        self.open_positions.pop(position_id, None)

        entry_cost = self.cost_model.leg(pos.entry_price, pos.qty, "buy")
        exit_cost = self.cost_model.leg(pos.exit_price, pos.qty, "sell")
        gross = (pos.exit_price - pos.entry_price) * pos.qty
        net = gross - entry_cost.total - exit_cost.total

        entry = {
            "trade_id": uuid.uuid4().hex[:16],
            "position_id": position_id,
            "trade_date": pos.opened_at.date().isoformat(),
            "closed_at": now,
            "direction": pos.direction,
            "strike": pos.strike,
            "opt_type": pos.opt_type,
            "expiry": pos.expiry,
            "lots": pos.lots,
            "qty": pos.qty,
            "entry_price": pos.entry_price,
            "exit_price": pos.exit_price,
            "gross_pnl": gross,
            "entry_cost": entry_cost.total,
            "exit_cost": exit_cost.total,
            "total_cost": entry_cost.total + exit_cost.total,
            "net_pnl": net,
            "r_multiple": (
                round((pos.exit_price - pos.entry_price) / pos.r_per_unit, 3)
                if pos.r_per_unit > 0
                else 0.0
            ),
            "exit_reason": pos.exit_reason.value,
            "hold_minutes": round(pos.hold_minutes(now), 2),
            "mode": pos.mode,
            "cost_breakdown": {"entry": entry_cost.as_dict(), "exit": exit_cost.as_dict()},
        }
        self.repo.insert_journal(entry)
        self.governor.record_close(net, now)
        # NOTE: printf-style formatting has NO comma-grouping flag. "%+,.0f"
        # raises ValueError, which logging turns into a swallowed "Logging
        # error" -- so this line would have failed on every single position
        # close. Pre-format with an f-string instead.
        log.info(
            "Position CLOSED %s: %s @ %.2f -> net %s (%.2fR)",
            position_id,
            pos.exit_reason.value,
            pos.exit_price,
            f"Rs {net:+,.0f}",
            entry["r_multiple"],
        )
        return entry

    def get(self, position_id: str) -> Position | None:
        return self.open_positions.get(position_id)

    def any_open(self) -> bool:
        return bool(self.open_positions)
