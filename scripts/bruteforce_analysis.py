"""Read-only analysis of the saved brute-force (cross-retune) results.

Answers "what worked where": per strategy x timeframe x basket, how many of the evaluated
settings were profitable (after costs) in each of the three periods, and which settings held
up in all three. Nothing is re-run; this only reads `data/research/cross_retune_*/evaluated.jsonl`.

    .venv/bin/python scripts/bruteforce_analysis.py > data/research/mtf_2026-11-11/bruteforce_analysis.txt
"""

from __future__ import annotations

import collections
import json
import statistics as st
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = {
    "v1 (both baskets)": ROOT / "data/research/cross_retune_2026-11-07",
    "v2 options": ROOT / "data/research/cross_retune_v2_options",
    "v2 stocks": ROOT / "data/research/cross_retune_v2_stocks",
}
PERIODS = ("train", "valid", "test")


def load() -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for run, path in RUNS.items():
        for line in (path / "evaluated.jsonl").open():
            r = json.loads(line)
            key = json.dumps([r["group"], r["params"]], sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            r["run"] = run
            rows.append(r)
    return rows


def ok(r: dict, min_n: int) -> bool:
    return all(r["stats"][p]["n"] >= min_n for p in PERIODS)


def direction(r: dict) -> str:
    p = r["params"]
    if "direction" in p:
        return str(p["direction"])
    al, as_ = p.get("allow_long", True), p.get("allow_short", True)
    return "both" if al and as_ else ("long" if al else "short")


def main() -> None:
    rows = load()
    print(f"unique settings evaluated: {len(rows)}")
    by_group: dict[tuple, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_group[tuple(r["group"])].append(r)

    print("\n== A. Per strategy x timeframe x basket (after costs; avg_ret = % per trade) ==")
    print(
        f"{'strategy':18} {'tf':4} {'basket':8} {'sets':>4} | {'%pos TUNE':>9} {'%pos CHOOSE':>11} {'%pos TEST':>9} "
        f"{'all3':>4} | {'med TUNE':>8} {'med CHO':>8} {'med TEST':>8} | {'best min3':>9} {'n(test)':>7}"
    )
    summary = []
    for g, rs in sorted(by_group.items(), key=lambda kv: (kv[0][2], kv[0][0], kv[0][1])):
        min_n = 20 if g[2] == "OPTIONS" else 60
        use = [r for r in rs if ok(r, min_n)]
        if not use:
            continue
        pos = {p: sum(r["stats"][p]["avg_ret"] > 0 for r in use) / len(use) * 100 for p in PERIODS}
        all3 = sum(all(r["stats"][p]["avg_ret"] > 0 for p in PERIODS) for r in use)
        med = {p: st.median(r["stats"][p]["avg_ret"] for r in use) for p in PERIODS}
        best = max(use, key=lambda r: min(r["stats"][p]["avg_ret"] for p in PERIODS))
        bmin = min(best["stats"][p]["avg_ret"] for p in PERIODS)
        summary.append((g, len(use), pos, all3, med, bmin, best))
        print(
            f"{g[0]:18} {g[1]:4} {g[2]:8} {len(use):4d} | {pos['train']:9.0f} {pos['valid']:11.0f} {pos['test']:9.0f} "
            f"{all3:4d} | {med['train']:8.3f} {med['valid']:8.3f} {med['test']:8.3f} | {bmin:9.3f} {best['stats']['test']['n']:7d}"
        )

    print(
        "\n== B. Settings positive after costs in ALL three periods (with a usable trade count) =="
    )
    winners = []
    for r in rows:
        g = tuple(r["group"])
        min_n = 20 if g[2] == "OPTIONS" else 60
        if ok(r, min_n) and all(r["stats"][p]["avg_ret"] > 0 for p in PERIODS):
            winners.append(r)
    winners.sort(key=lambda r: -min(r["stats"][p]["t"] for p in PERIODS))
    print(f"count: {len(winners)} of {len(rows)}")
    for r in winners[:40]:
        s = r["stats"]
        print(
            f"  {r['group'][0]:18} {r['group'][1]:4} {r['group'][2]:8} dir={direction(r):5} "
            + " ".join(
                f"{p[:2].upper()} n={s[p]['n']:4d} avg={s[p]['avg_ret']:+.3f} t={s[p]['t']:+.2f} br={s[p]['breadth']:.2f}"
                for p in PERIODS
            )
        )

    print(
        "\n== C. Direction (long / short / both) by basket: share of settings positive per period =="
    )
    d: dict[tuple, list[dict]] = collections.defaultdict(list)
    for r in rows:
        g = tuple(r["group"])
        min_n = 20 if g[2] == "OPTIONS" else 60
        if ok(r, min_n):
            d[(g[2], direction(r))].append(r)
    for k, rs in sorted(d.items()):
        pos = {p: sum(r["stats"][p]["avg_ret"] > 0 for r in rs) / len(rs) * 100 for p in PERIODS}
        med = {p: st.median(r["stats"][p]["avg_ret"] for r in rs) for p in PERIODS}
        print(
            f"  {k[0]:8} {k[1]:5} sets={len(rs):4d}  %pos TUNE/CHOOSE/TEST = {pos['train']:.0f}/{pos['valid']:.0f}/{pos['test']:.0f}"
            f"   median avg = {med['train']:+.3f}/{med['valid']:+.3f}/{med['test']:+.3f}"
        )

    print("\n== D. Timeframe by basket ==")
    d = collections.defaultdict(list)
    for r in rows:
        g = tuple(r["group"])
        min_n = 20 if g[2] == "OPTIONS" else 60
        if ok(r, min_n):
            d[(g[2], g[1])].append(r)
    for k, rs in sorted(d.items()):
        pos = {p: sum(r["stats"][p]["avg_ret"] > 0 for r in rs) / len(rs) * 100 for p in PERIODS}
        med = {p: st.median(r["stats"][p]["avg_ret"] for r in rs) for p in PERIODS}
        print(
            f"  {k[0]:8} {k[1]:4} sets={len(rs):4d}  %pos = {pos['train']:.0f}/{pos['valid']:.0f}/{pos['test']:.0f}"
            f"   median avg = {med['train']:+.3f}/{med['valid']:+.3f}/{med['test']:+.3f}"
        )

    print(
        "\n== E. Instruments: share of settings in which the instrument was profitable (per period) =="
    )
    inst: dict[tuple, list[float]] = collections.defaultdict(list)
    for r in rows:
        g = tuple(r["group"])
        min_n = 20 if g[2] == "OPTIONS" else 60
        if not ok(r, min_n):
            continue
        for p in PERIODS:
            for k, v in r["stats"][p]["per_instrument"].items():
                inst[(g[2], k, p)].append(v)
    names = sorted({(b, k) for (b, k, _p) in inst})
    for b, k in names:
        cells = []
        for p in PERIODS:
            v = inst[(b, k, p)]
            cells.append(f"{sum(x > 0 for x in v) / len(v) * 100:3.0f}% (med {st.median(v):+.3f})")
        print(f"  {b:8} {k:16} " + "  ".join(cells))

    print(
        "\n== F. Supertrend families: parameter values vs outcome (median avg_ret, share positive in TEST) =="
    )
    for strat in ("supertrend_vol", "supertrend_adx"):
        for basket in ("OPTIONS", "STOCK"):
            rs = [
                r
                for r in rows
                if r["group"][0] == strat
                and r["group"][2] == basket
                and ok(r, 20 if basket == "OPTIONS" else 60)
            ]
            print(f"  -- {strat} / {basket}: {len(rs)} settings")
            for key in (
                "st_period",
                "st_mult",
                "exit_on_flip",
                "target_r",
                "trail_atr_mult",
                "direction",
                "adx_min",
                "max_trades_per_day",
            ):
                buckets: dict[str, list[dict]] = collections.defaultdict(list)
                for r in rs:
                    if key in r["params"]:
                        buckets[str(r["params"][key])].append(r)
                if len(buckets) < 2:
                    continue
                parts = []
                for val, br in sorted(buckets.items()):
                    m = [st.median(x["stats"][p]["avg_ret"] for x in br) for p in PERIODS]
                    parts.append(f"{val}: n={len(br)} med={m[0]:+.2f}/{m[1]:+.2f}/{m[2]:+.2f}")
                print(f"     {key:18} " + " | ".join(parts))


if __name__ == "__main__":
    main()
