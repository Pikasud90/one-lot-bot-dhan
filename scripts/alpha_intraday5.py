"""Alpha search of 2026-11-12, step B5: does yesterday's move reverse in today's session?

Stocks gain overnight after an up day and give it back in the next session. Here: the five
indices. After a close-to-close move of at least k of its own 60-session spreads, take the
side AGAINST it at 09:20 the next session (put after an up day, call after a down day).

    PYTHONPATH=src:. .venv/bin/python -W ignore scripts/alpha_intraday5.py
"""

from __future__ import annotations

import alpha_lib as a
import numpy as np
from alpha_intraday1 import line, results

E = 5  # 09:20


def prev_z(inst: a.Inst) -> np.ndarray:
    close = inst.C[:, -1]
    cc = close / inst.pc - 1.0  # that session's close-to-close move
    z = np.full(inst.n, np.nan)
    allv = np.flatnonzero(inst.valid)
    for j, d in enumerate(allv):
        if j >= 41:
            y = allv[j - 1]
            z[d] = cc[y] / cc[allv[max(0, j - 61) : j - 1]].std()  # yesterday's move in past units
    return z


def main() -> None:
    insts = {s: a.Inst(s) for s in a.INDICES}
    params = {s: a.opt_params(s, insts[s]) for s in a.INDICES}
    Z = {s: prev_z(i) for s, i in insts.items()}  # noqa: N806
    G = {s: np.sign(i.o / i.pc - 1.0) for s, i in insts.items()}  # noqa: N806

    def fade(lo: float, hi: float, side: int, xm: int, gap: int = 0) -> list[dict]:
        R = []  # noqa: N806
        for s, inst in insts.items():
            z = Z[s] * side
            m = inst.valid & (inst.year >= 0) & (z >= lo) & (z < hi)
            if gap:
                m &= G[s] == gap * side  # gap with (+1) or against (-1) yesterday's move
            d = np.flatnonzero(m)
            tr = {
                "d": d,
                "e": np.full(len(d), E),
                "s": np.full(len(d), -side),
                "x": np.full(len(d), xm),
            }
            R.append(results(inst, params[s], tr))
        return R

    for side, nm in (
        (1, "AFTER AN UP DAY -> buy the put at 09:20"),
        (-1, "AFTER A DOWN DAY -> buy the call at 09:20"),
    ):
        print(f"\n== {nm} ==")
        for lo, hi in ((0.0, 0.75), (0.75, 1.5), (1.5, 99.0)):
            for xn in ("10:15", "12:15", "15:15"):
                print(line(f"z {lo:.2f}-{hi:.2f} out {xn}", fade(lo, hi, side, a.M(xn))))
        for g, gn in ((1, "gap with it"), (-1, "gap against it")):
            print(line(f"z >= 0.75, {gn}, out 15:15", fade(0.75, 99.0, side, a.M("15:15"), g)))


if __name__ == "__main__":
    main()
