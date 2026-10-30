"""What would have happened: replay every archived alert against the bars that followed.

For each signal (taken or not) the audit walks the fine bars after the signal bar and
reports, at the alert's own entry/stop/target levels on the UNDERLYING:

    outcome   TARGET | STOP | OPEN | SQUARE_OFF (intraday horizon ended) | NO_DATA
    mfe_r / mae_r   best / worst excursion in R (R = initial underlying risk)
    r_now     where it stands now (or where it closed)
    pnl_est   rupees for one unit (stocks: 1 share; index: the option lot, premium
              re-priced with the same Black-76 model at the underlying level)

This is a *paper* audit of the signal, not a fill simulation: it assumes the
underlying entry at the signal bar's close, which is what the alert showed.
Honest by construction: a tie between stop and target inside one bar counts as STOP.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from ..clock import IST


def _iso_to_ts(v) -> pd.Timestamp | None:
    if not v:
        return None
    t = pd.Timestamp(v)
    return (t.tz_localize(IST) if t.tzinfo is None else t.tz_convert(IST))


def audit_signal(sig: dict, bars: pd.DataFrame, now: dt.datetime, fine_minutes: int,
                 intraday: bool, exit_time: str = "15:15", premium_fn=None) -> dict:
    """`bars` = the instrument's fine bars (IST index). `premium_fn(underlying, ts)` maps an
    underlying level to an option premium for index signals (None for stocks)."""
    eco = sig.get("economics") or {}
    entry = eco.get("underlying_entry")
    stop = eco.get("underlying_stop")
    target = eco.get("underlying_target")
    bar_ts = _iso_to_ts(sig.get("bar_ts"))
    out = {"outcome": "NO_DATA", "mfe_r": None, "mae_r": None, "r_now": None, "pnl_est": None,
           "exit_ts": None, "exit_underlying": None, "bars": 0, "audited_at": now.isoformat()}
    if entry is None or stop is None or bar_ts is None or bars is None or bars.empty:
        return out
    sign = 1 if sig.get("direction") == "long" else -1
    risk = abs(float(entry) - float(stop))
    if risk <= 0:
        return out
    after = bars[bars.index > bar_ts]
    if intraday:
        hh, mm = (int(x) for x in str(exit_time).split(":"))
        day_end = bar_ts.normalize() + pd.Timedelta(hours=hh, minutes=mm)
        after = after[after.index + pd.Timedelta(minutes=fine_minutes) <= day_end + pd.Timedelta(minutes=fine_minutes)]
    if after.empty:
        out["outcome"] = "OPEN" if not intraday or now < (bar_ts.normalize() + pd.Timedelta(hours=15, minutes=15)).to_pydatetime() else "SQUARE_OFF"
        out["bars"] = 0
        return out
    hi, lo, cl = after["high"].to_numpy(float), after["low"].to_numpy(float), after["close"].to_numpy(float)
    fav = (hi - entry) * sign if sign > 0 else (entry - lo)
    adv = (entry - lo) if sign > 0 else (hi - entry)
    mfe = float(np.max(fav)) / risk if len(fav) else 0.0
    mae = float(np.max(adv)) / risk if len(adv) else 0.0
    outcome, exit_i, exit_px = None, None, None
    for i in range(len(after)):
        hit_stop = (lo[i] <= stop) if sign > 0 else (hi[i] >= stop)
        hit_tgt = target is not None and ((hi[i] >= target) if sign > 0 else (lo[i] <= target))
        if hit_stop:                                   # ties go to the stop
            outcome, exit_i, exit_px = "STOP", i, float(stop)
            break
        if hit_tgt:
            outcome, exit_i, exit_px = "TARGET", i, float(target)
            break
    if outcome is None:
        if intraday:
            session_over = now >= (bar_ts.normalize() + pd.Timedelta(hours=15, minutes=16)).to_pydatetime()
            outcome, exit_i, exit_px = ("SQUARE_OFF", len(after) - 1, float(cl[-1])) if session_over else ("OPEN", len(after) - 1, float(cl[-1]))
        else:
            outcome, exit_i, exit_px = "OPEN", len(after) - 1, float(cl[-1])
    r_now = sign * (exit_px - entry) / risk
    pnl = None
    try:
        if sig.get("kind") == "STOCK":
            pnl = sign * (exit_px - entry) * float(eco.get("qty") or 1)
        elif premium_fn is not None:
            ts_exit = after.index[exit_i]
            prem_exit = premium_fn(exit_px, ts_exit)
            prem_in = float(eco.get("entry_price") or 0)
            if prem_exit is not None and prem_in:
                pnl = (prem_exit - prem_in) * float(eco.get("qty") or 1)
    except Exception:  # noqa: BLE001
        pnl = None
    if pnl is not None:
        pnl -= float(eco.get("costs_inr") or 0)
    out.update({"outcome": outcome, "mfe_r": round(mfe, 2), "mae_r": round(mae, 2), "r_now": round(r_now, 2),
                "pnl_est": round(pnl, 2) if pnl is not None else None,
                "exit_ts": after.index[exit_i].isoformat() if exit_i is not None else None,
                "exit_underlying": round(exit_px, 2) if exit_px is not None else None, "bars": int(exit_i + 1 if exit_i is not None else 0)})
    return out


def strategy_scoreboard(signals: list[dict]) -> dict[str, dict]:
    """Per strategy: how its archived alerts fared (closed outcomes only)."""
    by: dict[str, dict] = {}
    for s in signals:
        a = s.get("outcome") or {}
        if a.get("outcome") in (None, "NO_DATA", "OPEN"):
            continue
        d = by.setdefault(s["strategy"], {"strategy": s["strategy"], "n": 0, "wins": 0, "r_sum": 0.0, "pnl_sum": 0.0,
                                           "targets": 0, "stops": 0, "square_offs": 0})
        d["n"] += 1
        r = float(a.get("r_now") or 0)
        d["r_sum"] += r
        d["pnl_sum"] += float(a.get("pnl_est") or 0)
        if r > 0:
            d["wins"] += 1
        key = {"TARGET": "targets", "STOP": "stops", "SQUARE_OFF": "square_offs"}.get(a.get("outcome"))
        if key:
            d[key] += 1
    for d in by.values():
        d["hit_rate"] = round(d["wins"] / d["n"] * 100, 1) if d["n"] else None
        d["avg_r"] = round(d["r_sum"] / d["n"], 3) if d["n"] else None
        d["pnl_sum"] = round(d["pnl_sum"], 0)
    return by


def confidence(strength: float | None, backtest: dict | None, live: dict | None, regime_ok: bool | None) -> dict:
    """0-100 with its parts. Neutral (50) wherever there is no evidence."""
    parts = {}
    parts["signal_strength"] = float(strength) if strength is not None else 50.0
    if backtest and backtest.get("trades"):
        n = int(backtest["trades"])
        er = float(backtest.get("expectancy_r") or 0)
        pf = float(backtest.get("profit_factor") or 0)
        ev = 50 + 100 * max(-0.5, min(0.5, er))            # +0.3R -> 80, -0.3R -> 20
        if pf < 1:
            ev -= 15
        w = min(1.0, n / 40)                                # little evidence -> pulled toward 50
        parts["backtest_evidence"] = round(50 + (ev - 50) * w, 1)
        parts["backtest_note"] = f"{n} trades, {er:+.3f}R, PF {pf:.2f}"
    else:
        parts["backtest_evidence"] = 50.0
        parts["backtest_note"] = "no saved backtest for this strategy/instrument/timeframe"
    if live and live.get("n", 0) >= 5:
        hr = float(live.get("hit_rate") or 0)
        avg_r = float(live.get("avg_r") or 0)
        parts["live_record"] = round(max(0, min(100, hr * 0.6 + 50 + avg_r * 50)), 1)
        parts["live_note"] = f"{live['n']} archived alerts, {hr:.0f}% positive, avg {avg_r:+.2f}R"
    else:
        parts["live_record"] = 50.0
        parts["live_note"] = "fewer than 5 archived alerts for this strategy"
    parts["regime_fit"] = 70.0 if regime_ok else (30.0 if regime_ok is False else 50.0)
    score = (0.35 * parts["signal_strength"] + 0.30 * parts["backtest_evidence"] + 0.20 * parts["live_record"]
             + 0.15 * parts["regime_fit"])
    label = "high" if score >= 65 else ("low" if score < 45 else "medium")
    return {"score": round(score, 1), "label": label, "parts": parts}
