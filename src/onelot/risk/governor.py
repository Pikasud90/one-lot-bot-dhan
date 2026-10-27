"""Risk governor — six kill switches, persisted across restarts.

*** v1 defect A5 ***
v1 had no daily loss limit, no trade cap, no consecutive-loss breaker, no cap
on concurrent positions and no manual halt. On a bad day it would have fired
signal after signal into a trend that was eating the account, and a restart
would have wiped whatever informal memory existed.

HALT is sticky: it persists to the database, survives a restart, and requires
an explicit operator reset. A kill switch you can accidentally clear by
restarting the process is not a kill switch.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from enum import Enum

from ..config import RiskCfg

log = logging.getLogger(__name__)


class RiskState(str, Enum):
    ACTIVE = "ACTIVE"
    HALTED = "HALTED"


@dataclass
class RiskSnapshot:
    trade_date: str
    mode: str
    state: RiskState
    realized_pnl: float = 0.0
    cumulative_pnl: float = 0.0
    drawdown: float = 0.0
    peak_equity: float = 0.0
    trades_taken: int = 0
    consecutive_losses: int = 0
    open_positions: int = 0
    halt_reason: str | None = None
    halted_at: dt.datetime | None = None
    manual_halt: bool = False
    standing_halt: bool = False
    limits: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "trade_date": self.trade_date, "mode": self.mode, "state": self.state.value,
            "realized_pnl": round(self.realized_pnl, 2),
            "cumulative_pnl": round(self.cumulative_pnl, 2),
            "drawdown": round(self.drawdown, 2),
            "peak_equity": round(self.peak_equity, 2),
            "trades_taken": self.trades_taken,
            "consecutive_losses": self.consecutive_losses,
            "open_positions": self.open_positions,
            "halt_reason": self.halt_reason,
            "halted_at": self.halted_at.isoformat() if self.halted_at else None,
            "manual_halt": self.manual_halt,
            "standing_halt": self.standing_halt,
            "limits": dict(self.limits),
        }


class RiskGovernor:
    def __init__(self, cfg: RiskCfg, capital: float, mode: str, trade_date: str,
                 repo=None):
        self.cfg = cfg
        self.capital = capital
        self.mode = mode
        self.trade_date = trade_date
        self.repo = repo

        self.realized_pnl = 0.0
        self.trades_taken = 0
        self.consecutive_losses = 0
        self.open_positions = 0
        self.state = RiskState.ACTIVE
        self.halt_reason: str | None = None
        self.halted_at: dt.datetime | None = None
        self.manual_halt = False
        self._standing_halt = False
        self.cumulative_pnl = 0.0
        self.peak_equity = 0.0
        self.drawdown = 0.0

        if repo is not None:
            self._load()
            self._load_equity_curve()

    # ── persistence ───────────────────────────────────────────────────────
    # Halts that OUTLIVE a session are stored under this key rather than
    # under a trade date. A cumulative drawdown halt, a consecutive-loss halt
    # and a manual panic halt must all survive midnight; only the DAILY loss
    # limit is allowed to reset with the date.
    GLOBAL_KEY = "__GLOBAL__"

    def _load(self) -> None:
        row = self.repo.get_risk_state(self.trade_date, self.mode)
        if row:
            self.realized_pnl = float(row.get("realized_pnl") or 0.0)
            self.trades_taken = int(row.get("trades_taken") or 0)
            self.consecutive_losses = int(row.get("consecutive_losses") or 0)
            if row.get("halted"):
                self.state = RiskState.HALTED
                self.halt_reason = row.get("halt_reason")
                self.halted_at = row.get("halted_at")
                log.warning("Restored today's HALT (%s): %s",
                            self.trade_date, self.halt_reason)

        # A halt from any previous session still binds.
        g = self.repo.get_risk_state(self.GLOBAL_KEY, self.mode)
        if g and g.get("halted"):
            self.state = RiskState.HALTED
            self.halt_reason = g.get("halt_reason")
            self.halted_at = g.get("halted_at")
            self.manual_halt = bool(g.get("manual_halt"))
            self.consecutive_losses = max(
                self.consecutive_losses, int(g.get("consecutive_losses") or 0))
            log.warning("Restored a STANDING halt that outlives the session: %s",
                        self.halt_reason)

    def _load_equity_curve(self) -> None:
        """Rebuild the cumulative equity curve from the journal.

        Deliberately derived from closed trades rather than a running counter:
        a counter can drift, and the journal is the record of what actually
        happened.
        """
        try:
            rows = self.repo.all_journal(mode=self.mode, limit=100_000)
        except Exception:
            return
        rows = sorted(rows, key=lambda r: r["closed_at"])
        equity = 0.0
        peak = 0.0
        for r in rows:
            equity += float(r["net_pnl"])
            peak = max(peak, equity)
        self.cumulative_pnl = equity
        self.peak_equity = peak
        self.drawdown = max(0.0, peak - equity)

    @property
    def max_drawdown_limit(self) -> float:
        return self.cfg.max_drawdown_inr

    def _check_drawdown(self, now: dt.datetime | None = None) -> bool:
        if self.drawdown >= self.max_drawdown_limit > 0:
            self.halt(
                f"CUMULATIVE DRAWDOWN LIMIT: Rs {self.drawdown:,.0f} from peak "
                f"(limit Rs {self.max_drawdown_limit:,.0f}). Stop trading and "
                "review the strategy before resetting.", now)
            return True
        return False

    def _save(self, now: dt.datetime | None = None) -> None:
        if self.repo is None:
            return
        stamp = now or dt.datetime.now(dt.timezone.utc)
        self.repo.save_risk_state({
            "trade_date": self.trade_date, "mode": self.mode,
            "realized_pnl": self.realized_pnl, "trades_taken": self.trades_taken,
            "consecutive_losses": self.consecutive_losses,
            "halted": self.state is RiskState.HALTED,
            "halt_reason": self.halt_reason, "halted_at": self.halted_at,
            "manual_halt": self.manual_halt, "updated_at": stamp,
        })
        # Mirror standing halts to the global key so tomorrow still sees them.
        self.repo.save_risk_state({
            "trade_date": self.GLOBAL_KEY, "mode": self.mode,
            "realized_pnl": self.cumulative_pnl, "trades_taken": 0,
            "consecutive_losses": self.consecutive_losses,
            "halted": self._standing_halt,
            "halt_reason": self.halt_reason if self._standing_halt else None,
            "halted_at": self.halted_at if self._standing_halt else None,
            "manual_halt": self.manual_halt, "updated_at": stamp,
        })

    # ── limits ────────────────────────────────────────────────────────────
    @property
    def daily_loss_limit(self) -> float:
        """Absolute rupees. With a fixed lot the loss on a trade is a rupee
        amount set by the market, not a percentage of anything — and this way
        the engine never needs to know your account size."""
        return self.cfg.daily_loss_limit_inr

    def limits_dict(self) -> dict:
        return {
            "daily_loss_limit": round(self.daily_loss_limit, 2),
            "max_drawdown_limit": round(self.max_drawdown_limit, 2),
            "max_trades_per_day": self.cfg.max_trades_per_day,
            "max_consecutive_losses": self.cfg.max_consecutive_losses,
            "max_open_positions": self.cfg.max_open_positions,
            "lots_per_trade": self.cfg.lots_per_trade,
        }

    # ── the decision ──────────────────────────────────────────────────────
    def may_open(self, now: dt.datetime | None = None) -> tuple[bool, str]:
        """All six switches, checked every single time."""
        if self.state is RiskState.HALTED:
            kind = "MANUAL HALT" if self.manual_halt else "HALTED"
            return False, f"{kind}: {self.halt_reason or 'no reason recorded'}"

        if self.realized_pnl <= -self.daily_loss_limit:
            self.halt(f"daily loss limit reached: Rs {self.realized_pnl:,.0f} "
                      f"vs limit Rs {-self.daily_loss_limit:,.0f}", now,
                      standing=False)
            return False, self.halt_reason or ""

        if self.trades_taken >= self.cfg.max_trades_per_day:
            return False, (f"daily trade cap reached "
                           f"({self.trades_taken}/{self.cfg.max_trades_per_day})")

        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            self.halt(f"{self.consecutive_losses} consecutive losses "
                      f"(limit {self.cfg.max_consecutive_losses}) — "
                      "stop trading and review", now)
            return False, self.halt_reason or ""

        if self._check_drawdown(now):
            return False, self.halt_reason or ""

        if self.open_positions >= self.cfg.max_open_positions:
            return False, (f"already at max concurrent positions "
                           f"({self.open_positions}/{self.cfg.max_open_positions})")

        return True, "ok"

    # ── mutation ──────────────────────────────────────────────────────────
    def record_open(self, now: dt.datetime | None = None) -> None:
        self.trades_taken += 1
        self.open_positions += 1
        self._save(now)

    def record_close(self, net_pnl: float, now: dt.datetime | None = None) -> None:
        self.realized_pnl += net_pnl
        self.cumulative_pnl += net_pnl
        self.peak_equity = max(self.peak_equity, self.cumulative_pnl)
        self.drawdown = max(0.0, self.peak_equity - self.cumulative_pnl)
        self.open_positions = max(0, self.open_positions - 1)
        if net_pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        if self._check_drawdown(now):
            pass
        elif self.realized_pnl <= -self.daily_loss_limit:
            self.halt(f"daily loss limit breached: Rs {self.realized_pnl:,.0f}", now,
                      standing=False)
        elif self.consecutive_losses >= self.cfg.max_consecutive_losses:
            self.halt(f"{self.consecutive_losses} consecutive losses", now)
        self._save(now)

    def halt(self, reason: str, now: dt.datetime | None = None,
             manual: bool = False, standing: bool = True) -> None:
        """Stop trading.

        `standing=True` means the halt outlives the session — cumulative
        drawdown, consecutive losses and manual halts all do. Only the daily
        loss limit passes `standing=False`, because that is the one limit that
        is genuinely about today.
        """
        if self.state is RiskState.HALTED:
            return
        self.state = RiskState.HALTED
        self.halt_reason = reason
        self.halted_at = now or dt.datetime.now(dt.timezone.utc)
        self.manual_halt = manual
        self._standing_halt = standing
        log.error("RISK HALT (%s%s): %s", "manual" if manual else "automatic",
                  ", standing" if standing else ", today only", reason)
        self._save(now)

    def manual_panic_halt(self, now: dt.datetime | None = None) -> None:
        """The big red button. Standing by definition — a panic halt that
        evaporated overnight would be worse than no button at all."""
        self.state = RiskState.ACTIVE          # so halt() does not early-return
        self.halt("manual panic halt by operator", now, manual=True, standing=True)

    def reset_halt(self, now: dt.datetime | None = None) -> None:
        """Deliberate operator action only — never automatic, never on restart."""
        log.warning("Risk halt cleared by operator (was: %s)", self.halt_reason)
        self.state = RiskState.ACTIVE
        self.halt_reason = None
        self.halted_at = None
        self.manual_halt = False
        self._standing_halt = False
        self.consecutive_losses = 0
        self.drawdown = 0.0
        self.peak_equity = self.cumulative_pnl
        self._save(now)

    def sync_open_positions(self, count: int) -> None:
        self.open_positions = count

    def snapshot(self) -> RiskSnapshot:
        return RiskSnapshot(
            trade_date=self.trade_date, mode=self.mode, state=self.state,
            realized_pnl=self.realized_pnl, cumulative_pnl=self.cumulative_pnl,
            drawdown=self.drawdown, peak_equity=self.peak_equity,
            trades_taken=self.trades_taken,
            consecutive_losses=self.consecutive_losses,
            open_positions=self.open_positions, halt_reason=self.halt_reason,
            halted_at=self.halted_at, manual_halt=self.manual_halt,
            standing_halt=self._standing_halt, limits=self.limits_dict(),
        )
