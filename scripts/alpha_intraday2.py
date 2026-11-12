"""Alpha search of 2026-11-12, step B2: conditioning the intraday momentum trade (PLAN.md B).

Time-of-day profile; A1 split by market-wide agreement, gap alignment, VIX, narrow range,
weekday, turn of month; A4 big-money confirmation from the heavyweight stocks (breadth, signed
volume, relative volume) for NIFTY and BANKNIFTY; and a market-wide form of A1.

    PYTHONPATH=src:. .venv/bin/python -W ignore scripts/alpha_intraday2.py
"""

from __future__ import annotations

import datetime as dt

import alpha_lib as a
import numpy as np
from alpha_intraday1 import a1_trades, line, results, sigma14

W_BANK = {"HDFCBANK": 28, "ICICIBANK": 24, "SBIN": 9, "AXISBANK": 9, "KOTAKBANK": 9}
W_NIFTY = {
    "HDFCBANK": 13, "ICICIBANK": 9, "RELIANCE": 8.5, "INFY": 5, "BHARTIARTL": 4.5, "LT": 4,
    "ITC": 3.5, "TCS": 3, "SBIN": 3, "AXISBANK": 3, "KOTAKBANK": 2.7, "MM": 2.5,
    "BAJFINANCE": 2.2, "SUNPHARMA": 1.7, "MARUTI": 1.5, "TATASTEEL": 1.2, "TMPV": 1.0,
    "HEROMOTOCO": 0.5,
}  # fmt: skip


def sub(r: dict, m: np.ndarray) -> dict:
    return {k: v[m] for k, v in r.items()}


def stock_feats(series: str, days: np.ndarray) -> dict[str, np.ndarray]:
    """Per index-session row and minute: sign of the move from the open, signed-volume
    imbalance so far, relative cumulative volume (vs the mean of the previous 20 sessions)."""
    s = a.Inst(series)
    pos = {int(d): i for i, d in enumerate(s.day)}
    rows = np.array([pos.get(int(d), -1) for d in days])
    C, V = s.C, s.V  # noqa: N806
    dC = np.diff(C, axis=1, prepend=C[:, :1])  # noqa: N806
    cumv = np.cumsum(V, axis=1)
    ofi = np.cumsum(np.sign(dC) * V, axis=1) / np.where(cumv > 0, cumv, np.nan)
    mean20 = np.full_like(cumv, np.nan)
    full = np.flatnonzero(s.full)
    for j, i in enumerate(full):
        if j >= 20:
            mean20[i] = cumv[full[j - 20 : j]].mean(axis=0)
    rvol = cumv / mean20
    mv = np.sign(C / s.o[:, None] - 1.0)
    out = {}
    for nm, arr in (("mv", mv), ("ofi", ofi), ("rvol", rvol)):
        o = np.full((len(days), a.NMIN), np.nan)
        ok = rows >= 0
        o[ok] = arr[rows[ok]]
        out[nm] = o
    return out


def weighted(feats: dict[str, dict], w: dict[str, float], nm: str) -> np.ndarray:
    num = den = 0.0
    for s, wt in w.items():
        x = feats[s][nm]
        ok = np.isfinite(x)
        num = num + np.where(ok, x, 0.0) * wt
        den = den + ok * wt
    return num / np.where(den > 0, den, np.nan)


def main() -> None:
    insts = {s: a.Inst(s) for s in a.INDICES}
    params = {s: a.opt_params(s, insts[s]) for s in a.INDICES}
    sigs = {s: sigma14(insts[s]) for s in a.INDICES}

    print("== Time of day: mean index move in each half hour, bp (5 indices; t on daily sums) ==")
    row = ""
    for b in range(12):
        lo, hi = b * 30, min(b * 30 + 30, a.NMIN - 1)
        xs, ds = [], []
        for inst in insts.values():
            d = np.flatnonzero(inst.valid & (inst.year >= 0))
            xs.append((inst.C[d, hi] / (inst.o[d] if lo == 0 else inst.C[d, lo]) - 1.0) * 1e4)
            ds.append(inst.day[d])
        m, t, _ = a.day_t(np.concatenate(xs), np.concatenate(ds))
        hh = a.GRID0 + lo
        row += f" {hh // 60:02d}:{hh % 60:02d} {m:+5.1f}(t{t:+4.1f})"
        if b % 4 == 3:
            print(row)
            row = ""

    for k in (1.0, 2.0):
        TR = {s: a1_trades(insts[s], sigs[s], k, None) for s in a.INDICES}  # noqa: N806
        R = {s: results(insts[s], params[s], TR[s]) for s in a.INDICES}  # noqa: N806
        print(f"\n== A1 k={k}, hold to 15:15: conditional cuts (und bp, modelled option %) ==")
        print(line("all trades", list(R.values())))
        # how many of the five indices are beyond k in the trade's direction at its entry checkpoint
        zdir = {}
        for s, inst in insts.items():
            up = np.maximum(inst.o, inst.pc)[:, None]
            dn = np.minimum(inst.o, inst.pc)[:, None]
            zl, zs = (inst.C / up - 1) / sigs[s], (inst.C / dn - 1) / sigs[s]
            zdir[s] = (
                dict(zip(inst.day.tolist(), zl, strict=False)),
                dict(zip(inst.day.tolist(), zs, strict=False)),
            )
        agree = {}
        for s, inst in insts.items():
            tr = TR[s]
            cnt = np.zeros(len(tr["d"]), dtype=int)
            for i, (d, e, sd) in enumerate(zip(tr["d"], tr["e"], tr["s"], strict=False)):
                day, tau = int(inst.day[d]), e - 1
                for s2 in a.INDICES:
                    z = zdir[s2][0 if sd == 1 else 1].get(day)
                    if (
                        z is not None
                        and np.isfinite(z[tau])
                        and (z[tau] >= k if sd == 1 else z[tau] <= -k)
                    ):
                        cnt[i] += 1
            agree[s] = cnt
        for nm, f in (
            ("  1 index beyond", lambda c: c == 1),
            ("  2-3 beyond", lambda c: (c >= 2) & (c <= 3)),
            ("  4-5 beyond", lambda c: c >= 4),
        ):
            print(line(nm, [sub(R[s], f(agree[s])) for s in a.INDICES]))
        cuts = {s: {} for s in a.INDICES}
        for s, inst in insts.items():
            d = TR[s]["d"]
            gap = inst.o[d] / inst.pc[d] - 1.0
            cuts[s]["gap with"] = np.sign(gap) == TR[s]["s"]
            cuts[s]["gap against"] = np.sign(gap) == -TR[s]["s"]
            iv = params[s]["iv"]
            med = np.array([np.median(iv[max(0, i - 250) : i]) if i >= 60 else np.nan for i in d])
            cuts[s]["VIX above its 250d median"] = iv[d] > med
            cuts[s]["VIX below its 250d median"] = iv[d] <= med
            rng = np.nanmax(inst.H, axis=1) / np.nanmin(inst.L, axis=1) - 1.0
            nr7 = np.array(
                [rng[i - 1] <= np.nanmin(rng[max(0, i - 7) : i]) if i >= 7 else False for i in d]
            )
            cuts[s]["after NR7"] = nr7
            wd = np.array([dt.date.fromordinal(int(x)).weekday() for x in inst.day[d]])
            for w, nm in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri")):
                cuts[s][nm] = wd == w
            mon = np.array([dt.date.fromordinal(int(x)).month for x in inst.day])
            first = np.r_[True, mon[1:] != mon[:-1]]
            last = np.r_[first[1:], False]
            tom = last.copy()
            for sh in (0, 1, 2):
                tom |= np.r_[np.zeros(sh, bool), first[: len(first) - sh]] if sh else first
            cuts[s]["turn of month, calls"] = tom[d] & (TR[s]["s"] == 1)
        for nm in cuts["NIFTY"]:
            print(line("  " + nm, [sub(R[s], cuts[s][nm]) for s in a.INDICES]))

        print(f"-- A4 big-money confirmation at the entry minute (k={k}) --")
        for idx, w in (("NIFTY", W_NIFTY), ("BANKNIFTY", W_BANK)):
            inst, tr, r = insts[idx], TR[idx], R[idx]
            feats = {s: stock_feats(s, inst.day) for s in w}
            B, O, RV = (weighted(feats, w, nm) for nm in ("mv", "ofi", "rvol"))  # noqa: N806
            d, tau, sd = tr["d"], tr["e"] - 1, tr["s"]
            b, o, rv = B[d, tau] * sd, O[d, tau] * sd, RV[d, tau]
            for nm, m in (
                (f"  {idx} breadth >= 0.7 with", b >= 0.7), (f"  {idx} breadth < 0.7", b < 0.7),
                (f"  {idx} signed volume with", o > 0.02), (f"  {idx} signed volume against", o < -0.02),
                (f"  {idx} rel volume >= 1.2", rv >= 1.2), (f"  {idx} rel volume < 0.9", rv < 0.9),
                (f"  {idx} all three confirm", (b >= 0.7) & (o > 0.02) & (rv >= 1.0)),
            ):  # fmt: skip
                print(line(nm, [sub(r, m)]))


if __name__ == "__main__":
    main()
