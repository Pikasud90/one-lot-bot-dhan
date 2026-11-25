"""US-091: the setup grid, instrument by instrument. Read-only study.

Run from the MAIN checkout (the data is there), this file from the US-091 worktree:

    cd "<main checkout>" && PYTHONPATH=src:scripts:. nice -n 10 .venv/bin/python -W ignore \
        "<worktree>/scripts/setup_grid.py" <stage> [<arg>]

Stages
  inventory    stage 0: what earlier runs say per instrument (no new backtest), to OUT/inventory/
  signals A|B  signals through the Lab's `generate_for_instrument`, cached per (instrument,
               strategy, timeframe, preset) in OUT/signals/. A: default preset at every timeframe;
               B: the other presets at the timeframes step A fixed (needs `select A`).
  labels       the conditions of every session (C1 to C5, C7, C9), cached in OUT/labels/, and
               the truncation tests of the condition labels and of the strategies' signals.
  trades A|B   entries at the open of the minute after the signal bar closes, one trade at a
               time, the desk's ratchet exit (as US-080), each trade with its conditions;
               OUT/trades/<instrument>.pkl (all steps of one instrument in one file).
  select A|B|C A: fix the timeframe per unit and strategy; B: fix the preset; C: every cell of
               steps A, B and C with its numbers and status, to OUT/cells.csv.
  placebo      20 shuffles of the condition labels over the days inside each instrument and
               calendar year (side and time of entry: over the trades), the same selection.
  real         real premiums from data/expired_options.db (read-only, no request) for the
               option cells that passed on the model.
  report       the tables, OUT/report.txt, and one OUT/profile_<INSTRUMENT>.csv per instrument.

`scripts/regime_study.py` and `scripts/ratchet_study.py` (and through them
`src/onelot/manage/ratchet.py`) and `src/onelot/mllab/states.py` are loaded by FILE PATH from this
worktree; the worktree's `src` is never on the path. Market data is read only; no store is ever
written. Outputs go to data/research/setup_grid_2026-11-24/ of the main checkout.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sqlite3
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

WT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):  # noqa: ANN202
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rg = _load("regime_study_us091", WT / "scripts" / "regime_study.py")
rs, h, ms, R = rg.rs, rg.h, rg.ms, rg.R
ST = _load("states_us091", WT / "src" / "onelot" / "mllab" / "states.py")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

assert Path(h.__file__).resolve().parents[1] == Path.cwd().resolve(), "run from the main checkout"

OUT = h.ROOT / "data" / "research" / "setup_grid_2026-11-24"
SIG = OUT / "signals"
LAB = OUT / "labels"
TRD = OUT / "trades"
INV = OUT / "inventory"
OPTIONS = ["NIFTY_OPT", "BANKNIFTY_OPT", "FINNIFTY_OPT", "SENSEX_OPT"]
STOCKS = list(h.STOCKS)
INSTS = OPTIONS + STOCKS
POOLS = {"POOL_4IDX": OPTIONS, "POOL_18STK": STOCKS}
WORKERS = 4  # the lead's limit since 2026-11-25 (3 before)
TFS = ("5m", "15m", "30m", "60m")
OWN_TF = {"microtrend": ("3m",), "ema_cross": ("12m", *TFS)}  # 12m: ema_cross's Lab default
MIN_TUNE_N, MIN_TUNE_DAYS = 60, 30  # a cell is examined from here (RESEARCH-RULES 5)
T_DISC, T_CONF = 2.5, 1.0
SIZE = {"OPTIONS": 1.0, "STOCK": 0.10}  # % a trade in TUNE worth trading
MIN_LATER = 20  # trades in CHOOSE and in TEST
REAL_GAP_PTS = 1.0  # points of premium: real premiums below the model where none traded
CAL0 = dt.date(2021, 11, 8)
YEARS = (2021, 2022, 2023, 2024, 2025, 2026)
CONDS: dict[str, tuple[str, ...]] = {
    "C1": ("UP-TREND", "DOWN-TREND", "CHOPPY", "NEUTRAL"),
    "C2": ("VIX HIGH", "VIX LOW"),
    "C3": ("VIX UP 5%", "VIX DOWN 5%", "VIX FLAT"),
    "C4": ("ACTIVE", "CALM"),
    "C5": ("GAP DOWN", "GAP FLAT", "GAP UP"),
    "C6": ("09:15-10:14", "10:15-12:59", "13:00-14:59", "15:00 on"),
    "C7": ("0", "1", "2 to 5", "6 to 10", "11 or more"),
    "C8": ("LONG", "SHORT"),
    "C9": ("VOL HIGH", "VOL LOW", "VOL MID"),
}
DAY_CONDS = ("C1", "C2", "C3", "C4", "C5", "C7", "C9")  # one label per session
TRADE_CONDS = ("C6", "C8")  # one label per trade


def kind(inst: str) -> str:
    return "OPTIONS" if inst in OPTIONS or inst == "POOL_4IDX" else "STOCK"


def strategies_for(inst: str) -> list[str]:
    from onelot.strategies.registry import REGISTRY, SUITABLE

    return [s for s in REGISTRY if kind(inst) in SUITABLE.get(s, ())]


def tfs_for(strategy: str) -> tuple[str, ...]:
    return OWN_TF.get(strategy, TFS)


def presets_for(strategy: str) -> dict[str, dict]:
    """DEFAULT plus the Lab's own presets (`onelot.lab.presets`: low and high); none for a
    strategy the Lab gives no search space (microtrend, supertrend_mtf)."""
    from onelot.lab.presets import presets_for as lab

    p = lab(strategy)
    return {k: v for k, v in p.items() if k == "default" or v}


def sig_path(inst: str, strategy: str, tf: str, preset: str) -> Path:
    return SIG / f"{inst}__{strategy}__{tf}__{preset}.npz"


# ───────────────────────────── stage: signals ──────────────────────────────


def _signals_of(task: tuple[str, list[tuple[str, str, str]]]) -> list[dict]:
    """All (strategy, timeframe, preset) of one instrument; one status row each."""
    from onelot.data.resample import minutes_of
    from onelot.lab.runner import BacktestConfig, generate_for_instrument

    inst, jobs = task
    h._init_worker()
    cache = h._cache
    out = []
    for name, tf, preset in jobs:
        f_out = sig_path(inst, name, tf, preset)
        if f_out.exists():
            continue
        t0 = time.time()
        prm = presets_for(name)[preset]
        try:
            cfg = BacktestConfig.from_settings(name, inst, timeframe=tf, params=dict(prm),
                                               start=h.T0.isoformat(), end=h.T_END.isoformat())  # fmt: skip
            _spec, _strat, f, _fine, _start = generate_for_instrument(cfg, cache)
            if f is None:
                raise ValueError("no bars in the window")
            dec = (f.index + pd.Timedelta(minutes=minutes_of(tf))).tz_convert("UTC")
            dec = dec.as_unit("ns").asi8
            arrs = {k: dec[f[k].to_numpy(bool)] for k in
                    ("long_entry", "short_entry", "long_exit", "short_exit")}  # fmt: skip
            np.savez(f_out, **arrs)
            out.append({"inst": inst, "strategy": name, "tf": tf, "preset": preset, "status": "ok",
                        "secs": round(time.time() - t0, 1),
                        **{k: len(v) for k, v in arrs.items()}})  # fmt: skip
        except Exception as exc:  # noqa: BLE001
            out.append({"inst": inst, "strategy": name, "tf": tf, "preset": preset,
                        "status": "error", "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
                        "trace": traceback.format_exc()[-600:]})  # fmt: skip
        cache.features.clear()
        print(f"{inst} {name} {tf} {preset}: {out[-1]['status']} {time.time() - t0:.0f}s",
              flush=True)  # fmt: skip
    return out


def jobs_a(inst: str) -> list[tuple[str, str, str]]:
    return [(s, tf, "default") for s in strategies_for(inst) for tf in tfs_for(s)]


def jobs_b(inst: str) -> list[tuple[str, str, str]]:
    fx = json.loads((OUT / "fixed_tf.json").read_text())
    out = []
    for s in strategies_for(inst):
        tfs = {fx[u][s] for u in (inst, pool_of(inst)) if s in fx.get(u, {})}
        for tf in sorted(tfs):
            out += [(s, tf, pr) for pr in presets_for(s) if pr != "default"]
    return out


def pool_of(inst: str) -> str:
    return "POOL_4IDX" if inst in OPTIONS else "POOL_18STK"


def stage_signals(step: str) -> None:
    SIG.mkdir(parents=True, exist_ok=True)
    fn = jobs_a if step == "A" else jobs_b
    tasks = [(i, fn(i)) for i in INSTS]
    tasks = [t for t in tasks if any(not sig_path(t[0], *j).exists() for j in t[1])]
    rows = []
    t0 = time.time()
    with ProcessPoolExecutor(WORKERS) as ex:
        for res in ex.map(_signals_of, tasks):
            rows.extend(res)
    with (OUT / "signals_status.jsonl").open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps({**r, "step": step}) + "\n")
    bad = [r for r in rows if r["status"] == "error"]
    print(f"signals {step}: {len(rows)} new runs, {len(bad)} errors, {time.time() - t0:.0f}s",
          flush=True)  # fmt: skip


# ───────────────────────────── labels ──────────────────────────────────────

C9_FROM, C9_TO, C9_ENTRY = 9 * 60 + 15, 9 * 60 + 44, 9 * 60 + 45


def day_of(ts: pd.DatetimeIndex) -> np.ndarray:
    return np.asarray(ts.date, dtype=object)


def turnover(inst: str, before_ns: int | None = None) -> pd.Series:
    """Rupee turnover (volume x close) of the minutes 09:15 to 09:44, per session date; bars
    opened before `before_ns` only when given."""
    b = h._cache.bars(h._cache.spec(inst).series_key, "1m")
    if before_ns is not None:
        b = b[b.index.as_unit("ns").asi8 < before_ns]
    mins = b.index.hour * 60 + b.index.minute
    q = b[(mins >= C9_FROM) & (mins <= C9_TO)]
    tv = (q["volume"] * q["close"]).groupby(day_of(q.index)).sum()
    return tv[tv > 0]


def tv_ratio(tv: pd.Series) -> pd.Series:
    """Turnover of a session over the median of the 20 sessions before it."""
    tv = tv.sort_index()
    return tv / tv.shift(1).rolling(20, min_periods=20).median()


def pool_turnover(before_ns: int | None = None) -> pd.Series:
    return pd.concat([turnover(s, before_ns) for s in STOCKS], axis=1).fillna(0.0).sum(axis=1)


def c9_of(r: float) -> str:
    if not np.isfinite(r):
        return ""
    return "VOL HIGH" if r > 1.2 else ("VOL LOW" if r < 0.8 else "VOL MID")


def c1_of(adx: float, c: float, s20: float) -> str:
    if not (np.isfinite(adx) and np.isfinite(s20)):
        return ""
    if adx < 20:
        return "CHOPPY"
    if adx >= 25 and c > s20:
        return "UP-TREND"
    if adx >= 25 and c < s20:
        return "DOWN-TREND"
    return "NEUTRAL"


def c3_of(x: float) -> str:
    if not np.isfinite(x):
        return ""
    return "VIX UP 5%" if x > 0.05 else ("VIX DOWN 5%" if x < -0.05 else "VIX FLAT")


def c5_of(z: float) -> str:
    if not np.isfinite(z):
        return ""
    return "GAP DOWN" if z < -0.5 else ("GAP UP" if z > 0.5 else "GAP FLAT")


def c6_of(minute: np.ndarray) -> np.ndarray:
    return np.where(minute < 615, "09:15-10:14", np.where(minute < 780, "10:15-12:59",
                    np.where(minute < 900, "13:00-14:59", "15:00 on")))  # fmt: skip


def c7_of(left: np.ndarray) -> np.ndarray:
    return np.where(left <= 0, "0", np.where(left == 1, "1", np.where(left <= 5, "2 to 5",
                    np.where(left <= 10, "6 to 10", "11 or more"))))  # fmt: skip


def day_labels(inst: str, tvr: pd.Series) -> pd.DataFrame:
    """C1 to C5 and C9 of every session of `inst` (2021-11-08 to 2026-11-07). C1 to C4 from the
    daily bars to the previous session; C5 from today's open against them; C9 from today's
    bars of 09:15 to 09:44 (used for entries from 09:45 only)."""
    key = h._cache.spec(inst).series_key
    d = h._cache.bars(key, "1d")
    cd = rg.conditions(key)  # C1 and C2 exactly as US-080, one row per daily bar of d
    days = pd.DatetimeIndex(d.index).normalize()
    v = h._cache.vix()
    vchg = v.pct_change().reindex(days, method="ffill").to_numpy(float)
    size = (
        ST.classify(d[["open", "high", "low", "close"]])["size"].reindex(d.index).to_numpy(object)
    )
    cl = d["close"].to_numpy(float)
    op = d["open"].to_numpy(float)
    sd20 = pd.Series(cl).pct_change().rolling(20, min_periods=20).std(ddof=1).to_numpy()
    dd = [x.date() for x in d.index]
    bd = np.array([x.toordinal() for x in dd])
    row = {x: i for i, x in enumerate(dd)}
    b1 = h._cache.bars(key, "1m")
    sess = sorted({x for x in day_of(b1.index) if h.T0 <= x <= h.T_END})
    out = []
    for x in sess:
        pos = int(np.searchsorted(bd, x.toordinal(), side="left")) - 1
        if pos < 0:
            out.append({"date": x})
            continue
        i = row.get(x)
        gz = (op[i] / cl[pos] - 1) / sd20[pos] if (i is not None and sd20[pos] > 0) else np.nan
        sz = size[pos]
        r9 = float(tvr.get(x, np.nan))
        out.append({"date": x, "C1": cd["cond"].iat[pos], "C2": cd["vix"].iat[pos],
                    "C3": c3_of(vchg[pos]), "C4": {"active": "ACTIVE", "calm": "CALM"}.get(sz, ""),
                    "C5": c5_of(gz), "C9": c9_of(r9), "adx": cd["adx"].iat[pos], "gapz": gz,
                    "vchg": vchg[pos], "tvr": r9})  # fmt: skip
    return pd.DataFrame(out).fillna(dict.fromkeys(("C1", "C2", "C3", "C4", "C5", "C9"), ""))


def _labels_of(inst: str) -> str:
    h._init_worker()
    f = LAB / f"{inst}.pkl"
    if not f.exists():
        tvr = pd.read_pickle(LAB / "tvr.pkl")[inst if inst in STOCKS else "POOL"]
        day_labels(inst, tvr.dropna()).to_pickle(f)
    return inst


def labels_truncated(inst: str, x: dt.date, entry_ns: int, minute: int) -> dict:
    """The same labels, computed from scratch on data cut at the entry minute's open: daily bars
    strictly before the session, one-minute bars opened before the entry."""
    from onelot.indicators import core as ind

    key = h._cache.spec(inst).series_key
    d = h._cache.bars(key, "1d")
    d = d[np.array([q.date() < x for q in d.index])]
    adx = float(ind.adx(d["high"], d["low"], d["close"], 14)["adx"].iloc[-1])
    cl = d["close"].to_numpy(float)
    prev = d.index[-1].date()
    v = h._cache.vix()
    v = v[np.array([q.date() <= prev for q in v.index])]
    vmed = float(v.iloc[-250:].median()) if len(v) >= 250 else np.nan
    c2 = "" if not np.isfinite(vmed) else ("VIX HIGH" if v.iloc[-1] > vmed else "VIX LOW")
    sz = ST.classify(d[["open", "high", "low", "close"]])["size"].iloc[-1]
    b1 = h._cache.bars(key, "1m")
    b1 = b1[b1.index.as_unit("ns").asi8 < entry_ns]
    today = b1[day_of(b1.index) == x]
    sd = float(np.std(cl[-21:][1:] / cl[-21:][:-1] - 1, ddof=1))
    gz = (float(today["open"].iloc[0]) / cl[-1] - 1) / sd
    c9 = ""
    if minute >= C9_ENTRY:
        tv = turnover(inst, entry_ns) if inst in STOCKS else pool_turnover(entry_ns)
        tv = tv.sort_index()
        past = tv[tv.index < x].iloc[-20:]
        if x in tv.index and len(past) == 20:
            c9 = c9_of(float(tv[x] / past.median()))
    return {"C1": c1_of(adx, cl[-1], float(cl[-20:].mean())), "C2": c2,
            "C3": c3_of(float(v.iloc[-1] / v.iloc[-2] - 1)),
            "C4": {"active": "ACTIVE", "calm": "CALM"}.get(sz, ""), "C5": c5_of(gz), "C9": c9}  # fmt: skip


def truncation_labels(n: int = 60, seed: int = 91) -> list[str]:
    rng = np.random.default_rng(seed)
    lines, bad = [], 0
    for _ in range(n):
        inst = str(rng.choice(INSTS))
        lab = pd.read_pickle(LAB / f"{inst}.pkl")
        lab = lab[lab["C1"] != ""]
        b1 = h._cache.bars(h._cache.spec(inst).series_key, "1m")
        r = lab.iloc[int(rng.integers(len(lab)))]
        x = r["date"]
        day = b1[day_of(b1.index) == x]
        mins = day.index.hour * 60 + day.index.minute
        k = int(rng.integers(1, len(day)))
        e_ns = int(day.index.as_unit("ns").asi8[k])
        got = labels_truncated(inst, x, e_ns, int(mins[k]))
        want = {c: r[c] for c in ("C1", "C2", "C3", "C4", "C5")}
        want["C9"] = r["C9"] if mins[k] >= C9_ENTRY else ""
        same = got == want
        bad += not same
        lines.append(f"  {inst} {x} {mins[k] // 60:02d}:{mins[k] % 60:02d} "
                     f"{'same' if same else f'DIFFERENT {got} vs {want}'}")  # fmt: skip
    lines.insert(0, f"TRUNCATION TEST, condition labels: {n - bad} of {n} unchanged")
    return lines


def truncation_signals(n: int = 50, seed: int = 191) -> list[str]:
    """Cut every input after a signal bar (bars, daily, market, minute bars; today's daily bar
    rebuilt from the minutes before the cut) and regenerate: the signal must still be there."""
    from onelot.data.resample import minutes_of
    from onelot.lab.runner import _session_params, _window
    from onelot.strategies.mtf_support import LadderCache
    from onelot.strategies.registry import get_strategy

    rng = np.random.default_rng(seed)
    files = sorted(SIG.glob("*__default.npz"))
    cache = h._cache
    lines, bad, done = [], 0, 0
    while done < n:
        f = files[int(rng.integers(len(files)))]
        inst, name, tf, preset = f.stem.split("__")
        z = np.load(f)
        col = str(rng.choice(["long_entry", "short_entry"]))
        if not len(z[col]):
            continue
        dec = int(z[col][int(rng.integers(len(z[col])))])
        spec = cache.spec(inst)
        key = spec.series_key
        start = pd.Timestamp(dec, tz="UTC").tz_convert(cache.bars(key, "1m").index.tz)
        sbar = start - pd.Timedelta(minutes=minutes_of(tf))
        params = _session_params(spec, dict(presets_for(name)[preset]), name)
        strat = get_strategy(name, {**params, "timeframe": tf})
        win, _sd = _window(cache.bars(key, tf), h.T0, h.T_END, 3)
        win = win[win.index <= sbar]
        m1 = cache.bars(key, "1m")
        m1 = m1[m1.index < start]

        def cut_daily(dkey: str, mins: pd.DataFrame, today: dt.date) -> pd.DataFrame:
            dd = cache.bars(dkey, "1d")
            dd = dd[np.array([q.date() < today for q in dd.index])]
            part = mins[day_of(mins.index) == today]
            if len(part):
                row = {"open": part["open"].iloc[0], "high": part["high"].max(),
                       "low": part["low"].min(), "close": part["close"].iloc[-1],
                       "volume": part["volume"].sum() if "volume" in part else 0.0}  # fmt: skip
                ix = pd.DatetimeIndex([pd.Timestamp(today, tz=dd.index.tz)])
                dd = pd.concat([dd, pd.DataFrame([row], index=ix)[dd.columns.intersection(
                    list(row))].reindex(columns=dd.columns)])  # fmt: skip
            return dd

        mkt = cache.market_symbol()
        market = cache.bars(mkt, tf) if mkt != key else pd.DataFrame()
        market = market[market.index <= sbar] if not market.empty else market
        mk1 = cache.bars(mkt, "1m")
        from onelot.strategies.base import Context

        ctx = Context(symbol=key, daily=cut_daily(key, m1, sbar.date()),
                      market=market if not market.empty else None,
                      extras={"fcache": {}, "fkey": ("trunc", key, tf, win.index[0], win.index[-1]),
                              "market_daily": cut_daily(mkt, mk1[mk1.index < start], sbar.date()),
                              "has_volume": bool(cache.series[key].has_volume)})  # fmt: skip
        if strat.multi_timeframe:
            ctx.extras["minute_bars"] = m1
            ctx.extras["mtf_cache"] = LadderCache()
        try:
            g = strat.generate(win, ctx)
            ok = bool(g[col].iloc[-1]) and g.index[-1] == sbar
        except Exception as exc:  # noqa: BLE001
            ok = False
            col = f"{col} error {type(exc).__name__}: {str(exc)[:80]}"
        bad += not ok
        done += 1
        lines.append(f"  {inst} {name} {tf} {col} at {sbar:%Y-%m-%d %H:%M}: "
                     f"{'unchanged' if ok else 'CHANGED'}")  # fmt: skip
    lines.insert(0, f"TRUNCATION TEST, strategy signals: {n - bad} of {n} unchanged")
    return lines


def stage_labels() -> None:
    LAB.mkdir(parents=True, exist_ok=True)
    h._init_worker()
    if not (LAB / "tvr.pkl").exists():
        tv = {s: turnover(s) for s in STOCKS}
        tvr = {s: tv_ratio(v) for s, v in tv.items()}
        tvr["POOL"] = tv_ratio(pd.concat(tv.values(), axis=1).fillna(0.0).sum(axis=1))
        pd.DataFrame(tvr).to_pickle(LAB / "tvr.pkl")
    with ProcessPoolExecutor(WORKERS) as ex:
        for inst in ex.map(_labels_of, INSTS):
            print(f"labels {inst}", flush=True)
    lines = truncation_labels()
    print("\n".join(lines[:1]), flush=True)
    lines += truncation_signals()
    (OUT / "truncation.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(x for x in lines if not x.startswith("  ") or "same" not in x), flush=True)


# ───────────────────────────── stage: trades ───────────────────────────────

LABEL_COLS = ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9")


def _trades_of(inst: str) -> str:
    h._init_worker()
    t0 = time.time()
    f_out = TRD / f"{inst}.pkl"
    old = pd.read_pickle(f_out) if f_out.exists() else pd.DataFrame()
    have = set() if old.empty else set(old[["strategy", "tf", "preset"]].astype(str)
                                           .itertuples(index=False, name=None))  # fmt: skip
    todo = []
    for f in sorted(SIG.glob(f"{inst}__*.npz")):
        _i, name, tf, preset = f.stem.split("__")
        if (name, tf, preset) not in have:
            todo.append((f, name, tf, preset))
    if not todo:
        return f"{inst}: nothing new"
    p = h.prepare(inst, tfs=("60m",))
    atr60 = ms.tf_age_atr(p, "60m", np.arange(p.n))[1]
    lab = pd.read_pickle(LAB / f"{inst}.pkl")
    pos = {x: i for i, x in enumerate(lab["date"])}
    si = np.array([pos.get(x, -1) for x in p.sess_dates])
    scol = {c: np.where(si >= 0, lab[c].to_numpy(object)[np.maximum(si, 0)], "")
            for c in ("C1", "C2", "C3", "C4", "C5", "C9")}  # fmt: skip
    nsess = len(p.sess_first)
    scol["C7"] = c7_of(p.exp_sess - np.arange(nsess)) if p.is_option else np.full(nsess, "")
    cal = np.array([x.year for x in p.sess_dates])
    frames, notes = [], []
    for f, name, tf, preset in todo:
        z = np.load(f)
        ent = {1: rg.to_minute(p, z["long_entry"]), -1: rg.to_minute(p, z["short_entry"])}
        nxs = {1: rg.next_at_or_after(p.n, rg.to_minute(p, z["long_exit"])),
               -1: rg.next_at_or_after(p.n, rg.to_minute(p, z["short_exit"]))}  # fmt: skip
        sig_sess = {1: np.searchsorted(p.ns, z["long_entry"], side="left") - 1,
                    -1: np.searchsorted(p.ns, z["short_entry"], side="left") - 1}  # fmt: skip
        cand = []
        for side in (1, -1):
            j, sb = ent[side], sig_sess[side]
            ok = (j < p.n) & (sb >= 0)
            j, sb = j[ok], sb[ok]
            j = j[p.sess[j] == p.sess[sb]]  # the entry minute is in the signal bar's session
            j = j[(p.year[j] >= 0) & (j < p.xi[p.sess[j]])]
            cand += [(int(x), side) for x in j]
        cand.sort(key=lambda t: (t[0], -t[1]))
        rows, busy, no_atr = [], -1, 0
        for j, side in cand:
            if j <= busy:
                continue
            if not (atr60[j] > 0):
                no_atr += 1
                continue
            x, px, why, how = rg.exit_of(p, j, side, atr60, nxs[side])
            rows.append((j, x, side, px, why, how))
            busy = x
        notes.append({"inst": inst, "strategy": name, "tf": tf, "preset": preset,
                      "trades": len(rows), "no_atr": no_atr})  # fmt: skip
        if not rows:
            continue
        E, X, S, PX, W, HOW = map(np.array, zip(*rows, strict=True))  # noqa: N806
        ret = np.empty(len(E))
        fill = np.full(len(E), np.nan)
        strike = np.full(len(E), np.nan)
        for side in (1, -1):
            m = side == S
            if m.any():
                ret[m] = h.pnl(p, side, 0, E[m], X[m], PX[m], p.ns[X[m]])["ret"]
                if p.is_option:
                    fill[m] = p.opt(side).entry_fill[E[m]]
                    strike[m] = p.opt(side).strike[E[m]]
        se = p.sess[E]
        mi = p.minute[E]
        fr = pd.DataFrame({
            "inst": inst, "strategy": name, "tf": tf, "preset": preset, "day": p.dayoff[E],
            "cal": cal[se], "period": rs.mb.PERIOD[p.year[E]], "side": S, "minute": mi,
            "ret": ret, "bp": S * (PX / p.o[E] - 1) * 1e4, "why": W, "held": X - E,
            "e0": p.ns[E] // 1_000_000_000, "x0": p.ns[X] // 1_000_000_000, "how": HOW,
            "level": PX, "fill": fill, "strike": strike,
        })  # fmt: skip
        for c in ("C1", "C2", "C3", "C4", "C5", "C7"):
            fr[c] = scol[c][se]
        fr["C9"] = np.where(mi >= C9_ENTRY, scol["C9"][se], "")
        fr["C6"] = c6_of(mi)
        fr["C8"] = np.where(S == 1, "LONG", "SHORT")
        frames.append(fr)
    new = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    df = pd.concat([old, new], ignore_index=True)
    for c in ("inst", "strategy", "tf", "preset", "how", *LABEL_COLS):
        df[c] = df[c].astype(str).astype("category")
    df.to_pickle(f_out)
    with (OUT / "trade_notes.jsonl").open("a", encoding="utf-8") as fh:
        for r in notes:
            fh.write(json.dumps(r) + "\n")
    return f"{inst}: {len(todo)} runs, {len(new)} trades, {time.time() - t0:.0f}s"


def stage_trades() -> None:
    TRD.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(WORKERS) as ex:
        for msg in ex.map(_trades_of, INSTS):
            print(msg, flush=True)
    if not (OUT / "costs.json").exists():
        h._init_worker()
        ic = h.LegCost.from_config(h._cost_cfg.product("equity_intraday"))
        x = np.array([1000.0])
        bp = (ic.leg(x, False)[0] + ic.leg(x, True)[0]) / 1000 * 1e4 + 2 * h.STOCK_SLIPPAGE * 1e4
        (OUT / "costs.json").write_text(json.dumps({"stock_round_trip_bp_at_1000": bp}))


# ───────────────────────────── selection ───────────────────────────────────


def load_trades() -> pd.DataFrame:
    df = pd.concat([pd.read_pickle(TRD / f"{i}.pkl") for i in INSTS], ignore_index=True)
    for c in ("inst", "strategy", "tf", "preset", *LABEL_COLS):
        df[c] = df[c].astype(str)
    df["pen"] = np.where(df["fill"].notna(), np.maximum(100.0 / df["fill"], 1.0), 0.0)
    return df


def units() -> dict[str, list[str]]:
    return {**{i: [i] for i in INSTS}, **POOLS}


def day_t(x: np.ndarray, d: np.ndarray) -> float:
    return rs.day_t(x, d)


def cstats(
    x: np.ndarray,
    day: np.ndarray,
    per: np.ndarray,
    pen: np.ndarray | None = None,
    cal: np.ndarray | None = None,
) -> dict:
    """The numbers of a cell (RESEARCH-RULES section 4); t with one draw per entry day."""
    out: dict = {"n": len(x), "days": len(np.unique(day)), "mean": x.mean() if len(x) else np.nan,
                 "t": day_t(x, day), "win": (x > 0).mean() * 100 if len(x) else np.nan}  # fmt: skip
    win, loss = x[x > 0], x[x <= 0]
    out["payoff"] = (
        win.mean() / abs(loss.mean()) if len(win) and len(loss) and loss.mean() else np.nan
    )
    for k, nm in enumerate(("tune", "choose", "test")):
        m = per == k
        out[f"n_{nm}"] = int(m.sum())
        out[f"days_{nm}"] = len(np.unique(day[m]))
        out[f"m_{nm}"] = x[m].mean() if m.any() else np.nan
        out[f"t_{nm}"] = day_t(x[m], day[m]) if m.any() else np.nan
    if pen is not None:
        out["m_less"] = (x - pen).mean() if len(x) else np.nan
    if cal is not None:
        for y in YEARS:
            m = cal == y
            out[f"n_{y}"] = int(m.sum())
            out[f"m_{y}"] = x[m].mean() if m.any() else np.nan
    return out


def flags(s: dict, knd: str) -> dict:
    ex = s["n_tune"] >= MIN_TUNE_N and s["days_tune"] >= MIN_TUNE_DAYS
    disc = ex and s["m_tune"] > 0 and s["t_tune"] >= T_DISC and s["m_tune"] >= SIZE[knd]
    conf = disc and s["m_choose"] > 0 and s["t_choose"] >= T_CONF and s["n_choose"] >= MIN_LATER
    pm = conf and s["m_test"] > 0 and s["n_test"] >= MIN_LATER
    pp = pm and (knd == "STOCK" or s.get("m_less", 0) > 0)  # placebo's stand-in for real
    allpos = all(s[f"m_{k}"] > 0 for k in ("tune", "choose", "test")) and s["t"] >= 2
    avoid = ex and s["t_tune"] <= -T_DISC and s["m_choose"] < 0 and s["m_test"] < 0
    return {"examined": ex, "discovered": disc, "confirmed": conf, "passed_model": pm,
            "passed_proxy": pp, "allpos_t2": allpos and ex, "avoid": avoid}  # fmt: skip


def cell_rows(
    df: pd.DataFrame, unit: str, keys: dict, by: str | None = None, years: bool = True
) -> list[dict]:
    """One row per value of `by` (or one for all trades) with the numbers and flags."""
    knd = kind(unit)
    groups = [("C0", "all", df)] if by is None else [
        (by, v, g) for v, g in df.groupby(by, observed=True) if v != ""]  # fmt: skip
    out = []
    for fam, val, g in groups:
        s = cstats(g["ret"].to_numpy(float), g["day"].to_numpy(), g["period"].to_numpy(),
                   g["pen"].to_numpy(float) if knd == "OPTIONS" else None,
                   g["cal"].to_numpy() if years else None)  # fmt: skip
        out.append({"unit": unit, "kind": knd, **keys, "cond": fam, "value": val, **s,
                    **flags(s, knd)})  # fmt: skip
    return out


def stage_select(step: str) -> None:
    df = load_trades()
    U = units()  # noqa: N806
    if step == "A":
        rows = []
        d = df[df.preset == "default"]
        for u, members in U.items():
            du = d[d.inst.isin(members)]
            for (s, tf), g in du.groupby(["strategy", "tf"]):
                if u in POOLS and g.inst.nunique() < len(members):
                    continue
                rows += cell_rows(g, u, {"strategy": s, "tf": tf, "preset": "default", "step": "A"})
        ca = pd.DataFrame(rows)
        ca.to_csv(OUT / "cells_A.csv", index=False)
        fixed: dict = {}
        for (u, s), g in ca.groupby(["unit", "strategy"]):
            g = g[g.n_tune >= MIN_TUNE_N]
            if len(g):
                fixed.setdefault(u, {})[s] = str(g.loc[g.m_tune.idxmax(), "tf"])
        (OUT / "fixed_tf.json").write_text(json.dumps(fixed, indent=1), encoding="utf-8")
        print(f"step A: {len(ca)} cells, {int(ca.examined.sum())} examined; fixed timeframes for "
              f"{sum(len(v) for v in fixed.values())} unit x strategy", flush=True)  # fmt: skip
        return
    fx = json.loads((OUT / "fixed_tf.json").read_text())
    if step == "B":
        rows = []
        for u, members in U.items():
            for s, tf in fx.get(u, {}).items():
                g0 = df[df.inst.isin(members) & (df.strategy == s) & (df.tf == tf)]
                for pr, g in g0.groupby("preset"):
                    if u in POOLS and g.inst.nunique() < len(members):
                        continue
                    rows += cell_rows(g, u, {"strategy": s, "tf": tf, "preset": pr, "step": "B"})
        cb = pd.DataFrame(rows)
        cb.to_csv(OUT / "cells_B.csv", index=False)
        fixed = {}
        for (u, s), g in cb.groupby(["unit", "strategy"]):
            g = g[g.n_tune >= MIN_TUNE_N]
            if len(g):
                fixed.setdefault(u, {})[s] = [
                    str(g.tf.iat[0]),
                    str(g.loc[g.m_tune.idxmax(), "preset"]),
                ]
        (OUT / "fixed.json").write_text(json.dumps(fixed, indent=1), encoding="utf-8")
        print(f"step B: {len(cb)} cells", flush=True)
        return
    fixed = json.loads((OUT / "fixed.json").read_text())
    rows = []
    for u, members in U.items():
        for s, (tf, pr) in fixed.get(u, {}).items():
            g = df[df.inst.isin(members) & (df.strategy == s) & (df.tf == tf) & (df.preset == pr)]
            for fam in CONDS:
                rows += cell_rows(g, u, {"strategy": s, "tf": tf, "preset": pr, "step": "C"}, fam)
    cc = pd.DataFrame(rows)
    ca, cb = pd.read_csv(OUT / "cells_A.csv"), pd.read_csv(OUT / "cells_B.csv")
    cb = cb[cb.preset != "default"]  # the default preset at the fixed timeframe is a cell of A
    cells = pd.concat([ca, cb, cc], ignore_index=True)
    cells["status"] = ""
    cells.to_csv(OUT / "cells.csv", index=False)
    e = cells[cells.examined]
    print(f"cells: {len(cells)}, examined {len(e)}, discovered {int(e.discovered.sum())}, "
          f"confirmed {int(e.confirmed.sum())}, passed on the model {int(e.passed_model.sum())}",
          flush=True)  # fmt: skip


# ───────────────────────────── placebo ─────────────────────────────────────


def stage_placebo(n_shuffle: int = 20) -> None:
    df = load_trades()
    fixed = json.loads((OUT / "fixed.json").read_text())
    labs = {i: pd.read_pickle(LAB / f"{i}.pkl") for i in INSTS}
    setups = []
    for u, members in units().items():
        for s, (tf, pr) in fixed.get(u, {}).items():
            g = df[df.inst.isin(members) & (df.strategy == s) & (df.tf == tf) & (df.preset == pr)]
            setups.append((u, s, tf, pr, g.copy()))
    dcols = ("C1", "C2", "C3", "C4", "C5", "C9")
    span = (h.T_END - CAL0).days + 1
    base = {}
    for i, lab in labs.items():
        off = np.array([(x - CAL0).days for x in lab["date"]])
        yr = np.array([x.year for x in lab["date"]])
        base[i] = (off, yr, {c: lab[c].to_numpy(object) for c in dcols})
    res = []
    for k in range(n_shuffle):
        rng = np.random.default_rng(9100 + k)
        # one permutation of the session labels per instrument and calendar year, by day offset
        perm: dict[str, dict[str, np.ndarray]] = {}
        for i, (off, yr, cols) in base.items():
            src = np.arange(len(off))
            for y in np.unique(yr):
                idx = np.flatnonzero(yr == y)
                src[idx] = rng.permutation(idx)
            perm[i] = {}
            for c in dcols:
                arr = np.full(span, "", dtype=object)
                arr[off] = cols[c][src]
                perm[i][c] = arr
        cnt = dict.fromkeys(
            ("examined", "discovered", "confirmed", "passed_model", "passed_proxy"), 0
        )
        for u, s, _tf, _pr, g in setups:
            g = g.copy()
            day = g["day"].to_numpy()
            inst = g["inst"].to_numpy()
            for c in dcols:
                v = np.empty(len(g), dtype=object)
                for i in np.unique(inst):
                    m = inst == i
                    v[m] = perm[i][c][day[m]]
                if c == "C9":
                    v = np.where(g["minute"].to_numpy() >= C9_ENTRY, v, "")
                g[c] = v
            groups = g.groupby(["inst", "cal"]).indices
            for c in ("C6", "C8"):  # trade-level: over the trades of an instrument and year
                v = g[c].to_numpy().copy()
                for q in groups.values():
                    v[q] = v[q][rng.permutation(len(q))]
                g[c] = v
            if kind(u) == "OPTIONS":  # C7, a session label: over the entry days
                c7 = {}
                for (i, _y), q in groups.items():
                    day_lab = g.iloc[q].groupby("day")["C7"].first()
                    c7.update({(i, a): b for a, b in zip(day_lab.index, rng.permutation(
                        day_lab.to_numpy()), strict=True)})  # fmt: skip
                g["C7"] = [c7[(i, d)] for i, d in zip(g["inst"], g["day"], strict=True)]
            for fam in CONDS:
                for r in cell_rows(g, u, {"strategy": s}, fam, years=False):
                    for f in cnt:
                        cnt[f] += bool(r[f])
        res.append(cnt)
        print(f"shuffle {k}: {cnt}", flush=True)
    pd.DataFrame(res).to_csv(OUT / "placebo.csv", index=False)


# ───────────────────────────── real premiums (store only) ──────────────────


class ReadOnlyStore:
    """`OptionBarStore.get` on a read-only connection (the store's own class opens it for
    writing and sets WAL)."""

    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self._mem: dict = {}

    def get(self, key, day):  # noqa: ANN001, ANN201
        from onelot.data.expired_options import Block, _unpack

        pk = (key.underlying, key.flag, key.code, key.opt, int(key.rel), day)
        if pk in self._mem:
            return self._mem[pk]
        row = self._db.execute(
            "SELECT complete, data FROM blocks WHERE underlying=? AND flag=? AND code=?"
            " AND opt=? AND rel=? AND day=?",
            (*pk[:5], day.isoformat()),
        ).fetchone()
        blk = None if row is None else Block(key, day, bool(row[0]), _unpack(row[1]))
        if len(self._mem) > 20000:
            self._mem.clear()
        self._mem[pk] = blk
        return blk


REAL_SERIES = {"NIFTY_OPT": "NIFTY", "BANKNIFTY_OPT": "BANKNIFTY", "FINNIFTY_OPT": "FINNIFTY",
               "SENSEX_OPT": "SENSEX"}  # fmt: skip


def stage_real() -> None:
    """Real premiums for the trades of every option cell that passed on the model."""
    import mtf_real_premiums as mr

    from onelot.config import CostConfig
    from onelot.costs import CostModel
    from onelot.data.expired_options import Underlying
    from onelot.data.universe import load_specs
    from onelot.lab.real_options import NeedBlock

    cells = pd.read_csv(OUT / "cells.csv", keep_default_na=False, na_values=[""])
    pas = cells[(cells.kind == "OPTIONS") & (cells.passed_model == True)]  # noqa: E712
    out_f = OUT / "real.csv"
    if pas.empty:
        pd.DataFrame(columns=["inst", "e0", "x0", "how", "level", "real_ret", "unpriced"]).to_csv(
            out_f, index=False
        )
        print("real: no option cell passed on the model; nothing to price", flush=True)
        return
    h._init_worker()
    df = load_trades()
    want = []
    for r in pas.itertuples():
        members = POOLS.get(r.unit, [r.unit])
        g = df[df.inst.isin(members) & (df.strategy == r.strategy) & (df.tf == r.tf)
               & (df.preset == r.preset)]  # fmt: skip
        if r.cond != "C0":
            g = g[g[r.cond] == r.value]
        want.append(g)
    tr = pd.concat(want).drop_duplicates(["inst", "e0", "x0", "how", "level"])
    series, insts = load_specs()
    store = ReadOnlyStore(h.ROOT / "data" / "expired_options.db")
    bt = h._settings.backtest
    rows = []
    for inst, g in tr.groupby("inst"):
        spec = h._cache.spec(inst)
        und = Underlying.from_specs(series[REAL_SERIES[inst]], insts[inst])
        bars = h._cache.bars(spec.series_key, "1m")
        costs = CostModel(CostConfig.load(h._settings.project_root / "config" / "costs.yaml")
                          .product(spec.product))  # fmt: skip
        pr = mr.CarryPricer(und, spec, bt, costs, store, bars, 1, source=None, slip_pct=0.0)
        for t in g.itertuples():
            try:
                real = pr.price_one(int(t.e0), int(t.x0), float(t.strike), int(t.side), t.how,
                                    float(t.level), {"ret": float(t.ret), "inr": 0.0})  # fmt: skip
            except NeedBlock:
                real = {"unpriced": "not in the store"}
            except Exception as exc:  # noqa: BLE001
                real = {"unpriced": f"error: {type(exc).__name__}: {str(exc)[:80]}"}
            rows.append({"inst": inst, "e0": t.e0, "x0": t.x0, "how": t.how, "level": t.level,
                         "real_ret": real.get("real_ret"), "unpriced": real.get("unpriced")})  # fmt: skip
    pd.DataFrame(rows).to_csv(out_f, index=False)
    print(f"real: {len(rows)} trades, priced {sum(r['real_ret'] is not None for r in rows)}",
          flush=True)  # fmt: skip


# ───────────────────────────── stage 0: inventory ──────────────────────────


def by_year(g: pd.DataFrame, col: str = "ret") -> dict:
    """Trades and mean % a trade per calendar year (column `yr`), best and worst year."""
    out: dict = {"n_all": len(g), "m_all": g[col].mean()}
    ms_ = {}
    for y in YEARS:
        q = g.loc[g.yr == y, col]
        out[f"n_{y}"], out[f"m_{y}"] = len(q), (q.mean() if len(q) else np.nan)
        if len(q) >= 20:
            ms_[y] = q.mean()
    out["best_year"] = max(ms_, key=ms_.get) if ms_ else ""
    out["worst_year"] = min(ms_, key=ms_.get) if ms_ else ""
    return out


def stage_inventory() -> None:
    """Stage 0: what the earlier runs say, per instrument. In-sample: it only proposes."""
    INV.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    res = h.ROOT / "data" / "research"
    # 1. the Lab's runs in the store (single runs, sweeps included): the latest run per setting
    db = sqlite3.connect(f"file:{h.ROOT / 'data' / 'onelot.db'}?mode=ro", uri=True)
    runs = pd.read_sql("SELECT id, config FROM backtest_runs WHERE kind='single' AND "
                       "status='DONE'", db)  # fmt: skip
    cfg = runs.config.map(json.loads)
    runs["inst"] = cfg.map(lambda c: c.get("instrument_key"))
    runs["strategy"] = cfg.map(lambda c: c.get("strategy"))
    runs["tf"] = cfg.map(lambda c: c.get("timeframe"))
    runs["preset"] = cfg.map(lambda c: c.get("_preset") or ("default" if not c.get("params")
                                                            else "custom"))  # fmt: skip
    runs["full"] = cfg.map(lambda c: c.get("start") is None and c.get("end") is None)
    runs = runs[runs.inst.isin(INSTS) & runs.full]
    t0 = time.time()
    agg = pd.read_sql("SELECT run_id, CAST(substr(entry_ts,1,4) AS INTEGER) AS yr, COUNT(*) AS n,"
                      " SUM(ret_pct) AS s FROM backtest_trades GROUP BY run_id, yr", db)  # fmt: skip
    print(f"inventory: Lab trades read in {time.time() - t0:.0f}s", flush=True)
    latest = runs.sort_values("id").groupby(["inst", "strategy", "tf", "preset"]).tail(1)
    agg = agg[agg.run_id.isin(latest.id)]
    for r in latest.itertuples():
        a = agg[agg.run_id == r.id]
        if a.n.sum() == 0:
            continue
        rec = {"source": "Lab runs (store)", "inst": r.inst, "strategy": r.strategy, "tf": r.tf,
               "preset": r.preset, "cond": "all", "n_all": int(a.n.sum()),
               "m_all": a.s.sum() / a.n.sum(), "comparable": "no"}  # fmt: skip
        ms_ = {}
        for y in YEARS:
            q = a[a.yr == y]
            n = int(q.n.sum())
            rec[f"n_{y}"], rec[f"m_{y}"] = n, (q.s.sum() / n if n else np.nan)
            if n >= 20:
                ms_[y] = rec[f"m_{y}"]
        rec["best_year"] = max(ms_, key=ms_.get) if ms_ else ""
        rec["worst_year"] = min(ms_, key=ms_.get) if ms_ else ""
        rows.append(rec)
    # 2. US-080 (regime): same exit, same costs, default timeframe: comparable with step A
    rt = pd.read_csv(res / "regime_2026-11-23" / "trades.csv")
    rt["yr"] = [(CAL0 + dt.timedelta(days=int(d))).year for d in rt["day"]]
    st = [json.loads(x) for x in (res / "regime_2026-11-23" / "signals_status.jsonl")
          .read_text().splitlines() if x.strip()]  # fmt: skip
    tfs = {r["strategy"]: r.get("tf") for r in st if r.get("tf")}
    for (inst, s), g in rt.groupby(["inst", "strategy"]):
        rows.append({"source": "US-080 regime", "inst": inst, "strategy": s, "tf": tfs.get(s),
                     "preset": "default", "cond": "all", "comparable": "yes", **by_year(g)})  # fmt: skip
        for c, q in g.groupby("cond"):
            rows.append({"source": "US-080 regime", "inst": inst, "strategy": s,
                         "tf": tfs.get(s), "preset": "default", "cond": c, "comparable": "yes",
                         **by_year(q)})  # fmt: skip
    # 3. US-074 (ratchet): micro-trend at 3m, its own exit and the chosen ratchet cell
    ro = pd.read_csv(res / "ratchet_2026-11-23" / "options.csv")
    chosen = rs.cell_name(json.loads((res / "ratchet_2026-11-23" / "chosen.json").read_text()))
    ro = ro[ro.cell.isin(["baseline", chosen])]
    ro["yr"] = [(CAL0 + dt.timedelta(days=int(d))).year for d in ro["day"]]
    for (inst, c), g in ro.groupby(["inst", "cell"]):
        rows.append({"source": f"US-074 ratchet ({c})", "inst": inst, "strategy": "microtrend",
                     "tf": "3m", "preset": "default", "cond": "all",
                     "comparable": "partly" if c != "baseline" else "no", **by_year(g)})  # fmt: skip
    # 4. cross-instrument retunes: default parameters, per instrument, the study's own periods
    for sub in ("cross_retune_2026-11-07", "cross_retune_v2_options", "cross_retune_v2_stocks"):
        f = res / sub / "evaluated.jsonl"
        for line in f.read_text().splitlines():
            e = json.loads(line)
            if e.get("params"):
                continue
            s, tf, _k = e["group"]
            per = {k: e["stats"].get(k, {}) for k in ("train", "valid", "test")}
            for inst in per["train"].get("per_instrument") or {}:
                if inst not in INSTS:
                    continue
                rows.append({"source": sub, "inst": inst, "strategy": s, "tf": tf,
                             "preset": "default", "cond": "all", "comparable": "no",
                             **{f"p_{k}": v.get("per_instrument", {}).get(inst)
                                for k, v in per.items()},
                             **{f"np_{k}": v.get("counts", {}).get(inst)
                                for k, v in per.items()}})  # fmt: skip
    inv = pd.DataFrame(rows)
    for inst in INSTS:
        inv[inv.inst == inst].to_csv(INV / f"inventory_{inst}.csv", index=False)
    inv.to_csv(INV / "inventory_all.csv", index=False)
    # the one-page summary: per instrument the best and worst setting with 100 trades or more
    L = []  # noqa: N806
    for inst in INSTS:
        q = inv[(inv.inst == inst) & (inv.cond == "all") & (inv.n_all >= 100)]
        L.append(f"{inst}: {len(q)} settings with 100 trades or more")
        for src, g in q.groupby("source"):
            g = g.sort_values("m_all")
            for nm, r in (("best", g.iloc[-1]), ("worst", g.iloc[0])):
                yrs = " ".join(f"{y}:{r[f'm_{y}']:+.2f}" for y in YEARS if r[f"n_{y}"] >= 20)
                L.append(f"   {src:<24} {nm:<5} {r.strategy} {r.tf} {r.preset}: {int(r.n_all)} "
                         f"trades {r.m_all:+.2f}% [{yrs}] best {r.best_year} worst "
                         f"{r.worst_year}")  # fmt: skip
        pos = q[(q.m_all > 0)]
        L.append(f"   settings positive over all years: {len(pos)} of {len(q)}")
    (INV / "summary.txt").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L), flush=True)


# ───────────────────────────── report ──────────────────────────────────────


def f2(v: float) -> str:
    return "n/a" if not np.isfinite(v) else f"{v:+.2f}".replace("-", "−")


def f1(v: float) -> str:
    return "n/a" if not np.isfinite(v) else f"{v:+.1f}".replace("-", "−")


def trio(r: pd.Series) -> str:
    return " / ".join(f2(r[f"m_{k}"]) for k in ("tune", "choose", "test"))


def cname(r: pd.Series) -> str:
    c = "" if r.cond == "C0" else f" | {r.cond} {r.value}"
    return f"{r.unit} {r.strategy} {r.tf} {r.preset}{c}"


def cline(r: pd.Series) -> str:
    yrs = " ".join(f"{y}:{f2(r[f'm_{y}'])}({int(r[f'n_{y}'])})" for y in YEARS
                   if f"n_{y}" in r and r[f"n_{y}"] > 0)  # fmt: skip
    less = f" less-pen {f2(r.m_less)}" if np.isfinite(r.get("m_less", np.nan)) else ""
    return (f"{cname(r)}: n {int(r.n)} days {int(r.days)} | {f2(r['mean'])} (t {f1(r.t)}) win "
            f"{r.win:.1f}% pay {r.payoff:.2f} | {trio(r)} | t T/C {f1(r.t_tune)}/{f1(r.t_choose)}"
            f" | n {int(r.n_tune)}/{int(r.n_choose)}/{int(r.n_test)}{less} | {yrs}")  # fmt: skip


def stage_report() -> None:
    cells = pd.read_csv(OUT / "cells.csv", keep_default_na=False, na_values=[""])
    for c in (
        "examined",
        "discovered",
        "confirmed",
        "passed_model",
        "passed_proxy",
        "allpos_t2",
        "avoid",
    ):
        cells[c] = cells[c].astype(str) == "True"
    cells["value"] = cells["value"].astype(str)
    real = pd.read_csv(OUT / "real.csv") if (OUT / "real.csv").exists() else pd.DataFrame()
    pl = pd.read_csv(OUT / "placebo.csv") if (OUT / "placebo.csv").exists() else pd.DataFrame()
    L: list[str] = []  # noqa: N806
    w = L.append
    cost = json.loads((OUT / "costs.json").read_text())
    w(f"stock round trip, one share at Rs 1,000: {cost['stock_round_trip_bp_at_1000']:.1f} bp")
    st = [json.loads(x) for x in (OUT / "signals_status.jsonl").read_text().splitlines() if x]
    errs = [r for r in st if r["status"] == "error"]
    w(f"signal runs: {len(st)}, errors {len(errs)}")
    for r in errs:
        w(f"   {r['inst']} {r['strategy']} {r['tf']} {r['preset']}: {r['reason']}")
    tf_ = OUT / "truncation.txt"
    if tf_.exists():
        w("\n".join(x for x in tf_.read_text().splitlines() if not x.startswith("  ")))
    # the real-premium reading of the option cells that passed on the model
    cells["real_mean"] = np.nan
    cells["real_priced"] = 0
    if len(real):
        df = load_trades()
        rr = real.drop_duplicates(["inst", "e0", "x0", "how", "level"])
        for i in cells.index[(cells.kind == "OPTIONS") & cells.passed_model]:
            r = cells.loc[i]
            g = df[df.inst.isin(POOLS.get(r.unit, [r.unit])) & (df.strategy == r.strategy)
                   & (df.tf == r.tf) & (df.preset == r.preset)]  # fmt: skip
            if r.cond != "C0":
                g = g[g[r.cond] == r.value]
            g = g.merge(rr, on=["inst", "e0", "x0", "how", "level"], how="left")
            rv = pd.to_numeric(g["real_ret"], errors="coerce")
            est = rv.fillna(g["ret"] - g["pen"])
            cells.loc[i, "real_mean"] = est.mean()
            cells.loc[i, "real_priced"] = int(rv.notna().sum())
    cells["passed"] = cells.passed_model & ((cells.kind == "STOCK") | (cells.real_mean > 0))
    e = cells[cells.examined]
    n_ex = len(e)
    luck = {"discovered": n_ex * 0.006, "confirmed": n_ex * 0.006 * 0.16,
            "passed": n_ex * 0.006 * 0.16 * 0.5}  # fmt: skip
    w("")
    w("COUNTS AGAINST LUCK")
    w(f"  cells tried {len(cells)}, examined {n_ex} (A {int(e.step.eq('A').sum())}, B "
      f"{int(e.step.eq('B').sum())}, C {int(e.step.eq('C').sum())})")  # fmt: skip
    for k in ("discovered", "confirmed", "passed"):
        w(f"  {k}: {int(e[k].sum())} (by arithmetic, luck alone {luck[k]:.2f})")
    ec = e[e.step == "C"]
    if len(pl):
        w(f"  conditioned cells (step C, examined): {len(ec)}; discovered {int(ec.discovered.sum())}"
          f", confirmed {int(ec.confirmed.sum())}, passed on the model "
          f"{int(ec.passed_model.sum())} (with the less-penalty stand-in "
          f"{int(ec.passed_proxy.sum())})")  # fmt: skip
        for k in ("examined", "discovered", "confirmed", "passed_model", "passed_proxy"):
            w(f"  placebo {k}: mean {pl[k].mean():.2f}, max {pl[k].max()}, min {pl[k].min()}")
    n_pass = int(e.passed.sum())
    luck_p = luck["passed"] + (pl["passed_proxy"].mean() if len(pl) else 0.0)
    clear = n_pass >= max(2 * luck_p, (pl["passed_proxy"].max() + 1) if len(pl) else 0) and n_pass
    cells["status"] = "REJECTED"
    cells.loc[cells.examined & cells.allpos_t2, "status"] = "WATCH"
    cells.loc[cells.passed, "status"] = "CANDIDATE" if clear else "WATCH"
    cells.loc[cells.avoid, "status"] = "AVOID"
    cells.loc[~cells.examined, "status"] = "not examined"
    w(f"  passes clearly above luck (at least twice luck's count and above every shuffle): "
      f"{'yes' if clear else 'no'}")  # fmt: skip
    cells.to_csv(OUT / "cells.csv", index=False)
    for u in units():
        c = cells[cells.unit == u]
        c.to_csv(OUT / f"profile_{u}.csv", index=False)
    w("")
    w("STEP A: TUNE mean by timeframe (default preset), per unit and strategy; * fixed")
    fx = json.loads((OUT / "fixed_tf.json").read_text())
    fixed = json.loads((OUT / "fixed.json").read_text())
    a = cells[cells.step == "A"]
    for u in units():
        w(f"  {u}")
        for s, g in a[a.unit == u].groupby("strategy"):
            parts = [f"{r.tf} {f2(r.m_tune)} ({int(r.n_tune)}{'' if r.examined else ' x'})"
                     f"{'*' if fx.get(u, {}).get(s) == r.tf else ''}"
                     for r in g.sort_values("tf").itertuples()]  # fmt: skip
            w(f"    {s:<18} " + "  ".join(parts))
    w("")
    w("STEP B: TUNE mean by preset at the fixed timeframe; * fixed")
    b = pd.read_csv(OUT / "cells_B.csv")
    for u in units():
        w(f"  {u}")
        for s, g in b[b.unit == u].groupby("strategy"):
            pick = fixed.get(u, {}).get(s, [None, None])[1]
            parts = [f"{r.preset} {f2(r.m_tune)} ({int(r.n_tune)}){'*' if r.preset == pick else ''}"
                     for r in g.itertuples()]  # fmt: skip
            w(f"    {s:<18} {g.tf.iat[0]:<4} " + "  ".join(parts))
    w("")
    w("PER UNIT: the fixed setup with the best TUNE mean (all trades), and the timeframes fixed")
    c0 = cells[(cells.cond == "C0") & cells.examined]
    for u in units():
        q = c0[c0.unit == u]
        q = q[[fixed.get(u, {}).get(r.strategy) == [r.tf, r.preset] for r in q.itertuples()]]
        tfs_u = pd.Series([v[0] for v in fixed.get(u, {}).values()]).value_counts()
        if q.empty:
            w(f"  {u}: no examined fixed setup")
            continue
        r = q.loc[q.m_tune.idxmax()]
        w(f"  {u}: timeframes fixed {dict(tfs_u)}; best {r.strategy} {r.tf} {r.preset} "
          f"{trio(r)} (TUNE t {f1(r.t_tune)}, n {int(r.n)}) {r.status}")  # fmt: skip
    for title, m in (("PASSED", cells.passed), ("DISCOVERED (not passed)",
                     cells.discovered & ~cells.passed), ("WATCH", cells.status == "WATCH")):  # fmt: skip
        w("")
        w(f"{title}: {int(m.sum())}")
        for _, r in cells[m].sort_values("t_tune", ascending=False).iterrows():
            extra = (f" | real reading {f2(r.real_mean)} ({int(r.real_priced)} priced)"
                     if r.kind == "OPTIONS" and r.passed_model else "")  # fmt: skip
            w("  " + cline(r) + extra)
    w("")
    w("NEIGHBOURS of each PASSED cell (TUNE mean, trades)")
    for _, r in cells[cells.passed].iterrows():
        w(f"  {cname(r)}")
        same = cells[(cells.unit == r.unit) & (cells.strategy == r.strategy)
                     & (cells.cond == "C0")]  # fmt: skip
        for _, q in same.iterrows():
            w(f"     {q.step} {q.tf} {q.preset}: {f2(q.m_tune)} ({int(q.n_tune)}) {trio(q)}")
        kin = cells[(cells.kind == r.kind) & (cells.strategy == r.strategy) & (cells.tf == r.tf)
                    & (cells.preset == r.preset) & (cells.cond == r.cond)
                    & (cells.value == r.value) & (cells.unit != r.unit)]  # fmt: skip
        for _, q in kin.iterrows():
            w(f"     other unit {q.unit}: {f2(q.m_tune)} ({int(q.n_tune)}) {trio(q)}")
    w("")
    av = cells[cells.status == "AVOID"]
    w(f"AVOID: {len(av)} cells ({int((av.kind == 'OPTIONS').sum())} options, "
      f"{int((av.kind == 'STOCK').sum())} stocks), strongest by TUNE t")  # fmt: skip
    for knd in ("OPTIONS", "STOCK"):
        q = av[av.kind == knd].sort_values("t_tune").head(25)
        for _, r in q.iterrows():
            w("  " + cline(r))
    w("")
    w("CONDITIONED OPTION CELLS with the highest TUNE mean (examined), for the record")
    q = cells[(cells.kind == "OPTIONS") & (cells.step == "C") & cells.examined]
    for _, r in q.sort_values("m_tune", ascending=False).head(15).iterrows():
        w("  " + cline(r))
    w("")
    w("STOCK CELLS with the highest TUNE mean (examined), for the record")
    q = cells[(cells.kind == "STOCK") & cells.examined]
    for _, r in q.sort_values("m_tune", ascending=False).head(15).iterrows():
        w("  " + cline(r))
    text = "\n".join(L) + "\n"
    (OUT / "report.txt").write_text(text, encoding="utf-8")
    print(text)


# ───────────────────────────── main ────────────────────────────────────────


def main() -> int:
    for d in (OUT, SIG, LAB, TRD, INV):
        d.mkdir(parents=True, exist_ok=True)
    stage, arg = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
    t0 = time.time()
    if stage == "signals":
        stage_signals(arg)
    elif stage == "labels":
        stage_labels()
    elif stage == "trades":
        stage_trades()
    elif stage == "select":
        stage_select(arg)
    elif stage == "placebo":
        stage_placebo()
    elif stage == "real":
        stage_real()
    elif stage == "report":
        stage_report()
    elif stage == "inventory":
        stage_inventory()
    else:
        raise SystemExit(f"unknown stage {stage}")
    print(f"stage {stage} {arg}: {time.time() - t0:.0f}s wall", flush=True)
    (OUT / f"done_{stage}{arg}").write_text(f"{time.time() - t0:.0f}s\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
