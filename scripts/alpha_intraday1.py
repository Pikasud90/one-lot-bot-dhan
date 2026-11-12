"""Alpha search of 2026-11-12, step B1: does the index itself show intraday predictability?

A1 noise-area momentum, A2 first half hour -> last half hour, A3 the opening gap (PLAN.md,
section B). Underlying results in basis points of the index (signed by the trade's side) and
the modelled one-lot option result in % of the premium paid, after costs.

    PYTHONPATH=src:. .venv/bin/python -W ignore scripts/alpha_intraday1.py
"""

from __future__ import annotations

import alpha_lib as a
import numpy as np

CHECKS = np.arange(a.M("09:45"), a.M("13:30") + 1, 15)
X = a.M("15:15")
LATE = np.arange(a.M("09:45"), a.M("15:00") + 1, 15)


def sigma14(inst: a.Inst) -> np.ndarray:
    mv = np.abs(inst.C / inst.o[:, None] - 1.0)
    sig = np.full_like(mv, np.nan)
    idx = np.flatnonzero(inst.full)
    for j, i in enumerate(idx):
        if j >= 14:
            sig[i] = mv[idx[j - 14 : j]].mean(axis=0)
    return sig


def a1_trades(inst: a.Inst, sig: np.ndarray, k: float, k_exit: float | None) -> dict:
    """First checkpoint where the move beyond the reference is >= k noise units."""
    up = np.maximum(inst.o, inst.pc)[:, None]
    dn = np.minimum(inst.o, inst.pc)[:, None]
    zl = (inst.C / up - 1.0) / sig
    zs = (inst.C / dn - 1.0) / sig
    ok = inst.valid & (inst.year >= 0) & np.isfinite(sig[:, CHECKS[0]])
    D, E, S, XM = [], [], [], []  # noqa: N806
    for d in np.flatnonzero(ok):
        for tau in CHECKS:
            side = 1 if zl[d, tau] >= k else (-1 if zs[d, tau] <= -k else 0)
            if side:
                xm = X
                if k_exit is not None:
                    z = zl[d] if side == 1 else -zs[d]
                    for t2 in LATE[LATE > tau]:
                        if z[t2] < k_exit:
                            xm = t2 + 1
                            break
                D.append(d), E.append(tau + 1), S.append(side), XM.append(xm)
                break
    return {
        k_: np.array(v, dtype=np.int64)
        for k_, v in zip("d e s x".split(), (D, E, S, XM), strict=False)
    }


def results(inst: a.Inst, P: dict, tr: dict) -> dict:  # noqa: N803
    d, e, s, x = tr["d"], tr["e"], tr["s"], tr["x"]
    und = s * (inst.C[d, x] / inst.C[d, e] - 1.0) * 1e4
    opt = np.empty(len(d))
    for side in (1, -1):
        m = s == side
        if m.any():
            opt[m] = a.opt_ret(inst, P, side, d[m], e[m], x[m])["ret"]
    return {"und": und, "opt": opt, "day": inst.day[d], "per": inst.period[d], "side": s, "e": e}


def line(name: str, R: list[dict]) -> str:  # noqa: N803
    cat = {k: np.concatenate([r[k] for r in R]) for k in R[0]}
    n = len(cat["und"])
    hit = (cat["und"] > 0).mean() * 100 if n else float("nan")
    out = f"{name:34s} n={n:5d} und {a.fmt(cat['und'], cat['day'], nd=1)}bp hit {hit:4.1f}% | opt {a.fmt(cat['opt'], cat['day'])}% |"
    for p in range(3):
        m = cat["per"] == p
        out += f" {a.PERIODS[p][:2]} {cat['und'][m].mean():+6.1f}bp/{cat['opt'][m].mean():+6.2f}%"
    return out


def main() -> None:
    insts = {s: a.Inst(s) for s in a.INDICES}
    params = {s: a.opt_params(s, insts[s]) for s in a.INDICES}
    sigs = {s: sigma14(insts[s]) for s in a.INDICES}
    print(
        "Sessions used (valid, in the research years):",
        {s: int((i.valid & (i.year >= 0)).sum()) for s, i in insts.items()},
    )
    P0 = params["NIFTY"]  # noqa: N806
    print(
        "option model: delta",
        P0["delta"],
        "half-spread",
        P0["half"],
        "| lots",
        {s: int(params[s]["lot"]) for s in a.INDICES},
    )

    print(
        "\n== Baseline: one trade every session, 09:46 -> 15:15 (modelled option, after costs) =="
    )
    for side, nm in ((1, "call"), (-1, "put")):
        R = []  # noqa: N806
        for s, inst in insts.items():
            d = np.flatnonzero(inst.valid & (inst.year >= 0))
            tr = {
                "d": d,
                "e": np.full(len(d), 31),
                "s": np.full(len(d), side),
                "x": np.full(len(d), X),
            }
            R.append(results(inst, params[s], tr))
        print(line(f"buy the {nm} every day", R))

    print("\n== A1 noise-area momentum: first checkpoint 09:45..13:30 with |z| >= k ==")
    keep = {}
    for k in (1.0, 1.5, 2.0):
        for kx, xn in (
            (None, "hold to 15:15"),
            (k, "exit back inside"),
            (0.0, "exit at reference"),
        ):
            R = [
                results(insts[s], params[s], a1_trades(insts[s], sigs[s], k, kx)) for s in a.INDICES
            ]  # noqa: N806
            keep[(k, xn)] = R
            print(line(f"k={k:.1f} {xn}", R))
    print("\n-- k=1.0, hold to 15:15, by index / side / entry time --")
    R = keep[(1.0, "hold to 15:15")]  # noqa: N806
    for s, r in zip(a.INDICES, R, strict=False):
        print(line(f"  {s}", [r]))
    for nm, f in (
        ("  calls (long)", lambda r: r["side"] == 1), ("  puts (short)", lambda r: r["side"] == -1),
        ("  entry by 10:16", lambda r: r["e"] <= a.M("10:16")),
        ("  entry 10:31..11:31", lambda r: (r["e"] > a.M("10:16")) & (r["e"] <= a.M("11:31"))),
        ("  entry after 11:31", lambda r: r["e"] > a.M("11:31")),
    ):  # fmt: skip
        print(line(nm, [{k: v[f(r)] for k, v in r.items()} for r in R]))

    print("\n== A2 first half hour -> last half hour: in at 15:00, out at 15:25 ==")
    e2, x2 = a.M("15:00"), a.M("15:25")
    for thr in (0.0, 0.002, 0.004):
        R = []  # noqa: N806
        for s, inst in insts.items():
            ok = inst.valid & (inst.year >= 0)
            r30 = inst.C[:, a.M("09:45")] / inst.pc - 1.0
            d = np.flatnonzero(ok & (np.abs(r30) >= thr) & (r30 != 0))
            tr = {
                "d": d,
                "e": np.full(len(d), e2),
                "s": np.sign(r30[d]).astype(np.int64),
                "x": np.full(len(d), x2),
            }
            R.append(results(inst, params[s], tr))
        print(line(f"|first half hour| >= {thr * 100:.1f}%", R))

    print("\n== A3 the opening gap, in units of its own 60-session spread: index move in bp ==")
    print(
        "bucket          days   open->15:15      09:45->15:15     10:15->15:15   (unsigned index move; t by day)"
    )
    G = {b: [] for b in range(5)}  # noqa: N806
    for inst in insts.values():
        gap = inst.o / inst.pc - 1.0
        ok = np.flatnonzero(inst.valid & (inst.year >= 0))
        allv = np.flatnonzero(inst.valid)
        for d in ok:
            past = allv[allv < d][-60:]
            if len(past) < 40:
                continue
            z = gap[d] / gap[past].std()
            b = int(np.searchsorted([-1.5, -0.5, 0.5, 1.5], z))
            G[b].append(
                (
                    inst.day[d],
                    inst.C[d, X] / inst.o[d] - 1,
                    inst.C[d, X] / inst.C[d, 30] - 1,
                    inst.C[d, X] / inst.C[d, 60] - 1,
                )
            )
    for b, nm in enumerate(("z < -1.5", "-1.5..-0.5", "-0.5..0.5", "0.5..1.5", "z > 1.5")):
        g = np.array(G[b])
        print(
            f"{nm:12s} {len(g):6d}   "
            + "   ".join(a.fmt(g[:, c] * 1e4, g[:, 0], nd=1) for c in (1, 2, 3))
        )


if __name__ == "__main__":
    main()
