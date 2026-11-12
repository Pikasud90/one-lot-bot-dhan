"""Deep backtest of the three paper-test candidates (owner request, 2026-11-11).

The desk's paper test runs three slots from `config/desk.yaml`:

* `index`    supertrend_vol 30m on the five index options (long only: BUY CE)
* `stock_a`  supertrend_vol 60m on the 18 stocks (short only, one share)
* `stock_b`  supertrend_adx 30m on the 18 stocks (short only, one share)

Each slot is run over the whole history (2021-11-08 .. 2026-11-07) on the repaired bars
(`onelot.data.hygiene`), with the one-minute fill path, and then again:

* before costs (`gross`), and at 1.5x and 2x the costs;
* on the bars as stored (`raw`), to measure what the data repair changes;
* with ONE setting changed at a time (`sens`), without the one-minute path, to see whether the
  chosen setting sits on a plateau or on a spike. These include the owner's Supertrend(14, 1.7).

    .venv/bin/python scripts/deep_backtest_candidates.py --run      # ~15 min, 5 processes
    .venv/bin/python scripts/deep_backtest_candidates.py --report   # tables from the saved runs

Evidence about a rule on past data, after modelled costs; not advice, not a forecast.
"""

from __future__ import annotations

import argparse
import json
import logging
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "research" / "mtf_2026-11-11" / "deep"

STOCKS = [
    "RELIANCE", "MM", "SBIN", "HDFCBANK", "ITC", "TMPV", "MARUTI", "ICICIBANK", "HEROMOTOCO",
    "INFY", "TCS", "AXISBANK", "KOTAKBANK", "LT", "BAJFINANCE", "BHARTIARTL", "TATASTEEL", "SUNPHARMA",
]  # fmt: skip
INDEX_OPTIONS = ["BANKNIFTY_OPT", "NIFTY_OPT", "FINNIFTY_OPT", "MIDCPNIFTY_OPT", "SENSEX_OPT"]
T0, T_END = "2021-11-08", "2026-11-07"
YEAR_STARTS = ["2021-11-08", "2022-11-08", "2023-11-08", "2024-11-08", "2025-11-08", "2026-11-08"]
YEAR_NAMES = ["Y1 21-22", "Y2 22-23", "Y3 23-24", "Y4 24-25", "Y5 25-26"]
PERIODS = {"TUNE": (0, 3), "CHOOSE": (3, 4), "TEST": (4, 5), "ALL": (0, 5)}

# One setting changed at a time, per slot. "direction" sets allow_long / allow_short.
SENS_COMMON = {
    "flip_window": [1, 2, 4],
    "adx_min": [0, 12, 20, 25],
    "entry_end": ["11:30", "13:00", "14:45"],
    "max_bars": [0, 6, 12, 24],
    "target_r": [0.0, 1.5, 2.5, 4.0],
    "trail_atr_mult": [0.0, 1.5, 2.5],
    "min_strength": [0, 40, 55, 70],
    "regime_filter": [True, False],
    "market_filter": [True, False],
    "max_trades_per_day": [1, 2],
    "direction": ["long", "short", "both"],
    "timeframe": ["15m", "30m", "60m"],
    "exit_on_flip": [True, False],
    "st": [(7, 2.0), (10, 2.0), (14, 1.5), (14, 1.7), (14, 2.0), (14, 2.5), (14, 3.0), (21, 2.0),
           (10, 3.0), (7, 3.0)],
}  # fmt: skip
SENS_EXTRA = {
    "index": {"rsi_long_min": [45, 50, 55, 60]},
    "stock_a": {"rsi_short_max": [40, 45, 48, 52], "rvol_min": [0.0, 1.0, 1.5]},
    "stock_b": {"max_vwap_dist_atr": [1.5, 3.0, 5.0], "rvol_min": [0.0, 0.9, 1.5],
                "adx_rising_bars": [1, 3, 5]},
}  # fmt: skip

_cache = None
_raw = None


def load_slots() -> dict[str, dict]:
    cfg = yaml.safe_load((ROOT / "config" / "desk.yaml").read_text(encoding="utf-8"))
    idx = cfg["defaults"]["index_strategies"][0]
    sa, sb = cfg["defaults"]["stock_strategies"][:2]
    return {
        "index": {**idx, "basket": INDEX_OPTIONS},
        "stock_a": {**sa, "basket": STOCKS},
        "stock_b": {**sb, "basket": STOCKS},
    }


def variants_for(slot_id: str, slot: dict) -> list[tuple[str, dict]]:
    """[(variant name, {timeframe, params, fine, cost, raw})]."""
    base = {"timeframe": slot["timeframe"], "params": dict(slot["params"])}
    out: list[tuple[str, dict]] = [
        ("base", {**base, "fine": True, "cost": 1.0}),
        ("gross", {**base, "fine": True, "cost": 0.0}),
        ("cost1.5", {**base, "fine": True, "cost": 1.5}),
        ("cost2", {**base, "fine": True, "cost": 2.0}),
        ("raw", {**base, "fine": True, "cost": 1.0, "raw": True}),
        ("base_coarse", {**base, "fine": False, "cost": 1.0}),
    ]
    space = {**SENS_COMMON, **SENS_EXTRA.get(slot_id, {})}
    for name, vals in space.items():
        for v in vals:
            p = dict(slot["params"])
            tf = slot["timeframe"]
            if name == "direction":
                p["allow_long"], p["allow_short"] = v in ("long", "both"), v in ("short", "both")
            elif name == "timeframe":
                tf = v
            elif name == "st":
                p["st_period"], p["st_mult"] = v
            else:
                p[name] = v
            label = f"sens|{name}|{v[0]}/{v[1]}" if name == "st" else f"sens|{name}|{v}"
            out.append((label, {"timeframe": tf, "params": p, "fine": False, "cost": 1.0}))
    return out


def _init_worker() -> None:
    global _cache, _raw
    logging.disable(logging.WARNING)
    sys.path.insert(0, str(ROOT / "src"))
    from onelot.lab.runner import DataCache

    _cache = DataCache()
    _raw = DataCache(clean=False)


FULL_COLS = [
    "side", "entry_price", "exit_price", "qty", "risk_ps", "gross_pnl", "costs", "net_pnl",
    "ret_pct", "r_multiple", "r_gross", "exit_reason", "strength", "mae_r", "mfe_r", "bars_held",
]  # fmt: skip


def _run_instrument(task: tuple) -> tuple:
    """(slot_id, strategy, instrument, [(variant, spec)]) -> (slot_id, instrument, {variant: frame})."""
    from onelot.lab.runner import BacktestConfig, run_backtest

    slot_id, strategy, inst, variants = task
    out: dict[str, pd.DataFrame | str] = {}
    t0 = time.time()
    for name, spec in variants:
        cache = _raw if spec.get("raw") else _cache
        kw = {"slippage_pct": 0.0} if spec["cost"] == 0 else {}
        cfg = BacktestConfig.from_settings(
            strategy, inst, timeframe=spec["timeframe"], params=dict(spec["params"]),
            use_fine_path=spec["fine"], cost_multiplier=spec["cost"], start=T0, end=T_END, **kw,
        )  # fmt: skip
        try:
            res = run_backtest(cfg, cache)
        except Exception as exc:  # noqa: BLE001 - one failed variant is reported, not fatal
            out[name] = f"{type(exc).__name__}: {str(exc)[:200]}"
            continue
        t = res.trades
        if t is None or t.empty:
            out[name] = pd.DataFrame(columns=["entry_ts", "exit_ts", *FULL_COLS])
            continue
        cols = FULL_COLS if not name.startswith("sens|") else ["ret_pct", "net_pnl", "gross_pnl"]
        df = t[cols].copy()
        df.insert(0, "exit_ts", pd.to_datetime(t["exit_ts"], utc=True).to_numpy())
        df.insert(0, "entry_ts", pd.to_datetime(t["entry_ts"], utc=True).to_numpy())
        out[name] = df.reset_index(drop=True)
    # keep the market series (NIFTY, VIX) but drop this instrument's bars: memory per process
    for c in (_cache, _raw):
        for k in [k for k in c._bars if k[0] not in ("NIFTY", "INDIAVIX")]:  # noqa: SLF001
            del c._bars[k]  # noqa: SLF001
        c.features.clear()
    return slot_id, inst, out, time.time() - t0


def run(workers: int, only: list[str] | None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    slots = load_slots()
    tasks = []
    for slot_id, slot in slots.items():
        if only and slot_id not in only:
            continue
        v = variants_for(slot_id, slot)
        for inst in slot["basket"]:
            tasks.append((slot_id, slot["name"], inst, v))
    print(f"{len(tasks)} instrument tasks, {sum(len(t[3]) for t in tasks)} backtests", flush=True)
    results: dict[str, dict[str, dict]] = {s: {} for s in slots}
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker) as ex:
        futs = [ex.submit(_run_instrument, t) for t in tasks]
        for n, fut in enumerate(as_completed(futs), 1):
            slot_id, inst, out, secs = fut.result()
            results[slot_id][inst] = out
            errs = [k for k, v in out.items() if isinstance(v, str)]
            print(
                f"[{n}/{len(tasks)}] {slot_id} {inst}: {len(out)} runs in {secs:.0f}s "
                f"(total {time.time() - t0:.0f}s){' ERRORS ' + str(errs[:3]) if errs else ''}",
                flush=True,
            )
    for slot_id, per_inst in results.items():
        if not per_inst:
            continue
        with (OUT / f"runs_{slot_id}.pkl").open("wb") as fh:
            pickle.dump({"slot": slots[slot_id], "runs": per_inst}, fh)
    print("saved", flush=True)


# ───────────────────────────────── report ─────────────────────────────────


def year_index(ts: pd.Series) -> np.ndarray:
    edges = pd.to_datetime(YEAR_STARTS).tz_localize("Asia/Kolkata").tz_convert("UTC").as_unit("ns")
    return np.searchsorted(edges.asi8, ts.astype("int64").to_numpy(), side="right") - 1


def stats(df: pd.DataFrame) -> dict:
    """Per-trade statistics with a day-clustered t (trades of one day move together)."""
    n = len(df)
    if n == 0:
        return {"n": 0}
    r = df["ret_pct"].to_numpy(float)
    net = df["net_pnl"].to_numpy(float)
    mean = r.mean()
    day = df["entry_ts"].dt.tz_convert("Asia/Kolkata").dt.date.to_numpy()
    dev = pd.Series(r - mean).groupby(day).sum().to_numpy()
    se = np.sqrt((dev**2).sum()) / n if n > 1 else np.nan
    pos, neg = net[net > 0].sum(), -net[net < 0].sum()
    return {
        "n": n,
        "win": float((net > 0).mean() * 100),
        "mean": float(mean),
        "med": float(np.median(r)),
        "t": float(mean / se) if se and se > 0 else float("nan"),
        "pf": float(pos / neg) if neg > 0 else float("inf"),
        "net": float(net.sum()),
        "days": int(len(set(day))),
    }


def fmt(s: dict) -> str:
    if not s.get("n"):
        return f"{'0':>6}"
    return (
        f"{s['n']:>6} {s['win']:>5.1f}% {s['mean']:>+7.3f} {s['med']:>+7.3f} {s['t']:>+6.2f} "
        f"{s['pf']:>5.2f} {s['net']:>+11.0f}"
    )


HDR = f"{'n':>6} {'win':>6} {'mean%':>7} {'med%':>7} {'t':>6} {'PF':>5} {'net Rs':>11}"


def pool(per_inst: dict, variant: str) -> pd.DataFrame:
    frames = []
    for inst, runs in per_inst.items():
        v = runs.get(variant)
        if isinstance(v, pd.DataFrame) and not v.empty:
            frames.append(v.assign(instrument=inst))
    if not frames:
        return pd.DataFrame(columns=["entry_ts", "ret_pct", "net_pnl", "instrument"])
    df = pd.concat(frames, ignore_index=True)
    for col in ("entry_ts", "exit_ts"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], utc=True).dt.as_unit("ns")
    df["year"] = year_index(df["entry_ts"])
    return df[(df["year"] >= 0) & (df["year"] < 5)].reset_index(drop=True)


def by_period(df: pd.DataFrame) -> dict[str, dict]:
    return {p: stats(df[(df["year"] >= a) & (df["year"] < b)]) for p, (a, b) in PERIODS.items()}


def market_context() -> pd.DataFrame:
    """Per session: what NIFTY and India VIX looked like BEFORE the open, and how the day went."""
    sys.path.insert(0, str(ROOT / "src"))
    logging.disable(logging.WARNING)
    from onelot.lab.runner import DataCache

    cache = DataCache()
    d = cache.bars("NIFTY", "1d")
    ctx = pd.DataFrame(index=d.index.tz_convert("Asia/Kolkata").date)
    c = d["close"].to_numpy(float)
    sma50 = pd.Series(c).rolling(50).mean().to_numpy()
    prev_c, prev_sma = np.roll(c, 1), np.roll(sma50, 1)
    ctx["trend"] = np.where(prev_c > prev_sma, "NIFTY above 50d avg", "NIFTY below 50d avg")
    ret5 = pd.Series(c).pct_change(5).shift(1).to_numpy()
    ctx["mom5"] = np.where(ret5 > 0, "last 5 days up", "last 5 days down")
    rng = (d["high"] - d["low"]).to_numpy(float)
    body = np.abs(d["close"] - d["open"]).to_numpy(float)
    eff = np.divide(body, rng, out=np.zeros_like(body), where=rng > 0)
    ctx["daytype"] = np.where(eff >= 0.6, "trend day (ex post)", "range day (ex post)")
    ctx["daydir"] = np.where(
        d["close"].to_numpy() >= d["open"].to_numpy(), "NIFTY day up", "NIFTY day down"
    )
    gap = d["open"].to_numpy(float) / prev_c - 1
    ctx["gap"] = np.where(np.abs(gap) >= 0.004, "gap >= 0.4%", "gap < 0.4%")
    vix = cache.vix()
    if vix is not None and len(vix):
        v = vix.copy()
        v.index = pd.DatetimeIndex(v.index).tz_localize(None).normalize()
        vprev = v.shift(1)
        lo, hi = vprev.quantile([1 / 3, 2 / 3])
        lab = np.where(vprev <= lo, f"VIX low (<= {lo:.1f})",
                       np.where(vprev <= hi, f"VIX mid ({lo:.1f}-{hi:.1f})", f"VIX high (> {hi:.1f})"))  # fmt: skip
        vmap = dict(zip([x.date() for x in v.index], lab, strict=True))
        ctx["vix"] = [vmap.get(x, "VIX n/a") for x in ctx.index]
    return ctx[~ctx.index.duplicated()]


def report() -> None:
    lines: list[str] = []
    w = lines.append
    ctx = market_context()
    for slot_id in ("index", "stock_a", "stock_b"):
        f = OUT / f"runs_{slot_id}.pkl"
        if not f.exists():
            continue
        blob = pickle.loads(f.read_bytes())
        slot, per_inst = blob["slot"], blob["runs"]
        unit = "% of premium" if slot_id == "index" else "% of price"
        w("=" * 100)
        w(
            f"{slot_id.upper()}: {slot['name']} {slot['timeframe']}  ({len(per_inst)} instruments, returns in {unit})"
        )
        w("params: " + json.dumps(slot["params"], sort_keys=True))
        w("=" * 100)
        errs = {(i, k): v for i, r in per_inst.items() for k, v in r.items() if isinstance(v, str)}
        if errs:
            w(f"!! {len(errs)} failed runs, e.g. {list(errs.items())[:2]}")
        base = pool(per_inst, "base")

        w("\n-- 1. Costs: the same trades before costs, at the modelled costs, and at 1.5x / 2x")
        w(f"{'':<12} {'period':<7} {HDR}")
        for v in ("gross", "base", "cost1.5", "cost2"):
            for p, s in by_period(pool(per_inst, v)).items():
                w(f"{v:<12} {p:<7} {fmt(s)}")
        if not base.empty:
            g, c = base["gross_pnl"].sum(), base["costs"].sum()
            w(f"   base run: gross Rs {g:+.0f}, charges Rs {c:.0f}, net Rs {base['net_pnl'].sum():+.0f} "
              f"({len(base)} trades; charges are {c / max(len(base), 1):.1f} Rs per trade)")  # fmt: skip

        w("\n-- 2. Year by year (base run)")
        w(f"{'year':<12} {HDR}")
        for y, name in enumerate(YEAR_NAMES):
            w(f"{name:<12} {fmt(stats(base[base['year'] == y]))}")

        w("\n-- 3. Instrument by period (base run): n / mean% / net Rs")
        w(f"{'instrument':<16}" + "".join(f"{p:>26}" for p in PERIODS))
        for inst in slot["basket"]:
            d = base[base["instrument"] == inst]
            cells = []
            for a, b in PERIODS.values():
                s = stats(d[(d["year"] >= a) & (d["year"] < b)])
                cells.append(
                    f"{s['n']:>5} {s['mean']:>+7.3f} {s['net']:>+10.0f}  "
                    if s.get("n")
                    else f"{'0':>5}{'':>21}"
                )
            w(f"{inst:<16}" + "".join(cells))

        def table(title: str, key: pd.Series, d: pd.DataFrame = base) -> None:
            w(f"\n-- {title}")
            w(f"{'':<26} {HDR}   mean% by period TUNE / CHOOSE / TEST")
            for val in sorted(pd.unique(key.dropna()), key=str):
                sub = d[(key == val).to_numpy()]
                bp = by_period(sub)
                per = " / ".join(
                    f"{bp[p]['mean']:+.3f}({bp[p]['n']})" if bp[p].get("n") else "-"
                    for p in ("TUNE", "CHOOSE", "TEST")
                )
                w(f"{str(val):<26} {fmt(stats(sub))}   {per}")

        if base.empty:
            continue
        ist = base["entry_ts"].dt.tz_convert("Asia/Kolkata")
        table("4. Exit reason", base["exit_reason"])
        table("5. Entry time (bar the trade was filled on)", ist.dt.strftime("%H:%M"))
        table("6. Weekday", ist.dt.day_name())
        table("7. Side", base["side"])
        st = pd.cut(
            base["strength"].astype(float),
            [-1, 40, 55, 70, 101],
            labels=["<=40", "41-55", "56-70", ">70"],
        )
        table("8. Strength score", st.astype(str))
        dates = ist.dt.date
        for col, title in (
            ("trend", "9a. NIFTY trend before the day"), ("mom5", "9b. NIFTY last five days"),
            ("vix", "9c. India VIX the day before"), ("gap", "9d. NIFTY opening gap"),
            ("daytype", "9e. Kind of day NIFTY had (known only afterwards)"),
            ("daydir", "9f. NIFTY day direction (known only afterwards)"),
        ):  # fmt: skip
            if col in ctx.columns:
                table(title, dates.map(ctx[col].to_dict()))

        w("\n-- 10. How much of the move the exits kept (R = initial risk)")
        mfe, mae, r = (base[c].astype(float) for c in ("mfe_r", "mae_r", "r_multiple"))
        w(f"   average best point reached (MFE) {mfe.mean():+.2f}R, worst point (MAE) {mae.mean():+.2f}R, "
          f"average result {r.mean():+.2f}R (gross {base['r_gross'].astype(float).mean():+.2f}R)")  # fmt: skip
        for thr in (0.5, 1.0, 2.0):
            hit = base[mfe >= thr]
            if len(hit):
                w(f"   reached +{thr}R: {len(hit)} trades ({len(hit) / len(base) * 100:.0f}%); of those "
                  f"{(hit['net_pnl'] <= 0).mean() * 100:.0f}% still ended at a loss; their average result "
                  f"{hit['r_multiple'].astype(float).mean():+.2f}R")  # fmt: skip
        w(f"   bars held: mean {base['bars_held'].mean():.1f}, median {base['bars_held'].median():.0f}; "
          f"winners {base[base['net_pnl'] > 0]['bars_held'].mean():.1f}, losers {base[base['net_pnl'] <= 0]['bars_held'].mean():.1f}")  # fmt: skip

        w("\n-- 11. Data repair: bars as stored (raw) vs repaired (base)")
        raw = pool(per_inst, "raw")
        w(f"{'':<12} {'period':<7} {HDR}")
        for name, d in (("raw", raw), ("repaired", base)):
            for p, s in by_period(d).items():
                w(f"{name:<12} {p:<7} {fmt(s)}")
        for inst in slot["basket"]:
            a, b = raw[raw["instrument"] == inst], base[base["instrument"] == inst]
            if len(a) != len(b) or abs(a["net_pnl"].sum() - b["net_pnl"].sum()) > 1:
                w(f"   {inst:<16} raw {len(a):>4} trades Rs {a['net_pnl'].sum():>+9.0f}   repaired {len(b):>4} trades Rs {b['net_pnl'].sum():>+9.0f}")  # fmt: skip

        w("\n-- 12. One setting changed at a time (no one-minute path); mean% per trade (trades)")
        coarse = by_period(pool(per_inst, "base_coarse"))
        w(f"{'setting':<34} {'TUNE':>16} {'CHOOSE':>16} {'TEST':>16} {'ALL':>16}  net Rs ALL")

        def row(label: str, bp: dict) -> str:
            cells = "".join(
                f"{bp[p]['mean']:>+9.3f}({bp[p]['n']:>4})" if bp[p].get("n") else f"{'-':>16}"
                for p in ("TUNE", "CHOOSE", "TEST", "ALL")
            )
            return f"{label:<34} {cells}  {bp['ALL'].get('net', 0):>+10.0f}"

        w(row("the slot as it runs (coarse)", coarse))
        names = sorted({k for r in per_inst.values() for k in r if k.startswith("sens|")},
                       key=lambda k: (k.split("|")[1], k))  # fmt: skip
        last = ""
        for k in names:
            _, pname, val = k.split("|")
            if pname != last:
                w(f"  {pname}")
                last = pname
            w(row(f"    = {val}", by_period(pool(per_inst, k))))
        w("")
    text = "\n".join(lines)
    (OUT.parent / "deep_backtest.txt").write_text(text + "\n", encoding="utf-8")
    print(text)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--only", nargs="*", help="slot ids: index stock_a stock_b")
    a = ap.parse_args()
    if a.run:
        run(a.workers, a.only)
    if a.report:
        report()


if __name__ == "__main__":
    main()
