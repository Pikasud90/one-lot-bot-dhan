"""One row per strategy and basket: the best timeframe across cross_retune runs.

The best timeframe is chosen WITHOUT looking at TEST: a set that passed the protocol first,
then one that failed only in VALID, then the rest; ties by the VALID t-statistic. When the
same group appears in several runs, the later run (given later) replaces the earlier one.

Usage:
  .venv/bin/python scripts/cross_retune_best.py --out docs/research/best.md \
      data/research/cross_retune_2026-11-07 data/research/cross_retune_v2_options ...
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

WEEKS = {"train": 52 * 3, "valid": 52, "test": 52}
BASKET_NAMES = {"STOCK": "Stocks (18, one share, intraday)", "OPTIONS": "Index options (5)"}


def fmt(v, d=2, pct=False, sign=False):
    if v is None:
        return "–"
    return f"{v:{'+' if sign else ''}.{d}f}{'%' if pct else ''}"


def verdict(rec: dict) -> tuple[int, str]:
    """(rank, words): lower rank = closer to a usable setting. TEST is only described."""
    status = rec["status"]
    test = rec["final"]["test"]
    if status == "pass":
        if (test["avg_ret"] or 0) > 0:
            return 0, "PASSED all three periods"
        return 1, "passed tuning + choosing, LOST in the test year"
    why = status.removeprefix("fail: ")
    if why.startswith("loses (or too few trades) in VALID"):
        return 2, "lost in the choosing year"
    if why.startswith("profitable in only"):
        return 3, why.replace("TRAIN years", "tuning years")
    if why.startswith("profitable on only"):
        return 4, why.replace("in TRAIN", "in the tuning years")
    if why.startswith("loses on average in TRAIN"):
        return 5, "loses in the tuning years"
    return 6, why


def load(runs: list[Path]) -> tuple[dict[tuple, dict], dict[tuple, int]]:
    """The picks per group (a later run replaces an earlier one) and, for every group a run
    searched, the most TRAIN trades any of its sets made (a group with no pick never had
    enough trades to be judged)."""
    groups: dict[tuple, dict] = {}
    most: dict[tuple, int] = {}
    for run in runs:
        for rec in json.loads((run / "final.json").read_text(encoding="utf-8")):
            groups[tuple(rec["group"])] = rec | {"run": run.name}
        seen: dict[tuple, int] = {}
        with (run / "evaluated.jsonl").open(encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                g = tuple(r["group"])
                seen[g] = max(seen.get(g, 0), int(r["stats"]["train"]["n"]))
        most.update(seen)
    return groups, most


def unjudged(groups: dict[tuple, dict], most: dict[tuple, int]) -> dict[str, list[str]]:
    """Per basket, the strategies with no pick on any timeframe, with their best trade count."""
    picked = {(g[0], g[2]) for g in groups}
    out: dict[str, dict[str, int]] = {}
    for (strategy, _tf, basket), n in most.items():
        if (strategy, basket) not in picked:
            cur = out.setdefault(basket, {})
            cur[strategy] = max(cur.get(strategy, 0), n)
    return {
        b: [
            f"{s} (at most {n} trades in three years across the basket)"
            for s, n in sorted(v.items())
        ]
        for b, v in out.items()
    }


def best_per_strategy(groups: dict[tuple, dict]) -> dict[str, list[dict]]:
    by: dict[tuple, list[dict]] = {}
    for (strategy, _tf, basket), rec in groups.items():
        by.setdefault((strategy, basket), []).append(rec)
    out: dict[str, list[dict]] = {}
    for (_strategy, basket), recs in sorted(by.items()):
        # every timeframe that passed is shown; otherwise the one closest to passing
        passed = [r for r in recs if verdict(r)[0] <= 1]
        chosen = passed or [
            min(recs, key=lambda r: (verdict(r)[0], -(r["final"]["valid"]["t"] or -99)))
        ]
        out.setdefault(basket, []).extend(chosen)
    for basket in out:
        out[basket].sort(key=lambda r: (verdict(r)[0], -(r["final"]["valid"]["t"] or -99)))
    return out


def describe(params: dict) -> str:
    """The knobs an owner reads first, in words; the full set follows in JSON."""
    bits = []
    d = params.get("direction", "both")
    bits.append({"long": "long only", "short": "short only"}.get(d, "long and short"))
    if "entry_start" in params or "entry_end" in params:
        bits.append(
            f"entries {params.get('entry_start', '09:30')}-{params.get('entry_end', '14:45')}"
        )
    if params.get("min_strength"):
        bits.append(f"strength >= {params['min_strength']}")
    if params.get("max_trades_per_day"):
        bits.append(f"max {params['max_trades_per_day']}/day")
    if params.get("target_r"):
        bits.append(f"target {params['target_r']}R")
    if params.get("trail_atr_mult"):
        bits.append(f"trail {params['trail_atr_mult']} ATR")
    if params.get("max_bars"):
        bits.append(f"time stop {params['max_bars']} bars")
    if params.get("partial_r"):
        bits.append(f"half off at {params['partial_r']}R")
    return ", ".join(bits)


def table(rows: list[dict]) -> list[str]:
    lines = [
        "| Strategy | Best TF | Verdict | Tuning Nov 21-Nov 24: avg · win% | "
        "Choosing Nov 24-Nov 25: avg · win% | Test Nov 25-Nov 26: avg · win% · trades | "
        "Test at 2x costs | Trades/week (all) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        f = r["final"]
        tr, va, te = f["train"], f["valid"], f["test"]
        c2 = (r.get("final_cost2") or {}).get("test", {})
        lines.append(
            f"| {r['group'][0]} | {r['group'][1]} | {verdict(r)[1]} | "
            f"{fmt(tr['avg_ret'], 2, True, True)} · {fmt(tr['win_rate'], 0, True)} | "
            f"{fmt(va['avg_ret'], 2, True, True)} · {fmt(va['win_rate'], 0, True)} | "
            f"{fmt(te['avg_ret'], 2, True, True)} · {fmt(te['win_rate'], 0, True)} · {te['n']} | "
            f"{fmt(c2.get('avg_ret'), 2, True, True)} | {fmt(tr['n'] / WEEKS['train'], 1)} |"
        )
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    groups, most = load(a.runs)
    best = best_per_strategy(groups)
    never = unjudged(groups, most)
    lines: list[str] = []
    for basket in ("STOCK", "OPTIONS"):
        if basket not in best:
            continue
        lines += [f"### {BASKET_NAMES[basket]}", "", *table(best[basket]), ""]
        if never.get(basket):
            lines += ["Too few trades to judge: " + "; ".join(never[basket]) + ".", ""]
    lines += ["### The settings", ""]
    for basket in ("STOCK", "OPTIONS"):
        for r in best.get(basket, []):
            g = r["group"]
            lines.append(
                f"- **{g[0]} {g[1]} {g[2]}** ({describe(r['params'])}): "
                f"`{json.dumps(r['params'], sort_keys=True)}`"
            )
    text = "\n".join(lines)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
