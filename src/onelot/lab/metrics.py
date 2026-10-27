"""Performance metrics. Sharpe/Sortino come from DAILY equity returns (annualised with sqrt(252)),
not from per-trade returns (a common mistake in the reference repos)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _max_consecutive(mask: np.ndarray) -> int:
    best = cur = 0
    for m in mask:
        cur = cur + 1 if m else 0
        best = max(best, cur)
    return best


def trade_stats(df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        return {"trades": 0}
    pnl = df["net_pnl"].to_numpy(float)
    r = df["r_multiple"].to_numpy(float)
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_win, gross_loss = wins.sum(), -losses.sum()
    out = {
        "trades": int(len(df)),
        "win_rate": float(len(wins) / len(df) * 100),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "payoff": float(wins.mean() / -losses.mean()) if len(wins) and len(losses) and losses.mean() < 0 else float("nan"),
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0,
        "net_pnl": float(pnl.sum()),
        "gross_pnl": float(df["gross_pnl"].sum()),
        "costs": float(df["costs"].sum()),
        "expectancy": float(pnl.mean()),
        "expectancy_r": float(r.mean()),
        "median_r": float(np.median(r)),
        "sqn": float(r.mean() / r.std(ddof=1) * math.sqrt(min(len(r), 100))) if len(r) > 1 and r.std(ddof=1) > 0 else 0.0,
        "max_consec_losses": _max_consecutive(pnl <= 0),
        "avg_bars_held": float(df["bars_held"].mean()),
        "avg_mfe_r": float(df["mfe_r"].mean()),
        "avg_mae_r": float(df["mae_r"].mean()),
        "long_trades": int((df["side"] == "LONG").sum()),
        "short_trades": int((df["side"] == "SHORT").sum()),
        "long_win_rate": float((df.loc[df["side"] == "LONG", "net_pnl"] > 0).mean() * 100) if (df["side"] == "LONG").any() else float("nan"),
        "short_win_rate": float((df.loc[df["side"] == "SHORT", "net_pnl"] > 0).mean() * 100) if (df["side"] == "SHORT").any() else float("nan"),
        "cost_share_pct": float(df["costs"].sum() / max(1e-9, abs(df["gross_pnl"]).sum()) * 100),
        "gross_profit": float(gross_win),
        "gross_loss": float(gross_loss),
        "largest_win": float(wins.max()) if len(wins) else 0.0,
        "largest_loss": float(losses.min()) if len(losses) else 0.0,
        "max_consec_wins": _max_consecutive(pnl > 0),
        "avg_risk_inr": float(df["risk_inr"].mean()) if "risk_inr" in df.columns else float("nan"),
    }
    # Trade-sequence drawdown in rupees (one lot: rupees are the unit that matters).
    eq = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.r_[0.0, eq])[1:]
    out["max_drawdown_inr_trades"] = float((peak - eq).max()) if len(eq) else 0.0
    return out


def equity_stats(daily_equity: pd.Series, capital: float, rf_annual_pct: float = 0.0) -> dict:
    if daily_equity is None or daily_equity.empty:
        return {}
    eq = daily_equity.astype(float)
    rets = eq.pct_change().fillna(eq.iloc[0] / capital - 1)
    days = len(eq)
    total = eq.iloc[-1] / capital - 1
    years = max(days / 252, 1 / 252)
    cagr = (eq.iloc[-1] / capital) ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1.0
    rf_d = rf_annual_pct / 100 / 252
    ex = rets - rf_d
    sd = ex.std(ddof=1)
    sharpe = float(ex.mean() / sd * math.sqrt(252)) if sd > 0 else 0.0
    dn = ex[ex < 0]
    dsd = math.sqrt((dn**2).sum() / max(1, len(ex))) if len(dn) else 0.0
    sortino = float(ex.mean() / dsd * math.sqrt(252)) if dsd > 0 else 0.0
    peak = np.maximum.accumulate(np.r_[capital, eq.to_numpy()])[1:]
    dd = eq.to_numpy() / peak - 1
    max_dd = float(dd.min())
    max_dd_inr = float((peak - eq.to_numpy()).max())
    # longest drawdown duration in trading days
    dur = cur = 0
    for v in dd:
        cur = cur + 1 if v < 0 else 0
        dur = max(dur, cur)
    return {
        "days": days,
        "total_return_pct": float(total * 100),
        "cagr_pct": float(cagr * 100),
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown_pct": float(max_dd * 100),
        "max_drawdown_inr": max_dd_inr,
        "max_dd_days": int(dur),
        "sharpe_note": ("Sharpe on fewer than 20 trading days is noise" if days < 20 else None),
        "calmar": float(cagr / -max_dd) if max_dd < 0 else float("inf") if cagr > 0 else 0.0,
        "positive_days_pct": float((rets > 0).sum() / max(1, (rets != 0).sum()) * 100),
        "final_equity": float(eq.iloc[-1]),
    }


def all_metrics(trades: pd.DataFrame, daily_equity: pd.Series, capital: float) -> dict:
    m = trade_stats(trades)
    m.update(equity_stats(daily_equity, capital))
    if m.get("days"):
        m["trades_per_day"] = m["trades"] / m["days"]
        m["net_per_day"] = m.get("net_pnl", 0.0) / m["days"]
    return m


def breakdown(trades: pd.DataFrame, by: str) -> pd.DataFrame:
    if trades is None or trades.empty:
        return pd.DataFrame()
    df = trades.copy()
    if by == "hour":
        df["hour"] = pd.to_datetime(df["entry_ts"]).dt.strftime("%H:00")
    elif by == "weekday":
        df["weekday"] = pd.to_datetime(df["entry_ts"]).dt.day_name()
    elif by == "strength_bucket":
        df["strength_bucket"] = pd.cut(df["strength"], [-1, 50, 65, 80, 101], labels=["<50", "50-65", "65-80", "80+"])
    g = df.groupby(by, observed=True)
    out = pd.DataFrame({
        "trades": g.size(),
        "win_rate": g["net_pnl"].apply(lambda s: (s > 0).mean() * 100),
        "net_pnl": g["net_pnl"].sum(),
        "avg_r": g["r_multiple"].mean(),
        "profit_factor": g["net_pnl"].apply(lambda s: s[s > 0].sum() / -s[s <= 0].sum() if (s <= 0).any() and s[s <= 0].sum() < 0 else np.inf),
    })
    return out.round(3)


def monthly_returns(daily_equity: pd.Series, capital: float) -> pd.DataFrame:
    if daily_equity is None or daily_equity.empty:
        return pd.DataFrame()
    eq = daily_equity.copy()
    eq.index = pd.DatetimeIndex(eq.index).tz_localize(None) if eq.index.tz is not None else eq.index
    m = eq.resample("ME").last()
    prev = m.shift(1).fillna(capital)
    r = (m / prev - 1) * 100
    tbl = pd.DataFrame({"year": r.index.year, "month": r.index.strftime("%b"), "ret": r.to_numpy()})
    return tbl.pivot(index="year", columns="month", values="ret")
