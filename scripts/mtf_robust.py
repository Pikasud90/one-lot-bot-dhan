"""Stage-2 finalists under stress and in depth (trade level).

Stage 2 (`mtf_stage2.py`) ranked about 15,000 structures per basket from yearly sums. This
script takes the few that matter - the top of the pre-declared ranking, its neighbours, the
paper slots under the owner's logic and the owner's plain rule - and looks at their trades:

* stress: costs x 1.5 and x 2, the entry one and two minutes late, the three ways of acting
  on the line (resting stop / next-minute alert / the flip), yesterday's VIX for the option
  model, and six Supertrend settings around the owner's (14, 1.7);
* the no-signal benchmark of the same side and nearest holding time;
* the PAPER-TEST CANDIDATE test written down in NOTES.md before stage 2 ran;
* depth: by year, by instrument, the distribution of results, how much of the profit a few
  trades carry, the rupee path at one unit (drawdown, months), holding time, entry time.

    .venv/bin/python scripts/mtf_robust.py --run [--workers 4]
    .venv/bin/python scripts/mtf_robust.py --report

Evidence about rules on past data, after modelled costs; not advice, not a forecast.
"""

from __future__ import annotations

import argparse
import math
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mtf_bruteforce as h  # noqa: E402
import mtf_stage2 as s2  # noqa: E402

OUT = h.OUT
STORE = OUT / "robust"
DEST = OUT / "mtf_robust.txt"

#: basket -> (key, structure as `mtf_stage2.name2` prints it, why it is here)
FINALISTS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "INDEX_OPTIONS": (
        ("carry30", "LONG 30m any/daily none gate[all six] trail 30m close <=14:45 carry",
         "top of the pre-declared ranking = the owner's rule, carried"),
        ("carry60", "LONG 60m any/once none gate[all six] trail 60m touch <=14:45 carry",
         "eligible neighbour on the 60m line"),
        ("carry15_volc", "LONG 15m flip<=2 vol_c gate[all six] trail 60m close <=14:45 carry",
         "positive in all three periods - but found WITH the test year"),
        ("intra_volc", "LONG 30m flip<=4 vol_c gate[all six] trail 1d close <=13:00 intra",
         "eligible, same day only (in effect: hold to the close)"),
        ("intra_15adx", "LONG 15m flip<=2 adx_a gate[E only] trail 15m touch <=13:00 intra",
         "top eligible same-day structure"),
        ("slot_vol", "LONG 30m flip<=2 vol_a gate[E only] trail 30m touch <=13:00 intra",
         "the supertrend_vol paper slot under the owner's logic"),
        ("owner_intra", "LONG 30m any/daily none gate[all six] trail 30m close <=14:45 intra",
         "the owner's rule, same day only"),
        ("owner_puts", "SHORT 30m any/daily none gate[all six] trail 30m close <=14:45 carry",
         "the owner's rule on the put side, carried"),
    ),
    "STOCKS": (
        ("carry60_adx", "LONG 60m flip<=2 adx_b gate[E+faster] trail 60m close <=14:45 carry",
         "top of the pre-declared ranking"),
        ("slot_adx", "SHORT 30m flip<=4 adx_a gate[E only] trail 30m close <=11:30 intra",
         "the supertrend_adx paper slot under the owner's logic"),
        ("slot_vol", "SHORT 60m flip<=4 vol_a gate[E only] trail 60m touch <=11:30 intra",
         "the supertrend_vol paper slot under the owner's logic"),
        ("owner_long", "LONG 30m any/daily none gate[all six] trail 30m close <=14:45 intra",
         "the owner's rule, long, same day"),
        ("owner_short", "SHORT 30m any/daily none gate[all six] trail 30m close <=14:45 intra",
         "the owner's rule, short, same day"),
    ),
}  # fmt: skip

RUN_VARIANTS: tuple[tuple[str, dict], ...] = (
    ("base", {}),
    ("costs x 1.5", {"cost_mult": 1.5}),
    ("costs x 2", {"cost_mult": 2.0}),
    ("entry 1 minute late", {"delay": 1}),
    ("entry 2 minutes late", {"delay": 2}),
    ("line acted on: resting stop", {"exit_mode": h.TOUCH}),
    ("line acted on: next-minute alert", {"exit_mode": h.ALERT}),
    ("line acted on: the flip (close)", {"exit_mode": h.CLOSE}),
)
PREP_VARIANTS: tuple[tuple[str, dict], ...] = (
    ("yesterday's VIX", {"iv_lag": "prev"}),
    ("Supertrend(10, 1.7)", {"period": 10}),
    ("Supertrend(21, 1.7)", {"period": 21}),
    ("Supertrend(14, 1.4)", {"mult": 1.4}),
    ("Supertrend(14, 2.0)", {"mult": 2.0}),
    ("Supertrend(14, 2.5)", {"mult": 2.5}),
    ("Supertrend(10, 3.0)", {"period": 10, "mult": 3.0}),
)
PERIODS = (("TUNE", (0, 1, 2)), ("CHOOSE", (3,)), ("TEST", (4,)), ("ALL", (0, 1, 2, 3, 4)))


def squash(name: str) -> str:
    return " ".join(name.split())


def codes_by_name() -> dict[str, np.ndarray]:
    return {squash(s2.name2(c)): c for c in s2.structures2()}


def basket_of(inst: str) -> str:
    return next(b for b, members in h.BASKETS.items() if inst in members)


def pack(p: h.Prep, tr: dict) -> dict[str, np.ndarray]:
    e = tr["E"]
    return {
        "year": p.year[e].astype(np.int8),
        "day": p.dayoff[e].astype(np.int32),
        "minute": p.minute[e].astype(np.int16),
        "ens": p.ns[e].astype(np.int64),
        "xns": np.asarray(tr["XT"], dtype=np.int64),
        "ret": np.asarray(tr["ret"], dtype=float),
        "inr": np.asarray(tr["inr"], dtype=float),
        "dp": np.asarray(tr["dp"], dtype=float),
        "raw": np.asarray(tr["raw"], dtype=float),
        "sess": (p.sess[tr["X"]] - p.sess[e]).astype(np.int16),
        "hit": np.asarray(tr["hit"], dtype=bool),
    }


def _robust_instrument(inst: str) -> tuple[str, float, int]:
    t0 = time.time()
    names = codes_by_name()
    fins = [(key, names[squash(name)]) for key, name, _ in FINALISTS[basket_of(inst)]]
    rows: list[tuple[str, str, dict]] = []

    def collect(p: h.Prep, variant: str, **kw: object) -> None:
        states: dict[str, s2.EState] = {}
        for key, code in fins:
            tf = s2.E_TFS[int(code[1])]
            if tf not in states:
                states[tf] = s2.entry_state(p, tf)
            rows.append((key, variant, pack(p, s2.trades_for(p, states[tf], code, **kw))))

    p = h.prepare(inst)
    for variant, kw in RUN_VARIANTS:
        collect(p, variant, **kw)
    for side, n_hold in s2.BENCH:
        rows.append((f"bench:{side}:{n_hold}", "base", pack(p, s2.hold_trades(p, side, n_hold))))
    is_option = p.is_option
    del p
    for variant, kw in PREP_VARIANTS:
        if "iv_lag" in kw and not is_option:
            continue
        p = h.prepare(inst, **kw)
        collect(p, variant)
        del p
    STORE.mkdir(parents=True, exist_ok=True)
    with (STORE / f"{inst}.pkl").open("wb") as fh:
        pickle.dump(rows, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return inst, round(time.time() - t0, 1), sum(len(r[2]["ret"]) for r in rows)


def run(workers: int, only: list[str] | None) -> None:
    insts = [i for b in h.BASKETS.values() for i in b if not only or i in only]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers, initializer=h._init_worker) as pool:
        futs = [pool.submit(_robust_instrument, i) for i in insts]
        for k, fut in enumerate(as_completed(futs), 1):
            inst, secs, n = fut.result()
            print(f"[{k}/{len(insts)}] {inst:<16} {secs:>6.1f}s {n:>9,} trades "
                  f"(elapsed {time.time() - t0:.0f}s)", flush=True)  # fmt: skip


# ───────────────────────────── report ─────────────────────────────────────


def load(basket: str) -> dict[tuple[str, str], pd.DataFrame]:
    parts: dict[tuple[str, str], list[pd.DataFrame]] = {}
    for inst in h.BASKETS[basket]:
        f = STORE / f"{inst}.pkl"
        if not f.exists():
            continue
        with f.open("rb") as fh:
            rows = pickle.load(fh)  # noqa: S301 - our own file, written by `--run`
        for key, variant, a in rows:
            df = pd.DataFrame(a)
            df["inst"] = inst
            parts.setdefault((key, variant), []).append(df)
    return {k: pd.concat(v, ignore_index=True) for k, v in parts.items()}


def t_day(df: pd.DataFrame, col: str = "ret") -> float:
    n = len(df)
    if n < 3:
        return float("nan")
    m = float(df[col].mean())
    g = df.groupby("day")[col].agg(["sum", "count"])
    resid = g["sum"].to_numpy() - m * g["count"].to_numpy()
    k = len(g)
    var = float((resid * resid).sum()) / (n * n) * k / max(k - 1, 1)
    return m / math.sqrt(var) if var > 0 else float("nan")


def cell(df: pd.DataFrame, years: tuple[int, ...], col: str = "ret") -> str:
    d = df[df["year"].isin(years)]
    if d.empty:
        return f"{0:>5} {'-':>8} {'':>5}"
    return f"{len(d):>5} {d[col].mean():>+8.3f} {t_day(d, col):>+5.1f}"


def mean_of(df: pd.DataFrame, years: tuple[int, ...], col: str = "ret") -> float:
    d = df[df["year"].isin(years)]
    return float(d[col].mean()) if len(d) else float("nan")


def variant_line(label: str, df: pd.DataFrame, col: str = "ret") -> str:
    cells = " | ".join(cell(df, yrs, col) for _, yrs in PERIODS)
    win = 100.0 * float((df[col] > 0).mean()) if len(df) else float("nan")
    return f"  {label:<33}{cells} | {win:>5.1f} {df['inr'].sum():>+12,.0f}"


HEAD = (
    f"  {'':<33}{'TUNE':^20} | {'CHOOSE':^20} | {'TEST':^20} | {'ALL':^20} | {'win%':>5} "
    f"{'net Rs':>12}\n"
    f"  {'':<33}" + " | ".join(f"{'n':>5} {'mean%':>8} {'t':>5}" for _ in PERIODS)
)


def drawdown(df: pd.DataFrame) -> tuple[float, str, str, int]:
    """Deepest fall of the rupee path (trades in exit order): size, from, to, longest days under."""
    d = df.sort_values("xns")
    eq = d["inr"].cumsum().to_numpy()
    if not len(eq):
        return 0.0, "-", "-", 0
    peak = np.maximum.accumulate(np.maximum(eq, 0.0))
    dd = eq - peak
    j = int(np.argmin(dd))
    i = int(np.argmax(np.maximum(eq[: j + 1], 0.0) == peak[j])) if peak[j] > 0 else 0
    ts = pd.to_datetime(d["xns"].to_numpy(), utc=True).tz_convert("Asia/Kolkata")
    under_start, longest = None, 0
    for k in range(len(eq)):
        if dd[k] < 0 and under_start is None:
            under_start = ts[k]
        if dd[k] >= 0 and under_start is not None:
            longest = max(longest, (ts[k] - under_start).days)
            under_start = None
    if under_start is not None:
        longest = max(longest, (ts[-1] - under_start).days)
    return float(dd[j]), str(ts[i].date()), str(ts[j].date()), longest


def depth(w, df: pd.DataFrame, unit: str, stocks: bool) -> None:  # noqa: ANN001
    r = df["ret"].to_numpy()
    n = len(r)
    if n == 0:
        w("    no trades")
        return
    w("    by year:        " + "   ".join(
        f"Y{y + 1} {int((df['year'] == y).sum()):>4} {mean_of(df, (y,)):>+7.3f}% "
        f"Rs {df.loc[df['year'] == y, 'inr'].sum():>+10,.0f}" for y in range(5)))  # fmt: skip
    w("    by instrument (trades, mean % ALL, mean % TEST, net Rs):")
    per = []
    for inst, d in df.groupby("inst", sort=False):
        per.append(f"{inst.replace('_OPT', '')} {len(d)} {d['ret'].mean():+.2f} "
                   f"{mean_of(d, (4,)):+.2f} {d['inr'].sum():+,.0f}")  # fmt: skip
    for k in range(0, len(per), 4):
        w("      " + "   ".join(per[k : k + 4]))
    pos = int((df.groupby("inst")["ret"].mean() > 0).sum())
    w(f"      instruments positive over five years: {pos} of {df['inst'].nunique()}")
    q = np.percentile(r, [1, 5, 25, 50, 75, 95, 99])
    w(f"    one trade, net %: worst {r.min():+.1f}  1% {q[0]:+.1f}  5% {q[1]:+.1f}  "
      f"25% {q[2]:+.2f}  median {q[3]:+.2f}  75% {q[4]:+.2f}  95% {q[5]:+.1f}  99% {q[6]:+.1f}  "
      f"best {r.max():+.1f}")  # fmt: skip
    wins, losses = r[r > 0], r[r <= 0]
    if len(wins) and len(losses):
        w(f"    winners {len(wins)} ({100 * len(wins) / n:.1f}%) average {wins.mean():+.2f}%; "
          f"losers {len(losses)} average {losses.mean():+.2f}%; payoff ratio "
          f"{wins.mean() / -losses.mean():.2f}")  # fmt: skip
    inr = np.sort(df["inr"].to_numpy())[::-1]
    total = inr.sum()
    srt = np.sort(r)[::-1]
    shares = []
    for frac in (0.01, 0.05, 0.10):
        k = max(int(round(n * frac)), 1)
        shares.append(f"best {int(frac * 100)}% ({k}) Rs {inr[:k].sum():+,.0f}")
    w(f"    concentration: total Rs {total:+,.0f}; " + "; ".join(shares))
    k1 = max(int(round(n * 0.01)), 1)
    w(f"      mean % without the best 5 trades {srt[5:].mean():+.3f}; without the best 1% "
      f"{srt[k1:].mean():+.3f}; without the best 5% {srt[max(int(round(n * 0.05)), 1):].mean():+.3f}; "
      f"without the worst 1% {srt[: n - k1].mean():+.3f}")  # fmt: skip
    dd, d0, d1, under = drawdown(df)
    month = pd.to_datetime(df["xns"].to_numpy(), utc=True).tz_convert("Asia/Kolkata")
    m = df.groupby(month.strftime("%Y-%m"))["inr"].sum()
    w(f"    rupees at one {unit} per signal: total {total:+,.0f}; deepest fall {dd:+,.0f} "
      f"({d0} to {d1}); longest time below an earlier high {under} days")  # fmt: skip
    w(f"      months with trades {len(m)}, positive {int((m > 0).sum())} "
      f"({100 * (m > 0).mean():.0f}%); worst month {m.idxmin()} {m.min():+,.0f}; "
      f"best month {m.idxmax()} {m.max():+,.0f}; median month {m.median():+,.0f}")  # fmt: skip
    order = df.sort_values("xns")["ret"].to_numpy() <= 0
    streak = best = 0
    for lost in order:
        streak = streak + 1 if lost else 0
        best = max(best, streak)
    w(f"      longest run of losing trades {best}")
    hold = df["sess"].to_numpy()
    buckets = [("same day", hold == 0), ("1 session", hold == 1), ("2-3", (hold >= 2) & (hold <= 3)),
               ("4+", hold >= 4)]  # fmt: skip
    w("    by holding time: " + "   ".join(
        f"{name} {int(mk.sum())} {r[mk].mean():+.2f}%" for name, mk in buckets if mk.any())
      + f"   (ended by the line {100 * df['hit'].mean():.0f}%, mean {hold.mean():.1f} sessions)")  # fmt: skip
    mn = df["minute"].to_numpy()
    tb = [("09:30-10:29", mn < 630), ("10:30-11:29", (mn >= 630) & (mn < 690)),
          ("11:30-12:59", (mn >= 690) & (mn < 780)), ("13:00-14:45", mn >= 780)]  # fmt: skip
    w("    by entry time:   " + "   ".join(
        f"{name} {int(mk.sum())} {r[mk].mean():+.2f}%" for name, mk in tb if mk.any()))  # fmt: skip
    if stocks and float(df["dp"].sum()) > 0:
        net = df["ret"] - df["dp"]
        w(f"    with the depository charge (Rs {h.DP_CHARGE_INR} per delivery sell) at ONE share: "
          f"mean {net.mean():+.3f}% per trade")  # fmt: skip


def report() -> None:
    lines: list[str] = []
    w = lines.append
    w("STAGE-2 FINALISTS UNDER STRESS AND IN DEPTH")
    w("=" * 118)
    w("Net result per trade after modelled costs: % of the share price (stocks, one share) or % of")
    w("the premium (index options, one lot, synthetic Black-76 premiums at India VIX x 1.15).")
    w("TUNE = years 1-3 (2021-11-08..2024-11-07), CHOOSE = year 4, TEST = year 5 (..2026-11-07).")
    w("t treats all trades entered on one day as one observation. Every structure here was picked")
    w("after looking at about 15,000 structures per basket, and the TEST year has been read for")
    w("neighbours before, so nothing below is a clean out-of-sample result.")
    w("")
    w("PAPER-TEST CANDIDATE (rule written in NOTES.md before stage 2 ran): eligible by the stage-1")
    w(
        "rule AND (a) better than the no-signal benchmark of its side and nearest holding time in TUNE"
    )
    w(
        "and in CHOOSE AND (b) still positive in TUNE and CHOOSE with costs x 1.5, with the entry one"
    )
    w("minute late and (options) with yesterday's VIX.")
    for basket, unit in (("INDEX_OPTIONS", "lot"), ("STOCKS", "share")):
        data = load(basket)
        if not data:
            continue
        w("")
        w("=" * 118)
        w(f"{basket.replace('_', ' ')}: {len(h.BASKETS[basket])} instruments")
        w("=" * 118)
        for key, name, why in FINALISTS[basket]:
            base = data.get((key, "base"))
            if base is None:
                continue
            side = 1 if name.startswith("LONG") else -1
            w("")
            w("-" * 118)
            w(f"[{key}] {squash(name)}")
            w(f"  {why}")
            w(HEAD)
            for variant, _ in RUN_VARIANTS:
                w(variant_line(variant, data[key, variant]))
            for variant, _ in PREP_VARIANTS:
                if (key, variant) in data:
                    w(variant_line(variant, data[key, variant]))
            mean_hold = float(base["sess"].mean()) if len(base) else 0.0
            n_hold = min(s2.HOLD_SESSIONS, key=lambda v: abs(v - mean_hold))
            bench = data.get((f"bench:{side}:{n_hold}", "base"))
            if bench is not None and len(bench):
                label = "the close" if n_hold == 0 else f"{n_hold} session(s) on"
                w(variant_line(f"no signal: 09:30 -> {label}", bench))
                checks = {
                    "beats the benchmark": all(
                        mean_of(base, y) > mean_of(bench, y) for _, y in PERIODS[:2]
                    ),
                    "costs x 1.5": all(
                        mean_of(data[key, "costs x 1.5"], y) > 0 for _, y in PERIODS[:2]
                    ),
                    "1 minute late": all(
                        mean_of(data[key, "entry 1 minute late"], y) > 0 for _, y in PERIODS[:2]
                    ),
                }
                if (key, "yesterday's VIX") in data:
                    checks["yesterday's VIX"] = all(
                        mean_of(data[key, "yesterday's VIX"], y) > 0 for _, y in PERIODS[:2]
                    )
                base_ok = all(mean_of(base, y) > 0 for _, y in PERIODS[:2])
                verdict = "PASSES" if base_ok and all(checks.values()) else "FAILS"
                w(f"  candidate test (TUNE and CHOOSE only): base positive {'yes' if base_ok else 'NO'}; "
                  + "; ".join(f"{k} {'yes' if v else 'NO'}" for k, v in checks.items())
                  + f"  ->  {verdict} the stress part"
                  + f"   [TEST year: {mean_of(base, (4,)):+.3f}%]")  # fmt: skip
            w("  depth (base):")
            depth(w, base, unit, basket == "STOCKS")
    text = "\n".join(lines) + "\n"
    DEST.write_text(text, encoding="utf-8")
    print(text)
    print(f"-> {DEST}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    if a.run:
        run(a.workers, [x for x in a.only.split(",") if x] or None)
    if a.report:
        report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
