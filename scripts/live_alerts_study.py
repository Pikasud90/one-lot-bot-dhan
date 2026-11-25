"""US-088: what the desk's own live alerts say. A read-only study.

Run from the MAIN checkout (the data is there), this file from the US-088 worktree:

    cd "<main checkout>" && PYTHONPATH=src:scripts:. nice -n 10 .venv/bin/python -W ignore \
        "<worktree>/scripts/live_alerts_study.py" <stage> [<stage> ...]

Stages
  inventory   Part A1 and A2: alerts by day, slot, instrument, side and data grade; the
              sessions the desk ran; the paper trades as recorded, split at the line-up change.
  excursions  Part A3: the best and worst move of the managed price before each exit.
  fills       Part A4: the option spreads the desk recorded, against the cost model.
  parity      Part B: the product's `microtrend` slot on the stored bars of the live window,
              signal by signal against the live alerts.
  whatif      Part C: the same alerts under other rules, model premiums (and real ones where
              the option store has them).
  history     Part D: the hypotheses (fixed in HYPOTHESES below before the stage ran) on five
              years of the micro-trend rule, with the selection rule and a placebo.
  report      every table, to OUT/report.txt.

Privacy: the tables desk_positions, positions, trade_journal, account_cache and app_settings
are never read. `desk_signals.status` is read only to count it; no output names it per alert.
The stores are opened through SQLite URIs with mode=ro; nothing is written to them. Outputs go
to data/research/live_alerts_2026-11-24/ in the main checkout.

US-105 note (25 Nov 2026): this script counts every paper trade as recorded, including the
three whose contract had no open interest (see `onelot.void.void_reason`); the desk's scores
leave them out. Read its tables with that in mind.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import logging
import math
import sqlite3
import sys
import threading
from pathlib import Path
from urllib.parse import quote

sys.path[:0] = [str(Path.cwd() / "src"), str(Path.cwd() / "scripts")]

import mtf_bruteforce as h  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

assert Path(h.__file__).resolve().parents[1] == Path.cwd().resolve(), "run from the main checkout"

OUT = h.ROOT / "data" / "research" / "live_alerts_2026-11-24"
STORE = h.ROOT / "data" / "onelot.db"
OPT_STORE = h.ROOT / "data" / "expired_options.db"
MT_DIR = h.ROOT / "data" / "research" / "microtrend_2026-11-13"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
LIVE_FIRST, LIVE_LAST = dt.date(2026, 10, 29), dt.date(2026, 11, 23)
# every slot but pullback was paused on 12 Nov at 11:06; `microtrend` ran from 13 Nov
LINEUP = dt.date(2026, 11, 13)
OWNER4 = ["NIFTY_OPT", "BANKNIFTY_OPT", "FINNIFTY_OPT", "SENSEX_OPT"]
TRAINING = "MIDCPNIFTY_OPT"
EXCLUDED = {"ADANIGREEN"}
POINT_GAP = 1.0  # real premiums below the model, points of % a trade (MICROTREND_STUDY)
BUCKETS = (("09:15", "10:15", "first hour"), ("10:15", "14:00", "middle"),
           ("14:00", "15:31", "last 90 min"))  # fmt: skip
SPREAD_BUCKETS = (("09:15", "10:00"), ("10:00", "11:30"), ("11:30", "13:00"), ("13:00", "15:31"))
MODEL_HALF_PCT, MODEL_TICKS, TICK = 0.6, 2, 0.05

# Part D. Written after parts A to C were read and BEFORE the history stage first ran
# (index alerts, model premiums, recorded exits; 22 alerts on 6 days).
# Each is a condition known at the entry minute of a micro-trend trade; its opposite is the
# AVOID candidate. Delay is a variant of the entry, not a label (no placebo).
HYPOTHESES = {
    "H1 entry before 10:15": "first-hour entries (09:30 to 10:14) lose (live: -6.4% model)",
    "H2 put": "puts do better than calls (live paper: puts +1.9%, calls -7.3%)",
    "H3 previous session active": "after an active session it pays, after a calm one it loses",
    "H4 VIX fell in the previous session": "after India VIX fell the rule does better",
    "H5 entry 3 minutes late": "a 3-minute delay costs nothing (live: -2.8% against -3.2%)",
}


# ───────────────────────────── helpers ─────────────────────────────────────


def ro() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{quote(str(STORE))}?mode=ro", uri=True)


def ro_engine():  # noqa: ANN201
    from sqlalchemy import create_engine

    uri = f"file:{quote(str(STORE))}?mode=ro"
    return create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))


def init_read_only() -> None:
    """`h._init_worker` with the cache on a read-only engine."""
    from onelot.config import CostConfig
    from onelot.lab.runner import DataCache
    from onelot.settings import current_settings

    if h._cache is not None:
        return
    logging.disable(logging.WARNING)
    h._cache = DataCache(engine=ro_engine())
    h._settings = current_settings()
    h._cost_cfg = CostConfig.load(h._settings.project_root / "config" / "costs.yaml")


def load_module(name: str, path: Path):  # noqa: ANN201
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def utc_naive_to_ist(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, format="mixed").dt.tz_localize("UTC").dt.tz_convert(IST)


def day_t(x: np.ndarray, d: np.ndarray) -> float:
    """t of the mean with all trades of one entry day as one draw."""
    x = np.asarray(x, float)
    if len(x) < 3:
        return float("nan")
    _, inv = np.unique(np.asarray(d), return_inverse=True)
    if inv.max() < 1:
        return float("nan")
    dev = np.bincount(inv, x) - x.mean() * np.bincount(inv)
    se = math.sqrt(float((dev**2).sum())) / len(x)
    return float(x.mean() / se) if se > 0 else float("nan")


def stats(x: np.ndarray, days: np.ndarray, inr: np.ndarray | None = None) -> dict:
    x = np.asarray(x, float)
    ok = np.isfinite(x)
    x, days = x[ok], np.asarray(days)[ok]
    if not len(x):
        return {"n": 0, "days": 0, "m": np.nan, "t": np.nan, "w": np.nan, "pay": np.nan,
                "inr": np.nan}  # fmt: skip
    win, loss = x[x > 0], x[x <= 0]
    pay = (win.mean() / abs(loss.mean())) if len(win) and len(loss) and loss.mean() else np.nan
    return {"n": len(x), "days": len(set(days.tolist())), "m": x.mean(), "t": day_t(x, days),
            "w": 100 * (x > 0).mean(), "pay": pay,
            "inr": float(np.nanmean(np.asarray(inr, float)[ok])) if inr is not None else np.nan}  # fmt: skip


def fmt(s: dict, inr: bool = False) -> str:
    if not s["n"]:
        return "      0 trades"
    out = (f"{s['n']:4d} tr {s['days']:3d} d {s['m']:+7.2f}% (t {s['t']:+5.1f}) "
           f"win {s['w']:5.1f}% pay {s['pay']:5.2f}")  # fmt: skip
    return out + (f"  Rs {s['inr']:+9.1f}" if inr else "")


def bucket(ts: pd.Timestamp) -> str:
    hm = ts.strftime("%H:%M")
    for lo, hi, name in BUCKETS:
        if lo <= hm < hi:
            return name
    return "other"


def save(df: pd.DataFrame, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT / name, index=False)


# ───────────────────────────── the record ──────────────────────────────────


def load_alerts() -> pd.DataFrame:
    """One row per alert with its paper trade. No owner action is read or kept."""
    with ro() as c:
        sig = pd.read_sql(
            "select id, created_at, series_key, instrument_key, kind, strategy, timeframe, side,"
            " bar_ts, payload from desk_signals",
            c,
        )
        pt = pd.read_sql(
            "select id as trade_id, alert_id, status as pstatus, close_reason, grade, data_source,"
            " qty, entry_ts, exit_ts, entry_paise, exit_paise, net_paise, costs_paise, risk_paise,"
            " plan from paper_trades",
            c,
        )
        ev = pd.read_sql(
            "select trade_id, kind as ekind, detail from paper_events"
            " where kind in ('FILLED', 'CLOSED')",
            c,
        )
    rows = []
    for r in sig.itertuples():
        p = json.loads(r.payload)
        eco = p.get("economics") or {}
        opt = eco.get("option") or {}
        data = p.get("data") or {}
        rows.append({
            "id": r.id, "inst": r.instrument_key, "series": r.series_key, "kind": r.kind,
            "strategy": r.strategy, "tf": r.timeframe, "aside": r.side,
            "sign": 1 if r.side in ("BUY", "BUY_CE") else -1,
            "src": data.get("source") or "none", "delayed": bool(data.get("delayed")),
            "u_entry": eco.get("underlying_entry"), "u_stop": eco.get("underlying_stop"),
            "u_target": eco.get("underlying_target"), "premium": eco.get("entry_price"),
            "costs_inr": eco.get("costs_inr"), "qty_alert": eco.get("qty"),
            "synthetic": eco.get("synthetic"), "strike": opt.get("strike"),
            "delta": opt.get("delta"), "bid": opt.get("bid"), "ask": opt.get("ask"),
        })  # fmt: skip
    a = pd.DataFrame(rows)
    a["created"] = utc_naive_to_ist(sig["created_at"])
    a["bar_ts"] = utc_naive_to_ist(sig["bar_ts"])
    a["day"] = a["created"].dt.date
    a["slot"] = a["strategy"] + " " + a["tf"]
    a["period"] = np.where(a["day"] < LINEUP, "before 13 Nov", "from 13 Nov")
    fills = {}
    closes = {}
    for r in ev.itertuples():
        d = json.loads(r.detail)
        (fills if r.ekind == "FILLED" else closes)[r.trade_id] = d
    pt["fill_under"] = [fills.get(t, {}).get("bar_open_paise", np.nan) / 100 for t in pt.trade_id]
    pt["prem_src"] = [fills.get(t, {}).get("premium_source") for t in pt.trade_id]
    pt["exit_level"] = [closes.get(t, {}).get("price_paise", np.nan) / 100 for t in pt.trade_id]
    pt["basis_paise"] = [
        ((json.loads(x).get("pricing") or {}).get("basis_paise")) for x in pt["plan"]
    ]
    pt = pt.drop(columns=["plan"])
    for k in ("entry_ts", "exit_ts"):
        pt[k] = utc_naive_to_ist(pt[k])
    a = a.merge(pt, left_on="id", right_on="alert_id", how="left")
    a["base_inr"] = a["entry_paise"] * a["qty"] / 100
    a["net_inr"] = a["net_paise"] / 100
    a["pct"] = a["net_inr"] / a["base_inr"] * 100
    a["hour"] = [bucket(t) for t in a["entry_ts"].fillna(a["created"])]
    return a.sort_values("created").reset_index(drop=True)


def sessions() -> list[dt.date]:
    init_read_only()
    b = h._cache.bars("NIFTY", "1m")
    d = sorted(set(b.index.date))
    return [x for x in d if LIVE_FIRST <= x <= LIVE_LAST]


# ───────────────────────────── Part A1, A2 ─────────────────────────────────


def stage_inventory() -> None:
    a = load_alerts()
    keep = ["id", "inst", "series", "kind", "slot", "aside", "sign", "src", "delayed", "u_entry",
            "u_stop", "u_target", "premium", "costs_inr", "qty_alert", "synthetic", "strike",
            "delta", "bid", "ask", "created", "bar_ts", "day", "period", "pstatus",
            "close_reason", "grade", "data_source", "qty", "entry_ts", "exit_ts", "entry_paise",
            "exit_paise", "net_inr", "base_inr", "pct", "costs_paise", "risk_paise",
            "fill_under", "prem_src", "exit_level", "basis_paise", "hour"]  # fmt: skip
    save(a[keep], "alerts.csv")
    with ro() as c:
        st = pd.read_sql("select status, count(*) n from desk_signals group by status", c)
        fq = pd.read_sql("select ts, kind from fill_quotes where kind = 'SAMPLE'", c)
    fq["t"] = utc_naive_to_ist(fq["ts"])
    fq["day"] = fq["t"].dt.date
    days = []
    for d in sessions():
        x = a[a.day == d]
        q = fq[fq.day == d]
        days.append({
            "day": d, "alerts": len(x), "index": int((x.kind == "INDEX").sum()),
            "stock": int((x.kind == "STOCK").sum()),
            "src": "/".join(sorted(set(x.src))) if len(x) else "",
            "grade": "/".join(f"{k}:{v}" for k, v in x.grade.value_counts().items()),
            "quotes_from": q.t.min().strftime("%H:%M") if len(q) else "",
            "quotes_to": q.t.max().strftime("%H:%M") if len(q) else "",
            "first_alert": x.created.min().strftime("%H:%M") if len(x) else "",
            "last_alert": x.created.max().strftime("%H:%M") if len(x) else "",
        })  # fmt: skip
    save(pd.DataFrame(days), "days.csv")
    save(st, "status_counts.csv")
    print(pd.DataFrame(days).to_string())
    print(st.to_string())


def paper_tables(a: pd.DataFrame) -> list[str]:
    L = []  # noqa: N806
    f = a[a.pstatus == "CLOSED"].copy()
    f["eday"] = f["entry_ts"].dt.date
    nofill = int((a.pstatus != "CLOSED").sum())
    L.append(f"paper trades closed {len(f)} of {len(a)} alerts (no fill: {nofill})")
    for per in ("before 13 Nov", "from 13 Nov", None):
        g = f if per is None else f[f.period == per]
        L.append(f"-- {per or 'all'}: " + fmt(stats(g.pct, g.eday, g.net_inr), True))
        for col in ("kind", "slot", "inst", "aside", "hour", "close_reason", "grade",
                    "data_source"):  # fmt: skip
            for k, gg in g.groupby(col, dropna=False):
                L.append(
                    f"   {col:12s} {str(k):22s} " + fmt(stats(gg.pct, gg.eday, gg.net_inr), True)
                )
    return L


# ───────────────────────────── Part A3 ─────────────────────────────────────


def minute(series: str) -> pd.DataFrame:
    init_read_only()
    b = h._cache.bars(series, "1m")
    lo = pd.Timestamp(LIVE_FIRST - dt.timedelta(days=40), tz=b.index.tz)
    return b[b.index >= lo]


def stage_excursions() -> None:
    a = pd.read_csv(OUT / "alerts.csv", parse_dates=["created", "entry_ts", "exit_ts"])
    a = a[a.pstatus == "CLOSED"]
    rows = []
    cache: dict[str, pd.DataFrame] = {}
    for r in a.itertuples():
        if r.series not in cache:
            cache[r.series] = minute(r.series)
        b = cache[r.series]
        touch = r.close_reason in ("STOP", "TRAIL_STOP", "TARGET")
        hi_t = pd.Timestamp(r.exit_ts)
        m = (b.index >= pd.Timestamp(r.entry_ts)) & (
            (b.index <= hi_t) if touch else (b.index < hi_t)
        )
        w = b[m]
        if w.empty or not np.isfinite(r.fill_under):
            continue
        e, s = float(r.fill_under), int(r.sign)
        fav = (w.high if s == 1 else w.low).to_numpy(float)
        adv = (w.low if s == 1 else w.high).to_numpy(float)
        best_path = s * (fav - e)
        k = int(np.argmax(best_path))
        best, worst = max(best_path.max(), 0.0), min((s * (adv - e)).min(), 0.0)
        risk = abs(float(r.u_entry) - float(r.u_stop)) if np.isfinite(r.u_stop) else np.nan
        tgt = abs(float(r.u_target) - float(r.u_entry)) if np.isfinite(r.u_target) else np.nan
        move = tgt if np.isfinite(tgt) and tgt > 0 else 3 * risk
        rows.append({
            "id": r.id, "kind": r.kind, "slot": r.slot, "period": r.period, "pct": r.pct,
            "best_pct": best / e * 100, "worst_pct": worst / e * 100, "best_r": best / risk,
            "worst_r": worst / risk, "to_best_min": (w.index[k] - w.index[0]).total_seconds() / 60,
            "held_min": len(w), "move_is_target": bool(np.isfinite(tgt) and tgt > 0),
            "frac": best / move if move > 0 else np.nan, "risk_pct": risk / e * 100,
        })  # fmt: skip
    df = pd.DataFrame(rows)
    save(df, "excursions.csv")
    print(f"excursions: {len(df)} trades")


def excursion_tables(x: pd.DataFrame) -> list[str]:
    L = ["kind / period: trades, best move % (median), worst % (median), best R, worst R,"  # noqa: N806
         " minutes to best (median) of held, share reaching 25/50/80/100% of the planned move,"
         " and of those the share that still lost"]  # fmt: skip
    for (k, per), g in list(x.groupby(["kind", "period"])) + [(("ALL", "all"), x)]:
        reach = []
        for f in (0.25, 0.5, 0.8, 1.0):
            hit = g.frac >= f
            lost = (g.pct[hit] < 0).mean() * 100 if hit.any() else np.nan
            reach.append(f"{100 * hit.mean():4.0f}% ({lost:3.0f}% lost)")
        L.append(f"   {k:6s} {per:14s} n {len(g):3d}  best {g.best_pct.median():+.2f}%  worst "
                 f"{g.worst_pct.median():+.2f}%  best {g.best_r.median():.2f} R  worst "
                 f"{g.worst_r.median():.2f} R  to best {g.to_best_min.median():.0f} of "
                 f"{g.held_min.median():.0f} min  reach " + " / ".join(reach))  # fmt: skip
    L.append(f"   planned move = the alert's target where it had one ({int(x.move_is_target.sum())}"
             f" trades), else 3 risks ({int((~x.move_is_target).sum())} trades)")  # fmt: skip
    return L


# ───────────────────────────── Part A4 ─────────────────────────────────────


def stage_fills() -> None:
    with ro() as c:
        q = pd.read_sql(
            "select ts, kind, instrument_key, segment, label, alert_id, side, offset_sec, ltp,"
            " bid, ask from fill_quotes",
            c,
        )
    q["t"] = utc_naive_to_ist(q["ts"])
    ok = (q.bid > 0) & (q.ask >= q.bid)
    q["mid"] = (q.bid + q.ask) / 2
    q["half_rs"] = np.where(ok, (q.ask - q.bid) / 2, np.nan)
    q["half_pct"] = q.half_rs / q.mid * 100
    q["model_half_pct"] = MODEL_HALF_PCT + MODEL_TICKS * TICK / q.mid * 100
    hm = q.t.dt.strftime("%H:%M")
    q["bucket"] = ""
    for lo, hi in SPREAD_BUCKETS:
        q.loc[(hm >= lo) & (hm < hi), "bucket"] = f"{lo} to {hi[:5]}"
    q["opt"] = q.segment.str.endswith("_FNO")
    save(q.drop(columns=["ts"]), "quotes.csv")
    print(f"quotes: {len(q)}")


def fill_tables(q: pd.DataFrame, a: pd.DataFrame) -> list[str]:
    L = []  # noqa: N806
    s = q[(q.kind == "SAMPLE") & q.opt & q.half_pct.notna()]
    L.append(f"option samples (monthly ~0.65 delta CE and PE): {len(s)} quotes, "
             f"{s.t.dt.date.nunique()} sessions")  # fmt: skip
    L.append("   index          n   premium  half spread Rs (median, p90)  half % (median, p90)"
             "  model half %  (0.6% + 2 ticks)")  # fmt: skip
    for k, g in list(s.groupby("instrument_key")) + [("ALL", s)]:
        L.append(f"   {k:14s} {len(g):4d} {g.mid.median():8.1f}   {g.half_rs.median():7.2f}"
                 f" {g.half_rs.quantile(0.9):7.2f}             {g.half_pct.median():5.2f}"
                 f" {g.half_pct.quantile(0.9):5.2f}         {g.model_half_pct.median():5.2f}")  # fmt: skip
    L.append("   by time of day (all five indices): half spread %, median")
    for k, g in s.groupby("bucket"):
        per = "  ".join(f"{i.replace('_OPT', '')} {gg.half_pct.median():.2f}"
                        for i, gg in g.groupby("instrument_key"))  # fmt: skip
        L.append(f"   {k:16s} all {g.half_pct.median():.2f} (n {len(g)})  {per}")
    st = q[(q.kind == "SAMPLE") & ~q.opt & q.half_pct.notna()]
    by = st.groupby("instrument_key").half_pct.median()
    L.append(f"stock samples: {len(st)} quotes on {st.instrument_key.nunique()} stocks; half spread"
             f" median of medians {by.median():.3f}% (range {by.min():.3f} to {by.max():.3f}),"
             f" model slippage 0.03% a side")  # fmt: skip
    al = q[(q.kind == "ALERT") & q.opt]
    L.append(f"alerted option contracts quoted at the alert: {len(al)}; half spread % median "
             f"{al.half_pct.median():.2f}, mean {al.half_pct.mean():.2f}")  # fmt: skip
    ch = a[(a.kind == "INDEX") & a.bid.notna() & (a.ask > 0)]
    hs = (ch.ask - ch.bid) / 2 / ((ch.ask + ch.bid) / 2) * 100
    L.append(f"live-chain alerts carrying bid/ask: {len(ch)}; half spread % median "
             f"{hs.median():.2f}, mean {hs.mean():.2f}, max {hs.max():.2f}")  # fmt: skip
    bs = a[(a.kind == "INDEX") & a.basis_paise.notna() & (a.premium > 0)]
    bp = bs.basis_paise / 100 / bs.premium * 100
    L.append(f"chain premium minus the model's (the paper book's basis), % of premium: n {len(bs)},"
             f" median {bp.median():+.1f}, range {bp.min():+.1f} to {bp.max():+.1f}")  # fmt: skip
    L.append("waiting after the alert (exec price: ask to buy), worse by % of premium:")
    base = q[q.kind == "ALERT"].set_index("alert_id")
    fo = q[q.kind == "FOLLOW"]
    for off, g in fo.groupby("offset_sec"):
        d = []
        for r in g.itertuples():
            b0 = base.loc[r.alert_id] if r.alert_id in base.index else None
            if b0 is None or not (b0.ask > 0 and r.ask > 0):
                continue
            d.append((r.ask - b0.ask) / b0.ask * 100)
        d = np.array(d)
        L.append(f"   after {int(off):3d} s: n {len(d)}, mean {d.mean():+.2f}%, median "
                 f"{np.median(d):+.2f}%, worse in {int((d > 0).sum())}")  # fmt: skip
    return L


# ───────────────────────────── preps over the live window ──────────────────


_PREPS: dict[str, h.Prep] = {}


def live_prep(inst: str, tfs: tuple[str, ...] = ("60m",)) -> h.Prep:
    """`h.prepare` with the bars through LIVE_LAST; the entry window is the live window."""
    key = f"{inst}|{','.join(tfs)}"
    if key in _PREPS:
        return _PREPS[key]
    init_read_only()
    old = h.T_END
    h.T_END = LIVE_LAST
    try:
        p = h.prepare(inst, tfs=tfs)
    finally:
        h.T_END = old
    days = np.array([p.sess_dates[s] for s in p.sess])
    live = (days >= LIVE_FIRST) & (days <= LIVE_LAST)
    p.inwin = (p.minute >= h.ENTRY_START_MIN) & (p.minute <= h.ENTRY_END_MIN) & live
    p.year = np.where(live, 0, p.year)  # never used for a period here
    _PREPS[key] = p
    return p


def bar_at(p: h.Prep, ts: pd.Timestamp, after: bool = False) -> int:
    """The first bar at (or, `after`, strictly after) `ts`."""
    t = int(pd.Timestamp(ts).as_unit("ns").value)
    return int(np.searchsorted(p.ns, t, side="right" if after else "left"))


def model_ret(p: h.Prep, side: int, e: int, x: int, px: float) -> float:
    r = h.pnl(p, side, 0, np.array([e]), np.array([x]), np.array([px]), np.array([p.ns[x]]))
    return float(r["ret"][0])


# ───────────────────────────── Part B ──────────────────────────────────────


def stage_parity() -> None:
    from onelot.data.universe import load_specs
    from onelot.desk.config import load_desk_config
    from onelot.sim.bars import StoreBars
    from onelot.sim.engine import build_signals
    from onelot.strategies.registry import get_strategy

    a = pd.read_csv(OUT / "alerts.csv", parse_dates=["created", "bar_ts", "entry_ts", "exit_ts"])
    live = a[a.slot == "microtrend 3m"]
    series, instruments = load_specs()
    cfg = load_desk_config()
    bars = StoreBars(ro_engine())
    rows = []
    for inst in cfg.indices:
        slot = next(s for s in inst.strategies if s.name == "microtrend")
        params = {k: v for k, v in slot.params.items() if k != "evidence"}
        spec = instruments[inst.tracked]
        mins = bars.minute_bars(inst.key, None, None)
        lo = pd.Timestamp(LIVE_FIRST - dt.timedelta(days=120)).tz_localize(IST)
        mins = mins[mins.index >= lo]
        strat = get_strategy("microtrend", {**params, "timeframe": "3m"})
        f, _ = build_signals(strat, spec, inst.key, "3m", mins, bars, series.get(inst.key))
        p = live_prep(inst.tracked)
        at = pd.DatetimeIndex(f.index) + pd.Timedelta(minutes=3)
        le, se = f["long_entry"].to_numpy(bool), f["short_entry"].to_numpy(bool)
        lx, sx = f["long_exit"].to_numpy(bool), f["short_exit"].to_numpy(bool)
        dates = np.asarray(at.date)
        busy = pd.Timestamp("2000-01-01", tz=IST)
        for i in range(len(f)):
            d, t = dates[i], at[i]
            if not (LIVE_FIRST <= d <= LIVE_LAST) or t <= busy or not (le[i] or se[i]):
                continue
            hm = t.strftime("%H:%M")
            if not ("09:30" <= hm <= "14:45"):
                continue
            side = 1 if le[i] else -1
            ex = lx if side == 1 else sx
            limit = pd.Timestamp(dt.datetime.combine(d, dt.time(15, 15)), tz=IST)
            x_t, why = None, "SESSION_END"
            for j in range(i + 1, len(f)):
                if dates[j] != d or at[j] >= limit:
                    break
                if ex[j]:
                    x_t, why = at[j], "SIGNAL_EXIT"
                    break
            e = bar_at(p, t)
            if x_t is None:
                x = int(p.xi[p.sess[e]])
                px = float(p.xp[p.sess[e]])
            else:
                x = bar_at(p, x_t)
                px = float(p.o[x])
            busy = pd.Timestamp(p.index[x])
            rows.append({
                "inst": inst.tracked, "day": d, "bar_ts": f.index[i], "side": side,
                "entry_t": p.index[e], "entry": float(p.o[e]), "exit_t": p.index[x],
                "exit": px, "why": why, "bp": side * (px / p.o[e] - 1) * 1e4,
                "model": model_ret(p, side, e, x, px),
            })  # fmt: skip
        print(f"{inst.key}: {sum(r['inst'] == inst.tracked for r in rows)} replay trades",
              flush=True)  # fmt: skip
    rp = pd.DataFrame(rows)
    rp["bar_ts"] = pd.to_datetime(rp["bar_ts"]).dt.tz_convert(IST)
    rp["match"] = ""
    for r in live.itertuples():
        m = (rp.inst == r.inst) & (rp.bar_ts == r.bar_ts) & (rp.side == r.sign)
        rp.loc[m, "match"] = r.id
    save(rp, "parity.csv")
    print(rp.to_string())


def parity_tables(rp: pd.DataFrame, a: pd.DataFrame, days: pd.DataFrame) -> list[str]:
    L = []  # noqa: N806
    live = a[a.slot == "microtrend 3m"]
    rp = rp.copy()
    rp["day"] = pd.to_datetime(rp["day"]).dt.date
    days = days.copy()
    days["day"] = pd.to_datetime(days["day"]).dt.date
    up = {r.day: (r.quotes_from, r.quotes_to, r.alerts) for r in days.itertuples()}
    for per, g in (("before 13 Nov (slot not yet live)", rp[rp.day < LINEUP]),
                   ("from 13 Nov (slot live)", rp[rp.day >= LINEUP])):  # fmt: skip
        L.append(f"replay {per}: " + fmt(stats(g.model, g.day)) + f"  index {g.bp.mean():+.1f} bp")
        for k, gg in g.groupby("inst"):
            L.append(f"   {k:15s} " + fmt(stats(gg.model, gg.day)))
    L.append("replay from 13 Nov, session by session: trades (owner four + MIDCPNIFTY), desk state")
    for d, g in rp[rp.day >= LINEUP].groupby("day"):
        qf, qt, n = up.get(d, ("", "", 0))
        state = f"quotes {qf} to {qt}" if isinstance(qf, str) and qf else "no quotes recorded"
        inside = 0
        if isinstance(qf, str) and qf:
            hm = g.entry_t.astype(str).str[11:16]
            inside = int(((hm >= qf) & (hm <= qt)).sum())
        L.append(f"   {d}  {len(g)} trades, {inside} while the desk was up ({state}); "
                 f"live microtrend alerts {int((live.day.astype(str) == str(d)).sum())}")  # fmt: skip
    for d in sorted(set(days.day) - set(rp.day)):
        if d >= LINEUP:
            L.append(f"   {d}  0 replay trades")
    m = rp[rp.match != ""]
    L.append(f"live microtrend alerts {len(live)}; matched by the replay on bar and side "
             f"{len(m)}")  # fmt: skip
    for r in live.itertuples():
        mm = rp[rp.match == r.id]
        if len(mm):
            q = mm.iloc[0]
            L.append(f"   {r.inst:15s} live fill {pd.Timestamp(r.entry_ts):%H:%M} at "
                     f"{r.fill_under:.2f}, exit {pd.Timestamp(r.exit_ts):%H:%M} {r.close_reason};"
                     f" replay fill {pd.Timestamp(q.entry_t):%H:%M} at {q.entry:.2f}, exit "
                     f"{pd.Timestamp(q.exit_t):%H:%M} {q.why}; live entry worse by "
                     f"{r.sign * (r.fill_under - q.entry) / q.entry * 1e4:+.1f} bp")  # fmt: skip
        else:
            L.append(f"   {r.inst:15s} {pd.Timestamp(r.bar_ts):%Y-%m-%d %H:%M} not in the replay")
    return L


# ───────────────────────────── Part C ──────────────────────────────────────


class ReadOnlyOptionStore:
    """`OptionBarStore.get` on a read-only connection (the class itself opens for writing)."""

    def __new__(cls, path: Path):  # noqa: ANN204
        from onelot.data.expired_options import OptionBarStore

        obj = OptionBarStore.__new__(OptionBarStore)
        obj.path = Path(path)
        obj._lock = threading.Lock()
        obj._db = sqlite3.connect(
            f"file:{quote(str(path))}?mode=ro", uri=True, check_same_thread=False
        )
        obj._mem = {}
        return obj


def real_pricers() -> dict:
    import mtf_real_premiums as mr
    from expired_options_fetch import underlyings

    from onelot.config import CostConfig
    from onelot.costs import CostModel

    init_read_only()
    store = ReadOnlyOptionStore(OPT_STORE)
    unds = underlyings()
    bt = h._settings.backtest
    out = {}
    for inst in OWNER4 + [TRAINING]:
        spec = h._cache.spec(inst)
        if spec.series_key not in unds:
            continue
        costs = CostModel(CostConfig.load(
            h._settings.project_root / "config" / "costs.yaml").product(spec.product))  # fmt: skip
        out[inst] = mr.CarryPricer(unds[spec.series_key], spec, bt, costs, store,
                                   h._cache.bars(spec.series_key, "1m"), 1, source=None,
                                   slip_pct=0.0)  # fmt: skip
    return out


def ratchet_exit(p: h.Prep, side: int, e: int, end: int, end_px: float, risk: float,
                 cost: float) -> tuple[int, float, str]:  # fmt: skip
    from onelot.manage import live as rlive
    from onelot.manage import ratchet as R  # noqa: N812

    prm = rlive.RatchetSettings().params
    plan = R.plan_from_risk(side, float(p.o[e]), risk, 3.0, cost)
    stt = R.start(plan)
    for k in range(e, end):
        hit = R.exit_on_bar(plan, stt, float(p.o[k]), float(p.h[k]), float(p.l[k]))
        if hit is not None:
            return k, float(hit[1]), hit[0]
        stt, _ = R.advance(plan, stt, float(p.h[k]), float(p.l[k]), prm)
    return end, end_px, "OUTER"


def prev_session_flags(series: str, day: dt.date) -> tuple[str | None, bool | None]:
    """(size of the previous session's state, India VIX rose in the previous session)."""
    states = load_module("states_us088", Path.cwd() / "src" / "onelot" / "mllab" / "states.py")
    init_read_only()
    d = h._cache.bars(series, "1d")
    cl = states.classify(d)
    days = np.array([x.date() for x in cl.index])
    i = int(np.searchsorted(days, day)) - 1
    size = cl["size"].iloc[i] if i >= 0 else None
    v = h._cache.vix()
    vd = np.array([x.date() for x in v.index])
    j = int(np.searchsorted(vd, day)) - 1
    rose = None
    if j >= 1 and vd[j] == days[i]:  # the VIX of that same previous session is known
        rose = bool(v.iloc[j] > v.iloc[j - 1])
    return size, rose


def stage_whatif() -> None:
    from onelot.manage import live as rlive

    a = pd.read_csv(OUT / "alerts.csv", parse_dates=["created", "entry_ts", "exit_ts"])
    a = a[(a.pstatus == "CLOSED") & ~a.series.isin(EXCLUDED)]
    pricers = real_pricers()
    rows = []
    for r in a.itertuples():
        p = live_prep(r.inst)
        side = int(r.sign)
        created = pd.Timestamp(r.created)
        e = bar_at(p, created, after=False)
        if p.index[e] <= created:  # the open of the minute after the alert
            e += 1
        s = int(p.sess[e])
        xi = int(p.xi[s])
        xr = bar_at(p, pd.Timestamp(r.exit_ts))
        touch = r.close_reason in ("STOP", "TRAIL_STOP", "TARGET")
        if r.close_reason == "SESSION_END" or xr >= xi:
            xr, xpx = xi, float(p.xp[s])
        else:
            xpx = float(r.exit_level) if touch else float(p.o[xr])
        if xr <= e:
            continue
        b = {"id": r.id, "inst": r.inst, "kind": r.kind, "slot": r.slot, "period": r.period,
             "day": str(r.day), "hour": bucket(p.index[e].tz_convert(IST)), "side": side,
             "paper": r.pct}  # fmt: skip
        b["recorded"] = model_ret(p, side, e, xr, xpx)
        b["fade"] = model_ret(p, -side, e, xr, xpx)
        for mnt in (15, 30, 60):
            x = min(e + mnt, xi)
            b[f"time{mnt}"] = model_ret(p, side, e, x, float(p.xp[s] if x == xi else p.o[x]))
        for dly in (1, 3, 5):
            b[f"delay{dly}"] = model_ret(p, side, e + dly, xr, xpx) if e + dly < xr else np.nan
        mins = minute(r.series)
        risk = rlive.first_risk(mins, created, r.slot.split()[1], rlive.RatchetSettings())
        if r.kind == "INDEX":
            cost = rlive.unit_cost(float(r.costs_inr or 0), int(r.qty_alert or 1),
                                   r.delta if np.isfinite(r.delta) else 0.65, option=True)  # fmt: skip
        else:
            cost = rlive.unit_cost(float(r.costs_inr or 0), 1)
        outer, opx = (xr, xpx) if r.close_reason == "SIGNAL_EXIT" else (xi, float(p.xp[s]))
        if rlive.usable(risk):
            k, lvl, why = ratchet_exit(p, side, e, outer, opx, risk, cost)
            b["ratchet"], b["ratchet_why"] = model_ret(p, side, e, k, lvl), why
        b["prev_size"], b["vix_rose"] = prev_session_flags(r.series, p.sess_dates[s])
        b["real"] = np.nan
        if r.kind == "INDEX" and r.inst in pricers:
            st = float(p.opt(side).strike[e])
            how = "close" if (xr == xi and p.xclose[s]) else ("touch" if touch else "open")
            try:
                got = pricers[r.inst].price_one(
                    int(p.ns[e] // 1_000_000_000), int(p.ns[xr] // 1_000_000_000), st, side,
                    how, xpx, {"ret": b["recorded"], "inr": 0.0},
                )  # fmt: skip
                b["real"] = got.get("real_ret") if got.get("real_ret") is not None else np.nan
                b["unpriced"] = got.get("unpriced")
            except Exception as exc:  # noqa: BLE001
                b["unpriced"] = f"{type(exc).__name__}"
        rows.append(b)
    df = pd.DataFrame(rows)
    save(df, "whatif.csv")
    print(f"whatif: {len(df)} alerts; real-priced {int(df.real.notna().sum())}")


VARIANTS = ("paper", "recorded", "ratchet", "time15", "time30", "time60", "fade", "delay1",
            "delay3", "delay5")  # fmt: skip


def whatif_tables(w: pd.DataFrame) -> list[str]:
    L = []  # noqa: N806
    for kind, unit in (("INDEX", "% of premium"), ("STOCK", "% of price, one share")):
        g = w[(w.kind == kind) & (w.inst != TRAINING)]
        L.append(f"{kind} alerts ({unit}; MIDCPNIFTY left out), {len(g)} alerts on "
                 f"{g.day.nunique()} days")  # fmt: skip
        for v in VARIANTS:
            s = stats(g[v], g.day)
            extra = ""
            if kind == "INDEX" and v != "paper":
                s2 = stats(g[v] - POINT_GAP, g.day)
                extra = f"   less 1.0 point: {s2['m']:+.2f}%"
            L.append(f"   {v:9s} " + fmt(s) + extra)
        if kind == "INDEX":
            pr = g[g.real.notna()]
            if len(pr):
                L.append(f"   real premiums, recorded exits: {len(pr)} priced, real {pr.real.mean():+.2f}%"
                         f" against model {pr.recorded.mean():+.2f}% on the same trades")  # fmt: skip
        for col in ("hour", "prev_size", "vix_rose"):
            for k, gg in g.groupby(col, dropna=False):
                L.append(f"   {col:9s} {str(k):12s} paper " + fmt(stats(gg.paper, gg.day))
                         + f" | model {stats(gg.recorded, gg.day)['m']:+.2f}%")  # fmt: skip
        L.append("   by slot, model on recorded exits / paper:")
        for k, gg in g.groupby("slot"):
            L.append(f"     {k:20s} " + fmt(stats(gg.recorded, gg.day)) + " | paper "
                     + f"{stats(gg.paper, gg.day)['m']:+.2f}%")  # fmt: skip
        if "ratchet_why" in g:
            L.append("   ratchet exits: " + ", ".join(
                f"{k} {v}" for k, v in g.ratchet_why.value_counts().items()))  # fmt: skip
    return L


# ───────────────────────────── Part D ──────────────────────────────────────


def stage_history() -> None:
    init_read_only()
    states = load_module("states_us088", Path.cwd() / "src" / "onelot" / "mllab" / "states.py")
    t = pd.read_csv(MT_DIR / "trades.csv")
    t = t[t["need"] == 3].reset_index(drop=True)
    real = pd.DataFrame([json.loads(x) for x in (MT_DIR / "real.jsonl").read_text().splitlines()])
    real = real.drop_duplicates(["inst", "e0"])[["inst", "e0", "real_ret"]]
    t = t.merge(real, on=["inst", "e0"], how="left")
    t["dday"] = pd.to_datetime(t["day"]).dt.date
    vix = h._cache.vix()
    vd = [x.date() for x in vix.index]
    vrise = dict(zip(vd[1:], (vix.to_numpy()[1:] > vix.to_numpy()[:-1]).tolist(), strict=True))
    t["first_hour"] = t["minute"] < 10 * 60 + 15
    t["put"] = t["side"] == -1
    t["prev_active"] = False
    t["vix_fell_prev"] = False
    t["delay3"] = np.nan
    for inst in h.INDEX_OPTIONS:
        spec = h._cache.spec(inst)
        cl = states.classify(h._cache.bars(spec.series_key, "1d"))
        days = np.array([x.date() for x in cl.index])
        m = t["inst"] == inst
        dd = t.loc[m, "dday"].to_numpy()
        i = np.searchsorted(days, dd) - 1
        size = cl["size"].to_numpy()
        t.loc[m, "prev_active"] = [(k >= 0 and size[k] == "active") for k in i]
        prevd = [days[k] if k >= 0 else None for k in i]
        t.loc[m, "vix_fell_prev"] = [vrise.get(x) is False for x in prevd]
        # the same trades entered 3 minutes later, same exit
        p = h.prepare(inst, tfs=("60m",))
        sub = t[m]
        e = np.searchsorted(p.ns, sub["e0"].to_numpy() * 1_000_000_000)
        x = np.searchsorted(p.ns, sub["x0"].to_numpy() * 1_000_000_000)
        ok = (e + 3 < x) & (p.ns[e] == sub["e0"].to_numpy() * 1_000_000_000)
        vals = np.full(len(sub), np.nan)
        for side in (1, -1):
            k = ok & (sub["side"].to_numpy() == side)
            if k.any():
                vals[k] = h.pnl(p, side, 0, e[k] + 3, x[k], sub["level"].to_numpy()[k],
                                p.ns[x[k]])["ret"]  # fmt: skip
        t.loc[m, "delay3"] = vals
        print(f"{inst}: {int(m.sum())} trades, delay priced {int(np.isfinite(vals).sum())}",
              flush=True)  # fmt: skip
    save(t, "history.csv")


def cell_stats(g: pd.DataFrame, col: str = "ret") -> dict:
    per = [g.loc[g.period == k, col] for k in range(3)]
    dd = [g.loc[g.period == k, "day"] for k in range(3)]
    return {
        "n": [len(x.dropna()) for x in per],
        "days": [d[x.notna()].nunique() for x, d in zip(per, dd, strict=True)],
        "m": [x.mean() for x in per],
        "t": [
            day_t(x.dropna().to_numpy(), d[x.notna()].to_numpy())
            for x, d in zip(per, dd, strict=True)
        ],
        "all": stats(g[col], g.day),
    }


def verdict(c: dict) -> str:
    n, d, m, t = c["n"], c["days"], c["m"], c["t"]
    if not (n[0] >= 60 and d[0] >= 30):
        return "not examined"
    if not (m[0] > 1.0 and t[0] >= 2.5):
        return "examined"
    if not (m[1] > 0 and t[1] >= 1.0 and n[1] >= 20):
        return "discovered"
    if not (m[2] > 0 and n[2] >= 20):
        return "confirmed"
    return "passed (model)"


def is_avoid(c: dict) -> bool:
    return c["n"][0] >= 60 and c["t"][0] <= -2.5 and c["m"][1] < 0 and c["m"][2] < 0


LABELS = {"H1 entry before 10:15": "first_hour", "H2 put": "put",
          "H3 previous session active": "prev_active",
          "H4 VIX fell in the previous session": "vix_fell_prev"}  # fmt: skip


def like_for_like(g: pd.DataFrame, col: str = "ret") -> pd.Series:
    """NIFTY and BANKNIFTY: the real result where priced, else the model less the gap."""
    pr = g[g.real_ret.notna()]
    gap = float((pr.real_ret - pr[col]).mean()) if len(pr) else -POINT_GAP
    return g.real_ret.fillna(g[col] + gap)


def history_tables(t: pd.DataFrame) -> list[str]:
    L = []  # noqa: N806
    own = t[t.inst.isin(OWNER4)].copy()
    rng = np.random.default_rng(88)
    L.append(f"base: four owner indices {len(own)} trades, model " + fmt(stats(own.ret, own.day)))
    L.append("cell: TUNE n/days % (t) | CHOOSE n % (t) | TEST n % | whole span | verdict")
    counts = {"examined": 0, "discovered": 0, "confirmed": 0, "passed": 0, "avoid": 0}
    cells = []
    for name, col in LABELS.items():
        for neg in (False, True):
            m = own[col].astype(bool) != neg
            cells.append((name + (" (opposite)" if neg else ""), own[m], "ret", col, neg))
    dl = own[own.delay3.notna()]
    cells.append(("H5 entry 3 minutes late", dl, "delay3", None, False))
    cells.append(("H5 same trades on time", dl, "ret", None, False))
    for name, g, col, _lab, _neg in cells:
        c = cell_stats(g, col)
        v = verdict(c)
        av = is_avoid(c)
        if v != "not examined" and "on time" not in name:
            counts["examined"] += 1
            counts["discovered"] += v in ("discovered", "confirmed", "passed (model)")
            counts["confirmed"] += v in ("confirmed", "passed (model)")
            counts["passed"] += v == "passed (model)"
            counts["avoid"] += av
        nb = g[g.inst.isin(["NIFTY_OPT", "BANKNIFTY_OPT"])]
        lfl = like_for_like(nb, col) if col == "ret" else nb[col] - POINT_GAP
        L.append(f"   {name:42s} {c['n'][0]:4d}/{c['days'][0]:3d} {c['m'][0]:+6.2f} ({c['t'][0]:+4.1f})"
                 f" | {c['n'][1]:4d} {c['m'][1]:+6.2f} ({c['t'][1]:+4.1f}) | {c['n'][2]:4d}"
                 f" {c['m'][2]:+6.2f} | {c['all']['m']:+6.2f} (t {c['all']['t']:+4.1f})"
                 f" | {v}{' AVOID' if av else ''} | real-like N+BN {lfl.mean():+.2f}% (n {len(lfl)})")  # fmt: skip
    L.append("   counts (9 cells: 4 labels, 4 opposites, the delay): " + ", ".join(
        f"{k} {v}" for k, v in counts.items()))  # fmt: skip
    L.append(f"   luck by arithmetic: {counts['examined']} x 0.6% x 16% x 50% = "
             f"{counts['examined'] * 0.006 * 0.16 * 0.5:.3f} passes")  # fmt: skip
    plc = {"discovered": [], "confirmed": [], "passed": [], "avoid": []}
    own["year"] = pd.to_datetime(own.day).dt.year
    for _ in range(20):
        dc = cf = ps = av = 0
        for _name, col in LABELS.items():
            sh = own.copy()
            for _k, g in own.groupby(["inst", "year"]):
                if col in ("put", "first_hour"):  # a label of the trade, not of the day
                    sh.loc[g.index, col] = rng.permutation(g[col].to_numpy())
                    continue
                dl_ = g.drop_duplicates("day")[["day", col]]
                perm = rng.permutation(dl_[col].to_numpy())
                mp = dict(zip(dl_["day"], perm, strict=True))
                sh.loc[g.index, col] = g["day"].map(mp).to_numpy()
            for neg in (False, True):
                c = cell_stats(sh[sh[col].astype(bool) != neg])
                v = verdict(c)
                dc += v in ("discovered", "confirmed", "passed (model)")
                cf += v in ("confirmed", "passed (model)")
                ps += v == "passed (model)"
                av += is_avoid(c)
        for k, x in zip(
            ("discovered", "confirmed", "passed", "avoid"), (dc, cf, ps, av), strict=True
        ):
            plc[k].append(x)
    L.append("   placebo (labels shuffled over days inside index and calendar year, 20 shuffles, "
             "8 label cells): mean " + ", ".join(f"{k} {np.mean(v):.2f}" for k, v in plc.items()))  # fmt: skip
    mid = t[t.inst == TRAINING]
    L.append("MIDCPNIFTY (training, apart): " + ", ".join(
        f"{n} {stats(mid[mid[c].astype(bool)].ret, mid[mid[c].astype(bool)].day)['m']:+.2f}%"
        for n, c in LABELS.items()))  # fmt: skip
    return L


# ───────────────────────────── report ──────────────────────────────────────


def stage_report() -> None:
    L: list[str] = []  # noqa: N806
    a = pd.read_csv(OUT / "alerts.csv", parse_dates=["created", "entry_ts", "exit_ts", "bar_ts"])
    a["day"] = pd.to_datetime(a["day"]).dt.date
    days = pd.read_csv(OUT / "days.csv")
    st = pd.read_csv(OUT / "status_counts.csv")
    L.append("PART A1: INVENTORY")
    L.append(f"alerts {len(a)}, {a.created.min():%Y-%m-%d} to {a.created.max():%Y-%m-%d}, "
             f"{a.day.nunique()} days with an alert; status " + ", ".join(
                 f"{r.status} {r.n}" for r in st.itertuples()))  # fmt: skip
    for col in ("period", "kind", "slot", "inst", "aside", "src", "grade", "data_source"):
        L.append(f"   {col:12s} " + ", ".join(f"{k} {v}" for k, v in
                                               a[col].fillna("none").value_counts().items()))  # fmt: skip
    L.append("sessions of the live window:")
    L.append(days.to_string(index=False))
    L.append("")
    L.append("PART A2: PAPER TRADES AS RECORDED (% of premium or of price; Rs a trade)")
    L += paper_tables(a)
    L.append("")
    if (OUT / "excursions.csv").exists():
        L.append("PART A3: EXCURSIONS")
        L += excursion_tables(pd.read_csv(OUT / "excursions.csv"))
        L.append("")
    if (OUT / "quotes.csv").exists():
        L.append("PART A4: FILL COSTS")
        q = pd.read_csv(OUT / "quotes.csv", parse_dates=["t"])
        L += fill_tables(q, a)
        L.append("")
    if (OUT / "parity.csv").exists():
        L.append("PART B: PARITY")
        rp = pd.read_csv(OUT / "parity.csv", parse_dates=["bar_ts", "entry_t", "exit_t"])
        rp["match"] = rp["match"].fillna("")
        L += parity_tables(rp, a, days)
        L.append("")
    if (OUT / "whatif.csv").exists():
        L.append("PART C: WHAT IF (model premiums unless marked)")
        L += whatif_tables(pd.read_csv(OUT / "whatif.csv"))
        L.append("")
    if (OUT / "history.csv").exists():
        L.append("PART D: HYPOTHESES ON FIVE YEARS (micro-trend, model premiums)")
        L += [f"   {k}: {v}" for k, v in HYPOTHESES.items()]
        L += history_tables(pd.read_csv(OUT / "history.csv"))
    text = "\n".join(L) + "\n"
    (OUT / "report.txt").write_text(text, encoding="utf-8")
    print(text)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    stages = sys.argv[1:] or ["report"]
    for name in stages:
        {"inventory": stage_inventory, "excursions": stage_excursions, "fills": stage_fills,
         "parity": stage_parity, "whatif": stage_whatif, "history": stage_history,
         "report": stage_report}[name]()  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
