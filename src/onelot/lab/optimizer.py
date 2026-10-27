"""Parameter optimisation, robust ("plateau") selection and rolling walk-forward analysis.

Guards against overfitting:
* objective = SQN-style score  mean(R)/std(R) * sqrt(min(N,100))  with a minimum-trade constraint,
  so a few lucky trades cannot win;
* the chosen parameters are the best of the top-k trials *after* averaging each one with jittered
  neighbours (a stable plateau beats an isolated spike - ref 3's "pick rank #100" instinct, done properly);
* honest performance = concatenated out-of-sample folds of a rolling walk-forward.
"""

from __future__ import annotations

import math
import os
import random
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field, replace

import numpy as np
import pandas as pd

from .metrics import all_metrics
from .runner import BacktestConfig, DataCache, run_backtest

Space = dict[str, dict]  # name -> {"lo","hi","step"} or {"choices": [...]}


# ----------------------------------------------------------------------------- sampling
def grid_values(spec: dict) -> list:
    if "choices" in spec:
        return list(spec["choices"])
    lo, hi, step = spec["lo"], spec["hi"], spec["step"]
    n = int(round((hi - lo) / step)) + 1
    vals = [lo + k * step for k in range(n)]
    if all(float(v).is_integer() for v in (lo, hi, step)):
        return [int(round(v)) for v in vals]
    return [round(v, 6) for v in vals]


def sample(space: Space, rng: random.Random) -> dict:
    return {k: rng.choice(grid_values(v)) for k, v in space.items()}


def jitter(params: dict, space: Space, rng: random.Random, steps: int = 1) -> dict:
    out = dict(params)
    for k, spec in space.items():
        vals = grid_values(spec)
        if k not in out or out[k] not in vals:
            continue
        i = vals.index(out[k])
        j = min(len(vals) - 1, max(0, i + rng.randint(-steps, steps)))
        out[k] = vals[j]
    return out


# ----------------------------------------------------------------------------- objective
def score(metrics: dict, min_trades: int = 20) -> float:
    n = metrics.get("trades", 0) or 0
    if n < min_trades:
        return -10.0 + n / max(1, min_trades)  # infeasible, but rank by how close
    exp_r = metrics.get("expectancy_r", 0.0)
    sd = metrics.get("_r_std") or 1.0
    return float(exp_r / sd * math.sqrt(min(n, 100)))


def _metrics_with_std(res) -> dict:
    m = dict(res.metrics)
    m["_r_std"] = float(res.trades["r_multiple"].std(ddof=1)) if len(res.trades) > 1 else 1.0
    return m


# ----------------------------------------------------------------------------- parallel evaluation
_CACHE: DataCache | None = None


def _init_worker() -> None:
    global _CACHE
    _CACHE = DataCache()


def _eval(cfg_dict: dict) -> tuple[dict, dict, pd.DataFrame | None]:
    global _CACHE
    if _CACHE is None:
        _CACHE = DataCache()
    keep_trades = cfg_dict.pop("_keep_trades", False)
    cfg = BacktestConfig(**cfg_dict)
    res = run_backtest(cfg, _CACHE)
    m = _metrics_with_std(res)
    return cfg.params, m, (res.trades if keep_trades else None)


class Evaluator:
    """Runs many backtests. Inside the engine process pass `cache` and keep
    `workers=1` (the Lab threads already share one SQLite engine); the CLI may
    fan out across processes."""

    def __init__(self, workers: int | None = None, cache: DataCache | None = None) -> None:
        global _CACHE
        if cache is not None:
            _CACHE = cache
            workers = 1
        self.workers = workers if workers is not None else max(1, min(4, (os.cpu_count() or 2) - 2))
        self._pool = ProcessPoolExecutor(self.workers, initializer=_init_worker) if self.workers > 1 else None

    def map(self, cfgs: list[BacktestConfig], keep_trades: bool = False):
        payload = [{**asdict(c), "_keep_trades": keep_trades} for c in cfgs]
        if self._pool is None:
            return [_eval(p) for p in payload]
        return list(self._pool.map(_eval, payload, chunksize=1))

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(cancel_futures=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ----------------------------------------------------------------------------- optimisation
@dataclass
class OptResult:
    best_params: dict
    best_score: float
    best_metrics: dict
    trials: pd.DataFrame
    robust_scores: dict = field(default_factory=dict)


def optimize(base: BacktestConfig, space: Space, evaluator: Evaluator, n_trials: int = 40, seed: int = 7,
             min_trades: int = 20, top_k: int = 5, n_jitter: int = 4,
             progress: Callable[[float, str], None] | None = None) -> OptResult:
    rng = random.Random(seed)
    candidates = [dict(base.params)]  # always include the current/default parameters
    seen = {tuple(sorted(candidates[0].items()))}
    while len(candidates) < n_trials:
        p = {**base.params, **sample(space, rng)}
        key = tuple(sorted(p.items()))
        if key not in seen:
            seen.add(key)
            candidates.append(p)
        if len(seen) > 50 * n_trials:
            break
    cfgs = [replace(base, params=p) for p in candidates]
    results = evaluator.map(cfgs)
    rows = []
    for params, m, _ in results:
        rows.append({**{f"p_{k}": v for k, v in params.items() if k in space}, "score": score(m, min_trades),
                     **{k: m.get(k) for k in ("trades", "win_rate", "profit_factor", "expectancy_r", "net_pnl", "sharpe")},
                     "_params": params})
    trials = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
    if progress:
        progress(0.7, "robust selection")
    # robust selection: average each top-k trial with jittered neighbours
    top = trials.head(top_k)
    neigh_cfgs, owners = [], []
    for i, row in top.iterrows():
        for _ in range(n_jitter):
            neigh_cfgs.append(replace(base, params={**base.params, **jitter(row["_params"], space, rng)}))
            owners.append(i)
    neigh = evaluator.map(neigh_cfgs) if neigh_cfgs else []
    robust: dict[int, list[float]] = {i: [row["score"]] for i, row in top.iterrows()}
    for owner, (_, m, _) in zip(owners, neigh, strict=True):
        robust[owner].append(score(m, min_trades))
    robust_med = {i: float(np.median(v)) for i, v in robust.items()}
    best_i = max(robust_med, key=robust_med.get) if robust_med else 0
    best = trials.loc[best_i]
    if progress:
        progress(1.0, "done")
    return OptResult(best["_params"], float(robust_med.get(best_i, best["score"])),
                     {k: best[k] for k in ("trades", "win_rate", "profit_factor", "expectancy_r", "net_pnl", "sharpe")},
                     trials.drop(columns=["_params"]), {int(k): v for k, v in robust_med.items()})


# ----------------------------------------------------------------------------- walk-forward
@dataclass
class WFResult:
    folds: list[dict]
    oos_trades: pd.DataFrame
    oos_metrics: dict
    default_oos_metrics: dict
    chosen_params: list[dict]


def trading_days(cache: DataCache, series_key: str, timeframe: str) -> list[pd.Timestamp]:
    b = cache.bars(series_key, timeframe)
    return sorted(set(b.index.normalize().unique())) if not b.empty else []


def walk_forward(base: BacktestConfig, space: Space, evaluator: Evaluator, days: list[pd.Timestamp],
                 is_days: int, oos_days: int, warmup: int = 3, n_trials: int = 40, min_trades: int = 15,
                 seed: int = 11, progress: Callable[[float, str], None] | None = None) -> WFResult:
    days = days[warmup:]
    folds_def = []
    start = 0
    while start + is_days + 1 <= len(days):
        is_d = days[start : start + is_days]
        oos_d = days[start + is_days : start + is_days + oos_days]
        if not oos_d:
            break
        folds_def.append((is_d, oos_d))
        start += oos_days
    folds, oos_parts, default_parts, chosen = [], [], [], []
    for k, (is_d, oos_d) in enumerate(folds_def):
        if progress:
            progress(k / max(1, len(folds_def)), f"fold {k + 1}/{len(folds_def)}")
        is_cfg = replace(base, start=str(is_d[0].date()), end=str(is_d[-1].date()))
        opt = optimize(is_cfg, space, evaluator, n_trials=n_trials, seed=seed + k, min_trades=min_trades)
        oos_cfg = replace(base, start=str(oos_d[0].date()), end=str(oos_d[-1].date()))
        (_, m_oos, t_oos), (_, m_def, t_def) = evaluator.map(
            [replace(oos_cfg, params=opt.best_params), oos_cfg], keep_trades=True)
        oos_parts.append(t_oos)
        default_parts.append(t_def)
        chosen.append(opt.best_params)
        folds.append({"fold": k + 1, "is": f"{is_d[0].date()}..{is_d[-1].date()}",
                      "oos": f"{oos_d[0].date()}..{oos_d[-1].date()}", "is_score": opt.best_score,
                      "is_trades": opt.best_metrics.get("trades"), "is_pf": opt.best_metrics.get("profit_factor"),
                      "oos_trades": m_oos.get("trades", 0), "oos_pf": m_oos.get("profit_factor"),
                      "oos_exp_r": m_oos.get("expectancy_r"), "oos_win_rate": m_oos.get("win_rate"),
                      "default_oos_pf": m_def.get("profit_factor"), "default_oos_exp_r": m_def.get("expectancy_r")})
    oos_nz = [t for t in oos_parts if t is not None and not t.empty]
    def_nz = [t for t in default_parts if t is not None and not t.empty]
    oos = pd.concat(oos_nz, ignore_index=True) if oos_nz else pd.DataFrame()
    dft = pd.concat(def_nz, ignore_index=True) if def_nz else pd.DataFrame()
    oos_m = _concat_metrics(oos, base.reference_capital)
    def_m = _concat_metrics(dft, base.reference_capital)
    if progress:
        progress(1.0, "done")
    return WFResult(folds, oos, oos_m, def_m, chosen)


def _concat_metrics(trades: pd.DataFrame, capital: float) -> dict:
    if trades is None or trades.empty:
        return {"trades": 0}
    t = trades.sort_values("exit_ts")
    eq = capital + t["net_pnl"].cumsum()
    eq.index = pd.DatetimeIndex(t["exit_ts"])
    daily = eq.groupby(eq.index.normalize()).last()
    m = all_metrics(t, daily, capital)
    m["_r_std"] = float(t["r_multiple"].std(ddof=1)) if len(t) > 1 else 1.0
    return m


# ----------------------------------------------------------------------------- robustness
def monte_carlo_drawdown(pnl: np.ndarray, n: int = 2000, seed: int = 3) -> dict:
    """Bootstrap the rupee trade sequence (one lot, no compounding): the
    distribution of max drawdown and of the final P&L in RUPEES."""
    pnl = np.asarray(pnl, dtype=float)
    if len(pnl) < 5:
        return {}
    rng = np.random.default_rng(seed)
    dds, finals = [], []
    for _ in range(n):
        seq = rng.choice(pnl, size=len(pnl), replace=True)
        eq = np.cumsum(seq)
        peak = np.maximum.accumulate(np.r_[0.0, eq])[1:]
        dds.append(float((peak - eq).max()))
        finals.append(float(eq[-1]))
    return {"mc_dd_median_inr": float(np.median(dds)), "mc_dd_p95_inr": float(np.percentile(dds, 95)),
            "mc_final_median_inr": float(np.median(finals)),
            "mc_prob_loss": float(np.mean(np.array(finals) < 0) * 100)}
