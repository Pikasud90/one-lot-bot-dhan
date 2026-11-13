"""Backtest of the owner's options rule of 2026-11-13 on five years of index bars.

Rule: Supertrend(14, 1.7) on 5m, 10m, 15m, 30m, 60m and 4h (the micro trend) and on 3m (the
trigger). Entry at a 3-minute close where all seven point the same way and did not at the
previous 3-minute close: a call when up, a put when down. Exit at the first 3-minute close
where at least 3 of the 7 point against the trade, or at the session's option exit (15:15).
One trade at a time per index, entries 09:30 to 14:45. Premiums are modelled (Black-76 at
India VIX x 1.15, monthly contract, the product's spread and charges), as in the earlier
research. Read-only on the store.

    PYTHONPATH=src:scripts:. .venv/bin/python -W ignore microtrend_backtest.py
"""

from __future__ import annotations

import numpy as np

import mtf_bruteforce as h

TFS = ("3m", "5m", "10m", "15m", "30m", "60m", "240m")
PERIOD = np.array([0, 0, 0, 1, 2])
NAMES = ("TUNE", "CHOOSE", "TEST")


def trades_of(p: h.Prep, need: int = 3) -> dict[str, np.ndarray]:
    D = p.D  # noqa: N806  (7, n): trend known at each one-minute bar's open
    n = p.n
    first = p.sess_first[p.sess]
    boundary = ((np.arange(n) - first) % 3 == 0) & (np.arange(n) > first)  # a 3m bar just closed
    up = (D == 1).all(axis=0)
    dn = (D == -1).all(axis=0)
    against_up = (D == -1).sum(axis=0)
    against_dn = (D == 1).sum(axis=0)
    idx = np.flatnonzero(boundary)
    E, X, S, R = [], [], [], []  # noqa: N806
    prev_up = prev_dn = False
    busy_until = -1
    for j in idx:
        a_up, a_dn = bool(up[j]), bool(dn[j])
        if j > busy_until and p.inwin[j]:
            side = 1 if (a_up and not prev_up) else (-1 if (a_dn and not prev_dn) else 0)
            if side:
                s = p.sess[j]
                xi = int(p.xi[s])
                if j < xi:
                    against = against_up if side == 1 else against_dn
                    k = j + 3
                    x, why = xi, 0
                    while k < xi:
                        if against[k] >= need:
                            x, why = k, 1
                            break
                        k += 3
                    E.append(j), X.append(x), S.append(side), R.append(why)
                    busy_until = x
        prev_up, prev_dn = a_up, a_dn
    return {
        k: np.array(v, dtype=np.int64) for k, v in zip("e x s r".split(), (E, X, S, R), strict=True)
    }


def main() -> None:
    h._init_worker()
    rows = []
    for inst in h.INDEX_OPTIONS:
        p = h.prepare(inst, tfs=TFS)
        tr = trades_of(p)
        ret = np.empty(len(tr["e"]))
        raw = np.empty(len(tr["e"]))
        for side in (1, -1):
            m = tr["s"] == side
            if not m.any():
                continue
            e, x = tr["e"][m], tr["x"][m]
            forced = x == p.xi[p.sess[e]]
            px = np.where(forced, p.xp[p.sess[e]], p.o[x])
            out = h.pnl(p, side, 0, e, x, px, p.ns[x])
            ret[m], raw[m] = out["ret"], side * (px / p.o[e] - 1.0) * 1e4
        yr = p.year[tr["e"]]
        ok = yr >= 0
        for i in np.flatnonzero(ok):
            rows.append(
                (
                    inst,
                    int(p.dayoff[tr["e"][i]]),
                    int(PERIOD[yr[i]]),
                    int(tr["s"][i]),
                    int(tr["r"][i]),
                    float(ret[i]),
                    float(raw[i]),
                    int(tr["x"][i] - tr["e"][i]),
                )
            )
        print(
            f"{inst:15s} trades {int(ok.sum()):5d}  mean {ret[ok].mean():+6.2f}%  index {raw[ok].mean():+5.1f} bp",
            flush=True,
        )
    a = np.array([(r[1], r[2], r[3], r[4], r[5], r[6], r[7]) for r in rows], dtype=float)
    day, per, side, why, ret, raw, mins = a.T

    def tday(v: np.ndarray, d: np.ndarray) -> float:
        u, inv = np.unique(d, return_inverse=True)
        s = np.bincount(inv, v)
        return float(s.mean() / (s.std(ddof=1) / np.sqrt(len(s)))) if len(s) > 1 else float("nan")

    def line(name: str, m: np.ndarray) -> str:
        if not m.any():
            return f"{name:26s} no trades"
        return (
            f"{name:26s} n={int(m.sum()):5d}  option {ret[m].mean():+6.2f}% (t {tday(ret[m], day[m]):+5.1f})  "
            f"index {raw[m].mean():+5.1f} bp  winners {(ret[m] > 0).mean() * 100:4.1f}%  held {mins[m].mean():5.0f} min"
        )

    print()
    print(line("ALL", np.ones(len(ret), bool)))
    for k, nm in enumerate(NAMES):
        print(line(nm, per == k))
    print(line("calls", side == 1))
    print(line("puts", side == -1))
    print(line("exit: 3 of 7 reversed", why == 1))
    print(line("exit: session end 15:15", why == 0))
    print(
        f"trades a year across five indices: {len(ret) / 5:.0f}; days with a trade: {len(np.unique(day))}"
    )


if __name__ == "__main__":
    main()
