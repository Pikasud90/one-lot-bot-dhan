"""Cross-instrument retune of every Lab strategy (owner request, 2026-11-07).

One parameter set per strategy x timeframe x basket must work ACROSS the basket, never for
one instrument. The protocol is fixed before any result is seen:

* TRAIN  2021-11-08 .. 2024-11-07  - the search ranks parameter sets here only;
* VALID  2024-11-08 .. 2025-11-07  - picks one set among each group's best TRAIN sets;
* TEST   2025-11-08 .. 2026-11-07  - judged ONCE, at the end, for the picked set only.

Each parameter set is backtested once per instrument over the full history (indicators look
back only; parameters do not adapt), and its trades are split by entry time into the three
periods. Score = pooled t-statistic of the net return per trade (after costs and slippage),
with a minimum number of trades and BREADTH: at least half of the instruments that traded
must be profitable on their own. Adani Green is excluded at the owner's request.

Search = successive halving: every group gets the defaults plus random sets; the better half
gets more random sets and neighbours of its best; the best quarter gets more neighbours. The
picked sets are re-run with the 1-minute fill path for the reported numbers.

Version 2 (same day, after version 1 found index volume switching on in the TEST year):
`--every-group` gives every group the full three rounds (version 1 halved ACROSS groups, so
weak groups saw only 16 sets); `--warm DIR` starts each group from a previous run's best TRAIN
sets; `--space v2` adds the entry start and the NIFTY market filter; `--min-years 2` demands a
profit in at least two of the three TRAIN years; every pick is re-run at double costs too.

Usage: .venv/bin/python scripts/cross_retune.py --out data/research/cross_retune [--workers 5]
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

STOCKS = [
    "RELIANCE", "MM", "SBIN", "HDFCBANK", "ITC", "TMPV", "MARUTI", "ICICIBANK", "HEROMOTOCO",
    "INFY", "TCS", "AXISBANK", "KOTAKBANK", "LT", "BAJFINANCE", "BHARTIARTL", "TATASTEEL", "SUNPHARMA",
]  # fmt: skip
INDEX_OPTIONS = ["BANKNIFTY_OPT", "NIFTY_OPT", "FINNIFTY_OPT", "MIDCPNIFTY_OPT", "SENSEX_OPT"]
BASKETS = {"STOCK": STOCKS, "OPTIONS": INDEX_OPTIONS}
TIMEFRAMES = ["15m", "30m", "60m"]
T0, TRAIN_END, VALID_END, T_END = "2021-11-08", "2024-11-08", "2025-11-08", "2026-11-08"
PERIODS = ("train", "valid", "test")
MIN_TRADES = {"STOCK": {"train": 60, "valid": 15}, "OPTIONS": {"train": 40, "valid": 10}}
MIN_INSTRUMENT_TRADES = 3  # an instrument counts toward breadth with at least this many trades

# The shared knobs that decide how picky and how patient a strategy is, added to each
# strategy's curated Lab space (onelot.lab.research.SPACES) where the strategy has them.
EXTRA_SPACE = {
    # "direction" sets allow_long / allow_short (long-only, short-only or both)
    "direction": {"choices": ["both", "long", "short"]},
    "regime_filter": {"choices": [True, False]},
    "entry_end": {"choices": ["11:30", "13:00", "14:45"]},
    "min_strength": {"choices": [0, 40, 55, 70]},
    "max_trades_per_day": {"choices": [1, 2]},
    "max_bars": {"choices": [0, 12, 24]},
    "partial_r": {"choices": [0.0, 1.0]},
}

# version 2 adds when a strategy may start trading and the NIFTY market filter
EXTRA_SPACE_V2 = {
    "entry_start": {"choices": ["09:30", "10:00", "10:30"]},
    "market_filter": {"choices": [True, False]},
}
SPACE_VERSION = "v1"
MIN_TRAIN_YEARS = 0  # TRAIN years (of three) that must be profitable; set by --min-years

_cache = None


def _init_worker() -> None:
    global _cache
    logging.disable(logging.WARNING)
    from onelot.lab.runner import DataCache

    _cache = DataCache()


def _run(task: tuple) -> tuple:
    """(strategy, timeframe, params_json, instrument, fine) -> (task, arrays | None, error)."""
    from onelot.lab.runner import BacktestConfig, run_backtest

    strategy, tf, pj, inst, fine = task[:5]
    cost_mult = task[5] if len(task) > 5 else 1.0
    params = json.loads(pj)
    direction = params.pop("direction", "both")
    if direction != "both":
        params["allow_long"], params["allow_short"] = direction == "long", direction == "short"
    gross = {"slippage_pct": 0.0} if cost_mult == 0 else {}  # cost x0 = before costs and slippage
    cfg = BacktestConfig.from_settings(
        strategy, inst, timeframe=tf, params=params, use_fine_path=fine, cost_multiplier=cost_mult,
        **gross,
    )  # fmt: skip
    try:
        res = run_backtest(cfg, _cache)
    except Exception as exc:  # noqa: BLE001 - one bad set never stops the search
        return task, None, f"{type(exc).__name__}: {str(exc)[:200]}"
    t = res.trades
    if t is None or t.empty:
        return task, (np.zeros(0, "int64"), np.zeros(0), np.zeros(0)), None
    ts = pd.to_datetime(t["entry_ts"], utc=True).astype("int64").to_numpy()
    return (
        task,
        (ts, t["ret_pct"].astype(float).to_numpy(), t["net_pnl"].astype(float).to_numpy()),
        None,
    )


def space_for(strategy: str) -> dict:
    from onelot.lab.research import SPACES
    from onelot.strategies.registry import REGISTRY

    fields = REGISTRY[strategy].Params.model_fields
    space = {k: v for k, v in (SPACES.get(strategy) or {}).items() if k in fields}
    extra = {**EXTRA_SPACE, **(EXTRA_SPACE_V2 if SPACE_VERSION == "v2" else {})}
    for k, v in extra.items():
        known = k in fields or (k == "direction" and "allow_long" in fields)
        if known and k not in space:
            space[k] = v
    return space


def values(spec: dict) -> list:
    from onelot.lab.optimizer import grid_values

    return grid_values(spec)


def sample(space: dict, rng: random.Random) -> dict:
    return {k: rng.choice(values(v)) for k, v in space.items()}


def neighbour(params: dict, space: dict, rng: random.Random) -> dict:
    """Move one or two parameters one step from `params` (a fresh set on an edge)."""
    out = dict(params)
    keys = list(space)
    for k in rng.sample(keys, k=min(len(keys), rng.choice([1, 1, 2]))):
        vals = values(space[k])
        cur = out.get(k, vals[len(vals) // 2])
        i = vals.index(cur) if cur in vals else len(vals) // 2
        out[k] = vals[min(len(vals) - 1, max(0, i + rng.choice([-1, 1])))]
    return out


def key_of(params: dict) -> str:
    return json.dumps(params, sort_keys=True, default=str)


# ── scoring ──────────────────────────────────────────────────────────────────────────────
_EDGES = [pd.Timestamp(x, tz="Asia/Kolkata").value for x in (T0, TRAIN_END, VALID_END, T_END)]
_TRAIN_YEARS = [
    pd.Timestamp(x, tz="Asia/Kolkata").value for x in (T0, "2022-11-08", "2023-11-08", TRAIN_END)
]


def period_stats(per_inst: dict[str, tuple], basket: str) -> dict:
    """Pooled and per-instrument figures per period from {instrument: (ts, ret, net)}."""
    out: dict = {}
    for i, name in enumerate(PERIODS):
        lo, hi = _EDGES[i], _EDGES[i + 1]
        rets, nets, inst_means, inst_counts = [], [], {}, {}
        for inst, (ts, ret, net) in per_inst.items():
            m = (ts >= lo) & (ts < hi)
            r, n = ret[m], net[m]
            rets.append(r)
            nets.append(n)
            inst_counts[inst] = int(m.sum())
            if m.sum() >= MIN_INSTRUMENT_TRADES:
                inst_means[inst] = float(r.mean())
        r = np.concatenate(rets) if rets else np.zeros(0)
        n_ = np.concatenate(nets) if nets else np.zeros(0)
        cnt = len(r)
        sd = float(r.std(ddof=1)) if cnt > 1 else float("nan")
        won, lost = float(n_[n_ > 0].sum()), float(-n_[n_ < 0].sum())
        traded = len(inst_means)
        out[name] = {
            "n": cnt,
            "win_rate": 100.0 * float((n_ > 0).mean()) if cnt else None,
            "avg_ret": float(r.mean()) if cnt else None,
            "t": float(r.mean() / sd * math.sqrt(cnt)) if cnt > 1 and sd > 0 else None,
            "pf": (won / lost) if lost > 0 else None,
            "net": float(n_.sum()),
            "breadth": (sum(v > 0 for v in inst_means.values()) / traded) if traded else None,
            "instruments_traded": traded,
            "per_instrument": {k: round(v, 4) for k, v in sorted(inst_means.items())},
            "counts": inst_counts,
        }
    # the three TRAIN years one by one: a set that only works in one market phase is fragile
    years = []
    for lo, hi in zip(_TRAIN_YEARS[:-1], _TRAIN_YEARS[1:], strict=True):
        r = [ret[(ts >= lo) & (ts < hi)] for ts, ret, _ in per_inst.values()]
        r = np.concatenate(r) if r else np.zeros(0)
        years.append({"n": len(r), "avg_ret": float(r.mean()) if len(r) else None})
    out["train"]["years"] = years
    out["train"]["years_positive"] = sum((y["avg_ret"] or 0) > 0 for y in years)
    return out


def train_score(st: dict, basket: str) -> float:
    """What the search climbs: the pooled TRAIN t-statistic, given enough trades. Breadth is
    not required here (a search that only sees losing sets must still be able to climb)."""
    tr = st["train"]
    if tr["n"] < MIN_TRADES[basket]["train"] or tr["t"] is None:
        return -math.inf
    return float(tr["t"])


def eligible(st: dict, basket: str) -> str | None:
    """None when a set may be picked, else why not: the hard rules of the protocol."""
    tr = st["train"]
    min_inst = 8 if basket == "STOCK" else 3
    if tr["n"] < MIN_TRADES[basket]["train"] or tr["t"] is None:
        return "too few TRAIN trades"
    if tr["instruments_traded"] < min_inst:
        return "traded too few instruments"
    if (tr["breadth"] or 0) < 0.5:
        return f"profitable on only {round(100 * (tr['breadth'] or 0))}% of instruments in TRAIN"
    if (tr["avg_ret"] or 0) <= 0:
        return "loses on average in TRAIN"
    if tr.get("years_positive", 3) < MIN_TRAIN_YEARS:
        return f"profitable in only {tr.get('years_positive')} of 3 TRAIN years"
    return None


def valid_ok(st: dict, basket: str) -> bool:
    va = st["valid"]
    return va["n"] >= MIN_TRADES[basket]["valid"] and (va["avg_ret"] or 0) > 0


# ── the search ───────────────────────────────────────────────────────────────────────────
class Search:
    def __init__(
        self, out: Path, workers: int, seed: int = 7, only: list[str] | None = None,
        timeframes: list[str] | None = None, baskets: list[str] | None = None,
        every_group: bool = False, warm: Path | None = None, warm_k: int = 6,
    ) -> None:  # fmt: skip
        self.only, self.timeframes, self.baskets = (
            only,
            timeframes or TIMEFRAMES,
            baskets or list(BASKETS),
        )
        self.out = out
        self.rng = random.Random(seed)
        self.pool = ProcessPoolExecutor(max_workers=workers, initializer=_init_worker)
        self.results: dict[tuple, dict[str, dict]] = {}  # group -> {params_key: record}
        self.log = (out / "evaluated.jsonl").open("a", encoding="utf-8")
        self.errors = 0
        self.every_group = every_group
        self.pick_costs: tuple[float, ...] = (1.0, 2.0)
        self.warm = self._warm_sets(warm, warm_k) if warm else {}

    @staticmethod
    def _warm_sets(run: Path, k: int) -> dict[tuple, list[dict]]:
        """Each group's best `k` TRAIN sets in an earlier run (ranked by TRAIN only)."""
        by: dict[tuple, list[dict]] = {}
        with (run / "evaluated.jsonl").open(encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                if not r.get("fine") and r.get("score") not in (None, -math.inf):
                    by.setdefault(tuple(r["group"]), []).append(r)
        return {
            g: [r["params"] for r in sorted(v, key=lambda r: r["score"], reverse=True)[:k]]
            for g, v in by.items()
        }

    def groups(self) -> list[tuple]:
        from onelot.strategies.registry import REGISTRY

        out = []
        for strategy in REGISTRY:
            if self.only and strategy not in self.only:
                continue
            for basket in self.baskets:
                if strategy == "gap_fade" and basket == "OPTIONS":
                    continue  # a stock's opening gap against NIFTY's: stocks only, as on the desk
                for tf in self.timeframes:
                    out.append((strategy, tf, basket))
        return out

    def evaluate(self, jobs: list[tuple[tuple, dict]], fine: bool = False) -> None:
        """jobs: [(group, params)] -> backtests on every instrument of the group's basket."""
        tasks, owner = [], {}
        for group, params in jobs:
            strategy, tf, basket = group
            pk = key_of(params)
            if not fine and pk in self.results.get(group, {}):
                continue
            for inst in BASKETS[basket]:
                task = (strategy, tf, pk, inst, fine)
                tasks.append(task)
                owner[task] = (group, pk)
        got: dict[tuple, dict[str, tuple]] = {}
        for task, arrays, err in self.pool.map(_run, tasks, chunksize=4):
            if err:
                self.errors += 1
                if self.errors <= 20:
                    print("  error", task[:4], err, flush=True)
                continue
            got.setdefault(owner[task], {})[task[3]] = arrays
        for (group, pk), per_inst in got.items():
            st = period_stats(per_inst, group[2])
            rec = {"group": list(group), "params": json.loads(pk), "fine": fine, "stats": st,
                   "score": train_score(st, group[2])}  # fmt: skip
            if not fine:
                self.results.setdefault(group, {})[pk] = rec
            self.log.write(json.dumps(rec, default=float) + "\n")
        self.log.flush()

    def best(self, group: tuple, k: int) -> list[dict]:
        recs = sorted(self.results.get(group, {}).values(), key=lambda r: r["score"], reverse=True)
        return [r for r in recs[:k] if r["score"] > -math.inf]

    def group_score(self, group: tuple) -> float:
        top = self.best(group, 1)
        return top[0]["score"] if top else -math.inf

    def run(self, r1: int, r2: int, r3: int) -> None:
        groups = self.groups()
        t0 = time.time()
        # round 1: every group - its defaults plus random sets
        jobs = []
        for g in groups:
            space = space_for(g[0])
            jobs.append((g, {}))
            jobs += [(g, dict(p)) for p in self.warm.get(g, [])]
            jobs += [(g, sample(space, self.rng)) for _ in range(r1 - 1)]
        print(f"round 1: {len(groups)} groups, {len(jobs)} sets", flush=True)
        self.evaluate(jobs)
        print(f"  done in {time.time() - t0:.0f}s", flush=True)
        # round 2: the better half - more random sets and neighbours of its best two
        ranked = sorted(groups, key=self.group_score, reverse=True)
        half = (
            ranked
            if self.every_group
            else [g for g in ranked[: max(1, len(ranked) // 2)] if self.group_score(g) > -math.inf]
        )
        jobs = []
        for g in half:
            space = space_for(g[0])
            jobs += [(g, sample(space, self.rng)) for _ in range(r2 // 2)]
            for rec in self.best(g, 2):
                jobs += [(g, neighbour(rec["params"], space, self.rng)) for _ in range(r2 // 4)]
        print(f"round 2: {len(half)} groups, {len(jobs)} sets", flush=True)
        self.evaluate(jobs)
        print(f"  done in {time.time() - t0:.0f}s", flush=True)
        # round 3: the best quarter - neighbours of its best four
        ranked = sorted(groups, key=self.group_score, reverse=True)
        quarter = [
            g
            for g in (ranked if self.every_group else ranked[: max(1, len(ranked) // 4)])
            if self.group_score(g) > -math.inf
        ]
        jobs = []
        for g in quarter:
            space = space_for(g[0])
            for rec in self.best(g, 4):
                jobs += [(g, neighbour(rec["params"], space, self.rng)) for _ in range(r3 // 4)]
        print(f"round 3: {len(quarter)} groups, {len(jobs)} sets", flush=True)
        self.evaluate(jobs)
        print(f"  done in {time.time() - t0:.0f}s", flush=True)

    def pick(self, k: int = 6) -> list[dict]:
        """Per group: among its best k TRAIN sets, the eligible one with the best VALID t-stat
        (VALID average above zero). A group where none qualifies reports its best TRAIN set
        and why it fails. Every pick is re-run with the 1-minute fill path; TEST is read now."""
        picks = []
        for g in self.results:
            top = self.best(g, k)
            if not top:
                continue
            ok = [
                r for r in top if eligible(r["stats"], g[2]) is None and valid_ok(r["stats"], g[2])
            ]
            if ok:
                chosen = max(ok, key=lambda r: r["stats"]["valid"]["t"] or -math.inf)
                status = "pass"
            else:
                chosen = top[0]
                why = eligible(chosen["stats"], g[2]) or "loses (or too few trades) in VALID"
                status = "fail: " + why
            picks.append(
                {"group": g, "params": chosen["params"], "search": chosen, "status": status}
            )
        passed = sum(p["status"] == "pass" for p in picks)
        print(
            f"{passed} of {len(picks)} group(s) pass; re-running every pick with 1-minute fills",
            flush=True,
        )
        tasks, owner = [], {}
        for p in picks:
            strategy, tf, basket = p["group"]
            pk = key_of(p["params"])
            for inst in BASKETS[basket]:
                for cost in self.pick_costs:
                    task = (strategy, tf, pk, inst, True, cost)
                    tasks.append(task)
                    owner[task] = (p["group"], pk, cost)
        got: dict[tuple, dict[str, tuple]] = {}
        for task, arrays, err in self.pool.map(_run, tasks, chunksize=2):
            if err:
                self.errors += 1
                continue
            got.setdefault(owner[task], {})[task[3]] = arrays
        out = []
        for p in picks:
            g, pk = p["group"], key_of(p["params"])
            st = period_stats(got.get((g, pk, 1.0), {}), g[2])
            st2 = period_stats(got.get((g, pk, 2.0), {}), g[2])
            st0 = period_stats(got.get((g, pk, 0.0), {}), g[2]) if 0.0 in self.pick_costs else None
            out.append({"group": list(g), "params": p["params"], "status": p["status"], "final": st,
                        "final_cost2": st2, "final_gross": st0, "search": p["search"]["stats"],
                        "train_score": p["search"]["score"]})  # fmt: skip
        return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/research/cross_retune")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--r1", type=int, default=16)
    ap.add_argument("--r2", type=int, default=24)
    ap.add_argument("--r3", type=int, default=24)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--only", nargs="*", help="strategy names (default: every Lab strategy)")
    ap.add_argument("--tfs", nargs="*", help=f"timeframes (default: {TIMEFRAMES})")
    ap.add_argument("--baskets", nargs="*", help="STOCK and/or OPTIONS (default: both)")
    ap.add_argument("--every-group", action="store_true", help="all three rounds for every group")
    ap.add_argument("--warm", type=Path, help="start from this earlier run's best TRAIN sets")
    ap.add_argument("--space", choices=["v1", "v2"], default="v1")
    ap.add_argument("--min-years", type=int, default=0, help="TRAIN years (of 3) in profit")
    ap.add_argument("--gross", action="store_true", help="also re-run picks before costs")
    a = ap.parse_args()
    global SPACE_VERSION, MIN_TRAIN_YEARS
    SPACE_VERSION, MIN_TRAIN_YEARS = a.space, a.min_years
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    logging.disable(logging.WARNING)
    (out / "args.json").write_text(json.dumps(vars(a), default=str, indent=1), encoding="utf-8")
    s = Search(out, a.workers, a.seed, a.only, a.tfs, a.baskets, a.every_group, a.warm)
    if a.gross:
        s.pick_costs = (0.0, 1.0, 2.0)
    t0 = time.time()
    s.run(a.r1, a.r2, a.r3)
    final = s.pick()
    (out / "final.json").write_text(json.dumps(final, indent=1, default=float), encoding="utf-8")
    print(f"done in {time.time() - t0:.0f}s; {s.errors} errors; results in {out}", flush=True)
    s.pool.shutdown()


if __name__ == "__main__":
    sys.exit(main())
