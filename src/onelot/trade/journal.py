"""Trade journal and performance metrics — net of every cost.

v1 produced signals and never recorded what happened to them, so there was no
way to compute expectancy, no way to see which exit rule earns its keep, and
no evidence base for changing a parameter. This is the feedback loop.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

import numpy as np


@dataclass
class JournalEntry:
    trade_id: str
    trade_date: str
    direction: str
    strike: float
    opt_type: str
    lots: int
    qty: int
    entry_price: float
    exit_price: float
    gross_pnl: float
    total_cost: float
    net_pnl: float
    r_multiple: float
    exit_reason: str
    hold_minutes: float
    mode: str


class TradeJournal:
    def __init__(self, repo):
        self.repo = repo

    def entries(self, mode: str | None = None, limit: int = 5000) -> list[dict]:
        return self.repo.all_journal(mode=mode, limit=limit)

    def metrics(self, mode: str | None = None) -> dict:
        rows = self.entries(mode)
        if not rows:
            return {"trades": 0, "note": "no closed trades yet"}

        net = np.array([float(r["net_pnl"]) for r in rows])
        gross = np.array([float(r["gross_pnl"]) for r in rows])
        costs = np.array([float(r["total_cost"]) for r in rows])
        rmul = np.array([float(r["r_multiple"] or 0.0) for r in rows])

        wins, losses = net[net > 0], net[net <= 0]
        gross_profit = float(wins.sum()) if len(wins) else 0.0
        gross_loss = float(-losses.sum()) if len(losses) else 0.0

        # Chronological equity curve for drawdown.
        chrono = sorted(rows, key=lambda r: r["closed_at"])
        equity = np.cumsum([float(r["net_pnl"]) for r in chrono])
        peak = np.maximum.accumulate(equity)
        max_dd = float(np.max(peak - equity)) if len(equity) else 0.0

        by_reason: dict[str, dict] = {}
        for r in rows:
            k = r["exit_reason"] or "UNKNOWN"
            b = by_reason.setdefault(k, {"count": 0, "net_pnl": 0.0, "wins": 0})
            b["count"] += 1
            b["net_pnl"] += float(r["net_pnl"])
            b["wins"] += int(float(r["net_pnl"]) > 0)
        for b in by_reason.values():
            b["net_pnl"] = round(b["net_pnl"], 2)
            b["win_rate"] = round(b["wins"] / b["count"], 3)

        total_gross = float(gross.sum())

        # ── risk-adjusted return ──────────────────────────────────────────
        # Sharpe is computed on DAILY net P&L in rupees, not on returns.
        # That is deliberate: Sharpe = mean(r)/std(r), and for r = pnl/capital
        # the capital term cancels in the ratio. So this number is identical
        # to the return-based Sharpe under constant capital, WITHOUT needing
        # to know the capital.
        by_day: dict[str, float] = {}
        for r in rows:
            by_day[r["trade_date"]] = by_day.get(r["trade_date"], 0.0) + float(r["net_pnl"])
        daily = np.array(list(by_day.values()), dtype=float)

        # Annualising by sqrt(252) from a handful of trading days produces a
        # confident-looking number built on almost nothing. Below this many
        # days the figure is reported as None with `sharpe_note` explaining
        # why, rather than printed as if it meant something.
        MIN_DAYS_FOR_SHARPE = 20
        sharpe = sortino = None
        sharpe_note = None
        if len(daily) < MIN_DAYS_FOR_SHARPE:
            sharpe_note = (
                f"not computed: {len(daily)} trading days, "
                f"need {MIN_DAYS_FOR_SHARPE}. Annualising from a "
                "sample this small is noise dressed as a statistic."
            )
        elif len(daily) >= 2:
            sd = float(daily.std(ddof=1))
            if sd > 0:
                sharpe = round(float(daily.mean()) / sd * np.sqrt(252.0), 3)
            downside = daily[daily < 0]
            if len(downside) >= 2:
                dd_sd = float(downside.std(ddof=1))
                if dd_sd > 0:
                    sortino = round(float(daily.mean()) / dd_sd * np.sqrt(252.0), 3)

        # Per-trade system quality: expectancy divided by its own dispersion.
        sqn = None
        if len(rmul) >= 2 and float(rmul.std(ddof=1)) > 0:
            sqn = round(float(rmul.mean()) / float(rmul.std(ddof=1)) * np.sqrt(len(rmul)), 3)

        peak_r = float(np.max(equity)) if len(equity) else 0.0
        return {
            "trades": len(rows),
            "wins": int(len(wins)),
            "losses": int(len(losses)),
            "win_rate": round(len(wins) / len(rows), 3),
            "net_pnl": round(float(net.sum()), 2),
            "gross_pnl": round(total_gross, 2),
            "total_costs": round(float(costs.sum()), 2),
            # The number that decides whether the strategy survives friction.
            "costs_pct_of_gross": (
                round(float(costs.sum()) / abs(total_gross), 4) if total_gross else None
            ),
            "avg_net_per_trade": round(float(net.mean()), 2),
            "expectancy_r": round(float(rmul.mean()), 3),
            "best": round(float(net.max()), 2),
            "worst": round(float(net.min()), 2),
            "avg_win": round(float(wins.mean()), 2) if len(wins) else 0.0,
            "avg_loss": round(float(losses.mean()), 2) if len(losses) else 0.0,
            "profit_factor": (round(gross_profit / gross_loss, 3) if gross_loss > 0 else None),
            "max_drawdown": round(max_dd, 2),
            "max_drawdown_pct_of_peak": (round(max_dd / peak_r, 4) if peak_r > 0 else None),
            "peak_equity": round(peak_r, 2),
            "final_equity": round(float(equity[-1]), 2) if len(equity) else 0.0,
            "trading_days": len(daily),
            "sharpe": sharpe,
            "sortino": sortino,
            "sharpe_note": sharpe_note,
            "sqn": sqn,
            "best_day": round(float(daily.max()), 2) if len(daily) else 0.0,
            "worst_day": round(float(daily.min()), 2) if len(daily) else 0.0,
            "avg_r_win": (round(float(rmul[rmul > 0].mean()), 3) if (rmul > 0).any() else 0.0),
            "avg_r_loss": (round(float(rmul[rmul <= 0].mean()), 3) if (rmul <= 0).any() else 0.0),
            "avg_hold_minutes": round(
                float(np.mean([float(r["hold_minutes"] or 0) for r in rows])), 1
            ),
            "by_exit_reason": by_reason,
        }

    def to_csv(self, mode: str | None = None) -> str:
        rows = self.entries(mode)
        cols = [
            "trade_date",
            "closed_at",
            "direction",
            "strike",
            "opt_type",
            "expiry",
            "lots",
            "qty",
            "entry_price",
            "exit_price",
            "gross_pnl",
            "entry_cost",
            "exit_cost",
            "total_cost",
            "net_pnl",
            "r_multiple",
            "exit_reason",
            "hold_minutes",
            "mode",
        ]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in cols})
        return buf.getvalue()
