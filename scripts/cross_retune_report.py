"""Summarise a cross_retune run: one row per strategy x timeframe x basket.

Usage: .venv/bin/python scripts/cross_retune_report.py data/research/cross_retune_2026-11-07
Writes report.md beside the run and prints the table.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

WEEKS = {"train": 52 * 3, "valid": 52, "test": 52}


def fmt(v, d=2, pct=False):
    if v is None:
        return "–"
    return f"{v:.{d}f}{'%' if pct else ''}"


def row(rec: dict) -> dict:
    g = rec["group"]
    f = rec["final"]
    n_inst = len(f["train"]["counts"]) or 1
    out = {
        "strategy": g[0],
        "tf": g[1],
        "basket": g[2],
        "status": rec["status"],
        "params": rec["params"],
    }
    for p in ("train", "valid", "test"):
        s = f[p]
        out[p] = {
            "n": s["n"],
            "per_week_each": s["n"] / WEEKS[p] / n_inst,
            "wr": s["win_rate"],
            "avg": s["avg_ret"],
            "pf": s["pf"],
            "t": s["t"],
            "breadth": s["breadth"],
        }
    out["years_positive"] = f["train"].get("years_positive")
    c2 = rec.get("final_cost2")
    out["test_cost2"] = c2["test"]["avg_ret"] if c2 else None
    out["test_breadth"] = f["test"]["breadth"]
    g0 = rec.get("final_gross")
    out["gross"] = (g0["train"]["avg_ret"], g0["test"]["avg_ret"]) if g0 else None
    return out


def defaults(run: Path) -> dict:
    """The defaults' figures per group (the first set each group evaluated)."""
    out = {}
    with (run / "evaluated.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            if r.get("params") == {} and not r.get("fine") and tuple(r["group"]) not in out:
                out[tuple(r["group"])] = r["stats"]
    return out


def main() -> None:
    run = Path(sys.argv[1])
    final = json.loads((run / "final.json").read_text(encoding="utf-8"))
    base = defaults(run)
    rows = sorted(
        (row(r) for r in final),
        key=lambda x: (x["status"] != "pass", -(x["valid"]["t"] or -99)),
    )
    lines = [
        "| Strategy | TF | Basket | Status | TRAIN avg/trade · WR · t · breadth · years+ | VALID avg · WR · t | TEST avg · WR · PF · trades · breadth | TEST at 2x costs | Trades/week each | Defaults TRAIN avg · t |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for x in rows:
        tr, va, te = x["train"], x["valid"], x["test"]
        d = base.get((x["strategy"], x["tf"], x["basket"]), {}).get("train", {})
        lines.append(
            f"| {x['strategy']} | {x['tf']} | {x['basket']} | {x['status']} | "
            f"{fmt(tr['avg'], 3, True)} · {fmt(tr['wr'], 1, True)} · {fmt(tr['t'])} · {fmt((tr['breadth'] or 0) * 100, 0, True)} · {x['years_positive'] if x['years_positive'] is not None else '–'}/3 | "
            f"{fmt(va['avg'], 3, True)} · {fmt(va['wr'], 1, True)} · {fmt(va['t'])} | "
            f"{fmt(te['avg'], 3, True)} · {fmt(te['wr'], 1, True)} · {fmt(te['pf'])} · {te['n']} · {fmt((x['test_breadth'] or 0) * 100, 0, True)} | "
            f"{fmt(x['test_cost2'], 3, True)} | "
            f"{fmt(tr['per_week_each'], 2)} | {fmt(d.get('avg_ret'), 3, True)} · {fmt(d.get('t'))} |"
        )
    if any(x["gross"] for x in rows):
        lines += ["", "Before costs and slippage (same picks): TRAIN avg · TEST avg per trade", ""]
        lines += [
            f"- {x['strategy']} {x['tf']} {x['basket']}: {fmt(x['gross'][0], 3, True)} · "
            f"{fmt(x['gross'][1], 3, True)}"
            for x in rows
            if x["gross"]
        ]
    passed = [x for x in rows if x["status"] == "pass"]
    lines += ["", f"{len(passed)} of {len(rows)} groups pass the protocol.", ""]
    for x in passed:
        lines.append(
            f"- {x['strategy']} {x['tf']} {x['basket']}: {json.dumps(x['params'], sort_keys=True)}"
        )
    text = "\n".join(lines)
    (run / "report.md").write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
