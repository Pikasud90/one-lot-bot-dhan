"""Lead's check of `microtrend_study.py`, written independently of it.

1. The study's headline figures, recomputed from its raw outputs.
2. Like-for-like situation cuts. The trades that could not be priced on real premiums are
   the large moves (the strike left the ATM+-10 window Dhan serves), so "real, priced only"
   understates the rule. Here a trade counts at its real premium result where it was priced
   and at its model result less the mean real-minus-model gap where it was not.
3. A spot check: 30 priced trades looked up in the option store with a reader of its own
   (no project code), to confirm the entry premium the study used and its friction.

Run from the main checkout after the study's `trades` and `price` stages (read-only):

    .venv/bin/python -W ignore scripts/microtrend_study_check.py
"""

from __future__ import annotations

import datetime as dt
import io
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path.cwd()
OUT = ROOT / "data" / "research" / "microtrend_2026-11-13"
STORE = ROOT / "data" / "expired_options.db"
REAL_INDICES = ["NIFTY_OPT", "BANKNIFTY_OPT"]
WEEKDAYS = "Mon Tue Wed Thu Fri".split()


def tday(v: pd.Series, d: pd.Series) -> float:
    """t of the mean with all trades of one entry day as one draw."""
    x = np.asarray(v, float)
    _, inv = np.unique(np.asarray(d), return_inverse=True)
    dev = np.bincount(inv, x) - x.mean() * np.bincount(inv)
    se = float(np.sqrt((dev**2).sum())) / len(x)
    return float(x.mean() / se) if se > 0 else float("nan")


def line(name: str, d: pd.DataFrame, col: str) -> str:
    if len(d) < 3:
        return f"{name:36s} n={len(d):5d}"
    per = " / ".join(f"{d.loc[d.period == k, col].mean():+.2f}" for k in range(3))
    return (
        f"{name:36s} n={len(d):5d}  {d[col].mean():+6.2f}% (t {tday(d[col], d.day):+5.1f})  "
        f"winners {100 * (d[col] > 0).mean():4.1f}%  {per}"
    )


def cuts(d: pd.DataFrame) -> list[tuple[str, pd.Series]]:
    m = d.minute
    return [
        ("ALL", d.side != 0),
        ("VIX above its one-year median", d.vix > d.vmed),
        ("VIX at or below its median", d.vix <= d.vmed),
        ("entry 09:30 to 10:29", (m >= 570) & (m < 630)),
        ("entry 10:30 to 11:59", (m >= 630) & (m < 720)),
        ("entry 12:00 to 13:29", (m >= 720) & (m < 810)),
        ("entry 13:30 to 14:45", m >= 810),
        ("calls", d.side == 1),
        ("puts", d.side == -1),
        ("with the previous day's move", np.sign(d.pcc) == d.side),
        ("against the previous day's move", np.sign(d.pcc) != d.side),
        ("yesterday's range over 1.2 x normal", d.rr > 1.2),
        ("yesterday's range up to 1.2 x normal", d.rr <= 1.2),
        ("4h trend turned on its last bar", d.age4h == 0),
        ("60m trend 0 to 2 bars old", d.age60 <= 2),
        *[(WEEKDAYS[k], d.wd == k) for k in range(5)],
    ]


def spot_check(b: pd.DataFrame, n: int = 30) -> None:
    """Find each sampled trade's entry and exit bar in the store and price it raw."""
    db = sqlite3.connect(f"file:{STORE}?mode=ro", uri=True)
    cache: dict[tuple[str, str, str], list] = {}

    def blocks(und: str, opt: str, day: str) -> list:
        key = (und, opt, day)
        if key not in cache:
            lo = (dt.date.fromisoformat(day) - dt.timedelta(days=45)).isoformat()
            rows = db.execute(
                "select code, data from blocks where underlying=? and flag='MONTH' and opt=?"
                " and day<=? and day>=? and rows>0",
                (und, opt, day, lo),
            ).fetchall()
            cache[key] = []
            for code, blob in rows:
                z = np.load(io.BytesIO(blob))
                cache[key].append((code, {name: z[name] for name in z.files}))
        return cache[key]

    def look(und: str, opt: str, day: str, ts: int, strike: float) -> list[tuple]:
        hits = []
        for code, z in blocks(und, opt, day):
            i = np.flatnonzero(z["ts"] == ts)
            if i.size and abs(float(z["strike"][i[0]]) - strike) < 0.01:
                hits.append((code, float(z["open"][i[0]]), float(z["close"][i[0]])))
        return hits

    same = found = 0
    raw, real = [], []
    for t in b[b.real_ret.notna()].sample(n, random_state=7).itertuples():
        und, opt = t.inst.replace("_OPT", ""), ("CE" if t.side == 1 else "PE")
        ent = [h for h in look(und, opt, t.day, t.e0, t.strike) if abs(h[1] - t.real_entry) < 0.011]
        if not ent:
            continue
        same += 1
        ex = [h for h in look(und, opt, t.day, t.x0, t.strike) if h[0] == ent[0][0]]
        if not ex:
            continue
        found += 1
        px = ex[0][2] if t.how == "close" else ex[0][1]
        raw.append((px / ent[0][1] - 1) * 100)
        real.append(t.real_ret)
    gap = np.array(raw) - np.array(real)
    print(f"spot check: entry bar found at the study's entry premium {same} of {n}; exit {found}")
    print(
        f"   raw premium move less the study's net result (its friction), points: mean "
        f"{gap.mean():.2f}, min {gap.min():.2f}, max {gap.max():.2f}; "
        f"correlation {np.corrcoef(raw, real)[0, 1]:.4f}"
    )


def main() -> None:
    df = pd.read_csv(OUT / "trades.csv")
    rows = [json.loads(x) for x in (OUT / "real.jsonl").read_text().splitlines() if x.strip()]
    real = pd.DataFrame(rows)[["inst", "e0", "real_ret", "real_entry"]]
    base = df[df.need == 3]
    b = base[base.inst.isin(REAL_INDICES)].merge(real, on=["inst", "e0"], how="left")
    pr, un = b[b.real_ret.notna()], b[b.real_ret.isna()]
    gap = float((pr.real_ret - pr.ret).mean())

    print("1. THE STUDY'S HEADLINE FIGURES, RECOMPUTED")
    for need in (3, 2, 4):
        print(line(f"model, five indices, exit {need} of 7", df[df.need == need], "ret"))
    print(line("model, NIFTY + BANKNIFTY, all", b, "ret"))
    print(line("model, the priced trades", pr, "ret"))
    print(line("REAL, the priced trades", pr, "real_ret"))
    print(line("model, the unpriced trades", un, "ret"))
    print(
        f"real minus model on the same trades: mean {gap:+.2f}, median "
        f"{(pr.real_ret - pr.ret).median():+.2f} (t {tday(pr.real_ret - pr.ret, pr.day):+.1f}); "
        f"calls {(pr.real_ret - pr.ret)[pr.side == 1].mean():+.2f}, "
        f"puts {(pr.real_ret - pr.ret)[pr.side == -1].mean():+.2f}"
    )

    b = b.assign(est=b.real_ret.fillna(b.ret + gap))
    five = base.assign(est=base.ret + gap)
    print("\n2. LIKE FOR LIKE: real where priced, model less the gap where not")
    print("   NIFTY + BANKNIFTY")
    for name, m in cuts(b):
        print(line(name, b[m], "est"))
    print("   five indices (model less the gap: an estimate)")
    for name, m in cuts(five):
        print(line(name, five[m], "est"))
    print("   the most favourable mix, chosen AFTER seeing the cuts (an upper bound)")
    for name, d in (("NIFTY + BANKNIFTY", b), ("five indices", five)):
        m = (d.vix <= d.vmed) & ~d.wd.isin([1, 2]) & (d.minute >= 630)
        print(line(f"{name}: low VIX, not Tue/Wed, 10:30+", d[m], "est"))
    print()
    spot_check(b)


if __name__ == "__main__":
    main()
