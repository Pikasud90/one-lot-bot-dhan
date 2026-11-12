"""Alpha search of 2026-11-12, step B3: is there a trend-day score? (PLAN.md B, A4 and A5)

Takes the A1 trades (k = 1.0, hold to 15:15, five indices) and asks whether features known at
the entry minute sort them: yesterday's range against its 20-session mean (compression), the
relative volume and breadth of the 18 stocks (big-money participation), VIX against its own
250-session median, how many indices are beyond the noise area, the strength and the time.
Dose-response tables, then a linear score fitted on TUNE ONLY and read on CHOOSE and TEST.

    PYTHONPATH=src:. .venv/bin/python -W ignore scripts/alpha_intraday3.py
"""

from __future__ import annotations

import alpha_lib as a
import mtf_bruteforce as h
import numpy as np
from alpha_intraday1 import a1_trades, results, sigma14

K = 1.0


def market_feats(days: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Equal-weighted over the 18 stocks, per (session of `days`, minute): relative cumulative
    volume (vs the mean of that stock's previous 20 sessions) and the mean sign of the move
    from the open."""
    rv_sum = np.zeros((len(days), a.NMIN))
    rv_n = np.zeros((len(days), a.NMIN))
    br_sum = np.zeros((len(days), a.NMIN))
    br_n = np.zeros((len(days), a.NMIN))
    for inst_name in h.STOCKS:
        s = a.Inst(inst_name)
        pos = {int(d): i for i, d in enumerate(s.day)}
        rows = np.array([pos.get(int(d), -1) for d in days])
        cumv = np.cumsum(s.V, axis=1)
        mean20 = np.full_like(cumv, np.nan)
        full = np.flatnonzero(s.full)
        for j, i in enumerate(full):
            if j >= 20:
                mean20[i] = cumv[full[j - 20 : j]].mean(axis=0)
        rvol = cumv / mean20
        mv = np.sign(s.C / s.o[:, None] - 1.0)
        ok = rows >= 0
        r = np.full((len(days), a.NMIN), np.nan)
        r[ok] = rvol[rows[ok]]
        good = np.isfinite(r)
        rv_sum += np.where(good, r, 0.0)
        rv_n += good
        m = np.full((len(days), a.NMIN), np.nan)
        m[ok] = mv[rows[ok]]
        good = np.isfinite(m)
        br_sum += np.where(good, m, 0.0)
        br_n += good
    return rv_sum / np.where(rv_n > 0, rv_n, np.nan), br_sum / np.where(br_n > 0, br_n, np.nan)


def table(title: str, key: np.ndarray, edges: list[float], names: list[str], T: dict) -> None:  # noqa: N803
    print(f"-- {title} --")
    b = np.searchsorted(edges, key)
    for i, nm in enumerate(names):
        m = (b == i) & np.isfinite(key)
        if m.sum() < 5:
            continue
        per = " ".join(
            f"{a.PERIODS[p][:2]} {T['und'][m & (T['per'] == p)].mean():+6.1f}bp/{T['opt'][m & (T['per'] == p)].mean():+6.2f}%"
            for p in range(3)
        )
        print(
            f"  {nm:22s} n={m.sum():5d} und {a.fmt(T['und'][m], T['day'][m], nd=1)}bp hit {(T['und'][m] > 0).mean() * 100:4.1f}% "
            f"| opt {a.fmt(T['opt'][m], T['day'][m])}% | {per}"
        )


def main() -> None:
    insts = {s: a.Inst(s) for s in a.INDICES}
    params = {s: a.opt_params(s, insts[s]) for s in a.INDICES}
    sigs = {s: sigma14(insts[s]) for s in a.INDICES}
    ref_days = insts["NIFTY"].day
    RV, BR = market_feats(ref_days)  # noqa: N806
    ref_pos = {int(d): i for i, d in enumerate(ref_days)}

    zl_by, zs_by = {}, {}
    for s, inst in insts.items():
        up = np.maximum(inst.o, inst.pc)[:, None]
        dn = np.minimum(inst.o, inst.pc)[:, None]
        zl_by[s] = dict(zip(inst.day.tolist(), (inst.C / up - 1) / sigs[s], strict=False))
        zs_by[s] = dict(zip(inst.day.tolist(), (inst.C / dn - 1) / sigs[s], strict=False))

    cols: dict[str, list] = {
        k: [] for k in "und opt day per side idx comp rvol breadth vix agree z tmin".split()
    }
    for si, (s, inst) in enumerate(insts.items()):
        tr = a1_trades(inst, sigs[s], K, None)
        r = results(inst, params[s], tr)
        rng = np.nanmax(inst.H, axis=1) / np.nanmin(inst.L, axis=1) - 1.0
        iv = params[s]["iv"]
        full = np.flatnonzero(inst.full)
        for d, e, sd in zip(tr["d"], tr["e"], tr["s"], strict=False):
            tau, day = e - 1, int(inst.day[d])
            prev = full[full < d][-21:]
            comp = rng[prev[-1]] / rng[prev[:-1]].mean() if len(prev) == 21 else np.nan
            j = ref_pos.get(day, -1)
            z = (zl_by[s][day] if sd == 1 else -zs_by[s][day])[tau]
            agree = 0
            for s2 in a.INDICES:
                zz = (zl_by[s2] if sd == 1 else zs_by[s2]).get(day)
                if (
                    zz is not None
                    and np.isfinite(zz[tau])
                    and (zz[tau] >= K if sd == 1 else zz[tau] <= -K)
                ):
                    agree += 1
            cols["comp"].append(comp)
            cols["rvol"].append(RV[j, tau] if j >= 0 else np.nan)
            cols["breadth"].append(BR[j, tau] * sd if j >= 0 else np.nan)
            cols["vix"].append(iv[d] / np.median(iv[max(0, d - 250) : d]) if d >= 60 else np.nan)
            cols["agree"].append(agree)
            cols["z"].append(z)
            cols["tmin"].append(tau)
        for k_ in ("und", "opt", "day", "per", "side"):
            cols[k_].extend(r[k_].tolist())
        cols["idx"].extend([si] * len(tr["d"]))
    T = {k_: np.array(v, dtype=float) for k_, v in cols.items()}  # noqa: N806
    T["per"] = T["per"].astype(int)
    print(
        f"A1 trades, k={K}, hold to 15:15: {len(T['und'])}; und {a.fmt(T['und'], T['day'], nd=1)}bp; opt {a.fmt(T['opt'], T['day'])}%"
    )

    table("Compression: yesterday's range / mean of the 20 sessions before", T["comp"],
          [0.5, 0.65, 0.8, 1.0, 1.3], ["< 0.5", "0.5-0.65", "0.65-0.8", "0.8-1.0", "1.0-1.3", "> 1.3"], T)  # fmt: skip
    table("Participation: relative volume of the 18 stocks at the entry minute", T["rvol"],
          [0.8, 0.95, 1.1, 1.3, 1.6], ["< 0.8", "0.8-0.95", "0.95-1.1", "1.1-1.3", "1.3-1.6", "> 1.6"], T)  # fmt: skip
    table(
        "Breadth of the 18 stocks in the trade's direction",
        T["breadth"],
        [0.0, 0.3, 0.6, 0.8],
        ["< 0", "0-0.3", "0.3-0.6", "0.6-0.8", "> 0.8"],
        T,
    )
    table(
        "VIX / its 250-session median",
        T["vix"],
        [0.85, 1.0, 1.2],
        ["< 0.85", "0.85-1.0", "1.0-1.2", "> 1.2"],
        T,
    )
    table(
        "Strength z at entry",
        T["z"],
        [1.5, 2.0, 3.0],
        ["1.0-1.5", "1.5-2.0", "2.0-3.0", "> 3.0"],
        T,
    )

    feats = ["comp", "rvol", "breadth", "vix", "agree", "z", "tmin"]
    Xraw = np.column_stack(
        [
            np.log(T["comp"]),
            np.log(T["rvol"]),
            T["breadth"],
            np.log(T["vix"]),
            T["agree"],
            np.log(T["z"]),
            T["tmin"],
        ]
    )  # noqa: N806
    ok = np.isfinite(Xraw).all(axis=1)
    tune = ok & (T["per"] == 0)
    mu, sd = Xraw[tune].mean(axis=0), Xraw[tune].std(axis=0)
    Xs = (Xraw - mu) / sd  # noqa: N806
    A = np.column_stack([np.ones(tune.sum()), Xs[tune]])  # noqa: N806
    y = T["und"][tune]
    beta, *_ = np.linalg.lstsq(A, y, rcond=None)
    resid = y - A @ beta
    cov = np.linalg.inv(A.T @ A) * resid.var(ddof=A.shape[1])
    print(
        "\n-- Linear score fitted on TUNE only (signed index move, bp, per 1 sd of the feature) --"
    )
    print(
        "  "
        + "  ".join(
            f"{f} {b:+5.2f}(t{b / np.sqrt(c):+4.1f})"
            for f, b, c in zip(feats, beta[1:], np.diag(cov)[1:], strict=False)
        )
    )
    score = np.full(len(ok), np.nan)
    score[ok] = np.column_stack([np.ones(ok.sum()), Xs[ok]]) @ beta
    cut = np.nanquantile(score[tune], [1 / 3, 2 / 3])
    print("  score terciles (cut points from TUNE):")
    for p in range(3):
        row = f"  {a.PERIODS[p]:6s}"
        for i, nm in enumerate(("low", "mid", "high")):
            m = ok & (T["per"] == p) & (np.searchsorted(cut, score) == i)
            row += f" | {nm} n={m.sum():4d} und {a.fmt(T['und'][m], T['day'][m], nd=1)}bp opt {a.fmt(T['opt'][m], T['day'][m])}%"
        print(row)
    np.savez(a.OUT / "a1_trades_k1.npz", **T, score=score)


if __name__ == "__main__":
    main()
