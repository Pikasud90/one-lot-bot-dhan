"""Study of the owner's `microtrend` options rule: real premiums, situations, exit variants.

Rule (as in `microtrend_backtest.py`): Supertrend(14, 1.7) on closed 3m, 5m, 10m, 15m, 30m,
60m and 4h bars; entry at a 3-minute close where all seven newly agree (call up, put down),
09:30 to 14:45, one trade at a time per index; exit at the first 3-minute close where at
least 3 of 7 point against, or at 15:15.

Stages (run from the MAIN checkout, PYTHONPATH=src:scripts:.):
  trades  every trade of the rule (exit 2, 3 and 4 of 7) on five indices, the model result
          and the entry-minute features; saved to OUT/trades.csv
  price   real premiums for NIFTY and BANKNIFTY (exit 3 of 7) from Dhan expired options:
          the store first, missing blocks through the project's research adapter at
          2 requests a second; no fetching at or after 08:50 IST. Trades go in passes of
          every 4th trade (offsets 0, 2, 1, 3), so a stopped run leaves an even sample.
  report  the tables, to OUT/report.txt
Market data is read only; `data/onelot.db` is never written.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sys
import time
from pathlib import Path

# the data and the reference modules live in the checkout this runs from (the main one)
sys.path[:0] = [str(Path.cwd() / "src"), str(Path.cwd() / "scripts")]

import microtrend_backtest as mb  # noqa: E402
import mtf_bruteforce as h  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

OUT = h.ROOT / "data" / "research" / "microtrend_2026-11-13"
TRADES = OUT / "trades.csv"
REAL = OUT / "real.jsonl"
NAMES = ("TUNE", "CHOOSE", "TEST")
DEADLINE = dt.time(8, 50)
K = 4
PASSES = (0, 2, 1, 3)
REAL_SERIES = {"NIFTY_OPT": "NIFTY", "BANKNIFTY_OPT": "BANKNIFTY"}


def now_ist() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30)))


# ───────────────────────────── trades and features ─────────────────────────


def tf_age_atr(p: h.Prep, tf: str, e: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Bars since the trend of `tf` turned (0 = it turned on the last closed bar) and the
    ATR(14, Wilder) of `tf` on the last bar closed at the entry minute's open."""
    tt = p.ladder[tf]
    d = tt.direction
    nb = len(d)
    rs = np.zeros(nb, dtype=np.int64)
    for i in range(1, nb):
        rs[i] = i if d[i] != d[i - 1] else rs[i - 1]
    a = np.searchsorted(p.ns, tt.start_ns)
    b = np.searchsorted(p.ns, tt.close_ns)
    ok = b > a
    hi = np.full(nb, np.nan)
    lo = np.full(nb, np.nan)
    cl = np.full(nb, np.nan)
    for i in np.flatnonzero(ok):
        hi[i], lo[i], cl[i] = p.h[a[i] : b[i]].max(), p.l[a[i] : b[i]].min(), p.c[b[i] - 1]
    pc = np.r_[np.nan, cl[:-1]]
    tr = np.nanmax(np.vstack([hi - lo, np.abs(hi - pc), np.abs(lo - pc)]), axis=0)
    atr = np.full(nb, np.nan)
    acc = None
    cnt = 0
    for i in range(nb):
        if not np.isfinite(tr[i]):
            atr[i] = acc if acc is not None else np.nan
            continue
        cnt += 1
        acc = tr[i] if acc is None else (acc * 13 + tr[i]) / 14 if cnt > 14 else (
            (acc * (cnt - 1) + tr[i]) / cnt)  # fmt: skip
        atr[i] = acc if cnt >= 14 else np.nan
    pos = np.searchsorted(tt.close_ns, p.ns[e], side="right") - 1
    pos = np.maximum(pos, 0)
    return pos - rs[pos], atr[pos]


def build_trades() -> pd.DataFrame:
    h._init_worker()
    vix = h._cache.vix().dropna()
    vix.index = pd.DatetimeIndex([pd.Timestamp(x).date() for x in vix.index])
    vmed = vix.rolling(250, min_periods=120).median()
    out = []
    for inst in h.INDEX_OPTIONS:
        t0 = time.time()
        p = h.prepare(inst, tfs=mb.TFS)
        first = p.sess_first[p.sess]
        bnd = ((np.arange(p.n) - first) % 3 == 0) & (np.arange(p.n) > first)
        up = (p.D == 1).all(axis=0)
        dn = (p.D == -1).all(axis=0)
        newal = np.zeros(p.n, bool)  # a new all-seven alignment at a 3-minute close
        idx = np.flatnonzero(bnd)
        prev_u = np.r_[False, up[idx][:-1]]
        prev_d = np.r_[False, dn[idx][:-1]]
        newal[idx] = (up[idx] & ~prev_u) | (dn[idx] & ~prev_d)
        len(p.sess_first)
        sc = p.c[p.sess_last]
        rng = np.array([(p.h[a : b + 1].max() - p.l[a : b + 1].min()) / p.c[b]
                        for a, b in zip(p.sess_first, p.sess_last, strict=True)])  # fmt: skip
        rmean = pd.Series(rng).rolling(20).mean().to_numpy()
        days = np.array([pd.Timestamp(d) for d in p.sess_dates])
        # the previous day's VIX and its 250-session median up to that day (no look-ahead)
        k = np.searchsorted(vix.index.values, days.astype("datetime64[ns]"), side="left") - 1
        vprev = np.where(k >= 0, vix.to_numpy()[np.maximum(k, 0)], np.nan)
        mprev = np.where(k >= 0, vmed.to_numpy()[np.maximum(k, 0)], np.nan)
        for need in (2, 3, 4):
            tr = mb.trades_of(p, need)
            e, x, s, r = tr["e"], tr["x"], tr["s"], tr["r"]
            keep = p.year[e] >= 0
            e, x, s, r = e[keep], x[keep], s[keep], r[keep]
            ret = np.empty(len(e))
            strike = np.empty(len(e))
            px = np.where(x == p.xi[p.sess[e]], p.xp[p.sess[e]], p.o[x])
            for side in (1, -1):
                m = s == side
                if m.any():
                    ret[m] = h.pnl(p, side, 0, e[m], x[m], px[m], p.ns[x[m]])["ret"]
                    strike[m] = p.opt(side).strike[e[m]]
            ss = p.sess[e]
            gap = p.o[p.sess_first[ss]] / sc[np.maximum(ss - 1, 0)] - 1
            pcc = sc[np.maximum(ss - 1, 0)] / sc[np.maximum(ss - 2, 0)] - 1
            age60, atr60 = tf_age_atr(p, "60m", e)
            age4h, _ = tf_age_atr(p, "240m", e)
            dist = s * (p.o[e] - p.L[5][e]) / atr60
            firsts = np.array(
                [not newal[p.sess_first[q] : j].any() for q, j in zip(ss, e, strict=False)]
            )
            rr = rng[np.maximum(ss - 1, 0)] / rmean[np.maximum(ss - 1, 0)]
            how = np.where((x == p.xi[ss]) & p.xclose[ss], "close", "open")
            out.append(pd.DataFrame({
                "inst": inst, "need": need, "day": [str(p.sess_dates[q]) for q in ss],
                "period": mb.PERIOD[p.year[e]], "side": s, "why": r,
                "minute": p.minute[e], "ret": ret,
                "bp": s * (px / p.o[e] - 1) * 1e4,
                "held": x - e, "e0": p.ns[e] // 1_000_000_000, "x0": p.ns[x] // 1_000_000_000,
                "strike": strike, "how": how, "level": px,
                "gap": gap, "pcc": pcc, "vix": vprev[ss], "vmed": mprev[ss],
                "age60": age60, "age4h": age4h, "dist60": dist, "first": firsts,
                "rr": rr, "wd": [p.sess_dates[q].weekday() for q in ss],
            }))  # fmt: skip
        print(f"{inst}: {time.time() - t0:.0f}s", flush=True)
        del p
    df = pd.concat(out, ignore_index=True)
    df.to_csv(TRADES, index=False)
    return df


# ───────────────────────────── real premiums ───────────────────────────────


def price() -> None:
    import mtf_real_premiums as mr
    from expired_options_fetch import STORE_PATH, research_adapter, underlyings

    from onelot.broker.base import TokenExpiredError
    from onelot.config import CostConfig
    from onelot.costs import CostModel
    from onelot.data.expired_options import OptionBarStore
    from onelot.lab.real_options import NeedBlock

    h._init_worker()
    df = pd.read_csv(TRADES)
    done = set()
    if REAL.exists():
        done = {(r["inst"], r["e0"]) for r in map(json.loads, REAL.read_text().splitlines())}
    logf = (OUT / "price.log").open("a", encoding="utf-8")

    def log(msg: str) -> None:
        line = f"[{now_ist():%H:%M:%S} IST] {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    state = {"stop": None}
    try:
        adapter = research_adapter(2.0)
    except Exception as exc:  # noqa: BLE001
        adapter = None
        state["stop"] = f"adapter not built ({type(exc).__name__}); store only"
        log(state["stop"])

    class StudyPricer(mr.CarryPricer):
        def _block(self, key, day):  # type: ignore[no-untyped-def]
            if self.source is not None and now_ist().time() >= DEADLINE:
                self.source = None
                state["stop"] = state["stop"] or f"deadline {DEADLINE} reached"
            try:
                return super()._block(key, day)
            except TokenExpiredError:
                self.source = None
                state["stop"] = "Dhan answered 401; store only"
                raise NeedBlock(key, day) from None

    store = OptionBarStore(STORE_PATH)
    unds = underlyings()
    bt = h._settings.backtest
    pricers = {}
    for inst, series in REAL_SERIES.items():
        spec = h._cache.spec(inst)
        bars = h._cache.bars(spec.series_key, "1m")
        bars = bars[bars.index < pd.Timestamp(h.T_END + dt.timedelta(days=1), tz=bars.index.tz)]
        costs = CostModel(CostConfig.load(
            h._settings.project_root / "config" / "costs.yaml").product(spec.product))  # fmt: skip
        pricers[inst] = StudyPricer(unds[series], spec, bt, costs, store, bars, 1,
                                    source=adapter, slip_pct=0.0)  # fmt: skip
    base = df[(df["need"] == 3) & df["inst"].isin(list(REAL_SERIES))].sort_values(["inst", "e0"])
    t0 = time.time()
    with REAL.open("a", encoding="utf-8") as fh:
        for off in PASSES:
            if state["stop"] and adapter is not None:
                break
            for inst in REAL_SERIES:
                sub = base[base["inst"] == inst].reset_index(drop=True)
                rp = pricers[inst]
                for i in range(off, len(sub), K):
                    t = sub.iloc[i]
                    if (inst, int(t["e0"])) in done:
                        continue
                    model = {"ret": float(t["ret"]), "inr": 0.0}
                    try:
                        real = rp.price_one(
                            int(t["e0"]),
                            int(t["x0"]),
                            float(t["strike"]),
                            int(t["side"]),
                            t["how"],
                            float(t["level"]),
                            model,
                        )
                    except NeedBlock:
                        real = {"unpriced": "block not fetched"}
                    except Exception as exc:  # noqa: BLE001
                        real = {"unpriced": f"error: {type(exc).__name__}: {str(exc)[:80]}"}
                    row = {
                        "inst": inst,
                        "e0": int(t["e0"]),
                        "pass": off,
                        "stopped": bool(state["stop"]),
                        "real_ret": real.get("real_ret"),
                        "unpriced": real.get("unpriced"),
                        "real_entry": real.get("real_entry"),
                    }
                    fh.write(json.dumps(row, default=str) + "\n")
                    fh.flush()
                req = sum(q.requests for q in pricers.values())
                log(f"pass {off} {inst} done; requests so far {req}; {time.time() - t0:.0f}s; "
                    f"stop={state['stop']}")  # fmt: skip
    log(f"END requests {sum(q.requests for q in pricers.values())} stop={state['stop']}")


# ───────────────────────────── report ─────────────────────────────────────


def day_t(x: np.ndarray, d: np.ndarray) -> float:
    n = len(x)
    if n < 3:
        return float("nan")
    m = x.mean()
    g = pd.DataFrame({"d": d, "x": x}).groupby("d")["x"].agg(["sum", "count"])
    dev = g["sum"].to_numpy() - m * g["count"].to_numpy()
    se = math.sqrt(float((dev * dev).sum())) / n
    return m / se if se > 0 else float("nan")


def stats(d: pd.DataFrame, col: str = "ret") -> dict:
    x = d[col].to_numpy(float)
    per = [d.loc[d["period"] == k, col].mean() if (d["period"] == k).any() else np.nan
           for k in range(3)]  # fmt: skip
    return {"n": len(d), "m": x.mean() if len(x) else np.nan,
            "t": day_t(x, d["day"].to_numpy()), "bp": d["bp"].mean(),
            "w": (x > 0).mean() * 100 if len(x) else np.nan, "per": per}  # fmt: skip


def fmt(label: str, s: dict, mark: str = "") -> str:
    p = " / ".join(f"{v:+6.2f}" for v in s["per"])
    return (f"{label:<26}{s['n']:>6}  {s['m']:+6.2f}% (t {s['t']:+5.1f})  {s['bp']:+6.1f} bp  "
            f"win {s['w']:4.1f}%  {p}  {mark}")  # fmt: skip


def cuts(d: pd.DataFrame) -> list[tuple[str, pd.Series]]:
    m = d["minute"]
    gap_with = np.sign(d["gap"]) == d["side"]
    pcc_with = np.sign(d["pcc"]) == d["side"]
    wd = "Mon Tue Wed Thu Fri".split()
    return [
        ("09:30 to 10:29", (m >= 570) & (m < 630)),
        ("10:30 to 11:59", (m >= 630) & (m < 720)),
        ("12:00 to 13:29", (m >= 720) & (m < 810)),
        ("13:30 to 14:45", m >= 810),
        ("call", d["side"] == 1), ("put", d["side"] == -1),
        ("with the gap", gap_with), ("against the gap", ~gap_with),
        ("with prev day", pcc_with), ("against prev day", ~pcc_with),
        ("VIX above median", d["vix"] > d["vmed"]), ("VIX below median", d["vix"] <= d["vmed"]),
        ("60m age 0 to 2", d["age60"] <= 2), ("60m age 3 to 6", d["age60"].between(3, 6)),
        ("60m age 7+", d["age60"] >= 7),
        ("4h age 0", d["age4h"] == 0), ("4h age 1 to 2", d["age4h"].between(1, 2)),
        ("4h age 3+", d["age4h"] >= 3),
        ("60m dist < 1 ATR", d["dist60"] < 1), ("60m dist 1 to 2 ATR", d["dist60"].between(1, 2)),
        ("60m dist > 2 ATR", d["dist60"] > 2),
        ("first alignment", d["first"].astype(bool)), ("later alignment", ~d["first"].astype(bool)),
        ("yday range < 0.7", d["rr"] < 0.7), ("yday range 0.7 to 1.2", d["rr"].between(0.7, 1.2)),
        ("yday range > 1.2", d["rr"] > 1.2),
        *[(wd[k], d["wd"] == k) for k in range(5)],
        *[(i.replace("_OPT", ""), d["inst"] == i) for i in h.INDEX_OPTIONS],
    ]  # fmt: skip


def report() -> None:
    df = pd.read_csv(TRADES)
    L: list[str] = []  # noqa: N806
    w = L.append
    hdr = f"{'':<26}{'trades':>6}  {'% premium (t)':>18}  {'index':>9}  {'winners':>9}  TUNE / CHOOSE / TEST"
    base = df[df["need"] == 3].copy()
    w("MODEL, all five indices, exit 3 of 7 (base) and the exit variants")
    w(hdr)
    for need in (3, 2, 4):
        d = df[df["need"] == need]
        w(fmt(f"exit {need} of 7: ALL", stats(d)))
        w(fmt(f"exit {need} of 7: calls", stats(d[d["side"] == 1])))
        w(fmt(f"exit {need} of 7: puts", stats(d[d["side"] == -1])))
        w(f"   held {d['held'].mean():.0f} min on average; days {d['day'].nunique()}")
    w("")
    real = None
    if REAL.exists():
        rows = [json.loads(x) for x in REAL.read_text().splitlines() if x]
        r = pd.DataFrame(rows).drop_duplicates(["inst", "e0"], keep="first")
        # a pass counts only if no row of it was written after a stop
        full = [
            p for p in PASSES if p in set(r["pass"]) and not r.loc[r["pass"] == p, "stopped"].any()
        ]
        base2 = base[base["inst"].isin(list(REAL_SERIES))].sort_values(["inst", "e0"]).copy()
        base2["pos"] = base2.groupby("inst").cumcount()
        base2["pass"] = base2["pos"] % K
        smp = base2[base2["pass"].isin(full)].merge(r[["inst", "e0", "real_ret", "unpriced", "real_entry"]],
                                                     on=["inst", "e0"], how="left")  # fmt: skip
        k = {4: 1, 2: 2, 1: 4}.get(len(full), f"passes {full}")
        smp["real_ret"] = pd.to_numeric(smp["real_ret"], errors="coerce")
        pr = smp[smp["real_ret"].notna()].copy()
        un = smp[smp["real_ret"].isna()]
        w(f"REAL PREMIUMS, NIFTY + BANKNIFTY, exit 3 of 7; sample every {k}th trade "
          f"(passes {full}); trades {len(base2)}, in sample {len(smp)}, priced {len(pr)} "
          f"({100 * len(pr) / max(len(smp), 1):.0f}%)")  # fmt: skip
        w(f"{'':<22}{'trades':>6}{'priced':>7}  {'MODEL all':>16}  {'MODEL priced':>16}  "
          f"{'REAL priced':>16}  {'real win':>8}")  # fmt: skip

        def rl(label: str, a: pd.DataFrame) -> None:
            b = a[a["real_ret"].notna()]

            def c(z: pd.DataFrame, col: str) -> str:
                if len(z) < 3:
                    return f"{'-':>16}"
                return f"{z[col].mean():+6.2f} (t{day_t(z[col].to_numpy(float), z['day'].to_numpy()):+5.1f})"

            w(f"{label:<22}{len(a):>6}{len(b):>7}  {c(a, 'ret')}  {c(b, 'ret')}  {c(b, 'real_ret')}  "
              f"{(b['real_ret'] > 0).mean() * 100 if len(b) else np.nan:7.1f}%")  # fmt: skip

        rl("ALL", smp)
        for kk, nm in enumerate(NAMES):
            rl(nm, smp[smp["period"] == kk])
        for inst in REAL_SERIES:
            rl(inst.replace("_OPT", ""), smp[smp["inst"] == inst])
        rl("calls", smp[smp["side"] == 1])
        rl("puts", smp[smp["side"] == -1])
        rl("exit 3 of 7 reversed", smp[smp["why"] == 1])
        rl("exit at 15:15", smp[smp["why"] == 0])
        w(f"model winners on priced {100 * (pr['ret'] > 0).mean():.1f}%; unpriced trades "
          f"{len(un)}: model {un['ret'].mean():+.2f}% (t {day_t(un['ret'].to_numpy(float), un['day'].to_numpy()):+.1f}), "
          f"index {un['bp'].mean():+.1f} bp against priced {pr['bp'].mean():+.1f} bp")  # fmt: skip
        if len(un):
            g = un["unpriced"].fillna("?").str.replace(r"(CE|PE) [\d.]+", r"\1", regex=True)
            g = g.str.slice(0, 60).value_counts().head(5)
            for why, c in g.items():
                w(f"   unpriced {c:>4}  {why}")
        if len(pr):
            w(f"real minus model per priced trade: mean "
              f"{(pr['real_ret'] - pr['ret']).mean():+.2f}, median {(pr['real_ret'] - pr['ret']).median():+.2f}")  # fmt: skip
            bl = smp.copy()
            bl["ret"] = bl["real_ret"].fillna(bl["ret"])  # unpriced trades at their model result
            w(fmt("BLEND real+model unpriced", stats(bl)))
        real = pr
        w("")
    w("CUTS (model, all 3,824 base trades); HOLDS = positive in all three periods and t >= 2")
    w(hdr)
    held = 0
    cl = cuts(base)
    for name, m in cl:
        s = stats(base[m])
        ok = all(v > 0 for v in s["per"]) and s["t"] >= 2
        held += ok
        w(fmt(name, s, "HOLDS" if ok else ""))
    w(f"cells {len(cl)}, holding {held}")
    if real is not None and len(real):
        w("")
        w("CUTS on the real-priced subset (REAL % premium), cells with at least 80 trades")
        rr = real.copy()
        rr["ret_model"] = rr["ret"]
        rr["ret"] = rr["real_ret"]
        n80 = held_r = 0
        for name, m in cuts(rr):
            sub = rr[m]
            if len(sub) < 80:
                continue
            n80 += 1
            s = stats(sub)
            ok = all(v > 0 for v in s["per"]) and s["t"] >= 2
            held_r += ok
            w(fmt(name, s, ("HOLDS " if ok else "") + f"model {sub['ret_model'].mean():+.2f}"))
        w(f"cells {n80}, holding {held_r}")
    text = "\n".join(L) + "\n"
    (OUT / "report.txt").write_text(text, encoding="utf-8")
    print(text)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    stages = sys.argv[1:] or ["trades", "report"]
    if "trades" in stages:
        build_trades()
    if "price" in stages:
        price()
    if "report" in stages:
        report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
