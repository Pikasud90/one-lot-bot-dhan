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
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
from sqlalchemy import Engine, select, update
from sqlalchemy.exc import OperationalError

from ..clock import IST
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
        # Stored times come back in UTC (TZDateTime); everything the Lab shows is IST, so a
        # 09:45 IST entry is never shown as 04:15 (owner feedback, 2026-11-07).
        if v.tzinfo is not None:
            v = v.astimezone(IST)
        return v.isoformat()
    return v


# Waits between attempts when SQLite reports "database is locked" (seconds; about 2.5 min
# in all). A job's status writes must land: a lost write left runs RUNNING forever.
LOCK_RETRY_DELAYS: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 30.0, 60.0)
INTERRUPTED = "interrupted: the app restarted while this ran; run it again"


def _locked(exc: BaseException) -> bool:
    return isinstance(exc, OperationalError) and "locked" in str(exc).lower()


def _retry_write(engine: Engine, fn, what: str):
    """Run `fn(conn)` in its own transaction, retrying while the database is locked.

    The transaction is atomic, so a retry repeats nothing that landed. Any other error,
    or a lock that outlasts every retry, is raised."""
    delays = LOCK_RETRY_DELAYS
    for attempt in range(len(delays) + 1):
        try:
            with engine.begin() as conn:
                return fn(conn)
        except OperationalError as exc:
            if not _locked(exc) or attempt >= len(delays):
                raise
            log.warning(
                "database locked while %s; retry %d in %.2fs", what, attempt + 1, delays[attempt]
            )
            time.sleep(delays[attempt])
    return None  # unreachable


class JobRunner:
    """One worker pool shared by every long-running UI action."""

    def __init__(self, engine: Engine, workers: int = 2) -> None:
        self.engine = engine
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lab")
        self._stop_flags: dict[int, threading.Event] = {}
        self._run_flags: dict[int, threading.Event] = {}
        self.recovered = self._recover_interrupted()

    def _recover_interrupted(self) -> int:
        """Runs and data jobs still RUNNING or QUEUED belong to a process that is gone (the
        app holds a single-instance lock): mark them FAILED so the Lab never waits on them."""

        def fn(conn) -> int:
            n = conn.execute(
                update(backtest_runs)
                .where(backtest_runs.c.status.in_(["RUNNING", "QUEUED"]))
                .values(status="FAILED", message=INTERRUPTED, finished_at=utcnow())
            ).rowcount
            m = conn.execute(
                update(data_jobs)
                .where(data_jobs.c.status.in_(["RUNNING", "QUEUED"]))
                .values(status="FAILED", message=INTERRUPTED, finished_at=utcnow())
            ).rowcount
            return int(n or 0) + int(m or 0)

        try:
            n = _retry_write(self.engine, fn, "recovering interrupted runs")
        except Exception:  # noqa: BLE001
            log.exception("could not recover interrupted Lab runs")
            return 0
        if n:
            log.warning("marked %d interrupted Lab run(s) / data job(s) FAILED", n)
        return int(n or 0)

    def _set_run(self, rid: int, what: str, **values) -> None:
        _retry_write(
            self.engine,
            lambda c: c.execute(
                update(backtest_runs).where(backtest_runs.c.id == rid).values(**values)
            ),
            what,
        )

    def _fail_run(self, rid: int, exc: BaseException, **extra) -> None:
        try:
            self._set_run(
                rid,
                f"marking run {rid} failed",
                status="FAILED",
                message=f"{type(exc).__name__}: {exc}"[:500],
                finished_at=utcnow(),
                **extra,
            )
        except Exception:  # noqa: BLE001
            log.exception("could not mark run %s failed", rid)

    def _set_job(self, jid: int, what: str, **values) -> None:
        _retry_write(
            self.engine,
            lambda c: c.execute(update(data_jobs).where(data_jobs.c.id == jid).values(**values)),
            what,
        )

    # ── backtests ─────────────────────────────────────────────────────────
    def submit_backtest(self, config: dict, label: str = "") -> int:
        with self.engine.begin() as conn:
            rid = conn.execute(
                backtest_runs.insert().values(
                    created_at=utcnow(),
                    kind="single",
                    label=label,
                    config=config,
                    status="QUEUED",
                    progress=0.0,
                )
            ).inserted_primary_key[0]
        self.pool.submit(self._run_backtest, int(rid), dict(config))
        return int(rid)

    def _progress(self, rid: int, frac: float, msg: str) -> None:
        """Best effort: a progress line lost to a locked database must never fail the run."""
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    update(backtest_runs)
                    .where(backtest_runs.c.id == rid)
                    .values(progress=float(frac), message=msg[:200])
                )
        except OperationalError as exc:
            if not _locked(exc):
                raise
            log.debug("progress of run %s skipped: database locked", rid)

    def _run_backtest(
        self, rid: int, cfgd: dict, cache: DataCache | None = None, light: bool = False
    ) -> None:
        """One backtest. `light` (sweep children) keeps no price chart in the run's artifacts:
        thousands of sweep runs with a chart each grew the database by gigabytes."""
        try:
            self._set_run(rid, f"starting run {rid}", status="RUNNING", progress=0.01)
            cfg = BacktestConfig(**{k: v for k, v in cfgd.items() if not k.startswith("_")})
            res = run_backtest(
                cfg,
                cache or DataCache(self.engine),
                progress=lambda f, m: self._progress(rid, 0.05 + 0.85 * f, m),
                keep_bars=not light,
            )
            rows = []
            for t in res.trades.to_dict("records"):
                rows.append(
                    {
                        "run_id": rid,
                        "strategy": t["strategy"],
                        "instrument_key": t["instrument_key"],
                        "side": t["side"],
                        "signal_ts": pd.Timestamp(t["signal_ts"]).to_pydatetime(),
                        "entry_ts": pd.Timestamp(t["entry_ts"]).to_pydatetime(),
                        "entry_price": float(t["entry_price"]),
                        "exit_ts": pd.Timestamp(t["exit_ts"]).to_pydatetime(),
                        "exit_price": float(t["exit_price"]),
                        "qty": float(t["qty"]),
                        "gross_pnl": float(t["gross_pnl"]),
                        "costs": float(t["costs"]),
                        "net_pnl": float(t["net_pnl"]),
                        "ret_pct": float(t["ret_pct"]),
                        "r_multiple": float(t["r_multiple"]),
                        "exit_reason": t["exit_reason"],
                        "strength": float(t["strength"] or 0),
                        "mae_r": float(t["mae_r"]),
                        "mfe_r": float(t["mfe_r"]),
                        "bars_held": int(t["bars_held"]),
                        "detail": _clean(t.get("detail") or {}),
                    }
                )
            eq = res.equity
            artifacts = {
                "equity": [[str(i.date()), float(v)] for i, v in eq.items()],
                "breakdowns": _clean(per_breakdowns(res)),
                "monthly": _clean(_monthly(eq, cfg.reference_capital)),
            }
            if not light:
                artifacts["chart"] = _clean(res.chart_payload())

            def done(conn) -> None:
                if rows:
                    conn.execute(backtest_trades.insert(), rows)
                conn.execute(
                    update(backtest_runs)
                    .where(backtest_runs.c.id == rid)
                    .values(
                        status="DONE",
                        progress=1.0,
                        finished_at=utcnow(),
                        config_hash=cfg.key(),
                        metrics=_clean(res.metrics),
                        artifacts=artifacts,
                        message=f"{len(res.trades)} trades",
                    )
                )

            _retry_write(self.engine, done, f"saving run {rid}")
        except Exception as exc:  # noqa: BLE001
            log.exception("backtest job %s failed", rid)
            self._fail_run(rid, exc, artifacts={"traceback": traceback.format_exc()[-4000:]})

    # ── sweeps (every strategy x instrument x timeframe, each a saved run) ──
    def submit_sweep(self, combos: list[dict], label: str = "", base: dict | None = None) -> int:
        payload = {"combos": combos, "n": len(combos), "base": base or {}}
        with self.engine.begin() as conn:
            rid = conn.execute(
                backtest_runs.insert().values(
                    created_at=utcnow(),
                    kind="sweep",
                    label=label or f"sweep of {len(combos)} runs",
                    config=payload,
                    status="QUEUED",
                    progress=0.0,
                )
            ).inserted_primary_key[0]
        flag = threading.Event()
        self._run_flags[int(rid)] = flag
        self.pool.submit(self._run_sweep, int(rid), payload, flag)
        return int(rid)

    def stop_run(self, rid: int) -> bool:
        flag = self._run_flags.get(rid)
        if flag is None:
            return False
        flag.set()
        return True

    def _run_sweep(self, rid: int, payload: dict, flag: threading.Event) -> None:
        from .sweep import leaderboard, summarise

        combos, base = payload["combos"], payload.get("base") or {}
        cache = DataCache(self.engine)
        results: list[dict] = []
        stopped = False
        try:
            self._set_run(
                rid,
                f"starting sweep {rid}",
                status="RUNNING",
                progress=0.01,
                message=f"0/{len(combos)}",
            )
            for i, combo in enumerate(combos):
                if flag.is_set():
                    stopped = True
                    break
                cfg = BacktestConfig.from_settings(
                    combo["strategy"],
                    combo["instrument_key"],
                    timeframe=combo["timeframe"],
                    params=combo.get("params") or {},
                    **base,
                )
                cfgd = {**cfg.__dict__, "_sweep_id": rid, "_preset": combo.get("preset", "default")}
                label = f"sweep#{rid} {cfg.strategy} {cfg.instrument_key} {cfg.timeframe} {combo.get('preset', '')}".strip()
                child = _retry_write(
                    self.engine,
                    lambda c, cfgd=cfgd, label=label: c.execute(
                        backtest_runs.insert().values(
                            created_at=utcnow(),
                            kind="single",
                            label=label,
                            config=cfgd,
                            status="QUEUED",
                            progress=0.0,
                        )
                    ).inserted_primary_key[0],
                    f"queueing a run of sweep {rid}",
                )
                self._run_backtest(int(child), dict(cfgd), cache=cache, light=True)
                with self.engine.connect() as conn:
                    row = conn.execute(
                        select(
                            backtest_runs.c.status, backtest_runs.c.metrics, backtest_runs.c.message
                        ).where(backtest_runs.c.id == child)
                    ).first()
                m = (row[1] or {}) if row else {}
                results.append(
                    {
                        "run_id": int(child),
                        "status": row[0] if row else "?",
                        **combo,
                        "trades": m.get("trades"),
                        "net_pnl": m.get("net_pnl"),
                        "profit_factor": m.get("profit_factor"),
                        "expectancy_r": m.get("expectancy_r"),
                        "message": (row[2] or "")[:120] if row else "",
                    }
                )
                self._progress(
                    rid,
                    (i + 1) / len(combos),
                    f"{i + 1}/{len(combos)} {cfg.strategy} {cfg.instrument_key} {cfg.timeframe}",
                )
            board = leaderboard(self.engine, sweep_id=rid, min_trades=10, limit=1000)
            summary = summarise(board)
            metrics = {
                "combos": len(combos),
                "done": len(results),
                "failed": sum(1 for r in results if r["status"] != "DONE"),
                "ranked": summary["ranked"],
                "positive": summary["positive"],
                "stopped": stopped,
            }
            self._set_run(
                rid,
                f"saving sweep {rid}",
                status="DONE",
                progress=1.0,
                finished_at=utcnow(),
                metrics=metrics,
                artifacts=_clean(
                    {
                        "results": results,
                        "leaderboard": board[:200],
                        "best_by_instrument": summary["best_by_instrument"],
                        "best_by_strategy": summary["best_by_strategy"],
                    }
                ),
                message=("stopped after " if stopped else "done: ")
                + f"{len(results)}/{len(combos)}",
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("sweep %s failed", rid)
            self._fail_run(rid, exc, artifacts=_clean({"results": results}))
        finally:
            self._run_flags.pop(rid, None)

    # ── optimisation / walk-forward ───────────────────────────────────────
    def submit_optimize(
        self,
        config: dict,
        space: dict,
        trials: int = 40,
        kind: str = "optimize",
        wf: dict | None = None,
        label: str = "",
    ) -> int:
        payload = {**config, "_space": space, "_trials": trials, "_wf": wf}
        with self.engine.begin() as conn:
            rid = conn.execute(
                backtest_runs.insert().values(
                    created_at=utcnow(),
                    kind=kind,
                    label=label,
                    config=payload,
                    status="QUEUED",
                    progress=0.0,
                )
            ).inserted_primary_key[0]
        self.pool.submit(self._run_opt, int(rid), dict(payload), kind)
        return int(rid)

    def _run_opt(self, rid: int, cfgd: dict, kind: str) -> None:
        from .optimizer import Evaluator, optimize, trading_days, walk_forward

        try:
            self._set_run(rid, f"starting {kind} {rid}", status="RUNNING", progress=0.01)
            space = cfgd.pop("_space")
            trials = int(cfgd.pop("_trials", 40))
            wf_cfg = cfgd.pop("_wf", None)
            base = BacktestConfig(**cfgd)
            cache = DataCache(self.engine)
            with Evaluator(workers=1, cache=cache) as ev:
                if kind == "optimize":
                    res = optimize(
                        base,
                        space,
                        ev,
                        n_trials=trials,
                        progress=lambda f, m: self._progress(rid, f, m),
                    )
                    out = {
                        "best_params": res.best_params,
                        "best_score": res.best_score,
                        "best_metrics": _clean(res.best_metrics),
                        "trials": json.loads(res.trials.to_json(orient="records")),
                    }
                    metrics = _clean(res.best_metrics)
                else:
                    days = trading_days(
                        cache, cache.spec(base.instrument_key).series_key, base.timeframe
                    )
                    wf_cfg = wf_cfg or {}
                    wf = walk_forward(
                        base,
                        space,
                        ev,
                        days,
                        wf_cfg.get("is_days"),
                        wf_cfg.get("oos_days"),
                        n_trials=trials,
                        progress=lambda f, m: self._progress(rid, f, m),
                    )
                    if not wf.folds:
                        raise RuntimeError(
                            "walk-forward produced no folds: " + wf.plan.get("note", "")
                        )
                    out = {
                        "folds": _clean(wf.folds),
                        "oos_metrics": _clean(wf.oos_metrics),
                        "default_oos_metrics": _clean(wf.default_oos_metrics),
                        "chosen_params": wf.chosen_params,
                        "plan": _clean(wf.plan),
                        "best_params": wf.chosen_params[-1] if wf.chosen_params else {},
                    }
                    metrics = _clean(wf.oos_metrics)
                    metrics["folds"] = len(wf.folds)
            self._set_run(
                rid,
                f"saving {kind} {rid}",
                status="DONE",
                progress=1.0,
                finished_at=utcnow(),
                metrics=metrics,
                artifacts=_clean(out),
                message="done",
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("optimisation job %s failed", rid)
            self._fail_run(rid, exc)

    # ── data backfill ─────────────────────────────────────────────────────
    def submit_backfill(
        self,
        series_keys: list[str] | None,
        resolutions: list[str] | None,
        provider: str | None,
        adapter=None,
        full: bool = False,
    ) -> int:
        payload = {
            "series": series_keys,
            "resolutions": resolutions,
            "provider": provider,
            "full": full,
        }
        with self.engine.begin() as conn:
            jid = conn.execute(
                data_jobs.insert().values(
                    kind="backfill",
                    status="QUEUED",
                    progress=0.0,
                    created_at=utcnow(),
                    payload=payload,
                )
            ).inserted_primary_key[0]
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
        """Best effort, like `_progress`."""
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    update(data_jobs)
                    .where(data_jobs.c.id == jid)
                    .values(progress=float(frac), message=msg[:200])
                )
        except OperationalError as exc:
            if not _locked(exc):
                raise
            log.debug("progress of data job %s skipped: database locked", jid)

    def _run_backfill(self, jid: int, payload: dict, adapter, flag: threading.Event) -> None:
        from ..data.backfill import backfill, choose_provider

        try:
            self._set_job(jid, f"starting data job {jid}", status="RUNNING")
            provider = choose_provider(payload.get("provider"), adapter)
            res = backfill(
                self.engine,
                provider,
                payload.get("series"),
                payload.get("resolutions"),
                full=bool(payload.get("full")),
                progress=lambda f, m: self._job_progress(jid, f, m),
                stop_flag=flag.is_set,
            )
            self._set_job(
                jid,
                f"saving data job {jid}",
                status="DONE" if not res.errors else "DONE_WITH_ERRORS",
                progress=1.0,
                finished_at=utcnow(),
                payload={**payload, "result": res.as_dict()},
                message=f"{res.rows} rows via {res.provider}; {len(res.errors)} errors",
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("backfill job %s failed", jid)
            try:
                self._set_job(
                    jid,
                    f"marking data job {jid} failed",
                    status="FAILED",
                    message=f"{type(exc).__name__}: {exc}"[:500],
                    finished_at=utcnow(),
                )
            except Exception:  # noqa: BLE001
                log.exception("could not mark data job %s failed", jid)
        finally:
            self._stop_flags.pop(jid, None)

    # ── reads ─────────────────────────────────────────────────────────────
    def list_runs(self, limit: int = 50) -> list[dict]:
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    select(
                        backtest_runs.c.id,
                        backtest_runs.c.created_at,
                        backtest_runs.c.finished_at,
                        backtest_runs.c.kind,
                        backtest_runs.c.label,
                        backtest_runs.c.config,
                        backtest_runs.c.status,
                        backtest_runs.c.progress,
                        backtest_runs.c.message,
                        backtest_runs.c.metrics,
                    )
                    .order_by(backtest_runs.c.id.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return [_clean(dict(r)) for r in rows]

    def get_run(self, rid: int, with_trades: bool = True) -> dict | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(select(backtest_runs).where(backtest_runs.c.id == rid))
                .mappings()
                .first()
            )
            if row is None:
                return None
            out = _clean(dict(row))
            if with_trades:
                tr = (
                    conn.execute(
                        select(backtest_trades)
                        .where(backtest_trades.c.run_id == rid)
                        .order_by(backtest_trades.c.entry_ts)
                    )
                    .mappings()
                    .all()
                )
                out["trades"] = [_clean(dict(t)) for t in tr]
        return out

    def list_jobs(self, limit: int = 20) -> list[dict]:
        with self.engine.connect() as conn:
            rows = (
                conn.execute(select(data_jobs).order_by(data_jobs.c.id.desc()).limit(limit))
                .mappings()
                .all()
            )
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
    return [
        {"month": i.strftime("%Y-%m"), "pnl": float(v), "ret_pct": float(v / capital * 100)}
        for i, v in pnl.items()
    ]
