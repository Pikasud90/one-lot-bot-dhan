"""Text summaries and file exports (CSV / JSON) for backtest results.

Charts are drawn in the browser from the JSON artifacts, so nothing here
depends on a plotting library.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from .metrics import breakdown, monthly_returns

KEY_METRICS = [
    ("trades", "Trades", "{:.0f}"),
    ("win_rate", "Win rate %", "{:.1f}"),
    ("profit_factor", "Profit factor", "{:.2f}"),
    ("expectancy_r", "Expectancy (R)", "{:+.3f}"),
    ("expectancy", "Expectancy (Rs)", "{:+,.0f}"),
    ("net_pnl", "Net P&L (Rs)", "{:,.0f}"),
    ("costs", "Costs (Rs)", "{:,.0f}"),
    ("max_drawdown_inr", "Max DD (Rs)", "{:,.0f}"),
    ("max_drawdown_pct", "Max DD %", "{:.2f}"),
    ("sharpe", "Sharpe", "{:.2f}"),
    ("sortino", "Sortino", "{:.2f}"),
    ("payoff", "Payoff", "{:.2f}"),
    ("max_consec_losses", "Max consec losses", "{:.0f}"),
    ("avg_bars_held", "Avg bars held", "{:.1f}"),
]


def fmt_metrics(m: dict) -> dict[str, str]:
    out = {}
    for k, label, f in KEY_METRICS:
        v = m.get(k)
        if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
            out[label] = "-"
        else:
            try:
                out[label] = f.format(v)
            except (ValueError, TypeError):
                out[label] = str(v)
    return out


def export_result(res, folder: str | Path, formats: tuple[str, ...] = ("csv", "json")) -> list[str]:
    folder = Path(folder).expanduser()
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = (
        folder
        / f"backtest_{res.config.strategy}_{res.instrument.key}_{res.config.timeframe}_{stamp}"
    )
    base.parent.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    trades = res.trades.copy()
    for col in ("signal_ts", "entry_ts", "exit_ts"):
        if col in trades:
            ts = pd.to_datetime(trades[col])
            trades[col] = ts.dt.tz_localize(None) if ts.dt.tz is not None else ts
    if "csv" in formats:
        p = Path(str(base) + "_trades.csv")
        trades.drop(columns=["detail"], errors="ignore").to_csv(p, index=False)
        written.append(str(p))
    if "json" in formats:
        p = base.with_suffix(".json")
        payload = {
            "config": asdict(res.config),
            "instrument": res.instrument.as_dict(),
            "metrics": res.metrics,
            "breakdowns": {
                by: breakdown(res.trades, by).reset_index().to_dict("records")
                for by in ("exit_reason", "hour", "weekday")
                if not res.trades.empty
            },
            "monthly": (
                monthly_returns(res.equity, res.config.reference_capital)
                .reset_index()
                .to_dict("records")
                if not res.equity.empty
                else []
            ),
        }
        p.write_text(json.dumps(payload, indent=2, default=str))
        written.append(str(p))
    return written
