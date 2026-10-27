"""Strategy research gate: PASS / WATCH / FAIL per (strategy, instrument, timeframe).

For every combination:
  1. default-parameter baseline (one unit per trade, full costs + slippage)
  2. rolling walk-forward: optimise in-sample, score ONLY the next unseen window
  3. final robust parameters on the full window (the live preset candidate)
  4. stress on the final parameters: costs x2, slippage x2, parameter jitter,
     Monte Carlo drawdown in rupees
  5. verdict from the OUT-OF-SAMPLE numbers only

Results go to docs/research/RESEARCH_REPORT.md and data/research/<stamp>/,
and PASS/WATCH parameter sets are stored in `param_sets` for the Lab and the
live engine's `strategy.live.lab_params`.
"""

from __future__ import annotations

import datetime as dt
import json
import random
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from ..clock import IST
from ..data.db import get_engine, utcnow
from ..data.schema import param_sets
from ..logging_setup import get_logger
from ..settings import PROJECT_ROOT, current_settings
from .metrics import breakdown
from .optimizer import Evaluator, jitter, monte_carlo_drawdown, optimize, trading_days, walk_forward
from .runner import BacktestConfig, DataCache

log = get_logger("lab.research")

# Deliberately small search spaces (5-8 knobs) to limit overfitting.
EXITS = {"target_r": {"choices": [0.0, 1.5, 2.5, 3.0]}, "trail_atr_mult": {"choices": [0.0, 1.5, 2.5]}}
SPACES: dict[str, dict[str, dict]] = {
    "ema_cross": {"fast": {"choices": [5, 9, 13]}, "slow": {"choices": [21, 34, 50]},
                  "adx_min": {"choices": [15, 20, 25]}, "confirm_atr": {"choices": [0.0, 0.15, 0.3]},
                  "rsi_margin": {"choices": [0, 3, 6]}, **EXITS},
    "supertrend_vol": {"st_period": {"choices": [7, 10, 14]}, "st_mult": {"choices": [1.5, 2.0, 2.5, 3.0]},
                       "adx_min": {"choices": [12, 18, 24]}, "flip_window": {"choices": [1, 2, 4]},
                       "rsi_long_min": {"choices": [50, 52, 55]}, **EXITS},
    "supertrend_adx": {"st_period": {"choices": [7, 10, 14]}, "st_mult": {"choices": [2.0, 2.5, 3.0]},
                       "adx_min": {"choices": [15, 20, 25]}, "flip_window": {"choices": [2, 4, 6]},
                       "max_vwap_dist_atr": {"choices": [1.0, 2.0, 3.0]}, **EXITS},
    "orb": {"or_minutes": {"choices": [15, 30]}, "direction_mode": {"choices": ["breakout", "first_candle"]},
            "stop_mode": {"choices": ["mid", "datr"]}, "stop_datr": {"choices": [0.1, 0.2]},
            "target_r": {"choices": [0.0, 1.5, 2.5]}, "trail_atr_mult": {"choices": [0.0, 2.0]}},
    "vwap_pullback": {"zone_atr": {"choices": [0.2, 0.4, 0.6]}, "adx_min": {"choices": [10, 15, 20, 25]},
                      "rvol_min": {"choices": [0.8, 1.0, 1.25]}, **EXITS},
    "squeeze": {"min_squeeze_bars": {"choices": [4, 6, 10]}, "kc_mult": {"choices": [1.25, 1.5, 2.0]},
                "dc_len": {"choices": [10, 20]}, **EXITS},
    "donchian_breakout": {"entry_len": {"choices": [10, 20, 40]}, "exit_len": {"choices": [5, 10, 20]},
                          "adx_min": {"choices": [10, 15, 20]}, "trail_atr_mult": {"choices": [0.0, 2.0, 3.0]}},
    "mean_reversion": {"entry_level": {"choices": [5, 10, 15]}, "exit_level": {"choices": [50, 60, 70]},
                       "trend_slow": {"choices": [30, 50, 80]}, "stop_atr": {"choices": [1.0, 1.5, 2.0]},
                       "max_bars": {"choices": [6, 12, 24]}},
    "intraday_momentum": {"open_window_min": {"choices": [15, 30, 45]}, "min_open_move_atr": {"choices": [0.0, 0.5, 1.0]},
                          "stop_atr": {"choices": [0.75, 1.0, 1.5]}, "require_prior_agree": {"choices": [False, True]}},
    "gap_fade": {"min_rel_gap": {"choices": [0.5, 0.75, 1.0]}, "min_gap": {"choices": [0.0, 0.3, 0.5]},
                 "confirm": {"choices": ["none", "red_bar", "below_vwap"]}, "stop_datr": {"choices": [0.5, 0.75, 1.0]},
                 "target_r": {"choices": [0.0, 1.5, 2.5]}},
    "benchmark_random": {"entry_prob": {"choices": [0.01, 0.02]}, **EXITS},
}

WF = {"fast": {"is_days": 20, "oos_days": 6, "min_trades": 12},
      "slow": {"is_days": 120, "oos_days": 40, "min_trades": 15}}
GATES = {"min_oos_trades": 30, "min_oos_pf": 1.2, "min_oos_exp_r": 0.05}


def tf_class(tf: str) -> str:
    return "slow" if tf in ("30m", "60m", "1h", "1d") else "fast"


def verdict(oos: dict, cost2: dict, jit_med: float) -> tuple[str, list[str]]:
    notes = []
    n = oos.get("trades", 0) or 0
    pf = oos.get("profit_factor", 0) or 0
    er = oos.get("expectancy_r", -1) if oos.get("expectancy_r") is not None else -1
    ok = True
    if n < GATES["min_oos_trades"]:
        ok = False
        notes.append(f"only {n} out-of-sample trades (< {GATES['min_oos_trades']})")
    if pf < GATES["min_oos_pf"]:
        ok = False
        notes.append(f"OOS profit factor {pf:.2f} < {GATES['min_oos_pf']}")
    if er < GATES["min_oos_exp_r"]:
        ok = False
        notes.append(f"OOS expectancy {er:+.3f}R < {GATES['min_oos_exp_r']}R")
    if (cost2.get("profit_factor") or 0) < 1.0:
        ok = False
        notes.append("loses money when costs are doubled")
    if jit_med <= 0:
        ok = False
        notes.append("parameter neighbourhood is not profitable (fragile optimum)")
    if ok:
        return "PASS", notes
    if n >= 15 and er > 0 and pf > 1.0:
        return "WATCH", notes
    return "FAIL", notes


def run_one(strategy: str, tf: str, instrument_key: str, ev: Evaluator, cache: DataCache,
            trials: int, progress=None) -> dict:
    cls = tf_class(tf)
    space = SPACES[strategy]
    s = current_settings()
    base = BacktestConfig(strategy=strategy, instrument_key=instrument_key, params={}, timeframe=tf,
                          slippage_pct=s.backtest.slippage_pct, reference_capital=s.backtest.reference_capital_inr,
                          warmup_sessions=s.backtest.warmup_sessions)
    spec = cache.spec(instrument_key)
    days = trading_days(cache, spec.series_key, tf)
    log.info("research %s %s %s: %d sessions", strategy, instrument_key, tf, len(days))
    (_, m_def, _), = ev.map([base])
    wf = walk_forward(base, space, ev, days, WF[cls]["is_days"], WF[cls]["oos_days"], n_trials=trials,
                      min_trades=WF[cls]["min_trades"], progress=progress)
    opt = optimize(base, space, ev, n_trials=trials, min_trades=WF[cls]["min_trades"] * 2)
    final = opt.best_params
    final_cfg = replace(base, params=final)
    rng = random.Random(5)
    jit_cfgs = [replace(base, params={**final, **jitter(final, space, rng)}) for _ in range(10)]
    stress = ev.map([final_cfg, replace(final_cfg, cost_multiplier=2.0),
                     replace(final_cfg, slippage_pct=base.slippage_pct * 2)] + jit_cfgs)
    m_is, m_cost2, m_slip2 = (x[1] for x in stress[:3])
    jit = [x[1] for x in stress[3:]]
    jit_exp = [j.get("expectancy_r", -1) if j.get("trades", 0) >= 5 else -1 for j in jit]
    jit_med = float(np.median(jit_exp)) if jit_exp else -1
    pnl = wf.oos_trades["net_pnl"].to_numpy(float) if not wf.oos_trades.empty else np.array([])
    mc = monte_carlo_drawdown(pnl)
    v, notes = verdict(wf.oos_metrics, m_cost2, jit_med)
    strength = breakdown(wf.oos_trades, "strength_bucket") if not wf.oos_trades.empty else pd.DataFrame()
    return {
        "strategy": strategy, "instrument": instrument_key, "category": spec.category, "timeframe": tf,
        "sessions": len(days), "period": f"{days[0].date()}..{days[-1].date()}" if days else "-",
        "default": _slim(m_def), "oos": _slim(wf.oos_metrics), "default_oos": _slim(wf.default_oos_metrics),
        "folds": wf.folds, "final_params": {k: final.get(k) for k in space}, "final_params_full": final,
        "in_sample_final": _slim(m_is), "cost_x2": _slim(m_cost2), "slippage_x2": _slim(m_slip2),
        "jitter_median_exp_r": jit_med, "monte_carlo": mc, "verdict": v, "notes": notes,
        "strength_calibration": strength.reset_index().to_dict("records") if not strength.empty else [],
        "oos_trades": wf.oos_trades,
    }


def _slim(m: dict) -> dict:
    keys = ["trades", "win_rate", "profit_factor", "expectancy_r", "expectancy", "net_pnl", "sharpe",
            "max_drawdown_inr", "max_drawdown_pct", "payoff", "costs", "long_trades", "short_trades",
            "trades_per_day", "total_return_pct", "avg_bars_held", "max_consec_losses"]
    out = {}
    for k in keys:
        v = m.get(k)
        out[k] = None if (isinstance(v, float) and (np.isnan(v) or np.isinf(v))) else v
    return out


C_ICON = {"PASS": "PASS", "WATCH": "WATCH", "FAIL": "FAIL"}


def save_param_sets(results: list[dict], engine=None) -> None:
    eng = engine or get_engine()
    stamp = dt.datetime.now(IST).strftime("%Y-%m-%d")
    with eng.begin() as conn:
        for r in results:
            if r["verdict"] not in ("PASS", "WATCH"):
                continue
            label = f"research {r['instrument']} {r['timeframe']} {stamp} ({r['verdict']})"
            conn.execute(param_sets.delete().where((param_sets.c.strategy == r["strategy"])
                                                   & (param_sets.c.label == label)))
            conn.execute(param_sets.insert().values(
                strategy=r["strategy"], label=label, timeframe=r["timeframe"], instrument_key=r["instrument"],
                params=r["final_params_full"], is_live=r["verdict"] == "PASS",
                metrics={"oos": r["oos"], "verdict": r["verdict"], "monte_carlo": r["monte_carlo"],
                         "notes": r["notes"]}, created_at=utcnow()))


def write_report(results: list[dict], out_dir: Path, data_note: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    docs = PROJECT_ROOT / "docs" / "research"
    docs.mkdir(parents=True, exist_ok=True)

    def f(v, fmt="{:.2f}"):
        return "-" if v is None else fmt.format(v)

    lines = ["# Strategy Research Report", "",
             f"Generated {dt.datetime.now(IST):%Y-%m-%d %H:%M} IST by `python -m onelot research`.", "",
             "## Data and method", "", data_note, "",
             "* **One unit per trade**: one share (stocks) or one lot (futures / options / currency); "
             "full Indian charges and slippage included; option premiums are Black-76 synthetic "
             "(India VIX-scaled IV) until Dhan's expired-options history is wired.",
             "* **Walk-forward**: parameters are optimised in-sample and scored only on the following, unseen "
             "out-of-sample (OOS) window; windows roll forward. **Only OOS numbers decide the verdict.**",
             "* **Robust selection**: the best of the top-5 trials after averaging with jittered neighbours.",
             f"* **Gates (PASS)**: OOS trades >= {GATES['min_oos_trades']}, OOS profit factor >= {GATES['min_oos_pf']}, "
             f"OOS expectancy >= {GATES['min_oos_exp_r']}R, still profitable with 2x costs, profitable parameter "
             "neighbourhood. WATCH = positive OOS but not yet robust; FAIL = no out-of-sample edge.", "",
             "## Verdict summary", "",
             "| Strategy | Instrument | TF | Verdict | OOS trades | OOS win % | OOS PF | OOS exp (R) | Net OOS (Rs) "
             "| 2x cost PF | Jitter exp (R) | MC DD p95 (Rs) |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        o, c2, mc = r["oos"], r["cost_x2"], r["monte_carlo"]
        lines.append(f"| {r['strategy']} | {r['instrument']} | {r['timeframe']} | **{r['verdict']}** | "
                     f"{o.get('trades', 0)} | {f(o.get('win_rate'), '{:.1f}')} | {f(o.get('profit_factor'))} | "
                     f"{f(o.get('expectancy_r'), '{:+.3f}')} | {f(o.get('net_pnl'), '{:,.0f}')} | "
                     f"{f(c2.get('profit_factor'))} | {f(r['jitter_median_exp_r'], '{:+.3f}')} | "
                     f"{f(mc.get('mc_dd_p95_inr'), '{:,.0f}')} |")
    lines += ["", "## Details", ""]
    for r in results:
        lines += [f"### {r['strategy']} on {r['instrument']} ({r['timeframe']}) - {r['verdict']}", "",
                  f"Sessions: {r['sessions']} ({r['period']}).", ""]
        if r["notes"]:
            lines += ["Why not PASS: " + "; ".join(r["notes"]) + ".", ""]
        lines += ["| Window | Trades | Win % | PF | Exp (R) | Long/Short | Net P&L (Rs) |", "|---|---|---|---|---|---|---|"]
        for name, m in (("Default params (full period)", r["default"]), ("Walk-forward OOS", r["oos"]),
                        ("Default params on the same OOS windows", r["default_oos"]),
                        ("Final params, full period (in-sample, optimistic)", r["in_sample_final"]),
                        ("Final params, costs x2", r["cost_x2"]), ("Final params, slippage x2", r["slippage_x2"])):
            lines.append(f"| {name} | {m.get('trades', 0)} | {f(m.get('win_rate'), '{:.1f}')} | "
                         f"{f(m.get('profit_factor'))} | {f(m.get('expectancy_r'), '{:+.3f}')} | "
                         f"{m.get('long_trades', 0)}/{m.get('short_trades', 0)} | {f(m.get('net_pnl'), '{:,.0f}')} |")
        lines += ["", "Walk-forward folds:", "",
                  "| Fold | In-sample | OOS | IS PF | OOS trades | OOS PF | OOS exp (R) | Default OOS PF |",
                  "|---|---|---|---|---|---|---|---|"]
        for fo in r["folds"]:
            lines.append(f"| {fo['fold']} | {fo['is']} | {fo['oos']} | {f(fo['is_pf'])} | {fo['oos_trades']} | "
                         f"{f(fo['oos_pf'])} | {f(fo['oos_exp_r'], '{:+.3f}')} | {f(fo['default_oos_pf'])} |")
        mc = r["monte_carlo"]
        if mc:
            lines += ["", f"Monte Carlo (OOS trades resampled, one unit): median max DD Rs {mc['mc_dd_median_inr']:,.0f}, "
                          f"95th pct Rs {mc['mc_dd_p95_inr']:,.0f}, probability of a losing run {mc['mc_prob_loss']:.0f}%."]
        lines += ["", "Final parameters: `" + json.dumps(r["final_params"], default=str) + "`", ""]
        if r["strength_calibration"]:
            lines += ["Strength-score calibration (OOS):", "", "| Strength | Trades | Win % | Avg R | PF |", "|---|---|---|---|---|"]
            for row in r["strength_calibration"]:
                lines.append(f"| {row['strength_bucket']} | {row['trades']} | {row['win_rate']:.1f} | "
                             f"{row['avg_r']:+.3f} | {f(row['profit_factor'])} |")
            lines.append("")
    path = docs / "RESEARCH_REPORT.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    slim = [{k: v for k, v in r.items() if k != "oos_trades"} for r in results]
    (out_dir / "research.json").write_text(json.dumps(slim, indent=2, default=str), encoding="utf-8")
    allt = [r["oos_trades"].assign(timeframe=r["timeframe"]) for r in results if not r["oos_trades"].empty]
    if allt:
        pd.concat(allt).drop(columns=["detail"], errors="ignore").to_csv(out_dir / "oos_trades.csv", index=False)
    return path


def run_research(strategies: list[str] | None, timeframes: list[str], instruments: list[str],
                 trials: int = 30, out_dir: Path | None = None, engine=None,
                 progress=None, workers: int | None = None) -> tuple[list[dict], Path]:
    from ..strategies.registry import REGISTRY

    strategies = strategies or [*REGISTRY, "benchmark_random"]
    cache = DataCache(engine)
    s = current_settings()
    out_dir = out_dir or (s.data_dir / "research" / dt.datetime.now(IST).strftime("%Y%m%d_%H%M"))
    results: list[dict] = []
    combos = [(st, tf, ik) for ik in instruments for tf in timeframes for st in strategies if st in SPACES]
    with Evaluator(workers=workers, cache=cache if (workers or 1) == 1 else None) as ev:
        for n, (st, tf, ik) in enumerate(combos, 1):
            if progress:
                progress((n - 1) / max(1, len(combos)), f"{st} {ik} {tf}")
            try:
                res = run_one(st, tf, ik, ev, cache, trials)
                results.append(res)
                o = res["oos"]
                log.info("RESULT %-18s %-14s %-4s %-5s OOS trades=%s PF=%s expR=%s", st, ik, tf,
                         res["verdict"], o.get("trades"), o.get("profit_factor"), o.get("expectancy_r"))
            except Exception as exc:  # noqa: BLE001
                log.exception("research failed for %s %s %s: %s", st, ik, tf, exc)
    from ..data.store import coverage_report

    cov = coverage_report(cache.engine)
    src = ", ".join(sorted(cov["source"].dropna().unique())) if not cov.empty else "-"
    note = (f"Instruments: {', '.join(instruments)}. Data source: **{src}**. Yahoo intraday history is capped "
            "(2m/5m/15m about the last 60 days, 60m about 2 years), so intraday results cover roughly 40 "
            "sessions and are a preliminary screen. Re-run on Dhan 1-minute history (5 years) before "
            "trusting any verdict.")
    path = write_report(results, out_dir, note)
    save_param_sets(results, cache.engine)
    if progress:
        progress(1.0, "done")
    return results, path
