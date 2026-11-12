"""Alpha search of 2026-11-12, step B4: the opening gap (PLAN.md B, A3), in detail.

The index's gain is made overnight and given back in the first half hour. This asks whether
fading the gap is a trade: after a gap of at least k of its own 60-session spreads, take the
side AGAINST the gap at 09:17 (put after a gap up, call after a gap down) and leave at a
fixed time. Index move in bp signed for the fade, and the modelled one-lot option result.

    PYTHONPATH=src:. .venv/bin/python -W ignore scripts/alpha_intraday4.py
"""

from __future__ import annotations

import alpha_lib as a
import numpy as np
from alpha_intraday1 import line, results

E = 2  # 09:17
EXITS = {"09:45": a.M("09:45"), "10:15": a.M("10:15"), "11:15": a.M("11:15"), "15:15": a.M("15:15")}


def gap_z(inst: a.Inst) -> np.ndarray:
    gap = inst.o / inst.pc - 1.0
    z = np.full(inst.n, np.nan)
    allv = np.flatnonzero(inst.valid)
    for j, d in enumerate(allv):
        if j >= 40:
            z[d] = gap[d] / gap[allv[max(0, j - 60) : j]].std()
    return z


def main() -> None:
    insts = {s: a.Inst(s) for s in a.INDICES}
    params = {s: a.opt_params(s, insts[s]) for s in a.INDICES}
    Z = {s: gap_z(i) for s, i in insts.items()}  # noqa: N806

    def fade(lo: float, hi: float, side_of_gap: int, xm: int, e: int = E) -> list[dict]:
        R = []  # noqa: N806
        for s, inst in insts.items():
            z = Z[s] * side_of_gap
            d = np.flatnonzero(inst.valid & (inst.year >= 0) & (z >= lo) & (z < hi))
            tr = {
                "d": d,
                "e": np.full(len(d), e),
                "s": np.full(len(d), -side_of_gap),
                "x": np.full(len(d), xm),
            }
            R.append(results(inst, params[s], tr))
        return R

    for side, nm in (
        (1, "GAP UP -> buy the put at 09:17"),
        (-1, "GAP DOWN -> buy the call at 09:17"),
    ):
        print(f"\n== {nm} ==")
        for lo, hi in ((0.5, 1.0), (1.0, 1.5), (1.5, 2.5), (2.5, 99.0), (1.0, 99.0)):
            for xn, xm in EXITS.items():
                print(line(f"|z| {lo:.1f}-{hi:.1f} out {xn}", fade(lo, hi, side, xm)))
    print("\n== Both sides, |z| >= 1.0, out 10:15: by index, and with a later entry ==")
    for s in a.INDICES:
        R = []  # noqa: N806
        for side in (1, -1):
            z = Z[s] * side
            inst = insts[s]
            d = np.flatnonzero(inst.valid & (inst.year >= 0) & (z >= 1.0))
            tr = {
                "d": d,
                "e": np.full(len(d), E),
                "s": np.full(len(d), -side),
                "x": np.full(len(d), a.M("10:15")),
            }
            R.append(results(inst, params[s], tr))
        print(line(f"  {s}", R))
    for e, en in ((1, "09:16"), (2, "09:17"), (5, "09:20"), (10, "09:25"), (15, "09:30")):
        print(
            line(
                f"  entry {en}",
                fade(1.0, 99.0, 1, a.M("10:15"), e) + fade(1.0, 99.0, -1, a.M("10:15"), e),
            )
        )


if __name__ == "__main__":
    main()
