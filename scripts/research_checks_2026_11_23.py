"""Independent re-derivation of the US-077 (market states) and US-080 (regime) figures.

Own code, own reading of the briefs; nothing is imported from the studies' scripts. Read-only on
the store and on the studies' raw outputs. Parts (argv[1]): A (market states), B1 (all-condition
table and cell count), B3 (timing spot checks), B4 (stock costs), C (the API of a private mock
instance on port 8795 and the legend text).

Run from the MAIN checkout (its harness, its data), never from the worktree:
  PYTHONPATH=src:scripts:. .venv/bin/python -W ignore <this file> A
"""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

sys.path[:] = [x for x in sys.path if Path(x or ".").resolve() != Path(__file__).resolve().parent]
import mtf_bruteforce as h  # noqa: E402

assert Path(h.__file__).resolve().parents[1] == Path.cwd().resolve(), h.__file__

WT = Path(__file__).resolve().parents[1]
MAIN = Path.cwd()
REG = MAIN / "data" / "research" / "regime_2026-11-23"
DIRS = ("bull", "flat", "bear")
RNG = np.random.default_rng(20261123)


def fm(x: float, d: int = 1) -> str:
    return "nan" if x is None or not np.isfinite(x) else f"{x:.{d}f}".replace("-", "-")


def cl_t(x: np.ndarray, key: np.ndarray) -> float:
    """mean / cluster-robust SE; all observations with the same key are one draw."""
    x = np.asarray(x, float)
    if len(x) < 3:
        return float("nan")
    _, inv = np.unique(np.asarray(key), return_inverse=True)
    dev = np.bincount(inv, x) - x.mean() * np.bincount(inv)
    se = np.sqrt(float((dev**2).sum())) / len(x)
    return float(x.mean() / se) if se > 0 else float("nan")


# ═════════════════════════════ Part A: market states ═════════════════════════════
def my_states(b: pd.DataFrame) -> dict[str, np.ndarray]:
    """Own implementation of the brief's rules. Returns arrays over the cleaned sessions."""
    hi, lo, c = (
        pd.to_numeric(b[k], errors="coerce").to_numpy(float) for k in ("high", "low", "close")
    )
    ok = np.isfinite(hi) & np.isfinite(lo) & np.isfinite(c) & (c > 0) & (hi >= lo)
    hi, lo, c = hi[ok], lo[ok], c[ok]
    dates = np.array([pd.Timestamp(t).date() for t in b.index[ok]])
    n = len(c)
    r = np.full(n, np.nan)
    r[1:] = np.log(c[1:] / c[:-1])
    rng = np.full(n, np.nan)
    rng[1:] = (hi[1:] - lo[1:]) / c[:-1]
    z = np.full(n, np.nan)
    act = np.full(n, -1)
    for t in range(21, n):
        s = np.std(r[t - 20 : t], ddof=1)  # the 20 sessions before t
        m = np.median(rng[t - 20 : t])
        if s > 0:
            z[t] = r[t] / s
        act[t] = int(rng[t] > m)
    d = np.where(np.isnan(z), -1, np.where(z > 0.5, 0, np.where(z < -0.5, 2, 1)))
    st = (d >= 0) & (act >= 0)
    return {"dates": dates[st], "d": d[st], "s": act[st], "z": z[st], "dropped": int((~ok).sum())}


def load_states() -> dict[str, dict[str, np.ndarray]]:
    h._init_worker()
    out = {}
    for inst in h.INDEX_OPTIONS + h.STOCKS:
        b = h._cache.bars(h._cache.spec(inst).series_key, "1d")
        b = b[np.isfinite(b["close"]) & (b["close"] > 0)]
        out[inst] = my_states(b)
    return out


def walk(d: np.ndarray, s: np.ndarray, start: int = 250) -> dict[str, np.ndarray]:
    base, sb = np.zeros(3), np.zeros(2)
    c1, c2, s1 = np.zeros((3, 3)), np.zeros((3, 3, 3)), np.zeros((2, 2))
    rows = []
    for j in range(len(d)):
        if j >= start:
            bp = base / base.sum()
            row = c1[d[j - 1]]
            if c2[d[j - 2], d[j - 1]].sum() >= 60:
                row = c2[d[j - 2], d[j - 1]]
            p = (row + 10 * bp) / (row.sum() + 10)
            sbp = sb / sb.sum()
            srow = s1[s[j - 1]]
            sp = (srow + 10 * sbp) / (srow.sum() + 10)
            rows.append(
                (int(np.argmax(p)), int(np.argmax(base)), int(np.argmax(sp)), int(np.argmax(sb)), j)
            )
        if j >= 1:
            c1[d[j - 1], d[j]] += 1
            s1[s[j - 1], s[j]] += 1
        if j >= 2:
            c2[d[j - 2], d[j - 1], d[j]] += 1
        base[d[j]] += 1
        sb[s[j]] += 1
    a = np.array(rows).reshape(-1, 5)
    return {
        "dl": a[:, 0], "dn": a[:, 1], "sl": a[:, 2], "sn": a[:, 3], "j": a[:, 4],
        "d": d[a[:, 4]], "s": s[a[:, 4]],
    }  # fmt: skip


def part_a() -> None:
    S = load_states()
    insts = {"indices": h.INDEX_OPTIONS, "stocks": h.STOCKS}
    print("dropped bad rows:", {k: v["dropped"] for k, v in S.items() if v["dropped"]})
    # 1. base rates
    print("\n1. BASE RATES  (bull flat bear | active | stated sessions)")
    for g, ii in insts.items():
        d = np.concatenate([S[i]["d"] for i in ii])
        s = np.concatenate([S[i]["s"] for i in ii])
        print(f"  {g:8} " + " ".join(f"{100 * (d == k).mean():.1f}" for k in range(3))
              + f" | {100 * s.mean():.1f} | {len(d)}")  # fmt: skip
    # 2. owner's example and 3. size memory, with the instrument's own base
    print("\n2. P(bull | two bulls before)  vs base (group base, sample-weighted own base), n, t")
    runs = {}
    for g, ii in insts.items():
        hit, bas, key, tot = [], [], [], 0
        tail = {1: [0, 0], 2: [0, 0], 3: [0, 0]}
        for i in ii:
            d, dt = S[i]["d"], S[i]["dates"]
            bb = (d == 0).mean()
            for t in range(2, len(d)):
                if d[t - 1] == 0 and d[t - 2] == 0:
                    hit.append(d[t] == 0)
                    bas.append(bb)
                    key.append(dt[t])
            tot += len(d)
            run = 0
            for t in range(len(d) - 1):
                run = run + 1 if d[t] == 0 else 0
                if run:
                    k = min(run, 3)
                    tail[k][0] += int(d[t + 1] == 0)
                    tail[k][1] += 1
        hit, bas = np.array(hit, float), np.array(bas)
        gb = np.mean(np.concatenate([S[i]["d"] for i in ii]) == 0)
        print(f"  {g:8} P={100 * hit.mean():.1f}% group base {100 * gb:.1f}% own-base "
              f"{100 * bas.mean():.1f}% n={len(hit)} t={cl_t(hit - bas, np.array(key)):.1f}")  # fmt: skip
        runs[g] = tail
    print("   runs, bull, share continued (n): " + "; ".join(
        f"{g} " + ", ".join(f"{k}: {100 * v[0] / v[1]:.1f}% ({v[1]})" for k, v in t.items())
        for g, t in runs.items()))  # fmt: skip
    print("\n3. SIZE memory  P(active|active), P(active|calm), base, n, t")
    for g, ii in insts.items():
        res = {}
        for prev in (1, 0):
            x, bas, key = [], [], []
            for i in ii:
                s, dt = S[i]["s"], S[i]["dates"]
                m = s[:-1] == prev
                x += list(s[1:][m])
                bas += [s.mean()] * int(m.sum())
                key += list(dt[1:][m])
            x, bas = np.array(x, float), np.array(bas)
            res[prev] = (x.mean(), bas.mean(), len(x), cl_t(x - bas, np.array(key)))
        print(f"  {g:8} active->active {100 * res[1][0]:.1f}% (n {res[1][2]}, t {res[1][3]:.1f})"
              f"  calm->active {100 * res[0][0]:.1f}% (n {res[0][2]}, t {res[0][3]:.1f})"
              f"  base {100 * res[1][1]:.1f}%")  # fmt: skip
    # 4. walk-forward
    print(
        "\n4. WALK-FORWARD (stated session index >= 250, 0-based)  n, dir hit, naive, size hit, naive"
    )
    allw = {k: [] for k in ("dl", "dn", "d", "sl", "sn", "s")}
    better = 0
    for g, ii in insts.items():
        acc = {k: [] for k in allw}
        for i in ii:
            w = walk(S[i]["d"], S[i]["s"])
            for k in acc:
                acc[k].append(w[k])
                allw[k].append(w[k])
            better += int((w["dl"] == w["d"]).mean() > (w["dn"] == w["d"]).mean())
        a = {k: np.concatenate(v) for k, v in acc.items()}
        print(f"  {g:8} n={len(a['d'])} dir {100 * (a['dl'] == a['d']).mean():.1f}% naive "
              f"{100 * (a['dn'] == a['d']).mean():.1f}% | size {100 * (a['sl'] == a['s']).mean():.1f}% "
              f"naive {100 * (a['sn'] == a['s']).mean():.1f}%")  # fmt: skip
    a = {k: np.concatenate(v) for k, v in allw.items()}
    print(f"  all      n={len(a['d'])} dir {100 * (a['dl'] == a['d']).mean():.1f}% naive "
          f"{100 * (a['dn'] == a['d']).mean():.1f}% | size {100 * (a['sl'] == a['s']).mean():.1f}% "
          f"naive {100 * (a['sn'] == a['s']).mean():.1f}%; model beat naive in {better} of 23")  # fmt: skip
    # 5. leak test against the product module, loaded by file path
    spec = importlib.util.spec_from_file_location(
        "states_under_test", WT / "src/onelot/mllab/states.py"
    )
    P = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(P)  # type: ignore[union-attr]
    keys = h.INDEX_OPTIONS + h.STOCKS
    bad = 0
    agree_own = 0
    for _ in range(30):
        inst = keys[int(RNG.integers(len(keys)))]
        b = h._cache.bars(h._cache.spec(inst).series_key, "1d")
        b = b[np.isfinite(b["close"]) & (b["close"] > 0)]
        t = int(RNG.integers(60, len(b)))
        full = P.classify(b)
        cut = P.classify(b.iloc[: t + 1])
        a, c = full.iloc[t], cut.iloc[t]
        same = (a["direction"], a["size"]) == (c["direction"], c["size"]) and np.isclose(
            a["z"], c["z"]
        )
        mine = my_states(b.iloc[: t + 1])
        mine_last = (DIRS[mine["d"][-1]], ("calm", "active")[mine["s"][-1]]) if len(mine["d"]) and (
            mine["dates"][-1] == pd.Timestamp(b.index[t]).date()) else None  # fmt: skip
        agree_own += mine_last == (c["direction"], c["size"])
        bad += not same
    print(f"\n5. LEAK TEST: 30 random cuts, product state of t changed by cutting later bars: {bad}; "
          f"my own implementation agrees with the product on the cut frame: {agree_own} of 30")  # fmt: skip
    # whole-history agreement of my states with the product's
    diff = 0
    tot = 0
    for inst in keys:
        b = h._cache.bars(h._cache.spec(inst).series_key, "1d")
        b = b[np.isfinite(b["close"]) & (b["close"] > 0)]
        f = P.classify(b)
        f = f[f["direction"].notna()]
        m = S[inst]
        tot += len(f)
        diff += int(
            len(f) != len(m["d"])
            or (np.array([DIRS.index(x) for x in f["direction"]]) != m["d"]).sum()
            + ((f["size"].to_numpy() == "active").astype(int) != m["s"]).sum()
        )
    print(
        f"   my states vs product states, all 23 instruments: {tot} sessions, differing cells {diff}"
    )


# ═════════════════════════════ Part B: the regime study ═════════════════════════════
NOTE_OPT = {  # strategy: (trades, mean, t, win, payoff, idx bp, tune, choose, test)
    "microtrend": (3116, -0.27, -0.7, 41.1, 1.35, 3.3, -0.10, 0.08, -1.20),
    "supertrend_mtf": (1517, -0.54, -0.8, 47.5, 1.01, 1.0, -0.22, -0.62, -1.45),
    "supertrend_adx": (549, -0.33, -0.5, 41.7, 1.30, 3.9, -0.26, 1.04, -2.17),
    "ema_cross": (2988, -0.62, -1.5, 44.2, 1.12, 2.0, -0.31, -1.55, -0.60),
    "supertrend_vol": (4129, -0.88, -3.1, 37.1, 1.39, 1.7, -0.74, -0.52, -1.73),
    "orb": (3921, -0.84, -2.0, 50.7, 0.85, 1.6, -0.78, -0.41, -1.48),
    "nr7_breakout": (428, -1.68, -2.0, 46.0, 0.84, -0.0, -1.00, -3.81, -2.08),
    "cpr_breakout": (2087, -1.28, -2.7, 49.1, 0.83, 0.3, -1.73, -0.34, -0.98),
    "ema_pullback": (441, -1.81, -2.1, 43.1, 0.96, -1.1, -1.76, -0.34, -3.28),
    "intraday_momentum": (4369, -1.67, -9.0, 37.7, 0.85, -0.2, -1.93, -1.48, -1.11),
}  # fmt: skip


def cell_stats(d: pd.DataFrame) -> dict:
    x = d["ret"].to_numpy(float)
    day = d["day"].to_numpy()
    per = [d.loc[d.period == k, "ret"] for k in range(3)]
    w, ls = x[x > 0], x[x <= 0]
    return {
        "n": len(x), "m": x.mean(), "t": cl_t(x, day), "win": 100 * (x > 0).mean(),
        "pay": w.mean() / abs(ls.mean()) if len(w) and len(ls) else np.nan, "bp": d["bp"].mean(),
        "per": [q.mean() if len(q) else np.nan for q in per],
        "tune_t": cl_t(per[0].to_numpy(), d.loc[d.period == 0, "day"].to_numpy()),
    }  # fmt: skip


def part_b1() -> None:
    df = pd.read_csv(REG / "trades.csv")
    print("trades:", len(df), "groups", df.group.value_counts().to_dict())
    # period column against the day offset (periods end 2024-11-07, 2025-11-07, 2026-11-07)
    ds = pd.Timestamp(h.T0) + pd.to_timedelta(df["day"], unit="D")
    per2 = np.where(
        ds <= pd.Timestamp("2024-11-07"), 0, np.where(ds <= pd.Timestamp("2025-11-07"), 1, 2)
    )
    print("period column disagrees with day offset in", int((per2 != df.period).sum()), "rows;",
          "dates", ds.min().date(), ds.max().date())  # fmt: skip
    print("\nB1. all conditions, per strategy (mine | note)  [options]")
    mx = 0.0
    for name, nt in NOTE_OPT.items():
        s = cell_stats(df[(df.group == "options") & (df.strategy == name)])
        mine = (s["n"], s["m"], s["t"], s["win"], s["pay"], s["bp"], *s["per"])
        diffs = [abs(a - b) for a, b in zip(mine[1:], nt[1:], strict=True)]
        mx = max(mx, max(diffs[:2] + diffs[5:]))
        print(f"  {name:18} n {mine[0]} ({nt[0]}) mean {mine[1]:+.2f} ({nt[1]:+.2f}) t {mine[2]:+.1f} "
              f"({nt[2]:+.1f}) win {mine[3]:.1f} ({nt[3]}) pay {mine[4]:.2f} ({nt[4]}) bp {mine[5]:+.1f} "
              f"({nt[5]:+.1f}) T/C/T {mine[6]:+.2f}/{mine[7]:+.2f}/{mine[8]:+.2f} "
              f"({nt[6]:+.2f}/{nt[7]:+.2f}/{nt[8]:+.2f})")  # fmt: skip
    print("  largest |difference| in mean or TUNE/CHOOSE/TEST:", round(mx, 3))
    o = df[df.group == "options"]
    print(f"  all option trades: {o.ret.mean():+.2f}% a trade; less 1.0 point {o.ret.mean() - 1:+.2f}; "
          f"tune {o[o.period == 0].ret.mean():+.2f}, test {o[o.period == 2].ret.mean():+.2f}")  # fmt: skip
    print("\n  stocks, 14 strategies (n, mean, t, T/C/T, gross bp)")
    rows = []
    for name, d in df[df.group == "stocks"].groupby("strategy"):
        s = cell_stats(d)
        rows.append((name, s))
        print(f"  {name:18} n {s['n']:6} mean {s['m']:+.3f} t {s['t']:+.1f} win {s['win']:.1f} "
              f"T/C/T {s['per'][0]:+.2f}/{s['per'][1]:+.2f}/{s['per'][2]:+.2f} bp {s['bp']:+.1f}")  # fmt: skip
    ms = [s["m"] for _, s in rows]
    ts = [s["t"] for _, s in rows]
    print(f"  stock means range {min(ms):+.2f} to {max(ms):+.2f}, t {min(ts):.1f} to {max(ts):.1f}; "
          f"gross bp range {min(s['bp'] for _, s in rows):+.1f} to {max(s['bp'] for _, s in rows):+.1f}")  # fmt: skip
    # B2: cells
    print("\nB2. cells (strategy x condition x group, >= 100 trades)")
    for key, vals, label in (
        ("cond", ("UP-TREND", "DOWN-TREND", "CHOPPY", "NEUTRAL"), "condition"),
        ("vix", ("VIX HIGH", "VIX LOW"), "VIX cut"),
    ):
        cells, pos, cand, small, top = 0, 0, 0, 0, []
        for (g, st, c), d in df[df[key].isin(vals)].groupby(["group", "strategy", key]):
            if len(d) < 100:
                small += 1
                continue
            cells += 1
            s = cell_stats(d)
            pos += s["per"][0] > 0
            cand += (s["per"][0] > 0) and (s["tune_t"] >= 2)
            top.append((s["tune_t"], g, st, c, len(d), s["per"]))
        top.sort(reverse=True)
        print(f"  {label}: {cells} cells (options {sum(1 for t in top if t[1] == 'options')}, "
              f"stocks {sum(1 for t in top if t[1] == 'stocks')}), {small} groups under 100; "
              f"TUNE mean > 0: {pos}; TUNE mean > 0 and t >= 2: {cand}")  # fmt: skip
        for t in top[:4]:
            print(f"     highest TUNE t {t[0]:+.2f}: {t[1]} {t[2]} {t[3]} n={t[4]} "
                  f"T/C/T {t[5][0]:+.2f}/{t[5][1]:+.2f}/{t[5][2]:+.2f}")  # fmt: skip
    # sessions
    ss = pd.read_csv(REG / "sessions.csv")
    ss["grp"] = np.where(ss.inst.str.endswith("_OPT"), "options", "stocks")
    print("\n  sessions (instrument-sessions) TUNE/CHOOSE/TEST by condition:")
    for g, d in ss.groupby("grp"):
        print("   ", g, {c: "/".join(str(int(((d.cond == c) & (d.period == p)).sum())) for p in range(3))
                         for c in ("UP-TREND", "DOWN-TREND", "CHOPPY", "NEUTRAL", "VIX HIGH")})  # fmt: skip


# ── Wilder ADX, own ──
def adx_wilder(hi: np.ndarray, lo: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    k = len(c)
    out = np.full(k, np.nan)
    if k < 2 * n + 1:
        return out
    up, dn = hi[1:] - hi[:-1], lo[:-1] - lo[1:]
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = np.maximum(hi[1:] - lo[1:], np.maximum(abs(hi[1:] - c[:-1]), abs(lo[1:] - c[:-1])))
    atr, ps, ms = tr[:n].sum(), pdm[:n].sum(), mdm[:n].sum()
    dx = []

    def _dx(a: float, p: float, m: float) -> float:
        pi, mi = 100 * p / a, 100 * m / a
        return 100 * abs(pi - mi) / (pi + mi) if pi + mi else 0.0

    dx.append(_dx(atr, ps, ms))
    for i in range(n, len(tr)):
        atr = atr - atr / n + tr[i]
        ps = ps - ps / n + pdm[i]
        ms = ms - ms / n + mdm[i]
        dx.append(_dx(atr, ps, ms))
    dx = np.array(dx)  # dx[j] belongs to bar j + n
    a = np.full(len(dx), np.nan)
    a[n - 1] = dx[:n].mean()
    for j in range(n, len(dx)):
        a[j] = (a[j - 1] * (n - 1) + dx[j]) / n
    out[n : n + len(dx)] = a  # bar index j + n
    return out


def cond_before(daily: pd.DataFrame, date) -> tuple[str, float, float, float]:  # noqa: ANN001
    cut = daily[np.array([x.date() for x in daily.index]) < date]
    hi, lo, c = (cut[k].to_numpy(float) for k in ("high", "low", "close"))
    a = adx_wilder(hi, lo, c)[-1]
    s20 = c[-20:].mean()
    if not np.isfinite(a):
        return "", a, s20, c[-1]
    lab = "NEUTRAL"
    if a < 20:
        lab = "CHOPPY"
    elif a >= 25 and c[-1] > s20:
        lab = "UP-TREND"
    elif a >= 25 and c[-1] < s20:
        lab = "DOWN-TREND"
    return lab, a, s20, c[-1]


def part_b3() -> None:
    h._init_worker()
    from onelot.indicators import core as ind
    from onelot.strategies.registry import REGISTRY

    df = pd.read_csv(REG / "trades.csv")
    picks = [("microtrend", "options"), ("orb", "stocks"), ("mean_reversion", "stocks")]
    for name, grp in picks:
        tf = REGISTRY[name].Params().timeframe
        tfm = int(tf[:-1])
        d0 = df[(df.strategy == name) & (df.group == grp)]
        cnt = d0.groupby(["inst", "day"]).size()
        one = d0.set_index(["inst", "day"]).loc[cnt[cnt == 1].index].reset_index()
        samp = one.iloc[RNG.choice(len(one), 30, replace=False)]
        print(
            f"\n=== {name} ({grp}, {tf}): {len(d0)} trades, {len(one)} on single-trade days; 30 sampled"
        )
        okA = okB = okC = nC = n_early = n_late = 0
        okR = nR = 0
        detail = []
        for _, tr in samp.iterrows():
            inst, side = tr.inst, int(tr.side)
            key = h._cache.spec(inst).series_key
            m1 = h._cache.bars(key, "1m")
            daily = h._cache.bars(key, "1d")
            date = (pd.Timestamp(h.T0) + pd.Timedelta(days=int(tr.day))).date()
            # (a) the condition from daily bars strictly before the date
            lab, adx, s20, last = cond_before(daily, date)
            okA += lab == tr.cond
            if lab != tr.cond:
                detail.append(f"   (a) {inst} {date}: mine {lab} (ADX {adx:.1f}) csv {tr.cond}")
            # (b) the entry minute from the cached decisions
            z = np.load(REG / "signals" / f"{name}__{inst}.npz")
            dec = z["long_entry" if side == 1 else "short_entry"]
            tsn = m1.index.tz_convert("UTC").as_unit("ns").asi8
            ent = np.searchsorted(tsn, dec, side="left")
            valid = ent < len(tsn)
            ent_dates = np.array(
                [m1.index[e].date() if v else None for e, v in zip(ent, valid, strict=True)]
            )
            sel = np.flatnonzero(ent_dates == date)
            other = z["short_entry" if side == 1 else "long_entry"]
            oent = np.searchsorted(tsn, other, side="left")
            odate = [m1.index[e].date() if e < len(tsn) else None for e in oent]
            first_other = [e for e, dd in zip(oent, odate, strict=True) if dd == date]
            if not len(sel):
                detail.append(f"   (b) {inst} {date}: no signal that day")
                continue
            e = int(ent[sel[0]])
            if first_other and min(first_other) < e:
                detail.append(
                    f"   (b) {inst} {date}: an opposite-side signal came earlier (not a fault by itself)"
                )
            gap = (tsn[e] - dec[sel[0]]) / 6e10
            sigbar_start = pd.Timestamp(dec[sel[0]], tz="UTC").tz_convert(
                m1.index.tz
            ) - pd.Timedelta(minutes=tfm)
            # the signal bar is complete at dec: its last minute is dec - 1 minute and present
            day_m = m1[np.array([x.date() for x in m1.index]) == date]
            on_grid = ((sigbar_start.hour * 60 + sigbar_start.minute) - (9 * 60 + 15)) % tfm == 0
            bar_min = day_m[
                (day_m.index >= sigbar_start)
                & (day_m.index < sigbar_start + pd.Timedelta(minutes=tfm))
            ]
            complete = len(bar_min) > 0 and bar_min.index[-1] < m1.index[e]
            okB += int(gap == 0 and on_grid and complete)
            n_early += gap < 0
            n_late += gap > 0
            # (c) the entry price: the open of that minute; check through exits whose price is known
            hold = int(tr.held)
            xi = e + hold
            if int(tr.why) in (5, 6) and xi < len(
                m1
            ):  # strategy exit or session exit: open of a minute
                s_open = float(m1["open"].iloc[xi])
                implied_open = s_open / (1 + side * tr.bp / 1e4)
                real_open = float(m1["open"].iloc[e])
                nC += 1
                okC += abs(implied_open / real_open - 1) < 1e-7
                prev_open = float(m1["open"].iloc[e - 1])
                next_open = float(m1["open"].iloc[e + 1])
                if abs(implied_open / real_open - 1) >= 1e-7:
                    detail.append(f"   (c) {inst} {date}: implied {implied_open:.2f} real {real_open:.2f} "
                                  f"prev {prev_open:.2f} next {next_open:.2f}")  # fmt: skip
            # (b2) mean_reversion: my RSI(2) on the signal bar
            if name == "mean_reversion":
                five = h_resample(m1, tfm)
                rs = rsi_wilder(five["close"].to_numpy(float), 2)
                pos = five.index.get_indexer([sigbar_start])[0]
                if pos >= 0 and np.isfinite(rs[pos]):
                    nR += 1
                    okR += (rs[pos] < 10) if side == 1 else (rs[pos] > 90)
        print(f"  (a) condition label from daily bars before the date: {okA} of 30 agree")
        print(f"  (b) entry minute == close of a complete, grid-aligned signal bar: {okB} of 30 "
              f"(earlier {n_early}, later {n_late})")  # fmt: skip
        print(f"  (c) entry open implied by the exit price and bp equals the real open: {okC} of {nC} "
              f"(trades whose exit price is an open: strategy or session exit)")  # fmt: skip
        if nR:
            print(f"  (b2) own RSI(2) on the signal bar is beyond the entry level: {okR} of {nR}")
        for x in detail[:12]:
            print(x)
    _ = ind


def h_resample(m1: pd.DataFrame, tfm: int) -> pd.DataFrame:
    g = m1.copy()
    o = g.index.normalize() + pd.Timedelta(hours=9, minutes=15)
    k = ((g.index - o) // pd.Timedelta(minutes=tfm)).to_numpy()
    key = pd.MultiIndex.from_arrays([g.index.normalize(), k])
    agg = g.groupby(key).agg({"open": "first", "high": "max", "low": "min", "close": "last"})
    start = [d + pd.Timedelta(hours=9, minutes=15 + int(kk) * tfm) for d, kk in agg.index]
    agg.index = pd.DatetimeIndex(start)
    return agg


def rsi_wilder(c: np.ndarray, n: int) -> np.ndarray:
    d = np.diff(c, prepend=np.nan)
    up, dn = np.where(d > 0, d, 0.0), np.where(d < 0, -d, 0.0)
    ru = pd.Series(up).ewm(alpha=1 / n, adjust=False).mean().to_numpy()
    rd = pd.Series(dn).ewm(alpha=1 / n, adjust=False).mean().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        return 100 - 100 / (1 + ru / rd)


def part_b4() -> None:
    import yaml

    cfg = yaml.safe_load(open(MAIN / "config" / "costs.yaml"))["products"]["equity_intraday"]
    b, t = cfg["brokerage"], cfg["taxes"]
    slip = 0.0003
    print("equity_intraday:", b, "slippage a side", slip)
    for px in (300.0, 1300.0, 13000.0, 1000.0):
        ef, xf = px * (1 + slip), px * (1 - slip)  # a long, flat price

        def leg(turn: float, sell: bool, b=b, t=t) -> float:
            br = min(b["per_order_inr"], b["pct_of_turnover"] * turn, b["cap_inr"])
            fees = (t["exchange_txn_pct"] + t["sebi_turnover_pct"] + t["ipft_pct"]) * turn
            tot = br + fees + t["gst_pct"] * (br + fees)
            tot += (t["stt_sell_pct"] if sell else t["stt_buy_pct"]) * turn
            if not sell:
                tot += t["stamp_duty_pct"] * turn
            return tot

        charges = leg(ef, False) + leg(xf, True)
        slip_rs = (ef - px) + (px - xf)
        print(f"  price {px:>7.0f}: charges Rs {charges:.3f} = {1e4 * charges / px:.2f} bp; "
              f"slippage {1e4 * slip_rs / px:.2f} bp; round trip {1e4 * (charges + slip_rs) / px:.2f} bp")  # fmt: skip
    print("  study:", json.loads((REG / "costs.json").read_text()))


# ═════════════════════════════ Part C: the mock instance ═════════════════════════════
def part_c() -> None:
    base = "http://127.0.0.1:8795"

    def get(path: str) -> tuple[int, object]:
        assert ":8795" in base
        try:
            with urllib.request.urlopen(base + path, timeout=60) as r:  # noqa: S310
                txt = r.read().decode()
                return r.status, json.loads(
                    txt, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c))
                )
        except urllib.error.HTTPError as ex:
            return ex.code, ex.read().decode()[:200]

    code, ins = get("/api/mllab/states/instruments")
    print("instruments:", code, ins if code != 200 else [(i["key"], i["kind"]) for i in ins["instruments"]]
          if isinstance(ins, dict) and "instruments" in ins else str(ins)[:300])  # fmt: skip
    items = ins["instruments"] if isinstance(ins, dict) and "instruments" in ins else ins
    for i in items:
        code, js = get(f"/api/mllab/states?instrument={i['key']}")
        if code != 200:
            print(i["key"], code, js)
            continue
        print(i["key"], code, "keys", sorted(js)[:20], "sessions", len(js.get("sessions", [])),
              "note:", js.get("note"), "wf:", js.get("walk_forward"), "next:", (js.get("next") or {}).get("conviction"))  # fmt: skip
    print("unknown:", get("/api/mllab/states?instrument=NOPE_XYZ"))


if __name__ == "__main__":
    {"A": part_a, "B1": part_b1, "B3": part_b3, "B4": part_b4, "C": part_c}[sys.argv[1]]()
