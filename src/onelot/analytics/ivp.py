"""IV Percentile with explicit data-sufficiency handling.

*** v1 defect B5 ***
IV Percentile is by definition a rank over ~252 sessions. On day one you have
zero. v1 would have computed a percentile over a 1-element series and got 0 or
100 — silently passing or silently blocking every single trade, with no
indication that the number was meaningless.

Here, insufficiency is a first-class state with three configured behaviours:
    strict   — block all signals until history is adequate (the default)
    degraded — rank over whatever exists, flagged loudly on every alert
    off      — skip the gate entirely, recorded in the audit trail

The ATM IV history is accumulated from our own recorded chain snapshots, so it
is Bank Nifty's own volatility rather than a proxy.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

import numpy as np

from .indicators import percentile_rank

Mode = Literal["strict", "degraded", "off"]


@dataclass(frozen=True)
class IVPResult:
    percentile: float          # 0-100, NaN if undefined
    iv_rank: float             # (cur - min) / (max - min) * 100
    current_iv: float
    n_observations: int
    required_observations: int
    sufficient: bool
    mode: Mode
    usable: bool               # may this value gate a signal at all?
    note: str

    def gate_passes(self, max_percentile: float) -> tuple[bool, str]:
        if self.mode == "off":
            return True, "IVP gate disabled in config"
        if not self.usable:
            return False, self.note
        if np.isnan(self.percentile):
            return False, "IV percentile undefined"
        ok = self.percentile < max_percentile
        return ok, f"IVP {self.percentile:.1f} {'<' if ok else '>='} {max_percentile}"

    def as_dict(self) -> dict:
        return {
            "percentile": None if np.isnan(self.percentile) else round(self.percentile, 2),
            "iv_rank": None if np.isnan(self.iv_rank) else round(self.iv_rank, 2),
            "current_iv": round(self.current_iv, 5),
            "n_observations": self.n_observations,
            "required_observations": self.required_observations,
            "sufficient": self.sufficient, "mode": self.mode,
            "usable": self.usable, "note": self.note,
        }


class IVPercentileEngine:
    def __init__(self, mode: Mode = "strict", lookback_days: int = 252,
                 min_history_days: int = 60):
        self.mode = mode
        self.lookback_days = lookback_days
        self.min_history_days = min_history_days

    def compute(self, history: list[float] | np.ndarray, current_iv: float) -> IVPResult:
        h = np.asarray([x for x in np.asarray(history, dtype=float) if not np.isnan(x)])
        if self.lookback_days and len(h) > self.lookback_days:
            h = h[-self.lookback_days:]
        n = len(h)
        sufficient = n >= self.min_history_days

        if np.isnan(current_iv) or current_iv <= 0:
            return IVPResult(float("nan"), float("nan"), current_iv, n,
                             self.min_history_days, sufficient, self.mode, False,
                             "current ATM IV unavailable")

        if self.mode == "off":
            return IVPResult(float("nan"), float("nan"), current_iv, n,
                             self.min_history_days, sufficient, self.mode, True,
                             "IVP gate disabled")

        if n == 0:
            return IVPResult(float("nan"), float("nan"), current_iv, 0,
                             self.min_history_days, False, self.mode, False,
                             "no ATM IV history recorded yet")

        pct = percentile_rank(h, current_iv)
        lo, hi = float(np.min(h)), float(np.max(h))
        rank = 100.0 * (current_iv - lo) / (hi - lo) if hi > lo else float("nan")

        if sufficient:
            return IVPResult(pct, rank, current_iv, n, self.min_history_days, True,
                             self.mode, True, f"{n} sessions of ATM IV history")

        if self.mode == "strict":
            return IVPResult(pct, rank, current_iv, n, self.min_history_days, False,
                             self.mode, False,
                             f"only {n}/{self.min_history_days} sessions of IV history — "
                             f"IVP is not yet meaningful (mode=strict blocks signals)")

        return IVPResult(pct, rank, current_iv, n, self.min_history_days, False,
                         self.mode, True,
                         f"DEGRADED: IVP over only {n}/{self.min_history_days} sessions — "
                         f"treat this number with suspicion")


def atm_iv_from_chain(rows: list[dict], spot_or_future: float) -> float:
    """Extract a representative ATM IV: the average of the CE and PE IVs at the
    strike nearest the underlying. Averaging the two sides cancels most of the
    put/call skew and any one-sided quote staleness.
    """
    if not rows or spot_or_future <= 0:
        return float("nan")
    strikes = sorted({float(r["strike"]) for r in rows if r.get("strike") is not None})
    if not strikes:
        return float("nan")
    atm = min(strikes, key=lambda s: abs(s - spot_or_future))
    ivs = [
        float(r["iv"]) for r in rows
        if r.get("strike") is not None and float(r["strike"]) == atm
        and r.get("iv") is not None and float(r["iv"]) > 0
    ]
    return float(np.mean(ivs)) if ivs else float("nan")


def today_key(ts: dt.datetime) -> str:
    return ts.date().isoformat()
