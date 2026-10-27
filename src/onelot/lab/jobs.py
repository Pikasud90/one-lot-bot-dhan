"""Background jobs for the web app: backtests, optimisations and backfills run
on a worker thread inside the engine process and report progress through the
`backtest_runs` / `data_jobs` tables, which the UI polls.

Threads rather than subprocesses: the Lab is numpy-bound and releases the GIL
in the hot loops, the engine process already owns the database, and a thread
cannot leave a zombie behind on Windows.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
from sqlalchemy import Engine, select, update

from ..data.db import utcnow
from ..data.schema import backtest_runs, backtest_trades, data_jobs
from ..logging_setup import get_logger
from .runner import BacktestConfig, DataCache, per_breakdowns, run_backtest

log = get_logger("lab.jobs")


def _clean(v):
    if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
        return None
    if isinstance(v, np.floating):
        return _clean(float(v))
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if isinstance(v, (pd.Timestamp, dt.datetime)):
        return v.isoformat()
    return v


class JobRunner:
    """One worker pool shared by every long-running UI action."""

    def __init__(self, engine: Engine, workers: int = 2) -> None:
        self.engine = engine
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lab")
        self._stop_flags: dict[int, threading.Event] = {}

    # ── backtests ─────────────────────────────────────────────────────────
    def submit_backtest(self, config: dict, label: str = "") -> int:
        with self.engine.begin() as conn:
            rid = conn.execute(backtest_runs.insert().values(
                created_at=utcnow(), kind="single", label=label, config=config, status="QUEUED",
                progress=0.0)).inserted_primary_key[0]
        self.pool.submit(self._run_backtest, int(rid), dict(config))
        return int(rid)

    def _progress(self, rid: int, frac: float, msg: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(backtest_runs).where(backtest_runs.c.id == rid)
                         .values(progress=float(frac), message=msg[:200]))

    def _run_backtest(self, rid: int, cfgd: dict) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(backtest_runs).where(backtest_runs.c.id == rid)
                         .values(status="RUNNING", progress=0.01))
        try:
            cfg = BacktestConfig(**cfgd)
            res = run_backtest(cfg, DataCache(self.engine),
                               progress=lambda f, m: self._progress(rid, 0.05 + 0.85 * f, m),
                               keep_bars=True)
            rows = []
            for t in res.trades.to_dict("records"):
                rows.append({
                    "run_id": rid, "strategy": t["strategy"], "instrument_key": t["instrument_key"],
                    "side": t["side"], "signal_ts": pd.Timestamp(t["signal_ts"]).to_pydatetime(),
                    "entry_ts": pd.Timestamp(t["entry_ts"]).to_pydatetime(),
                    "entry_price": float(t["entry_price"]),
                    "exit_ts": pd.Timestamp(t["exit_ts"]).to_pydatetime(),
                    "exit_price": float(t["exit_price"]), "qty": float(t["qty"]),
                    "gross_pnl": float(t["gross_pnl"]), "costs": float(t["costs"]),
                    "net_pnl": float(t["net_pnl"]), "ret_pct": float(t["ret_pct"]),
                    "r_multiple": float(t["r_multiple"]), "exit_reason": t["exit_reason"],
                    "strength": float(t["strength"] or 0), "mae_r": float(t["mae_r"]),
                    "mfe_r": float(t["mfe_r"]), "bars_held": int(t["bars_held"]),
                    "detail": _clean(t.get("detail") or {})})
            eq = res.equity
            artifacts = {
                "equity": [[str(i.date()), float(v)] for i, v in eq.items()],
                "breakdowns": _clean(per_breakdowns(res)),
                "chart": _clean(res.chart_payload()),
                "monthly": _clean(_monthly(eq, cfg.reference_capital)),
            }
            with self.engine.begin() as conn:
                if rows:
                    conn.execute(backtest_trades.insert(), rows)
                conn.execute(update(backtest_runs).where(backtest_runs.c.id == rid).values(
                    status="DONE", progress=1.0, finished_at=utcnow(), config_hash=cfg.key(),
                    metrics=_clean(res.metrics), artifacts=artifacts,
                    message=f"{len(res.trades)} trades"))
        except Exception as exc:  # noqa: BLE001
            log.exception("backtest job %s failed", rid)
            with self.engine.begin() as conn:
                conn.execute(update(backtest_runs).where(backtest_runs.c.id == rid).values(
                    status="FAILED", message=f"{type(exc).__name__}: {exc}"[:500],
                    finished_at=utcnow(), artifacts={"traceback": traceback.format_exc()[-4000:]}))

    # ── optimisation / walk-forward ───────────────────────────────────────
    def submit_optimize(self, config: dict, space: dict, trials: int = 40, kind: str = "optimize",
                        wf: dict | None = None, label: str = "") -> int:
        payload = {**config, "_space": space, "_trials": trials, "_wf": wf}
        with self.engine.begin() as conn:
            rid = conn.execute(backtest_runs.insert().values(
                created_at=utcnow(), kind=kind, label=label, config=payload, status="QUEUED",
                progress=0.0)).inserted_primary_key[0]
        self.pool.submit(self._run_opt, int(rid), dict(payload), kind)
        return int(rid)

    def _run_opt(self, rid: int, cfgd: dict, kind: str) -> None:
        from .optimizer import Evaluator, optimize, trading_days, walk_forward

        with self.engine.begin() as conn:
            conn.execute(update(backtest_runs).where(backtest_runs.c.id == rid)
                         .values(status="RUNNING", progress=0.01))
        try:
            space = cfgd.pop("_space")
            trials = int(cfgd.pop("_trials", 40))
            wf_cfg = cfgd.pop("_wf", None)
            base = BacktestConfig(**cfgd)
            cache = DataCache(self.engine)
            with Evaluator(workers=1, cache=cache) as ev:
                if kind == "optimize":
                    res = optimize(base, space, ev, n_trials=trials,
                                   progress=lambda f, m: self._progress(rid, f, m))
                    out = {"best_params": res.best_params, "best_score": res.best_score,
                           "best_metrics": _clean(res.best_metrics),
                           "trials": json.loads(res.trials.to_json(orient="records"))}
                    metrics = _clean(res.best_metrics)
                else:
                    days = trading_days(cache, cache.spec(base.instrument_key).series_key, base.timeframe)
                    wf = walk_forward(base, space, ev, days, int((wf_cfg or {}).get("is_days", 40)),
                                      int((wf_cfg or {}).get("oos_days", 10)), n_trials=trials,
                                      progress=lambda f, m: self._progress(rid, f, m))
                    out = {"folds": _clean(wf.folds), "oos_metrics": _clean(wf.oos_metrics),
                           "default_oos_metrics": _clean(wf.default_oos_metrics),
                           "chosen_params": wf.chosen_params,
                           "best_params": wf.chosen_params[-1] if wf.chosen_params else {}}
                    metrics = _clean(wf.oos_metrics)
            with self.engine.begin() as conn:
                conn.execute(update(backtest_runs).where(backtest_runs.c.id == rid).values(
                    status="DONE", progress=1.0, finished_at=utcnow(), metrics=metrics,
                    artifacts=_clean(out), message="done"))
        except Exception as exc:  # noqa: BLE001
            log.exception("optimisation job %s failed", rid)
            with self.engine.begin() as conn:
                conn.execute(update(backtest_runs).where(backtest_runs.c.id == rid).values(
                    status="FAILED", message=f"{type(exc).__name__}: {exc}"[:500], finished_at=utcnow()))

    # ── data backfill ─────────────────────────────────────────────────────
    def submit_backfill(self, series_keys: list[str] | None, resolutions: list[str] | None,
                        provider: str | None, adapter=None, full: bool = False) -> int:
        payload = {"series": series_keys, "resolutions": resolutions, "provider": provider, "full": full}
        with self.engine.begin() as conn:
            jid = conn.execute(data_jobs.insert().values(
                kind="backfill", status="QUEUED", progress=0.0, created_at=utcnow(),
                payload=payload)).inserted_primary_key[0]
        flag = threading.Event()
        self._stop_flags[int(jid)] = flag
        self.pool.submit(self._run_backfill, int(jid), payload, adapter, flag)
        return int(jid)

    def stop_job(self, jid: int) -> bool:
        flag = self._stop_flags.get(jid)
        if flag is None:
            return False
        flag.set()
        return True

    def _job_progress(self, jid: int, frac: float, msg: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(data_jobs).where(data_jobs.c.id == jid)
                         .values(progress=float(frac), message=msg[:200]))

    def _run_backfill(self, jid: int, payload: dict, adapter, flag: threading.Event) -> None:
        from ..data.backfill import backfill, choose_provider

        with self.engine.begin() as conn:
            conn.execute(update(data_jobs).where(data_jobs.c.id == jid).values(status="RUNNING"))
        try:
            provider = choose_provider(payload.get("provider"), adapter)
            res = backfill(self.engine, provider, payload.get("series"), payload.get("resolutions"),
                           full=bool(payload.get("full")),
                           progress=lambda f, m: self._job_progress(jid, f, m), stop_flag=flag.is_set)
            with self.engine.begin() as conn:
                conn.execute(update(data_jobs).where(data_jobs.c.id == jid).values(
                    status="DONE" if not res.errors else "DONE_WITH_ERRORS", progress=1.0,
                    finished_at=utcnow(), payload={**payload, "result": res.as_dict()},
                    message=f"{res.rows} rows via {res.provider}; {len(res.errors)} errors"))
        except Exception as exc:  # noqa: BLE001
            log.exception("backfill job %s failed", jid)
            with self.engine.begin() as conn:
                conn.execute(update(data_jobs).where(data_jobs.c.id == jid).values(
                    status="FAILED", message=f"{type(exc).__name__}: {exc}"[:500], finished_at=utcnow()))
        finally:
            self._stop_flags.pop(jid, None)

    # ── reads ─────────────────────────────────────────────────────────────
    def list_runs(self, limit: int = 50) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(backtest_runs.c.id, backtest_runs.c.created_at, backtest_runs.c.finished_at,
                                       backtest_runs.c.kind, backtest_runs.c.label, backtest_runs.c.config,
                                       backtest_runs.c.status, backtest_runs.c.progress, backtest_runs.c.message,
                                       backtest_runs.c.metrics)
                                .order_by(backtest_runs.c.id.desc()).limit(limit)).mappings().all()
        return [_clean(dict(r)) for r in rows]

    def get_run(self, rid: int, with_trades: bool = True) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(backtest_runs).where(backtest_runs.c.id == rid)).mappings().first()
            if row is None:
                return None
            out = _clean(dict(row))
            if with_trades:
                tr = conn.execute(select(backtest_trades).where(backtest_trades.c.run_id == rid)
                                  .order_by(backtest_trades.c.entry_ts)).mappings().all()
                out["trades"] = [_clean(dict(t)) for t in tr]
        return out

    def list_jobs(self, limit: int = 20) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(data_jobs).order_by(data_jobs.c.id.desc()).limit(limit)).mappings().all()
        return [_clean(dict(r)) for r in rows]


def _monthly(eq: pd.Series, capital: float) -> list[dict]:
    if eq is None or eq.empty:
        return []
    e = eq.copy()
    e.index = pd.DatetimeIndex(e.index)
    if e.index.tz is not None:
        e.index = e.index.tz_localize(None)
    m = e.resample("ME").last()
    prev = m.shift(1).fillna(capital)
    pnl = m - prev
    return [{"month": i.strftime("%Y-%m"), "pnl": float(v), "ret_pct": float(v / capital * 100)}
            for i, v in pnl.items()]
