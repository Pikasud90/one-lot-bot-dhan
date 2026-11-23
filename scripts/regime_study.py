"""US-080: does any registered strategy earn after costs inside one market condition? Read-only.

Run from the MAIN checkout (the data is there), this file from the US-080 worktree:

    cd "<main checkout>" && PYTHONPATH=src:scripts:. nice -n 10 .venv/bin/python -W ignore \
        "<worktree>/scripts/regime_study.py" <stage> [<stage> ...]

Stages
  signals  every suitable strategy of the registry, default parameters and timeframe, through
           the Lab's `generate_for_instrument`; entry and exit decision times cached on disk
           (OUT/signals/<strategy>__<instrument>.npz). At most 4 worker processes.
  trades   entries at the open of the minute after the signal bar closes, one trade at a time,
           the desk's ratchet exit (1.5 x ATR(14) of 60m, target 3 R, extend on, the strategy's
           exit kept, the harness's session exit); each trade gets the condition of its day.
  report   the tables, to OUT/report.txt.

`scripts/ratchet_study.py` (and through it `src/onelot/manage/ratchet.py`) is loaded by FILE
PATH from this worktree; the worktree's `src` is never on the path. Market data is read only;
`data/onelot.db` is never written. Outputs go to data/research/regime_2026-11-23/.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
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


rs = _load("ratchet_study_us080", WT / "scripts" / "ratchet_study.py")
h, ms, R = rs.h, rs.ms, rs.R

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

assert Path(h.__file__).resolve().parents[1] == Path.cwd().resolve(), "run from the main checkout"

OUT = h.ROOT / "data" / "research" / "regime_2026-11-23"
SIG = OUT / "signals"
OPTIONS = ["NIFTY_OPT", "BANKNIFTY_OPT", "FINNIFTY_OPT", "SENSEX_OPT"]
STOCKS = list(h.STOCKS)
GROUPS = {"options": OPTIONS, "stocks": STOCKS}
WORKERS = 4
K_ATR, TARGET_R = 1.5, 3.0  # the product's setting
PRM = R.RatchetParams(extend=True)
OPT_COST_FRAC = rs.COST_FRAC  # plan cost of an option: 0.05% of the index (as in US-074)
REAL_GAP = 1.0  # points of premium a trade: real premiums below the model
CONDS = ("UP-TREND", "DOWN-TREND", "CHOPPY", "NEUTRAL")
NAMES = ("TUNE", "CHOOSE", "TEST")
MIN_TRADES = 100
STRATEGY, SESSION = rs.STRATEGY, rs.SESSION


def kind(inst: str) -> str:
    return "OPTIONS" if inst in OPTIONS else "STOCK"


def strategies_for(inst: str) -> list[str]:
    from onelot.strategies.registry import REGISTRY, SUITABLE

    return [s for s in REGISTRY if kind(inst) in SUITABLE.get(s, ())]


# ───────────────────────────── stage: signals ──────────────────────────────


def _signals_of(inst: str) -> list[dict]:
    """All suitable strategies on one instrument; one row of status per strategy."""
    from onelot.data.resample import minutes_of
    from onelot.lab.runner import BacktestConfig, generate_for_instrument
    from onelot.strategies.registry import REGISTRY

    h._init_worker()
    cache = h._cache
    out = []
    for name in strategies_for(inst):
        f_out = SIG / f"{name}__{inst}.npz"
        if f_out.exists():
            out.append({"strategy": name, "inst": inst, "status": "cached"})
            continue
        t0 = time.time()
        dp = REGISTRY[name].Params()
        tf = dp.timeframe
        # The strategy's own window: the Lab's `_session_params` would otherwise put the
        # instrument's 09:30 to 14:45 over a strategy's default (gap_fade 09:15 to 09:45,
        # intraday_momentum 14:30 to 15:00).
        own = {"entry_start": dp.entry_start, "entry_end": dp.entry_end}
        try:
            cfg = BacktestConfig.from_settings(name, inst, timeframe=tf, params=own,
                                               start=h.T0.isoformat(), end=h.T_END.isoformat())  # fmt: skip
            _spec, _strat, f, _fine, _start = generate_for_instrument(cfg, cache)
            if f is None:
                raise ValueError("no bars in the window")
            mins = minutes_of(tf) if tf != "1d" else 0
            dec = (f.index + pd.Timedelta(minutes=mins)).tz_convert("UTC").as_unit("ns").asi8
            arrs = {k: dec[f[k].to_numpy(bool)] for k in
                    ("long_entry", "short_entry", "long_exit", "short_exit")}  # fmt: skip
            np.savez(f_out, **arrs, tf=np.array(tf))
            out.append({"strategy": name, "inst": inst, "status": "ok", "tf": tf,
                        "secs": round(time.time() - t0), **{k: len(v) for k, v in arrs.items()}})  # fmt: skip
        except Exception as exc:  # noqa: BLE001
            out.append({"strategy": name, "inst": inst, "status": "error", "tf": tf,
                        "reason": f"{type(exc).__name__}: {str(exc)[:160]}",
                        "trace": traceback.format_exc()[-600:]})  # fmt: skip
        print(f"{inst} {name}: {out[-1]['status']} {time.time() - t0:.0f}s", flush=True)
    return out


def stage_signals() -> None:
    SIG.mkdir(parents=True, exist_ok=True)
    insts = OPTIONS + STOCKS
    rows = []
    with ProcessPoolExecutor(WORKERS) as ex:
        for res in ex.map(_signals_of, insts):
            rows.extend(res)
    log = OUT / "signals_status.jsonl"
    with log.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    bad = [r for r in rows if r["status"] == "error"]
    print(f"signals: {len(rows)} strategy x instrument, {len(bad)} errors", flush=True)


# ───────────────────────────── conditions ──────────────────────────────────


def conditions(series_key: str) -> pd.DataFrame:
    """Per daily bar t: the condition and VIX side that hold for the NEXT session (values of
    bar t only, so a session reads the bars of the sessions before it)."""
    from onelot.indicators import core as ind

    d = h._cache.bars(series_key, "1d")
    adx = ind.adx(d["high"], d["low"], d["close"], 14)["adx"].to_numpy(float)
    c = d["close"].to_numpy(float)
    s20 = pd.Series(c).rolling(20, min_periods=20).mean().to_numpy()
    cond = np.full(len(c), "", dtype=object)
    ok = np.isfinite(adx) & np.isfinite(s20)
    cond[ok] = "NEUTRAL"
    cond[ok & (adx < 20)] = "CHOPPY"
    cond[ok & (adx >= 25) & (c > s20)] = "UP-TREND"
    cond[ok & (adx >= 25) & (c < s20)] = "DOWN-TREND"
    v = h._cache.vix()
    vmed = v.rolling(250, min_periods=250).median()
    days = pd.DatetimeIndex(d.index).normalize()
    vx = v.reindex(days, method="ffill").to_numpy(float)
    vm = vmed.reindex(days, method="ffill").to_numpy(float)
    vside = np.where(
        np.isfinite(vx) & np.isfinite(vm), np.where(vx > vm, "VIX HIGH", "VIX LOW"), ""
    )
    return pd.DataFrame({"date": [x.date() for x in d.index], "cond": cond, "vix": vside,
                         "adx": adx})  # fmt: skip


def day_lookup(cd: pd.DataFrame, dates: list[dt.date]) -> tuple[np.ndarray, np.ndarray]:
    """Condition and VIX side for each session date, from the last daily bar BEFORE it."""
    bd = np.array([x.toordinal() for x in cd["date"]])
    q = np.array([x.toordinal() for x in dates])
    pos = np.searchsorted(bd, q, side="left") - 1  # strictly before the session
    cond = np.where(pos >= 0, cd["cond"].to_numpy()[np.maximum(pos, 0)], "")
    vix = np.where(pos >= 0, cd["vix"].to_numpy()[np.maximum(pos, 0)], "")
    return cond, vix


# ───────────────────────────── stage: trades ───────────────────────────────


def to_minute(p: h.Prep, dec_ns: np.ndarray) -> np.ndarray:
    """The minute bar opening at or after each decision time (p.n when none)."""
    return np.searchsorted(p.ns, dec_ns, side="left")


def next_at_or_after(n: int, idx: np.ndarray) -> np.ndarray:
    ev = np.zeros(n + 1, dtype=bool)
    ev[idx[idx < n]] = True
    pos = np.where(ev, np.arange(n + 1), n)
    return np.minimum.accumulate(pos[::-1])[::-1]


def plan_cost(p: h.Prep, e: float) -> float:
    if p.is_option:
        return OPT_COST_FRAC * e
    ic = p.costs["intraday"]
    x = np.array([e])
    return float(ic.leg(x, False)[0] + ic.leg(x, True)[0] + 2 * h.STOCK_SLIPPAGE * e)


def exit_of(p: h.Prep, j: int, side: int, atr60: np.ndarray, nx: np.ndarray) -> tuple:
    """`ratchet_study.one_exit` with the product's cell and this strategy's own exit signal."""
    s = int(p.sess[j])
    xi = int(p.xi[s])
    xs = int(nx[j + 1]) if j + 1 <= p.n else p.n  # an exit decided after the entry minute
    if xs < xi:
        end, why, px, how = xs, STRATEGY, float(p.o[xs]), "open"
    else:
        end, why, px = xi, SESSION, float(p.xp[s])
        how = "close" if p.xclose[s] else "open"
    stop_end = end + 1 if (why == SESSION and p.xclose[s]) else end
    e = float(p.o[j])
    plan = R.plan_from_risk(side, e, K_ATR * float(atr60[j]), TARGET_R, plan_cost(p, e))
    hit = rs.ratchet_numpy(plan, PRM, p.o[j:stop_end], p.h[j:stop_end], p.l[j:stop_end])
    if hit is None:
        return end, px, why, how
    k, lvl, code, gap = hit
    return j + k, lvl, code, ("open" if gap else "touch")


def _trades_of(inst: str) -> tuple[pd.DataFrame, list[dict]]:
    h._init_worker()
    t0 = time.time()
    p = h.prepare(inst, tfs=("60m",))
    atr60 = ms.tf_age_atr(p, "60m", np.arange(p.n))[1]
    cd = conditions(h._cache.spec(inst).series_key)
    s_cond, s_vix = day_lookup(cd, p.sess_dates)
    frames, notes = [], []
    for name in strategies_for(inst):
        f_in = SIG / f"{name}__{inst}.npz"
        if not f_in.exists():
            notes.append({"strategy": name, "inst": inst, "note": "no signals file"})
            continue
        z = np.load(f_in)
        ent = {1: to_minute(p, z["long_entry"]), -1: to_minute(p, z["short_entry"])}
        nxs = {1: next_at_or_after(p.n, to_minute(p, z["long_exit"])),
               -1: next_at_or_after(p.n, to_minute(p, z["short_exit"]))}  # fmt: skip
        # the last minute bar opened before the decision time: inside the signal bar
        sig_sess = {1: np.searchsorted(p.ns, z["long_entry"], side="left") - 1,
                    -1: np.searchsorted(p.ns, z["short_entry"], side="left") - 1}  # fmt: skip
        cand = []
        for side in (1, -1):
            j = ent[side]
            sb = sig_sess[side]
            ok = (j < p.n) & (sb >= 0)
            j, sb = j[ok], sb[ok]
            same = p.sess[j] == p.sess[sb]
            j = j[same]
            j = j[(p.year[j] >= 0) & (j < p.xi[p.sess[j]])]
            cand += [(int(x), side) for x in j]
        cand.sort(key=lambda t: (t[0], -t[1]))  # both sides on one minute: the long (rare)
        both = len(cand) - len({c[0] for c in cand})
        no_atr = 0
        rows, busy = [], -1
        for j, side in cand:
            if j <= busy:
                continue
            if not (atr60[j] > 0):
                no_atr += 1
                continue
            x, px, why, how = exit_of(p, j, side, atr60, nxs[side])
            rows.append((j, x, side, px, why, how))
            busy = x
        notes.append({"strategy": name, "inst": inst, "both_sides": both, "no_atr": no_atr,
                      "trades": len(rows)})  # fmt: skip
        if not rows:
            continue
        E, X, S, PX, W, _HOW = map(np.array, zip(*rows, strict=True))  # noqa: N806
        ret = np.empty(len(E))
        for side in (1, -1):
            m = side == S
            if m.any():
                ret[m] = h.pnl(p, side, 0, E[m], X[m], PX[m], p.ns[X[m]])["ret"]
        se = p.sess[E]
        frames.append(pd.DataFrame({
            "strategy": name, "inst": inst, "group": "options" if p.is_option else "stocks",
            "day": p.dayoff[E], "period": rs.mb.PERIOD[p.year[E]], "side": S, "ret": ret,
            "bp": S * (PX / p.o[E] - 1) * 1e4, "why": W, "held": X - E,
            "cond": s_cond[se], "vix": s_vix[se],
        }))  # fmt: skip
    print(f"{inst}: {time.time() - t0:.0f}s", flush=True)
    return (pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()), notes


def session_counts(inst: str) -> list[dict]:
    """Sessions in each condition per period (the instrument's own one-minute sessions)."""
    h._init_worker()
    spec = h._cache.spec(inst)
    b = h._cache.bars(spec.series_key, "1m")
    dates = sorted(set(b.index.date))
    dates = [d for d in dates if h.T0 <= d <= h.T_END]
    cond, vix = day_lookup(conditions(spec.series_key), dates)
    yr = np.searchsorted([d.toordinal() for d in h.YEAR_STARTS],
                         [d.toordinal() for d in dates], side="right") - 1  # fmt: skip
    per = rs.mb.PERIOD[np.clip(yr, 0, 4)]
    return [{"inst": inst, "period": int(pp), "cond": c, "vix": v}
            for pp, c, v in zip(per, cond, vix, strict=True)]  # fmt: skip


def stage_trades() -> None:
    insts = OPTIONS + STOCKS
    frames, notes, sess = [], [], []
    with ProcessPoolExecutor(WORKERS) as ex:
        for df, nt in ex.map(_trades_of, insts):
            frames.append(df)
            notes.extend(nt)
        for rows in ex.map(session_counts, insts):
            sess.extend(rows)
    pd.concat(frames, ignore_index=True).to_csv(OUT / "trades.csv", index=False)
    pd.DataFrame(notes).to_csv(OUT / "trade_notes.csv", index=False)
    pd.DataFrame(sess).to_csv(OUT / "sessions.csv", index=False)
    if not (OUT / "costs.json").exists():
        h._init_worker()
        ic = h.LegCost.from_config(h._cost_cfg.product("equity_intraday"))
        x = np.array([1000.0])
        bp = (ic.leg(x, False)[0] + ic.leg(x, True)[0]) / 1000 * 1e4 + 2 * h.STOCK_SLIPPAGE * 1e4
        (OUT / "costs.json").write_text(json.dumps({"stock_round_trip_bp_at_1000": bp}))


# ───────────────────────────── report ──────────────────────────────────────

day_t = rs.day_t


def stats(d: pd.DataFrame) -> dict:
    x = d["ret"].to_numpy(float)
    win, loss = x[x > 0], x[x <= 0]
    aw = win.mean() if len(win) else np.nan
    al = loss.mean() if len(loss) else np.nan
    per = [d.loc[d.period == k, "ret"] for k in range(3)]
    return {
        "n": len(x), "m": x.mean() if len(x) else np.nan, "t": day_t(x, d["day"].to_numpy()),
        "w": (x > 0).mean() * 100 if len(x) else np.nan,
        "pay": aw / abs(al) if (len(win) and len(loss) and al) else np.nan,
        "per": [q.mean() if len(q) else np.nan for q in per],
        "pn": [len(q) for q in per],
        "tt": day_t(per[0].to_numpy(), d.loc[d.period == 0, "day"].to_numpy()),
        "bp": d["bp"].mean(),
    }  # fmt: skip


def f2(v: float) -> str:
    return "n/a" if not np.isfinite(v) else f"{v:+.2f}".replace("-", "−")


def f1(v: float) -> str:
    return "n/a" if not np.isfinite(v) else f"{v:+.1f}".replace("-", "−")


def trio(s: dict) -> str:
    return " / ".join(f2(v) for v in s["per"])


HDR = ("| cell | trades | % a trade (t) | win % | payoff | idx/stock bp | TUNE / CHOOSE / TEST "
       "| TUNE t | trades T/C/T |\n|---|---|---|---|---|---|---|---|---|")  # fmt: skip


def line(name: str, s: dict) -> str:
    return (f"| {name} | {s['n']} | {f2(s['m'])} ({f1(s['t'])}) | {s['w']:.1f} | "
            f"{s['pay']:.2f} | {f1(s['bp'])} | {trio(s)} | {f1(s['tt'])} | "
            f"{'/'.join(str(n) for n in s['pn'])} |")  # fmt: skip


def candidates(df: pd.DataFrame, key: str, values: tuple[str, ...]) -> tuple[list, int]:
    """Cells strategy x <key> x group with >= MIN_TRADES; the candidates among them."""
    cells, out = 0, []
    for (g, strat, c), d in df[df[key].isin(values)].groupby(["group", "strategy", key]):
        if len(d) < MIN_TRADES:
            continue
        cells += 1
        s = stats(d)
        if s["per"][0] > 0 and s["tt"] >= 2 and s["per"][1] > 0:
            out.append((g, strat, c, s))
    return out, cells


def stage_report() -> None:
    L: list[str] = []  # noqa: N806
    w = L.append
    df = pd.read_csv(OUT / "trades.csv")
    sess = pd.read_csv(OUT / "sessions.csv").fillna("")
    notes = pd.read_csv(OUT / "trade_notes.csv")
    st_path = OUT / "signals_status.jsonl"
    status = [json.loads(x) for x in st_path.read_text().splitlines() if x.strip()]
    cost = json.loads((OUT / "costs.json").read_text())
    w(f"stock round trip (one share at Rs 1,000, charges + slippage): "
      f"{cost['stock_round_trip_bp_at_1000']:.1f} bp")  # fmt: skip
    errs = [r for r in status if r["status"] == "error"]
    w(f"signal errors: {len(errs)}")
    for r in errs:
        w(f"   {r['strategy']} on {r['inst']}: {r['reason']}")
    w(f"no-condition trades (warm-up): {(df.cond.isna() | (df.cond == '')).sum()}")
    w(f"both sides on one minute (long taken): {int(notes.both_sides.fillna(0).sum())}; "
      f"entries without a 60m ATR: {int(notes.no_atr.fillna(0).sum())}")  # fmt: skip
    w("")
    w("SESSIONS PER CONDITION (TUNE / CHOOSE / TEST)")
    for g, insts in GROUPS.items():
        s = sess[sess.inst.isin(insts)]
        w(f"  {g} ({len(insts)} instruments, instrument-sessions)")
        for c in (*CONDS, ""):
            q = s[s.cond == c]
            w(f"    {c or 'none (warm-up)':<12} "
              + " / ".join(str(int((q.period == k).sum())) for k in range(3))
              + f"  ({100 * len(q) / max(len(s), 1):.0f}%)")  # fmt: skip
        for v in ("VIX HIGH", "VIX LOW"):
            q = s[s.vix == v]
            w(f"    {v:<12} " + " / ".join(str(int((q.period == k).sum())) for k in range(3)))
    for inst in OPTIONS:
        s = sess[sess.inst == inst]
        w(f"    {inst:<14} " + "  ".join(f"{c} {(s.cond == c).mean() * 100:.0f}%" for c in CONDS))
    w("")
    w("ALL CONDITIONS, per strategy and group (sorted by TUNE)")
    for g in GROUPS:
        w(f"  {g}")
        w(HDR)
        d = df[df.group == g]
        ss = {k: stats(v) for k, v in d.groupby("strategy")}
        for k in sorted(ss, key=lambda k: -ss[k]["per"][0]):
            w(line(k, ss[k]))
        if g == "options":
            w(f"  options all strategies: model {f2(d.ret.mean())}, less 1.0 point "
              f"{f2(d.ret.mean() - REAL_GAP)}")  # fmt: skip
        w("")
    w("EVERY CELL strategy x condition x group with >= 100 trades (sorted by group, TUNE)")
    for g in GROUPS:
        w(f"  {g}")
        w(HDR)
        d = df[(df.group == g) & df.cond.isin(CONDS)]
        rows = [(f"{s} | {c}", stats(v)) for (s, c), v in d.groupby(["strategy", "cond"])
                if len(v) >= MIN_TRADES]  # fmt: skip
        for name, s in sorted(rows, key=lambda r: -r[1]["per"][0]):
            w(line(name, s))
        small = [(s, c, len(v)) for (s, c), v in d.groupby(["strategy", "cond"])
                 if len(v) < MIN_TRADES]  # fmt: skip
        w(f"  cells under 100 trades (not examined): {len(small)}: "
          + ", ".join(f"{s}|{c}|{n}" for s, c, n in small))  # fmt: skip
        w("")
    cand, cells = candidates(df, "cond", CONDS)
    w(f"CANDIDATES (TUNE > 0 with t >= 2, CHOOSE > 0): {len(cand)} of {cells} cells; "
      f"chance alone gives about {0.025 * cells:.1f} before the CHOOSE filter")  # fmt: skip
    n_tune = sum(
        1 for (g, s, c), d in df[df.cond.isin(CONDS)].groupby(["group", "strategy", "cond"])
        if len(d) >= MIN_TRADES and (st := stats(d))["per"][0] > 0 and st["tt"] >= 2
    )  # fmt: skip
    w(f"   cells passing the TUNE half alone: {n_tune}")
    w(HDR + " TEST pass |")
    for g, s, c, st in sorted(cand, key=lambda r: (r[0], r[2], -r[3]["per"][0])):
        te = st["per"][2]
        ok = np.isfinite(te) and te > 0 and (g == "stocks" or te - REAL_GAP > 0)
        extra = f" less 1.0: {f2(te - REAL_GAP)}" if g == "options" else ""
        w(line(f"{g} {s} {c}", st) + f" {'PASS' if ok else 'fail'}{extra} |")
    w("")
    w("THE SWITCH (per condition and group: the candidate with the highest TUNE)")
    for g in GROUPS:
        picks = {}
        for gg, s, c, st in cand:
            if gg == g and (c not in picks or st["per"][0] > picks[c][1]["per"][0]):
                picks[c] = (s, st)
        parts = []
        for c in CONDS:
            if c in picks:
                parts.append(df[(df.group == g) & (df.cond == c) & (df.strategy == picks[c][0])])
        w(f"  {g}: " + ", ".join(f"{c} -> {picks[c][0]}" if c in picks else f"{c} -> no trade"
                                 for c in CONDS))  # fmt: skip
        if not parts:
            w("    no candidate in any condition: the switch never trades")
            continue
        port = pd.concat(parts)
        for k, nm in ((1, "CHOOSE"), (2, "TEST")):
            q = port[port.period == k]
            if len(q):
                real = f", less 1.0 point {f2(q.ret.mean() - REAL_GAP)}" if g == "options" else ""
                w(f"    {nm}: {len(q)} trades, {f2(q.ret.mean())}% a trade "
                  f"(t {f1(day_t(q.ret.to_numpy(), q.day.to_numpy()))}){real}")  # fmt: skip
                for c in CONDS:
                    qc = q[q.cond == c]
                    if len(qc):
                        w(f"      {c}: {len(qc)} trades, {f2(qc.ret.mean())}% "
                          f"(t {f1(day_t(qc.ret.to_numpy(), qc.day.to_numpy()))})")  # fmt: skip
            else:
                w(f"    {nm}: no trades")
    w("")
    w("VIX CUT (robustness only): strategy x VIX side x group")
    vc, vcells = candidates(df, "vix", ("VIX HIGH", "VIX LOW"))
    w(f"  candidates by the same rule: {len(vc)} of {vcells} cells "
      f"(chance about {0.025 * vcells:.1f})")  # fmt: skip
    for g in GROUPS:
        w(f"  {g}")
        w(HDR)
        d = df[(df.group == g) & df.vix.isin(["VIX HIGH", "VIX LOW"])]
        for (s, v), q in d.groupby(["strategy", "vix"]):
            if len(q) >= MIN_TRADES:
                mark = " *" if any(x[1] == s and x[2] == v and x[0] == g for x in vc) else ""
                w(line(f"{s} | {v}{mark}", stats(q)))
    w("")
    w("BY STYLE x condition (pooled trades; for the verdict's best and worst condition)")
    style = {"trend": ["ema_cross", "supertrend_vol", "supertrend_adx", "supertrend_mtf",
                       "microtrend", "orb", "donchian_breakout", "intraday_momentum",
                       "cpr_breakout", "nr7_breakout", "squeeze", "ema_pullback"],
             "reversion": ["mean_reversion", "gap_fade", "turtle_soup", "vwap_pullback"]}  # fmt: skip
    for g in GROUPS:
        for sty, names in style.items():
            d = df[(df.group == g) & df.strategy.isin(names) & df.cond.isin(CONDS)]
            if d.empty:
                continue
            parts = []
            for c in CONDS:
                q = d[d.cond == c]
                if len(q):
                    s = stats(q)
                    parts.append(f"{c} {len(q)} {f2(s['m'])} (t {f1(s['t'])}) [{trio(s)}]")
            w(f"  {g} {sty}: " + "; ".join(parts))
    text = "\n".join(L) + "\n"
    (OUT / "report.txt").write_text(text, encoding="utf-8")
    print(text)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    stages = sys.argv[1:] or ["report"]
    for name in stages:
        {"signals": stage_signals, "trades": stage_trades, "report": stage_report}[name]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
