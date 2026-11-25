"""Independent check of docs/research/LIVE_ALERTS_2026-11-24.md (US-088).

Written from the schema and the writers of the tables, not from `live_alerts_study.py`: nothing
of the study is imported. Stores are opened through SQLite URIs with mode=ro. The tables
desk_positions, positions, trade_journal, account_cache and app_settings are never read.

Stages: paper, down, spread, midcp, delay, history. Run from the main checkout:
  PYTHONPATH=src:scripts:. nice -n 10 .venv/bin/python -W ignore \
      <worktree>/scripts/live_alerts_check.py <stage>
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd

from onelot.clock import IST as APP_IST

ROOT = Path.cwd()
DB = ROOT / "data" / "onelot.db"
IST = APP_IST  # the app's one zone object (a fixed +05:30 offset since US-096)
CUT = dt.date(2026, 11, 13)


def ro(path: Path = DB) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)


def ist(s: pd.Series) -> pd.Series:
    """Stored times are naive UTC."""
    return pd.to_datetime(s, format="mixed").dt.tz_localize("UTC").dt.tz_convert(IST)


def tstat(x: np.ndarray, d: np.ndarray) -> float:
    """t of the mean, all trades of one day one cluster (cluster-robust standard error)."""
    x = np.asarray(x, float)
    if len(x) < 3 or len(set(d)) < 2:
        return float("nan")
    m = x.mean()
    s = pd.Series(x - m).groupby(np.asarray(d)).sum()
    se = float(np.sqrt((s**2).sum())) / len(x)
    return m / se if se > 0 else float("nan")


def line(name: str, x: pd.Series, d: pd.Series, inr: pd.Series | None = None) -> str:
    x = x.dropna()
    if not len(x):
        return f"{name:38s} 0 trades"
    d = d.loc[x.index]
    win, loss = x[x > 0], x[x <= 0]
    pay = win.mean() / abs(loss.mean()) if len(win) and len(loss) and loss.mean() else np.nan
    extra = f"  Rs {inr.loc[x.index].mean():+9.1f}" if inr is not None else ""
    return (f"{name:38s} {len(x):4d} tr {d.nunique():3d} d {x.mean():+7.2f}% "
            f"t {tstat(x.to_numpy(), d.to_numpy()):+5.1f} win {100 * (x > 0).mean():5.1f}% "
            f"pay {pay:5.2f}{extra}")  # fmt: skip


# ----------------------------------------------------------------------------- 1. paper


def stage_paper() -> None:
    with ro() as c:
        pt = pd.read_sql(
            "select alert_id, status, close_reason, kind, side, strategy, timeframe, qty,"
            " entry_ts, entry_paise, net_paise from paper_trades",
            c,
        )
        sg = pd.read_sql("select id, created_at, status from desk_signals", c)
    sg["created"] = ist(sg.created_at)
    sg["cday"] = sg.created.dt.date
    print("alerts in desk_signals:", len(sg), "| by status:", sg.status.value_counts().to_dict())
    print("alerts by creation day:", sg.groupby("cday").size().to_dict())
    print("paper_trades rows:", len(pt), "| status:", pt.status.value_counts().to_dict())
    pt = pt.merge(sg[["id", "cday"]], left_on="alert_id", right_on="id")
    pt["slot"] = pt.strategy + " " + pt.timeframe
    cl = pt[pt.status == "CLOSED"].copy()
    cl["eday"] = ist(cl.entry_ts).dt.date
    cl["inr"] = cl.net_paise / 100
    cl["pct"] = cl.net_paise / (cl.entry_paise * cl.qty) * 100
    print("kinds:", cl.kind.value_counts().to_dict(), "sides:", cl.side.value_counts().to_dict())
    for lab, g in (("BEFORE 13 Nov (alert day)", cl[cl.cday < CUT]),
                   ("FROM 13 Nov", cl[cl.cday >= CUT]), ("ALL", cl)):  # fmt: skip
        print(f"== {lab}")
        print(line("all", g.pct, g.eday, g.inr))
        for k, gg in g.groupby("kind"):
            print(line(f"kind {k}", gg.pct, gg.eday, gg.inr))
        for k, gg in g.groupby("slot"):
            print(line(f"slot {k}", gg.pct, gg.eday, gg.inr))
        for k, gg in g.groupby(["kind", "slot"]):
            print(line(f"  {k[0]} / {k[1]}", gg.pct, gg.eday, gg.inr))
        ix = g[g.kind == "INDEX"]
        for k, gg in ix.groupby("side"):
            print(line(f"index side {k}", gg.pct, gg.eday))
        for k, gg in g.groupby("close_reason"):
            print(line(f"close {k}", gg.pct, gg.eday))
    ix = cl[(cl.cday < CUT) & (cl.kind == "INDEX")]
    print("index before 13 Nov, per slot: ")
    for k, gg in ix.groupby("slot"):
        print(line(f"  {k}", gg.pct, gg.eday, gg.inr))
    print("alerts before / from 13 Nov by slot")
    sg2 = sg.merge(pt[["alert_id", "slot", "kind"]], left_on="id", right_on="alert_id", how="left")
    print(
        sg2.assign(per=np.where(sg2.cday < CUT, "before", "from")).groupby(["per", "slot"]).size()
    )


# ----------------------------------------------------------------------------- 2. down days


def stage_down() -> None:
    days = pd.bdate_range("2026-11-02", "2026-11-24").date
    out = {}
    with ro() as c:

        def per_day(sql: str, col: str = "ts") -> pd.DataFrame:
            df = pd.read_sql(sql, c)
            df["t"] = ist(df[col])
            return df

        sources = {
            "sentiment_scores": ("select created_at ts from sentiment_scores", "ts"),
            "fill_quotes": ("select ts from fill_quotes", "ts"),
            "engine_events": ("select ts from engine_events", "ts"),
            "paper_events": ("select ts from paper_events", "ts"),
            "gate_audit": ("select ts from gate_audit where ts >= '2026-11-01'", "ts"),
            "signal_evals": ("select ts from signal_evaluations", "ts"),
            "snapshots": ("select taken_at ts from market_snapshots", "ts"),
            "delivery_evals": ("select evaluated_at ts from delivery_evaluations", "ts"),
        }
        for name, (sql, col) in sources.items():
            df = per_day(sql, col)
            df = df[df.t.dt.hour.between(9, 15)]  # inside the session hours, IST
            out[name] = df.groupby(df.t.dt.date).t.agg(
                lambda s: f"{len(s)}:{s.min():%H:%M}-{s.max():%H:%M}"
            )
        bars = pd.read_sql(
            "select ts, source, count(*) n from bars where series_key = 'NIFTY' and resolution ="
            " '1m' and ts >= '2026-11-01' group by date(ts, '+330 minutes'), source",
            c,
        )
        sg = pd.read_sql("select created_at ts from desk_signals", c)
    out["alerts"] = ist(sg.ts).dt.date.value_counts().sort_index().astype(str)
    tab = pd.DataFrame(out).reindex(days).fillna("-")
    pd.set_option("display.width", 250, "display.max_columns", 20)
    print(tab.to_string())
    bars["day"] = ist(bars.ts).dt.date
    print("NIFTY 1m bars by day and source (all 'dhan' = cannot tell live from topped up):")
    print(bars.groupby(["day", "source"]).n.sum().to_string())


# ----------------------------------------------------------------------------- 3. spread


def stage_spread() -> None:
    with ro() as c:
        q = pd.read_sql("select * from fill_quotes", c)
    q["t"] = ist(q.ts)
    q["day"] = q.t.dt.date
    print("rows by kind:", q.kind.value_counts().to_dict(), "| days:", sorted(set(q.day)))
    print("segments:", q.segment.value_counts().to_dict())
    bid_ok = (q.bid > 0) & (q.ask > 0) & (q.ask >= q.bid)
    q["half_rs"] = np.where(bid_ok, (q.ask - q.bid) / 2, np.nan)
    q["mid"] = (q.ask + q.bid) / 2
    q["half_pct"] = q.half_rs / q.mid * 100
    print("quotes with a valid two-sided book:", int(bid_ok.sum()), "of", len(q))
    opt = q[(q.kind == "SAMPLE") & q.segment.str.contains("FNO")]
    print("option SAMPLE rows", len(opt), "valid", int(opt.half_pct.notna().sum()))
    v = opt[opt.half_pct.notna()]
    for k, g in v.groupby("instrument_key"):
        print(f"{k:16s} n {len(g):4d} days {g.day.nunique()} prem {g.mid.median():8.1f} "
              f"Rs med {g.half_rs.median():7.2f} p90 {g.half_rs.quantile(.9):7.2f} | "
              f"% med {g.half_pct.median():5.2f} p90 {g.half_pct.quantile(.9):5.2f}")  # fmt: skip
    hm = v.t.dt.strftime("%H:%M")
    print("by time of day, all indices (and SENSEX):")
    for lo, hi in (("09:15", "10:00"), ("10:00", "11:30"), ("11:30", "13:00"), ("13:00", "15:31")):
        g = v[(hm >= lo) & (hm < hi)]
        s = g[g.instrument_key.str.startswith("SENSEX")]
        print(f"  {lo}-{hi} all n {len(g)} med {g.half_pct.median():.2f} | SENSEX n {len(s)} "
              f"med {s.half_pct.median():.2f} days {s.day.nunique()}")  # fmt: skip
    # the "before 10:00" for the first sample of the day only
    s10 = v[(v.instrument_key.str.startswith("SENSEX")) & (hm < "10:00")]
    print("SENSEX before 10:00 per day:", s10.groupby("day").half_pct.median().round(2).to_dict())
    stk = q[(q.kind == "SAMPLE") & ~q.segment.str.contains("FNO") & q.half_pct.notna()]
    by = stk.groupby("instrument_key").half_pct.median()
    print(f"stocks: {len(stk)} quotes, {stk.instrument_key.nunique()} stocks, {stk.day.nunique()}"
          f" days; median of per-stock medians {by.median():.4f} range {by.min():.4f} to "
          f"{by.max():.4f}; pooled median {stk.half_pct.median():.4f}, mean {stk.half_pct.mean():.4f}")  # fmt: skip
    al = q[(q.kind == "ALERT") & q.segment.str.contains("FNO")]
    print("ALERT option quotes:", len(al), "valid", int(al.half_pct.notna().sum()),
          "median half %", round(al.half_pct.median(), 2))  # fmt: skip
    fo = q[q.kind == "FOLLOW"].merge(
        q[q.kind == "ALERT"][["alert_id", "ask"]], on="alert_id", suffixes=("", "0")
    )
    fo = fo[(fo.ask > 0) & (fo.ask0 > 0)]
    fo["worse"] = (fo.ask - fo.ask0) / fo.ask0 * 100
    for off, g in fo.groupby("offset_sec"):
        print(f"follow {off}s: n {len(g)} median {g.worse.median():+.2f} worse in "
              f"{int((g.worse > 0).sum())}")  # fmt: skip


# ----------------------------------------------------------------------------- 6. midcpnifty


def supertrend(h: np.ndarray, lo: np.ndarray, c: np.ndarray, n: int, mult: float) -> np.ndarray:
    """Direction (+1 up, -1 down, 0 none): Wilder ATR seeded by a simple mean, final bands."""
    k = len(c)
    pc = np.r_[np.nan, c[:-1]]
    tr = np.maximum.reduce([h - lo, np.abs(h - pc), np.abs(lo - pc)])
    tr[0] = np.nan
    atr = np.full(k, np.nan)
    if k <= n:
        return np.zeros(k, int)
    atr[n] = tr[1 : n + 1].mean()
    for i in range(n + 1, k):
        atr[i] = (atr[i - 1] * (n - 1) + tr[i]) / n
    mid = (h + lo) / 2
    ub, lb = mid + mult * atr, mid - mult * atr
    fub, flb = ub.copy(), lb.copy()
    d = np.zeros(k, int)
    d[n] = 1 if c[n] >= mid[n] else -1
    for i in range(n + 1, k):
        fub[i] = ub[i] if (ub[i] < fub[i - 1] or c[i - 1] > fub[i - 1]) else fub[i - 1]
        flb[i] = lb[i] if (lb[i] > flb[i - 1] or c[i - 1] < flb[i - 1]) else flb[i - 1]
        if d[i - 1] == 1:
            d[i] = -1 if c[i] < flb[i] else 1
        else:
            d[i] = 1 if c[i] > fub[i] else -1
    d[: 5 * n] = 0  # five times the period of warm-up, as the product does
    return d


def ladder_dirs(m1: pd.DataFrame, at: pd.Timestamp) -> dict[str, int]:
    """Direction of each timeframe's last bar CLOSED at or before `at` (bar start + length)."""
    out = {}
    for tf in (3, 5, 10, 15, 30, 60, 240):
        b = m1.resample(f"{tf}min", origin="start_day", offset="9h15min", closed="left",
                        label="left").agg({"open": "first", "high": "max", "low": "min",
                                           "close": "last"}).dropna()  # fmt: skip
        d = supertrend(b.high.to_numpy(), b.low.to_numpy(), b.close.to_numpy(), 14, 1.7)
        closed = (b.index + pd.Timedelta(minutes=tf)) <= at
        out[f"{tf}m"] = int(d[np.flatnonzero(closed)[-1]])
    return out


def load_1m(series: str, since: str, until: str) -> pd.DataFrame:
    with ro() as c:
        b = pd.read_sql(
            "select ts, open, high, low, close from bars where series_key = ? and resolution ="
            " '1m' and ts >= ? and ts < ? order by ts",
            c, params=(series, since, until),
        )  # fmt: skip
    b["ts"] = ist(b.ts)
    return b.set_index("ts")


def stage_midcp() -> None:
    with ro() as c:
        sg = pd.read_sql(
            "select id, created_at, instrument_key, side, bar_ts from desk_signals where strategy"
            " = 'microtrend'", c)  # fmt: skip
    sg["created"] = ist(sg.created_at)
    sg["bar"] = ist(sg.bar_ts)
    print(sg[["id", "instrument_key", "side", "created", "bar"]].to_string())
    for r in sg.itertuples():
        series = r.instrument_key.replace("_OPT", "")
        m1 = load_1m(series, "2026-06-01", "2026-11-24")
        # the 3m bar that closed at the alert: decide at its close (bar start + 3 minutes)
        at = r.bar + pd.Timedelta(minutes=3)
        last = m1.index[m1.index <= r.created][-1]
        print(f"\n{series}: alert bar {r.bar:%H:%M} closes {at:%H:%M}; last stored 1m bar "
              f"at or before the alert {last:%H:%M}; bars that morning: "
              f"{int((m1.index.date == r.bar.date()).sum())}")  # fmt: skip
        for label, t in (("previous 3m close", at - pd.Timedelta(minutes=3)), ("at alert", at)):
            dirs = ladder_dirs(m1[m1.index < t], t)
            nd = sum(v == -1 for v in dirs.values())
            nu = sum(v == 1 for v in dirs.values())
            print(f"  {label} {t:%H:%M}: {dirs} -> {nu} up, {nd} down")
        # what the desk may have held: the morning bars at every second minute only (the
        # regrade event lists 09:15, 09:17, ... as the bars it had before 10:57)
        mod = (m1.index.hour * 60 + m1.index.minute - 555) % 2
        thin = m1[
            ~(
                (m1.index.date == r.bar.date())
                & (m1.index.hour * 60 + m1.index.minute < 658)
                & (mod == 1)
            )
        ]
        dirs = ladder_dirs(thin[thin.index < at], at)
        nd = sum(v == -1 for v in dirs.values())
        print(
            f"  thinned morning (drop the even minutes before 10:58) {at:%H:%M}: {dirs} -> {nd} down"
        )


# ----------------------------------------------------------------------------- 4. delay


def stage_delay() -> None:
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]  # the main checkout's harness
    import mtf_bruteforce as h  # noqa: PLC0415

    assert Path(h.__file__).resolve().parents[1] == ROOT.resolve()
    from sqlalchemy import create_engine  # noqa: PLC0415

    from onelot.config import CostConfig  # noqa: PLC0415
    from onelot.lab.runner import DataCache  # noqa: PLC0415
    from onelot.settings import current_settings  # noqa: PLC0415

    uri = f"file:{quote(str(DB))}?mode=ro"
    h._cache = DataCache(
        engine=create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
    )
    h._settings = current_settings()
    h._cost_cfg = CostConfig.load(h._settings.project_root / "config" / "costs.yaml")
    with ro() as c:
        sg = pd.read_sql("select id, created_at, series_key, kind, side from desk_signals", c)
        pt = pd.read_sql(
            "select t.alert_id, t.status, t.close_reason, t.exit_ts, e.detail from paper_trades t"
            " join paper_events e on e.trade_id = t.id and e.kind = 'CLOSED' where t.status ="
            " 'CLOSED' and t.kind = 'INDEX'", c)  # fmt: skip
    sg["created"] = ist(sg.created_at)
    pt["exit"] = ist(pt.exit_ts)
    a = sg.merge(pt, left_on="id", right_on="alert_id")
    a = a[a.series_key != "MIDCPNIFTY"]
    print("index alerts closed, not MIDCPNIFTY:", len(a), "days", a.created.dt.date.nunique())
    preps: dict = {}
    res = {k: [] for k in ("rec", "d1", "d3", "d5")}
    days = []
    for r in a.itertuples():
        inst = r.series_key + "_OPT"
        if inst not in preps:
            old = h.T_END
            h.T_END = dt.date(2026, 11, 23)
            try:
                preps[inst] = h.prepare(inst, tfs=("60m",))
            finally:
                h.T_END = old
        p = preps[inst]
        side = 1 if r.side in ("BUY", "BUY_CE") else -1
        ns = int(r.created.tz_convert("UTC").value)
        e = int(np.searchsorted(p.ns, ns, side="right"))  # first bar strictly after the alert
        s = int(p.sess[e])
        xi = int(p.xi[s])
        xr = int(np.searchsorted(p.ns, int(r.exit.tz_convert("UTC").value)))
        touch = r.close_reason in ("STOP", "TRAIL_STOP", "TARGET")
        if r.close_reason == "SESSION_END" or xr >= xi:
            xr, px = xi, float(p.xp[s])
        else:
            px = float(p.o[xr])
        if touch and xr < xi:  # a stop or target is filled at its own level, not the bar open
            px = json.loads(r.detail)["price_paise"] / 100
            res.setdefault("touch", []).append(r.id)
        row = {}
        for k, dly in (("rec", 0), ("d1", 1), ("d3", 3), ("d5", 5)):
            if e + dly >= xr:
                row[k] = np.nan
                continue
            o = h.pnl(p, side, 0, np.array([e + dly]), np.array([xr]), np.array([px]),
                      np.array([p.ns[xr]]))  # fmt: skip
            row[k] = float(o["ret"][0])
        for k in ("rec", "d1", "d3", "d5"):
            res[k].append(row[k])
        days.append(str(p.sess_dates[s]))
    d = pd.Series(days)
    print("touch exits priced at the recorded stop level:", len(res.get("touch", [])))
    for k in ("rec", "d1", "d3", "d5"):
        x = pd.Series(res[k])
        print(line(f"delay {k}", x, d))


# ----------------------------------------------------------------------------- 5. history

PERIODS = (("TUNE", dt.date(2024, 11, 7)), ("CHOOSE", dt.date(2025, 11, 7)),
           ("TEST", dt.date(2026, 11, 7)))  # fmt: skip


_CACHE: list = []


def daily(series: str, before: dt.date | None = None, raw: bool = False) -> pd.DataFrame:
    """Daily bars by session date. raw: the stored 1d rows; else the harness's own daily bars
    (built from the cleaned one-minute bars, as every backtest reads them)."""
    if raw:
        with ro() as c:
            d = pd.read_sql(
                "select ts, high, low, close from bars where series_key = ? and resolution ="
                " '1d' order by ts", c, params=(series,))  # fmt: skip
        d["day"] = ist(d.ts).dt.date
        d = d.set_index("day")
    else:
        if not _CACHE:
            sys.path[:0] = [str(ROOT / "src"), str(ROOT / "scripts")]
            from sqlalchemy import create_engine  # noqa: PLC0415

            from onelot.lab.runner import DataCache  # noqa: PLC0415

            uri = f"file:{quote(str(DB))}?mode=ro"
            eng = create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
            _CACHE.append(DataCache(engine=eng))
        d = _CACHE[0].bars(series, "1d")[["high", "low", "close"]].copy()
        d["day"] = [x.date() for x in d.index]
        d = d.set_index("day")
    return d if before is None else d[d.index < before]


def active_flags(d: pd.DataFrame) -> pd.Series:
    """True where the session's range (of the previous close) beat the median of the 20 before."""
    prev = d.close.shift(1)
    rng = (d.high - d.low) / prev
    med = rng.shift(1).rolling(20).median()
    out = (rng > med).where(med.notna() & rng.notna())
    out.iloc[:21] = np.nan
    return out


def vix_rose(v: pd.DataFrame) -> pd.Series:
    return (v.close > v.close.shift(1)).where(v.close.shift(1).notna())


def prev_flag(day: dt.date, ser: pd.Series) -> float:
    """The flag of the last session strictly before `day` (nan if none)."""
    k = ser.index.searchsorted(day) - 1
    return float(ser.iloc[k]) if k >= 0 else float("nan")


def cell(g: pd.DataFrame) -> str:
    out = []
    for lab, hi in PERIODS:
        lo = {"TUNE": dt.date(1900, 1, 1), "CHOOSE": PERIODS[0][1], "TEST": PERIODS[1][1]}[lab]
        x = g[(g.dday > lo) & (g.dday <= hi)]
        out.append(f"{len(x):4d}/{x.day.nunique():3d} {x.ret.mean():+6.2f} "
                   f"(t {tstat(x.ret.to_numpy(), x.day.to_numpy()):+4.1f})")  # fmt: skip
    return " | ".join(out) + f" | all {g.ret.mean():+6.2f} (t {tstat(g.ret.to_numpy(), g.day.to_numpy()):+4.1f})"  # fmt: skip


def stage_history() -> None:
    raw = len(sys.argv) > 2 and sys.argv[2] == "raw"
    print("daily data:", "raw stored 1d rows" if raw else "the harness daily bars")
    mt = ROOT / "data" / "research" / "microtrend_2026-11-13"
    t = pd.read_csv(mt / "trades.csv")
    t = t[(t.need == 3) & t.inst.isin(["NIFTY_OPT", "BANKNIFTY_OPT", "FINNIFTY_OPT", "SENSEX_OPT"])]
    t = t.reset_index(drop=True)
    t["dday"] = pd.to_datetime(t.day).dt.date
    print("trades", len(t), "periods in file:", t.period.value_counts().sort_index().to_dict())
    mine = np.select([t.dday <= PERIODS[0][1], t.dday <= PERIODS[1][1], t.dday <= PERIODS[2][1]],
                     [0, 1, 2], 3)  # fmt: skip
    print("my period equals the file's:", bool((mine == t.period.to_numpy()).all()),
          "| beyond TEST:", int((mine == 3).sum()))  # fmt: skip
    vx = daily("INDIAVIX", raw=raw)
    rose = vix_rose(vx)
    t["first_hour"] = t.minute < 10 * 60 + 15
    t["act"] = np.nan
    t["vfell"] = np.nan
    act_all, truncated = {}, 0
    for inst, g in t.groupby("inst"):
        d = daily(inst.replace("_OPT", ""), raw=raw)
        act = active_flags(d)
        act_all[inst] = (d, act)
        t.loc[g.index, "act"] = [prev_flag(x, act) for x in g.dday]
        # the VIX flag is read on the previous session of THIS instrument's calendar
        pv = [d.index[d.index.searchsorted(x) - 1] for x in g.dday]
        t.loc[g.index, "vfell"] = [
            1.0 - rose.get(x, np.nan) if x in rose.index else np.nan for x in pv
        ]
        t.loc[g.index, "prev_day"] = pv
    assert (t.prev_day < t.dday).all(), "a previous session that is not before the entry day"
    # truncation test: cut every daily series at the entry day (no data of that day or after)
    rng = np.random.default_rng(5)
    bad = 0
    for i in rng.choice(len(t), 250, replace=False):
        r = t.iloc[i]
        d = daily(r.inst.replace("_OPT", ""), before=r.dday, raw=raw)
        a2 = prev_flag(r.dday, active_flags(d))
        v2 = daily("INDIAVIX", before=r.dday, raw=raw)
        pv = d.index[-1]
        rs = vix_rose(v2)
        f2 = 1.0 - rs.get(pv, np.nan) if pv in rs.index else np.nan
        bad += not (np.isclose(a2, r.act, equal_nan=True) and np.isclose(f2, r.vfell, equal_nan=True))  # fmt: skip
        truncated += 1
    print(f"truncation test: {truncated} random trades, {bad} flags changed when the data after "
          "the entry day was cut")  # fmt: skip
    print("unknown flags: active", int(t.act.isna().sum()), "vix", int(t.vfell.isna().sum()))
    hs = pd.read_csv(ROOT / "data" / "research" / "live_alerts_2026-11-24" / "history.csv")
    hs = hs[hs.inst.isin(t.inst.unique())]
    print("flags agree with the study's cached file: first_hour",
          bool((hs.first_hour.to_numpy() == t.first_hour.to_numpy()).all()),
          "| prev_active", int((hs.prev_active.to_numpy() == (t.act.to_numpy() == 1)).sum()), "of", len(t),
          "| vix_fell", int((hs.vix_fell_prev.to_numpy() == (t.vfell.to_numpy() == 1)).sum()), "of", len(t))  # fmt: skip
    dif = hs.assign(mine=(t.act.to_numpy() == 1), pd_=t.prev_day.to_numpy())
    dif = dif[dif.prev_active != dif.mine]
    print("prev_active differences by instrument:", dif.inst.value_counts().to_dict())
    print(dif[["inst", "day", "pd_", "prev_active", "mine"]].head(12).to_string())
    dv = hs.assign(mine=(t.vfell.to_numpy() == 1), pd_=t.prev_day.to_numpy())
    dv = dv[dv.vix_fell_prev != dv.mine]
    print("vix_fell differences by instrument:", dv.inst.value_counts().to_dict())
    print(dv[["inst", "day", "pd_", "vix_fell_prev", "mine"]].head(12).to_string())
    print("TUNE / CHOOSE / TEST  n/days mean (t), model % of premium, four owner indices")
    print(" all          ", cell(t))
    for name, m in (("H1 before 10:15", t.first_hour), ("H1 opposite", ~t.first_hour),
                    ("H3 prev active", t.act == 1), ("H3 prev calm", t.act == 0),
                    ("H4 VIX fell", t.vfell == 1), ("H4 VIX rose", t.vfell == 0)):  # fmt: skip
        print(f"{name:14s}", cell(t[m]))
    for name, m in (("H3 act, unknown", t.act.isna()), ("H4 vix, unknown", t.vfell.isna())):
        print(f"{name:14s} n {int(m.sum())}")


if __name__ == "__main__":
    {"paper": stage_paper, "down": stage_down, "spread": stage_spread, "midcp": stage_midcp,
     "delay": stage_delay, "history": stage_history}[sys.argv[1]]()  # fmt: skip
