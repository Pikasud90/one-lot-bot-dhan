"""Brute-force sweeps and the results leaderboard.

Every backtest the Lab runs is already persisted in `backtest_runs` (config, metrics,
artifacts). A SWEEP is simply "run every (strategy x instrument x timeframe) in a grid",
each as an ordinary saved run tagged with the sweep id, so the leaderboard can rank
them afterwards - and so a later conversation can read the table instead of re-running.

Ranking is deliberately boring: an SQN-style score (mean R x sqrt(n), capped at 100
trades) that is -inf below `min_trades`, with out-of-sample runs (walk-forward) flagged
so they can be preferred over in-sample ones.
"""

from __future__ import annotations

import datetime as dt
import itertools
import math
from typing import Any

from sqlalchemy import Engine, select

from ..clock import IST
from ..data.schema import backtest_runs
from ..settings import load_yaml
from ..strategies.registry import REGISTRY

FOCUS_TIMEFRAMES = ["5m", "15m", "30m", "60m"]


def desk_config() -> dict:
    return load_yaml("desk.yaml") or {}


def focus_instruments() -> list[str]:
    """The desk watchlist as Lab instrument keys: index options + the ten stocks."""
    cfg = desk_config()
    out = [str(i.get("tracked") or f"{i['key']}_OPT") for i in cfg.get("indices") or []]
    out += [str(s["key"]) for s in cfg.get("stocks") or []]
    return out


def plan(
    strategies: list[str] | None,
    instruments: list[str] | None,
    timeframes: list[str] | None,
    presets: bool = False,
) -> list[dict[str, Any]]:
    strategies = strategies or list(REGISTRY)
    instruments = instruments or focus_instruments()
    timeframes = timeframes or FOCUS_TIMEFRAMES
    bad = [s for s in strategies if s not in REGISTRY]
    if bad:
        raise ValueError(f"unknown strategies: {bad}")
    if not presets:
        return [
            {"strategy": s, "instrument_key": i.upper(), "timeframe": tf}
            for s, i, tf in itertools.product(strategies, instruments, timeframes)
        ]
    from .presets import presets_for

    out = []
    for s, i, tf in itertools.product(strategies, instruments, timeframes):
        for name, params in presets_for(s).items():
            out.append(
                {
                    "strategy": s,
                    "instrument_key": i.upper(),
                    "timeframe": tf,
                    "params": params,
                    "preset": name,
                }
            )
    return out


def matrix(engine: Engine, sweep_id: int | None = None, min_trades: int = 5) -> dict:
    """strategy -> instrument -> rows (timeframe x preset), rows sorted by win rate, high to low.

    Also marks, per (strategy, instrument), the best row by score and, per strategy, the
    best instrument. This is the operator's "which strategy, which instrument, which
    timeframe, which setting" table."""
    rows = leaderboard(
        engine, sweep_id=sweep_id, min_trades=min_trades, limit=100000, latest_only=True
    )
    tree: dict[str, dict[str, list[dict]]] = {}
    for r in rows:
        preset = _preset_of(r)
        r = {**r, "preset": preset}
        tree.setdefault(r["strategy"], {}).setdefault(r["instrument"], []).append(r)
    out_strats = []
    for strat, insts in tree.items():
        inst_rows = []
        for inst, lst in insts.items():
            lst.sort(
                key=lambda x: (
                    (x["win_rate"] or 0),
                    (x["score"] if x["score"] is not None else -1e9),
                ),
                reverse=True,
            )
            ranked = [x for x in lst if x["score"] is not None]
            best = max(ranked, key=lambda x: x["score"]) if ranked else None
            inst_rows.append(
                {
                    "instrument": inst,
                    "category": lst[0]["category"],
                    "rows": lst,
                    "best": best,
                    "best_win_rate": lst[0]["win_rate"] if lst else None,
                }
            )
        inst_rows.sort(key=lambda x: x["best"]["score"] if x["best"] else -1e9, reverse=True)
        best_inst = inst_rows[0]["best"] if inst_rows and inst_rows[0]["best"] else None
        out_strats.append(
            {
                "strategy": strat,
                "instruments": inst_rows,
                "best": best_inst,
                "positive": sum(
                    1
                    for ir in inst_rows
                    for x in ir["rows"]
                    if x["score"] is not None
                    and x["score"] > 0
                    and (x["profit_factor"] or 0) >= 1.2
                ),
            }
        )
    out_strats.sort(key=lambda x: x["best"]["score"] if x["best"] else -1e9, reverse=True)
    return {
        "strategies": out_strats,
        "runs": len(rows),
        "sweep_id": sweep_id,
        "min_trades": min_trades,
    }


def _preset_of(r: dict) -> str:
    p = r.get("params") or {}
    if not p:
        return "default"
    from .presets import presets_for

    for name, params in presets_for(r["strategy"]).items():
        if params and all(p.get(k) == v for k, v in params.items()) and len(p) == len(params):
            return name
    return "custom"


def rank_score(m: dict, min_trades: int = 10) -> float:
    n = int(m.get("trades") or 0)
    if n < min_trades:
        return float("-inf")
    er = m.get("expectancy_r")
    if er is None or (isinstance(er, float) and math.isnan(er)):
        return float("-inf")
    pf = m.get("profit_factor") or 0.0
    score = float(er) * math.sqrt(min(n, 100))
    if pf < 1.0:
        score -= 0.5
    return score


LEADERBOARD_SORTS = frozenset(
    {"score", "win_rate", "profit_pct", "profit_factor", "expectancy_r", "net_pnl", "trades"}
)


def leaderboard(
    engine: Engine,
    strategy: str | None = None,
    instrument: str | None = None,
    timeframe: str | None = None,
    category: str | None = None,
    kind: str | None = None,
    min_trades: int = 10,
    sweep_id: int | None = None,
    limit: int = 300,
    latest_only: bool = True,
    sort: str = "score",
    order: str = "desc",
) -> list[dict]:
    """Saved runs as leaderboard rows, sorted by `sort` (score, win_rate, profit_pct,
    profit_factor, expectancy_r, net_pnl, trades) in `order` (desc or asc) BEFORE the limit.

    Only the columns a row needs are read: a run's `artifacts` (equity curve, chart, trade
    breakdowns) average well over 100 KB, and reading them for thousands of runs made the
    Matrix and the leaderboard time out (owner feedback, 2026-11-07)."""
    if sort not in LEADERBOARD_SORTS:
        raise ValueError(f"sort must be one of {sorted(LEADERBOARD_SORTS)}")
    if order not in ("asc", "desc"):
        raise ValueError("order must be asc or desc")
    q = select(
        backtest_runs.c.id,
        backtest_runs.c.kind,
        backtest_runs.c.label,
        backtest_runs.c.config,
        backtest_runs.c.metrics,
        backtest_runs.c.created_at,
    ).where(backtest_runs.c.status == "DONE")
    if kind:
        q = q.where(backtest_runs.c.kind == kind)
    else:
        q = q.where(backtest_runs.c.kind.in_(["single", "walkforward"]))
    with engine.connect() as conn:
        rows = [
            dict(r) for r in conn.execute(q.order_by(backtest_runs.c.id.desc())).mappings().all()
        ]
    out: list[dict] = []
    seen: set[tuple] = set()
    for r in rows:
        cfg = r.get("config") or {}
        m = r.get("metrics") or {}
        if sweep_id is not None and cfg.get("_sweep_id") != sweep_id:
            continue
        s, i, tf = (
            cfg.get("strategy"),
            (cfg.get("instrument_key") or "").upper(),
            cfg.get("timeframe"),
        )
        if strategy and s != strategy:
            continue
        if instrument and i != instrument.upper():
            continue
        if timeframe and tf != timeframe:
            continue
        if category and (m.get("category") or _category_of(i)) != category.upper():
            continue
        key = (
            r["kind"],
            s,
            i,
            tf,
            _params_key(cfg.get("params")),
            cfg.get("sentiment_mode", "off"),
        )
        if latest_only and key in seen:
            continue
        seen.add(key)
        n = int(m.get("trades") or 0)
        oos = r["kind"] == "walkforward"
        out.append(
            {
                "run_id": r["id"],
                "kind": r["kind"],
                "oos": oos,
                "strategy": s,
                "instrument": i,
                "category": m.get("category") or _category_of(i),
                "timeframe": tf,
                "params": cfg.get("params") or {},
                "sentiment_mode": cfg.get("sentiment_mode", "off"),
                "trades": n,
                "win_rate": m.get("win_rate"),
                "profit_pct": _finite(m.get("total_return_pct")),
                "profit_factor": _finite(m.get("profit_factor")),
                "expectancy_r": m.get("expectancy_r"),
                "net_pnl": m.get("net_pnl"),
                "max_drawdown_inr": m.get("max_drawdown_inr"),
                "sharpe": m.get("sharpe"),
                "payoff": _finite(m.get("payoff")),
                "folds": m.get("folds"),
                "pricing": m.get("pricing"),
                "score": rank_score(m, min_trades),
                "created_at": (
                    r["created_at"].astimezone(IST)
                    if r["created_at"].tzinfo is not None
                    else r["created_at"]
                ).isoformat()
                if isinstance(r["created_at"], dt.datetime)
                else r["created_at"],
                "label": r.get("label"),
                "sweep_id": cfg.get("_sweep_id"),
            }
        )
    if sort == "score":
        out.sort(
            key=lambda x: (x["score"] if x["score"] != float("-inf") else -1e9, x["trades"]),
            reverse=order == "desc",
        )
    else:
        # A run without the figure always sorts last, whichever way the column is read.
        have = [x for x in out if isinstance(x.get(sort), (int, float))]
        lack = [x for x in out if not isinstance(x.get(sort), (int, float))]
        have.sort(key=lambda x: (x[sort], x["trades"]), reverse=order == "desc")
        out = have + lack
    for x in out:
        if x["score"] == float("-inf"):
            x["score"] = None
    return out[:limit]


def summarise(rows: list[dict]) -> dict:
    """Best per instrument and per strategy, for the leaderboard header."""
    best_inst: dict[str, dict] = {}
    best_strat: dict[str, dict] = {}
    for r in rows:
        if r["score"] is None:
            continue
        if r["instrument"] not in best_inst or r["score"] > best_inst[r["instrument"]]["score"]:
            best_inst[r["instrument"]] = r
        if r["strategy"] not in best_strat or r["score"] > best_strat[r["strategy"]]["score"]:
            best_strat[r["strategy"]] = r
    positive = [
        r
        for r in rows
        if r["score"] is not None and r["score"] > 0 and (r["profit_factor"] or 0) >= 1.2
    ]
    return {
        "runs": len(rows),
        "ranked": sum(1 for r in rows if r["score"] is not None),
        "positive": len(positive),
        "best_by_instrument": best_inst,
        "best_by_strategy": best_strat,
    }


def _params_key(p: Any) -> str:
    try:
        import json

        return json.dumps(p or {}, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        return str(p)


def _finite(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def _category_of(instrument_key: str) -> str:
    if instrument_key.endswith("_OPT"):
        return "OPTIONS"
    if instrument_key.endswith("_FUT"):
        return "FUTURES"
    if instrument_key in ("USDINR", "EURINR", "GBPINR", "JPYINR", "EURUSD"):
        return "CURRENCY"
    return "STOCK"
